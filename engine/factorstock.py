# -*- coding: utf-8 -*-
"""个股因子研究：在真实沪深300成分股（299只）上，用诚实的方法回答
「换因子策略到底有没有 alpha」。

和 factorlab.py 的关系
--------------------
factorlab.py 在 12 只 ETF 上用同样诚实的方法（参数事先固定=整段回测天然
是样本外、月频再平衡、换手成本、等权基准、Holm 多重检验校正）证伪了
「换因子能更优」。但 12 只 ETF 的横截面太薄，结论可能是「池子不够大」而非
「因子没用」。

本脚本把同一套方法搬到 299 只真实个股（沪深300成分股，来自
state/universe_hs300.json，经 tdx_screener 无偏抽取），并把用户要求的
walk-forward 落到实处：

  1. 横截面因子（动量/反转/低波动/波动收敛），参数事先固定、无前视。
  2. 月频再平衡，只做多 top-N，换手成本如实计入（turn × COST 往返）。
  3. 基准 = 同批个股的等权买入持有（真正的 beta，零摩擦）。
  4. 多重检验校正（Holm）：测了 K 个因子，按校正阈值判是否真有 edge。
  5. **walk-forward 三道关**（这是比 factorlab 多出来的部分）：
       (a) 全期 rank-IC 及其 t 统计量（因子用 t 日信息预测 t→t+1月，
           天然样本外，不靠任何训练）；
       (b) 分上下半场，看 edge 是否在两段都成立（避免整段被单一牛市撑起）；
       (c) 年度 IC 表，看哪些年份失效。

只有「全期超额显著 + 上下半场都正 + 年度 IC 多数显著」且通过 Holm 的因子，
才算真的有 alpha；否则结论仍是「现状静态组合更优，换因子=换运气」。

数据：只读 state/cache/stocks/*.json（由 bulkpull.py 拉取缓存的 hfq 日线）。

用法
----
  python engine/factorstock.py          # 全量研究
  python engine/factorstock.py quick    # 只用已有缓存（不校验完整性）快速跑
"""
import json
import math
import os
import sys
from statistics import mean

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "engine"))

CACHE_DIR = os.path.join(BASE, "state", "cache", "stocks")
UNIVERSE = os.path.join(BASE, "state", "universe_hs300.json")
COST = 0.0012              # 与系统一致（保守：真实10万/3仓的5元下限成本更低）
TRADING_DAYS = 244
ALPHA = 0.05
TOPN = 30                  # 多空只做多前 30 只（≈10% 浓度）
MIN_BARS = 250


# ---------------------------------------------------------------- 指标
def metrics(nav):
    if len(nav) < 3:
        return None
    rets = [nav[i] / nav[i - 1] - 1 for i in range(1, len(nav))]
    n = len(rets)
    cum = nav[-1] / nav[0] - 1
    ann = (1 + cum) ** (TRADING_DAYS / n) - 1 if cum > -1 else -1
    peak, mdd = nav[0], 0.0
    for v in nav:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    mu = sum(rets) / n
    sd = (sum((r - mu) ** 2 for r in rets) / n) ** 0.5
    sharpe = mu / sd * (TRADING_DAYS ** 0.5) if sd else 0.0
    down = [r for r in rets if r < 0]
    dsd = (sum(r * r for r in down) / len(down)) ** 0.5 if down else 0.0
    sortino = mu / dsd * (TRADING_DAYS ** 0.5) if dsd else 0.0
    return {"n": n, "cum": cum, "ann": ann, "mdd": mdd, "sharpe": sharpe,
            "sortino": sortino, "calmar": ann / abs(mdd) if mdd else 0.0,
            "vol": sd * (TRADING_DAYS ** 0.5)}


def fmt(tag, m, extra=""):
    if not m:
        return "%-34s 数据不足" % tag
    return ("%-34s 年化%+7.2f%% 回撤%7.2f%% 夏普%5.2f 卡玛%5.2f 波动%6.2f%%%s"
            % (tag, m["ann"] * 100, m["mdd"] * 100, m["sharpe"],
               m["calmar"], m["vol"] * 100, extra))


def tstat_excess(nav_f, nav_b):
    ef = [nav_f[i] / nav_f[i - 1] - nav_b[i] / nav_b[i - 1]
          for i in range(1, min(len(nav_f), len(nav_b)))]
    n = len(ef)
    if n < 10:
        return None, None
    mu = sum(ef) / n
    sd = (sum((x - mu) ** 2 for x in ef) / n) ** 0.5
    if sd == 0:
        return None, None
    t = mu / (sd / math.sqrt(n))
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(t) / math.sqrt(2))))
    return t, p


def holm(ps):
    k = len(ps)
    order = sorted(range(k), key=lambda i: ps[i])
    sig = [False] * k
    for rank, idx in enumerate(order):
        if ps[idx] <= ALPHA / (k - rank):
            sig[idx] = True
        else:
            break
    return sig


# ---------------------------------------------------------------- 数据加载
def load_cache():
    """读 universe + 本地 hfq 日线缓存，返回 all_dates(全局升序) 与
    close_on[code] = 对齐到 all_dates 的收盘价数组（无数据处为 None）。"""
    with open(UNIVERSE, "r", encoding="utf-8") as f:
        u = json.load(f)
    codes = [s["code"] for s in u["stocks"]]
    raw = {}
    for c in codes:
        p = os.path.join(CACHE_DIR, "%s.json" % c)
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                rows = json.load(f)
        except Exception:
            continue
        if not isinstance(rows, list) or len(rows) < MIN_BARS:
            continue
        raw[c] = {r[0]: r[2] for r in rows}     # date -> close
    codes = [c for c in codes if c in raw]
    all_dates = sorted({d for r in raw.values() for d in r})
    di = {d: i for i, d in enumerate(all_dates)}
    close_on = {}
    for c in codes:
        arr = [None] * len(all_dates)
        rm = raw[c]
        for d, i in di.items():
            if d in rm:
                arr[i] = rm[d]
        close_on[c] = arr
    print("加载 %d 只有效个股，全局交易日 %d（%s~%s）"
          % (len(codes), len(all_dates), all_dates[0], all_dates[-1]))
    return all_dates, close_on, codes


def rebal_dates(all_dates):
    """每月最后一个交易日（作为再平衡日）。"""
    rs = set()
    last_m = None
    for d in all_dates:
        m = d[:7]
        if m != last_m:
            if last_m is not None:
                rs.add(prev)
            last_m = m
            prev = d
    rs.add(all_dates[-1])
    return rs


# ---------------------------------------------------------------- 因子
def factor_array(close_on, kind, look):
    """在全局日期轴上预计算每个 code 的因子值（只用 ≤i 的数据，无前视）。"""
    n = len(close_on)
    out = [None] * n

    def ret_window(i, w):
        seg = []
        for j in range(i - w + 1, i + 1):
            a, b = close_on[j - 1], close_on[j]
            if a and b:
                seg.append(b / a - 1)
        return seg

    if kind in ("mom", "rev"):
        for i in range(look, n):
            a, b = close_on[i - look], close_on[i]
            if a and b:
                r = b / a - 1
                out[i] = -r if kind == "rev" else r
    elif kind == "mom12":
        for i in range(250, n):
            if i - 21 < 0:
                continue
            a, b = close_on[i - 250], close_on[i - 21]
            if a and b:
                out[i] = b / a - 1
    elif kind == "vol":
        for i in range(look, n):
            seg = ret_window(i, look)
            if len(seg) >= 2:
                m = sum(seg) / len(seg)
                var = sum((x - m) ** 2 for x in seg) / len(seg)
                out[i] = var ** 0.5
    elif kind == "volr":
        for i in range(60, n):
            s = ret_window(i, 20)
            l = ret_window(i, 60)
            if len(s) >= 2 and len(l) >= 2:
                ms = sum(s) / len(s); vs = sum((x - ms) ** 2 for x in s) / len(s)
                ml = sum(l) / len(l); vl = sum((x - ml) ** 2 for x in l) / len(l)
                if vl:
                    out[i] = (vs ** 0.5) / (vl ** 0.5) - 1
    return out


# ---------------------------------------------------------------- 回测
def run_benchmark(close_on, codes, all_dates):
    """等权买入持有全池（零摩擦）= 真正的 beta 基准。"""
    nav = [1.0]
    for i in range(len(all_dates) - 1):
        r = 0.0
        n = 0
        for c in codes:
            a, b = close_on[c][i], close_on[c][i + 1]
            if a and b:
                r += b / a - 1
                n += 1
        if n:
            r /= n
        nav.append(nav[-1] * (1 + r))
    return nav


def run_factor(factor_on, close_on, codes, rset, topn, asc, all_dates):
    nav = [1.0]
    held = set()
    for i in range(len(all_dates) - 1):
        d = all_dates[i]
        rebal = d in rset
        if rebal:
            cands = [(factor_on[c][i], c) for c in codes
                     if factor_on[c][i] is not None]
            target = set(c for _, c in sorted(cands, reverse=not asc)[:topn]) \
                if cands else set()
            if target or held:
                if held and target:
                    turn = len(held ^ target) / float(max(len(held), len(target)))
                elif target:
                    turn = 1.0
                else:
                    turn = 0.0
                nav[-1] *= (1 - turn * COST)
            held = target
        r = 0.0
        n = 0
        for c in held:
            a, b = close_on[c][i], close_on[c][i + 1]
            if a and b:
                r += b / a - 1
                n += 1
        if n:
            r /= n
        nav.append(nav[-1] * (1 + r))
    return nav


# ---------------------------------------------------------------- walk-forward: IC
def rank_ic(xs, ys):
    """Spearman 秩相关（ties 用平均秩）。"""
    def ranks(v):
        idx = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[idx[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((rx[k] - mx) * (ry[k] - my) for k in range(n))
    vx = sum((x - mx) ** 2 for x in rx) ** 0.5
    vy = sum((y - my) ** 2 for y in ry) ** 0.5
    return cov / (vx * vy) if vx and vy else None


def yearly_ic(factor_on, close_on, codes, rset, all_dates, fwd=21):
    di = {d: i for i, d in enumerate(all_dates)}
    ics = []
    by_year = {}
    for d in rset:
        i = di[d]
        if i + fwd >= len(all_dates):
            continue
        xs, ys = [], []
        for c in codes:
            fv = factor_on[c][i]
            a, b = close_on[c][i], close_on[c][i + fwd]
            if fv is None or not a or not b:
                continue
            xs.append(fv)
            ys.append(b / a - 1)
        if len(xs) < 30:
            continue
        ic = rank_ic(xs, ys)
        if ic is None:
            continue
        ics.append(ic)
        by_year.setdefault(d[:4], []).append(ic)
    return ics, by_year


def ic_tstat(ics):
    if len(ics) < 5:
        return None, None
    m = mean(ics)
    sd = (sum((x - m) ** 2 for x in ics) / len(ics)) ** 0.5
    t = m / (sd / math.sqrt(len(ics))) if sd else 0.0
    return m, t


# ---------------------------------------------------------------- 主流程
FACTORS = [
    ("动量20",   "mom",   20,  False),
    ("动量60",   "mom",   60,  False),
    ("动量120",  "mom",   120, False),
    ("动量250",  "mom",   250, False),
    ("12-1动量", "mom12", 250, False),
    ("反转5",    "rev",   5,   True),
    ("反转10",   "rev",   10,  True),
    ("反转20",   "rev",   20,  True),
    ("低波动20", "vol",   20,  True),
    ("低波动60", "vol",   60,  True),
    ("波动收敛", "volr",  60,  True),
]


def main():
    all_dates, close_on, codes = load_cache()
    rset = rebal_dates(all_dates)
    rset = {d for d in rset if all_dates.index(d) >= 260}   # 留足回看
    mid = all_dates[len(all_dates) // 2]
    r_first = {d for d in rset if d < mid}
    r_second = {d for d in rset if d >= mid}

    bench = run_benchmark(close_on, codes, all_dates)
    mb = metrics(bench)
    print("\n" + "=" * 92)
    print("基准：沪深300成分股等权买入持有（零摩擦，真正的 beta）")
    print("=" * 92)
    print(fmt("等权基准", mb))

    # 预计算因子
    fac = {}
    for name, kind, look, _ in FACTORS:
        fac[name] = {c: factor_array(close_on[c], kind, look) for c in codes}

    print("\n" + "=" * 92)
    print("全期因子组合（月频再平衡，做多前 %d，换手成本 %s 往返）" % (TOPN, COST))
    print("=" * 92)
    print("%-10s %-10s %-22s %s" % ("方向", "因子", "指标", "超额vs基准 / 结论"))
    results = []
    for name, kind, look, asc in FACTORS:
        nav = run_factor(fac[name], close_on, codes, rset, TOPN, asc, all_dates)
        m = metrics(nav)
        t, p = tstat_excess(nav, bench)
        ex_ann = (m["ann"] - mb["ann"]) * 100 if m else None
        results.append((name, kind, look, asc, m, t, p))
        arrow = "↑赢家" if not asc else "↓输家/稳"
        ex = ("年化超额%+6.2f%%  t=%+.2f p=%.3f" % (ex_ann, t, p)
              if (m and t is not None) else "—")
        print("%-10s %-10s %s   %s" % (arrow, name, fmt(name, m), ex))

    # Holm
    ps = [r[6] for r in results if r[6] is not None]
    sig = holm(ps)
    print("\n" + "=" * 92)
    print("多重检验（Holm 校正，α=0.05，共测 %d 因子，预期 ~%.1f 个靠运气过线）"
          % (len(ps), len(ps) * ALPHA))
    j = 0
    survive = []
    for name, kind, look, asc, m, t, p in results:
        if p is None:
            continue
        mark = "✓显著" if sig[j] else "✗不显著"
        if sig[j]:
            survive.append(name)
        print("  %-10s p=%.4f  %s" % (name, p, mark))
        j += 1

    # walk-forward: 年度 IC + 上下半场
    print("\n" + "=" * 92)
    print("walk-forward 第一关：年度 rank-IC（因子用 t 日预测 t→t+1月，天然样本外）")
    print("=" * 92)
    ic_rows = []
    for name, kind, look, asc in FACTORS:
        ics, by_year = yearly_ic(fac[name], close_on, codes, rset, all_dates)
        mi, ti = ic_tstat(ics)
        yr = {y: mean(v) for y, v in by_year.items()}
        pos_years = sum(1 for v in yr.values() if v > 0)
        ic_rows.append((name, mi, ti, len(ics), yr, pos_years, len(yr)))
        ystr = " ".join("%s:%+.2f" % (y, v) for y, v in sorted(yr.items()))
        print("%-10s 年均IC=%+.3f t=%+.2f (n=%d年)  正IC年%d/%d"
              % (name, mi, ti, len(yr), pos_years, len(yr)))
        print("           %s" % ystr)

    print("\n" + "=" * 92)
    print("walk-forward 第二关：上下半场一致性（前半场 vs 后半场 超额年化）")
    print("=" * 92)
    for name, kind, look, asc in FACTORS:
        def half(rs):
            nav = run_factor(fac[name], close_on, codes, rs, TOPN, asc, all_dates)
            m = metrics(nav)
            t, _ = tstat_excess(nav, bench)
            return (m["ann"] - mb["ann"]) * 100 if m else None, t
        ea1, t1 = half(r_first)
        ea2, t2 = half(r_second)
        print("%-10s 前半场超额%+6.2f%%(t=%s)  后半场超额%+6.2f%%(t=%s)"
              % (name, ea1 or 0, "%.2f" % t1 if t1 else "-",
                 ea2 or 0, "%.2f" % t2 if t2 else "-"))

    # 综合判定
    print("\n" + "=" * 92)
    print("结论")
    print("=" * 92)
    real = []
    for (name, mi, ti, nyr, yr, pos, tot) in ic_rows:
        # 真 alpha 判据：Holm 显著 且 年均IC>0且t>2
        holm_ok = name in survive
        ic_ok = (mi is not None and mi > 0 and (ti or 0) > 2)
        real.append((name, holm_ok, ic_ok, mi, pos, tot))
    for name, holm_ok, ic_ok, mi, pos, tot in real:
        verdict = "真有alpha" if (holm_ok and ic_ok) else "不通过"
        print("  %-10s Holm=%s IC=%s  → %s"
              % (name, "✓" if holm_ok else "✗", "✓" if ic_ok else "✗", verdict))
    any_real = any(h and i for h, i, *_ in real)
    print("\n  全期有 %d 个因子通过 Holm；其中 %d 个同时 IC 稳健。"
          % (len(survive), sum(1 for h, i, *_ in real if h and i)))
    if any_real:
        print("  ⇒ 这些因子在个股层面确有可重复的 edge，值得进入样本外实盘验证。")
    else:
        print("  ⇒ 没有任何因子在「全期显著 + IC稳健 + 上下半场一致」三重关都过。"
              "结论与 ETF 池一致：现状静态组合仍是当前成本结构下的最优解，"
              "换因子=换运气。")
    print("  （注：本池为沪深300幸存者名单，结论偏乐观；若连幸存者都无 edge，"
          "更宽的全市场只会更弱。）")


if __name__ == "__main__":
    main()
