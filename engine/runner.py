# -*- coding: utf-8 -*-
"""执行引擎：把策略跑在真实行情上，记账到模拟账户。

设计要点：
  * 幂等重放：每次运行都把账户恢复到「当日开盘状态」重新推演，
    避免重复运行导致重复记账；结果只取决于当日分钟数据。
  * 无未来函数：第 i 分钟的决策只用 ticks[0..i-1]，成交在 ticks[i]。
  * 全部规则由 broker 强制校验（T+1、涨跌停、整百、资金）。
"""
import argparse
import csv
import json
import os
import shutil
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker      # noqa: E402
import market      # noqa: E402
import strategy    # noqa: E402

STATE = broker.STATE
OPEN_FILE = os.path.join(STATE, "account_open.json")


# --------------------------------------------------------------------------
def reset_to_open(day):
    """把账户恢复到当日开盘状态（幂等前提）。"""
    cur = broker.load_account()
    if os.path.exists(OPEN_FILE):
        with open(OPEN_FILE, "r", encoding="utf-8") as f:
            op = json.load(f)
        if op.get("date") == day:
            acc = op["account"]
            broker.save_account(acc)
            _drop_today_trades(day)
            return acc
    # 当日首次运行：把当前账户存档为开盘状态
    with open(OPEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": day, "account": cur}, f, ensure_ascii=False, indent=2)
    _drop_today_trades(day)
    return cur


def _drop_today_trades(day):
    if not os.path.exists(broker.TRADE_FILE):
        return
    with open(broker.TRADE_FILE, "r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    keep = [rows[0]] + [r for r in rows[1:] if r and not r[1].startswith(day)]
    with open(broker.TRADE_FILE, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(keep)


def _fix_seq(acc):
    """重放后交易序号与 CSV 对齐。"""
    if not os.path.exists(broker.TRADE_FILE):
        return
    with open(broker.TRADE_FILE, "r", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    acc["trade_seq"] = len(rows) - 1 if rows else 0
    broker.save_account(acc)


# --------------------------------------------------------------------------
HEARTBEAT = os.path.join(STATE, "heartbeat.json")


def _heartbeat(day, snap, events):
    """记录一次成功运行，供 selfcheck 判断任务有没有静默漏跑。

    自动化不补跑，漏一天就是漏一天。有了这个文件，至少能查出来。
    """
    prev = {}
    if os.path.exists(HEARTBEAT):
        try:
            with open(HEARTBEAT, "r", encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:  # noqa: BLE001
            prev = {}
    days = set(prev.get("days") or [])
    days.add(day)
    data = {
        "last_run": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_day": day,
        "days": sorted(days)[-90:],
        "equity": round(float(snap.get("equity") or 0), 2),
        "positions": len(snap.get("positions") or {}),
        "events_today": len(events),
        "auto": True,
    }
    try:
        with open(HEARTBEAT, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


def _warn_overnight(acc, day, verbose=True):
    """检测前一交易日遗留的仓位。

    策略是日内的，正常收盘后不该有持仓。若存在，说明收盘定稿任务没执行
    或尾盘强平没生效——这是执行问题，不是策略问题，必须显式告警。
    """
    stale = []
    for code, pos in (acc.get("positions") or {}).items():
        for lot in (pos.get("lots") or []):
            if lot.get("date") != day:
                stale.append((code, pos.get("name") or code,
                              lot.get("shares", 0), lot.get("date")))
    if stale and verbose:
        print("  [警告] 存在过夜仓位（违反日内平仓原则）：")
        for code, name, sh, d in stale:
            print(f"    {code} {name} {sh} 股，建仓日 {d}")
        print("    可能原因：收盘定稿任务（15:05）未执行，或强平未生效。")
    return stale


def run(verbose=True, params=None, execute=True, ttl=600):
    params = params or dict(strategy.PARAMS)
    acc = broker.load_account()
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    _warn_overnight(acc, day, verbose)

    quotes = market.fetch_quotes(strategy.CORE_POOL)
    pool = strategy.validate_pool(quotes, params)
    if not pool:
        print("今日无可交易标的（流动性/价格不达标）")
        return None

    # 拉分时
    data = {}
    for p in pool:
        try:
            tr = market.fetch_trends(p["code"], ndays=1, ttl=ttl)
            if tr["ticks"]:
                data[p["code"]] = tr
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  ! {p['code']} 分时拉取失败: {e}")

    if not data:
        print("无分时数据")
        return None

    # --- 交易日守卫 -------------------------------------------------------
    # 非交易日（周末、节假日、开盘前）时，分时接口不会返回空，而是把「上一个
    # 交易日」的完整分钟线原样吐回来，时间戳也是那一天的。若照常重放，就会拿
    # 昨天的行情当今天推演，并通过 reset_to_open/_drop_today_trades 把昨天的
    # 成交重新记一遍——凭空多出一批假流水，而且账本看起来完全正常。
    #
    # 2026-10-21 补：本地一天只跑一次，撞节假日概率低；但搬到云端后是每天
    # 定时自动跑，节假日必然命中，所以这道守卫是上云的前置条件。
    _days = {t["t"][:10] for tr in data.values() for t in tr["ticks"] if t.get("t")}
    if day not in _days:
        print("非交易日或尚未开盘：拿到的是 %s 的分时，今天 %s"
              % ("、".join(sorted(_days)) or "无", day))
        return None

    # 守卫通过后才动账本。顺序很重要：先确认今天真的有行情，再存档、再放账本。
    if execute:
        acc = reset_to_open(day)
    # dry 也要把日期推到今天：否则 snapshot 会把今天的净值写进昨天那一行
    broker.roll_day(acc, day)

    # 统一时间轴
    tl = set()
    for tr in data.values():
        tl |= {t["t"] for t in tr["ticks"]}
    tlist = sorted(tl)

    # 大盘参照（沪深300 ETF 作为市场代理，避免再请求指数接口）
    mkt = data.get("510300") or list(data.values())[0]
    mkt_pre = mkt["preclose"] or mkt["ticks"][0]["price"]

    idx = {c: {t["t"]: k for k, t in enumerate(tr["ticks"])} for c, tr in data.items()}
    ctx = {"trades": 0, "market_pct": 0.0, "preclose": None}
    pos = None
    events = []

    # 开盘就有持仓（过夜仓）= 上一次收盘没平掉。必须接回来继续管理，
    # 否则这里认为是空仓，会去开第二只——而资金天然不够同时摊薄两只。
    if acc.get("positions"):
        c0 = next(iter(acc["positions"]))
        if c0 in data:
            pos = {"code": c0,
                   "entry": broker.avg_cost(acc["positions"][c0]),
                   "entry_idx": 0,
                   "shares": broker.position_shares(acc["positions"][c0]),
                   "overnight": True}
            if verbose:
                print(f"  [续管] 开盘持仓 {c0} {pos['shares']} 份 "
                      f"成本 {pos['entry']:.3f}，按日内规则平掉")

    for i, t in enumerate(tlist):
        tm = t[11:16]
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M")
        if "510300" in idx and t in idx["510300"]:
            mk = data["510300"]["ticks"][idx["510300"][t]]["price"]
            ctx["market_pct"] = (mk / mkt_pre - 1) * 100

        # --- 持仓管理 ---
        if pos:
            c = pos["code"]
            ticks = data[c]["ticks"]
            k = idx[c].get(t)
            if k is None:
                # 这一分钟该标的没有 tick（停牌 / 数据缺口）。
                # 不能因为少一根数据就把仓位留到过夜：到强平时刻就用最后一根价平掉。
                if tm < params["force_exit"]:
                    continue
                k = len(ticks) - 1
            if k >= 1:
                pctx = dict(ctx)
                pctx["preclose"] = data[c]["preclose"] or ticks[0]["price"]
                # 「能不能卖」和「该不该卖」是两件事：
                # 股票型 ETF 是 T+1，当天买入的份额当天卖不掉；
                # 跨境/黄金/债券 ETF 才是 T+0。不分开就会出现假成交。
                can_sell = bool(pos.get("overnight")) or broker.is_t0_etf(c)
                act, reason, detail = strategy.decide(
                    ticks, k - 1,
                    {"entry": pos["entry"], "entry_idx": pos["entry_idx"]},
                    pctx, params, can_sell=can_sell)
                # decide 看到的时间来自 ticks[k-1]，缺数据时它不知道已经 14:50，
                # 所以强平要按时间轴再压一次。
                if tm >= params["force_exit"] and act != "SELL" and can_sell:
                    act, reason = "SELL", "尾盘强平"
                if act == "SELL" and execute:
                    try:
                        r = broker.sell(acc, c, data[c]["name"] or c,
                                        ticks[k]["price"], pos["shares"], dt,
                                        reason=reason,
                                        preclose=data[c]["preclose"])
                        pos = None
                        ctx["trades"] += 1
                        events.append({"t": t, "act": "SELL", "code": c,
                                       "name": data[c]["name"], "price": r["price"],
                                       "shares": r["shares"], "pnl": r["pnl"],
                                       "fee": r["fee"], "reason": reason})
                    except broker.Reject as e:
                        events.append({"t": t, "act": "REJECT", "code": c,
                                       "reason": str(e)})
            continue

        # --- 开仓：全池扫描 ---
        best = None
        for c, tr in data.items():
            if t not in idx[c]:
                continue
            k = idx[c][t]
            if k < 22:
                continue
            cctx = dict(ctx)
            cctx["preclose"] = tr["preclose"] or tr["ticks"][0]["price"]
            act, reason, detail = strategy.decide(tr["ticks"], k - 1, None, cctx, params)
            if act == "BUY":
                score = abs(detail.get("dev", 0))
                if not best or score > best[0]:
                    best = (score, c, tr, k, reason)

        if best:
            _, c, tr, k, reason = best
            px = tr["ticks"][k]["price"]
            budget = (acc["cash"] - 5.5) * 0.97
            shares = int(budget / px / 100) * 100
            if shares >= 100 and execute:
                try:
                    r = broker.buy(acc, c, tr["name"] or c, px, shares, dt,
                                   reason=reason, preclose=tr["preclose"])
                    pos = {"code": c, "entry": r["price"], "entry_idx": k,
                           "shares": r["shares"], "date": day,
                           "overnight": False}
                    events.append({"t": t, "act": "BUY", "code": c,
                                   "name": tr["name"], "price": r["price"],
                                   "shares": r["shares"], "fee": r["fee"],
                                   "reason": reason})
                except broker.Reject as e:
                    events.append({"t": t, "act": "REJECT", "code": c,
                                   "reason": str(e)})

    # --- 收盘兜底强平 ---
    # 走到这里还有仓位，说明该标的尾盘没有 tick，循环里的强平根本没机会执行。
    # 不补这一步，仓位就会安静地过夜。
    if execute and pos and acc.get("positions") and tlist:
        can_sell = bool(pos.get("overnight")) or broker.is_t0_etf(pos["code"])
        if tlist[-1][11:16] >= params["force_exit"] and not can_sell:
            # T+1 当天买入的份额，尾盘也确实卖不掉。这是规则，不是故障——
            # 但要明确记一笔，否则第二天看到过夜仓会以为是程序漏了。
            events.append({"t": tlist[-1], "act": "LOCKED", "code": pos["code"],
                           "reason": "T+1 锁定，今日买入不可卖出，转次日续管"})
        if tlist[-1][11:16] >= params["force_exit"] and can_sell:
            c = pos["code"]
            ticks = data[c]["ticks"]
            dt2 = datetime.strptime(tlist[-1], "%Y-%m-%d %H:%M")
            try:
                r = broker.sell(acc, c, data[c]["name"] or c, ticks[-1]["price"],
                                pos["shares"], dt2, reason="尾盘强平(兜底)",
                                preclose=data[c]["preclose"])
                pos = None
                ctx["trades"] += 1
                events.append({"t": tlist[-1], "act": "SELL", "code": c,
                               "name": data[c]["name"], "price": r["price"],
                               "shares": r["shares"], "pnl": r["pnl"],
                               "fee": r["fee"], "reason": "尾盘强平(兜底)"})
            except broker.Reject as e:
                events.append({"t": tlist[-1], "act": "REJECT", "code": c,
                               "reason": str(e)})

    if execute:
        _fix_seq(acc)

    # dry 只算不写：否则会用今天的数覆盖昨天的净值行
    snap = broker.snapshot(acc, quotes, note="盘中运行", write=execute)
    if execute:
        _heartbeat(day, snap, events)

    if verbose:
        print("=" * 62)
        print(f"  模拟盘执行  {now:%Y-%m-%d %H:%M}   数据截止 {tlist[-1][11:]}")
        print("=" * 62)
        print(f"  大盘(沪深300ETF) {ctx['market_pct']:+.2f}%   可交易标的 {len(pool)} 只")
        print(f"  权益 ¥{snap['equity']:,.2f}   现金 ¥{snap['cash']:,.2f}   "
              f"市值 ¥{snap['market_value']:,.2f}")
        print(f"  当日盈亏 ¥{snap['day_pnl']:+,.2f}   累计 ¥{snap['total_pnl']:+,.2f}")
        print("-" * 62)
        if events:
            for e in events:
                if e["act"] == "REJECT":
                    print(f"  ✗ {e['t'][11:]} {e['code']} 拒单: {e['reason']}")
                elif e["act"] == "LOCKED":
                    print(f"  🔒 {e['t'][11:]} {e['code']} {e['reason']}")
                elif e["act"] == "BUY":
                    print(f"  → {e['t'][11:]} 买入 {e['name']} {e['price']:.3f} "
                          f"×{e['shares']} = ¥{e['price']*e['shares']:,.0f}  "
                          f"费{e['fee']:.2f}  [{e['reason']}]")
                else:
                    print(f"  ← {e['t'][11:]} 卖出 {e['name']} {e['price']:.3f} "
                          f"×{e['shares']}  盈亏{e['pnl']:+.2f} 费{e['fee']:.2f}  "
                          f"[{e['reason']}]")
        else:
            print("  今日无满足条件的信号（空仓等待）")
        if acc["positions"]:
            print("-" * 62)
            for c, p in acc["positions"].items():
                sh = broker.position_shares(p)
                px = (quotes.get(c) or {}).get("price") or broker.avg_cost(p)
                print(f"  持仓 {p['name']} {sh} 份 @ {broker.avg_cost(p):.3f}  "
                      f"现价 {px:.3f}  浮盈 {(px/broker.avg_cost(p)-1)*100:+.2f}%")
        print("=" * 62)

    # 成交落地后立刻推到云端，手机端几秒内就能看到新仓位。
    # 放在最后、吞掉所有异常：同步是锦上添花，绝不能反过来把交易搞失败。
    # push_cloud 自己用流水签名做幂等，没成交时一个请求都不发。
    if execute:
        try:
            import push_cloud
            push_cloud.push()
        except Exception:       # noqa: BLE001
            pass

    return {"snapshot": snap, "events": events, "pool": pool, "acc": acc}


def _acquire_lock():
    """拿一个排他锁，防止两个 runner 同时改账本。

    为什么要它：定时任务会重叠——盘中每 15 分钟的循环、收盘定稿、手动补跑、
    巡检任务，都可能在同一分钟触发。runner 的流程是「读账户 → 重放全天 →
    写账户」，两个进程同时跑就是经典的 lost update：后写的那个把先写的覆盖掉，
    流水少一笔、现金对不上，而且**不会报错**，要等第二天 doctor 才发现。

    用 O_EXCL 建文件：这个操作在文件系统层是原子的，谁先建谁拿到。
    拿不到说明已经有一个在跑，直接退出——比两个一起写坏账本强得多。
    """
    path = os.path.join(STATE, "runner.lock")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return path
    except FileExistsError:
        # 被强杀（Ctrl+C、断电）会留下僵尸锁，超过 10 分钟就认定它死了
        try:
            if time.time() - os.path.getmtime(path) > 600:
                os.remove(path)
                return _acquire_lock()
        except OSError:
            pass
        return None


def _in_session(now=None):
    """是否在 A 股连续竞价时段。"""
    t = (now or datetime.now()).strftime("%H:%M")
    return ("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00")


def _loop(interval):
    """
    常驻循环：交易时段内每 N 秒重放一次。

    为什么必须有它：调度器的 rrule 只收单个 BYMINUTE，最快只能做到整点一次，
    而本策略是分钟级的「偏离 + 反弹」均值回归——09:50 出的信号，一小时后再
    看见时价格早已弹回 VWAP 上方，等于白等。整点调度只能当兜底，
    真正贴近实盘的执行要靠这个进程。

    每轮独立拿锁：这样单次 run 崩溃不会把锁带走，锁的粒度也更细。
    """
    # pythonw 启动时没有控制台，sys.stdout 是 None，任何 print 都会抛
    # AttributeError 把循环打死——而进程还在，看起来"在跑"，实际早死了。
    # 这里先兜住，两条流都落到文件，出问题还能回溯。
    if sys.stdout is None or sys.stderr is None:
        _out = open(os.path.join(STATE, "loop_out.log"), "a",
                    encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _out
        if sys.stderr is None:
            sys.stderr = _out
    print("  常驻循环启动  间隔 %ds  仅 %s 内执行  Ctrl+C 退出"
          % (interval, "09:30-11:30 / 13:00-15:00"))
    log_path = os.path.join(STATE, "loop.log")
    pid_path = os.path.join(STATE, "loop.pid")
    if not os.path.exists(log_path):
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("time,pool,equity,positions,events\n")
    with open(pid_path, "w", encoding="ascii") as f:
        f.write(str(os.getpid()))
    quiet_until = None
    while True:
        now = datetime.now()
        if now.weekday() >= 5 or not _in_session(now):
            # 非交易时段：不空转刷屏，只在跨进/跨出时段时说一句
            if quiet_until != now.strftime("%H:%M")[:2]:
                quiet_until = now.strftime("%H:%M")[:2]
                print("  [%s] 非交易时段，等待中" % now.strftime("%H:%M"))
            time.sleep(min(interval, 30))
            continue
        quiet_until = None
        lock = _acquire_lock()
        if lock is None:
            time.sleep(interval)          # 别人在跑，让开
            continue
        try:
            # ttl 必须小于循环间隔，否则数据一直是旧的：
            # market.fetch_trends 默认 ttl=600，就算每 60 秒跑一次，
            # 也要每 10 分钟才真正换一份分时——循环看着在跑，其实在看历史。
            r = run(verbose=False, execute=True, ttl=max(20, interval - 5))
        except Exception as e:            # 单轮异常不能杀死循环
            print("  [%s] 本轮异常：%s: %s"
                  % (now.strftime("%H:%M"), type(e).__name__, e))
            r = None
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass

        if r:
            ev = r.get("events") or []
            snap = r["snapshot"]
            # 只有出现成交/拒单才大声说，否则安静地记一行日志
            if ev:
                print("  [%s] 权益 ¥%.2f  持仓 %d  事件 %d 笔"
                      % (now.strftime("%H:%M"), snap["equity"],
                         len(r["acc"]["positions"]), len(ev)))
                for e in ev:
                    if e["act"] == "BUY":
                        print("     → 买入 %s %.3f ×%d  [%s]"
                              % (e["name"], e["price"], e["shares"], e["reason"]))
                    elif e["act"] == "SELL":
                        print("     ← 卖出 %s %.3f  盈亏 %+.2f  [%s]"
                              % (e["name"], e["price"], e["pnl"], e["reason"]))
                    else:
                        print("     ✗ 拒单 %s  %s" % (e["code"], e["reason"]))
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write("%s,%d,%.2f,%d,%d\n"
                            % (now.strftime("%Y-%m-%d %H:%M:%S"), len(r["pool"]),
                               snap["equity"], len(r["acc"]["positions"]), len(ev)))
            except OSError:
                pass
        time.sleep(interval)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只推演不记账")
    ap.add_argument("--loop", type=int, default=0, metavar="SEC",
                    help="常驻循环，每 SEC 秒跑一次（仅交易时段）")
    a = ap.parse_args()

    if a.loop > 0:
        try:
            _loop(a.loop)
        except KeyboardInterrupt:
            pid_path = os.path.join(STATE, "loop.pid")
            try:
                os.remove(pid_path)
            except OSError:
                pass
            print("\n  常驻循环已停止")
    elif a.dry:
        run(execute=False)      # 只读推演，不占锁，方便随时排查
    else:
        lock = _acquire_lock()
        if lock is None:
            print("  另一个 runner 正在运行，本次跳过（并发写会让账本丢流水）")
            raise SystemExit(0)
        try:
            run(execute=True)
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass
