#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地账本和渲染层同步到云端展示副本，并判断要不要重新发布。

它做两件事，但**不负责发布**——发布要调平台工具，那是 agent 干的活：

    1. 比对并同步：account.json / trades.csv / equity.csv，以及
       dashboard.py / theme.py（云端和本地必须一模一样的渲染层）
    2. 判断线上是不是落后了：拿「当前内容指纹」跟上次发布后记下的
       指纹比，不同就说明线上还是旧的

为什么不比修改时间：
    broker 每次存快照都会重写 account.json，即使数字没变 mtime 也会变。
    拿 mtime 比会把「没变化」误判成「有新成交」，于是天天白发布一次、
    白重启一次沙箱，正在看页面的人还得闪一下。

为什么还要记「已发布指纹」：
    只比「本地 vs 云端目录」，会漏掉一种情况——目录早就同步了，但没人去
    发布，线上跑的还是旧代码。那种情况下账本和代码都"一致"，脚本却报
    NEED_PUBLISH=0，自动化就跳过了，线上永远停在旧版本。
    所以发布成功后必须调一次 `--mark`，把当时的指纹钉下来。

用法：
    python engine/sync_cloud.py            # 同步 + 判断，打印结论
    python engine/sync_cloud.py --check    # 只看，不动文件
    python engine/sync_cloud.py --mark     # 发布成功后调用，记下已发布指纹

最后一行给调用方（自动化任务）看：
    NEED_PUBLISH=1  → 线上落后了，需要重新发布
    NEED_PUBLISH=0  → 一致，什么都不用做
"""
import argparse
import hashlib
import json
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_STATE = os.path.join(BASE, "state")
SRC_ENGINE = os.path.join(BASE, "engine")

# 云端副本默认在同级目录；允许用环境变量覆盖，方便换机器
DST_ROOT = os.environ.get("CLOUD_DIR") or os.path.join(
    os.path.dirname(BASE), "TradingSim_cloud")
DST_STATE = os.path.join(DST_ROOT, "state")
DST_ENGINE = os.path.join(DST_ROOT, "engine")

FILES = ("account.json", "trades.csv", "equity.csv")

# 云端 serve.py 运行时真正会 import 的模块：这些必须和本地一模一样，
# 否则云端用的是旧池子、旧规则，页面就和账本对不上。
#   - dashboard / theme：渲染
#   - broker：serve 里要用 avg_cost / position_shares 算持仓
#   - strategy：serve 里要用 CORE_POOL 决定拉哪些标的的行情
#   - market：serve 用它拉报价与分时（2026-10-20 自审补入。原先漏了它，
#     结果是数据层的修复只落在本地，云端永远跑旧版行情代码，且没人会察觉）
# runner / backtest 只跑在本地，不同步；
# serve.py 两边故意不同（云端有 PORT / 0.0.0.0 / 写接口适配），也不能覆盖。
SHARED = ("dashboard.py", "theme.py", "broker.py", "strategy.py", "market.py")

MARK = os.path.join(DST_STATE, ".published")


def sha(path):
    """内容指纹。文件不存在返回 None，好和"空文件"区分开。"""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def brief(path):
    """把账本压成一句人话，方便一眼看出两边差在哪。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            acc = json.load(f)
    except Exception:  # noqa: BLE001
        return "（读不出来）"
    return "现金 %.2f / 持仓 %d / 流水 %d 笔" % (
        acc.get("cash", 0.0), len(acc.get("positions") or {}),
        acc.get("trade_seq", 0))


def fingerprint():
    """云端副本当前内容的指纹：账本 + 渲染代码。

    只在两边一致之后调用，所以它代表「云端目录现在长什么样」。
    """
    h = hashlib.sha256()
    for name in FILES:
        h.update((sha(os.path.join(DST_STATE, name)) or "-").encode())
    for name in SHARED:
        h.update((sha(os.path.join(DST_ENGINE, name)) or "-").encode())
    return h.hexdigest()[:16]


def read_mark():
    try:
        with open(MARK, "r", encoding="ascii") as f:
            return f.read().strip() or None
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    ap.add_argument("--mark", action="store_true",
                    help="发布成功后调用，记下当前指纹为「已发布」")
    args = ap.parse_args()

    if not os.path.isdir(DST_ROOT):
        print("云端副本不存在：%s" % DST_ROOT)
        print("NEED_PUBLISH=0")
        return 2

    os.makedirs(DST_STATE, exist_ok=True)

    if args.mark:
        fp = fingerprint()
        with open(MARK, "w", encoding="ascii") as f:
            f.write(fp)
        # 本地也留一份。push_cloud 靠它判断「刚重新发布过，线上账本可能
        # 已经被部署包覆盖回旧版本，必须重推一次」——只记在云端副本里的话，
        # 本地 push 看不见，就会出现「手机显示空仓、本地显示持仓」的分叉。
        try:
            with open(os.path.join(SRC_STATE, ".published"), "w",
                      encoding="ascii") as f:
                f.write(fp)
        except OSError:
            pass
        print("已发布指纹  %s" % fp)
        print("NEED_PUBLISH=0")
        return 0

    # ---- 同步渲染层 ----
    code_diff = [n for n in SHARED
                 if sha(os.path.join(SRC_ENGINE, n))
                 != sha(os.path.join(DST_ENGINE, n))]

    print("本地    %s" % brief(os.path.join(SRC_STATE, "account.json")))
    print("云端    %s" % brief(os.path.join(DST_STATE, "account.json")))

    if code_diff:
        print("代码    %s 与云端不一致" % "、".join(code_diff))
        if not args.check:
            for n in code_diff:
                shutil.copy2(os.path.join(SRC_ENGINE, n),
                             os.path.join(DST_ENGINE, n))
                print("  已同步  %s" % n)

    # ---- 同步账本 ----
    diff = [n for n in FILES
            if sha(os.path.join(SRC_STATE, n))
            != sha(os.path.join(DST_STATE, n))]
    if diff:
        print("差异    %s" % "、".join(diff))

    if args.check:
        stale = fingerprint() != read_mark()
        if diff or code_diff or stale:
            print("结论    有差异或线上落后（--check 未写入）")
            print("NEED_PUBLISH=1")
        else:
            print("结论    一致")
            print("NEED_PUBLISH=0")
        return 0

    for name in diff:
        src = os.path.join(SRC_STATE, name)
        dst = os.path.join(DST_STATE, name)
        if not os.path.exists(src):
            print("  跳过    %s（本地没有这个文件）" % name)
            continue
        shutil.copy2(src, dst)
        print("  已同步  %s" % name)

    # ---- 判断线上是否落后 ----
    fp = fingerprint()
    if fp == read_mark():
        print("结论    与上次发布一致，不需要发布")
        print("NEED_PUBLISH=0")
        return 0

    if not (diff or code_diff):
        print("结论    内容没变，但线上仍是旧版本（发布后未 --mark？）")
    else:
        print("结论    云端文件已更新，需重新发布后生效")
    print("NEED_PUBLISH=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
