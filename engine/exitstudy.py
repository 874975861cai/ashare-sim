# -*- coding: utf-8 -*-
"""出场规则对照实验：同一批入场信号，只换出场方式，看差多少。

为什么做这个：
    2026-10-28 用户质问「行情大涨你卖飞」。查账发现不是运气问题——
    现规则在 T+1 下退化成「次日 09:31 无条件卖出」，而策略的立论是
    「买入日内超跌、等它回归」。等回归需要时间，但规则不给时间。
    10-27 买 588000 @1.367，次日 09:31 以 1.338 割掉，当天下午回到 1.367。
    这不是止损，这是在偏离最大的那一刻强制离场。

    单看 5 笔不能下结论，所以拿 250 个交易日的日线做同一信号的出场对照。

数据：
    state/cache/daykline_pool_250.json（10 只 ETF，2025-09-30 ~ 2026-10-19）
    这是日线，只能近似入场信号（收盘价相对 5 日均线偏离 + 当日下跌），
    不能还原分钟级的 VWAP/动量确认。所以本实验**只用来比较出场规则**，
    不用来判断入场信号本身好坏。

费用口径（1 万本金，真实费率）：
    佣金 5 元下限 × 2 = 10 元 = 0.10%
    滑点按 1 个 tick / 边 ≈ 0.02% × 2 = 0.04%
    单次往返合计 ≈ 0.14%

用法：
    python engine/exitstudy.py
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, "state", "cache", "daykline_pool_250.json")

COST = 0.0014          # 单次往返总成本（佣金 + 滑点）
MA_N = 5               # 偏离基准均线
DEV_ENTER = -0.008     # 收盘价低于 5 日均线 0.8% 才考虑入场
DAY_DROP = -0.004      # 当日至少跌 0.4%
MAX_HOLD = 3           # 最长持有交易日（不含买入当日）


def load():
    with open(CACHE, "r", encoding="utf-8") as f:
        d = json.load(f)
    out = {}
    for item in d["data"]["data"]:
        sym = item.get("symbol") or ""
        code = sym[2:]
        nodes = (item.get("data") or {}).get("nodes") or []
        if not nodes:
            continue
        bars = sorted(nodes, key=lambda n: n["date"])
        out[code] = bars
    return out


def ma(bars, i, n):
    if i < n:
        return None
    return sum(b["last"] for b in bars[i - n + 1:i + 1]) / n


def find_entries(bars):
    """近似入场点：收盘低于 MA5 超过 0.8%，且当日跌幅超过 0.4%。"""
    idx = []
    for i in range(MA_N, len(bars) - MAX_HOLD - 1):
        b = bars[i]
        m = ma(bars, i, MA_N)
        if not m:
            continue
        dev = b["last"] / m - 1
        day = b["last"] / bars[i - 1]["last"] - 1
        if dev <= DEV_ENTER and day <= DAY_DROP:
            idx.append(i)
    return idx


def ret(entry, exit_px):
    return exit_px / entry - 1 - COST


def exits(bars, i):
    """返回各种出场规则的成交价。i 是买入当日（收盘买入）。"""
    e = bars[i]["last"]
    nxt = bars[i + 1]
    out = {}
    # E0 次日开盘（≈ 现行「次日 09:31 卖出」）
    out["次日开盘卖(现行)"] = nxt["open"]
    # E1 次日收盘卖
    out["次日收盘卖"] = nxt["last"]
    # E2 回本就卖：从次日起，收盘价 >= 成本即卖，否则持到第 MAX_HOLD 日收盘
    v = None
    for k in range(1, MAX_HOLD + 1):
        b = bars[i + k]
        if b["last"] >= e:
            v = b["last"]
            break
        if b["high"] >= e:          # 盘中触及成本价也算回本
            v = e
            break
    out["回本即卖(≤3日)"] = v if v is not None else bars[i + MAX_HOLD]["last"]
    # E3 止盈 +1.5% / 止损 -3%，盘中触发优先，否则持到第 3 日收盘
    tp, sl = e * 1.015, e * 0.97
    v = None
    for k in range(1, MAX_HOLD + 1):
        b = bars[i + k]
        if b["low"] <= sl:
            v = sl
            break
        if b["high"] >= tp:
            v = tp
            break
    out["止盈1.5%/止损3%(≤3日)"] = v if v is not None else bars[i + MAX_HOLD]["last"]
    # E4 固定持有 2 日收盘
    out["持两日收盘卖"] = bars[i + 2]["last"]
    return {k: ret(e, x) for k, x in out.items()}


def simulate(data, rule, one_at_a_time=True):
    """单仓顺序回测：同一时间只持一仓，按日期推进。"""
    events = []
    for code, bars in data.items():
        for i in find_entries(bars):
            events.append((bars[i]["date"], code, i))
    events.sort()
    if not one_at_a_time:
        return [exits(data[c], i)[rule] for _, c, i in events]

    equity, busy_until, taken = 1.0, "", []
    for d, c, i in events:
        if d <= busy_until:
            continue
        r = exits(data[c], i)[rule]
        equity *= (1 + r)
        busy_until = data[c][i + MAX_HOLD]["date"]
        taken.append(r)
    return taken


def main():
    data = load()
    rules = ["次日开盘卖(现行)", "次日收盘卖", "回本即卖(≤3日)",
             "止盈1.5%/止损3%(≤3日)", "持两日收盘卖"]

    print("=" * 74)
    print("出场规则对照（同一批入场信号，只换出场）")
    print("=" * 74)

    # 全样本（每个信号都进场，用于看规则本身的质量）
    print("\n【口径 A】每个信号都独立进场（不限仓位，纯粹比较出场规则）")
    print("%-24s %6s %8s %8s %8s %10s" % ("出场规则", "笔数", "胜率", "单笔均值", "累计", "最差一笔"))
    base = None
    for r in rules:
        rs = simulate(data, r, one_at_a_time=False)
        win = sum(1 for x in rs if x > 0) / len(rs)
        cum = 1.0
        for x in rs:
            cum *= (1 + x)
        print("%-24s %6d %7.1f%% %7.2f%% %7.2f%% %9.2f%%" % (
            r, len(rs), win * 100, sum(rs) / len(rs) * 100,
            (cum - 1) * 100, min(rs) * 100))
        if base is None:
            base = sum(rs) / len(rs)

    print("\n【口径 B】单仓顺序（同一时间只持一仓，贴近实盘约束）")
    print("%-24s %6s %8s %8s %10s" % ("出场规则", "笔数", "胜率", "单笔均值", "累计"))
    for r in rules:
        rs = simulate(data, r, one_at_a_time=True)
        win = sum(1 for x in rs if x > 0) / len(rs)
        cum = 1.0
        for x in rs:
            cum *= (1 + x)
        print("%-24s %6d %7.1f%% %7.2f%% %9.2f%%" % (
            r, len(rs), win * 100, sum(rs) / len(rs) * 100, (cum - 1) * 100))

    print("\n【参照】同期买入并持有（2025-09-30 ~ 2026-10-19，250 个交易日）")
    for code, bars in sorted(data.items()):
        bh = bars[-1]["last"] / bars[0]["open"] - 1
        print("  %s  %+.2f%%" % (code, bh * 100))
    print("\n注：单次往返成本按 %.2f%% 计（佣金 5 元×2 + 滑点）。" % (COST * 100))
    print("    日线近似入场，只见出场规则之差，不代表现有分钟信号的优劣。")


if __name__ == "__main__":
    main()
