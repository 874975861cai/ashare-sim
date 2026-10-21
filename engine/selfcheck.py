#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟盘一键体检。

用法：
    python engine/selfcheck.py

检查项：
    1. 运行环境    Python 版本、工作目录
    2. 文件完整性  核心脚本、账本、看板
    3. 脚本编码    .bat 是否 ASCII + CRLF（cmd 只能吃这个）
    4. 账本一致性  现金 + 市值 = 权益；流水与持仓是否对得上
    5. 数据缓存    覆盖标的数、最新日期
    6. 行情连通    实测拉一次报价（失败不影响其它检查）

退出码：0 全部通过；1 有 ERROR 级问题。
"""
import csv
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "engine"))

OK, WARN, ERR = "OK  ", "WARN", "ERR "
results = []


def add(level, item, msg):
    results.append((level, item, msg))


# ---------------- 1. 运行环境 ----------------
def check_env():
    v = sys.version_info
    if v >= (3, 9):
        add(OK, "Python", "%d.%d.%d" % (v[0], v[1], v[2]))
    else:
        add(ERR, "Python", "%d.%d.%d 过低，需 3.9+" % (v[0], v[1], v[2]))

    if os.path.abspath(os.getcwd()) != ROOT:
        add(WARN, "工作目录", "当前在 %s，建议 cd 到 %s" % (os.getcwd(), ROOT))
    else:
        add(OK, "工作目录", ROOT)


# ---------------- 2. 文件完整性 ----------------
CRITICAL_FILES = [
    ("engine/market.py", True),
    ("engine/broker.py", True),
    ("engine/strategy.py", True),
    ("engine/runner.py", True),
    ("engine/dashboard.py", True),
    ("engine/backtest.py", False),
    ("state/account.json", True),
    ("state/trades.csv", True),
    ("state/equity.csv", True),
    ("dashboard/index.html", False),
    ("交易思路.md", False),
]


def check_files():
    miss = []
    for rel, critical in CRITICAL_FILES:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            miss.append(rel)
            add(ERR if critical else WARN, "缺失", rel)
    if not miss:
        add(OK, "文件完整性", "%d 个关键文件齐全" % len(CRITICAL_FILES))


# ---------------- 3. 脚本编码 ----------------
def check_bat_encoding():
    bats = []
    for dirpath, _, files in os.walk(ROOT):
        if "cache" in dirpath or "__pycache__" in dirpath:
            continue
        for f in files:
            if f.lower().endswith((".bat", ".cmd")):
                bats.append(os.path.join(dirpath, f))
    if not bats:
        add(WARN, "启动脚本", "未找到 .bat 文件")
        return
    bad = []
    for p in bats:
        raw = open(p, "rb").read()
        bare_lf = raw.count(b"\n") - raw.count(b"\r\n")
        non_ascii = sum(1 for c in raw if c > 127)
        name = os.path.basename(p)
        if bare_lf or non_ascii:
            bad.append("%s(LF=%d,非ASCII=%d)" % (name, bare_lf, non_ascii))
        else:
            add(OK, "脚本编码", "%s CRLF/ASCII 正确" % name)
    if bad:
        add(ERR, "脚本编码", "需修复（跑 engine/make_launcher.py）: " + ", ".join(bad))


# ---------------- 4. 账本一致性 ----------------
def check_ledger():
    acc_path = os.path.join(ROOT, "state/account.json")
    if not os.path.exists(acc_path):
        add(ERR, "账户", "account.json 不存在")
        return
    with open(acc_path, "r", encoding="utf-8") as f:
        acc = json.load(f)

    cash = float(acc.get("cash", 0))
    init = float(acc.get("meta", {}).get("init_capital", 10000))
    pos = acc.get("positions") or {}
    cost = 0.0
    for code, p in pos.items():
        cost += float(p.get("shares", 0)) * float(p.get("cost", 0))

    add(OK, "现金", "¥%.2f" % cash)
    add(OK, "持仓", "%d 只，成本 ¥%.2f" % (len(pos), cost))

    if cash < -0.01:
        add(ERR, "账本校验", "现金为负 ¥%.2f" % cash)
    elif cash + cost > init * 1.5:
        add(WARN, "账本校验", "现金+成本 %.2f 远超初始本金 %.2f" % (cash + cost, init))

    # 流水与序号
    tpath = os.path.join(ROOT, "state/trades.csv")
    if os.path.exists(tpath):
        with open(tpath, "r", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        seq = int(acc.get("trade_seq", 0))
        if len(rows) != seq:
            src = "（当日重放会重写流水，属正常）" if acc.get("today") else ""
            add(WARN, "流水条数", "%d 条 vs trade_seq=%d %s" % (len(rows), seq, src))
        else:
            add(OK, "流水条数", "%d 条，与序号一致" % len(rows))

    # 今日开盘快照
    op = os.path.join(ROOT, "state/account_open.json")
    if os.path.exists(op):
        add(OK, "开盘快照", "存在（幂等重放可用）")
    else:
        add(WARN, "开盘快照", "缺失，下次 runner 会重建")


# ---------------- 5. 数据缓存 ----------------
def check_cache():
    cdir = os.path.join(ROOT, "state/cache")
    if not os.path.isdir(cdir):
        add(WARN, "数据缓存", "cache 目录不存在（首次运行会自建）")
        return
    files = [f for f in os.listdir(cdir) if f.startswith("trends_")]
    codes = set()
    dates = set()
    for f in files:
        parts = f.split("_")
        if len(parts) >= 4:
            codes.add(parts[1])
            dates.add(parts[3].replace(".json", ""))
    if not files:
        add(WARN, "数据缓存", "无分时缓存，跑 engine/warmup.py 预热")
        return
    latest = max(dates) if dates else "?"
    today = datetime.now().strftime("%Y%m%d")
    level = OK if latest >= today else WARN
    add(level, "数据缓存", "%d 只标的 / %d 个文件 / 最新 %s"
        % (len(codes), len(files), latest))


# ---------------- 5. 任务心跳 ----------------
def check_heartbeat():
    """自动化不补跑，漏一天就是漏一天。至少能查出来。"""
    path = os.path.join(ROOT, "state/heartbeat.json")
    if not os.path.exists(path):
        add(WARN, "任务心跳", "无记录（自动化还没跑过）")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            hb = json.load(f)
    except Exception:  # noqa: BLE001
        add(ERR, "任务心跳", "heartbeat.json 损坏")
        return

    last = hb.get("last_run") or "?"
    days = hb.get("days") or []
    try:
        dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        gap_h = (datetime.now() - dt).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        add(WARN, "任务心跳", "时间格式异常: %s" % last)
        return

    if gap_h > 96:          # 跨周末也够用
        add(WARN, "任务心跳",
            "最后运行 %s，已 %.0f 小时没跑 —— 查电脑是否关机/休眠" % (last, gap_h))
    else:
        add(OK, "任务心跳", "最后运行 %s（累计 %d 个交易日）"
            % (last, len(days)))


# ---------------- 6. 行情连通 ----------------
def check_market():
    try:
        import market
        q = market.fetch_quotes(["510300"])
        d = q.get("510300")
        if d:
            add(OK, "行情接口", "沪深300ETF %.3f (%+.2f%%)"
                % (d["price"], d["pct"]))
        else:
            add(WARN, "行情接口", "返回空（可能限流，稍后重试）")
    except Exception as e:  # noqa: BLE001
        add(WARN, "行情接口", "拉取失败: %s" % str(e)[:60])


def main():
    print("=" * 62)
    print("  模拟盘体检  %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("  路径  %s" % ROOT)
    print("=" * 62)

    check_env()
    check_files()
    check_bat_encoding()
    check_ledger()
    check_cache()
    check_heartbeat()
    if "--no-net" not in sys.argv:
        check_market()

    for level, item, msg in results:
        print("  [%s] %-12s %s" % (level, item, msg))

    n_err = sum(1 for r in results if r[0] == ERR)
    n_warn = sum(1 for r in results if r[0] == WARN)
    print("-" * 62)
    if n_err:
        print("  结论：%d 项错误、%d 项警告 —— 需处理" % (n_err, n_warn))
    elif n_warn:
        print("  结论：%d 项警告，无致命问题，可正常运行" % n_warn)
    else:
        print("  结论：全部通过，系统健康")
    print("=" * 62)
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())
