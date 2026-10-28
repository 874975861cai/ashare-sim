#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则搜索：在 3 年日线上找「样本内外都成立」的低换手规则。

为什么要有这个工具
------------------
2026-10-28 之前两次失败，根因不同：

1. 日内均值回归：入场信号本身没有 edge（换什么出场都不赚钱）。
2. 趋势跟随 MA20：97 笔 / 923 日，单次往返成本约 0.18%，
   光手续费就吃掉 97*0.18% = 17.5%，-41% 里有一半是成本。

所以这次搜索的第一硬约束不是「收益多高」，而是 **换手足够低**——
低到交易成本在结果里是小数点后的噪音，而不是主导项。

所有结果都必须按样本内/样本外分开报。只看合并数字等于自欺：
2026-10-28 那次的「恐慌买入 +11%」就是合并 250 天（恰好全是样本外）算出来的。

执行假设（与实盘引擎一致）
--------------------------
- 信号在 T 日收盘确认，T+1 日**开盘**成交（T+1 制度下这是最早可实现点）
- 单次往返成本 COST=0.18%（佣金 5 元下限往返 + 一个 tick 滑点）
- 单仓：一次只持一只（1 万本金分仓会被 5 元佣金下限罚两次）
"""

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, "state", "cache", "daykline_3y.json")

COST = 0.0018          # 单次往返
IS = ("2023-01-01", "2025-06-30")     # 样本内
OOS = ("2025-07-01", "2026-12-31")    # 样本外


def load():
    with open(CACHE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    data = {}
    for c, kl in raw.items():
        bars = []
        for line in kl:
            p = line.split(",")
            if len(p) < 5:
                continue
            bars.append({"date": p[0], "open": float(p[1]), "close": float(p[2]),
                         "high": float(p[3]), "low": float(p[4])})
        data[c] = bars
    return data


def ma(bars, i, n):
    if i - n + 1 < 0:
        return None
    return sum(b["close"] for b in bars[i - n + 1:i + 1]) / n


def stat(rets):
    if not rets:
        return None
    cum = 1.0
    for r in rets:
        cum *= (1 + r)
    win = sum(1 for r in rets if r > 0) / len(rets)
    return {"n": len(rets), "win": win * 100,
            "avg": sum(rets) / len(rets) * 100,
            "cum": (cum - 1) * 100,
            "worst": min(rets) * 100,
            "best": max(rets) * 100}


def line(tag, s):
    if not s:
        return "%-30s %s" % (tag, "0 笔")
    return ("%-30s %3d笔 胜率%5.1f%% 单笔%+6.2f%% 累计%+7.2f%% 最差%+6.2f%%"
            % (tag, s["n"], s["win"], s["avg"], s["cum"], s["worst"]))


# ---------------------------------------------------------------- 规则族
def r_trend(bars, fast, slow, band=0.0):
    """快线上穿慢线且都上行 → 持有；跌破 → 空仓。

    band: 需要跌破慢线 band 比例才离场，避免均线附近反复摩擦。
    """
    sig = [0] * len(bars)
    hold = False
    for i in range(len(bars)):
        f, s = ma(bars, i, fast), ma(bars, i, slow)
        if f is None or s is None:
            continue
        s_prev = ma(bars, i - 5, slow)
        if not hold:
            if f > s * (1 + band) and s_prev is not None and s > s_prev:
                hold = True
        else:
            if f < s * (1 - band):
                hold = False
        sig[i] = 1 if hold else 0
    return sig


def r_price_ma(bars, n, band=0.0):
    """收盘价站上 N 日均线 n 日 → 持有。经典时序动量。"""
    sig = [0] * len(bars)
    hold = False
    for i in range(len(bars)):
        m = ma(bars, i, n)
        if m is None:
            continue
        if not hold and bars[i]["close"] > m * (1 + band):
            hold = True
        elif hold and bars[i]["close"] < m * (1 - band):
            hold = False
        sig[i] = 1 if hold else 0
    return sig


def r_absmom(bars, look, band=0.0):
    """过去 look 日收益为正 → 持有（绝对动量）。"""
    sig = [0] * len(bars)
    for i in range(len(bars)):
        if i < look:
            continue
        r = bars[i]["close"] / bars[i - look]["close"] - 1
        sig[i] = 1 if r > band else 0
    return sig


def r_donchian(bars, n):
    """突破 N 日最高价入场，跌破 N 日最低价离场。"""
    sig = [0] * len(bars)
    hold = False
    for i in range(len(bars)):
        if i < n:
            continue
        hi = max(b["high"] for b in bars[i - n:i])
        lo = min(b["low"] for b in bars[i - n:i])
        if not hold and bars[i]["close"] > hi:
            hold = True
        elif hold and bars[i]["close"] < lo:
            hold = False
        sig[i] = 1 if hold else 0
    return sig


def r_voltarget(bars, n=20, target=0.20):
    """波动率低于目标就持有（低波动异象的粗糙代理）。"""
    import math
    sig = [0] * len(bars)
    for i in range(len(bars)):
        if i < n + 1:
            continue
        rets = [bars[k]["close"] / bars[k - 1]["close"] - 1
                for k in range(i - n + 1, i + 1)]
        sd = math.sqrt(sum(r * r for r in rets) / len(rets))
        ann = sd * math.sqrt(244)
        sig[i] = 1 if ann < target else 0
    return sig


# ---------------------------------------------------------------- 回测
def backtest(bars, sig, lo, hi):
    """信号 T 日收盘确认 → T+1 开盘成交。返回每笔收益列表。"""
    rets = []
    entry = None
    for i in range(len(bars) - 1):
        want = sig[i]
        nxt = bars[i + 1]
        if not (lo <= nxt["date"] < hi):
            # 区间外只更新持仓状态，不记录
            if entry is not None and want == 0:
                entry = None
            continue
        if want == 1 and entry is None:
            entry = nxt["open"]
        elif want == 0 and entry is not None:
            rets.append(nxt["open"] / entry - 1 - COST)
            entry = None
    if entry is not None and lo <= bars[-1]["date"] < hi:
        rets.append(bars[-1]["close"] / entry - 1 - COST)
    return rets


def main():
    data = load()
    codes = sorted(data)
    print("标的 %d 只，日线 %d 根（%s ~ %s）\n"
          % (len(codes), len(data[codes[0]]),
             data[codes[0]][0]["date"], data[codes[0]][-1]["date"]))

    # 基准：等权持有 / 单只买入持有
    print("=" * 96)
    print("基准")
    print("=" * 96)
    for c in codes:
        b = data[c]
        isr = b[[x["date"] for x in b].index(
            [x["date"] for x in b if x["date"] >= IS[0]][0])]
        # 用区间首尾算买入持有
        def bh(lo, hi):
            seg = [x for x in b if lo <= x["date"] < hi]
            if len(seg) < 2:
                return None
            return (seg[-1]["close"] / seg[0]["open"] - 1) * 100
        a, o = bh(*IS), bh(*OOS)
        print("%-8s 样本内 %+8.2f%%   样本外 %+8.2f%%"
              % (c, a if a is not None else 0, o if o is not None else 0))

    print()
    print("=" * 96)
    print("规则族 A：双均线趋势（快慢线上穿持有，跌破空仓）")
    print("=" * 96)
    for code in ("510300", "510880", "512100", "159915", "588000"):
        bars = data[code]
        for fast, slow in ((10, 60), (20, 60), (20, 120), (5, 40), (10, 100)):
            sig = r_trend(bars, fast, slow)
            a = stat(backtest(bars, sig, *IS))
            o = stat(backtest(bars, sig, *OOS))
            print(line("%s MA%d/MA%d 内" % (code, fast, slow), a))
            print(line("%s MA%d/MA%d 外" % (code, fast, slow), o))
            print()

    print("=" * 96)
    print("规则族 B：价格 vs 单均线（时序动量）")
    print("=" * 96)
    for code in ("510300", "510880", "512100", "159915"):
        bars = data[code]
        for n in (60, 100, 120, 200):
            sig = r_price_ma(bars, n)
            a = stat(backtest(bars, sig, *IS))
            o = stat(backtest(bars, sig, *OOS))
            print(line("%s 价格>MA%d 内" % (code, n), a))
            print(line("%s 价格>MA%d 外" % (code, n), o))
            print()

    print("=" * 96)
    print("规则族 C：绝对动量（过去 N 日为正则持有）")
    print("=" * 96)
    for code in ("510300", "510880", "512100", "159915"):
        bars = data[code]
        for look in (20, 60, 120, 200):
            sig = r_absmom(bars, look)
            a = stat(backtest(bars, sig, *IS))
            o = stat(backtest(bars, sig, *OOS))
            print(line("%s 动量%d 内" % (code, look), a))
            print(line("%s 动量%d 外" % (code, look), o))
            print()

    print("=" * 96)
    print("规则族 D：唐奇安通道突破")
    print("=" * 96)
    for code in ("510300", "510880", "512100", "159915"):
        bars = data[code]
        for n in (20, 55, 120):
            sig = r_donchian(bars, n)
            a = stat(backtest(bars, sig, *IS))
            o = stat(backtest(bars, sig, *OOS))
            print(line("%s 通道%d 内" % (code, n), a))
            print(line("%s 通道%d 外" % (code, n), o))
            print()

    print("=" * 96)
    print("规则族 E：低波动持有（年化波动 < 目标则持仓）")
    print("=" * 96)
    for code in ("510300", "510880", "512100", "159915"):
        bars = data[code]
        for n, t in ((20, 0.15), (20, 0.20), (60, 0.18)):
            sig = r_voltarget(bars, n, t)
            a = stat(backtest(bars, sig, *IS))
            o = stat(backtest(bars, sig, *OOS))
            print(line("%s 波动%d/%0.2f 内" % (code, n, t), a))
            print(line("%s 波动%d/%0.2f 外" % (code, n, t), o))
            print()


if __name__ == "__main__":
    main()
