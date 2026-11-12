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
from datetime import datetime

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

# 缓存「过期」的判定线（自然日）。春节休市最长约 9 天，取 12 天留余量：
# 短于此不值得一提，长于此说明这份缓存已经跨了半个多月，Q3 的「最近 3 年」
# 其实停在三周前——2026-11-12 实测缓存停在 10-27，而报告里一个字都没提。
STALE_DAYS = 12


def stale_days(bars):
    """最后一根 K 线距今天多少自然日；取不到日期时返回 None。

    为什么单独成一个函数：这个数字必须能进自测。2026-11-12 之前它根本不存在，
    于是 riskstudy 拿 16 天前的缓存算「未来回撤」，报告照样出得漂漂亮亮。
    """
    if not bars:
        return None
    try:
        last = datetime.strptime(str(bars[-1].get("date", ""))[:10], "%Y-%m-%d")
    except (ValueError, TypeError, AttributeError):
        return None
    return (datetime.now() - last).days


def is_stale(bars, limit=STALE_DAYS):
    d = stale_days(bars)
    return d is None or d > limit


def _write_json_atomic(path, obj):
    """tmp + fsync + os.replace，与 broker._atomic_write_text 同一套做法。

    10 分钱的教训（2026-11-10）：直接 open("w") 覆盖，进程被杀就留下
    「大小对、内容全零」的文件。这里虽然只是缓存、坏了会重取，但半截 JSON
    会被下面的 except 当成「没有缓存」→ 静默重打一次接口，仍然不报错。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_bars(code, years=10, refresh=False):
    """取后复权日线。返回 (bars, src)，src ∈ {"cache", "fresh", "cache-fallback"}。

    **为什么要返回第二项**：旧版本只有 bars，于是「读的是三周前的缓存」
    和「刚打的接口」在下游长得一模一样。2026-11-12 实测 10 只全停在 10-27，
    报告一个字没提。现在 main 会把 src 打出来，且过期缓存会单独告警。

    后复权与落盘缓存的来龙去脉：

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
    cached = None
    if not refresh and os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("bars"):
                cached = d["bars"]
        except Exception:  # noqa: BLE001
            cached = None
    if cached is not None and not is_stale(cached):
        return cached, "cache"

    # 缓存缺失或过期才打接口。过期的旧缓存不能直接丢：接口挂了它仍是
    # 唯一能用的东西，但必须让调用方知道「这份是旧的」。
    try:
        bars = market.fetch_daily_bars_tx(code, years=years, adjust="hfq")
    except Exception as e:  # noqa: BLE001
        if cached is not None:
            print("  [警告] %s 刷新失败（%s），沿用 %s 天前的缓存"
                  % (code, str(e)[:40], stale_days(cached)))
            return cached, "cache-fallback"
        raise
    if not bars:
        if cached is not None:
            print("  [警告] %s 接口返回 0 根，沿用旧缓存" % code)
            return cached, "cache-fallback"
        raise RuntimeError("接口返回 0 根日线: %s" % code)
    _write_json_atomic(p, {"code": code, "years": years, "adjust": "hfq", "bars": bars})
    return bars, "fresh"


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


def quantile(a, p):
    """升序序列的 p 分位（下标向下取整）。空序列返回 None。

    抽出来是为了能被自测直接调——2026-11-12 之前它藏在 q4 的循环里，
    而它的用法（对**回撤**取 90/99 分位当「悲观情形」）方向是反的：
    升序排的回撤里，90% 位置的数比中位数**浅**。详见 q4 的说明。
    """
    if not a:
        return None
    p = min(1.0, max(0.0, p))
    return a[min(len(a) - 1, int(len(a) * p))]


def _ref_start(rows):
    """出现次数最多的起始日 = 这池子的公共区间（通常是数据最早的那天）。"""
    cnt = {}
    for r in rows:
        cnt[r[7]] = cnt.get(r[7], 0) + 1
    if not cnt:
        return None
    return max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0]


def rankable(rows, dirty=()):
    """可以进「夏普排名」的行：区间一致，且数据体检没报脏。

    为什么必须卡这两条：排名是这张表唯一的结论性输出，而
      - 科创50 只有 2020 年起的 6 年，把 6 年年化跟 10 年年化排在一起，
        排出来的「名次」没有意义（2026-11-12 实测它 -1.80% 垫底，
        但少掉的那 4 年恰好是 2016-2019 的震荡期）；
      - 有拆分没处理的序列（sanity 报 False）会把年化波动算到 60%+，
        放进去排名等于让脏数据决定谁是第一。
    两者都不报错，只会安静地给出一个错名次。
    """
    if not rows:
        return []
    ref = _ref_start(rows)
    return [r for r in rows if r[7] == ref and r[0] not in dirty]


# ---------------------------------------------------------------- Q1 + 指标表
def q1_metrics(data, years, dirty=()):
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
        rows.append((c, r, v, s, mdd_v, cm,
                     "%s~%s" % (b[0]["date"][:7], b[-1]["date"][:7]),
                     b[0]["date"], b[-1]["date"]))
        print("%-9s %+7.2f%% %7.2f%% %8.2f %+7.2f%% %8.2f  %s"
              % ("%s %s" % (c, NAMES.get(c, "")), r * 100, v * 100, s, mdd_v * 100, cm,
                 "%s~%s" % (b[0]["date"][:4], b[-1]["date"][:4])))
    rows.sort(key=lambda x: -(x[3] if x[3] is not None else -99))
    ok = rankable(rows, dirty)
    print("\n  夏普排名：%s"
          % (" > ".join("%s(%.2f)" % (NAMES.get(r[0], r[0]), r[3])
                        for r in ok if r[3] is not None) or "（无可比标的）"))
    ref = _ref_start(rows)
    skipped = [r for r in rows if r not in ok]
    if ref and skipped:
        print("  未进排名：%s"
              % "、".join("%s（%s，%s）" % (NAMES.get(r[0], r[0]), r[6],
                                          "数据体检报脏" if r[0] in dirty else "区间与其他不同")
                         for r in skipped))
        print("  共同区间是 %s 起，少几年就是少几轮牛熊，年化不能直接比。" % ref[:7])
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
    scored, off = [], []
    for c in POOL:
        b = data.get(c)
        if not b or len(b) < n + 5:
            continue
        # 「对齐到同一终点」——旧注释这么写，代码却没校验：它只是各自取
        # 末尾 n+1 根。某只停牌几天、或缓存比别的旧两周，「最近 3 年」就
        # 变成了错开的窗口，排名还照出，谁也不会发现。
        seg = b[-(n + 1):]
        scored.append((c, seg[-1]["close"] / seg[0]["close"] - 1,
                       seg[0]["date"], seg[-1]["date"]))
    if not scored:
        print("  可比标的不足")
        return
    ref_end = max(x[3] for x in scored)
    keep = []
    for x in scored:
        gap = 0
        try:
            gap = (datetime.strptime(ref_end, "%Y-%m-%d")
                   - datetime.strptime(x[3], "%Y-%m-%d")).days
        except (ValueError, TypeError):
            gap = 999
        (keep if gap <= 7 else off).append(x)
    if off:
        print("  [警告] %d 只的数据没有对齐到 %s，已剔除（终点差 >7 天，窗口错开）:"
              % (len(off), ref_end))
        for c, _r, _d0, d1 in off:
            print("      %s 终点 %s" % (c, d1))
    scored = keep
    if len(scored) < 3:
        print("  对齐后可比标的不足（%d 只）" % len(scored))
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

        print("\n  持有 %d 年：" % h)
        # 收益用「差尾」：5% 分位就是最差的 5%。回撤**不能照抄这个方向**——
        # mdds 升序排列，最深的在最前面，90%/99% 位置上的数比中位数**浅**。
        # 旧代码把 pct(mdds,.9)/.99 写在「中位」右边当悲观档，读者会以为
        # 「90% 的概率回撤不超过 -6.27%」。实测 1 年期：中位 -10.66%，
        # 却把 90% 档标成 -6.27%、99% 档标成 -4.20% —— 数字全对，含义全反。
        print("    期末收益  5%%分位 %+7.2f%%  中位 %+7.2f%%  95%%分位 %+7.2f%%  亏损概率 %.1f%%"
              % (quantile(finals, .05) * 100, quantile(finals, .5) * 100,
                 quantile(finals, .95) * 100,
                 sum(1 for x in finals if x < 0) / len(finals) * 100))
        print("    最大回撤  中位 %6.2f%%   10%%概率更深于 %6.2f%%   1%%概率更深于 %6.2f%%   最深 %6.2f%%"
              % (quantile(mdds, .5) * 100, quantile(mdds, .1) * 100,
                 quantile(mdds, .01) * 100, mdds[0] * 100))
        for th in (0.10, 0.15, 0.20, 0.25, 0.30):
            p = sum(1 for x in mdds if x <= -th) / len(mdds) * 100
            flag = "  ← 熔断线" if th == 0.25 else ""
            print("      P(回撤 ≥ %2.0f%%) = %5.1f%%%s" % (th * 100, p, flag))


def parse_args(argv):
    """返回 (years, refresh)。非法参数直接报错，不靠 traceback 交代。

    旧版本对 `--years abc` 是 `int()` 抛 ValueError，输出一屏栈；
    对 `--years` 后面不带值是静默忽略、按 10 年跑 —— 一个「说错了参数
    却照常出报告」的典型。
    """
    years, refresh = 10, False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--refresh":
            refresh = True
        elif a == "--years" or a.startswith("--years="):
            if "=" in a:
                raw = a.split("=", 1)[1]
            elif i + 1 < len(argv):
                i += 1
                raw = argv[i]
            else:
                raise ValueError("--years 后面要跟年数（如 --years 10）")
            try:
                years = int(raw)
            except ValueError:
                raise ValueError("--years 要整数，收到 %r" % raw)
            if not 1 <= years <= 20:
                raise ValueError("--years 取 1~20（后复权日线就这么多），收到 %d" % years)
        else:
            raise ValueError("不认识的参数 %r（支持 --years N / --refresh）" % a)
        i += 1
    return years, refresh


def main(argv=None):
    argv = sys.argv if argv is None else argv
    try:
        years, refresh = parse_args(argv)
    except ValueError as e:
        print("参数错误：%s" % e)
        return 2

    print("加载 %d 年日线（腾讯，后复权）%s…" % (years, "，强制刷新" if refresh else ""))
    data, dirty, failed, fallback = {}, [], [], []
    for c in POOL:
        try:
            b, src = load_bars(c, years=years, refresh=refresh)
        except Exception as e:  # noqa: BLE001
            # 旧代码只打 str(e)，IndexError/RuntimeError("") 会印成「失败 」——
            # 空原因比没有原因更误导：看起来像是标的本身有问题。
            failed.append(c)
            print("  %-8s 失败 %s: %s" % (c, type(e).__name__, str(e)[:50]))
            continue
        if not b:
            failed.append(c)
            print("  %-8s 失败：拿到 0 根日线（不当作正常标的）" % c)
            continue
        data[c] = b
        if src == "cache-fallback":
            fallback.append(c)
        if not sanity(b, c):
            dirty.append(c)
        print("  %-8s %4d 根  %s ~ %s  [%s]"
              % (c, len(b), b[0]["date"], b[-1]["date"],
                 {"cache": "缓存", "fresh": "新取", "cache-fallback": "旧缓存"}[src]))
        time.sleep(0.15)

    # 数据新鲜度：整体说一句，过期就明说。旧版本只把日期印在每行末尾，
    # 2026-11-12 实测 10 只全停在 10-27（16 天前），报告里没有任何提示。
    fresh = [b for b in data.values() if b]
    if fresh:
        newest = max(b[-1]["date"] for b in fresh)
        print("\n数据截至 %s（%s 天前）" % (newest, stale_days([{"date": newest}])))
        if is_stale([{"date": newest}]):
            print("  ⚠ 已超过 %d 天：Q3 说的「最近 3 年」其实停在这一天。" % STALE_DAYS)
            print("     接口取不到新数据时只能这样，但结论要按这个日期读；"
                  "能联网时加 --refresh 重取。")
    if failed:
        print("  ⚠ %d/%d 只没取到数据：%s（横截面排名与 Q3 的「共 N 只」都不完整）"
              % (len(failed), len(POOL), "、".join(failed)))
    if dirty:
        print("  ⚠ 数据体检报脏、已排除出排名：%s（先查拆分/复权，别急着信年化）"
              % "、".join(dirty))
    if fallback:
        print("  ⚠ 刷新失败、沿用旧缓存：%s" % "、".join(fallback))

    if not data:
        print("\n一只都没取到数据，无法计算。")
        return 1

    q1_metrics(data, years, dirty=dirty)
    hong = data.get("510880")
    if not hong:
        print("\n510880（红利）没有数据，Q2/Q3/Q4 全部跳过 —— 这三个问题正是本文件的目的。")
        return 1
    q2_rolling(hong, 3)
    q3_selection_bias(data, 3)
    q4_montecarlo(hong)
    print("\n" + "=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

