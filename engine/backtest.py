# -*- coding: utf-8 -*-
"""回放引擎：多标的、统一时间轴、逐分钟决策。

防未来函数约定：
  * 第 i 分钟的信号只用 ticks[0..i] 的信息；
  * 成交发生在第 i+1 分钟的价格（下一根分钟 bar 的市价单）。
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker       # noqa: E402
import market      # noqa: E402
import strategy    # noqa: E402

CACHE = {}


def load_pool(codes, ndays=5, verbose=True, offline=False, report=None):
    """加载分时，按交易日分组。preclose 用前一交易日最后价格。

    offline=True 时只读本地缓存，不发起网络请求（限流时用）。
    report: 传入 dict 时回填 {"requested", "ok", "failed": [(code, 原因)]}。

    为什么要 report：返回值只有「成功的那些」，少了哪只、为什么少，全被吞掉。
    12/13 只和 3/13 只返回的都是一个非空 dict，调用方没法区分「池子完整」和
    「大半标的拉取失败」。回测照跑，收益照算，看着完全正常。
    """
    out = {}
    failed = []
    degraded = []
    today = market.time.strftime("%Y%m%d")
    for c in codes:
        try:
            if offline:
                path = market._cpath(f"trends_{c}_{ndays}_{today}.json")
                blob = market._read(path)
                if not blob:
                    old = market._stale(f"trends_{c}_{ndays}_")
                    blob = old
                if not blob:
                    if verbose:
                        print(f"  - {c} 无缓存")
                    failed.append((c, "无缓存"))
                    continue
                tr = blob["data"]
            else:
                tr = market.fetch_trends(c, ndays=ndays)
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  ! {c} 拉取失败: {e}")
            failed.append((c, "拉取失败: %s" % str(e)[:40]))
            continue
        # 0 根也要当失败：否则会建出 {"days": {}} 这种「存在但什么都没有」
        # 的条目，打印成「+ 588000 / 0 点 0 天」，看着像成功。
        if not tr.get("ticks"):
            if verbose:
                print(f"  - {c} 无分时数据（0 根）")
            failed.append((c, "0 根分时"))
            continue
        by_day = defaultdict(list)
        for t in tr["ticks"]:
            by_day[t["t"][:10]].append(t)
        days = sorted(by_day)
        pre = {}
        for k, d in enumerate(days):
            if k == 0:
                pre[d] = by_day[d][0]["price"]
            else:
                pre[d] = by_day[days[k - 1]][-1]["price"]
        out[c] = {"name": tr["name"] or c, "days": {d: by_day[d] for d in days},
                  "preclose": pre, "first_day_no_preclose": days[0]}
        # 降级缓存（接口挂了，拿的是更早写下的文件）照样能用，但必须留痕：
        # 回测/归因/优化都吃这份数据，用旧缓存算出来的结论要打折扣。
        if tr.get("stale"):
            degraded.append((c, tr.get("as_of") or "?"))
        if verbose:
            print(f"  + {c} {(tr['name'] or c)[:14]:<16} {len(tr['ticks'])} 点 / {len(days)} 天"
                  + ("  [降级缓存]" if tr.get("stale") else ""))
    if report is not None:
        report.update({"requested": len(codes), "ok": len(out), "failed": failed,
                       "degraded": degraded})
    if failed and verbose:
        print("  ! %d/%d 只标的没加载成功：%s"
              % (len(failed), len(codes),
                 "、".join("%s(%s)" % (c, why) for c, why in failed)))
    if degraded and verbose:
        print("  ! %d 只标的用的是降级缓存（截至 %s）：结论要打折扣"
              % (len(degraded),
                 "、".join("%s@%s" % (c, t) for c, t in degraded)))
    return out


def trading_days(data):
    ds = set()
    for v in data.values():
        ds |= set(v["days"].keys())
    return sorted(ds)


def _limit_block(code, name, preclose, side, quote, px):
    """涨跌停/封板校验，口径对齐 broker.buy / broker.sell。

    实盘 broker 会 Reject「涨停封板」「跌停封板」，回测原来根本不做这层判断：
    涨停日照买（实盘买不到）、跌停日照卖（实盘卖不掉，会被封在里面过夜）。
    这不是小数点的差别，是收益分布的形状——回测恰好在最极端的行情里给出
    实盘拿不到的价格，于是系统性低估尾部亏损。optimize/weekly 都拿它的结果
    做判断，所以这层缺失会一路传染。

    返回 None 表示可成交，否则返回不能成交的原因。
    """
    if not preclose:
        return None
    up, down = broker.limit_prices(code, preclose, name)
    if side == "BUY":
        if quote > up + 1e-9:
            return "涨停封板：报价 %.3f 高于涨停价 %.3f" % (quote, up)
        if px > up + 1e-9:
            return "涨停封板：吃一档后 %.3f 超过涨停价 %.3f" % (px, up)
        return None
    if quote < down - 1e-9:
        return "跌停封板：报价 %.3f 低于跌停价 %.3f" % (quote, down)
    if px < down - 1e-9:
        return "跌停封板：让一档后 %.3f 跌破跌停价 %.3f" % (px, down)
    return None


def run_backtest(data, params, days, capital=10000.0, verbose=False):
    """组合回放：单票集中，全池扫描。

    与实盘 broker 对齐的四条撮合规则：
      * 滑点：买入 +1 tick / 卖出 -1 tick，与 broker.buy/sell 一致；
      * 涨跌停/封板：成交价越过涨跌停价就拒单（2026-10-28 补）。以前只做滑点
        不做限价校验，跌停日照样卖得出去——实盘那是 Reject，仓位会封在里面；
      * R6 大盘熔断：每分钟按 510300（缺则取池内首个标的）重算 market_pct，
        以前这里恒为 0.0，等于回测里根本没有熔断这条规则；
      * 停牌日权益：用最后一个已知价计市值，不能把持仓当消失了。
    参数 max_trades_per_day 曾在此声明但从未生效（真实约束在
    strategy.decide 里读 params["max_trades"]），已删除，避免读代码的人
    误以为 R8 在这一层被强制。
    """
    cash = capital
    equity_val = capital
    start_equity = capital
    pos = None            # {code, entry, entry_idx, t, shares, day, overnight}
    trades = []
    blocked = []          # 被涨跌停/封板挡下的单子，不能静默丢
    day_pnls = []
    last_px = {}          # code -> 最后一个已知成交价，停牌日用来计市值

    def note_block(day, c, side, t, why, tag=""):
        """同一天同一只同一方向同一性质只记第一条，否则跌停日会刷几百条。"""
        key = (day, c, side, tag)
        if key in seen_blocks:
            return
        seen_blocks.add(key)
        blocked.append({"day": day, "code": c, "side": side, "t": t,
                        "reason": why})

    seen_blocks = set()

    # ------------------------------------------------------------------
    # 持有型（mode="hold"）的两处对齐，2026-10-29 补。
    #
    # 1) 买哪只必须在这里钉住。strategy.decide() 的签名里没有「标的」，
    #    它只回答「该不该买」——hold 分支只要过了 hold_entry_time 就返回 BUY。
    #    回测因此会买 data 里的第一只（CORE_POOL 顺序 → 510300），而实盘
    #    runner 明确买 PARAMS["hold_target"]（510880）。两只 ETF 的三年收益
    #    差 30 个百分点，回归探针却一直报「与基线一致」：它量的是另一只票。
    #
    # 2) 熔断冷却。runner 在 broker.sell 之后写 acc["hold_paused_until"]，
    #    回测没有对应物，于是熔断当天就立刻买回来——回测里的 -25% 熔断
    #    实际上没起到「离场」作用，只是多交了两次手续费。
    # ------------------------------------------------------------------
    hold_tgt = (str(params.get("hold_target") or "")
                if params.get("mode") == "hold" else "")
    pause_until = ""      # 熔断冷却截止日（含），语义同 runner 的 hold_paused_until

    # 大盘代理：实盘 runner 用 510300，池里没有就退化为首个标的
    mkt = "510300" if "510300" in data else (next(iter(data)) if data else None)
    if verbose and mkt != "510300":
        print(f"  ! 无 510300 数据，大盘熔断参照退化为 {mkt}")
    # 窗口首日没有「前一交易日收盘」可用，preclose 只能取当日第一根价格。
    # 那一分钟的 market_pct 因此恒为 0，R6 实际是「相对开盘」而非「相对昨收」，
    # 会放行低开低走的日子。影响仅限首日，但必须说出来——不然读结果的人
    # 以为整段回放都带着熔断这条规则。
    if verbose and days and any(v.get("first_day_no_preclose") == days[0]
                                for v in data.values()):
        print(f"  · 首日 {days[0]} 无前收盘，R6 按「相对开盘」计，非相对昨收")

    for day in days:
        # 隔夜仓：昨天买入的 T+1 标的，今天才解锁。
        # entry_idx 记的是"当天第几根分钟线"，昨天的索引放到今天没有意义，
        # 置 -1 等价于「一开盘就已超过持有上限」，即次日开盘按市价了结。
        if pos and pos.get("day") != day:
            pos["entry_idx"] = -1
            pos["overnight"] = True
        ctx = {"trades": 0, "market_pct": 0.0, "preclose": None}
        # 时间轴：取当日所有标的的时间并集（同一交易时段，基本一致）
        tl = set()
        for v in data.values():
            if day in v["days"]:
                tl |= {t["t"] for t in v["days"][day]}
        tlist = sorted(tl)
        # 索引：code -> {t: idx}
        idxmap = {}
        for c, v in data.items():
            if day in v["days"]:
                idxmap[c] = {t["t"]: k for k, t in enumerate(v["days"][day])}

        for i, t in enumerate(tlist):
            tm = t[11:16]
            if i == 0:
                continue

            # 大盘涨跌：和 runner 一样逐分钟更新，否则 R6 熔断在回测里形同虚设
            if mkt and day in data[mkt]["days"]:
                mk = idxmap.get(mkt, {}).get(t)
                mpre = data[mkt]["preclose"].get(day)
                if mk is not None and mpre:
                    ctx["market_pct"] = (data[mkt]["days"][day][mk]["price"]
                                         / mpre - 1) * 100

            # 持仓：出场判定
            if pos:
                c = pos["code"]
                if day in data[c]["days"] and t in idxmap[c]:
                    ticks = data[c]["days"][day]
                    k = idxmap[c][t]
                    if k < 2:
                        continue
                    # 用 k-1 决策，k 成交
                    pctx = dict(ctx)
                    pctx["preclose"] = data[c]["preclose"][day]
                    can_sell = bool(pos.get("overnight")) or broker.is_t0_etf(c)
                    act, reason, detail = strategy.decide(
                        ticks, k - 1,
                        {"entry": pos["entry"], "entry_idx": pos["entry_idx"]},
                        pctx, params, can_sell=can_sell)
                    if act == "SELL":
                        # 卖方让一档，与 broker.sell 的撮合口径一致
                        q = ticks[k]["price"]
                        px = broker.round_price(
                            c, q - broker.price_tick(c))
                        why = _limit_block(c, data[c]["name"],
                                           pctx["preclose"], "SELL", q, px)
                        if why:
                            # 跌停封板：实盘卖不掉，仓位继续留着。回测必须一样，
                            # 否则回测能「在跌停价顺利离场」，实盘被封在里面过夜。
                            note_block(day, c, "SELL", t, why)
                            continue
                        amt = px * pos["shares"]
                        fee = broker.calc_fee(c, "SELL", amt)
                        proceeds = amt - fee["total"]
                        cost = pos["entry"] * pos["shares"] + pos["fee_buy"]
                        pnl = proceeds - cost
                        cash += proceeds
                        trades.append({"day": day, "code": c,
                                       "name": data[c]["name"], "side": "SELL",
                                       "t": t, "price": px, "shares": pos["shares"],
                                       "pnl": pnl, "reason": reason})
                        pos = None
                        ctx["trades"] += 1
                        # 熔断离场后要冷却，否则下一分钟就买回来（同 runner）
                        if reason.startswith("熔断"):
                            cd = int(params.get("hold_cooldown_days", 30))
                            pause_until = (datetime.strptime(day, "%Y-%m-%d")
                                           + timedelta(days=cd)).strftime("%Y-%m-%d")
                continue

            # 空仓：全池扫描，选偏离最深的
            # 熔断冷却期内不建仓（date 字符串比较，与 runner 的 day < until 同义）
            if pause_until and day < pause_until:
                continue
            if hold_tgt and hold_tgt not in data:
                # 持有型标的缺数据 = 今日不建仓。以前这里什么都不做，
                # 「标的没数据」和「策略决定不买」看起来一模一样。
                note_block(day, hold_tgt, "BUY", t if tlist else "",
                           "持有型标的缺数据，本日不建仓", tag="hold_target")
                continue
            best = None
            for c, v in data.items():
                if hold_tgt and c != hold_tgt:
                    continue
                if day not in v["days"] or t not in idxmap[c]:
                    continue
                ticks = v["days"][day]
                k = idxmap[c][t]
                # k=0 → decide 的 ticks[k-1] 是 ticks[-1]，即当日最后一根。
                # 回放里这就是未来数据（与 runner 同一处坑，两边都要挡）。
                if k < 1:
                    continue
                # 22 根预热门槛是给日内指标（vol_ratio 要 20 根）用的。
                # 持有型不读任何指标，套用它等于把 hold_entry_time=09:35
                # 悄悄推到 09:52——参数写的和实际做的不一样。
                if params.get("mode") != "hold" and k < 22:
                    continue
                cctx = dict(ctx)
                cctx["preclose"] = v["preclose"][day]
                act, reason, detail = strategy.decide(ticks, k - 1, None, cctx, params)
                if act == "BUY":
                    score = abs(detail.get("dev", 0))
                    if not best or score > best[0]:
                        best = (score, c, ticks, k, reason)

            if best:
                _, c, ticks, k, reason = best
                # 买方吃一档，与 broker.buy 的撮合口径一致
                q = ticks[k]["price"]
                px = broker.round_price(
                    c, q + broker.price_tick(c))
                why = _limit_block(c, data[c]["name"],
                                   data[c]["preclose"].get(day), "BUY", q, px)
                if why:
                    # 涨停封板买不到。实盘 runner 在这里也是 Reject 后空手收场
                    # （只取最优一只，不换次优），回测保持同样的行为。
                    note_block(day, c, "BUY", t, why)
                    continue
                # 与 runner 同一口径：留 5 元手续费 + 0.1% 价格缓冲，其余全部买入。
                # 旧的 (cash-5.5)*0.97 是给日内策略留的余量，在持有型下让 3% 资金
                # 永久闲置，回测的仓位因此系统性小于实盘。
                budget = cash - 5.0
                shares = int(budget / (px * 1.001) / 100) * 100
                if shares >= 100:
                    amt = px * shares
                    fee = broker.calc_fee(c, "BUY", amt)
                    if amt + fee["total"] <= cash:
                        cash -= amt + fee["total"]
                        pos = {"code": c, "entry": px, "entry_idx": k,
                               "shares": shares, "fee_buy": fee["total"], "t": t,
                               "day": day, "overnight": False}
                        trades.append({"day": day, "code": c,
                                       "name": data[c]["name"], "side": "BUY",
                                       "t": t, "price": px, "shares": shares,
                                       "pnl": 0.0, "reason": reason})

        # 收盘仍未平仓：T+0 标的按最后价强平；T+1 标的当天买的根本卖不掉，带着过夜
        # 例外同 runner：mode="hold" 本来就要过夜，这里不能强平。否则持有型策略在
        # 回测里退化成「每天买、每天尾盘卖」，和实盘跑的完全不是同一套规则。
        if pos and params.get("mode") != "hold":
            c = pos["code"]
            can_close = bool(pos.get("overnight")) or broker.is_t0_etf(c)
            if can_close and day in data[c]["days"]:
                last = data[c]["days"][day][-1]
                # 强平同样是卖出，同样让一档
                q = last["price"]
                px = broker.round_price(
                    c, q - broker.price_tick(c))
                why = _limit_block(c, data[c]["name"],
                                   data[c]["preclose"].get(day), "SELL", q, px)
                if why:
                    # 收盘封在跌停上，强平也卖不掉。仓位带着过夜，不是数据丢了。
                    note_block(day, c, "SELL", last["t"], "收盘强平受阻：" + why,
                               tag="close")
                else:
                    amt = px * pos["shares"]
                    fee = broker.calc_fee(c, "SELL", amt)
                    proceeds = amt - fee["total"]
                    cost = pos["entry"] * pos["shares"] + pos["fee_buy"]
                    cash += proceeds
                    trades.append({"day": day, "code": c, "name": data[c]["name"],
                                   "side": "SELL", "t": last["t"], "price": px,
                                   "shares": pos["shares"], "pnl": proceeds - cost,
                                   "reason": "收盘强平"})
                    pos = None

        # 权益必须把持仓市值算进去：T+1 过夜仓只算现金的话，
        # 日盈亏会凭空少掉持仓那一大块，看着像"今天亏了"，其实是没结算。
        # 停牌（当日无 tick）时用最后一个已知价，不能让持仓凭空消失。
        equity_val = cash
        if pos:
            c = pos["code"]
            if day in data[c]["days"]:
                px_end = data[c]["days"][day][-1]["price"]
                last_px[c] = px_end
            else:
                px_end = last_px.get(c, pos["entry"])
            equity_val += px_end * pos["shares"]
        day_pnl = round(equity_val - start_equity, 2)
        day_pnls.append(day_pnl)
        start_equity = equity_val
        if verbose:
            # 曾在这里用 equity_val - start_equity 打印，而上一行刚把
            # start_equity 赋成 equity_val，于是每天都印 +0.00 —— 一个
            # 看着完全正常的 0，比报错更难发现。改用先算好的 day_pnl。
            print(f"  {day}  日盈亏 {day_pnl:+8.2f}  权益 {equity_val:9.2f}")

    return {"final": round(equity_val, 2), "trades": trades,
            "day_pnls": day_pnls, "open_pos": pos, "blocked": blocked}


def metrics(res, capital=10000.0):
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    wins = [t for t in sells if t["pnl"] > 0]
    n = len(sells)
    op = res.get("open_pos") or None
    return {
        "ret_pct": round((res["final"] / capital - 1) * 100, 3),
        "pnl": round(res["final"] - capital, 2),
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_pnl": round(sum(t["pnl"] for t in sells) / n, 2) if n else 0.0,
        "best": round(max([t["pnl"] for t in sells], default=0), 2),
        "worst": round(min([t["pnl"] for t in sells], default=0), 2),
        # 回放窗口结束时还握着仓位：ret_pct 里含着未平仓市值，不能当成
        # 「已实现收益」看。以前这个信息只藏在 res 里，metrics 一取就没了，
        # 报告读起来像「1 笔完整往返」，实际是「1 笔买入 + 1 笔从未离场」。
        "open_code": op["code"] if op else None,
        "open_shares": op["shares"] if op else 0,
        "blocked": len(res.get("blocked") or []),
    }
