# -*- coding: utf-8 -*-
"""风险预算：在「无因子 alpha、只能赚 beta」的前提下，回答「该承担多大风险、
换多少收益、怎么配才最划算」。

背景（前置结论）
----------------
factorlab.py / factorstock.py 已在 12 只 ETF 和 186 只真实个股上证明：扣费后
没有任何横截面因子能稳定跑赢等权 beta。→ 收益来源只能是资产配置（beta 倾斜），
不是选股/择时。于是真问题变成：把钱分配到哪些低相关资产、承担多大回撤、换
多少年化。

方法（刻意保守，避免二次过拟合）
--------------------------------
  * 不用均值-方差最优化（历史均值极不稳定，会把仓位推到过拟合角落）。
  * 只评估「事先定好、可解释」的几组静态权重，外加风险平价（1/vol）这种
    无参数规则。
  * 组合按「年度再平衡」模拟（与 live 组合规则一致），换手成本如实计入。
  * 用几何年化、日频最大回撤、Calmar(年化/回撤)、Sharpe(无风险=0) 四个口径。
  * 同时给出各资产单独统计 + 相关矩阵（分散化到底值多少）。

口径（2026-11-14 明确，此前含糊过）
-----------------------------------
  * 年化 = 几何年化 (末/初)^(244/n)-1，和实际拿到的复利一致。
  * Sharpe = **算术日均收益 × 244 / 年化波动**，这是 Sharpe 的定义式。
    旧实现拿「几何年化 / 波动」当 Sharpe：高波动资产的几何收益被
    波动拖低（差 ≈ vol²/2），于是夏普被系统性压低——偏偏这几组候选
    比的就是不同波动率，等于用一把偏心的尺子去比。两个口径同时给出，
    差异大到会翻结论时看得出来。
  * Calmar / Sharpe 在分母为 0（无回撤 / 无波动）时是**判不了**，
    返回 None 并在表里印 "—"，不返回 0.0。旧写法返回 0.0 会让一条
    只涨不跌的曲线排到最后一名。

窗口
----
含债券的可行配置集要求 6 资产同时有数据 → 2017-08-24 起（511260 十年国债
ETF 上市日）到缓存末日。约 9.2 年，覆盖 2018 熊市、2020 疫情急跌、2021-24
A 股长熊、2025-26 修复，压力场景足够。资产单独统计用各自全历史。

用法
----
  python engine/riskbudget.py            # 打印表格 + 末尾 JSON（供画图）
"""
import datetime
import json
import math
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, "state", "cache", "hiret_bars.json")
TRADING_DAYS = 244
COST = 0.0012            # 往返换手成本（与 factorstock 一致）
# 现金腿刻意不建模：候选配置全是满仓，留一块现金会让「年化/回撤」不可比。
# （旧代码留了个 CASH_ANN=0.015 却从没用过，容易被读成「现金收益已计入」。）
STALE_WARN_DAYS = 7      # 缓存末日距今天超过这么多自然日就告警
ASSETS = [
    ("510300", "沪深300"),
    ("159915", "创业板"),
    ("513100", "纳指"),
    ("518880", "黄金"),
    ("510880", "红利"),
    ("511260", "国债"),
]
CODES = [c for c, _ in ASSETS]
NAME = dict(ASSETS)


# ------------------------------------------------------------------ 数据
def load_raw():
    with open(CACHE, "r", encoding="utf-8") as f:
        d = json.load(f)
    out = {}
    for c in d:
        rows = d[c]
        dates = [r[0] for r in rows]
        closes = [float(r[2]) for r in rows]
        out[c] = (dates, closes)
    return out


def align(raw, codes):
    """取所有 codes 都有数据的日期交集，返回 (dates, {code:[close]})。"""
    common = None
    maps = {}
    for c in codes:
        dates, closes = raw[c]
        maps[c] = dict(zip(dates, closes))
        s = set(dates)
        common = s if common is None else (common & s)
    dates = sorted(common)
    series = {c: [maps[c][d] for d in dates] for c in codes}
    return dates, series


# ------------------------------------------------------------------ 指标
def _fmt(v, spec="%.2f"):
    """None（判不了）印成 "—"，不要印成 0.00。"""
    return "—" if v is None else spec % v


def rets(closes):
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


MIN_OBS = 30            # 少于这么多根日线，任何年化指标都是噪声


def stats_from_closes(closes):
    """返回 dict；样本不足返回 None（None 表示「判不了」，不是「表现是 0」）。

    sharpe/calmar 在分母为 0 时是 None —— 见文件头「口径」那段：
    返回 0.0 会把「没有回撤」说成「性价比最差」。
    """
    r = rets(closes)
    n = len(r)
    if n < MIN_OBS:
        return None
    ann = (closes[-1] / closes[0]) ** (TRADING_DAYS / n) - 1
    m = sum(r) / n
    var = sum((x - m) ** 2 for x in r) / n
    vol = math.sqrt(var) * math.sqrt(TRADING_DAYS)
    # 最大回撤
    peak = closes[0]
    mdd = 0.0
    for c in closes:
        if c > peak:
            peak = c
        dd = c / peak - 1
        if dd < mdd:
            mdd = dd
    # Sharpe 用算术日均收益年化，不用几何年化（理由见文件头）
    sharpe = (m * TRADING_DAYS) / vol if vol > 0 else None
    calmar = ann / abs(mdd) if mdd < 0 else None
    return dict(ann=ann, vol=vol, mdd=mdd, sharpe=sharpe, calmar=calmar, n=n)


def metrics_nav(nav):
    return stats_from_closes(nav)


# ------------------------------------------------------------------ 组合
def portfolio_nav(dates, series, weights):
    """年度再平衡（每年首个交易日调回目标权重），换手成本计入。返回 nav 列表。

    权重先归一：不满 1 的权重表会让 nav 一步跳到 0.9 或 1.1，之后所有
    指标都建立在这个假跳空上，而表面上看只是「收益低了点」。
    """
    tot = sum(v for v in weights.values() if v > 0)
    if tot <= 0:
        raise ValueError("权重全为非正数，无法构建组合")
    weights = {c: v / tot for c, v in weights.items() if v > 0}
    codes = sorted(weights)
    hold = {c: weights[c] for c in codes}
    nav = [1.0]
    last_year = dates[0][:4]
    for i in range(1, len(dates)):
        y = dates[i][:4]
        prev = {c: series[c][i - 1] for c in codes}
        cur = {c: series[c][i] for c in codes}
        total = sum(hold[c] * cur[c] / prev[c] for c in codes)
        if y != last_year:
            # 再平衡：把持仓调到 total * w
            turn = 0.0
            for c in codes:
                tv = hold[c] * cur[c] / prev[c]
                tgt = total * weights[c]
                turn += abs(tgt - tv)
            cost = 0.5 * turn * COST
            for c in codes:
                hold[c] = total * weights[c] * (1 - cost)
            last_year = y
        else:
            for c in codes:
                hold[c] = hold[c] * cur[c] / prev[c]
        nav.append(sum(hold[c] for c in codes))
    return nav


def risk_parity(vols):
    inv = {c: 1.0 / v for c, v in vols.items() if v > 0}
    s = sum(inv.values())
    return {c: inv[c] / s for c in inv}


# ------------------------------------------------------------------ 主流程
def main():
    raw = load_raw()
    missing = [c for c in CODES if c not in raw]
    if missing:
        print("缺数据:", missing)
        sys.exit(1)

    # 数据时效：缓存不会自动刷新，停在哪天必须说出来。
    # 2026-11-12 在 riskstudy 上踩过：缓存 16 天没更新，报告一字未提，
    # 换用新数据后 Q3 结论直接翻符号。
    ends = {c: raw[c][0][-1] for c in CODES}
    last = max(ends.values())
    today = datetime.date.today().isoformat()
    try:
        stale = (datetime.date.fromisoformat(today)
                 - datetime.date.fromisoformat(last)).days
    except ValueError:
        stale = None
    print("数据截至 %s（%s），缓存 %s" % (last, today, CACHE))
    if len(set(ends.values())) > 1:
        # 各标的末日不一致 = 有一只抓取失败、沿用了旧缓存（hiret.py 的降级路径）
        print("[注意] 各标的末日不一致，对齐全窗口会被最短的那只截断：")
        for c in CODES:
            print("         %s %s 停 %s" % (c, NAME[c], ends[c]))
    if stale is not None and stale > STALE_WARN_DAYS:
        print("[告警] 数据已停 %d 天（> %d 天）：结论只代表到 %s 为止的行情，"
              "先跑 `python engine/hiret.py fetch` 再引用。"
              % (stale, STALE_WARN_DAYS, last))

    # 资产单独统计（各自全历史）
    print("=" * 96)
    print("各资产单独表现（各自全历史，日频，Sharpe 无风险利率=0）")
    print("=" * 96)
    print("%-8s %-10s %8s %8s %9s %8s %8s   %s" %
          ("代码", "名称", "年化", "波动", "最大回撤", "Sharpe", "Calmar", "区间"))
    asset_stats = {}
    short = []
    for c, nm in ASSETS:
        d, cl = raw[c]
        st = stats_from_closes(cl)
        asset_stats[c] = st
        if st is None:
            short.append("%s %s（%d 根）" % (c, nm, len(cl)))
            print("%-8s %-10s %s   %s~%s" % (c, nm, "样本不足，指标判不了", d[0], d[-1]))
            continue
        print("%-8s %-10s %+7.1f%% %7.1f%% %8.1f%% %8s %8s   %s~%s" % (
            c, nm, st["ann"] * 100, st["vol"] * 100, st["mdd"] * 100,
            _fmt(st["sharpe"]), _fmt(st["calmar"]), d[0], d[-1]))
    if short:
        print("[失败] 以下标的样本不足 %d 根，年化/夏普/回撤都无法计算：%s"
              % (MIN_OBS, "、".join(short)))
        sys.exit(1)

    # 对齐窗口
    dates, series = align(raw, CODES)
    if len(dates) < MIN_OBS:
        # 空交集时 dates=[] 会让下面的 dates[0] 抛 IndexError，看不出是数据问题
        print("[失败] 六种资产没有共同的交易日（交集 %d 天），无法比较组合。"
              "通常是有标的的缓存停在很早的日期，先补数据。" % len(dates))
        sys.exit(1)
    print("\n对齐全窗口：%s ~ %s（%d 个交易日，约 %.1f 年）" %
          (dates[0], dates[-1], len(dates), len(dates) / TRADING_DAYS))

    # 相关矩阵（日收益）
    R = {c: rets(series[c]) for c in CODES}
    n = len(dates) - 1
    mu = {c: sum(R[c]) / n for c in CODES}
    sd = {c: math.sqrt(sum((x - mu[c]) ** 2 for x in R[c]) / n) for c in CODES}

    flat = [c for c in CODES if not sd[c]]
    if flat:
        # 波动为 0 的标的不可能算相关系数（分母 0）。静默跳过会让矩阵里
        # 出现一个 0.00，看起来像「完全不相关」，那是另一回事。
        print("[警告] %s 波动为 0（数据可能是横盘填充），相关矩阵里印 n/a"
              % "、".join("%s %s" % (c, NAME[c]) for c in flat))

    def corr(a, b):
        if not sd[a] or not sd[b]:
            return None
        nu = sum((R[a][i] - mu[a]) * (R[b][i] - mu[b]) for i in range(n))
        return nu / n / (sd[a] * sd[b])

    print("\n" + "=" * 96)
    print("相关矩阵（日收益，窗口内）")
    print("=" * 96)
    print("%-10s" % "" + "".join("%9s" % NAME[c] for c in CODES))
    for a in CODES:
        print("%-10s" % NAME[a]
              + "".join("%9s" % ("n/a" if corr(a, b) is None else "%.2f" % corr(a, b))
                        for b in CODES))

    # 候选配置
    vols = {c: sd[c] * math.sqrt(TRADING_DAYS) for c in CODES}
    rp = risk_parity(vols)
    configs = []
    configs.append(("现状 纳指40/黄金30/创业板30",
                    {"513100": 0.40, "518880": 0.30, "159915": 0.30}))
    configs.append(("等权 6资产", {c: 1.0 / 6 for c in CODES}))
    configs.append(("风险平价 6资产(1/vol)", rp))
    configs.append(("保守 债40/红利20/纳指15/黄金15/沪深300 10",
                    {"511260": 0.40, "510880": 0.20, "513100": 0.15,
                     "518880": 0.15, "510300": 0.10}))
    configs.append(("平衡 纳指25/黄金20/沪深300 20/红利15/债20",
                    {"513100": 0.25, "518880": 0.20, "510300": 0.20,
                     "510880": 0.15, "511260": 0.20}))
    configs.append(("进取 纳指35/创业板20/沪深300 15/黄金20/红利10",
                    {"513100": 0.35, "159915": 0.20, "510300": 0.15,
                     "518880": 0.20, "510880": 0.10}))
    # —— 针对「现状」的增量改良：用国债/红利替换拖累项创业板 ——
    configs.append(("现状改良A 纳指45/黄金30/国债25",
                    {"513100": 0.45, "518880": 0.30, "511260": 0.25}))
    configs.append(("现状改良B 纳指40/黄金30/红利15/国债15",
                    {"513100": 0.40, "518880": 0.30, "510880": 0.15, "511260": 0.15}))
    configs.append(("现状改良C 纳指40/黄金30/创业板15/国债15",
                    {"513100": 0.40, "518880": 0.30, "159915": 0.15, "511260": 0.15}))
    configs.append(("高进攻 纳指55/黄金45",
                    {"513100": 0.55, "518880": 0.45}))

    print("\n" + "=" * 96)
    print("候选组合（年度再平衡、含换手成本；%s ~ %s）" % (dates[0], dates[-1]))
    print("=" * 96)
    print("%-44s %8s %8s %9s %8s %8s" %
          ("组合", "年化", "波动", "最大回撤", "Sharpe", "Calmar"))
    results = []
    for label, w in configs:
        nav = portfolio_nav(dates, series, w)
        st = metrics_nav(nav)
        if st is None:
            results.append(dict(label=label, weights=w, ann=None, vol=None,
                                mdd=None, sharpe=None, calmar=None, n=0))
            print("%-44s 样本不足，判不了（%d 天）" % (label, len(nav)))
            continue
        results.append(dict(label=label, weights=w, **st))
        print("%-44s %+7.1f%% %7.1f%% %8.1f%% %8s %8s" % (
            label, st["ann"] * 100, st["vol"] * 100, st["mdd"] * 100,
            _fmt(st["sharpe"]), _fmt(st["calmar"])))

    # 按 Calmar 排序推荐。calmar=None（无回撤）排最前并单独标注，
    # 它确实优于任何有回撤的组合；但绝不能被当成 0.0 排在最后。
    ranked = sorted(results,
                    key=lambda x: (x["calmar"] is None, -(x["calmar"] or 0.0)))
    print("\n按 Calmar(年化/回撤) 排序（Calmar 为 — 表示没有回撤，判不了比值）：")
    for i, r in enumerate(ranked, 1):
        if r["ann"] is None:
            print("  %d. %-40s 样本不足，不参与排名" % (i, r["label"]))
            continue
        print("  %d. %-40s Calmar=%s  年化%+.1f%% 回撤%.1f%%" %
              (i, r["label"], _fmt(r["calmar"], "%.2f"),
               r["ann"] * 100, r["mdd"] * 100))

    # 输出 JSON 供画图
    dump = dict(
        window=[dates[0], dates[-1]], ndays=len(dates),
        as_of=last, stale_days=stale, ends=ends,
        assets=[dict(code=c, name=NAME[c], **asset_stats[c]) for c, _ in ASSETS],
        portfolios=[dict(label=r["label"], weights=r["weights"],
                         ann=r["ann"], vol=r["vol"], mdd=r["mdd"],
                         sharpe=r["sharpe"], calmar=r["calmar"]) for r in results],
        risk_parity=rp,
    )
    print("\n__JSON__")
    print(json.dumps(dump, ensure_ascii=False))


if __name__ == "__main__":
    main()
