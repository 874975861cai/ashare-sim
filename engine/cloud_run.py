# -*- coding: utf-8 -*-
"""云端单入口：给 GitHub Actions 之类的外部定时器调用一次就跑完。

为什么不搬「常驻循环」：runner.run() 是确定性重放——每次运行都把账本恢复到
当日开盘状态，再用当日分时从 09:30 重演到最后一根。所以外部定时器**不需要**
精确到分钟，延迟几十分钟也只影响「手机上看到得晚一点」，成交价和成交时刻都
取自分时数据里的分钟戳，不受影响。

这一点是整套云端方案能白嫖免费额度成立的根本原因：不需要保活一个进程，
一天被调用几十次、甚至只调用一次，账本结果都一样。

用法：
    python engine/cloud_run.py            交易时段内正常执行
    python engine/cloud_run.py --force    跳过时段判断（人工补跑/调试）
    python engine/cloud_run.py --check    只判断该不该跑，不执行

退出码：
    0 = 已执行
    2 = 不在交易时段 / 非交易日，正常跳过（不是错误）
    3 = 在窗口内但拿不到行情（无标的 / 无分时），这是故障，不是跳过
    1 = 执行出错
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dashboard   # noqa: E402
import runner      # noqa: E402

# 云端机器的系统时区不可靠（GitHub runner 是 UTC），一律按北京时间判断。
CST = timezone(timedelta(hours=8))

# 上午段 / 下午段。上界刻意放到 :59 而不是收盘时刻：
# GitHub 的 cron 本身就有 5-20 分钟抖动，若严格卡在 11:35 / 15:30，
# 一次抖动就会让当轮被误判成「非交易时段」而跳过。重放引擎用的是分时数据
# 里的分钟戳，晚跑半小时结果完全一样，所以放宽只有好处。
WINDOWS = (("09:30", "11:59"), ("13:00", "15:59"))


def now_cst():
    return datetime.now(CST)


def in_window(now=None):
    now = now or now_cst()
    if now.weekday() >= 5:
        return False, "周末"
    hm = now.strftime("%H:%M")
    for lo, hi in WINDOWS:
        if lo <= hm <= hi:
            return True, hm
    return False, "非交易时段（%s）" % hm


def main(argv):
    force = "--force" in argv
    check = "--check" in argv

    now = now_cst()
    ok, why = in_window(now)
    print("北京时间 %s  %s" % (now.strftime("%Y-%m-%d %H:%M:%S"), why))

    if not ok and not force:
        print("跳过：不在可执行窗口内。")
        return 2
    if check:
        print("结论：应当执行。")
        return 0

    # runner.run() 内部已经包含：
    #   交易日守卫（分时日期 != 今天就退出，防止节假日重放昨天造出假成交）
    #   幂等重放、T+1 校验、尾盘强平、写账本、自动 push 到云端看板
    res = runner.run(verbose=True)
    if res is None:
        # 守卫拦下（非交易日/无行情）。这种情况不该算失败，但也别继续生成看板，
        # 否则会把昨天的净值当成今天写进 equity.csv。
        #
        # 但「非交易日」和「接口拿到 0 条行情」必须分开：前者是日历，后者是故障。
        # 原来一律返回 2，workflow 把 2 当「正常跳过」，于是数据源挂一整天
        # 也和周末一模一样——账本不动、日志干净、没人知道。
        why = getattr(runner, "LAST_SKIP", "") or "unknown"
        if why == "not_trading_day":
            print("非交易日/未开盘，本轮正常跳过。")
            return 2
        print("在交易窗口内但没有可用行情（%s），本轮算失败。" % why)
        return 3

    # 静态看板：云端用 Pages 或任意静态托管展示，不依赖常驻服务
    try:
        path = dashboard.build(quiet=False)
        print("看板: %s" % path)
    except Exception as e:  # noqa: BLE001
        # 看板挂了不能影响账本，账本才是唯一的真相
        print("看板生成失败（不影响账本）: %s: %s" % (type(e).__name__, e))

    print("云端执行完成。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(json.dumps({"ok": False, "error": "%s: %s" % (type(e).__name__, e)},
                         ensure_ascii=False))
        raise SystemExit(1)
