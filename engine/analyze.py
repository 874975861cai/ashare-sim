# -*- coding: utf-8 -*-
"""信号质量诊断：不加任何出场规则，直接看「入场信号出现后」的未来收益分布。

如果未来 N 分钟的收益中位数扣掉成本后仍为负，说明假设本身错了，
再好的止盈止损也只是在负期望上做无用功。
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest     # noqa: E402
import strategy     # noqa: E402

HORIZONS = [5, 10, 20, 30, 60]
COST = 0.0015       # 往返成本 0.15%

CODES = ["510300", "510500", "512100", "159915", "510050", "588000", "510880",
         "159919", "510330", "159901"]


def diagnose(dev_th, mom_th, verbose=True):
    data = backtest.load_pool(CODES, 5, verbose=False, offline=True)
    days = backtest.trading_days(data)
    stats = {h: [] for h in HORIZONS}
    n_sig = 0
    per_day = defaultdict(int)

    for c, v in data.items():
        for day in days:
            ticks = v["days"].get(day)
            if not ticks or len(ticks) < 70:
                continue
            pre = v["preclose"].get(day) or ticks[0]["price"]
            for i in range(22, len(ticks) - max(HORIZONS)):
                tm = ticks[i]["t"][11:16]
                if not ("09:50" <= tm <= "14:20"):
                    continue
                dev = strategy.dev_from_vwap(ticks, i)
                mom = strategy.momentum(ticks, i, 3)
                dayret = ticks[i]["price"] / pre - 1
                if dev <= -dev_th and mom >= mom_th and dayret <= -0.004:
                    n_sig += 1
                    per_day[day] += 1
                    entry = ticks[i]["price"]
                    for h in HORIZONS:
                        stats[h].append(ticks[i + h]["price"] / entry - 1)

    if verbose:
        print(f"信号阈值 dev<=-{dev_th*100:.2f}%  mom3>={mom_th*100:.2f}%  "
              f"当日跌>=0.4%")
        print(f"信号总数 {n_sig}，日均 {n_sig/max(len(days),1):.1f} 个  "
              f"分布 {dict(per_day)}")
        print(f"\n{'持有':<6}{'样本':>6}{'均值':>9}{'中位数':>9}{'胜率>0':>8}"
              f"{'胜率>成本':>10}{'最好':>9}{'最差':>9}")
        for h in HORIZONS:
            r = stats[h]
            if not r:
                continue
            r_sorted = sorted(r)
            mean = sum(r) / len(r)
            med = r_sorted[len(r) // 2]
            wr = sum(1 for x in r if x > 0) / len(r) * 100
            wr_cost = sum(1 for x in r if x > COST) / len(r) * 100
            print(f"{h:<6}分{len(r):>6}{mean*100:>8.3f}%{med*100:>8.3f}%"
                  f"{wr:>7.1f}%{wr_cost:>9.1f}%{max(r)*100:>8.2f}%{min(r)*100:>8.2f}%")
    return stats, n_sig


if __name__ == "__main__":
    for dev_th in (0.0055, 0.0080, 0.0100):
        diagnose(dev_th, 0.0010)
        print()
