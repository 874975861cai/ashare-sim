#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高收益/高风险结构研究：把「持有红利」换成更高 beta 的资产，该选什么。

为什么要重做，而不是把 hold_target 改个代码就完事
--------------------------------------------------
原来的结论（engine/rule_search*.py，3 年日线、10 只 ETF）是：
「在这个池子 + 1 万本金的成本结构下，择时没有 edge，只有持有不动稳健」。
那个结论的前提有两处现在变了：

  1 本金 1 万 -> 10 万。佣金 5 元下限从「往返 0.10%」降到「往返 0.025%」，
    分 2~3 只不再是惩罚，可选结构从「单标的」变成「多标的组合」。
  2 用户明确要「高收益高风险」。红利 ETF 的十年年化 4.88% / 回撤 -20%
    是这套池子的上限，不是策略没调好——想要更高收益，必须换资产，
    而不是继续在低波动资产上找买卖点。

所以本脚本只回答一个问题：**在高 beta 资产上，「一直持有」和
「月频动量轮动」哪个更划算**。不回答「能不能靠日内交易赚更多」——
那条路已经在 130 个组合上被证伪，不再重测。

数据诚实性
----------
行业/主题 ETF 大多 2019 年后才上市，横截面轮动需要多只同时存在，
所以轮动部分只能从 2019-01 起算（约 7.8 年、一次完整牛熊）。
这个样本比红利十年结论弱，本脚本的输出必须如实标注，不能当成
「和十年红利同样可信」的结论使用。

用法
----
  python engine/hiret.py fetch     # 拉数据到 state/cache/hiret_bars.json
  python engine/hiret.py run       # 跑回测并打印对比表
"""

import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "engine"))

CACHE = os.path.join(BASE, "state", "cache", "hiret_bars.json")

# 候选池：按「想要多少 beta」分三档。
# 代码/名称只用于报告可读性，回测只认代码。
POOL = {
    # --- 宽基（有 10 年以上历史，beta 1.0~1.4）---
    "510300": "沪深300ETF",
    "510500": "中证500ETF",
    "512100": "中证1000ETF",
    "159915": "创业板ETF",
    "510050": "上证50ETF",
    "510880": "红利ETF",
    # --- 高 beta 宽基/行业（beta 1.3~2.0，多数 2019 年后）---
    "588000": "科创50ETF",
    "512880": "证券ETF",
    "512760": "半导体ETF",
    "512690": "酒ETF",
    "512170": "医疗ETF",
    "512660": "军工ETF",
    "515030": "新能源车ETF",
    # --- 跨境（与 A 股相关性低，是真正的分散来源）---
    "513050": "中概互联ETF",
    "513180": "恒生科技ETF",
    "513100": "纳指ETF",
    # --- 防守（负相关/低相关，用于「轮出去」而不是空仓）---
    "518880": "黄金ETF",
    "511260": "十年国债ETF",
}

# 横截面轮动的可用池：只放「想赚 beta」的，防守品单独当避风港。
RISK_POOL = ["159915", "512100", "512880", "512760", "512690",
             "512170", "512660", "515030", "513050", "513100", "510500"]
DEFENSIVE = "518880"          # 轮出时持黄金，而不是空仓吃 0

COST = 0.0012                  # 10 万本金往返：佣金 0.05% + 滑点 0.07%
TRADING_DAYS = 244


# ---------------------------------------------------------------- 取数
def fetch_all(years=11):
    import market
    out = {}
    old = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, "r", encoding="utf-8") as f:
                old = json.load(f)
        except Exception:  # noqa: BLE001
            old = {}
    for code, name in POOL.items():
        try:
            rows = market.fetch_daily_bars_tx(code, years=years, adjust="hfq")
        except Exception as e:  # noqa: BLE001
            print("  [失败] %s %s: %s" % (code, name, e))
            if code in old:
                out[code] = old[code]
                print("         沿用旧缓存 %d 根" % len(old[code]))
            continue
        out[code] = [[r["date"], r["open"], r["close"], r["high"], r["low"]]
                     for r in rows]
        print("  [OK] %s %-14s %4d 根  %s ~ %s"
              % (code, name, len(out[code]), out[code][0][0], out[code][-1][0]))
        time.sleep(0.3)
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print("已写入 %s（%d 只）" % (CACHE, len(out)))


def load():
    with open(CACHE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    data = {}
    for c, rows in raw.items():
        data[c] = [{"date": r[0], "open": float(r[1]), "close": float(r[2]),
                    "high": float(r[3]), "low": float(r[4])} for r in rows]
    return data


# ---------------------------------------------------------------- 指标
def metrics(nav, bench=None):
    """nav: 日净值序列（已含成本）。返回年化/回撤/夏普/Calmar。"""
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
            "sortino": sortino,
            "calmar": ann / abs(mdd) if mdd else 0.0,
            "vol": sd * (TRADING_DAYS ** 0.5)}


def fmt(tag, m, extra=""):
    if not m:
        return "%-30s 数据不足" % tag
    return ("%-30s 年化%+7.2f%% 回撤%7.2f%% 夏普%5.2f 卡玛%5.2f 波动%6.2f%%%s"
            % (tag, m["ann"] * 100, m["mdd"] * 100, m["sharpe"],
               m["calmar"], m["vol"] * 100, extra))


def year_table(nav, dates):
    """按自然年拆收益，看每年到底靠什么赚/亏。"""
    out = {}
    for i in range(1, len(nav)):
        y = dates[i][:4]
        out[y] = out.get(y, 1.0) * (nav[i] / nav[i - 1])
    return {y: (v - 1) * 100 for y, v in out.items()}


# ---------------------------------------------------------------- 策略
class Series(object):
    """单只标的的日期->价格查询。

    为什么不按「交集」对齐：横截面池里最晚上市的是新能源车(2020-03)，
    取交集会把 2016-2019 整整四年样本砍掉，等于用最短的标的绑架全池。
    真实世界是「新 ETF 上市后进入可选池」，所以用并集 + 缺失跳过：
    上市前的日子这只标的不参与打分，上市后自动加入。
    """

    def __init__(self, bars):
        self.dates = [b["date"] for b in bars]
        self.close = [b["close"] for b in bars]
        self.open = [b["open"] for b in bars]

    def k(self, d, strict=False):
        """<=d 的最后一根下标；strict=True 要求当天有数据（打分用）。"""
        import bisect
        i = bisect.bisect_right(self.dates, d) - 1
        if i < 0:
            return -1
        if strict and self.dates[i] != d:
            return -1
        return i

    def close_at(self, d):
        i = self.k(d)
        return self.close[i] if i >= 0 else None


def align(data, codes):
    """并集日期 + 各代码 Series。"""
    dates = sorted({b["date"] for c in codes for b in data[c]})
    px = {c: Series(data[c]) for c in codes}
    return dates, px


def bt_hold(data, code, lo="", hi="9999"):
    """买入持有：区间第一天开盘买，最后一天收盘记净值。"""
    bars = [b for b in data[code] if lo <= b["date"] <= hi]
    if len(bars) < 3:
        return None, None
    entry = bars[0]["open"] * (1 + COST / 2)
    nav = [1.0]
    for b in bars[1:]:
        nav.append(b["close"] / entry - COST / 2)
    return nav, [b["date"] for b in bars]


def bt_xsec(data, look, rebal, topn=1, abs_band=None, defensive=DEFENSIVE,
            lo="", hi="9999", cost=COST):
    """月频横截面动量轮动。

    look   : 动量回看交易日
    rebal  : 调仓间隔交易日
    topn   : 持有几只（10 万本金可以持 2 只）
    abs_band: 绝对动量门槛，最强标的的 look 日收益低于它就换到 defensive。
              传 None 表示不做绝对动量过滤（永远满仓风险资产）。
    """
    codes = [c for c in RISK_POOL if c in data]
    if defensive:
        codes_all = codes + [defensive]
    else:
        codes_all = codes
    dates, px = align(data, codes_all)
    ks = [i for i, d in enumerate(dates) if lo <= d <= hi]
    if len(ks) < look + 5:
        return None, None
    i0, i1 = ks[0], ks[-1]

    nav = [1.0]
    held = {}          # code -> 权重
    last_rebal = -10 ** 9
    out_dates = [dates[i0]]

    for i in range(i0, i1):
        # 调仓判定：用 i 日收盘信号，i+1 开盘成交（无前视）
        if i - last_rebal >= rebal:
            scores = []
            for c in codes:
                s = px[c]
                k1, k0 = s.k(dates[i], strict=True), s.k(dates[i - look])
                if k1 < 0 or k0 < 0 or s.close[k0] <= 0:
                    continue
                scores.append((s.close[k1] / s.close[k0] - 1, c))
            scores.sort(reverse=True)
            best = scores[:topn]
            if abs_band is not None and best and best[0][0] < abs_band:
                target = {defensive: 1.0} if defensive else {}
            elif best:
                w = 1.0 / len(best)
                target = {c: w for _, c in best}
            else:
                target = {defensive: 1.0} if defensive else {}
            last_rebal = i
        else:
            target = None

        # 当日收益（用 i -> i+1 的收盘，避免用开盘价虚构成交）
        r = 0.0
        if held:
            num, den = 0.0, 0.0
            for c, w in held.items():
                a, b = px[c].close_at(dates[i]), px[c].close_at(dates[i + 1])
                if a and b:
                    num += w * (b / a - 1)
                    den += w
            r = num / den if den else 0.0
        nav.append(nav[-1] * (1 + r))
        out_dates.append(dates[i + 1])

        # 调仓成交发生在 i+1 开盘 -> 用 i+1 当日收益的一半近似开盘->收盘，
        # 更严谨的做法是按开盘价重算，这里用换手率扣成本即可（成本已远大于差异）
        if target is not None:
            turn = 0.0
            for c in set(list(held) + list(target)):
                turn += abs(target.get(c, 0.0) - held.get(c, 0.0))
            nav[-1] *= (1 - turn * cost / 2)
            held = target

    return nav, out_dates


def bt_tsmom(data, code, look, defensive=DEFENSIVE, lo="", hi="9999"):
    """单标的时序动量：close > look 日前 close 就持有，否则换 defensive。

    defensive=None 表示空仓持现金（0 收益）——这是判断「择时本身有没有
    edge」的唯一干净对照。若换成黄金后收益大涨、换成现金后毫无优势，
    那所谓 edge 就是黄金自己在涨，不是信号在工作。
    """
    codes = [code] + ([defensive] if defensive and defensive != code else [])
    dates, px = align(data, codes)
    ks = [i for i, d in enumerate(dates) if lo <= d <= hi]
    if len(ks) < look + 5:
        return None, None
    i0, i1 = ks[0], ks[-1]
    nav = [1.0]
    out_dates = [dates[i0]]
    in_risk = None
    for i in range(i0, i1):
        s = px[code]
        k1, k0 = s.k(dates[i], strict=True), s.k(dates[i - look])
        if k1 < 0 or k0 < 0 or s.close[k0] <= 0:
            nav.append(nav[-1])
            out_dates.append(dates[i + 1])
            continue
        sig = s.close[k1] / s.close[k0] - 1
        want = sig > 0
        if in_risk is None or want != in_risk:
            # 一次切换 = 卖出旧仓 + 买入新仓，两边都吃一次单边成本，
            # 合计正好等于「往返 COST」。扣半次是漏算（写错过）。
            if in_risk is not None:
                nav[-1] *= (1 - COST)
            in_risk = want
        hold_c = code if want else defensive
        if hold_c is None:
            r = 0.0
        else:
            a, b = px[hold_c].close_at(dates[i]), px[hold_c].close_at(dates[i + 1])
            r = (b / a - 1) if (a and b) else 0.0
        nav.append(nav[-1] * (1 + r))
        out_dates.append(dates[i + 1])
    return nav, out_dates


def bt_mix(data, weights, lo="", hi="9999", rebal_days=244, cost=COST):
    """固定配比 + 定期再平衡。不预测、不择时，只靠相关性赚钱。

    这是 10 万本金真正打开的那扇门：1 万只能单标的，10 万可以拿 4 只
    各 2.5 万，佣金 5 元 = 0.02%，分散几乎没有额外成本。
    """
    codes = [c for c in weights if c in data]
    if len(codes) < 2:
        return None, None
    tot = sum(weights[c] for c in codes)
    w0 = {c: weights[c] / tot for c in codes}
    dates, px = align(data, codes)
    ks = [i for i, d in enumerate(dates) if lo <= d <= hi]
    if len(ks) < 260:
        return None, None
    i0, i1 = ks[0], ks[-1]

    nav = [1.0]
    out = [dates[i0]]
    w = dict(w0)
    last = 0
    for i in range(i0, i1):
        num, den = 0.0, 0.0
        for c, wt in w.items():
            a, b = px[c].close_at(dates[i]), px[c].close_at(dates[i + 1])
            if a and b:
                num += wt * (b / a - 1)
                den += wt
        r = num / den if den else 0.0
        nav.append(nav[-1] * (1 + r))
        out.append(dates[i + 1])
        # 权重随价格自然漂移
        drift = {}
        for c, wt in w.items():
            a, b = px[c].close_at(dates[i]), px[c].close_at(dates[i + 1])
            drift[c] = wt * ((b / a) if (a and b) else 1.0)
        s = sum(drift.values())
        w = {c: v / s for c, v in drift.items()} if s else w
        # 定期拉回目标权重
        if i - last >= rebal_days:
            turn = sum(abs(w0.get(c, 0) - w.get(c, 0)) for c in w)
            nav[-1] *= (1 - turn * cost / 2)
            w = dict(w0)
            last = i
    return nav, out


# ---------------------------------------------------------------- 报告
def report(data):
    """跑完所有对照，打印三层结论：单标的 / 时序 / 横截面。"""
    print("=" * 96)
    print("第一层：单标的买入持有（区间取各自数据起点，看 beta 到底换来什么）")
    print("=" * 96)
    rows = []
    for c, name in POOL.items():
        if c not in data:
            continue
        nav, ds = bt_hold(data, c)
        if not nav:
            continue
        m = metrics(nav)
        m.update({"code": c, "name": name, "from": ds[0], "to": ds[-1],
                  "years": len(ds) / TRADING_DAYS, "nav": nav, "dates": ds})
        rows.append(m)
        print(fmt("%s %s (%s起)" % (c, name, ds[0][:7]), m,
                  "  %.1f年" % m["years"]))

    print()
    print("=" * 96)
    print("第二层：近 8 年统一窗口（2019-01 起，能并排比）")
    print("=" * 96)
    LO, HI = "2019-01-01", "2026-12-31"
    base = {}
    for c, name in POOL.items():
        if c not in data:
            continue
        nav, ds = bt_hold(data, c, LO, HI)
        if not nav:
            continue
        m = metrics(nav)
        base[c] = m
        print(fmt("%s %-12s" % (c, name), m))
        if c in ("510300", "510880"):
            print("     分年:", " ".join(
                "%s %+.1f%%" % (y, v) for y, v in year_table(nav, ds).items()))

    # 基准：沪深300 同窗口
    nav_bench, ds_bench = bt_hold(data, "510300", LO, HI)
    nav_bench_s = nav_bench

    print()
    print("=" * 96)
    print("第三层：月频动量轮动（横截面 topn + 绝对动量过滤，防守=黄金）")
    print("=" * 96)
    print("%-46s %s" % ("结构", "指标"))
    best = None
    for look in (60, 120, 250):
        for rebal in (10, 20):
            for topn in (1, 2):
                for band in (None, 0.0):
                    tag = ("xsec look%d rebal%d top%d %s"
                           % (look, rebal, topn,
                              "无过滤" if band is None else "abs>0"))
                    nav, ds = bt_xsec(data, look, rebal, topn, band, DEFENSIVE, LO, HI)
                    if not nav:
                        continue
                    m = metrics(nav)
                    print(fmt(tag, m))
                    # 与同窗口最强单标的持有比：年化和回撤都要占优才算改进
                    if (m["ann"] > base.get("159915", {}).get("ann", 9)
                            and m["mdd"] > base.get("159915", {}).get("mdd", 0)):
                        if not best or m["calmar"] > best[1]["calmar"]:
                            best = (tag, m, nav, ds)

    print()
    print("=" * 96)
    print("第四层：单标的时序动量（对照，看「择时」在高 beta 上是否仍然无效）")
    print("=" * 96)
    for c in ("159915", "512880", "512100", "510500"):
        if c not in data:
            continue
        for look in (120, 250):
            nav, ds = bt_tsmom(data, c, look, DEFENSIVE, LO, HI)
            if not nav:
                continue
            print(fmt("%s 时序 look%d (vs黄金)" % (c, look), metrics(nav)))
        print(fmt("%s 纯持有" % c, base.get(c), "  <- 对照"))

    print()
    print("=" * 96)
    print("第五层：样本内 / 样本外切分（2019-01~2023-06 内，2023-07~2026-11 外）")
    print("=" * 96)
    IS = ("2019-01-01", "2023-06-30")
    OOS = ("2023-07-01", "2026-12-31")
    for look, rebal, topn, band in ((120, 20, 2, 0.0), (250, 20, 2, 0.0),
                                    (120, 20, 1, None), (60, 10, 1, None)):
        line = "xsec look%d rebal%d top%d %s" % (look, rebal, topn,
                                                 "abs" if band is not None else "none")
        for seg, (a, b) in (("内", IS), ("外", OOS)):
            nav, _ = bt_xsec(data, look, rebal, topn, band, DEFENSIVE, a, b)
            m = metrics(nav) if nav else None
            print(fmt(line + " [" + seg + "]", m))
    for c in ("159915", "510300", "510880", "512880"):
        if c not in data:
            continue
        for seg, (a, b) in (("内", IS), ("外", OOS)):
            nav, _ = bt_hold(data, c, a, b)
            print(fmt("%s 持有 [%s]" % (c, seg), metrics(nav) if nav else None))
    print()
    print("=" * 96)
    print("第六层：决定性对照——时序动量的「防守资产」换成现金还剩什么")
    print("=" * 96)
    print("如果 vs黄金 很美、vs现金 全无优势，那 edge 属于黄金，不属于信号。")
    print()
    for c in ("159915", "512100", "510500"):
        if c not in data:
            continue
        h, hd = bt_hold(data, c, LO, HI)
        print(fmt("%s 纯持有" % c, metrics(h)))
        for d, dn in ((None, "现金0%"), ("518880", "黄金"), ("511260", "国债")):
            if d and d not in data:
                continue
            for look in (120, 250):
                nav, _ = bt_tsmom(data, c, look, d, LO, HI)
                if nav:
                    print(fmt("  时序 look%d -> %s" % (look, dn), metrics(nav)))
        print()

    print()
    print("=" * 96)
    print("第七层：固定配比 + 年度再平衡（不预测、不择时的唯一方案）")
    print("=" * 96)
    mixes = [
        ("保守 红4国3金2纳1", {"510880": 0.4, "511260": 0.3, "518880": 0.2, "513100": 0.1}),
        ("均衡 纳3金3中5 2国2", {"513100": 0.3, "518880": 0.3, "510500": 0.2, "511260": 0.2}),
        ("进攻 纳4金3中5 3", {"513100": 0.4, "518880": 0.3, "510500": 0.3}),
        ("进攻+ 纳4金3创3", {"513100": 0.4, "518880": 0.3, "159915": 0.3}),
        ("纯A股 创4中5 4红2", {"159915": 0.4, "510500": 0.4, "510880": 0.2}),
        ("纯A股高beta 创5中5 5", {"159915": 0.5, "510500": 0.5}),
        ("纳指单吊", {"513100": 1.0}),
        ("黄金单吊", {"518880": 1.0}),
    ]
    for tag, w in mixes:
        nav, ds = bt_mix(data, w, LO, HI)
        m = metrics(nav) if nav else None
        print(fmt(tag, m))
        if m and m["ann"] > 0.1:
            print("     分年:", " ".join("%s %+.1f%%" % (y, v)
                                         for y, v in year_table(nav, ds).items()))

    print()
    print("第八层：组合的样本内外一致性（同配比切两段）")
    for tag, w in mixes[:6]:
        for seg, (a, b) in (("内", IS), ("外", OOS)):
            nav, _ = bt_mix(data, w, a, b)
            print(fmt("%s [%s]" % (tag, seg), metrics(nav) if nav else None))
        print()

    return base


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "run":
        report(load())
    elif sys.argv[1] == "fetch":
        fetch_all()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
