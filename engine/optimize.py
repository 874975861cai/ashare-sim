# -*- coding: utf-8 -*-
"""离线优化：用历史交易日回放，网格搜索策略参数。

原则：只用「今天之前」的完整交易日做优化，今天的数据留作样本外执行。

2026-11-09 自审（模块轮转）：这个脚本最大的问题不是算错，而是**算了一件
没有意义的事**。它的网格只有五个日内维度（dev_enter / mom_turn /
take_profit / stop_loss / max_hold_min），而 2026-11-07 策略已切到
`mode="mix"`（跨市场配比），决策在 `strategy.plan_mix` 里，上述五个参数
一个都不被读取。108 组组合跑出来必然是同一个数，脚本却照旧打印
「Top 10 参数组」，读者只会看到「dev 0.0070 排第一」。

所以现在有两道闸：
  1. 组合/持有模式下直接说明「这张网格量不到现役策略」，不排名；
  2. 日内模式下也先**实测**每个维度改一遍结果是否会变（`probe_dims`），
     把不生效的维度剔出网格。不写死参数名 —— 策略以后再换模式、
     或者某个参数变成没人读的遗留项，这里都会自己发现。
"""
import itertools
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest    # noqa: E402
import market      # noqa: E402
import strategy    # noqa: E402

GRID = {
    # 以 strategy.PARAMS 的当前值为中心，左右各取一档。
    # 2026-10-20 自审发现这里原本整张表停留在 v1.0（tp 0.0008~0.0026、
    # hold 30/60、dev 0.0040~0.0075），连当时的现行参数 0.0080/0.0050/20
    # 都不在搜索空间里 —— 优化器在搜一个已经不存在的策略版本，
    # 却会把「Top1 收益」当成对当前策略的建议。下面 build_grid() 会显式
    # 校验现参数一定在网格内，再漂移就直接报错，不再静默。
    "dev_enter": [0.0070, 0.0080, 0.0090],
    "mom_turn": [0.0005, 0.0010],
    "take_profit": [0.0035, 0.0050, 0.0065],
    "stop_loss": [0.0030, 0.0040],
    "max_hold_min": [15, 20, 30],
}

MIN_TRADES_FOR_RANK = 5   # 少于这个笔数，Top10 排名没有统计意义


def build_grid(params=None):
    """确认现行参数落在网格内；不在就补进去并明说。"""
    params = params or strategy.PARAMS
    grid = {k: list(v) for k, v in GRID.items()}
    for k, vals in grid.items():
        cur = params.get(k)
        if cur is None:
            continue
        if cur not in vals:
            print(f"  [警告] 网格未覆盖现行参数 {k}={cur}，已补入")
            grid[k] = sorted(vals + [cur])
    return grid


def _reject_reason(q, params):
    """为什么这只标的没进池子。区分「没报价」和「不达标」，不要合成一句。"""
    if not q:
        return "无报价（接口未返回）"
    if q.get("amount") is None or q.get("price") is None:
        # 报价字段残缺（接口改版/半截 JSON）。旧代码在这里直接 KeyError，
        # 更旧的一版会把它当成「不达标」——两种都不如直说。
        return "报价字段不全（缺成交额或价格）"
    if q["amount"] < params["min_amount"]:
        return f"成交额 {q['amount']/1e8:.1f}亿 < 门槛 {params['min_amount']/1e8:.0f}亿"
    if q["price"] < params["min_price"]:
        return f"价格 {q['price']:.3f} < 门槛 {params['min_price']:.1f}"
    return ""


def build_pool(top=15):
    p = dict(strategy.PARAMS)
    p["min_amount"] = 2e8
    p["min_price"] = 1.2
    q = market.fetch_quotes(strategy.CORE_POOL)
    pool = strategy.validate_pool(q, p)
    passed = {x["code"] for x in pool}
    print(f"核心池 {len(strategy.CORE_POOL)} 只，通过校验 {len(pool)} 只：")
    for x in pool:
        print(f"  {x['code']} {x['name'][:16]:<18} {x['price']:>7.3f} "
              f"额{x['amount']/1e8:>6.1f}亿 振幅{x['range_pct']:>5.2f}% "
              f"tick{x['tick_friction']:.3f}%")
    rejected = [(c, _reject_reason(q.get(c), p)) for c in strategy.CORE_POOL
                if c not in passed]
    rejected = [(c, r) for c, r in rejected if r]
    if rejected:
        print(f"  未入池 {len(rejected)} 只：")
        for c, r in rejected:
            print(f"    - {c} {r}")
    # top 以前声明了却没用（永远是「通过几只就是几只」）。回测窗口里
    # 每多一只就多一份数据缺失风险，所以这里真的截断，并且说出来。
    if top and len(pool) > top:
        print(f"  按 top={top} 截断：{len(pool)} → {top} 只（按振幅/成交额排序）")
        pool = pool[:top]
    return pool


def rank_rows(rows, min_trades=MIN_TRADES_FOR_RANK):
    """只把成交笔数够的组合拿去排名。

    为什么必须过滤（2026-11-09）：`metrics` 的 `ret_pct` 在「一笔没成交」
    时是 0.00%，而一组亏钱的参数是负的 —— 排序按收益降序，
    **什么都不做**的第一名。旧代码只打一条横幅说「样本不足」，
    下面照样把这张 Top10 印出来，读者按第一行去改参数就是改成一个不交易的策略。
    """
    return [r for r in rows if r[1] >= min_trades]


def probe_dims(data, days, params, grid, capital=None):
    """返回 `(inert, ref_metrics)`：在本窗口里「参数改了结果也不变」的维度。

    判据是实测，不是名单：把每个维度推到离现参数最远的一档，重跑一遍，
    收益/成交笔数全部相同就认定该维度在本窗口内没有作用面。
    """
    ref = backtest.metrics(backtest.run_backtest(
        data, params, days, capital=capital))
    inert = []
    for k, vals in grid.items():
        cur = params.get(k)
        try:
            cands = [v for v in vals if cur is None or v != cur]
            alt = max(cands, key=lambda v: abs(float(v) - float(cur or 0)))
        except (TypeError, ValueError):
            inert.append(k)
            continue
        p = dict(params)
        p[k] = alt
        m = backtest.metrics(backtest.run_backtest(
            data, p, days, capital=capital))
        if (m["ret_pct"], m["trades"], m["fills"]) == (
                ref["ret_pct"], ref["trades"], ref["fills"]):
            inert.append(k)
    return inert, ref


def _print_trades(r, limit=12):
    for t in r["trades"][:limit]:
        print(f"  {t['day']} {t['t'][11:16]} {t['side']:<4} {t['code']} "
              f"{t['price']:.3f} x{t['shares']:>5} 盈亏{t['pnl']:>8.2f}  {t['reason']}")


def main():
    pool = build_pool(15)
    codes = [p["code"] for p in pool]
    if not codes:
        print("\n池子为空，无法回测。先查报价接口，别把它当成「今天没好机会」。")
        return None

    print("\n加载分时数据…")
    data = backtest.load_pool(codes, ndays=6)
    if not backtest.trading_days(data):
        # 联网这条路今天可能整片挂掉（东财对 ndays>1 的批量请求经常直接断连），
        # 但本地缓存里也许还有完整的 5 日窗口。doctor 一直走的就是缓存，
        # 所以这里降级到缓存而不是直接放弃。
        print("  [降级] 联网分时没有拿到任何交易日，改用本地缓存（5 日窗口）…")
        data = backtest.load_pool(codes, ndays=5, offline=True)

    missing = sorted(set(codes) - set(data))
    if missing:
        print(f"  [注意] {len(missing)} 只无分时数据：{','.join(missing)}")

    today = datetime.now().strftime("%Y-%m-%d")
    all_days = backtest.trading_days(data)
    days = [d for d in all_days if d < today]
    print(f"\n优化样本日: {days}")

    # 样本不足必须明说停在哪一步，不能拿着空结果报「Top1 收益 0.00%」——
    # 0.00% 和「没有数据」在输出里长得一模一样，但结论完全相反。
    if not data:
        print("\n无任何分时数据：联网失败，且本地缓存也没有可读文件。")
        print("先跑 doctor.py 看数据源，别把它当成「今天没好机会」。")
        return None
    if not days:
        if all_days:
            print(f"\n缓存里的交易日只有 {','.join(all_days)}（含今日），"
                  f"没有可用于优化的完整历史日。")
        else:
            print("\n有标的条目但没有任何交易日数据，本次不产出参数建议。")
        return None
    if len(days) < 2:
        print(f"\n候选交易日仅 {len(days)} 天（{days[0]}），"
              f"单日样本一律不作数，本次不产出参数建议。")
        return None

    # 本金口径：回测必须按实盘本金跑。1 万和 10 万的差别不是「量级」而是
    # 手续费下限（5 元）与整手取整，同一份行情能差 0.19pp。
    cap = backtest.live_capital()
    base = dict(strategy.PARAMS)
    mode = str(base.get("mode") or "daytrade")
    print(f"\n回测本金 {cap:,.2f}（取自账本 meta.init_capital）· 模式 {mode}")

    r = backtest.run_backtest(data, base, days, capital=cap, verbose=True)
    m = backtest.metrics(r)
    print(f"\n[基线 现行参数] {m}")
    _print_trades(r)

    if mode in ("mix", "hold"):
        # 组合/持有模式的决策不在 decide() 里，下面这张日内网格从第一天起
        # 就没被现役策略读过。打印一张 108 组全等的「Top10」等于伪造「已检验」。
        print(f"\n[跳过网格] 现行模式 {mode} 下，本脚本的五个网格维度"
              f"（{'/'.join(GRID)}）都不被策略读取：")
        if mode == "mix":
            w = base.get("mix_weights") or {}
            print(f"  组合权重 {'、'.join(f'{c} {v*100:.0f}%' for c, v in w.items())}，"
                  f"调仓由「距上次 {int(base.get('mix_rebal_days') or 244)} 交易日」"
                  f"和「偏离超 {float(base.get('mix_band') or 0.12)*100:.0f}%」触发，"
                  f"两者是约束不是旋钮。")
            print("  年化/回撤的证据在 hiret.py（11 年日线），不在几天的分钟线上。")
        print("  硬凑一张恒等表，比不跑更危险：它给了一个虚假的「已检验」。")
        print("  需要评估日内参数，请先切回 mode=\"daytrade\" 并重跑本脚本。")
        return None

    grid = build_grid(base)
    inert, _ = probe_dims(data, days, base, grid, capital=cap)
    if inert:
        print(f"\n[剔除无效维度] {len(inert)} 个维度改在本窗口内不改变任何结果："
              f"{'、'.join(inert)}")
        print("  （不是「参数无效」，是这些参数在这批数据上没被触发；"
              "它们不进网格，避免用 0 笔差异去排名。）")
        grid = {k: v for k, v in grid.items() if k not in inert}
    if not grid:
        print("\n[跳过网格] 剩余维度为空：所有候选参数在本窗口内都改变不了结果，"
              "排名只会是无意义的并列。")
        return None

    keys = list(grid)
    combos = list(itertools.product(*[grid[k] for k in keys]))
    print(f"\n网格搜索 {len(combos)} 组（维度 {len(keys)}：{'/'.join(keys)}）…")
    rows = []
    for n, vals in enumerate(combos, 1):
        p = dict(base)
        p.update(dict(zip(keys, vals)))
        rr = backtest.run_backtest(data, p, days, capital=cap)
        mm = backtest.metrics(rr)
        rows.append((mm["ret_pct"], mm["trades"], mm["win_rate"], p, mm))
        if n % 20 == 0:
            print(f"  …{n}/{len(combos)}")

    rows.sort(key=lambda x: (-x[0], -x[1]))
    qualified = rank_rows(rows)
    if not qualified:
        # 全都不够 5 笔：名次在这里没有意义。仍然把前几组印出来，
        # 但标签必须写清「这是参照，不是建议」，别让第一行被当成结论。
        print(f"\n[样本不足] 全部 {len(rows)} 组都少于 {MIN_TRADES_FOR_RANK} 笔成交，"
              f"不排名。下面只是收益最高的几组，供看「哪些参数没被否决」，"
              f"**不能当参数建议**：")
        for i, (ret, n, wr, p, mm) in enumerate(rows[:5], 1):
            print(f"{i:>2}. 收益{ret:>6.2f}%  交易{n:>2}笔  胜率{wr:>5.1f}%  "
                  f"dev{p['dev_enter']:.4f} mom{p['mom_turn']:.4f} "
                  f"tp{p['take_profit']:.4f} sl{p['stop_loss']:.4f} "
                  f"hold{p['max_hold_min']}")
        return rows

    print(f"\nTop 10 参数组（仅含 ≥{MIN_TRADES_FOR_RANK} 笔成交的 {len(qualified)}/"
          f"{len(rows)} 组）：")
    for i, (ret, n, wr, p, mm) in enumerate(qualified[:10], 1):
        print(f"{i:>2}. 收益{ret:>6.2f}%  交易{n:>2}笔  胜率{wr:>5.1f}%  "
              f"dev{p['dev_enter']:.4f} mom{p['mom_turn']:.4f} "
              f"tp{p['take_profit']:.4f} sl{p['stop_loss']:.4f} hold{p['max_hold_min']}")

    return rows


if __name__ == "__main__":
    main()
