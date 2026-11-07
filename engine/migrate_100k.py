#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把模拟盘从 1 万 / 单标的红利，迁移到 10 万 / 跨市场配比组合。

这一次迁移和之前所有「调参」不同：它同时改了三条线
----------------------------------------------------
  1 本金  1 万 -> 10 万（追加约 9.02 万）
  2 结构  单标的持有 -> 三资产配比（纳指/黄金/创业板 40/30/30）
  3 账本  必须新增「入金流」概念，否则当日盈亏会把入金算成盈利

为什么不能直接把 cash 加上去就完事
----------------------------------
`broker.snapshot` 的 day_pnl = 今日权益 - 昨日权益。直接加钱，周一的
「当日盈亏」会显示 +90,169.60，然后：
  · 周报「最佳交易日」永久被这条假记录占据
  · 日胜率、日波动、夏普全部失真
  · 以后再也没法用 day_pnl 做任何判断
所以 `acc["flows"]` 记下入金日与金额，snapshot 扣掉它。已同步改
`broker.snapshot`（新增 flow 列），本脚本只负责写入。

510880 不在这里卖
-----------------
持仓必须由 runner 在真实价格、真实时点卖掉。脚本里卖掉 = 用收盘价
虚构一笔成交，和 2026-10-28「重放建仓」是同一类错误。所以这里保留
2900 股红利，等下一个交易日 09:35 由 plan_mix 的「移出组合」清掉。

用法
----
  python engine/migrate_100k.py            # 演练，只打印不落盘
  python engine/migrate_100k.py --apply    # 真正执行（先自动备份）
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "engine"))

import broker  # noqa: E402

TARGET = 100000.0            # 目标总资金
STATE = os.path.join(BASE, "state")


def next_weekday(d):
    """入金记在「下一个工作日」——周末迁移时今天就入金是假账。"""
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写盘")
    ap.add_argument("--target", type=float, default=TARGET)
    args = ap.parse_args()

    acc = broker.load_account()
    meta = acc.setdefault("meta", {})
    init = float(meta.get("init_capital") or 0)
    cash = float(acc.get("cash") or 0)

    # 权益口径优先用 equity.csv 最后一行的市值快照 —— 用户说的「加到 10 万」
    # 是总资产到 10 万，不是「投入成本到 10 万」。用成本口径会凭空多算
    # 浮亏那部分（当前 -89.10），结果账面只有 99,910.90。
    book = None
    try:
        import csv
        with open(broker.EQUITY_FILE, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        if rows:
            last = rows[-1]
            age = (datetime.now().date()
                   - datetime.strptime(last["date"], "%Y-%m-%d").date()).days
            if age <= 5:          # 周末迁移时最后一行是 2~3 天前，仍然有效
                book = float(last["equity"])
                print("权益口径：%s 市值快照 ¥%s（%d 天前）"
                      % (last["date"], f"{book:,.2f}", age))
    except (OSError, KeyError, ValueError):
        book = None
    if book is None:
        book = cash
        for c, p in (acc.get("positions") or {}).items():
            book += broker.position_shares(p) * broker.avg_cost(p)
        print("权益口径：成本法（无可用净值快照）¥%s" % f"{book:,.2f}")

    need = round(args.target - book, 2)
    day = next_weekday(datetime.now().date())

    print("当前账面（成本口径） ¥%s" % f"{book:,.2f}")
    print("  现金 ¥%s  持仓 %s" % (
        f"{cash:,.2f}",
        "、".join("%s %d股@%.3f" % (c, broker.position_shares(p), broker.avg_cost(p))
                 for c, p in (acc.get("positions") or {}).items()) or "无"))
    print("累计投入本金 ¥%s" % f"{init:,.2f}")

    if init >= args.target - 1:
        print("\n已是 10 万口径（init_capital=%.2f），无需迁移。" % init)
        return 0
    if need <= 0:
        print("\n账面已达到 %.0f，无需追加。" % args.target)
        return 0

    print("\n追加入金 ¥%s，记入 %s（下一个工作日）" % (f"{need:,.2f}", day))
    print("迁移后：现金 ¥%s   累计本金 ¥%s"
          % (f"{cash + need:,.2f}", f"{init + need:,.2f}"))
    print("注意：2900 股 510880 不在这里卖，由下一个交易日 09:35 的")
    print("      plan_mix「移出组合」按真实价格清掉。")

    if not args.apply:
        print("\n[演练] 未写盘。加 --apply 执行。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    dst = os.path.join(STATE, "_backup_%s" % stamp)
    shutil.copytree(STATE, dst, ignore=shutil.ignore_patterns(
        "_backup_*", "cache", "*.log"))
    print("\n已备份 state -> %s" % dst)

    acc["cash"] = round(cash + need, 2)
    meta["init_capital"] = round(init + need, 2)
    # 持仓上限从 1 放到 3：broker 默认仍是 1，不放这一条，组合第二只就拒单。
    # 2026-11-07 写用例时踩过——只买成了第一只，日志是「持仓上限 1 只」。
    import strategy
    meta["max_positions"] = len(strategy.PARAMS.get("mix_weights") or {}) or 1
    meta["capital_note"] = ("2026-11-07 迁移：本金 1 万 -> 10 万，"
                            "结构 单标的红利 -> 纳/金/创 40/30/30")
    flows = acc.setdefault("flows", {})
    flows[day] = round(float(flows.get(day) or 0) + need, 2)
    broker.save_account(acc)
    print("已写入 account.json：cash=%.2f  init_capital=%.2f  flows[%s]=%.2f"
          % (acc["cash"], meta["init_capital"], day, flows[day]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
