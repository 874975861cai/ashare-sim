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
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta

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


def _read_json_state(p):
    """读 JSON，并把「没有」和「读不出来」分开：("missing"/"corrupt"/"ok", 值)。

    _read_json 两种都返回 None，调用方于是把「首次」当成「没坏」。
    账本类文件混了这两种还有 CRIT 兜着（"不存在或损坏"）；基线文件混起来更糟：
    一个写坏的基线会被报成「首次建立基线」——绿的。真正的代码退化就这么
    被一行 OK 盖过去了，而且顺手把旧基线覆盖掉，连证据都不剩。
    """
    if not os.path.exists(p):
        return "missing", None
    try:
        with open(p, "r", encoding="utf-8-sig") as f:   # 容忍 BOM
            val = json.load(f)
    except Exception:  # noqa: BLE001
        return "corrupt", None
    if not isinstance(val, dict):
        return "corrupt", None
    return "ok", val


def _save_baseline(path, payload):
    """写回归基线，返回 "written" / "unchanged" / "error:<原因>"。

    两条要求都是被事故逼出来的：

    1) **原子写**（临时文件 + os.replace）。2026-11-10 早上 state/baseline.json
       变成了 489 个 NUL 字节：直接 open(w) 覆盖目标文件，进程在写盘中途被杀
       或者机器异常掉电，NTFS 会留下一个「大小对、内容全零」的文件。
       最坏情况应该是「这轮没写进去」，而不是「写坏」。

    2) **内容没变就不写**。saved 时间戳每轮都不同，旧写法每跑一次 doctor 就把
       工作区弄脏一次：一天三次巡检都得先 stash 再 pop，而 stash 正要搬动这个
       正在被改写的文件。少写一次就少一次踩雷的机会。
    """
    st, old = _read_json_state(path)
    if st == "ok" and {k: v for k, v in old.items() if k != "saved"} == payload:
        return "unchanged"
    rec = dict(payload)
    rec["saved"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        broker._atomic_write_text(
            path, json.dumps(rec, ensure_ascii=False, indent=2))
    except OSError as e:
        # 写不进去必须喊：调用方接下来都会打印「已按…重设基线」，
        # 而实际上什么都没记住 —— 下一轮仍拿旧基线比，
        # 那时的「一致/漂移」是在和一个已经不存在的基准对话。
        add(WARN, "回归", "回测基线",
            "基线写入失败（%s）：本轮结果没有被记住，下一轮仍旧拿旧基线比" % e)
        return "error:%s" % e
    return "written"


def _read_trades():
    p = broker.TRADE_FILE
    if not os.path.exists(p):
        return []
    with open(p, "r", encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("seq")]


def _git(*args, timeout=30):
    """跑一条 git 命令。失败一律返回字符串，不抛异常——体检不能被网络搞崩。"""
    try:
        r = subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", str(e)


def sync_decision(behind, ahead, conflicts):
    """决定「本地副本要不要从云端拉一次」。

    纯函数，方便自测。返回 (动作, 说明)，动作为：
        skip   本地已最新，什么都不用做
        pull   可以快进合并
        manual 分叉 / 会覆盖本地未提交改动，交给人判断
    """
    if behind <= 0:
        return "skip", "本地已是最新"
    if ahead > 0:
        return "manual", "本地有 %d 个未推送提交，与云端分叉，不自动合并" % ahead
    if conflicts:
        return "manual", "本地未提交改动会与云端冲突：%s" % "、".join(sorted(conflicts)[:3])
    return "pull", "本地副本落后云端 %d 个提交" % behind


# ==========================================================================
# 账本重放：纯函数，方便自测
# ==========================================================================
def replay(rows, init=10000.0, flows=None):
    """按流水重算现金/持仓/已实现盈亏，并收集规则违规。

    必须用**和 broker 完全相同的算法**：先进先出、按批次核销、
    卖出费用按份额摊到每个批次。用平均成本法重算会和券商对不上，
    然后检查器自己就是错的——这种「看起来对」的检查比没有更危险。

    flows: {日期: 入金金额}。2026-11-09 追加 9 万把本金从 1 万加到 10 万，
    没有这一参数，`init` 里多出的 9 万会让**历史上每一笔**成交的
    「现金链」都少 9 万，11 条 CRIT 全是假警报（已实测）。入金必须按
    日期插入，而不是一次性塞进起点。

    返回 (cash, positions, realized, problems)
    positions: {code: [{"shares","price","fee"}]}，未被核销的批次
    problems : [(级别, 描述)]
    """
    flows = dict(flows or {})
    # init 是「累计投入本金」，含全部入金；从流水起点回算时要先扣掉，
    # 再按各自日期加回去。
    cash = float(init) - sum(flows.values())
    pending = sorted(flows.items())
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

        # ---- 入金/出金按日期入账 ----
        # 必须插在成交结算之前：同一天先入金再买入是常态，顺序反了
        # 那一笔的 cash_after 就会差一个入金额。
        if d:
            while pending and pending[0][0] <= d:
                cash += pending[0][1]
                pending.pop(0)

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

    # 流水之后才发生的入金：还没有成交跟它配对，也要计入现金
    for _d, amt in pending:
        cash += amt

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
    fails += selftest_sync()
    fails += selftest_overnight()
    fails += selftest_watch_window()
    return fails


def selftest_sync():
    """同步决策是纯逻辑，最容易写反（例如把「分叉」当成能快进）。
    这里把四种情形都钉住，防止哪天改坏了变成自动覆盖别人的提交。"""
    fails = []
    cases = [
        ((0, 0, ()), "skip", "本地已最新"),
        ((3, 0, ()), "pull", "落后 3 个提交且无冲突，应允许快进"),
        ((3, 1, ()), "manual", "本地有未推送提交，不能自动合并"),
        ((3, 0, ("state/equity.csv",)), "manual", "会覆盖本地未提交改动，不能自动合并"),
    ]
    for (args, want, why) in cases:
        got, _ = sync_decision(*args)
        if got != want:
            fails.append("同步决策错误（%s）：得到 %s，应为 %s" % (why, got, want))
    return fails


def selftest_overnight():
    """过夜仓判定必须跟着策略模式走。

    2026-10-28 切持有型后，旧的日内判定每天收盘都对一笔正常持仓报 WARN
    （2026-10-30 复现）。这里把四种情形钉死：既防回退成天天误报，
    也防有人图省事改成「有仓一律不报」。
    """
    fails = []
    one = {"510880": {"lots": [{"date": "2026-10-29", "shares": 2900, "price": 3.4}]}}
    mix3 = ("159915", "513100", "518880")
    three = {"513100": {"lots": [{"date": "2026-11-09", "shares": 22100}]},
             "518880": {"lots": [{"date": "2026-11-09", "shares": 8200}]},
             "159915": {"lots": [{"date": "2026-11-09", "shares": 12400}]}}
    cases = [
        (one, "2026-10-30", 16, "hold", "510880", (), OK,
         "持有型过夜属设计内，应以 OK 说明，不该报警"),
        (one, "2026-10-30", 16, "daytrade", "", (), WARN,
         "日内策略过夜必须报警"),
        (one, "2026-10-30", 10, "daytrade", "", (), None,
         "收盘前持仓是当天建的，不算过夜"),
        ({"510300": {"lots": [{"date": "2026-10-29"}]}}, "2026-10-30", 16,
         "hold", "510880", (), WARN,
         "持有型却持有非目标标的，必须报警"),
        ({}, "2026-10-30", 16, "hold", "510880", (), None, "空仓不该报警"),
        # 组合模式（2026-11-07 转 mix 后补）：三只标的过夜是设计，不该天天报警；
        # 但组合外的旧持仓必须报——那是「移出组合」没执行成功。
        (three, "2026-11-10", 16, "mix", "", mix3, OK,
         "组合持仓过夜属设计内，不该按日内口径报警"),
        (one, "2026-11-10", 16, "mix", "", mix3, WARN,
         "组合里却持有 510880，说明移出组合没成功，必须报警"),
    ]
    for positions, d, h, mode, tgt, mix, want, why in cases:
        got, _ = overnight_judgment(positions, d, h, mode, tgt, mix)
        if got != want:
            fails.append("过夜仓判定错误（%s）：得到 %s，应为 %s" % (why, got, want))
    return fails


def selftest_watch_window():
    """对账窗口的边界。

    上界从 15:30 放宽到 16:30 是为了补上收盘后那段盲区（2026-10-30 踩过），
    但放宽过头会让晚上十点还在报「云端没跑」，同样会把真问题淹掉。
    """
    fails = []
    cases = [
        (4, "09:59", False, "开盘前太早，cron 抖动还没落地"),
        (4, "10:00", True, "10:00 起该问了"),
        (4, "15:30", True, "收盘前后都要盯着"),
        (4, "16:30", True, "收盘后仍在对账窗口内"),
        (4, "16:31", False, "太晚，别在夜里刷存在感"),
        (5, "10:00", False, "周六不判"),
        (6, "15:00", False, "周日不判"),
    ]
    for wd, hm, want, why in cases:
        if ledger_watch_active(wd, hm) != want:
            fails.append("对账窗口错误（%s）：%s@%s" % (why, wd, hm))
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

    # 不再自动拉起本地服务。
    # 2026-10-21 起拓扑变了：账本主人是 GitHub Actions，看板由云端维护，
    # 本地只是一份只读镜像。在这里拉起 serve.py 有三个坏处——它会占端口、
    # 往 dashboard/ 写文件、并且让「本地没在跑」这个正常状态每轮被自动抹掉，
    # 于是没人知道本地其实一直在空转。要开本地页面请手动双击 打开模拟盘.bat。
    add(OK, "运行时", "实时服务",
        "本地未运行（看板由云端维护，属正常；需要本地页面请跑 打开模拟盘.bat）")
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
    if age is not None and age > limit and port is None:
        # 本地服务没起时，这份页面本来就只是静态快照（看板以云端为准），
        # 老化不是缺陷。顺手重写一份，用户偶尔双击打开时别看到太旧的数；
        # 重写失败也不报警——这个文件不影响任何账务判断。
        if DO_FIX:
            _regen_page()
        add(OK, "运行时", "页面新鲜度",
            "静态快照（%.0f 小时前），本地服务未启用，属正常" % (age / 3600))
    elif age is not None and age > limit:
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


def _ledger_last_day():
    """账本里最后一行净值是哪一天（读文件内容，不受本地副本新旧影响）。"""
    try:
        with open(broker.EQUITY_FILE, "r", encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.DictReader(f) if r.get("date")]
        return rows[-1]["date"] if rows else ""
    except (OSError, csv.Error):
        return ""


def _sync_local_ledger():
    """本地只是账本的一个副本，云端跑完才会提交。

    所以「盘中缺今日净值行」有两种可能：云端真的没跑，或者云端跑了、
    本地没拉。分不清就会天天误报「云端自动化没跑」——2026-10-29 就踩过。
    这里先 fetch，再按 sync_decision 决定能不能快进合并。
    返回 (是否已同步, 补充说明)。
    """
    if not DO_FIX:
        return False, "；--no-fix，未尝试同步本地副本"
    rc, _, _ = _git("rev-parse", "--is-inside-work-tree")
    if rc != 0:
        return False, "；不是 git 工作区，无法判断云端是否记账"

    rc, _, err = _git("fetch", "--quiet", "origin", "main", timeout=45)
    if rc != 0:
        first = (err.splitlines() or ["网络异常"])[0][:60]
        return False, "；拉取云端失败：%s" % first

    _, behind_s, _ = _git("rev-list", "--count", "HEAD..origin/main")
    _, ahead_s, _ = _git("rev-list", "--count", "origin/main..HEAD")
    try:
        behind, ahead = int(behind_s or 0), int(ahead_s or 0)
    except ValueError:
        return False, "；无法解析与远端的差异"

    # 只有「合并会覆盖到的、本地又改过的文件」才算冲突
    _, dirty_s, _ = _git("diff", "--name-only")
    _, incoming_s, _ = _git("diff", "--name-only", "HEAD", "origin/main")
    dirty = set(x for x in dirty_s.splitlines() if x.strip())
    incoming = set(x for x in incoming_s.splitlines() if x.strip())

    # 工作区字节已经等于云端那份的，对齐它不会丢任何东西，先放行
    same = set()
    for p in sorted(dirty & incoming):
        rc, _, _ = _git("diff", "--quiet", "origin/main", "--", p)
        if rc == 0:
            same.add(p)
    conflict, align = split_conflict(dirty, incoming, same)

    action, msg = sync_decision(behind, ahead, conflict)
    if action == "skip":
        return False, "；云端也没有今日提交（本地已同步到最新）"
    if action == "manual":
        return False, "；云端已提交 %d 个版本待同步，但%s" % (behind, msg)

    # 到这一步确定要快进合并了：把「本来就等于云端那份」的脏文件对齐，
    # 否则 git 会以「本地改动会被覆盖」为由拒绝合并。
    if align:
        rc, _, err = _git("checkout", "origin/main", "--", *sorted(align))
        if rc != 0:
            first = (err.splitlines() or ["未知错误"])[0][:60]
            return False, "；本地改动与云端一致，可以对齐，但 checkout 失败：%s" % first

    rc, _, err = _git("merge", "--ff-only", "origin/main")
    if rc != 0:
        first = (err.splitlines() or ["未知错误"])[0][:60]
        return False, "；云端有 %d 个提交待同步，快进合并失败：%s" % (behind, first)
    return True, "%s，现已快进合并" % msg


def _num(s):
    """把 "¥9,919.50" 这类展示串还原成 float；失败返回 None。"""
    try:
        return float(str(s).replace("¥", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def split_conflict(dirty, incoming, same_as_remote):
    """把「本地改过、快进合并又会覆盖」的文件分成两类。

    为什么要分：本地推了代码但 pull 不下来（github.com 不通、api.github.com 通）
    时会留下一种状态——工作区内容和云端那份**逐字节相同**，但相对 HEAD 是脏的。
    旧的冲突判定只看「脏 ∩ 待合入」，于是把它当成必须人工处理的冲突，明明没有
    任何本地改动会丢，却永远报「云端有提交待同步，但本地有改动」（2026-11-04）。
    这里把「已等于云端那份」的挑出来：对这些文件 checkout 到 origin/main 的版本
    不改变任何字节，因此可以直接对齐；剩下的才是真冲突。
    """
    both = set(dirty) & set(incoming)
    align = {p for p in both if p in set(same_as_remote)}
    return both - align, align


def check_mirror():
    """本地只是只读镜像：它「看起来没成交」，不等于云端没成交。

    2026-10-29 踩过这个坑：github.com:443 一整天反复超时，`git fetch` 拉不动，
    本地 state/account.json 停在「空仓 / cash 9919.50」，于是 doctor 17 项全绿；
    而同一时刻线上看板是 n_pos=1 / cash 54.50 —— 云端早就把 510880 买进去了。
    差一点把「本地镜像过期」写成「云端自动化没跑」。

    /api/state 是本机唯一一条不依赖 github 的真实来源，用它做交叉核对：
    cash / 持仓数 / 已平笔数三项都对上，才算「镜像与线上一致」。
    对不上时不能说「账本错了」——账本在云端，错的只可能是本地这份副本。
    """
    try:
        import urllib.request
        import push_cloud
    except Exception:  # noqa: BLE001
        add(WARN, "账本", "镜像同步", "无法导入 push_cloud，跳过镜像核对")
        return

    now = datetime.now()
    try:
        with urllib.request.urlopen(push_cloud.URL + "/api/state", timeout=6) as r:
            st = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        # 交易时段的「核对不了」本身就是风险：这段时间正是需要知道
        # 本地这份账是不是今天的时候。非交易时段不吵。
        hm = now.strftime("%H:%M")
        if now.weekday() < 5 and "09:00" <= hm <= "16:00":
            add(WARN, "账本", "镜像同步",
                "线上看板不可达（%s），无法确认本地镜像是不是最新的"
                % type(e).__name__)
        else:
            add(OK, "账本", "镜像同步", "线上看板不可达，非交易时段不核对")
        return

    import broker
    acc = broker.load_account()
    local_cash = float(acc.get("cash") or 0)
    local_pos = len(acc.get("positions") or {})
    local_sell = len([r for r in _read_trades() if r.get("side") == "SELL"])

    r_cash = _num(st.get("cash"))
    r_pos = st.get("n_pos")
    r_sell = st.get("n_trades")
    if r_cash is None or r_pos is None or r_sell is None:
        add(WARN, "账本", "镜像同步", "线上看板返回字段不全，无法核对")
        return

    ok = (abs(r_cash - local_cash) <= 0.011 and int(r_pos) == local_pos
          and int(r_sell) == local_sell)
    if ok:
        add(OK, "账本", "镜像同步",
            "与线上看板一致（cash %.2f / 持仓 %d / 已平 %d 笔）"
            % (r_cash, int(r_pos), int(r_sell)))
        return

    add(WARN, "账本", "镜像同步",
        "本地镜像与线上不一致：线上 cash %.2f / 持仓 %d / 已平 %d 笔，"
        "本地 cash %.2f / 持仓 %d / 已平 %d 笔。账本在云端，本地这份只是副本，"
        "先同步再复盘"
        % (r_cash, int(r_pos), int(r_sell), local_cash, local_pos, local_sell))


def ledger_watch_active(weekday, hm):
    """现在该不该拿「今天的净值行」去追究自动化。纯函数，方便自测。

    weekday 用 datetime.weekday()（周一=0）；hm 是 "HH:MM"。

    下界 10:00：云端 cron 有 5-20 分钟抖动，太早问等于催。
    上界 16:30：收盘后才是对账的时刻，15:30 就撒手会把「本地落后」漏成绿灯。
    """
    if weekday >= 5:
        return False
    return "10:00" <= hm <= "16:30"


def cloud_run_evidence(runs, today):
    """从 Actions 运行记录里数出「今天云端到底跑了几轮」。

    纯函数，方便自测。runs 是 api.github.com 的 workflow_runs 列表。
    只认 completed + success：排队中/失败的一轮证明不了账本被写对。
    返回 (轮数, 最后一轮的北京时间 "HH:MM")。
    """
    n, last = 0, ""
    for w in runs or []:
        created = str(w.get("created_at") or "")
        if len(created) < 19:
            continue
        try:
            dt = datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        cst = dt + timedelta(hours=8)
        if cst.strftime("%Y-%m-%d") != today:
            continue
        if str(w.get("status")) != "completed" or str(w.get("conclusion")) != "success":
            continue
        n += 1
        hm = cst.strftime("%H:%M")
        if hm > last:
            last = hm
    return n, last


def _actions_runs():
    """拉云端最近若干轮 Actions 记录。返回 (runs, 失败原因)。

    为什么走 api.github.com 而不是 git fetch：两者是不同主机。2026-11-03
    实测 github.com:443 连不上（git fetch 全挂），api.github.com 却正常——
    正是需要判断「云端跑没跑」的那天，走 git 会一无所获。
    未认证调用限额 60 次/小时，体检一天跑几次，够用。
    """
    rc, url, _ = _git("remote", "get-url", "origin")
    if rc != 0 or not url:
        return None, "读不到 origin 地址"
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?$", url.strip())
    if not m:
        return None, "origin 不是 github 仓库"
    api = ("https://api.github.com/repos/%s/%s/actions/runs?per_page=20"
           % (m.group(1), m.group(2)))
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "ashare-sim-doctor"})
        with urllib.request.urlopen(req, timeout=8) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        return None, "查 Actions 失败：%s" % type(e).__name__
    return (d.get("workflow_runs") or []), ""


def check_heartbeat():
    """判断「自动化还在跑吗」。

    有两个信号：本地 heartbeat.json 和账本里的净值行。
    heartbeat 只有本地 runner 会写，而且被 .gitignore 排除、从不上传，
    所以云端跑一百次也不会更新它——只盯它，会在本地停跑 40 小时后天天
    误报「自动化被关」，而云端其实一直在记账。2026-10-21 之后拓扑变了，
    账本才是唯一真相：净值表里有没有「今天」这一行，才是最硬的证据。
    """
    hb = _read_json(os.path.join(STATE, "heartbeat.json")) or {}
    last = str(hb.get("last_run") or "")
    hb_dt = None
    if last:
        try:
            hb_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            add(WARN, "运行时", "任务心跳", "时间格式异常 %s" % last)
            return

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    led_day = _ledger_last_day()
    note = ("；本地心跳停在 %s，本地不记账，属正常" % last) if last else ""

    if led_day == today:
        add(OK, "运行时", "任务心跳", "今日账本已写入%s" % note)
        return

    hm = now.strftime("%H:%M")
    # 收盘后也要判。15:30 就撒手的话，最该确认「今天到底定稿没有」的那半小时
    # 反而没人看：2026-10-30 收盘后本地副本落后云端两个提交、净值表停在
    # 10-29，体检照样全绿。云端最后一轮派工在 15:55 左右，窗口放到 16:30
    # 给提交留余量；再晚就没意义了，那时人已经不在电脑前。
    # 10:00 之前不判：云端 cron 有 5-20 分钟抖动，太早问等于催。
    active = ledger_watch_active(now.weekday(), hm)
    if active:
        # 缺今日净值行时，先分清「云端没跑」还是「本地没拉」，别冤枉云端
        synced, detail = _sync_local_ledger()
        if synced and _ledger_last_day() == today:
            add(FIX, "运行时", "任务心跳",
                "本地副本过期，已从云端同步并写入今日账本%s" % note)
            return
        seg = "盘中" if hm <= "15:30" else "收盘后"
        # 账本的提交策略是「有新成交才提交，无成交只在收盘后提交一次」
        # （见 trade.yml「账本入库存档」）。所以盘中缺今日净值行，多数时候
        # 只是「今天还没成交」，不是云端没跑——可这两种情况在本地长得一模
        # 一样。补一条不依赖账本自身的证据：Actions 运行记录。云端真跑过就
        # 说清楚，确实没跑才继续喊；否则每个无成交的早上都喊一遍「可能没跑」，
        # 真没跑那天反而没人信（2026-11-03 实测：09:32 云端跑成功过一轮，
        # 只因无成交未提交，体检照样报「可能没跑」）。
        runs, why = _actions_runs()
        n, last_t = cloud_run_evidence(runs, today)
        if n and hm < "15:30":
            add(OK, "运行时", "任务心跳",
                "云端今日已执行 %d 轮（最后 %s）；今日无成交，按提交策略盘中不入库，"
                "本地镜像没有今日净值行属正常%s" % (n, last_t, note))
            return
        if n:
            why = "；云端今日已执行 %d 轮（最后 %s），但账本没有今日净值行" % (n, last_t)
        elif why:
            why = "；%s" % why
        else:
            why = "；云端今日没有成功执行的记录"
        add(WARN, "运行时", "任务心跳",
            "%s但今天账本还没有净值行（最后 %s），云端自动化可能没跑%s%s"
            % (seg, led_day or "无", detail, why))
        return

    if hb_dt is not None:
        gap = (now - hb_dt).total_seconds() / 3600
        if gap <= 40:
            add(OK, "运行时", "任务心跳", "最后 %s" % last)
            return
    add(OK, "运行时", "任务心跳",
        "非盘中时段，不判漏跑（账本最后净值行 %s）%s" % (led_day or "无", note))


def _strategy_mode():
    """读当前策略模式。导入失败退回日内口径——宁可多报，不可漏报。"""
    try:
        import strategy
        return (str(strategy.PARAMS.get("mode") or "daytrade"),
                str(strategy.PARAMS.get("hold_target") or ""),
                tuple(sorted((strategy.PARAMS.get("mix_weights") or {}))))
    except Exception:  # noqa: BLE001
        return "daytrade", "", ()


def overnight_judgment(positions, today, hour, mode="daytrade", hold_target="",
                       mix_codes=()):
    """判断「收盘后还持有前一日的仓位」是故障还是设计。纯函数，方便自测。

    日内策略收盘必须清空，有仓就是执行问题；持有型（mode="hold"）本来
    就要过夜，这时再喊「违反日内平仓」等于把正常和故障揉成同一句话，
    喊多了真漏平仓那天反而没人看。持有型真正该报的是「持有非目标标的」，
    那说明账本和策略已经不一致。

    mode="mix"（2026-11-07 三次转向后）同理：三只组合标的本来就要过夜，
    旧口径会给每一天都报一次「日内策略应清空」。组合真正该报的是
    「持有组合外的标的」——那是 plan_mix 的「移出组合」没执行成功。
    """
    stale = sorted({(c, str(l.get("date"))) for c, p in (positions or {}).items()
                    for l in (p.get("lots") or []) if str(l.get("date")) != today})
    if not stale or hour < 15:          # 收盘前的仓是当天建的，不算过夜
        return None, ""
    shown = "、".join("%s@%s" % x for x in stale[:3])
    if str(mode) == "hold":
        off = sorted({c for c, _ in stale if hold_target and c != hold_target})
        if off:
            return WARN, "持有型目标 %s，却持有 %s（账本与策略口径不一致）" % (
                hold_target, "、".join(off))
        return OK, "%s 为持有型持仓（mode=hold），过夜属设计内" % shown
    if str(mode) == "mix":
        tgt = set(mix_codes or ())
        off = sorted({c for c, _ in stale if c not in tgt})
        if off:
            return WARN, ("组合外持仓 %s（目标组合 %s），应由 plan_mix 的「移出组合」"
                          "清掉；连续多日仍在说明清仓没执行成功"
                          % ("、".join(off), "/".join(mix_codes) or "未配置"))
        return OK, "%s 为组合持仓（mode=mix，共 %d 只），过夜属设计内" % (
            shown, len(tgt))
    return WARN, "收盘后仍有前一日建仓：%s（日内策略应清空）" % shown


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
    r_cash, r_pos, r_realized, probs = replay(rows, init, acc.get("flows"))
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
        # 本金必须按分打印：2026-11-07 迁移后本金是 100169.60，用 %.0f 会被显示成
        # 100170，这行"恒等式"就凭空差 0.40，等于让最该自证的一行算不平。
        add(OK, "账本", "资金恒等式", "%.2f = 本金 %.2f + 已实现 %.2f" %
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

    # ---- 持仓只数上限 ----
    # 口径跟着账本走：2026-11-07 起 10 万本金按 40/30/30 持三只，
    # 上限写在 meta.max_positions。写死 1 会让组合账户每天报 CRIT，
    # 和「过夜仓」那次同一个病：正常状态被报成违规，真出问题时没人看。
    cap = broker._max_positions(acc)
    if len(positions) > cap:
        add(CRIT, "账本", "持仓上限",
            "同时持有 %d 只（上限 %d 只）：%s"
            % (len(positions), cap, "、".join(positions)))
    elif positions:
        add(OK, "账本", "持仓上限", "%d / %d" % (len(positions), cap))

    # ---- 过夜仓 ----
    # 口径必须跟着策略走：2026-10-28 起 mode="hold"，过夜是设计目标。
    # 旧判定只认日内口径，于是每天收盘都对一笔正常持仓喊「应清空」——
    # runner._warn_overnight 早就区分 hold_mode 了，检查器却没跟上（2026-10-30 修）。
    mode, hold_target, mix_codes = _strategy_mode()
    now = datetime.now()
    lvl, msg = overnight_judgment(positions, now.strftime("%Y-%m-%d"),
                                  now.hour, mode, hold_target, mix_codes)
    if lvl:
        add(lvl, "账本", "过夜仓", msg)

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


def _params_sig(params):
    """策略参数指纹：用来区分「参数变了」和「代码行为变了」。"""
    blob = json.dumps([[k, params[k]] for k in sorted(params)],
                      ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:8]


# 影响回测结果的文件。改这几个以外的任何文件（渲染层、巡检脚本）都不算代码变更。
_BT_FILES = ("backtest.py", "strategy.py", "broker.py", "market.py")


def _code_sig():
    """回测相关代码的指纹：区分「数据集换了」和「代码换了」。"""
    h = hashlib.sha1()
    for name in _BT_FILES:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        try:
            with open(p, "rb") as f:
                h.update(name.encode("utf-8") + b"\0" + f.read() + b"\0")
        except OSError:
            h.update(name.encode("utf-8") + b"\0MISSING\0")
    return h.hexdigest()[:8]


def _check_dataset_window(data, rep, backtest, strategy):
    """回测数据集是不是「同一天写下的 5 日横截面」。

    为什么单独一条：backtest.trading_days() 取的是各标的日期的**并集**，
    各标的的缓存写在不同日期时，并集会拼出一个横跨数周、后段只剩一两只标的的
    假窗口；回测照跑、指标照算、基线照比，每一项都是绿的。2026-11-02 实测正是
    这种状态：13 只标的里 11 只停在 10-20、2 只到 10-30，并集 13 个交易日，
    其中 7 天只有 1~3 只标的参与。
    """
    h = backtest.dataset_health(data, ndays=5)
    deg = rep.get("degraded") or []
    dg_txt = ""
    if deg:
        ds = sorted({str(t)[:10] for _, t in deg})
        dg_txt = "%d/%d 只用旧缓存（截至 %s）" % (
            len(deg), rep.get("requested", len(strategy.CORE_POOL)),
            ds[0] if len(ds) == 1 else "%s~%s" % (ds[0], ds[-1]))
    if h["span"] > h["ndays"] or h["thin"]:
        thin_txt = "、".join("%s:%d只" % (d[5:], k) for d, k in h["thin"][:4])
        add(WARN, "数据", "回测数据集",
            "窗口是拼出来的：并集 %d 天（缓存应为 %d 天），%d 天不足半数标的（%s）%s%s"
            % (h["span"], h["ndays"], len(h["thin"]), thin_txt,
               "；" if dg_txt else "", dg_txt))
    elif dg_txt:
        add(WARN, "数据", "回测数据集",
            "%s；横截面 %d 只 × %d 天" % (dg_txt, h["codes"], h["span"]))
    else:
        add(OK, "数据", "回测数据集",
            "%d 只 × %d 天，同一天写入，横截面完整" % (h["codes"], h["span"]))


def universe_shift(old, new):
    """标的覆盖变化的方向：grew / shrank / changed / ""（无变化或无从比较）。

    为什么单独判（2026-11-08）：基线只记「交易日列表」，而缓存补上一只标的
    并不改变交易日列表。当天 warmup 补上 513100（13 只 → 14 只）后，探针从
    1.28% 变 -0.16%，days 一模一样、参数指纹一模一样、代码指纹也一模一样，
    于是报成「同一份数据、所以是代码行为变了」——那句话会把排查引向一个
    根本不存在的代码退化。数据变宽/变窄本身就是结果会变的理由，必须分开说。
    """
    a, b = set(old or ()), set(new or ())
    if not a or a == b:
        return ""
    if a < b:
        return "grew"
    if b < a:
        return "shrank"
    return "changed"


def check_backtest_baseline():
    """同一份缓存、同一套参数，回测结果每次必须一样。

    这是防「改了无关代码，策略行为悄悄变了」的最后一道闸。它有两处会骗过自己：

    1) 参数改了和代码坏了，报的是同一句话。只比 ret 时，「调了 dev_enter」
       会打印成「结果漂移（同一份数据同一套参数）」—— 后半句是假的。真漂移
       从此淹在假警报里，闸门等于没有。所以基线里存参数指纹，指纹变了就
       明确说「参数变更，已重设基线」。

    2) 2026-10-28 关闭入场后，回测恒为 0 笔 / 0.00%，此时「与基线一致」
       什么都没校验：出场逻辑改坏了照样是 0.00%。所以回归探针固定用
       entry_enabled=True 跑一遍，只比探针结果 —— 探针测的是代码，
       不是「今天想不想交易」。

    3) 数据集每天向后滚一格，days 一变就按当天结果重设基线。可「代码改了」
       和「数据换了」经常发生在同一晚：第二天早上新基线是用改过的代码跑出来的，
       退化就被当成新正常放行了，闸门在最需要它的那天失效。所以基线同时存
       代码指纹，滚动当天若代码也变过，就说「无法区分」，不假装是「一致」。
    """
    base_f = os.path.join(STATE, "baseline.json")
    try:
        import backtest
        import strategy
    except Exception:  # noqa: BLE001
        add(WARN, "回归", "回测基线", "无法导入 backtest")
        return
    try:
        rep = {}
        data = backtest.load_pool(strategy.CORE_POOL, ndays=5,
                                  verbose=False, offline=True, report=rep)
        if not data:
            add(WARN, "回归", "回测基线", "本地缓存不足，跳过")
            return
        # 数据集窗口体检（2026-11-02 补）。必须在这里做，因为只有这里手上已经
        # 有加载好的 data；而且要卡在「比基线」之前——基线本身可能是错的。
        _check_dataset_window(data, rep, backtest, strategy)
        days = [d for d in backtest.trading_days(data)
                if d != datetime.now().strftime("%Y-%m-%d")]
        if not days:
            add(WARN, "回归", "回测基线", "无完整交易日")
            return
        universe = sorted(data)
        live_p = dict(strategy.PARAMS)
        probe_p = dict(strategy.PARAMS)
        probe_p["entry_enabled"] = True
        mode = str(strategy.PARAMS.get("mode") or "daytrade")
        # 探针口径跟着策略模式走：mix 的决策在 plan_mix 里，日内路径根本走不到它。
        # 2026-11-07 切 mix 后，「现行参数 0.00% / 0 笔」被当成了「与基线一致」，
        # 而它真正的意思是「这一行什么都没量」。口径要存进基线，否则下次换模式
        # 会把「口径变了」报成「代码退化」。
        probe_kind = "mix" if mode == "mix" else "daytrade"
        # 本金口径必须写进基线（2026-11-09 补）：账本 11-07 已从 1 万迁到 10 万，
        # 而回测默认本金曾是 10000 —— 同一份行情按 1 万算比按 10 万算多 0.19pp
        # 手续费拖累（5 元佣金下限被放大十倍）。口径变了却不记，
        # 下次改回默认值时这条闸门会把「本金换了」报成「代码退化」。
        cap = backtest.live_capital()
        m = backtest.metrics(backtest.run_backtest(data, probe_p, days,
                                                  capital=cap))
        m_live = backtest.metrics(backtest.run_backtest(data, live_p, days,
                                                       capital=cap))
        ret = round(float(m.get("ret_pct", 0)), 4)
        n = int(m.get("trades", 0))
        sig = _params_sig(live_p)
    except Exception as e:  # noqa: BLE001
        add(WARN, "回归", "回测基线", "回测失败 %s" % str(e)[:60])
        return

    # 标的缺失要说出来：12/13 只和 13/13 只的「一致」不是同一件事
    miss = rep.get("failed") or []
    tail = ""
    if miss:
        tail = "；%d/%d 只标的缺数据（%s），本轮回归没覆盖到它们" % (
            len(miss), rep.get("requested", len(strategy.CORE_POOL)),
            "、".join("%s:%s" % (c, str(w)[:16]) for c, w in miss[:3]))
    # 用的是旧缓存也要说出来：「同一份数据两次结果一样」在数据本身过期时
    # 只证明代码稳定，不证明结论有效。别让读者把绿看成「这数能信」。
    if rep.get("degraded"):
        tail += "；其中 %d 只用旧缓存（见「数据 回测数据集」）" % len(rep["degraded"])
    live_txt = "现行参数 %.2f%% / %d 笔" % (float(m_live.get("ret_pct", 0)),
                                          int(m_live.get("trades", 0)))
    if mode == "mix":
        # 组合模式的「笔」只数卖出，建仓那几笔买入全在 fills 里。不分开写，
        # 「0 笔」会被读成「回测什么都没发生」或「策略没建仓」。
        live_txt = "现行参数 %.2f%%（成交 %d 笔：买入 %d / 卖出 %d）" % (
            float(m_live.get("ret_pct", 0)), int(m_live.get("fills", 0)),
            int(m_live.get("fills", 0)) - int(m_live.get("trades", 0)),
            int(m_live.get("trades", 0)))
    if m_live.get("open_code"):
        live_txt += "，期末仍持有 %s %d 份（未平仓，收益含浮动）" % (
            m_live["open_code"], m_live["open_shares"])
    elif int(m_live.get("open_n") or 0) > 1:
        live_txt += ("，期末持有 %d 只组合标的未平仓（收益含浮动；分钟级只覆盖 "
                     "09:35 调仓与持有，年度收益与回撤见 hiret.py）"
                     % int(m_live["open_n"]))

    def save():
        return _save_baseline(base_f, {
            "ret_pct": ret, "trades": n, "days": days,
            "universe": universe,
            "params": sig, "code": _code_sig(), "probe": probe_kind,
            "capital": round(cap, 2)})

    bstate, base = _read_json_state(base_f)
    if bstate == "missing":
        save()
        add(OK, "回归", "回测基线",
            "首次建立基线 探针 %.2f%% / %d 笔（%s）%s" % (ret, n, live_txt, tail))
        return
    if bstate == "corrupt":
        # 损坏 ≠ 首次。绝不能默默 save() 后报「首次建立基线」：那等于把
        # 「这一轮什么都没比」说成绿，并且顺手毁掉旧基线。先把坏文件挪走留证。
        quarantine = ""
        try:
            q = os.path.join(STATE, "_corrupt_baseline_%s.json"
                             % datetime.now().strftime("%Y%m%d_%H%M%S"))
            os.replace(base_f, q)
            quarantine = "；原文件已隔离为 state/%s" % os.path.basename(q)
        except OSError as e:
            quarantine = "；原文件隔离失败（%s）" % e
        save()
        add(WARN, "回归", "回测基线",
            "基线文件损坏（不是合法 JSON 或内容为空），本轮**无法比对**："
            "已按当前结果重建基线 探针 %.2f%% / %d 笔（%s），本次与历史是否有"
            "差异归因不了%s%s" % (ret, n, live_txt, quarantine, tail))
        return

    # 交易日没变、但标的覆盖变了：这是「数据换了」，不是「代码变了」。
    # 必须卡在 days 比较之前，否则 days 相等就会走到漂移判定，把补数据报成退化。
    if "universe" not in base:
        # 老基线只记了交易日。补记覆盖，并明确说出「这一次的差异归因不了」——
        # 不能默默 save()，否则「重设了基线」会把一次真退化盖成绿色。
        code_same = base.get("code") == _code_sig()
        save()
        add(OK if code_same else WARN, "回归", "回测基线",
            "旧基线未记录标的覆盖，已补记为 %d 只并按当前数据集重设基线 "
            "探针 %.2f%% / %d 笔（旧基线 %.2f%% / %d 笔）%s"
            % (len(universe), ret, n, float(base.get("ret_pct", 0)),
               base.get("trades", 0),
               "；回测代码指纹未变 %s" % _code_sig() if code_same
               else "；回测代码也变过，这次差异归因不了"))
        return

    shift = universe_shift(base.get("universe"), universe)
    if shift:
        code_same = base.get("code") == _code_sig()
        save()
        lvl = OK if (shift == "grew" and code_same) else WARN
        add(lvl, "回归", "回测基线",
            "回测数据集标的覆盖%s（%d 只 → %d 只：%s），已按新数据集重设基线 "
            "探针 %.2f%% / %d 笔（旧基线 %.2f%% / %d 笔，交易日未变所以此前会被"
            "误报成代码漂移）%s"
            % ({"grew": "变宽", "shrank": "变窄"}.get(shift, "换了一批"),
               len(base.get("universe") or []), len(universe),
               "、".join(sorted(set(universe) - set(base.get("universe") or []))[:4])
               or "移除 " + "、".join(sorted(set(base.get("universe") or [])
                                            - set(universe))[:4]),
               ret, n, float(base.get("ret_pct", 0)), base.get("trades", 0),
               "；回测代码指纹未变 %s，可判定为数据变化" % _code_sig() if code_same
               else "；回测代码同时变过，无法区分数据/代码"))
        return

    if base.get("days") != days:
        # 缓存按天滚动，days 变了是常态，不是故障。但「换了数据集」和
        # 「同一份数据结果漂移」必须分开说，否则报「与基线一致」是假话。
        again = base.get("code")
        now = _code_sig()
        save()
        if again and again != now:
            # 代码和数据集同一晚都变了：新基线是「改过的代码 + 新数据」跑出来的，
            # 谁造成的差异分不开。这条不能因「已重设」就报绿。
            add(WARN, "回归", "回测基线",
                "数据集滚动（%d 日 → %d 日）与回测代码变更（%s → %s）同时发生："
                "新基线 %.2f%% / %d 笔已按两者重设，无法据此判断这是数据变化还是代码退化"
                "（旧基线 %.2f%% / %d 笔）。确认无退化后，下一次数据集滚动起恢复比对%s"
                % (len(base.get("days") or []), len(days), again, now, ret, n,
                   float(base.get("ret_pct", 0)), base.get("trades", 0),
                   "；另 " + tail.strip("；") if tail else ""))
            return
        if not again:
            add(OK, "回归", "回测基线",
                "数据集滚动（%d 日 → %d 日），已按新数据集重设基线 探针 %.2f%% / %d 笔"
                "（旧基线无代码指纹，本轮无法区分数据/代码；已补记 %s）%s"
                % (len(base.get("days") or []), len(days), ret, n, now, tail))
            return
        add(OK, "回归", "回测基线",
            "数据集滚动（%d 日 → %d 日，回测代码未变 %s），已按新数据集重设基线 "
            "探针 %.2f%% / %d 笔（%s）%s"
            % (len(base.get("days") or []), len(days), now, ret, n, live_txt, tail))
        return

    if "params" not in base:
        # 旧格式基线没有参数指纹，只能补记，不能拿它判「参数变更」
        save()
        add(OK, "回归", "回测基线",
            "旧基线补记参数指纹 %s，探针 %.2f%% / %d 笔（%s）%s"
            % (sig, ret, n, live_txt, tail))
        return

    if base.get("params") != sig:
        # 参数换了：结果当然会变，这不是回归。说清楚是谁变了、变成什么。
        save()
        add(OK, "回归", "回测基线",
            "策略参数变更（%s → %s），已按新参数重设基线 探针 %.2f%% / %d 笔（%s）%s"
            % (base.get("params") or "未记录", sig, ret, n, live_txt, tail))
        return

    if base.get("probe") != probe_kind:
        # 探针口径变了（日内 ↔ 组合）。这两个口径量的根本不是同一件事，
        # 拿新旧结果比「漂移」是假警报；但也不能默默重设——要说清旧基线是什么口径。
        save()
        add(OK, "回归", "回测基线",
            "回归探针口径变更（%s → %s），已按新口径重设基线 探针 %.2f%% / %d 笔（%s）%s"
            % (base.get("probe") or "未记录", probe_kind, ret, n, live_txt, tail))
        return

    if round(float(base.get("capital") or 0), 2) != round(cap, 2):
        # 本金口径变了（老基线固定按 1 万跑，2026-11-07 账本已迁到 10 万）。
        # ret_pct 当然会变，这不是代码退化；但也不能默默 save()。
        # 不单独写一条的话，它会落进下面的 drift 分支，被报成
        # 「同一份数据、参数指纹未变，所以是代码行为变了」——一个假警报。
        old_cap = base.get("capital")
        old_txt = ("未记录（旧版固定 1 万）" if not old_cap
                   else "%.2f" % float(old_cap))
        save()
        add(OK, "回归", "回测基线",
            "回测本金口径变更：%s → %.2f（账本 meta.init_capital）。"
            "已按新本金重设基线 探针 %.2f%% / %d 笔（旧基线 %.2f%% / %d 笔）%s。"
            "本金只影响佣金下限与整手取整，不影响策略决策，所以这不是退化；"
            "但它足以改变收益率量级，必须显式记账"
            % (old_txt, cap, ret, n, float(base.get("ret_pct", 0)),
               base.get("trades", 0), tail))
        return

    drift = abs(ret - float(base.get("ret_pct", 0)))
    now_code = _code_sig()
    if drift <= 0.05 and n == base.get("trades") and base.get("code") != now_code:
        # 结果没变、但回测代码变了：指纹必须当场刷新。
        # 不刷新的话，这个旧指纹会一直躺到下一个数据集滚动日，那天闸门比
        # 「旧指纹 ≠ 新指纹」，报「数据集与代码同时变更、无法区分」——而我们
        # 此刻明明就站在能区分的位置上：同一份数据、同一个窗口、结果一模一样。
        # 2026-11-06 就是这样：改了 broker 的涨跌停取整，探针仍是 -3.55% / 0 笔，
        # 若不刷新，11-07 滚动日会凭空多一条假警报。
        save()
        add(OK, "回归", "回测基线",
            "探针 %.2f%% / %d 笔与基线一致；回测代码指纹已刷新 %s → %s（结果未变，"
            "属等价改写）%s" % (ret, n, base.get("code") or "未记录", now_code, tail))
        return
    if drift > 0.05 or n != base.get("trades"):

        add(WARN, "回归", "回测基线",
            "结果漂移：探针 %.2f%%→%.2f%%，%d→%d 笔（同一份数据、参数指纹 %s 未变，"
            "所以是代码行为变了）%s"
            % (float(base.get("ret_pct", 0)), ret, base.get("trades", 0), n,
               sig, tail))
    else:
        add(OK, "回归", "回测基线",
            "探针 %.2f%% / %d 笔，与基线一致；%s%s" % (ret, n, live_txt, tail))


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
    check_mirror()
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
