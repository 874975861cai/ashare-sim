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
  SIM_LOCAL_WRITE=1 python engine/migrate_100k.py --apply   # 真写（先自动备份）

不加 SIM_LOCAL_WRITE 时 --apply 会被拒绝：本地 state/ 只是云端账本的只读
镜像（账本主人在 GitHub Actions），在这里改本金 = 与云端分叉。改完必须把
state/ 推回云端，否则下一轮 Actions 会用云端账本覆盖。
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


def flow_day(now, last_row):
    """入金记在哪一天 —— 这一步曾经写错，且错了不会报错。

    旧写法是「今天 + 1 天，跳过周末」，看着讲得通，实际两种常见情形都错：
      · 交易日盘中迁移 → 记到次日。当天那根净值行漏扣（假盈利 9 万），
        次日又在**已经含钱**的 day_start 上再扣一次（假亏损 9 万）。
      · 收盘后迁移 → 记到次日，但当天那根行已经定稿，钱要算到下一笔变动。
    配合 broker.snapshot 的区间消费（(上一行日期, 今天]），规则应当是：
    记在「上一根净值行之后、且不早于今天」的第一天。
    """
    today = now.date()
    if last_row and last_row > today:
        # 未来日期的行只可能是占位行（如 11-07 预写的 11-09 入金行），
        # 那天会被真实数据重写，入金必须记在那天，记到它后面就会多扣一次。
        return last_row.strftime("%Y-%m-%d")
    if last_row and last_row == today and now.hour >= 15:
        # 今天的行已经定稿（收盘后），钱算下一笔变动；盘中(<15:00)则当场记，
        # 因为当天还会再写一次快照，会把这一天重算进去。
        return (today + timedelta(days=1)).strftime("%Y-%m-%d")
    return today.strftime("%Y-%m-%d")


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
    last_row = None
    basis = "成本法"
    why = ""
    now = datetime.now()
    try:
        import csv
        with open(broker.EQUITY_FILE, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        rows, why = [], "净值表读不到（%s）" % e.__class__.__name__
    if rows:
        last = rows[-1]
        try:
            last_row = datetime.strptime(last.get("date") or "", "%Y-%m-%d").date()
            age = (now.date() - last_row).days
            if age <= 5:          # 周末迁移时最后一行是 2~3 天前，仍然有效
                book = float(last["equity"])
                basis = "市值快照"
                # age 可能为负：入金占位行预先写在未来（11-07 就写了 11-09 那行），
                # 直接打「-1 天前」会读成人话里的相反意思。
                when = ("%d 天前" % age if age > 0
                        else "今天" if age == 0
                        else "预写占位行，%d 天后" % -age)
                print("权益口径：%s 市值快照 ¥%s（%s）"
                      % (last["date"], f"{book:,.2f}", when))
            else:
                why = "净值表最后一行为 %d 天前，超过 5 天" % age
        except (KeyError, ValueError, TypeError):
            why = "净值表字段无法解析（date=%r）" % (last.get("date"),)
    if book is None:
        book = cash
        for c, p in (acc.get("positions") or {}).items():
            book += broker.position_shares(p) * broker.avg_cost(p)
        # 降级要说清理由：成本法漏掉浮动盈亏，目标金额会跟着偏差。
        print("权益口径：成本法 ¥%s%s"
              % (f"{book:,.2f}", "（降级原因：%s）" % why if why else ""))

    need = round(args.target - book, 2)
    day = flow_day(now, last_row)

    # 标签必须跟着实际口径走：这里曾经写死「成本口径」，而 book 多半来自
    # 市值快照，两者差一个浮动盈亏（今天正好差 89.10），读数的人会按错的口径
    # 去核对手里的数。和 11-07 周报「净盈亏」标签错是同一类问题。
    print("当前账面（%s） ¥%s" % (basis, f"{book:,.2f}"))
    print("  现金 ¥%s  持仓 %s" % (
        f"{cash:,.2f}",
        "、".join("%s %d股@%.3f" % (c, broker.position_shares(p), broker.avg_cost(p))
                 for c, p in (acc.get("positions") or {}).items()) or "无"))
    print("累计投入本金 ¥%s" % f"{init:,.2f}")

    if init >= args.target - 1:
        print("\n已是 %.0f 元口径（init_capital=%.2f），无需迁移。"
              % (args.target, init))
        return 0
    if need <= 0:
        print("\n账面已超 %.0f（当前 ¥%s，多出 ¥%s），本脚本只加钱不减钱，"
              "无需追加。" % (args.target, f"{book:,.2f}", f"{-need:,.2f}"))
        return 0

    print("\n追加入金 ¥%s，记入 %s" % (f"{need:,.2f}", day))
    print("迁移后：现金 ¥%s   累计本金 ¥%s"
          % (f"{cash + need:,.2f}", f"{init + need:,.2f}"))
    print("注意：2900 股 510880 不在这里卖，由下一个交易日 09:35 的")
    print("      plan_mix「移出组合」按真实价格清掉。")

    if not args.apply:
        print("\n[演练] 未写盘。加 --apply 执行。")
        return 0
    # 本地 state/ 只是云端账本的只读镜像（账本主人在 GitHub Actions）。
    # 在这里改本金 = 与云端分叉：下一轮 Actions 用云端账本重放，白改，
    # 而且本地镜像从此和线上对不上，复盘会拿错数字。和 sync_cloud.py 同一个闸。
    if os.environ.get("SIM_LOCAL_WRITE") != "1":
        print("\n[拒绝写盘] 本地 state/ 是云端账本的只读镜像，不在这里改账本。")
        print("  确实要动账本时，显式加环境变量：")
        print("    SIM_LOCAL_WRITE=1 python engine/migrate_100k.py --apply")
        print("  改完必须把 state/ 推回云端，否则下一轮 Actions 会覆盖它。")
        return 2

    # 带秒：同一分钟内跑两次不会撞上已存在的备份目录（撞了会在写盘前抛栈）。
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
    # 这张便签以前写死「2026-11-07 迁移：本金 1 万 -> 10 万」。再迁一次
    # （比如加到 20 万）它照样这么写，读的人会以为钱没加进去。改成按实际数字生成。
    meta["capital_note"] = ("%s 入金 %s：本金 %s -> %s"
                            % (day, f"{need:,.2f}",
                               f"{init:,.2f}", f"{init + need:,.2f}"))
    flows = acc.setdefault("flows", {})
    flows[day] = round(float(flows.get(day) or 0) + need, 2)
    broker.save_account(acc)
    print("已写入 account.json：cash=%.2f  init_capital=%.2f  flows[%s]=%.2f"
          % (acc["cash"], meta["init_capital"], day, flows[day]))
    print("equity.csv 未动：净值行的权益由下一根快照按 cash+市值 重算。")
    print("下一步：把 state/ 推回云端（本地改完不推 = 下一轮 Actions 覆盖它）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
