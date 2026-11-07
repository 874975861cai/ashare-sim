#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周报生成器：读账本 → 出结论 → 判断要不要动参数。

设计原则（写死在代码里，不靠每次临场判断）：

    1. 样本不够就不调参。门槛：累计 >= 20 笔 且 >= 10 个交易日。
       理由：小样本下任何参数优化都是拟合噪音，越调越差。

    2. 就算达标，新参数也必须在回测里跑赢旧参数才采纳，
       否则维持原样。宁可不动，不可乱动。

    3. 过夜持仓、超频交易、单日大亏，一律标为「规则偏离」，
       这些比盈亏数字更重要——它们说明执行出了问题。

用法：
    python engine/weekly.py            # 最近 5 个交易日
    python engine/weekly.py --days=10
    python engine/weekly.py --quiet    # 只写文件不打印
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest   # noqa: E402
import broker     # noqa: E402
import strategy   # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL = os.path.join(BASE, "journal")

MIN_TRADES = 20        # 样本门槛：笔数（仅日内模式）
MIN_DAYS = 10          # 样本门槛：交易日
DAY_LOSS_LIMIT = -0.01  # 单日亏损超 1% 视为异常

# 2026-11-07：持有型的评审门槛不能按笔数算。
#   一年成交 1~2 笔，「20 笔」在结构上不可达 —— 代码从 10-30 就在报告里
#   提示「门槛需要人工重新定义，本脚本不擅自放宽」，而没人去改，结果
#   10-28 到 11-07 连续两周没有任何复核。一条永远不响的警报等于没有警报。
#   持有型真正的风险不是「样本不足就调参」，是「长期不看」。改按交易日计：
#   20 个交易日 ≈ 1 个自然月，到点必须人工复核一次。
HOLD_REVIEW_DAYS = 20
REVIEW_FILE = os.path.join(BASE, "state", "review.json")


# --------------------------------------------------------------------------
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_all():
    acc = broker.load_account()
    trades = read_csv(broker.TRADE_FILE)
    equity = read_csv(broker.EQUITY_FILE)
    return acc, trades, equity


def fnum(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
def summarize(trades, equity, days=5):
    """统计最近 N 个交易日 + 累计。"""
    dates = [e["date"] for e in equity if e.get("date")]
    recent_dates = set(dates[-days:]) if dates else set()

    sells_all = [t for t in trades if t.get("side") == "SELL"]
    sells_recent = [t for t in sells_all
                    if t.get("datetime", "")[:10] in recent_dates]

    def stat(rows):
        n = len(rows)
        wins = [t for t in rows if fnum(t.get("pnl")) > 0]
        pnls = [fnum(t.get("pnl")) for t in rows]
        return {
            "n": n,
            "win": len(wins),
            "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
            "pnl": round(sum(pnls), 2) if pnls else 0.0,
            "avg": round(sum(pnls) / n, 2) if n else 0.0,
            "best": round(max(pnls), 2) if pnls else 0.0,
            "worst": round(min(pnls), 2) if pnls else 0.0,
        }

    eq_recent = [e for e in equity if e.get("date") in recent_dates]
    day_pnls = [fnum(e.get("day_pnl")) for e in eq_recent]

    return {
        "recent_dates": sorted(recent_dates),
        "recent": stat(sells_recent),
        "total": stat(sells_all),
        "n_days": len(dates),
        "n_days_recent": len(recent_dates),
        "flat_days": sum(1 for e in eq_recent if fnum(e.get("day_pnl")) == 0),
        "day_pnls": day_pnls,
    }


def deviations(trades, equity, acc):
    """规则偏离检查：比盈亏更能说明问题。

    返回 (issues, notes)：
      issues —— 真正需要处理的违规（计入「另有 N 项规则偏离」）
      notes  —— 需要解释但不构成违规的情况（如持有型的市值波动）
    """
    issues = []
    notes = []

    # 1) 过夜持仓
    #    2026-10-23 修：股票型 ETF 是 T+1，当天买入的份额当天根本卖不掉，
    #    过夜是交易规则强制的，不是执行偏离。只有 T+0 品种（跨境/黄金/债券）
    #    才存在「本来可以日内平掉却没平掉」这种真正的偏离。
    #    不加这个区分，周报会天天报「违规」，把真偏离淹掉。
    forced = [c for c in acc.get("positions", {}) if not broker.is_t0_etf(c)]
    avoidable = [c for c in acc.get("positions", {}) if broker.is_t0_etf(c)]
    if avoidable:
        issues.append("**存在可平未平的过夜持仓**：%s（T+0 品种，本可日内平仓）"
                      % "、".join(avoidable))

    # 2) 单日交易超频
    per_day = {}
    for t in trades:
        if t.get("side") != "BUY":
            continue
        d = t.get("datetime", "")[:10]
        per_day[d] = per_day.get(d, 0) + 1
    cap = strategy.PARAMS.get("max_trades", 3)
    for d, n in per_day.items():
        if n > cap:
            issues.append("**单日超频**：%s 买入 %d 次，上限 %d" % (d, n, cap))

    # 3) 单日大亏
    #    2026-11-06 修：-1% 这条线是给日内策略定的。持有型一次建仓拿数月，
    #    标的单日跌 2% 是行情，不是「违规」——把它列进「规则偏离需处理」，
    #    等于每周都在报一件本来就该发生的事，真偏离会被这类噪音淹掉。
    #    持有型真正的控制线是 hold_stop（距成本），所以这里改口径：
    #      · 建仓之后的浮亏 → 只作说明（附距 hold_stop 还有多远）
    #      · 建仓之前的亏损 → 标为历史（属已停用的日内策略）
    mode = str(strategy.PARAMS.get("mode") or "intraday")
    hold_start = None
    avg_cost = 0.0
    shares = 0
    if mode == "hold":
        lot_dates, cost_amt = [], 0.0
        for pos in acc.get("positions", {}).values():
            for lot in pos.get("lots", []):
                if lot.get("date"):
                    lot_dates.append(lot["date"])
                sh = fnum(lot.get("shares"))
                shares += sh
                cost_amt += sh * fnum(lot.get("price"))
        hold_start = min(lot_dates) if lot_dates else None
        avg_cost = (cost_amt / shares) if shares else 0.0

    for e in equity:
        dp = fnum(e.get("day_pnl"))
        eq = fnum(e.get("equity"), 10000)
        if not (eq and dp / eq < DAY_LOSS_LIMIT):
            continue
        d = str(e.get("date") or "")
        pct = dp / eq * 100
        if mode != "hold":
            issues.append("**单日大亏**：%s 亏 ¥%.2f（%.2f%%）" % (d, dp, pct))
            continue
        if hold_start and d >= hold_start:
            gap = None
            if avg_cost > 0 and shares > 0:
                mv = eq - fnum(acc.get("cash"))
                gap = (mv / shares / avg_cost - 1) * 100
            tail = ("距 hold_stop %.0f%% 还有 %.2f 个百分点"
                    % (fnum(strategy.PARAMS.get("hold_stop")) * 100,
                       gap - fnum(strategy.PARAMS.get("hold_stop")) * 100)
                    if gap is not None else "控制线为 hold_stop")
            notes.append("（浮亏说明）%s 单日 ¥%.2f（%.2f%%），为持有型仓位的市值波动，"
                         "非执行偏离；%s。" % (d, dp, pct, tail))
        else:
            notes.append("（历史）%s 单日 ¥%.2f（%.2f%%），发生在持有型建仓（%s）之前，"
                         "归属已停用的日内策略，不属本期偏离。"
                         % (d, dp, pct, hold_start or "未建仓"))

    return issues, notes


def review_age(equity):
    """距上次**人工评审**过了多少个交易日。

    只认 state/review.json 里登记的评审。不拿自动生成的周报当评审记录 ——
    周报每天都能生成，用它当「已复核」等于自欺，两周不复盘的教训就在这。
    """
    dates = [r.get("date") for r in equity if r.get("date")]
    if not dates:
        return "", 0
    last = ""
    try:
        with open(REVIEW_FILE, "r", encoding="utf-8") as f:
            last = (json.load(f) or {}).get("last_review") or ""
    except Exception:  # noqa: BLE001
        last = ""
    after = [d for d in dates if d > last] if last else list(dates)
    return last, len(after)


def gate(n_trades, n_days, hold_mode=False, days_since=None, last_review=""):
    """评审门槛。两种模式问的问题不同，不能共用一个数。

    日内模式问「样本够不够调参」→ 笔数 + 日数。
    持有模式问「多久没看过了」→ 交易日数。持有型没有可日常调的参数，
    它真正的风险是没人看，所以门槛必须按时间算，不能按成交笔数算。
    """
    if hold_mode:
        n = days_since or 0
        return {
            "ok": n >= HOLD_REVIEW_DAYS,
            "mode": "hold",
            "days_since": n,
            "last_review": last_review,
            "need_days": max(0, HOLD_REVIEW_DAYS - n),
            "n_trades": n_trades, "n_days": n_days, "need_trades": 0,
        }
    ok = n_trades >= MIN_TRADES and n_days >= MIN_DAYS
    return {
        "ok": ok,
        "mode": "daytrade",
        "n_trades": n_trades, "n_days": n_days,
        "need_trades": max(0, MIN_TRADES - n_trades),
        "need_days": max(0, MIN_DAYS - n_days),
    }


# --------------------------------------------------------------------------
def pool_audit(ndays=5):
    """池子体检：不看成交，直接对每个「信号段」按真实止盈止损判定出场。

    为什么需要这个：4 天只成交 2 笔，样本量根本不够评审参数。但每个交易日
    每只标的都会出现多次「偏离达标」的时刻——把这些信号段各自按 TP/SL/时间
    止损走一遍，样本立刻放大到几十上百个，才能在周级尺度上看清哪只标的
    真的会回归、哪只只是一路阴跌。

    与「未来最大涨幅」的区别（踩过的坑）：最大涨幅测不出「先跌 2% 被止损
    打掉、之后才涨」这种情况，会系统性高估信号质量。这里严格按时间顺序
    逐分钟推进，先碰到哪个就按哪个算。

    成本：1 万本金满仓，佣金双边各 5 元 = 0.10%，加 tick 摩擦 ≈ 0.134%。
    """
    try:
        data = backtest.load_pool(strategy.CORE_POOL, ndays=ndays,
                                  verbose=False, offline=True)
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None

    P = strategy.PARAMS
    COST = 0.134
    today = datetime.now().strftime("%Y-%m-%d")
    days = [d for d in backtest.trading_days(data) if d < today]
    if not days:
        return None

    out = []
    for code, v in data.items():
        per_day = v.get("days") or {}
        sig = tp = sl = to = 0
        rets = []
        amps = []
        for day, seq in per_day.items():
            if day not in days or len(seq) < 100:
                continue
            amps.append((max(x["price"] for x in seq)
                         / min(x["price"] for x in seq) - 1) * 100)
            for i in range(22, len(seq)):
                dev = (seq[i]["price"] / seq[i]["avg"] - 1) * 100 if seq[i]["avg"] else 0
                if dev > -P["dev_enter"] * 100:
                    continue
                # 段首去重：连续达标的分钟算同一个信号段
                if i > 0:
                    pv = (seq[i-1]["price"] / seq[i-1]["avg"] - 1) * 100 if seq[i-1]["avg"] else 0
                    if pv <= -P["dev_enter"] * 100:
                        continue

                px = seq[i]["price"]
                tp_px = px * (1 + P["take_profit"])
                sl_px = px * (1 - P["stop_loss"])
                end = min(i + 1 + int(P["max_hold_min"]), len(seq))
                exit_px, kind = None, "timeout"
                for j in range(i + 1, end):
                    if seq[j]["price"] >= tp_px:
                        exit_px, kind = tp_px, "tp"; break
                    if seq[j]["price"] <= sl_px:
                        exit_px, kind = sl_px, "sl"; break
                if exit_px is None:
                    exit_px = seq[end - 1]["price"]
                sig += 1
                if kind == "tp":
                    tp += 1
                elif kind == "sl":
                    sl += 1
                else:
                    to += 1
                rets.append((exit_px / px - 1) * 100 - COST)

        if not sig:
            continue
        out.append({
            "code": code,
            "name": (v.get("name") or code)[:12],
            "signals": sig,
            "tp": tp, "sl": sl, "timeout": to,
            "tp_rate": tp / sig * 100,
            "avg_ret": sum(rets) / len(rets),
            "amp": sum(amps) / len(amps) if amps else 0,
        })

    out.sort(key=lambda x: -x["avg_ret"])
    return {"days": days, "rows": out, "cost": COST}


def audit_verdict(audit):
    """把体检结果翻译成人话。样本太少时明确说「先别动」。"""
    if not audit or not audit["rows"]:
        return None
    rows = audit["rows"]
    n = sum(r["signals"] for r in rows)
    if len(audit["days"]) < 5:
        return ("样本只有 %d 个交易日，**不足以做池子调整**，先积累。" % len(audit["days"]))

    good = [r for r in rows if r["avg_ret"] > 0.05]
    bad = [r for r in rows if r["avg_ret"] < -0.05]
    msg = []
    if good:
        msg.append("正期望标的：%s" % "、".join(
            "%s(%+.2f%%)" % (r["name"], r["avg_ret"]) for r in good[:3]))
    if bad:
        msg.append("负期望标的：%s，**下次评审优先考虑剔除**" % "、".join(
            "%s(%+.2f%%)" % (r["name"], r["avg_ret"]) for r in bad[-3:]))
    if not msg:
        msg.append("全部标的在成本线附近，无明显优劣")
    msg.append("共 %d 个信号段" % n)
    return "；".join(msg)


# --------------------------------------------------------------------------
def try_backtest(ndays=5):
    """离线回测当前参数 vs 几个候选。数据不足则返回 None。

    注意：缓存按 `trends_{code}_{ndays}_{date}.json` 存，
    请求的 ndays 必须与 warmup 时一致，否则找不到文件。
    """
    try:
        data = backtest.load_pool(strategy.CORE_POOL, ndays=ndays,
                                  verbose=False, offline=True)
    except Exception:  # noqa: BLE001
        return None
    days = [d for d in backtest.trading_days(data)
            if d < datetime.now().strftime("%Y-%m-%d")]
    if len(days) < 3:
        return None

    base = dict(strategy.PARAMS)

    # 2026-10-30 修：mode="hold" 下这张表结构上不可能有信息量。
    #   6 组候选改的全是 dev_enter / stop_loss / take_profit / max_hold_min，
    #   而 strategy.decide 的 hold 分支一个都不读 → 结果必然恒等。
    #   10-30 那期就是这样：6 行全是 -1.93% / 0 笔，看着像「参数无效」，
    #   实际是「参数没被测试」。那个 0 笔也不是没交易，是只买不卖（无 SELL 计入）。
    #   把恒等表当评审依据，比不跑回测更危险——它给了一个虚假的「已检验」。
    #   持有型的参数只有 hold_target / hold_stop / hold_entry_time，
    #   而它们的效果必须用多年日线看（engine/riskstudy.py），不是 5~12 天分钟线。
    if str(base.get("mode") or "daytrade") == "hold":
        return {
            "days": days, "rows": [], "note":
            "当前策略为持有型（mode=hold，标的 %s），本表 6 组候选改的都是"
            "**已停用的日内参数**（dev_enter / 止盈 / 止损 / 持仓时长），"
            "hold 分支根本不读取，结果恒等属预期，**不是参数无效，是参数没被测试**。\n>\n"
            "> 持有型真正可调的只有 hold_target / hold_stop / hold_entry_time，"
            "而且它们的优劣要用多年日线看，%d 天的分钟线不足以判断。"
            "请用 `python engine/riskstudy.py` 做长区间检验。"
            % (base.get("hold_target") or "-", len(days)),
        }

    variants = [
        ("当前参数", {}),
        ("入场放宽 0.7%", {"dev_enter": 0.0070}),
        ("入场收紧 0.9%", {"dev_enter": 0.0090}),
        ("止损放宽 0.5%", {"stop_loss": 0.0050}),
        ("持仓缩到 15 分", {"max_hold_min": 15}),
        ("止盈提到 0.6%", {"take_profit": 0.0060}),
    ]
    rows = []
    for label, patch in variants:
        p = dict(base)
        p.update(patch)
        try:
            r = backtest.run_backtest(data, p, days)
            m = backtest.metrics(r)
            rows.append({"label": label, "m": m})
        except Exception:  # noqa: BLE001
            continue
    return {"days": days, "rows": rows}


# --------------------------------------------------------------------------
def render_markdown(summary, gate_info, issues, bt, acc, audit=None, notes=None):
    notes = notes or []
    now = datetime.now()
    recent = summary["recent"]
    total = summary["total"]
    init = acc["meta"]["init_capital"]
    # 权益必须取「市值口径」的那一行。账本 equity.csv 的最后一行是券商快照，
    # 已按最新价重估；而 cash + 持仓成本 会把未平仓浮盈浮亏全部抹掉
    # ——2026-10-23 就因此把 +201.60 报成 -344.20（差异正好是 512100 的浮盈）。
    eq_rows = read_csv(broker.EQUITY_FILE)
    if eq_rows and fnum(eq_rows[-1].get("equity")) > 0:
        equity_val = fnum(eq_rows[-1].get("equity"))
        equity_src = "市值口径（%s 快照）" % eq_rows[-1].get("date", "")
    else:
        equity_val = fnum(acc.get("cash")) + sum(
            broker.position_shares(p) * broker.avg_cost(p)
            for p in acc.get("positions", {}).values())
        equity_src = "成本口径（无净值快照，未计入浮盈亏）"
    forced_overnight = [c for c in acc.get("positions", {})
                        if not broker.is_t0_etf(c)]

    L = []
    L.append("# 模拟盘周报 %s" % now.strftime("%Y-%m-%d"))
    L.append("")
    L.append("> 自动生成于 %s · 覆盖 %d 个交易日（%s）"
             % (now.strftime("%H:%M"),
                summary["n_days_recent"],
                " ~ ".join([summary["recent_dates"][0],
                            summary["recent_dates"][-1]])
                if summary["recent_dates"] else "无数据"))
    L.append("")

    # ---- 结论先行 ----
    L.append("## 一句话结论")
    hold_mode = gate_info.get("mode") == "hold"
    if hold_mode:
        n = gate_info["days_since"]
        last = gate_info.get("last_review") or "从未登记"
        if gate_info["ok"]:
            verdict = ("持有型：距上次人工评审 **%d 个交易日**（门槛 %d），"
                       "**已到期，必须复核一次**。" % (n, HOLD_REVIEW_DAYS))
        else:
            verdict = ("持有型：距上次人工评审 %d 个交易日（门槛 %d），"
                       "还差 %d 个交易日到期。"
                       % (n, HOLD_REVIEW_DAYS, gate_info["need_days"]))
        verdict += " 上次评审登记：%s。" % last
    elif gate_info["ok"]:
        verdict = ("样本已达标（%d 笔 / %d 日），**可以评审参数**。"
                   % (gate_info["n_trades"], gate_info["n_days"]))
    else:
        # 2026-10-23 修：门槛是「笔数 且 日数」两个条件同时满足，
        # 原来写「或」把缺口说小了一半，容易让人误判「再撑几天就够了」。
        verdict = ("样本不足（%d 笔 / %d 日），**本周不调参，继续观察**。"
                   "还差 %d 笔，且还差 %d 个交易日（两个条件都要满足）。"
                   % (gate_info["n_trades"], gate_info["n_days"],
                      gate_info["need_trades"], gate_info["need_days"]))
    if issues:
        verdict += " 另有 %d 项规则偏离需处理。" % len(issues)
    elif notes:
        verdict += " 无规则偏离（另有 %d 条浮亏/历史说明，见下）。" % len(notes)
    else:
        verdict += " 无规则偏离。"
    L.append("")
    L.append(verdict)
    if hold_mode:
        # 2026-11-07：原门槛「20 笔」在持有型下永远达不到，等于把评审机制废掉。
        # 现在改成按交易日计（HOLD_REVIEW_DAYS），这里说明为什么换口径，
        # 免得以后有人以为是放宽标准。
        L.append("")
        L.append("> 门槛口径说明：持有型一年成交 1~2 笔，按「%d 笔」算等于永不评审，"
                 "而 10-28~11-07 确实连续两周无人复核。持有型没有可日常调的参数，"
                 "真正的风险是「长期不看」，故门槛改为 **%d 个交易日 ≈ 1 个自然月**，"
                 "到点必须人工复核；复核结论登记在 `state/review.json`。"
                 % (MIN_TRADES, HOLD_REVIEW_DAYS))
    L.append("")

    # ---- 实测 ----
    L.append("## 实测表现")
    L.append("")
    L.append("| 指标 | 本期 | 累计 |")
    L.append("|---|---|---|")
    L.append("| 完成交易 | %d 笔 | %d 笔 |" % (recent["n"], total["n"]))
    L.append("| 胜率 | %.1f%% | %.1f%% |"
             % (recent["win_rate"], total["win_rate"]))
    # 2026-11-07 修：「净盈亏」列一直是**已实现**，但标签写着「净」，与下面
    # 市值口径的账户权益直接打架（11-06：净盈亏 -80.50，权益却是 9763.70）。
    # 10-23 修过一次同类问题（权益按成本价算，浮盈全丢），这里是另一半：
    # 口径对了，标签还在骗人。改成「已实现」，并显式补一行未实现。
    unreal = equity_val - init - total["pnl"]
    L.append("| 已实现净盈亏 | ¥%+.2f | ¥%+.2f |" % (recent["pnl"], total["pnl"]))
    L.append("| 未实现浮动 | — | ¥%+.2f |" % unreal)
    L.append("| 单笔均值 | ¥%+.2f | ¥%+.2f |" % (recent["avg"], total["avg"]))
    L.append("| 最好 / 最差 | ¥%+.2f / ¥%+.2f | ¥%+.2f / ¥%+.2f |"
             % (recent["best"], recent["worst"], total["best"], total["worst"]))

    L.append("")
    L.append("> 已实现 + 未实现 = 累计 %.2f 元，与账户权益一致。"
             "只看「已实现」会漏掉持仓浮亏，只看权益会不知道亏在哪个环节。"
             % (total["pnl"] + unreal))
    L.append("")
    L.append("账户权益 ¥%.2f（初始 ¥%.0f，%s），本期 %d 个交易日中空仓 %d 天。"
             % (equity_val, init, equity_src,
                summary["n_days_recent"], summary["flat_days"]))
    L.append("")

    # ---- 规则偏离 ----
    L.append("## 规则偏离检查")
    L.append("")
    if issues:
        for s in issues:
            L.append("- " + s)
    else:
        L.append("- 无。仓位、频率、时段、强平均符合规则。")
    if forced_overnight:
        L.append("- （说明）持有 T+1 品种 %s，过夜是规则强制、非执行偏离；"
                 "但这意味着**当日买入的仓位当日无法止损**，风险敞口实际是隔夜跳空。"
                 % "、".join(forced_overnight))
    for s in notes:
        L.append("- " + s)
    L.append("")

    # ---- 回测对照 ----
    L.append("## 参数对照（离线回测）")
    L.append("")
    if bt is None:
        L.append("本地缓存不足（需 ≥3 个交易日分钟数据），本期跳过回测。"
                 "可跑 `python engine/warmup.py` 补数据。")
    elif not bt.get("rows"):
        # 结构上不适用（如 mode=hold），给原因而不是一张恒等表
        L.append("样本：%d 个交易日（%s ~ %s）"
                 % (len(bt["days"]), bt["days"][0], bt["days"][-1]))
        L.append("")
        L.append("> %s" % (bt.get("note") or "本表不适用"))
    else:
        L.append("样本：%d 个交易日（%s ~ %s）"
                 % (len(bt["days"]), bt["days"][0], bt["days"][-1]))
        L.append("")
        L.append("| 参数组 | 收益 | 笔数 | 胜率 | 单笔均值 |")
        L.append("|---|---|---|---|---|")
        for row in bt["rows"]:
            m = row["m"]
            mark = " **←**" if row["label"] == "当前参数" else ""
            L.append("| %s%s | %+.2f%% | %d | %.0f%% | %+.2f |"
                     % (row["label"], mark, m["ret_pct"], m["trades"],
                        m["win_rate"], m["avg_pnl"]))
        L.append("")
        if not gate_info["ok"]:
            L.append("> ⚠️ 样本天数太少，本表**仅供观察，不作为调参依据**。"
                     "少于 10 个交易日的回测结论不可信。")
    L.append("")

    # ---- 池子体检 ----
    L.append("## 池子体检")
    L.append("")
    if audit and audit["rows"]:
        L.append("> 不看成交，把每个「信号段」独立按 止盈/止损/时间止损 走一遍。"
                 "成交少的时候，这是唯一能扩大样本的办法。成本按 %.3f%% 计。"
                 % audit["cost"])
        L.append("")
        L.append("| 标的 | 信号段 | 止盈 | 止损 | 超时 | 止盈率 | 净期望 | 日均振幅 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in audit["rows"]:
            mark = " **←**" if r is audit["rows"][0] else ""
            L.append("| %s %s%s | %d | %d | %d | %d | %.0f%% | %+.2f%% | %.2f%% |"
                     % (r["code"], r["name"], mark, r["signals"],
                        r["tp"], r["sl"], r["timeout"], r["tp_rate"],
                        r["avg_ret"], r["amp"]))
        L.append("")
        v = audit_verdict(audit)
        if str(strategy.PARAMS.get("mode") or "") == "hold":
            # 体检算的是「日内信号段」的期望，持有型根本不产生日内信号。
            # 保留表格是为了对照池子质地，但绝不能让它输出「剔除某只」的结论
            # ——持有型只买一只，剔除清单对它没有操作意义。
            # （2026-10-30：原来这里直接复用 audit_verdict，末尾那句
            #  「下次评审优先考虑剔除」在 hold 模式下是误导性指令。）
            rows = audit["rows"]
            good = [r for r in rows if r["avg_ret"] > 0.05]
            tgt = next((r for r in rows
                        if r["code"] == str(strategy.PARAMS.get("hold_target"))), None)
            L.append("**读数**：当前为持有型（mode=hold），本表**不用于选股决策**，"
                     "仅作池子质地参照——正期望 %d / %d 只；持有标的 %s 的日内"
                     "净期望 %s（此处为负也正常：持有型赚的是多年趋势，不是日内回归）。"
                     % (len(good), len(rows),
                        (tgt or {}).get("name", "-"),
                        "%+.2f%%" % tgt["avg_ret"] if tgt else "无数据"))
        elif v:
            L.append("**读数**：%s" % v)
        L.append("")
        L.append("> 注意：净期望是「每个信号段都入场」的理想值，实际一次只能持一仓，"
                 "真实收益会低于此表。这张表用来**比较标的之间的相对质量**，不是预测收益。")
    else:
        L.append("缓存里没有完整历史分时，无法体检。先跑 `engine/warmup.py` 预热。")
    L.append("")

    # ---- 下一步 ----
    L.append("## 下一步")
    L.append("")
    if issues:
        L.append("1. 先解决规则偏离，再谈参数。")
        L.append("2. 其余保持不动。")
    elif not gate_info["ok"]:
        L.append("1. 保持参数不变，继续累积样本。")
        L.append("2. 每日 14:45 盘中执行、15:05 收盘定稿照常运行。")
        L.append("3. 达到 %d 笔 / %d 日后自动进入评审。" % (MIN_TRADES, MIN_DAYS))
    else:
        L.append("1. 对照上表，若某组明显优于当前且逻辑可解释，才考虑采纳。")
        L.append("2. 一次只改一个参数，改完观察 5 个交易日再评估。")
        L.append("3. 若回测优势来自个别交易日，视为噪音，不采纳。")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*由 engine/weekly.py 自动生成，不依赖人工判断*")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    acc, trades, equity = load_all()
    summary = summarize(trades, equity, days=args.days)
    issues, notes = deviations(trades, equity, acc)
    hold_mode = str(strategy.PARAMS.get("mode") or "") == "hold"
    last_review, days_since = review_age(equity)
    gate_info = gate(summary["total"]["n"], summary["n_days"],
                     hold_mode=hold_mode, days_since=days_since,
                     last_review=last_review)
    # 即使样本未达标也跑回测：数据是否够用应由 try_backtest 自己判断，
    # 否则「样本不足」会被误报成「缓存不足」，掩盖真实状态。
    # 未达标时 render_markdown 会标注「仅供观察，不作为调参依据」。
    bt = try_backtest(ndays=max(args.days, 5))

    audit = pool_audit(ndays=max(args.days, 5))
    md = render_markdown(summary, gate_info, issues, bt, acc, audit, notes)
    os.makedirs(JOURNAL, exist_ok=True)
    path = os.path.join(JOURNAL, "weekly-%s.md" % datetime.now().strftime("%Y-%m-%d"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)

    if not args.quiet:
        print(md)
        print()
        print("=" * 58)
        print("  周报已写入: %s" % path)
        print("=" * 58)
    return 0


if __name__ == "__main__":
    sys.exit(main())
