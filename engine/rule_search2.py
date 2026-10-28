#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则搜索 第 2 轮：跨标的结构。

第 1 轮（rule_search.py）已证伪的方向
------------------------------------
单只 ETF + 择时：8 个规则族、约 130 个参数组合，几乎没有一个在样本内为正。
尤其在震荡的 2023-01~2025-06，双均线族胜率普遍 0%~15%。

这不是参数没调好，是「单标的择时」本身在这份数据上不成立。

本轮只测一类新结构
------------------
**双重动量（Dual Momentum）**：横截面选最强 + 绝对动量过滤（为负则空仓）。

为什么它可能成立：
  1. 只持 1 只 → 1 万本金不会被 5 元佣金下限罚两次（分仓成本翻倍）；
  2. 调仓周期按周/双周 → 3 年交易次数 15~40 次，成本 2.7%~7.2%，
     而不是 MA20 那 97 次的 17.5%；
  3. 绝对动量过滤让它在系统性下跌时空仓，这是单标的择时做不到的
     （单标的只能「跟着自己的均线走」，横截面能「换到还在涨的品种」）。

同时测一个保守对照：红利底仓 + 长均线风控。
"""

import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, "state", "cache", "daykline_3y.json")

COST = 0.0018
IS = ("2023-01-01", "2025-06-30")
OOS = ("2025-07-01", "2026-12-31")


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
            "worst": min(rets) * 100}


def line(tag, s):
    if not s:
        return "%-34s %s" % (tag, "0 笔")
    return ("%-34s %3d笔 胜率%5.1f%% 单笔%+6.2f%% 累计%+7.2f%% 最差%+6.2f%%"
            % (tag, s["n"], s["win"], s["avg"], s["cum"], s["worst"]))


# ------------------------------------------------------ 双重动量
def dual_momentum(data, look, rebal, abs_band=0.0, lo=None, hi=None):
    """每 rebal 个交易日：看过去 look 日动量，选最强一只（动量为正）持有。

    返回每笔收益（一次换仓算一笔）。空仓期不计笔数。

    注意区间处理：只在 [lo, hi) 内建仓/收尾。先前版本在样本内回测时，
    收尾会用 dates[-1] 的收盘价平仓，等于把仓位一直持到 2026 年，
    两段结果互相污染。这里先框定下标范围再回测。
    """
    codes = sorted(data)
    dates = [b["date"] for b in data[codes[0]]]
    idx = {}
    for c in codes:
        idx[c] = {b["date"]: k for k, b in enumerate(data[c])}

    # 区间内允许评估的最早/最晚下标（要留一根给次日成交）
    ks = [i for i, d in enumerate(dates) if lo <= d < hi]
    if len(ks) < 3:
        return []
    i_start, i_end = ks[0], ks[-1]

    rets = []
    cur = None
    entry = None
    i = max(i_start, look + 1)
    while i < i_end:
        dt = dates[i]
        nxt = dates[i + 1]
        scores = []
        for c in codes:
            k = idx[c].get(dt)
            if k is None or k < look:
                continue
            r = data[c][k]["close"] / data[c][k - look]["close"] - 1
            scores.append((r, c))
        if not scores:
            i += 1
            continue
        scores.sort(reverse=True)
        best_r, best_c = scores[0]
        target = best_c if best_r > abs_band else None

        if target != cur:
            def px_of(code):
                k = idx[code].get(nxt)
                return data[code][k]["open"] if k is not None else None

            if cur is not None and entry is not None:
                p = px_of(cur)
                if p is not None:
                    rets.append(p / entry - 1 - COST)
            if target is not None:
                entry = px_of(target)
            else:
                entry = None
            cur = target
        i += rebal

    # 收尾：用区间内最后一个交易日的收盘价
    if cur is not None and entry is not None:
        k = idx[cur].get(dates[i_end])
        if k is not None:
            rets.append(data[cur][k]["close"] / entry - 1 - COST)
    return rets


# ------------------------------------------------------ 红利底仓 + 风控
def red_with_guard(data, code, n, rebal=5):
    """默认满仓红利，跌破 N 日均线才空仓（站回则买入）。保守对照。"""
    bars = data[code]
    rets = []
    entry = None
    for i in range(n + 1, len(bars) - 1):
        m = sum(b["close"] for b in bars[i - n + 1:i + 1]) / n
        want = bars[i]["close"] > m
        if want and entry is None:
            entry = bars[i + 1]["open"]
        elif not want and entry is not None:
            rets.append(bars[i + 1]["open"] / entry - 1 - COST)
            entry = None
    if entry is not None:
        rets.append(bars[-1]["close"] / entry - 1 - COST)
    return rets


def main():
    data = load()
    dates_all = [b["date"] for b in data[sorted(data)[0]]]
    print("日线 %d 根 %s ~ %s，标的 %d 只\n"
          % (len(dates_all), dates_all[0], dates_all[-1], len(data)))

    def slice_stat(rs, lo, hi):
        # dual_momentum 内部已按区间过滤
        return stat(rs)

    print("=" * 100)
    print("双重动量：每 R 日调仓，选过去 L 日动量最强的一只，动量为负则空仓")
    print("（换仓按次日开盘成交，一次换仓记一笔，往返成本 0.18%）")
    print("=" * 100)
    print("%-34s %s" % ("参数", "样本内 2023-01~2025-06"))
    best = []
    for look in (20, 60, 120):
        for rebal in (10, 20, 40):
            for band in (0.0, 0.01):
                a = dual_momentum(data, look, rebal, band, *IS)
                o = dual_momentum(data, look, rebal, band, *OOS)
                sa, so = stat(a), stat(o)
                tag = "回看%d/调仓%d日/阈值%.0f%%" % (look, rebal, band * 100)
                print(line(tag + " 内", sa))
                print(line(tag + " 外", so))
                if sa and so and sa["n"] >= 8 and so["n"] >= 4 \
                        and sa["cum"] > 0 and so["cum"] > 0:
                    comb = (1 + sa["cum"] / 100) * (1 + so["cum"] / 100) - 1
                    best.append((comb, tag, sa, so))
                print()

    print("=" * 100)
    print("通过「样本内外同时为正 + 笔数够」的候选")
    print("=" * 100)
    if not best:
        print("无")
    for comb, tag, sa, so in sorted(best, reverse=True):
        print("%-34s 复合%+7.2f%% (内%+6.2f%% 外%+6.2f%%) 笔数 %d+%d"
              % (tag, comb * 100, sa["cum"], so["cum"], sa["n"], so["n"]))

    print()
    print("=" * 100)
    print("保守对照：红利 510880 底仓 + 跌破 MA_N 空仓")
    print("=" * 100)
    for n in (60, 120, 200, 250):
        a = red_with_guard(data, "510880", n)
        # 分段
        def seg(rs_lo, rs_hi):
            return None
        # red_with_guard 不返回日期，改用区间重算
        bars = data["510880"]
        def run(lo, hi):
            rets = []
            entry = None
            for i in range(n + 1, len(bars) - 1):
                m = sum(b["close"] for b in bars[i - n + 1:i + 1]) / n
                want = bars[i]["close"] > m
                d = bars[i + 1]["date"]
                if not (lo <= d < hi):
                    if not want and entry is not None:
                        entry = None
                    continue
                if want and entry is None:
                    entry = bars[i + 1]["open"]
                elif not want and entry is not None:
                    rets.append(bars[i + 1]["open"] / entry - 1 - COST)
                    entry = None
            if entry is not None:
                rets.append(bars[-1]["close"] / entry - 1 - COST)
            return rets
        sa, so = stat(run(*IS)), stat(run(*OOS))
        print(line("红利+MA%d 内" % n, sa))
        print(line("红利+MA%d 外" % n, so))
        print()

    print("=" * 100)
    print("对照：红利 510880 纯买入持有")
    print("=" * 100)
    for lo, hi, nm in ((IS[0], IS[1], "样本内"), (OOS[0], OOS[1], "样本外")):
        seg = [b for b in data["510880"] if lo <= b["date"] < hi]
        if len(seg) >= 2:
            r = (seg[-1]["close"] / seg[0]["open"] - 1) * 100
            print("%-8s %+7.2f%%（%s ~ %s，%d 根）"
                  % (nm, r, seg[0]["date"], seg[-1]["date"], len(seg)))


if __name__ == "__main__":
    main()
