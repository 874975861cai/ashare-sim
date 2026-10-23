# -*- coding: utf-8 -*-
"""盈亏归因：把每笔交易的收益拆成「日内」与「隔夜跳空」两段。

为什么必须拆：
    T+1 品种当天买入当天卖不掉，止盈止损根本执行不了。
    结果是「日内均线回归」这个信号赚的钱（或亏的钱），
    和「隔夜跳空」赚的钱（或亏的钱）混在一个数字里。
    不拆开，就永远不知道策略到底有没有效——
    可能信号是对的、只是被跳空吃掉；也可能信号本来就是噪音。

同时给出 T+0 反事实：
    假设同一笔交易能日内平仓，按现有 止盈/止损/时间止损/强平 规则，
    会在什么价位出场、赚多少。两者之差 = 「T+1 锁定的净代价」。

分解口径（三段，乘积正好等于实际收益）：
    日内段   entry        -> 当日收盘
    跳空段   当日收盘      -> 次日开盘
    早盘段   次日开盘      -> 实际卖出
    entry -> 收盘 -> 次日开盘 -> 卖出，三段连乘 == 卖出价/买入价

用法：
    python engine/gapattr.py
    python engine/gapattr.py --json
"""
import argparse
import csv
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker     # noqa: E402
import market     # noqa: E402
import strategy   # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKS_CACHE = {}


def read_trades():
    path = os.path.join(broker.STATE, "trades.csv")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def fnum(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def get_ticks(code, ndays=10):
    key = (code, ndays)
    if key not in TICKS_CACHE:
        tr = market.fetch_trends(code, ndays=ndays)
        TICKS_CACHE[key] = tr.get("ticks") or []
    return TICKS_CACHE[key]


CACHE_DIR = os.path.join(broker.STATE, "cache")
BARS_CACHE = {}


def daily_bars(code, beg="20261001", end="20261231"):
    """日线 open/close，权威口径。

    为什么不用分钟数据算「收盘」：
      state/cache 里的分钟文件是「当天某个时刻」抓的，午休抓的就只到 11:30。
      2026-10-21 那份只到 11:30，末价 2.728，而真实收盘 2.713 —
      用它做分割会把 -0.59% 的日内亏算成 -0.04%，跳空段被虚增。
      日线的开/收盘是结算口径，没有这个问题。
    """
    if code in BARS_CACHE:
        return BARS_CACHE[code]
    import urllib.request
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
           "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           "&klt=101&fqt=1&beg=%s&end=%s" % (market.secid(code), beg, end))
    out = {}
    try:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with op.open(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
        for line in (d.get("data") or {}).get("klines") or []:
            p = line.split(",")
            if len(p) >= 5:
                out[p[0]] = {"open": float(p[1]), "close": float(p[2]),
                             "high": float(p[3]), "low": float(p[4])}
    except Exception:  # noqa: BLE001
        out = {}

    if not out:
        # 网络被限流（东财会 RemoteDisconnected）时读兜底文件，
        # 不然复盘脚本就成了「能不能联网」的赌注。
        fb = os.path.join(CACHE_DIR, "daykline_fallback.json")
        try:
            with open(fb, "r", encoding="utf-8") as f:
                out = json.load(f).get(code) or {}
        except Exception:  # noqa: BLE001
            out = {}

    BARS_CACHE[code] = out
    return out


def load_day(code, day):
    """取某标某日的分钟序列，**优先读本地缓存**。

    为什么要读缓存而不是直接打接口：
      state/cache 里存着 `trends_<code>_<ndays>_<抓取日>.json`，
      是当天运行时就落盘的，历史每天都在。东财的 trends2 / klt=1 接口
      会限流、会 RemoteDisconnected，复盘时打十几次就断了。
      缓存是本地既成事实，比网络可靠；网络只作兜底。

    注意：缓存可能不完整（午休抓的只到 11:30），调用方若要「收盘价」
    请用 daily_bars，不要用这里的最后一根。
    """
    key = (code, day)
    if key in TICKS_CACHE:
        return TICKS_CACHE[key]

    cand = [os.path.join(CACHE_DIR, "trends_%s_1_%s.json"
                         % (code, day.replace("-", "")))]
    # 5 日包也可能覆盖目标日
    if os.path.isdir(CACHE_DIR):
        for fn in sorted(os.listdir(CACHE_DIR)):
            if fn.startswith("trends_%s_5_" % code):
                cand.append(os.path.join(CACHE_DIR, fn))

    for p in cand:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        ticks = (d.get("data") or d).get("ticks") or []
        rows = [t for t in ticks if t["t"].startswith(day)]
        if rows:
            TICKS_CACHE[key] = rows
            return rows

    # 缓存没有，退回网络（只对当日有效）
    try:
        tr = market.fetch_trends(code, ndays=1)
        rows = [t for t in (tr.get("ticks") or []) if t["t"].startswith(day)]
        if rows:
            TICKS_CACHE[key] = rows
            return rows
    except Exception:  # noqa: BLE001
        pass
    TICKS_CACHE[key] = []
    return []


def day_view(code, day):
    """返回某日的 (开盘价, 收盘价)：优先用日线，退回分钟首末。"""
    bars = daily_bars(code)
    b = bars.get(day)
    if b:
        return b["open"], b["close"]
    rows = load_day(code, day)
    if not rows:
        return None, None
    return rows[0]["price"], rows[-1]["price"]


def pair_round_trips(trades):
    """BUY -> SELL 配对（同一标的 FIFO）。未平仓的留在 open 里。"""
    open_lots, closed = {}, []
    for t in trades:
        code = t.get("code")
        side = t.get("side")
        if side == "BUY":
            open_lots.setdefault(code, []).append(t)
        elif side == "SELL":
            q = open_lots.get(code) or []
            if not q:
                continue
            b = q.pop(0)
            closed.append((b, t))
    leftover = [b for q in open_lots.values() for b in q]
    return closed, leftover


def t0_counterfactual(code, day, entry_px, entry_time, params=strategy.PARAMS):
    """假设允许日内平仓，按现有规则当日会在哪里出场。"""
    rows = load_day(code, day)
    if not rows:
        return None
    # 找到入场分钟在当日序列中的位置
    idx = None
    for i, t in enumerate(rows):
        if t["t"][11:16] >= entry_time:
            idx = i
            break
    if idx is None:
        return None
    for i in range(idx, len(rows)):
        tm = rows[i]["t"][11:16]
        px = rows[i]["price"]
        pnl = px / entry_px - 1
        if pnl >= params["take_profit"]:
            return {"px": px, "pnl": pnl, "why": "止盈",
                    "t": rows[i]["t"][11:16], "hold": i - idx}
        if pnl <= -params["stop_loss"]:
            return {"px": px, "pnl": pnl, "why": "止损",
                    "t": rows[i]["t"][11:16], "hold": i - idx}
        if i - idx >= params["max_hold_min"]:
            return {"px": px, "pnl": pnl, "why": "时间止损",
                    "t": rows[i]["t"][11:16], "hold": i - idx}
        if tm >= params["force_exit"]:
            return {"px": px, "pnl": pnl, "why": "尾盘强平",
                    "t": rows[i]["t"][11:16], "hold": i - idx}
    last = rows[-1]
    return {"px": last["price"], "pnl": last["price"] / entry_px - 1,
            "why": "收盘仍在", "t": last["t"][11:16], "hold": len(rows) - idx}


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    trades = read_trades()
    closed, leftover = pair_round_trips(trades)

    rows = []
    for b, s in closed:
        code = b["code"]
        eday = b["datetime"][:10]
        xday = s["datetime"][:10]
        epx = fnum(b["price"])
        xpx = fnum(s["price"])
        _, ecls = day_view(code, eday)
        xop, _ = day_view(code, xday)
        if not ecls or not xop or not epx:
            continue
        intraday = ecls / epx - 1
        gap = xop / ecls - 1
        drift = xpx / xop - 1
        total = xpx / epx - 1
        cf = t0_counterfactual(code, eday, epx, b["datetime"][11:16])
        rows.append({
            "code": code, "name": b.get("name", ""),
            "buy": b["datetime"][:16], "sell": s["datetime"][:16],
            "entry": epx, "exit": xpx, "close": ecls, "next_open": xop,
            "intraday": intraday, "gap": gap, "drift": drift, "total": total,
            "pnl": fnum(s.get("pnl")), "reason": s.get("reason", ""),
            "t0": cf,
        })

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print("没有已平仓的完整回合，无法归因。")
        return 0

    print("=" * 78)
    print("盈亏归因：日内段 / 隔夜跳空段 / 早盘段")
    print("=" * 78)
    hdr = "%-16s %8s %8s %9s %9s %9s %9s %10s" % (
        "买入→卖出", "买入价", "卖出价", "日内", "跳空", "早盘", "合计", "净盈亏")
    print(hdr)
    print("-" * 78)
    for r in rows:
        print("%-16s %8.3f %8.3f %8.2f%% %8.2f%% %8.2f%% %8.2f%% %10.2f" % (
            r["buy"][5:16], r["entry"], r["exit"],
            r["intraday"] * 100, r["gap"] * 100, r["drift"] * 100,
            r["total"] * 100, r["pnl"]))
    print("-" * 78)
    si = sum(r["intraday"] for r in rows)
    sg = sum(r["gap"] for r in rows)
    sd = sum(r["drift"] for r in rows)
    st = sum(r["total"] for r in rows)
    sp = sum(r["pnl"] for r in rows)
    print("%-16s %8s %8s %8.2f%% %8.2f%% %8.2f%% %8.2f%% %10.2f" % (
        "合计", "", "", si * 100, sg * 100, sd * 100, st * 100, sp))

    print()
    print("==", "T+0 反事实（假设当日可平仓，按现有规则出场）", "=" * 20)
    for r in rows:
        cf = r["t0"]
        if not cf:
            print("  %s: 无分时数据，跳过" % r["buy"][:16])
            continue
        print("  %s 实际 %+.2f%%（%s） | 若能日内平：%+.2f%%（%s @%s，持 %d 分）| 差 %+.2f%%" % (
            r["buy"][5:16], r["total"] * 100, r["reason"] or "—",
            cf["pnl"] * 100, cf["why"], cf["t"], cf["hold"],
            (r["total"] - cf["pnl"]) * 100))

    locked = [r for r in rows if r["t0"]]
    if locked:
        fact = sum(r["total"] for r in locked)
        hypo = sum(r["t0"]["pnl"] for r in locked)
        print()
        print("  T+1 锁定的净代价：实际 %+.2f%%  vs  日内可平 %+.2f%%  →  %+.2f%%" % (
            fact * 100, hypo * 100, (fact - hypo) * 100))
        print("  （正数=T+1 帮了忙，负数=T+1 拖了后腿）")

    if leftover:
        print()
        print("==", "未平仓", "=" * 48)
        for b in leftover:
            code = b["code"]
            eday = b["datetime"][:10]
            epx = fnum(b["price"])
            print("  %s 买入 %s @%.3f，尚未卖出（T+1 锁定或持有中）" % (
                code, b["datetime"][:16], epx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
