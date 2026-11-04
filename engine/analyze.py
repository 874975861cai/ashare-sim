# -*- coding: utf-8 -*-
"""信号质量诊断：不加任何出场规则，直接看「入场信号出现后」的未来收益分布。

如果未来 N 分钟的收益中位数扣掉成本后仍为负，说明假设本身错了，
再好的止盈止损也只是在负期望上做无用功。

口径（2026-11-04 校准，此前三条都在悄悄给错结论）：
  1 信号定义与 strategy.decide 的入场条件对齐：dev / 3 分钟动量 / 当日跌幅，
    默认还要求「脱离近 15 分钟低点」的反弹确认（use_rebound=True）。
    阈值一律取 strategy.PARAMS，不再在本文件里另写一份 -0.4%——两处定义
    迟早会分叉，而分叉后这里算的就不是策略真正会做的那个信号了。
  2 持有期按**日历分钟**计。分时是一天 241 根（09:30-11:30、13:01-15:00），
    午休前后索引相邻但时间不连续，用 ticks[i+h] 当「h 分钟后」会把 11:00 的
    信号算成持有 2.5 小时。跨时段的样本直接丢弃并计数。
  3 没有前收盘的首个交易日不参与统计：那天 preclose 是当日第一根价格，
    「当日跌 0.4%」实际是「相对开盘跌 0.4%」，与别的日子不是一个口径。
  数据缺失 / 降级缓存 / 零样本都会明说，不再打印一张全 0 的「正常」表。
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest     # noqa: E402
import strategy     # noqa: E402

HORIZONS = [5, 10, 20, 30, 60]
COST = 0.0015       # 往返成本 0.15%（tick 0.05% + 佣金 0.10%）

# 一个交易日至少要 22 根预热 + 最长持有期 + 1 不越界，才可能产出样本。
# 原来是硬编码 70，而 range(22, len-60) 在 len<=82 时是空循环：
# 70~81 根的日子会被当成「可用」却一根样本都算不出来，静默消失。
MIN_TICKS = 22 + max(HORIZONS) + 1

CODES = ["510300", "510500", "512100", "159915", "510050", "588000", "510880",
         "159919", "510330", "159901"]


def _minute_of_day(t):
    """'2026-11-04 09:30' -> 570。只取时分，用于判断持有期是否被午休截断。"""
    return int(t[11:13]) * 60 + int(t[14:16])


def same_session_ahead(ticks, i, h):
    """ticks[i + h] 是否正好在 ticks[i] 之后 h 个**日历分钟**。

    跨午休的样本必须剔除：11:20 的第 60 根是 13:50，实际持有 2.5 小时。
    2026-11-04 实测，60 分钟档 40 个样本里有 8 个是这种（均值 0.924% vs
    纯 60 分钟的 0.178%），足以把「60 分钟持有很赚」这个结论凭空造出来。
    """
    t0, t1 = ticks[i]["t"], ticks[i + h]["t"]
    if t1[:10] != t0[:10]:
        return False
    return _minute_of_day(t1) - _minute_of_day(t0) == h


def _median(r):
    """偶数个样本取中间两个的均值。

    旧写法 sorted(r)[len(r)//2] 在偶数样本时直接拿偏大的那个，
    样本越少偏得越明显。
    """
    s = sorted(r)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def diagnose(dev_th=None, mom_th=None, verbose=True, codes=None, ndays=5,
             use_rebound=True):
    """跑一遍信号生存分析，返回本次统计的口径与结果。

    返回 dict：
      stats / n_sig / per_day / days / n_dataset / skipped / cross /
      failed / degraded / dev_th / mom_th / day_th / rebound_th
    """
    p = strategy.PARAMS
    dev_th = p["dev_enter"] if dev_th is None else dev_th
    mom_th = p["mom_turn"] if mom_th is None else mom_th
    day_th = p["min_day_drop"]
    reb_th = p["rebound"] if use_rebound else 0.0
    # 可入场时段也取自 PARAMS：以前这里硬编码 "09:50"/"14:20"，
    # 策略哪天改了入场窗口，诊断还按旧窗口取样，算的是另一个信号。
    win_lo = p.get("earliest_entry", "09:50")
    win_hi = p.get("latest_entry", "14:20")
    codes = codes or CODES

    report = {}
    data = backtest.load_pool(codes, ndays, verbose=False, offline=True,
                              report=report)
    if not data:
        # 原来是直接跑完循环、打印「信号总数 0，日均 0.0」+ 一张空表，
        # 看着像「今天没有信号」，实际是「今天没有数据」。
        raise RuntimeError("没有任何标的的分时数据（请求 %d 只，全部失败）"
                           % len(codes))
    days = backtest.trading_days(data)

    stats = {h: [] for h in HORIZONS}
    cross = {h: 0 for h in HORIZONS}
    per_day = defaultdict(int)
    n_sig = 0
    n_dataset = 0
    skip_short = 0
    skip_nopre = 0
    days_with_data = set()

    for c, v in data.items():
        nopre = v.get("first_day_no_preclose")
        for day in days:
            ticks = v["days"].get(day)
            if not ticks:
                continue
            if len(ticks) < MIN_TICKS:
                skip_short += 1
                continue
            pre = v["preclose"].get(day)
            if not pre or day == nopre:
                skip_nopre += 1
                continue
            n_dataset += 1
            days_with_data.add(day)

            for i in range(22, len(ticks) - max(HORIZONS)):
                tm = ticks[i]["t"][11:16]
                if not (win_lo <= tm <= win_hi):
                    continue
                dev = strategy.dev_from_vwap(ticks, i)
                mom = strategy.momentum(ticks, i, 3)
                dayret = ticks[i]["price"] / pre - 1
                if not (dev <= -dev_th and mom >= mom_th and dayret <= -day_th):
                    continue
                if reb_th > 0:
                    low15 = min(x["price"] for x in ticks[max(0, i - 15):i + 1])
                    if not low15 or ticks[i]["price"] / low15 - 1 < reb_th:
                        continue
                n_sig += 1
                per_day[day] += 1
                entry = ticks[i]["price"]
                for h in HORIZONS:
                    if not same_session_ahead(ticks, i, h):
                        cross[h] += 1
                        continue
                    stats[h].append(ticks[i + h]["price"] / entry - 1)

    if verbose:
        print("信号阈值 dev<=-%.2f%%  动量3min>=%.2f%%  当日跌>=%.2f%%  %s  "
              "时段 %s-%s"
              % (dev_th * 100, mom_th * 100, day_th * 100,
                 ("反弹>=%.2f%%" % (reb_th * 100)) if reb_th > 0
                 else "!! 未套反弹确认，与策略实际入场条件不同",
                 win_lo, win_hi))
        if report.get("failed"):
            print("!! %d/%d 只标的没加载成功：%s"
                  % (len(report["failed"]), report.get("requested", len(codes)),
                     "、".join("%s(%s)" % (c, w) for c, w in report["failed"])))
        if report.get("degraded"):
            print("!! %d 只用的是降级缓存（截至 %s）：结论要打折扣"
                  % (len(report["degraded"]),
                     "、".join("%s@%s" % (c, t) for c, t in report["degraded"])))
        print("可用横截面 %d 个（标的×交易日），覆盖 %d/%d 个交易日；"
              "跳过：分时不足 %d 个、无前收盘 %d 个"
              % (n_dataset, len(days_with_data), len(days), skip_short, skip_nopre))
        if n_dataset == 0:
            raise RuntimeError("缓存里的分时都不足 %d 根或没有前收盘，"
                               "算不出样本（这不是「没有信号」）" % MIN_TICKS)
        print("信号总数 %d，出现在 %d 个交易日，日均 %.1f 个  分布 %s"
              % (n_sig, len(per_day), n_sig / max(len(days_with_data), 1),
                 dict(sorted(per_day.items()))))
        if n_sig == 0:
            print("（阈值下当天确实一次都没触发，不是数据问题）")
        elif len(per_day) == 1:
            # 单个交易日的样本彼此高度相关：同一天里同一只标的的相邻信号
            # 其实是同一段行情。样本数看着有几十个，独立事件可能只有几个，
            # 此时任何「胜率 90%」都不足以当证据。
            print("!! 信号只落在 1 个交易日（%s）：样本高度相关，"
                  "不能当作独立样本推结论" % list(per_day)[0])
        drop = ["%d分 %d 个" % (h, cross[h]) for h in HORIZONS if cross[h]]
        if drop:
            print("跨午休丢弃样本：%s" % "，".join(drop))
        print()
        print("%-6s%7s%10s%10s%9s%11s%9s%9s"
              % ("持有", "样本", "均值", "中位数", "胜率>0", "胜率>成本",
                 "最好", "最差"))
        for h in HORIZONS:
            r = stats[h]
            if not r:
                # 以前是 continue，整行消失，看的人以为没这一档；
                # 实际是「这一档没有可用样本」。
                print("%-6s%7s%10s%10s%9s%11s%9s%9s"
                      % ("%d分" % h, 0, "—", "—", "—", "—", "—", "—"))
                continue
            mean = sum(r) / len(r)
            print("%-6s%7d%9.3f%%%9.3f%%%8.1f%%%10.1f%%%8.2f%%%8.2f%%"
                  % ("%d分" % h, len(r), mean * 100, _median(r) * 100,
                     sum(1 for x in r if x > 0) / len(r) * 100,
                     sum(1 for x in r if x > COST) / len(r) * 100,
                     max(r) * 100, min(r) * 100))

    return {"stats": stats, "n_sig": n_sig, "per_day": dict(per_day),
            "days": days, "days_with_data": sorted(days_with_data),
            "n_dataset": n_dataset, "cross": cross,
            "failed": report.get("failed", []),
            "degraded": report.get("degraded", []),
            "dev_th": dev_th, "mom_th": mom_th, "day_th": day_th,
            "rebound_th": reb_th}


if __name__ == "__main__":
    rc = 0
    for dev_th in (0.0055, 0.0080, 0.0100):
        try:
            diagnose(dev_th)
        except RuntimeError as e:
            print("诊断失败：%s" % e)
            rc = 2
            break
        print()
    sys.exit(rc)
