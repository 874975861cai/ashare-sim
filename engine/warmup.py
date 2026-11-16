# -*- coding: utf-8 -*-
"""数据预热：把核心池的 5 日分时一次性拉到本地缓存。

东方财富接口有分钟级限流，因此这里用「慢速串行 + 失败长退避」，
跑一次后回测与优化全部离线可用。

为什么要分四种结局（2026-11-02 自审）：
    warm 的唯一目的是**把缓存刷新到今天**。但 market.fetch_trends 在接口挂掉时
    不会抛错，而是**降级**返回更早写下的缓存（带 stale 标记）——原实现把这种
    返回值当成成功：实测 2026-11-02 跑 warm()，13 只标的里 11 只其实是 10-20 的
    旧文件，脚本仍打印「完成：成功 15 / 失败 0」。唯一的观测点就这样把「数据陈旧」
    抹掉了，而 backtest / weekly / exitstudy 全都吃这份缓存。
    所以结局必须是四种，不能合成「成功/失败」两桶：
      fresh   —— 真的联网拉到了，点数与日数都对；
      stale   —— 降级，用的是旧缓存（没刷到），记下 as_of；
      partial —— 拉到了，但日数不够 ndays（停牌、新股、接口截断）；
      fail    —— 抛错，重试用尽。
    另外还要核对「所有标的的最后交易日是否同一天」：backtest.trading_days() 取的是
    各标的日期的**并集**，窗口一旦错开，5 天回测会拼成跨越两三周、后段只剩一两只
    标的的假窗口（2026-11-02 实测：13 天里有 7 天只有 1~3 只标的）。

退出码（给 shell / 自动化用，别指望读屏）：
    0 = 全部 fresh 且窗口一致；1 = 有 fail；2 = 没有 fail，但有 stale / partial / 窗口错开。
    2 不并进 0：「刷到了」和「能跑但不是新的」是两件事，脚本自己得先分得清。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market      # noqa: E402
import strategy    # noqa: E402

NDAYS = 5
ATTEMPTS = 3
WAIT_BASE = 25          # 退避 25s / 50s
SPACING = 6             # 每只之间间隔，避开东财分钟级限流
# 这些异常说明代码或数据结构坏了，不是限流。重试只会把 bug 拖成 75 秒的「限流」，
# 最后在 fail 里留一个代码，读日志的人看不出是代码问题。
NO_RETRY = (KeyError, IndexError, TypeError, AttributeError, NameError)
# 连续这么多只标的「重试到底仍是降级」就不再重试后面的：这说明整条接口挂了，
# 再逐只等 75 秒只是把盘前例行拖成一个小时，刷不回来一分数据。
GIVE_UP_STREAK = 3


def _classify(tr, ndays):
    """把一次 fetch_trends 的返回值分成 fresh / stale / partial / fail。"""
    if not tr:
        return "fail", "返回空"
    ticks = tr.get("ticks") or []
    if not ticks:
        return "fail", "0 根分时"
    days = sorted({t["t"][:10] for t in ticks})
    if tr.get("stale"):
        return "stale", tr.get("stale_reason") or "降级缓存"
    if len(days) < ndays:
        return "partial", "只有 %d 天（要 %d 天），截至 %s" % (len(days), ndays, days[-1])
    return "fresh", ""


def warm(codes=None, ndays=NDAYS, verbose=True):
    """拉取 ndays 日分时。返回汇总 dict（见模块 docstring 的四种结局）。

    codes=None 才代表「整池」；传空列表是「一只都不抓」——原实现用
    `codes or strategy.CORE_POOL` 把两者合成一回事，调用方想抓 0 只时
    会意外拉满 15 只、跑满两分钟。
    """
    if codes is None:
        codes = strategy.CORE_POOL
    codes = [str(c) for c in codes]
    buckets = {"fresh": [], "stale": [], "partial": [], "fail": []}
    why = {}            # code -> 结论文案
    last_day = {}       # code -> 最后一根分时的日期
    total = len(codes)

    stale_streak = 0    # 连续「重试到底仍是降级」的标的数，见 GIVE_UP_STREAK
    for i, c in enumerate(codes, 1):
        tr = None
        state = note = ""
        for attempt in range(ATTEMPTS):
            try:
                tr = market.fetch_trends(c, ndays=ndays, ttl=0)
            except NO_RETRY:
                raise
            except Exception as e:  # noqa: BLE001
                tr = None
                if attempt == ATTEMPTS - 1:
                    kind = type(e).__name__
                    if verbose:
                        print("[%d/%d] %s 失败：%s: %s" % (i, total, c, kind, str(e)[:80]))
                    buckets["fail"].append(c)
                    why[c] = "%s: %s" % (kind, str(e)[:60])
                else:
                    wait = WAIT_BASE * (attempt + 1)
                    if verbose:
                        print("[%d/%d] %s 重试中，等待 %ds… (%s)"
                              % (i, total, c, wait, str(e)[:60]))
                    time.sleep(wait)
                continue

            state, note = _classify(tr, ndays)
            if state == "fresh":
                break
            # 降级 = 这一次没刷到，只是拿旧文件顶上。对「把缓存刷到今天」来说
            # 它就是失败，必须重试。原实现这里直接 break 去记账，重试分支只对
            # **完全没有缓存**的标的生效（那种情况 fetch_trends 才抛错），于是
            # 最常见的情形——接口抽风、旧文件还在——一次都不重试：
            # 2026-11-16 实测 12/15 只第一次降级就定了稿，而同一台机器隔几分钟
            # 再连同一个主机就能拉通；反倒是不带缓存的 511260 老老实实等了 25s/50s。
            # 代价可控：一次降级约 30s（内部已含 5 次快重试），比一整天拿旧数据算
            # 回归基线便宜得多。partial（停牌/新股/接口截断）不重试，重试也变不出
            # 那几天的行情。
            if state == "stale" and attempt < ATTEMPTS - 1 and stale_streak < GIVE_UP_STREAK:
                wait = WAIT_BASE * (attempt + 1)
                if verbose:
                    print("[%d/%d] %s 降级，重试中，等待 %ds… (%s)"
                          % (i, total, c, wait, note[:50]))
                time.sleep(wait)
                continue
            break
        if tr is None:
            continue

        state, note = _classify(tr, ndays)
        buckets[state].append(c)
        why[c] = note
        stale_streak = stale_streak + 1 if state == "stale" else 0
        ticks = tr.get("ticks") or []
        days = sorted({t["t"][:10] for t in ticks})
        last_day[c] = days[-1] if days else None
        if verbose:
            label = {"fresh": "", "stale": "  [降级]", "partial": "  [不完整]"}.get(state, "")
            print("[%d/%d] %s %s  %d 点  日数 %d%s%s"
                  % (i, total, c, (tr.get("name") or c), len(ticks), len(days), label,
                     ("  " + note) if note else ""))
        if i < total:
            time.sleep(SPACING)

    out = {
        "requested": total,
        "fresh": buckets["fresh"],
        "stale": buckets["stale"],
        "partial": buckets["partial"],
        "fail": buckets["fail"],
        "why": why,
        "last_day": last_day,
        "window_mismatch": [],
    }
    # 窗口一致性：所有标的最后一次分时必须是同一天。
    # 不一致时回测的日期并集会拼出「只有一两只标的的交易日」，指标照算、看着正常。
    got = {v for v in last_day.values() if v}
    out["window_mismatch"] = sorted(got) if len(got) > 1 else []
    if verbose:
        _report(out)
    return out


def _report(out):
    n = out["requested"]
    print("\n完成：fresh %d / 降级 %d / 不完整 %d / 失败 %d（共 %d）"
          % (len(out["fresh"]), len(out["stale"]), len(out["partial"]),
             len(out["fail"]), n))
    for key, title in (("stale", "降级（没刷到，用的是旧缓存）"),
                       ("partial", "不完整（日数不够）"),
                       ("fail", "失败")):
        if out[key]:
            print("  %s：%s" % (title, "、".join(
                "%s（%s）" % (c, out["why"].get(c, "")) for c in out[key])))
    if out["window_mismatch"]:
        print("  窗口不一致：最后交易日有 %d 种（%s）——回测按日期并集会拼出"
              "跨越数周的假窗口，先补齐数据再跑回测"
              % (len(out["window_mismatch"]), "、".join(out["window_mismatch"])))


def main(codes=None, ndays=NDAYS):
    out = warm(codes=codes, ndays=ndays)
    if out["fail"]:
        return 1
    if out["stale"] or out["partial"] or out["window_mismatch"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
