# -*- coding: utf-8 -*-
"""因子实验室：用正确方法回答「要不要重新挖掘因子」。

为什么写这个，而不是直接改策略
------------------------------
用户问「要不要重新挖掘因子、换因子策略」。之前的 rule_search / rule_search2 /
exitstudy 已经在这个池子 + 这套成本上做过穷举，结论是「样本内外同时为正 +
笔数够」的候选 = 0。但那个结论是用「整段回测挑最好参数」的方法得出的，
本身就有前视 + 多重检验偏差。所以这次不重复旧的毛病，改成：

  1. 只在池内做横截面因子（动量/反转/波动/量能），每个因子的参数事先固定，
     不在整段数据上挑——参数固定 = 整段回测天然是样本外。
  2. 换手成本如实计入（COST/2 单边，每月再平衡）。
  3. 比较基准是「同一批资产的等权买入持有」（也就是 beta 本身），因子要赢的
     是它，不是无风险利率。
  4. 多重检验校正：我们测了 K 个因子，必然有 ~K*5% 个靠运气过线。用 Holm
     逐步校正，只有校正后仍显著的才算「真有 edge」。

这直接回答用户的问题：这套 18 只 ETF + 日线 + 5 元佣金下限的成本结构下，
到底有没有任何一个因子能在扣除成本后稳定跑赢 beta。有 → 值得做因子策略；
没有 → 现状（静态 beta 组合）就是最优解，换因子是换运气。

用法
----
  python engine/factorlab.py        # 跑全部因子 + 多重检验校正
"""
import json
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hiret as H

COST = H.COST
TRADING_DAYS = H.TRADING_DAYS
ALPHA = 0.05


# ---------------------------------------------------------------- 数据
def build_series():
    data = H.load()
    out = {}
    for c, rows in data.items():
        dates = [r["date"] for r in rows]
        closes = [float(r["close"]) for r in rows]
        out[c] = (dates, closes, {d: i for i, d in enumerate(dates)})
    return out


# ---------------------------------------------------------------- 因子
def fval(series, c, d, kind, look):
    """在日期 d 收盘算因子值，只用 d 及之前的数据（无前视）。"""
    dates, closes, d2i = series[c]
    if d not in d2i:
        return None
    p = d2i[d]
    if kind in ("mom", "rev"):
        if p < look:
            return None
        ret = closes[p] / closes[p - look] - 1
        return -ret if kind == "rev" else ret
    if kind == "mom12":
        # 12-1 动量：t-250 -> t-21 的收益，跳过最近 1 月以隔离短期反转
        if p < look or p - 21 < 0:
            return None
        return closes[p - 21] / closes[p - look] - 1
    if kind == "vol":
        if p < look:
            return None
        rs = [closes[i] / closes[i - 1] - 1 for i in range(p - look + 1, p + 1)]
        if len(rs) < 2:
            return None
        m = sum(rs) / len(rs)
        var = sum((r - m) ** 2 for r in rs) / len(rs)
        return var ** 0.5
    if kind == "volr":
        # 短期波动 / 长期波动 - 1：<0 波动收敛（偏稳），>0 波动放大
        if p < 60:
            return None
        def v(w):
            rs = [closes[i] / closes[i - 1] - 1 for i in range(p - w + 1, p + 1)]
            m = sum(rs) / len(rs)
            return (sum((r - m) ** 2 for r in rs) / len(rs)) ** 0.5
        s, l = v(20), v(60)
        return (s / l - 1) if l else None
    return None


# ---------------------------------------------------------------- 回测
# 关键：NAV 必须是「日频」序列，metrics() 才能正确年化。
# 之前把月频再平衡的净值当日频年化，数字虚高 ~20 倍，已修正。
def rebal_set(all_dates, lo, hi):
    rs = set()
    last_m = None
    for d in all_dates:
        if not (lo <= d <= hi):
            continue
        m = d[:7]
        if m != last_m:
            rs.add(d)
            last_m = m
    return rs


def daily_nav(series, all_dates, codes, hold_fn, rset):
    """逐日推进，hold_fn(d, prev) 返回 (新持仓, 是否调仓)。返回日频净值。"""
    nav = [1.0]
    prev = set()
    for i, d in enumerate(all_dates):
        new_hold, rebal = hold_fn(d, prev) if d in rset else (prev, False)
        r = 0.0
        n = 0
        for c in new_hold:
            dates, closes, d2i = series[c]
            if d not in d2i:
                continue
            pi = d2i[d]
            if pi == 0:
                continue
            pd = all_dates[i - 1]
            if pd not in d2i:
                continue
            a = closes[d2i[pd]]
            b = closes[pi]
            if a:
                r += b / a - 1
                n += 1
        r = r / n if n else 0.0
        nav.append(nav[-1] * (1 + r) * (1 - COST / 2 if rebal else 1.0))
        prev = new_hold
    return nav


def run_factor(series, all_dates, codes, kind, look, topn, ascending=False,
               lo="2006-01-01", hi="2099-12-31"):
    rset = rebal_set(all_dates, lo, hi)
    if len(rset) < 3:
        return None

    def fn(d, prev):
        cands = []
        for c in codes:
            v = fval(series, c, d, kind, look)
            if v is not None:
                cands.append((v, c))
        if not cands:
            return set(), True
        cands.sort(reverse=not ascending)
        return set(c for _, c in cands[:topn]), True

    return daily_nav(series, all_dates, codes, fn, rset)


def run_equal_weight(series, all_dates, codes, lo="2006-01-01", hi="2099-12-31"):
    rset = rebal_set(all_dates, lo, hi)
    if len(rset) < 3:
        return None

    def fn(d, prev):
        cs = [c for c in codes if d in series[c][2]]
        return set(cs), True

    return daily_nav(series, all_dates, codes, fn, rset)


# ---------------------------------------------------------------- 统计
def tstat_excess(nav_f, nav_b):
    """因子相对基准的日超额收益 t 统计量 + 近似 p 值。"""
    ef = [nav_f[i] / nav_f[i - 1] - nav_b[i] / nav_b[i - 1]
          for i in range(1, min(len(nav_f), len(nav_b)))]
    n = len(ef)
    if n < 10:
        return None, None
    mu = sum(ef) / n
    sd = (sum((x - mu) ** 2 for x in ef) / n) ** 0.5
    if sd == 0:
        return None, None
    se = sd / math.sqrt(n)
    t = mu / se
    # 双尾正态近似 p
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return t, p


def holm(ps):
    """Holm 逐步校正；返回每项的『是否仍显著』。"""
    k = len(ps)
    order = sorted(range(k), key=lambda i: ps[i])
    sig = [False] * k
    for rank, idx in enumerate(order):
        thr = ALPHA / (k - rank)
        if ps[idx] <= thr:
            sig[idx] = True
        else:
            break
    return sig


# ---------------------------------------------------------------- 主流程
FACTORS = [
    ("动量20",   "mom",   20,  False),
    ("动量60",   "mom",   60,  False),
    ("动量120",  "mom",   120, False),
    ("动量250",  "mom",   250, False),
    ("12-1动量", "mom12", 250, False),   # 跳过最近 1 月：close/d-250 - close/d-21
    ("反转5",    "rev",   5,   True),    # 近期跌多的（升序取最小=输家）
    ("反转10",   "rev",   10,  True),
    ("反转20",   "rev",   20,  True),
    ("低波动20", "vol",   20,  True),
    ("低波动60", "vol",   60,  True),
    ("波动收敛", "volr",  60,  True),
]

# 横截面池：只用「想赚 beta」的风险资产，防守品（黄金/国债）不当候选。
XSEC = ["159915", "512100", "512880", "512760", "512690", "512170",
        "512660", "515030", "513050", "513100", "510500", "510300"]


def main():
    series = build_series()
    all_dates = sorted(set().union(*[set(s[0]) for s in series.values()]))
    codes = [c for c in XSEC if c in series]
    print("资产池（有数据）: %s" % " ".join(codes))
    print("样本区间: %s ~ %s，共 %d 个交易日" % (all_dates[0], all_dates[-1],
                                              len(all_dates)))

    bench = run_equal_weight(series, all_dates, codes)
    print("\n%-12s %s" % ("基准(等权)", H.fmt("同一批资产等权买入持有", H.metrics(bench))))

    print("\n%-10s %-22s %s   %s" % ("方向", "因子", "指标", "超额vs基准"))
    rows = []
    for name, kind, look, asc in FACTORS:
        nav = run_factor(series, all_dates, codes, kind, look, topn=3,
                         ascending=asc)
        m = H.metrics(nav) if nav else None
        t, p = tstat_excess(nav, bench) if nav else (None, None)
        rows.append((name, kind, look, asc, m, t, p))
        ex = ("年化超额%+6.2f%% t=%+.2f p=%.3f"
              % ((m["ann"] - H.metrics(bench)["ann"]) * 100, t, p)
              if (m and t is not None) else "—")
        print("%-10s %-22s %s   %s" % ("↑" if not asc else "↓", name,
                                       H.fmt(name, m), ex))

    # 多重检验校正
    ps = [r[6] for r in rows if r[6] is not None]
    sig = holm(ps)
    print("\n多重检验（Holm 校正，α=0.05，共测 %d 个因子，预期 ~%.1f 个靠运气过线）"
          % (len(ps), len(ps) * ALPHA))
    j = 0
    any_survive = False
    for name, kind, look, asc, m, t, p in rows:
        if p is None:
            continue
        mark = "✓显著" if sig[j] else "✗不显著"
        if sig[j]:
            any_survive = True
        print("  %-10s p=%.3f  %s" % (name, p, mark))
        j += 1

    print("\n结论:", "有因子通过校正 → 值得做因子策略并进入样本外验证"
                    if any_survive else
                    "没有任何因子在多重检验后还能跑赢 beta → "
                    "现状静态组合已是当前成本结构下的最优解，换因子=换运气")


if __name__ == "__main__":
    main()
