# -*- coding: utf-8 -*-
"""离线优化：用历史交易日回放，网格搜索策略参数。

原则：只用「今天之前」的完整交易日做优化，今天的数据留作样本外执行。
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
    return pool


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

    # 基线
    base = strategy.PARAMS
    r = backtest.run_backtest(data, base, days, verbose=True)
    m = backtest.metrics(r)
    print(f"\n[基线 现行参数] {m}")
    for t in r["trades"]:
        print(f"  {t['day']} {t['t'][11:16]} {t['side']:<4} {t['code']} "
              f"{t['price']:.3f} x{t['shares']:>5} 盈亏{t['pnl']:>8.2f}  {t['reason']}")

    # 网格
    grid = build_grid(base)
    keys = list(grid)
    combos = list(itertools.product(*[grid[k] for k in keys]))
    print(f"\n网格搜索 {len(combos)} 组…")
    rows = []
    for n, vals in enumerate(combos, 1):
        p = dict(base)
        p.update(dict(zip(keys, vals)))
        rr = backtest.run_backtest(data, p, days)
        mm = backtest.metrics(rr)
        rows.append((mm["ret_pct"], mm["trades"], mm["win_rate"], p, mm))
        if n % 20 == 0:
            print(f"  …{n}/{len(combos)}")

    rows.sort(key=lambda x: (-x[0], -x[1]))
    best_n = rows[0][1] if rows else 0
    if best_n < MIN_TRADES_FOR_RANK:
        # 只有 1~2 笔的「Top1 收益」等于拟合了那一天的运气。
        # 以前这里直接把名次印出来，看不出样本量这个前提。
        print(f"\n[样本不足] 最优组也只有 {best_n} 笔成交 < {MIN_TRADES_FOR_RANK} 笔，"
              f"下面的排名只能当「哪些参数没被否决」看，不能当参数建议。")
    print("\nTop 10 参数组：")
    for i, (ret, n, wr, p, mm) in enumerate(rows[:10], 1):
        print(f"{i:>2}. 收益{ret:>6.2f}%  交易{n:>2}笔  胜率{wr:>5.1f}%  "
              f"dev{p['dev_enter']:.4f} mom{p['mom_turn']:.4f} "
              f"tp{p['take_profit']:.4f} sl{p['stop_loss']:.4f} hold{p['max_hold_min']}")

    return rows


if __name__ == "__main__":
    main()
