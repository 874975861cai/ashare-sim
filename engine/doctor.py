#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动体检 + 自愈（doctor）。

为什么需要它：
    selfcheck.py 只回答「文件在不在、接口通不通」。
    它查不出真正会咬人的东西：账本算错一分钱、流水序号断号、
    页面写的是三天前的数、某次改动让回测结果悄悄漂移。
    这些都不会报错，只会安静地给出错误结论。

    所以这里换一套标准：**不变量**。
    不变量是「无论跑多少次都必须成立的等式」，一旦破了就是 bug，
    不需要人去猜哪里不对。

分级：
    OK    正常
    FIX   发现问题，已自动修好
    WARN  可疑，但不动账本，需要人看一眼
    CRIT  硬规则被违反 / 账务对不上，脚本不会擅自改钱，必须处理

用法：
    python engine/doctor.py              # 体检 + 自动修复
    python engine/doctor.py --no-fix     # 只体检
    python engine/doctor.py --deep       # 加联网数据质量检查
    python engine/doctor.py --quiet      # 只打印有问题的项
    python engine/doctor.py --json       # 机读输出

退出码：0 = 没有未处理的 CRIT，1 = 有。
"""
import argparse
import ast
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import broker      # noqa: E402  账本路径只认 broker，避免两处各写一份

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "engine")
STATE = os.path.join(ROOT, "state")
CACHE = os.path.join(STATE, "cache")
PAGE = os.path.join(ROOT, "dashboard", "index.html")
JOURNAL = os.path.join(ROOT, "journal")

OK, FIX, WARN, CRIT = "OK", "FIX", "WARN", "CRIT"
RANK = {OK: 0, FIX: 1, WARN: 2, CRIT: 3}

RESULTS = []
DO_FIX = True


def add(level, area, item, msg):
    RESULTS.append({"level": level, "area": area, "item": item, "msg": msg})


def _read_json(p, default=None):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _read_trades():
    p = broker.TRADE_FILE
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("seq")]


# ==========================================================================
# 账本重放：纯函数，方便自测
# ==========================================================================
def replay(rows, init=10000.0):
    """按流水重算现金/持仓/已实现盈亏，并收集规则违规。

    必须用**和 broker 完全相同的算法**：先进先出、按批次核销、
    卖出费用按份额摊到每个批次。用平均成本法重算会和券商对不上，
    然后检查器自己就是错的——这种「看起来对」的检查比没有更危险。

    返回 (cash, positions, realized, problems)
    positions: {code: [{"shares","price","fee"}]}，未被核销的批次
    problems : [(级别, 描述)]
    """
    cash = float(init)
    pos = {}
    realized = 0.0
    probs = []
    expect_seq = 1

    for r in rows:
        try:
            seq = int(r["seq"])
            code = r["code"]
            side = r["side"].upper()
            price = float(r["price"])
            shares = int(float(r["shares"]))
            amount = float(r["amount"])
            fee = float(r["total_fee"])
        except (KeyError, ValueError):
            probs.append((CRIT, "流水字段无法解析: %s" % (r.get("seq"),)))
            continue

        # ---- 序号连续 ----
        if seq != expect_seq:
            probs.append((WARN, "流水序号断档：期望 %d，实际 %d" % (expect_seq, seq)))
        expect_seq = seq + 1

        # ---- 金额 = 价 × 量 ----
        if abs(amount - price * shares) > 0.02:
            probs.append((CRIT, "#%d 金额不符：%.2f ≠ %.3f×%d" %
                          (seq, amount, price, shares)))

        # ---- 费用公式 ----
        comm = max(amount * 0.00025, 5.0)
        if abs(float(r["commission"]) - round(comm, 2)) > 0.02:
            probs.append((WARN, "#%d 佣金不符：%.2f ≠ %.2f" %
                          (seq, float(r["commission"]), round(comm, 2))))
        if abs(fee - (float(r["commission"]) + float(r["stamp_tax"])
                      + float(r["transfer_fee"]))) > 0.02:
            probs.append((WARN, "#%d 费用合计不符" % seq))

        # ---- 交易时段 ----
        d = ""
        try:
            dt = datetime.strptime(r["datetime"], "%Y-%m-%d %H:%M:%S")
            hm = dt.hour * 100 + dt.minute
            d = dt.strftime("%Y-%m-%d")
            if not ((930 <= hm <= 1130) or (1300 <= hm <= 1500)):
                probs.append((CRIT, "#%d 成交在非竞价时段 %s" % (seq, r["datetime"][11:])))
        except ValueError:
            probs.append((WARN, "#%d 时间无法解析" % seq))

        if side == "BUY":
            if shares <= 0 or shares % 100 != 0:
                probs.append((CRIT, "#%d 买入非整百：%d" % (seq, shares)))
            cash -= (amount + fee)
            pos.setdefault(code, []).append(
                {"shares": shares, "price": price, "fee": fee, "date": d})
        else:
            lots = pos.get(code, [])
            left = shares
            cost = 0.0
            new_lots = []
            for lot in lots:
                if left <= 0:
                    new_lots.append(lot)
                    continue
                # T+1：股票当日买入的批次不可卖
                if lot.get("date") == d and not broker.is_etf(code):
                    new_lots.append(lot)
                    continue
                take = min(lot["shares"], left)
                u_fee = lot["fee"] * take / lot["shares"]
                u_sell = fee * take / shares if shares else 0.0
                cost += take * lot["price"] + u_fee + u_sell
                left -= take
                if take < lot["shares"]:
                    new_lots.append({"shares": lot["shares"] - take,
                                     "price": lot["price"],
                                     "fee": round(lot["fee"] - u_fee, 2),
                                     "date": lot.get("date", "")})
            if left > 0:
                probs.append((CRIT, "#%d 卖出超过持仓：多出 %d 份" % (seq, left)))
            else:
                pos[code] = new_lots
            cash += (amount - fee)
            pnl_calc = round(amount - cost, 2)
            realized += pnl_calc
            try:
                if r.get("pnl") and abs(float(r["pnl"]) - pnl_calc) > 0.02:
                    probs.append((WARN, "#%d 盈亏记录 %.2f ≠ 重算 %.2f" %
                                  (seq, float(r["pnl"]), pnl_calc)))
            except ValueError:
                probs.append((WARN, "#%d 盈亏字段非数字" % seq))

        # ---- 现金链路 ----
        try:
            if abs(float(r["cash_after"]) - cash) > 0.02:
                probs.append((CRIT, "#%d 现金链断裂：账面 %.2f，重算 %.2f" %
                              (seq, float(r["cash_after"]), cash)))
        except (KeyError, ValueError):
            probs.append((WARN, "#%d 缺 cash_after" % seq))

    pos = {c: l for c, l in pos.items() if l}
    return round(cash, 2), pos, round(realized, 2), probs


def lot_cost(lots):
    """批次成本：股数×价格 + 该批次买入费，与 broker.avg_cost 同源。"""
    return round(sum(l["shares"] * l["price"] + l.get("fee", 0) for l in lots), 2)


def ledger_identity(acc, cash, pos, realized):
    """核心不变量：现金 + 未核销批次成本 == 初始本金 + 已实现盈亏。"""
    init = float((acc.get("meta") or {}).get("init_capital") or 10000.0)
    hold_cost = sum(lot_cost(v) for v in pos.values())
    left = round(cash + hold_cost, 2)
    right = round(init + realized, 2)
    return left, right, round(left - right, 2)


def selftest_replay():
    """验证检查器本身有效。

    真实账本经常是空的（今天 0 笔），空账本上任何检查都「通过」——
    那是假的安全感。所以这里构造一份正确账本 + 一份被篡改的账本，
    确认正确的不报警、错误的必被抓到。
    """
    good = [
        {"seq": "1", "datetime": "2026-10-17 10:00:00", "code": "510300", "name": "沪深300ETF",
         "side": "BUY", "price": "4.000", "shares": "2000", "amount": "8000.00",
         "commission": "5.00", "stamp_tax": "0.00", "transfer_fee": "0.00",
         "total_fee": "5.00", "net_cash": "-8005.00", "cash_after": "1995.00",
         "pnl": "", "reason": "t"},
        {"seq": "2", "datetime": "2026-10-17 10:20:00", "code": "510300", "name": "沪深300ETF",
         "side": "SELL", "price": "4.020", "shares": "2000", "amount": "8040.00",
         "commission": "5.00", "stamp_tax": "0.00", "transfer_fee": "0.00",
         "total_fee": "5.00", "net_cash": "8035.00", "cash_after": "10030.00",
         "pnl": "30.00", "reason": "t"},
    ]
    bad = [dict(good[0]), dict(good[1])]
    bad[1]["cash_after"] = "9999.00"      # 篡改现金
    bad[1]["shares"] = "1990"             # 非整百 + 超过持仓？持股 2000，卖 1990 合法但金额不符

    fails = []
    c, p, r, probs = replay(good, 10000.0)
    if probs:
        fails.append("正确账本被误报: %s" % probs[:2])
    if abs(c - 10030.0) > 0.01:
        fails.append("正确账本现金算出 %.2f，应为 10030.00" % c)
    if p:
        fails.append("正确账本应无持仓")

    _, _, _, probs2 = replay(bad, 10000.0)
    if not any("现金链断裂" in m for _, m in probs2):
        fails.append("篡改现金未被发现")
    return fails


# ==========================================================================
# A 运行时
# ==========================================================================
def _ping(port, timeout=0.5):
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port,
                                    timeout=timeout) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception:  # noqa: BLE001
        return False


def find_port():
    for p in range(8899, 8920):
        if _ping(p):
            return p
    return None


def check_service():
    pidf = os.path.join(STATE, "serve.pid")
    port = find_port()
    if port:
        add(OK, "运行时", "实时服务", "在线，端口 %d" % port)
        return port

    # 端口没人监听，但 pid 文件还在 → 上次是异常退出
    if os.path.exists(pidf):
        if DO_FIX:
            try:
                os.remove(pidf)
                add(FIX, "运行时", "残留 pid", "已清理 state/serve.pid（进程早就不在了）")
            except OSError:
                add(WARN, "运行时", "残留 pid", "无法删除 state/serve.pid")
        else:
            add(WARN, "运行时", "残留 pid", "进程不存在但 pid 文件还在")

    # 尝试拉起
    if DO_FIX:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            subprocess.Popen([exe, os.path.join(ENGINE, "serve.py"), "--no-browser"],
                             cwd=ROOT, startupinfo=si, close_fds=True)
            for _ in range(25):
                time.sleep(0.4)
                if find_port():
                    add(FIX, "运行时", "实时服务", "原来没跑，已自动拉起")
                    return find_port()
        except Exception as e:  # noqa: BLE001
            add(WARN, "运行时", "拉起失败", str(e)[:60])

    add(CRIT, "运行时", "实时服务", "不在运行，页面只能是快照。双击 打开模拟盘.bat")
    return None


def _page_age():
    if not os.path.exists(PAGE):
        return None
    return time.time() - os.path.getmtime(PAGE)


def check_page(port):
    if not os.path.exists(PAGE):
        if DO_FIX and _regen_page():
            add(FIX, "运行时", "看板文件", "缺失，已重新生成")
        else:
            add(CRIT, "运行时", "看板文件", "dashboard/index.html 不存在")
        return

    age = _page_age()
    # 服务在线时，页面每 30 秒会被回写一次；超过 5 分钟说明回写没生效
    limit = 300
    if age is not None and age > limit:
        if DO_FIX and _regen_page():
            add(FIX, "运行时", "页面新鲜度",
                "快照已 %.0f 分钟没更新，已重写" % (age / 60))
        else:
            add(WARN, "运行时", "页面新鲜度", "快照已 %.0f 分钟没更新" % (age / 60))
    else:
        add(OK, "运行时", "页面新鲜度", "上次写入 %.0f 秒前" % (age or 0))

    # 页面必须带自更新脚本 + 至少一个可连的接口地址，否则永远是快照
    try:
        with open(PAGE, "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        add(CRIT, "运行时", "看板文件", "无法读取")
        return

    gaps = _page_gaps(html)
    if gaps:
        # 只报警不够。能确定页面是旧版，就该直接重写成当前版本——
        # 否则用户下次打开还是那张不会动的照片。
        if DO_FIX and _regen_page():
            try:
                with open(PAGE, "r", encoding="utf-8") as f:
                    gaps = _page_gaps(f.read())
            except OSError:
                gaps = ["页面重写后仍无法读取"]
            if not gaps:
                add(FIX, "运行时", "页面脚本", "旧版页面已重写为当前版本")
                return
        add(WARN, "运行时", "页面脚本", "缺少：" + "、".join(gaps))
    else:
        add(OK, "运行时", "页面脚本", "自更新脚本完整")


def _page_gaps(html):
    need = {"livebar": "断线横幅", "127.0.0.1:8899": "本地接口地址",
            "requestAnimationFrame": "资金流场动画"}
    return [v for k, v in need.items() if k not in html]


def _regen_page():
    try:
        import dashboard
        acc, trades, equity = dashboard.load_state()
        pool, src = dashboard.pool_status(total_budget=8)
        html = dashboard.render(acc, trades, equity, pool, src, live=False)
        os.makedirs(os.path.dirname(PAGE), exist_ok=True)
        with open(PAGE, "w", encoding="utf-8") as f:
            f.write(html)
        return True
    except Exception:  # noqa: BLE001
        return False


def check_heartbeat():
    hb = _read_json(os.path.join(STATE, "heartbeat.json"))
    if not hb:
        add(WARN, "运行时", "任务心跳", "无记录（自动化还没跑过）")
        return
    last = str(hb.get("last_run") or "")
    try:
        dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        add(WARN, "运行时", "任务心跳", "时间格式异常 %s" % last)
        return
    gap = (datetime.now() - dt).total_seconds() / 3600
    # 周末与长假不判，只看「超过 40 小时没跑过」
    if gap > 40:
        add(WARN, "运行时", "任务心跳",
            "已 %.0f 小时没运行（最后 %s），检查自动化是否被关" % (gap, last))
    else:
        add(OK, "运行时", "任务心跳", "最后 %s" % last)


# ==========================================================================
# B 账本
# ==========================================================================
def check_ledger():
    acc = _read_json(broker.ACCOUNT_FILE)
    if not acc:
        add(CRIT, "账本", "account.json", "不存在或损坏")
        return

    init = float((acc.get("meta") or {}).get("init_capital") or 10000.0)
    cash = float(acc.get("cash") or 0)
    positions = acc.get("positions") or {}
    rows = _read_trades()

    # ---- 账本重放一致性 ----
    r_cash, r_pos, r_realized, probs = replay(rows, init)
    for lvl, msg in probs:
        add(lvl, "账本", "流水规则", msg)

    if abs(cash - r_cash) > 0.02:
        add(CRIT, "账本", "现金对不上",
            "account.json %.2f ≠ 流水重算 %.2f（差 %.2f）" % (cash, r_cash, cash - r_cash))
    else:
        add(OK, "账本", "现金", "%.2f，与流水重算一致" % cash)

    # ---- 核心恒等式 ----
    left, right, diff = ledger_identity(acc, r_cash, r_pos, r_realized)
    if abs(diff) > 0.02:
        add(CRIT, "账本", "资金恒等式",
            "现金+持仓成本 %.2f ≠ 本金+已实现 %.2f（差 %.2f）" % (left, right, diff))
    else:
        add(OK, "账本", "资金恒等式", "%.2f = 本金 %.0f + 已实现 %.2f" %
            (left, init, r_realized))

    # ---- 持仓结构与重放比对 ----
    bad_lot = []
    for code, pos in positions.items():
        lots = pos.get("lots") or []
        if not lots:
            bad_lot.append("%s 无批次" % code)
            continue
        for lot in lots:
            for k in ("date", "shares", "price"):
                if lot.get(k) in (None, "", 0) and k != "price":
                    bad_lot.append("%s 批次缺 %s" % (code, k))
            if float(lot.get("price") or 0) <= 0:
                bad_lot.append("%s 批次价格为 0" % code)
    if bad_lot:
        add(CRIT, "账本", "持仓结构", "；".join(bad_lot[:4]))
    else:
        add(OK, "账本", "持仓结构", "%d 个标的，批次完整" % len(positions))

    a_shares = {c: sum(l["shares"] for l in (p.get("lots") or []))
                for c, p in positions.items()}
    r_shares = {c: sum(l["shares"] for l in lots) for c, lots in r_pos.items()}
    mismatch = [c for c in set(a_shares) | set(r_shares)
                if a_shares.get(c, 0) != r_shares.get(c, 0)]
    if mismatch:
        add(CRIT, "账本", "持仓对不上",
            "与流水重放不一致：" + "、".join(mismatch[:4]))
    elif positions:
        add(OK, "账本", "持仓数量", "与流水重放一致")

    # ---- 硬约束：单标的 ----
    if len(positions) > 1:
        add(CRIT, "账本", "持仓上限",
            "同时持有 %d 只（策略规定 1 只）：%s" % (len(positions), "、".join(positions)))
    elif positions:
        add(OK, "账本", "持仓上限", "1 / 1")

    # ---- 过夜仓 ----
    today = datetime.now().strftime("%Y-%m-%d")
    stale = [(c, l.get("date")) for c, p in positions.items()
             for l in (p.get("lots") or []) if l.get("date") != today]
    if stale and datetime.now().hour >= 15:
        add(WARN, "账本", "过夜仓",
            "收盘后仍有前一日建仓：%s（日内策略应清空）" %
            "、".join("%s@%s" % x for x in stale[:3]))

    # ---- 净值表 ----
    eqf = broker.EQUITY_FILE
    if os.path.exists(eqf):
        with open(eqf, "r", encoding="utf-8-sig", newline="") as f:
            er = [r for r in csv.DictReader(f) if r.get("date")]
        dates = [r["date"] for r in er]
        if len(dates) != len(set(dates)):
            dup = [d for d in set(dates) if dates.count(d) > 1]
            add(CRIT, "账本", "净值表", "重复日期：%s" % "、".join(sorted(dup)[:3]))
        elif dates != sorted(dates):
            add(WARN, "账本", "净值表", "日期未按升序排列")
        else:
            add(OK, "账本", "净值表", "%d 个交易日" % len(dates))
    else:
        add(WARN, "账本", "净值表", "equity.csv 不存在")


# ==========================================================================
# C 代码健康
# ==========================================================================
def check_py_compile():
    bad = []
    for fn in sorted(os.listdir(ENGINE)):
        if not fn.endswith(".py"):
            continue
        p = os.path.join(ENGINE, fn)
        r = subprocess.run([sys.executable, "-m", "py_compile", p],
                           capture_output=True)
        if r.returncode != 0:
            bad.append(fn)
    if bad:
        add(CRIT, "代码", "语法", "编译失败：" + "、".join(bad))
    else:
        n = len([f for f in os.listdir(ENGINE) if f.endswith(".py")])
        add(OK, "代码", "语法", "%d 个模块全部编译通过" % n)


def check_js():
    if not os.path.exists(PAGE):
        return
    with open(PAGE, "r", encoding="utf-8") as f:
        html = f.read()
    blocks, i = [], 0
    while True:
        a = html.find("<script>", i)
        if a < 0:
            break
        b = html.find("</script>", a)
        if b < 0:
            break
        blocks.append(html[a + 8:b])
        i = b + 9
    if not blocks:
        add(WARN, "代码", "内联脚本", "页面里没有 <script>")
        return

    node = None
    for cand in (r"C:\Program Files\nodejs\node.exe", "node"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True)
            node = cand
            break
        except Exception:  # noqa: BLE001
            continue
    if not node:
        add(WARN, "代码", "内联脚本", "%d 段，未找到 node，跳过语法校验" % len(blocks))
        return

    tmp = os.path.join(STATE, "_jscheck.js")
    bad = 0
    try:
        for k, code in enumerate(blocks):
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(code)
            r = subprocess.run([node, "--check", tmp], capture_output=True)
            if r.returncode != 0:
                bad += 1
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if bad:
        add(CRIT, "代码", "内联脚本", "%d/%d 段语法错误" % (bad, len(blocks)))
    else:
        add(OK, "代码", "内联脚本", "%d 段语法通过" % len(blocks))


def _bat_problem(path):
    """返回 .bat 的问题类型（"LF" / "非ASCII" / None）。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    if b"\n" in raw.replace(b"\r\n", b""):
        return "LF"
    if any(c > 127 for c in raw):
        return "非ASCII"
    return None


def _fix_bat(path):
    """就地修 .bat：LF -> CRLF，非 ASCII 字节降级成 '?'。

    为什么 doctor 要自己动手：make_launcher 只重建它列表里那几个脚本，
    用户新加的 .bat 它根本不碰。以前这里跑完 make_launcher 就直接报
    "已重写"，属于谎报——文件其实没动，下次体检照样报，用户还以为修过了。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return False
    fixed = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    fixed = bytes((c if c < 128 else 63) for c in fixed)     # 63 = '?'
    if fixed == raw:
        return False
    try:
        with open(path, "wb") as f:
            f.write(fixed)
        return True
    except OSError:
        return False


def check_bats():
    bad = []
    bats = [f for f in os.listdir(ROOT) if f.lower().endswith((".bat", ".cmd"))]
    for fn in bats:
        p = _bat_problem(os.path.join(ROOT, fn))
        if p:
            bad.append(fn + "(%s)" % p)
    if not bad:
        add(OK, "代码", "启动脚本", "%d 个 .bat 全部 CRLF+ASCII" % len(bats))
        return
    if not DO_FIX:
        add(CRIT, "代码", "启动脚本", "编码异常：" + "、".join(bad))
        return

    # 先让 make_launcher 重建它自己维护的脚本（保持内容与模板一致）
    subprocess.run([sys.executable, os.path.join(ENGINE, "make_launcher.py")],
                   capture_output=True)
    # 再复查一遍，剩下的（通常就是用户新加的）自己动手修
    still = []
    for fn in bats:
        path = os.path.join(ROOT, fn)
        if _bat_problem(path):
            _fix_bat(path)
            if _bat_problem(path):
                still.append(fn)
    if still:
        add(CRIT, "代码", "启动脚本", "编码异常且无法自动修复：" + "、".join(still))
    else:
        add(FIX, "代码", "启动脚本", "编码异常已重写：" + "、".join(bad[:4]))


def check_deps():
    """不允许第三方依赖：换机器要能直接跑。"""
    std = set(getattr(sys, "stdlib_module_names", ()))
    local = {f[:-3] for f in os.listdir(ENGINE) if f.endswith(".py")}
    third = set()
    for fn in os.listdir(ENGINE):
        if not fn.endswith(".py"):
            continue
        try:
            tree = ast.parse(open(os.path.join(ENGINE, fn), encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    m = a.name.split(".")[0]
                    if m not in std and m not in local:
                        third.add(m)
            elif isinstance(node, ast.ImportFrom) and node.module:
                m = node.module.split(".")[0]
                if m not in std and m not in local:
                    third.add(m)
    if third:
        add(WARN, "代码", "外部依赖", "用到非标准库：" + "、".join(sorted(third)))
    else:
        add(OK, "代码", "外部依赖", "零第三方依赖")


# ==========================================================================
# D 数据
# ==========================================================================
def check_cache():
    if not os.path.isdir(CACHE):
        add(WARN, "数据", "缓存", "cache 目录不存在")
        return
    files = [f for f in os.listdir(CACHE) if f.startswith("trends_")]
    codes = {f.split("_")[1] for f in files if len(f.split("_")) > 2}
    if not files:
        add(WARN, "数据", "缓存", "无分时缓存")
        return

    bad_t, bad_px = 0, 0
    for f in files:
        d = _read_json(os.path.join(CACHE, f), {})
        ticks = ((d or {}).get("data") or {}).get("ticks") or []
        if not ticks:
            continue
        ts = [t.get("t") for t in ticks]
        if ts != sorted(ts):
            bad_t += 1
        for t in ticks:
            if not t.get("price") or float(t["price"]) <= 0:
                bad_px += 1
                break
            if t.get("avg") is not None and float(t["avg"]) <= 0:
                bad_px += 1
                break
    if bad_t or bad_px:
        add(CRIT, "数据", "分时完整性",
            "时间乱序 %d 个文件，价格异常 %d 个文件" % (bad_t, bad_px))
    else:
        add(OK, "数据", "分时完整性", "%d 个文件，时间有序/价格为正" % len(files))

    add(OK if len(codes) >= 10 else WARN, "数据", "缓存覆盖",
        "%d 只标的 / %d 个文件" % (len(codes), len(files)))


def check_consistency_deep():
    """报价 vs 分时末笔：两套数据源互相校验。"""
    try:
        import dashboard
    except Exception:  # noqa: BLE001
        add(WARN, "数据", "交叉验证", "无法导入 dashboard")
        return
    try:
        pool, src = dashboard.pool_status(total_budget=15)
    except Exception as e:  # noqa: BLE001
        add(WARN, "数据", "交叉验证", "取数失败 %s" % str(e)[:50])
        return
    bad = []
    for r in pool:
        if r.get("vwap") is None or not r.get("price"):
            continue
        # 分时均价与现价差 20% 以上，必然是数据错位
        gap = abs(r["price"] / r["vwap"] - 1)
        if gap > 0.20:
            bad.append("%s 现价 %.3f / 均价 %.3f" % (r["code"], r["price"], r["vwap"]))
    if bad:
        add(CRIT, "数据", "交叉验证", "现价与均价严重偏离：" + "；".join(bad[:3]))
    else:
        add(OK, "数据", "交叉验证", "%d 只标的现价与均价一致" % len(pool))


# ==========================================================================
# E 回归
# ==========================================================================
def check_selftest():
    """跑端到端自测：合成行情驱动券商/执行/策略。

    这一步不可省。前面所有账本检查都建立在真实流水上，
    而真实流水经常是空的——空账本上什么都通过。
    自测在临时目录里造极端情况，不依赖真实数据有没有成交。
    """
    p = os.path.join(ENGINE, "selftest.py")
    if not os.path.exists(p):
        add(WARN, "回归", "端到端自测", "selftest.py 不存在")
        return
    try:
        r = subprocess.run([sys.executable, p], capture_output=True, timeout=120)
        out = (r.stdout or b"").decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        add(CRIT, "回归", "端到端自测", "超时（120s），可能死循环")
        return
    if r.returncode == 0:
        n = out.count("[PASS]")
        add(OK, "回归", "端到端自测", "%d 项全部通过" % n)
    else:
        fails = [ln.strip()[7:] for ln in out.splitlines() if ln.strip().startswith("[FAIL]")]
        detail = "；".join(fails[:3]) if fails else "见 selftest 输出"
        add(CRIT, "回归", "端到端自测", "%d 项失败：%s" % (max(len(fails), 1), detail))


def check_backtest_baseline():
    """同一份缓存、同一套参数，回测结果每次必须一样。

    这是防「改了无关代码，策略行为悄悄变了」的最后一道闸。
    """
    base_f = os.path.join(STATE, "baseline.json")
    try:
        import backtest
        import strategy
    except Exception:  # noqa: BLE001
        add(WARN, "回归", "回测基线", "无法导入 backtest")
        return
    try:
        data = backtest.load_pool(strategy.CORE_POOL, ndays=5,
                                  verbose=False, offline=True)
        if not data:
            add(WARN, "回归", "回测基线", "本地缓存不足，跳过")
            return
        days = [d for d in backtest.trading_days(data)
                if d != datetime.now().strftime("%Y-%m-%d")]
        if not days:
            add(WARN, "回归", "回测基线", "无完整交易日")
            return
        res = backtest.run_backtest(data, dict(strategy.PARAMS), days)
        m = backtest.metrics(res)
        ret = round(float(m.get("ret_pct", 0)), 4)
        n = int(m.get("trades", 0))
    except Exception as e:  # noqa: BLE001
        add(WARN, "回归", "回测基线", "回测失败 %s" % str(e)[:60])
        return

    base = _read_json(base_f)
    if not base:
        with open(base_f, "w", encoding="utf-8") as f:
            json.dump({"ret_pct": ret, "trades": n,
                       "days": days, "saved": datetime.now().strftime("%Y-%m-%d %H:%M")},
                      f, ensure_ascii=False, indent=2)
        add(OK, "回归", "回测基线", "首次建立基线 %.2f%% / %d 笔" % (ret, n))
        return

    same_days = base.get("days") == days
    drift = abs(ret - float(base.get("ret_pct", 0)))
    if not same_days:
        # 缓存按天滚动，days 变了是常态，不是故障。但「换了数据集」和
        # 「同一份数据结果漂移」必须分开说——旧逻辑会在这时报「与基线一致」，
        # 那是假话：数据完全不同，根本没有可比性。
        # 这里明确重设基线，并说清楚是按哪批数据重设的。
        try:
            with open(base_f, "w", encoding="utf-8") as f:
                json.dump({"ret_pct": ret, "trades": n, "days": days,
                           "saved": datetime.now().strftime("%Y-%m-%d %H:%M")},
                          f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        add(OK, "回归", "回测基线",
            "数据集滚动（%d 日 → %d 日），已按新数据集重设基线 %.2f%% / %d 笔"
            % (len(base.get("days") or []), len(days), ret, n))
        return
    if drift > 0.05 or n != base.get("trades"):
        add(WARN, "回归", "回测基线",
            "结果漂移：%.2f%%→%.2f%%，%d→%d 笔（同一份数据同一套参数）"
            % (float(base.get("ret_pct", 0)), ret, base.get("trades", 0), n))
    else:
        add(OK, "回归", "回测基线", "%.2f%% / %d 笔，与基线一致" % (ret, n))


# ==========================================================================
# 汇总
# ==========================================================================
def report(quiet=False):
    order = {CRIT: 0, WARN: 1, FIX: 2, OK: 3}
    rows = sorted(RESULTS, key=lambda r: (order[r["level"]], r["area"]))
    width_a = max([len(r["area"]) for r in rows] + [4])
    width_i = max([len(r["item"]) for r in rows] + [4])

    if not quiet:
        print("=" * 74)
        print("  自动体检   %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        print("=" * 74)
    for r in rows:
        if quiet and r["level"] in (OK,):
            continue
        tag = {"OK": " OK  ", "FIX": " FIX ", "WARN": "WARN ", "CRIT": "CRIT "}[r["level"]]
        print("  [%s] %-*s %-*s %s" % (tag, width_a, r["area"],
                                       width_i, r["item"], r["msg"]))

    n_crit = sum(1 for r in RESULTS if r["level"] == CRIT)
    n_warn = sum(1 for r in RESULTS if r["level"] == WARN)
    n_fix = sum(1 for r in RESULTS if r["level"] == FIX)
    n_ok = sum(1 for r in RESULTS if r["level"] == OK)
    print("-" * 74)
    print("  通过 %d · 已修 %d · 待看 %d · 严重 %d" % (n_ok, n_fix, n_warn, n_crit))
    if n_crit:
        print("  结论：有 %d 项严重问题需要处理（脚本不会擅自改账本）" % n_crit)
    elif n_warn:
        print("  结论：可用，但有 %d 项值得看一眼" % n_warn)
    else:
        print("  结论：全部不变量成立")
    print("=" * 74)
    return n_crit


def save(n_crit):
    out = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "crit": n_crit, "items": RESULTS}
    try:
        with open(os.path.join(STATE, "doctor.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

    bad = [r for r in RESULTS if r["level"] in (CRIT, WARN)]
    if bad:
        os.makedirs(JOURNAL, exist_ok=True)
        p = os.path.join(JOURNAL, "doctor-%s.md" % datetime.now().strftime("%Y-%m-%d"))
        new = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if new:
                f.write("# 自动体检记录 %s\n" % datetime.now().strftime("%Y-%m-%d"))
            f.write("\n## %s\n\n" % datetime.now().strftime("%H:%M"))
            for r in bad:
                f.write("- **[%s] %s / %s** — %s\n" % (r["level"], r["area"],
                                                       r["item"], r["msg"]))
            f.write("\n")


def main():
    global DO_FIX
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fix", action="store_true", help="只体检不修")
    ap.add_argument("--deep", action="store_true", help="加联网交叉验证")
    ap.add_argument("--quiet", action="store_true", help="只打印非 OK 项")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--no-backtest", action="store_true", help="跳过回测基线")
    a = ap.parse_args()
    DO_FIX = not a.no_fix

    # 先验证检查器本身：真实账本经常是空的，空账本上的「通过」没有意义
    fails = selftest_replay()
    if fails:
        add(CRIT, "自检", "检查器", "自测失败：" + "；".join(fails[:3]))
    else:
        add(OK, "自检", "检查器", "篡改样本能被抓到，正确样本不误报")

    port = check_service()
    check_page(port)
    check_heartbeat()
    check_ledger()
    check_py_compile()
    check_js()
    check_bats()
    check_deps()
    check_cache()
    if a.deep:
        check_consistency_deep()
    if not a.no_backtest:
        check_backtest_baseline()
    if not a.no_backtest:
        check_selftest()

    n_crit = report(a.quiet)
    save(n_crit)

    if a.json:
        print(json.dumps({"crit": n_crit, "items": RESULTS}, ensure_ascii=False))
    return 1 if n_crit else 0


if __name__ == "__main__":
    sys.exit(main())
