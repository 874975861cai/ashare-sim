#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持有型策略的风险体检：长区间、滚动窗口、标的层选择偏差、蒙特卡洛。

## 为什么需要这个文件

策略已经定成「满仓持有 510880，不择时」，参数只有一个 -25% 熔断。
对这个形态，很多回测指标的意义变了：

- 没有入场/出场参数，所以「调参过拟合」不是主要风险；
- **真正的主要风险是「我挑了三年表现最好的那只」** —— 标的层面的选择偏差。
  2023-01~2026-10 恰好是红利风格占优的一段，在这段之后挑标的，
  等于用未来的信息做选择。

所以这里不比参数，只回答四个问题：
  Q1 拉长到 10 年，红利还成立吗？（可能只是风格周期）
  Q2 在任何时点开始持有 3 年，赚钱概率多大？（滚动窗口，不用挑起点）
  Q3 如果三年前随机选一只，红利排第几？（选择偏差）
  Q4 未来最大回撤可能有多深？（蒙特卡洛，用来校准 -25% 熔断线）

用法：python engine/riskstudy.py [--years 10]
"""
import json
import math
import os
import random
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market  # noqa: E402

POOL = ["510300", "510330", "510500", "510050", "510880",
        "159901", "159915", "159919", "512100", "588000"]
NAMES = {"510300": "沪深300", "510330": "沪深300(华夏)", "510500": "中证500",
         "510050": "上证50", "510880": "红利", "159901": "深证100",
         "159915": "创业板", "159919": "沪深300(嘉实)",
         "512100": "中证1000", "588000": "科创50"}

RF = 0.015          # 无风险利率，2026 年一年期定存/国债约 1.5%
TRADING_DAYS = 252
CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "state", "cache")


def load_bars(code, years=10, refresh=False):
    """取日线（**后复权**）。落盘缓存，避免每次都打接口。

    为什么必须用后复权，这是踩出来的坑：
      不复权序列遇到「份额拆分」会跳空。512100 在 2022-09-01→09-05 从
      0.982 跳到 2.713（+176.3%），159901 在 2021-04-09→04-12 从 7.411
      掉到 3.629（-51.0%）——两个都是 1 拆 N，实际没有任何损失。
      不做处理直接算收益率，会把拆分当成暴涨暴跌：
      512100 的年化波动被算成 61.91%（真实约 25-30%），
      159901 的最大回撤被算成 -76.42%（真实约 45%）。
      而且**不报错**，表格照样出得漂漂亮亮。

    为什么用后复权而不是前复权：
      腾讯的前复权是**等差口径**（逐次减分红），高分红标的长区间会被减成
      负数（本函数已在校验里拦）。后复权是乘性的，永远为正，且已处理
      拆分与分红，适合十年跨度。红利这种高股息标的尤其不能用不复权——
      它的收益有很大一块来自分红，不复权会系统性低估 2-4 个百分点/年。
    """
    p = os.path.join(CACHE, "risk_%s_%dy_hfq.json" % (code, years))
    if not refresh and os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("bars"):
                return d["bars"]
        except Exception:  # noqa: BLE001
            pass
    bars = market.fetch_daily_bars_tx(code, years=years, adjust="hfq")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"code": code, "years": years, "adjust": "hfq", "bars": bars},
                  f, ensure_ascii=False)
    return bars


def sanity(bars, code=""):
    """数据体检：ETF 有涨跌停（最宽 20%），单日 |收益| > 25% 必是脏数据。

    没有这道闸门，拆分会伪装成行情，报表看起来很专业但全是错的。
    """
    bad = []
    for i in range(1, len(bars)):
        r = bars[i]["close"] / bars[i - 1]["close"] - 1
        if abs(r) > 0.25:
            bad.append((bars[i - 1]["date"], bars[i]["date"], r))
    if bad:
        print("  [警告] %s 有 %d 处疑似未处理拆分/除权（单日绝对值 >25%%）:" % (code, len(bad)))
        for d0, d1, r in bad[:3]:
            print("      %s -> %s  %+.1f%%" % (d0, d1, r * 100))
        return False
    return True


def rets_of(bars):
    """日收益序列（按收盘价，含分红跳空的失真，这里只做相对比较）。"""
    return [bars[i]["close"] / bars[i - 1]["close"] - 1 for i in range(1, len(bars))]


def ann_ret(bars):
    if len(bars) < 2:
        return None
    days = len(bars) - 1
    total = bars[-1]["close"] / bars[0]["close"]
    if total <= 0:
        return None
    return total ** (TRADING_DAYS / days) - 1


def ann_vol(rets):
    if len(rets) < 2:
        return None
    return statistics.stdev(rets) * math.sqrt(TRADING_DAYS)


def sharpe(bars):
    r, v = ann_ret(bars), ann_vol(rets_of(bars))
    if r is None or not v:
        return None
    return (r - RF) / v


def max_dd(bars):
    peak, mdd, at = None, 0.0, None
    for b in bars:
        v = b["close"]
        peak = v if peak is None else max(peak, v)
        dd = v / peak - 1
        if dd < mdd:
            mdd, at = dd, b["date"]
    return mdd, at


def calmar(bars):
    r = ann_ret(bars)
    mdd, _ = max_dd(bars)
    if r is None or not mdd:
        return None
    return r / abs(mdd)


# ---------------------------------------------------------------- Q1 + 指标表
def q1_metrics(data, years):
    print("=" * 78)
    print("Q1  拉长到 %d 年：红利还成立吗？（同期横截面比较）" % years)
    print("=" * 78)
    print("%-9s %8s %8s %8s %8s %8s %10s"
          % ("标的", "年化收益", "年化波动", "夏普", "最大回撤", "Calmar", "区间"))
    rows = []
    for c in POOL:
        b = data.get(c)
        if not b or len(b) < 250:
            continue
        r, v = ann_ret(b), ann_vol(rets_of(b))
        s = sharpe(b)
        mdd_v, _mdd_at = max_dd(b)
        cm = calmar(b)
        rows.append((c, r, v, s, mdd_v, cm, "%s~%s" % (b[0]["date"][:7], b[-1]["date"][:7])))
        print("%-9s %+7.2f%% %7.2f%% %8.2f %+7.2f%% %8.2f  %s"
              % ("%s %s" % (c, NAMES.get(c, "")), r * 100, v * 100, s, mdd_v * 100, cm,
                 "%s~%s" % (b[0]["date"][:4], b[-1]["date"][:4])))
    rows.sort(key=lambda x: -x[3])
    print("\n  夏普排名：%s" % " > ".join("%s(%.2f)" % (NAMES.get(r[0], r[0]), r[3]) for r in rows))
    return rows


# ---------------------------------------------------------------- Q2 滚动窗口
def q2_rolling(bars, window_years=3):
    """所有滚动 N 年窗口的收益分布。

    不看「我挑的起点」，看「任何起点」。这是最诚实的持有型检验：
    我什么时候开始持有是不可控的（今天建仓就是随机起点）。
    """
    print("\n" + "=" * 78)
    print("Q2  任何时点开始持有 %d 年：赚钱概率有多大？（滚动窗口，全部起点）" % window_years)
    print("=" * 78)
    n = int(TRADING_DAYS * window_years)
    if len(bars) < n + 5:
        print("  数据不够（%d 根 < %d）" % (len(bars), n))
        return
    idx = {b["date"]: i for i, b in enumerate(bars)}
    res = []
    for i in range(0, len(bars) - n):
        r = bars[i + n]["close"] / bars[i]["close"] - 1
        # 窗口内最大回撤
        seg = bars[i:i + n + 1]
        mdd, _ = max_dd(seg)
        res.append((bars[i]["date"], bars[i + n]["date"], r, mdd))
    rs = sorted(x[2] for x in res)
    win = sum(1 for x in res if x[2] > 0) / len(res)
    print("  窗口数 %d   盈利概率 %.1f%%   中位收益 %+.2f%%   最好 %+.2f%%   最差 %+.2f%%"
          % (len(res), win * 100, rs[len(rs) // 2] * 100, rs[-1] * 100, rs[0] * 100))
    mdds = sorted(x[3] for x in res)
    print("  窗口内最大回撤：中位 %.2f%%   最深 %.2f%%"
          % (mdds[len(mdds) // 2] * 100, mdds[0] * 100))
    # 分年度起点
    print("\n  按起始年份看（每个起点持有满 %d 年）:" % window_years)
    byyear = {}
    for d0, d1, r, mdd in res:
        byyear.setdefault(d0[:4], []).append(r)
    for y in sorted(byyear):
        v = byyear[y]
        print("    起 %s 年：%2d 个起点  收益 %+.2f%% ~ %+.2f%%  均值 %+.2f%%"
              % (y, len(v), min(v) * 100, max(v) * 100, sum(v) / len(v) * 100))


# ---------------------------------------------------------------- Q3 选择偏差
def q3_selection_bias(data, window_years=3):
    """三年前如果随机选一只，红利排第几？

    这是本项目最大的隐藏偏差：510880 是在「已经知道哪只涨得多」之后选的。
    若随机选的期望远低于红利，说明策略收益里有很大一块来自事后选标，
    而不是来自「持有红利」这个可复现的规则。
    """
    print("\n" + "=" * 78)
    print("Q3  选择偏差：%d 年前随机选一只，红利排第几？" % window_years)
    print("=" * 78)
    n = int(TRADING_DAYS * window_years)
    scored = []
    for c in POOL:
        b = data.get(c)
        if not b or len(b) < n + 5:
            continue
        # 对齐到同一终点
        seg = b[-(n + 1):]
        r = seg[-1]["close"] / seg[0]["close"] - 1
        scored.append((c, r, seg[0]["date"], seg[-1]["date"]))
    if len(scored) < 3:
        print("  可比标的不足")
        return
    scored.sort(key=lambda x: -x[1])
    print("  最近 %d 年（终点对齐）收益排名：" % window_years)
    for i, (c, r, d0, d1) in enumerate(scored, 1):
        mark = "  ← 我选的" if c == "510880" else ""
        print("   %2d. %-9s %+7.2f%%  (%s~%s)%s"
              % (i, "%s %s" % (c, NAMES.get(c, "")), r * 100, d0[:7], d1[:7], mark))
    rs = [x[1] for x in scored]
    mean = sum(rs) / len(rs)
    med = sorted(rs)[len(rs) // 2]
    hong = next((x[1] for x in scored if x[0] == "510880"), None)
    if hong is not None:
        print("\n  等权随机选一只的期望 %+.2f%%  中位 %+.2f%%" % (mean * 100, med * 100))
        print("  红利 %+.2f%% → 超出随机期望 %+.2f 个百分点"
              % (hong * 100, (hong - mean) * 100))
        better = sum(1 for r in rs if r > hong)
        print("  比红利更好的有 %d 只（共 %d 只）" % (better, len(rs)))
        if better == 0:
            print("  ⚠ 红利是这 %d 年里最好的一只 —— 这既是事实，也是选择偏差的来源。" % len(rs))
            print("     不能把「挑了最好的一只」当成「持有红利这个规则」的收益。")


# ---------------------------------------------------------------- Q4 蒙特卡洛
def q4_montecarlo(bars, block=20, trials=4000, horizons=(1, 3)):
    """块 bootstrap 日收益，估计未来回撤与终值分布。

    为什么用块 bootstrap 而不是逐日独立抽样：日收益有自相关（连续下跌会
    聚集），逐日抽样会把下跌打散，系统性低估回撤 —— 那正是我要估的量。

    block=20（约一个月）保留短期聚集性。这是保守估计，不是精确预测。
    """
    print("\n" + "=" * 78)
    print("Q4  蒙特卡洛：未来回撤能有多深？（块 bootstrap，%d 次，块长 %d 日）"
          % (trials, block))
    print("=" * 78)
    rets = rets_of(bars)
    if len(rets) < block * 2:
        print("  数据不够")
        return
    rng = random.Random(20261028)      # 固定种子，结果可复现
    for h in horizons:
        days = int(TRADING_DAYS * h)
        finals, mdds = [], []
        for _ in range(trials):
            seq = []
            while len(seq) < days:
                st = rng.randrange(0, len(rets) - block + 1)
                seq.extend(rets[st:st + block])
            seq = seq[:days]
            eq, peak, mdd = 1.0, 1.0, 0.0
            for r in seq:
                eq *= (1 + r)
                peak = max(peak, eq)
                mdd = min(mdd, eq / peak - 1)
            finals.append(eq - 1)
            mdds.append(mdd)
        finals.sort()
        mdds.sort()

        def pct(a, p):
            return a[min(len(a) - 1, int(len(a) * p))]

        print("\n  持有 %d 年：" % h)
        print("    期末收益  5%%分位 %+7.2f%%  中位 %+7.2f%%  95%%分位 %+7.2f%%  亏损概率 %.1f%%"
              % (pct(finals, .05) * 100, pct(finals, .5) * 100,
                 pct(finals, .95) * 100,
                 sum(1 for x in finals if x < 0) / len(finals) * 100))
        print("    最大回撤  中位 %6.2f%%   90%%分位 %6.2f%%   99%%分位 %6.2f%%   最深 %6.2f%%"
              % (pct(mdds, .5) * 100, pct(mdds, .9) * 100,
                 pct(mdds, .99) * 100, mdds[0] * 100))
        for th in (0.10, 0.15, 0.20, 0.25, 0.30):
            p = sum(1 for x in mdds if x <= -th) / len(mdds) * 100
            flag = "  ← 熔断线" if th == 0.25 else ""
            print("      P(回撤 ≥ %2.0f%%) = %5.1f%%%s" % (th * 100, p, flag))


def main():
    years = 10
    for i, a in enumerate(sys.argv):
        if a == "--years" and i + 1 < len(sys.argv):
            years = int(sys.argv[i + 1])

    print("加载 %d 年日线（腾讯，后复权）…" % years)
    data = {}
    for c in POOL:
        try:
            b = load_bars(c, years=years)
            data[c] = b
            sanity(b, c)
            print("  %-8s %4d 根  %s ~ %s" % (c, len(b), b[0]["date"], b[-1]["date"]))
        except Exception as e:  # noqa: BLE001
            print("  %-8s 失败 %s" % (c, str(e)[:50]))
        time.sleep(0.15)

    q1_metrics(data, years)
    hong = data.get("510880")
    if hong:
        q2_rolling(hong, 3)
        q3_selection_bias(data, 3)
        q4_montecarlo(hong)
    print("\n" + "=" * 78)


if __name__ == "__main__":
    main()
