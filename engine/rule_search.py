#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则搜索：在十年日线上找「样本内外都成立」的低换手规则。

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

数据口径（2026-11-12 修，这是本文件最重要的一节）
------------------------------------------------
旧版本读 `state/cache/daykline_3y.json`，那是一份**前复权**序列：没有复权口径
元数据、不会过期、也没有任何一行提示，报告只印「日线 923 根（2023-01-03 ~
2026-10-28）」——读者无从判断这是哪套价格。

实测这份前复权数据把 510880 的同期收益算成 **+46.04%**，而腾讯后复权是
**+27.25%**（510300 差 3.0pp，512100/159915 差 <0.4pp）。高分红标的被系统性
高估 18.8pp，原因是前复权（等差口径，见 market.fetch_daily_bars_tx 的注释）
逐次减分红，减出来的序列在上涨行情里等于自带杠杆。

后果不是「数字略旧」，而是**结论被数据喂出来**：本文件的核心用途是回答
「持有 vs 择时」，被高估的正是持有收益那一侧。用一份把买入持有吹高 19pp
的数据去证明买入持有更好，是自我实现。现在统一走
`riskstudy.load_bars`（腾讯后复权 + 缓存/过期判定 + 原子写 + 拆分体检），
并在开头播报「数据截至 X（N 天前）」。

窗口末未平仓的处理（2026-11-12 修）
----------------------------------
旧 `backtest()` 只对「覆盖到数据末根」的窗口结清（样本外），样本内那半边的
边界仓直接丢掉不记——同一个指标，两段用两把尺子。实测 25 个样本内参数组里
有 14 组受影响。现在两段统一按「窗口内最后一根收盘价结清」。
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import riskstudy  # noqa: E402  后复权加载器（缓存/过期/原子写/体检都在里面）

COST = 0.0018          # 单次往返
IS = ("2023-01-01", "2025-06-30")     # 样本内
OOS = ("2025-07-01", "2026-12-31")    # 样本外
YEARS = 10             # 十年后复权缓存：覆盖 2023-01 起的全部分析窗口，200 日均线也有预热
STALE_DAYS = 12        # 与 riskstudy 同一把尺子（春节最长休市约 9 天）

# 标的池按规则族分开列，方便测试时替换/裁剪
POOL_A = ("510300", "510880", "512100", "159915", "588000")
POOL_BCD = ("510300", "510880", "512100", "159915")
POOL = tuple(sorted(set(POOL_A) | set(POOL_BCD)))


# ---------------------------------------------------------------- 数据
def load(refresh=False):
    """取后复权日线。返回 (data, health)。

    **为什么返回第二项**：旧版本遇到缺标的直接 KeyError（或让空列表流进
    max()/min()），遇到短序列静默给出 0 笔——「没数据」和「规则没触发」
    在报告里长得一模一样。现在缺口、陈旧、体检异常都进 health，由 main 播报。
    """
    data, health = {}, {"missing": [], "stale": [], "unknown": [], "dirty": [],
                       "src": {}, "ends": {}}
    for c in POOL:
        try:
            bars, src = riskstudy.load_bars(c, years=YEARS, refresh=refresh)
        except Exception as e:  # noqa: BLE001
            health["missing"].append((c, str(e)[:80] or e.__class__.__name__))
            continue
        if not bars or len(bars) < 60:
            health["missing"].append((c, "只有 %d 根日线" % len(bars or [])))
            continue
        # sanity 内有 bars[i]["close"] / bars[i-1]["close"] - 1：一根坏 K 线
        # （close=0 / 缺字段）会抛 ZeroDivisionError / KeyError。旧代码在
        # try 外调用它，于是**一只标的的坏缓存把整轮 5 只全带崩**，
        # 报告一行都出不来。这里收进 health 并跳过该标的，其余照常评估。
        try:
            clean = riskstudy.sanity(bars, c)
            d = riskstudy.stale_days(bars)
            end = str(bars[-1].get("date", "") or "")
        except Exception as e:  # noqa: BLE001
            health["missing"].append(
                (c, "K线体检失败 %s: %s" % (type(e).__name__, str(e)[:60]
                                          or e.__class__.__name__)))
            continue
        if not clean:
            health["dirty"].append(c)
        if d is None:
            # 日期解析不了 = 新鲜度**未知**，不是「过期」。旧代码把两者并进
            # stale，下游印成「过期缓存（>12 天）：510300 None 天前」——
            # 一个无法判断的量被当成已知且超过阈值。
            health["unknown"].append(c)
        elif d > STALE_DAYS:
            health["stale"].append((c, d))
        data[c] = bars
        health["src"][c] = src
        health["ends"][c] = end
    return data, health


def ma(bars, i, n):
    if i < 0 or i - n + 1 < 0:
        return None
    return sum(b["close"] for b in bars[i - n + 1:i + 1]) / n


# 「窗口内一根可成交 K 线都没有」的哨兵。
# 不能用 None 或 []：那两者经过 stat()/line() 会被印成「规则未触发」，
# 把「根本没评估」和「评估过、没信号」合成同一句结论（2026-11-13 修）。
NO_DATA = object()


def window_cover(bars, lo, hi):
    """窗口内有多少根**可成交** K 线。

    信号在 T 日收盘确认、T+1 开盘成交，所以成交永远落在 bars[1:] 上；
    数 bars 会把窗口首日那根算进去，而它其实只是信号日。
    """
    return sum(1 for b in bars[1:] if lo <= b["date"] < hi)


def stat(rets):
    if rets is NO_DATA:
        return NO_DATA
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
    """一行结果。三种状态必须分开印，不能合成一句。

    旧注释说「0 笔要写清是没触发，不是没有数据」，可 stat() 对
    「窗口内无数据」和「规则没触发」都返回 None，这句话根本兑现不了：
    缓存停在样本外开始日之前时，整段样本外会印成「0 笔（规则未触发）」，
    读者以为规则被检验过、只是没出信号。
    """
    if s is NO_DATA:
        return "%-30s %s" % (tag, "窗口内无数据（未评估）")
    if not s:
        return "%-30s %s" % (tag, "0 笔（有数据，规则未触发）")
    return ("%-30s %3d笔 胜率%5.1f%% 单笔%+6.2f%% 累计%+7.2f%% 最差%+6.2f%%"
            % (tag, s["n"], s["win"], s["avg"], s["cum"], s["worst"]))


def pct(v):
    """基准收益的统一打印：**取不到数据不能印成 +0.00%**。

    旧代码 `a if a is not None else 0` 让「该标的在窗口内不足 2 根」显示成
    「+0.00%」——一个不存在的持平收益，看着比亏钱还安全。
    """
    return "无数据" if v is None else "%+7.2f%%" % v


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
    """波动率低于目标就持有（低波动异象的粗糙代理）。

    波动率按**去均值的标准差**算（年化）。旧版用 sqrt(mean(r^2))（未减均值的
    RMS），把漂移也算成波动：连续小涨 0.2%/日 的序列会被算成 40% 年化波动
    而被判「不低波动」，可它实际上几乎没有波动。函数名与注释说的都是波动率，
    就按波动率算。
    """
    import math
    import statistics
    sig = [0] * len(bars)
    for i in range(len(bars)):
        if i < n + 1:
            continue
        rets = [bars[k]["close"] / bars[k - 1]["close"] - 1
                for k in range(i - n + 1, i + 1)]
        if len(rets) < 2:
            continue
        sd = statistics.stdev(rets)
        ann = sd * math.sqrt(244)
        sig[i] = 1 if ann < target else 0
    return sig


# ---------------------------------------------------------------- 回测
def backtest(bars, sig, lo, hi):
    """信号 T 日收盘确认 → T+1 开盘成交。返回每笔收益列表。

    窗口末仍未平仓的，按**窗口内最后一根收盘价**结清——样本内、样本外同一口径。
    旧版本只在「窗口覆盖到数据末根」时才结清（也就是只有样本外会），样本内的
    边界仓被静默丢弃，同一个指标两段用两把尺子。

    窗口内一根可成交 K 线都没有时返回 NO_DATA，不是 []。
    """
    if window_cover(bars, lo, hi) == 0:
        return NO_DATA
    rets = []
    entry = None
    last = None
    for i in range(len(bars) - 1):
        want = sig[i]
        nxt = bars[i + 1]
        if not (lo <= nxt["date"] < hi):
            continue
        last = nxt
        if want == 1 and entry is None:
            entry = nxt["open"]
        elif want == 0 and entry is not None:
            rets.append(nxt["open"] / entry - 1 - COST)
            entry = None
    if entry is not None and last is not None:
        rets.append(last["close"] / entry - 1 - COST)
    return rets


def bh(bars, lo, hi):
    """区间买入持有：首日开盘买、末日收盘卖。窗口内不足 2 根返回 None。"""
    seg = [x for x in bars if lo <= x["date"] < hi]
    if len(seg) < 2:
        return None
    return (seg[-1]["close"] / seg[0]["open"] - 1) * 100


# ---------------------------------------------------------------- 入口
def parse_args(argv):
    refresh = False
    for a in argv:
        if a == "--refresh":
            refresh = True
        else:
            raise ValueError("不认识的参数 %r（支持 --refresh）" % a)
    return refresh


def report_health(data, health):
    """数据体检播报。返回 True = 数据可用。"""
    # 允许调用方只带部分桶（自测/复用时常见）。缺桶 = 该类问题没有，
    # 不是「数据正常」的兜底：真正的数据在 data 里，这里只补齐播报用的键。
    for k, v in (("missing", []), ("stale", []), ("unknown", []),
                 ("dirty", []), ("src", {}), ("ends", {})):
        health.setdefault(k, v)
    if not data:
        print("[错误] 一只标的都没取到，无法评估：")
        for c, why in health["missing"]:
            print("    %s  %s" % (c, why))
        print("      能联网时加 --refresh 重取；离线且无缓存时本文件无法工作。")
        return False

    # 末根日期读不出来的标的已记进 unknown，别让空串混进终点列表：
    # 否则 sorted 会把它排到最前面，印出「最旧 」这种空值。
    ends = sorted(d for d in health["ends"].values() if d)
    if not ends:
        print("[错误] 取到了数据但没有一根能读出终点日期，无法判断新鲜度。")
        return False
    # 「数据截至」必须是**最新**终点。旧代码取 ends[0]（最旧），却在同一段
    # 把 ends[-1] 称作「最新」——一只标的落后，整份报告就被标成 N 天前，
    # 读者会以为全池数据都旧。新鲜度按最新算，落后单独告警。
    asof = ends[-1]
    oldest = ends[0]
    age = riskstudy.stale_days([{"date": asof}])
    srcs = {}
    for s in health["src"].values():
        srcs[s] = srcs.get(s, 0) + 1
    print("数据：后复权（腾讯）｜%d/%d 只｜数据截至 %s（%s）｜最旧 %s｜来源 %s"
          % (len(data), len(POOL), asof,
             "未知" if age is None else "%d 天前" % age, oldest,
             " / ".join("%s %d" % (k, v) for k, v in sorted(srcs.items()))))

    # 终点不一致：某只停牌/缓存更旧会让「最近 N 年」变成错开的窗口
    gaps = [(c, d) for c, d in health["ends"].items() if d and d != asof]
    if gaps:
        print("  [警告] 数据终点不一致（最新 %s，最旧 %s，相差窗口错开）：%s"
              % (asof, oldest,
                 "、".join("%s 止于 %s" % (c, d) for c, d in sorted(gaps))))
    # stale 里只该有整数天数。若混进 None（旧版把「未知」塞进来的写法），
    # 绝不能印成「过期缓存：510300 None 天前」——按「未知」播报。
    stale, unknown = [], list(health.get("unknown", []))
    for c, d in health.get("stale", []):
        if isinstance(d, int):
            stale.append((c, d))
        else:
            unknown.append(c)
    if stale:
        print("  [警告] 过期缓存（>%d 天）：%s —— 结论不代表当前市场，能联网时加 --refresh"
              % (STALE_DAYS, "、".join("%s %s 天前" % (c, d) for c, d in stale)))
    if unknown:
        print("  [警告] %d 只新鲜度未知（末根日期无法解析，不算过期也不算新鲜）：%s"
              % (len(unknown), "、".join(unknown)))
    if health["missing"]:
        print("  [警告] 缺 %d 只，相关规则族会跳过：%s"
              % (len(health["missing"]),
                 "、".join("%s(%s)" % (c, w) for c, w in health["missing"])))
    if health["dirty"]:
        print("  [警告] 体检异常（疑似未处理拆分/除权，单日 >25%%）：%s"
              % "、".join(health["dirty"]))
    print("窗口：样本内 %s ~ %s｜样本外 %s ~ %s（各标的实际止于自身终点，最新 %s）"
          % (IS[0], IS[1], OOS[0], OOS[1], asof))
    print("口径：T 日收盘确认 → T+1 开盘成交；窗口末未平仓按窗口内最后一根收盘结清"
          "（两段同一口径）；单次往返成本 %.2f%%" % (COST * 100))
    print()
    return True


def main(argv=None):
    try:
        refresh = parse_args(sys.argv[1:] if argv is None else argv)
    except ValueError as e:
        print(str(e))
        return 2

    print("加载后复权日线（%d 年，%s）…" % (YEARS, "强制刷新" if refresh else "优先用缓存"))
    data, health = load(refresh)
    if not report_health(data, health):
        return 1

    print("=" * 96)
    print("基准：买入持有")
    print("=" * 96)
    for c in POOL:
        if c not in data:
            continue
        print("%-8s 样本内 %s   样本外 %s"
              % (c, pct(bh(data[c], *IS)), pct(bh(data[c], *OOS))))

    print()
    print("=" * 96)
    print("规则族 A：双均线趋势（快慢线上穿持有，跌破空仓）")
    print("=" * 96)
    for code in POOL_A:
        if code not in data:
            print("%s 缺数据，跳过\n" % code)
            continue
        bars = data[code]
        for fast, slow in ((10, 60), (20, 60), (20, 120), (5, 40), (10, 100)):
            sig = r_trend(bars, fast, slow)
            print(line("%s MA%d/MA%d 内" % (code, fast, slow),
                       stat(backtest(bars, sig, *IS))))
            print(line("%s MA%d/MA%d 外" % (code, fast, slow),
                       stat(backtest(bars, sig, *OOS))))
            print()

    print("=" * 96)
    print("规则族 B：价格 vs 单均线（时序动量）")
    print("=" * 96)
    for code in POOL_BCD:
        if code not in data:
            print("%s 缺数据，跳过\n" % code)
            continue
        bars = data[code]
        for n in (60, 100, 120, 200):
            sig = r_price_ma(bars, n)
            print(line("%s 价格>MA%d 内" % (code, n),
                       stat(backtest(bars, sig, *IS))))
            print(line("%s 价格>MA%d 外" % (code, n),
                       stat(backtest(bars, sig, *OOS))))
            print()

    print("=" * 96)
    print("规则族 C：绝对动量（过去 N 日为正则持有）")
    print("=" * 96)
    for code in POOL_BCD:
        if code not in data:
            print("%s 缺数据，跳过\n" % code)
            continue
        bars = data[code]
        for look in (20, 60, 120, 200):
            sig = r_absmom(bars, look)
            print(line("%s 动量%d 内" % (code, look),
                       stat(backtest(bars, sig, *IS))))
            print(line("%s 动量%d 外" % (code, look),
                       stat(backtest(bars, sig, *OOS))))
            print()

    print("=" * 96)
    print("规则族 D：唐奇安通道突破")
    print("=" * 96)
    for code in POOL_BCD:
        if code not in data:
            print("%s 缺数据，跳过\n" % code)
            continue
        bars = data[code]
        for n in (20, 55, 120):
            sig = r_donchian(bars, n)
            print(line("%s 通道%d 内" % (code, n),
                       stat(backtest(bars, sig, *IS))))
            print(line("%s 通道%d 外" % (code, n),
                       stat(backtest(bars, sig, *OOS))))
            print()

    print("=" * 96)
    print("规则族 E：低波动持有（年化波动 < 目标则持仓）")
    print("=" * 96)
    for code in POOL_BCD:
        if code not in data:
            print("%s 缺数据，跳过\n" % code)
            continue
        bars = data[code]
        for n, t in ((20, 0.15), (20, 0.20), (60, 0.18)):
            sig = r_voltarget(bars, n, t)
            print(line("%s 波动%d/%0.2f 内" % (code, n, t),
                       stat(backtest(bars, sig, *IS))))
            print(line("%s 波动%d/%0.2f 外" % (code, n, t),
                       stat(backtest(bars, sig, *OOS))))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
