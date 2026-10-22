#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端自测：用合成行情把「不该发生的事」变成可回归的断言。

为什么需要它：
    selfcheck 查环境，doctor 查不变量，但两者都在**真实账本**上跑。
    真实账本经常是空的（今天 0 笔），空账本上什么都「通过」——
    那是假的安全感。

    这个文件不碰真实账户，全部在临时目录里用假行情跑，
    所以可以放心地制造极端情况：资金不足、缺数据、过夜仓、重复运行。

覆盖的 bug（都是真实发生过的）：
  1. `--dry` 试跑把今天的净值写进昨天那一行
  2. 已有过夜仓时又开了第二只（资金不够摊薄两只）
  3. 持仓标的尾盘没数据 → 强平不执行 → 仓位过夜
  4. 重复运行导致重复记账（幂等性）
  5. 决策偷看未来数据

用法：
    python engine/selftest.py          # 全部跑，约 2 秒
    python engine/selftest.py -v       # 打印每个断言的细节
退出码：0 = 全过，1 = 有失败。
"""
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker      # noqa: E402
import doctor      # noqa: E402
import market      # noqa: E402
import runner      # noqa: E402
import strategy    # noqa: E402

VERBOSE = False
CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def today():
    """测试行情必须造在「今天」。

    runner 里有一道交易日守卫：分时里所有 tick 的日期都不等于今天，就认定
    「非交易日/还没开盘」直接退出。用例若把日期写死成某一天，过了那天，
    守卫会把整条链路拦在账本之前——于是幂等、过夜仓、尾盘强平三条用例
    全部因为「拿到的是 2026-10-19 的分时」而红，看起来像三个 bug，
    其实是一个：测的不是代码，是日历。2026-10-22 就是这么红的。
    """
    return datetime.now().strftime("%Y-%m-%d")


def days_before(n):
    """n 天前的日期，用于造「前一日建仓」的过夜仓。"""
    return datetime.fromtimestamp(datetime.now().timestamp() - n * 86400).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 临时 state 沙箱
# --------------------------------------------------------------------------
@contextmanager
def sandbox():
    tmp = tempfile.mkdtemp(prefix="tsim_")
    old = (broker.STATE, broker.ACCOUNT_FILE, broker.TRADE_FILE, broker.EQUITY_FILE,
           runner.STATE, runner.OPEN_FILE, runner.HEARTBEAT)
    broker.STATE = tmp
    broker.ACCOUNT_FILE = os.path.join(tmp, "account.json")
    broker.TRADE_FILE = os.path.join(tmp, "trades.csv")
    broker.EQUITY_FILE = os.path.join(tmp, "equity.csv")
    runner.STATE = tmp
    runner.OPEN_FILE = os.path.join(tmp, "account_open.json")
    runner.HEARTBEAT = os.path.join(tmp, "heartbeat.json")
    try:
        yield tmp
    finally:
        (broker.STATE, broker.ACCOUNT_FILE, broker.TRADE_FILE, broker.EQUITY_FILE,
         runner.STATE, runner.OPEN_FILE, runner.HEARTBEAT) = old
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# 合成行情
# --------------------------------------------------------------------------
def session_minutes():
    """09:30-11:30 + 13:00-15:00，一分钟一根。"""
    out = []
    for h, m0, m1 in ((9, 30, 60), (10, 0, 60), (11, 0, 30)):
        for m in range(m0, m1):
            out.append((h, m))
    for h, m0, m1 in ((13, 0, 60), (14, 0, 60), (15, 0, 1)):
        for m in range(m0, m1):
            out.append((h, m))
    return out


def mk_ticks(day, prices, minutes=None, preclose=4.0):
    """按给定价格序列生成 ticks；avg 用累计均价模拟。"""
    minutes = minutes or session_minutes()
    ticks, camt, cvol = [], 0.0, 0.0
    for k, px in enumerate(prices):
        if k >= len(minutes):
            break
        h, m = minutes[k]
        vol = 10000.0
        camt += px * vol
        cvol += vol
        ticks.append({"t": "%s %02d:%02d" % (day, h, m),
                      "price": round(px, 3), "vol": vol,
                      "avg": round(camt / cvol, 4)})
    return ticks


def mk_quote(code, price, preclose=4.0):
    return {"code": code, "name": code, "price": price, "preclose": preclose,
            "open": preclose, "high": max(price, preclose) * 1.01,
            "low": min(price, preclose) * 0.99, "pct": (price / preclose - 1) * 100,
            "amount": 5e8, "vol": 1e6}


def flat_prices(n, px, drift=0.0):
    return [px * (1 + drift * k / max(n - 1, 1)) for k in range(n)]


def install_fake_market(trends, quotes):
    """替换 runner 依赖的两个取数函数。"""
    old_q = runner.market.fetch_quotes
    old_t = runner.market.fetch_trends

    def fq(codes):
        return {c: quotes[c] for c in codes if c in quotes}

    def ft(code, ndays=1, ttl=600):
        d = trends.get(code)
        if not d:
            raise RuntimeError("no data")
        return d

    runner.market.fetch_quotes = fq
    runner.market.fetch_trends = ft
    return old_q, old_t


def restore_market(old):
    runner.market.fetch_quotes, runner.market.fetch_trends = old


def seed_position(acc, code, shares, price, day):
    acc["positions"][code] = {
        "name": code, "realized": 0.0,
        "lots": [{"date": day, "shares": shares, "price": price,
                  "fee": 5.0, "time": "10:00"}],
    }
    acc["cash"] = round(acc["cash"] - shares * price - 5.0, 2)
    broker.save_account(acc)
    return acc


# ==========================================================================
# 案例
# ==========================================================================
@case("券商规则：整百 / 资金 / 时段")
def t_broker_rules():
    with sandbox():
        acc = broker.load_account()
        dt = datetime(2026, 10, 19, 10, 0)
        try:
            broker.buy(acc, "510300", "x", 4.0, 150, dt)
            raise AssertionError("非整百买入竟然通过")
        except broker.Reject:
            pass
        try:
            broker.buy(acc, "510300", "x", 4.0, 100000, dt)
            raise AssertionError("资金不足竟然通过")
        except broker.Reject:
            pass
        try:
            broker.buy(acc, "510300", "x", 4.0, 100, datetime(2026, 10, 19, 12, 0))
            raise AssertionError("午休时段竟然成交")
        except broker.Reject:
            pass


@case("券商规则：持仓上限 1 只")
def t_max_positions():
    with sandbox():
        acc = broker.load_account()
        dt = datetime(2026, 10, 19, 10, 0)
        broker.buy(acc, "510300", "a", 4.0, 1000, dt)
        try:
            broker.buy(acc, "510500", "b", 6.7, 400, dt)
            raise AssertionError("同时持有了两只（上限未生效）")
        except broker.Reject as e:
            check("上限" in str(e), "拒绝原因不对：%s" % e)
        same = broker.buy(acc, "510300", "a", 4.0, 100, dt)   # 同标的不受限制
        check(same["shares"] == 100, "同标的加仓被误拦")


@case("费用：ETF 免印花税，佣金有 5 元下限")
def t_fee():
    etf = broker.calc_fee("510300", "SELL", 4000.0)
    check(etf["stamp_tax"] == 0 and etf["transfer_fee"] == 0, "ETF 被收了印花税/过户费")
    check(etf["commission"] == 5.0, "小额佣金未按下限：%.2f" % etf["commission"])
    big = broker.calc_fee("510300", "SELL", 1000000.0)
    check(abs(big["commission"] - 250.0) < 0.01, "大额佣金算错：%.2f" % big["commission"])
    stk = broker.calc_fee("600000", "SELL", 1000000.0)
    check(abs(stk["stamp_tax"] - 500.0) < 0.01, "股票卖出未收印花税")


@case("T+1：股票型 ETF 当日买入不可卖出")
def t_t1_etf():
    """2026-10-20 修掉的真实错误：原来所有 ETF 都被当成 T+0。

    上交所《ETF 相关问题》第 3 条：只有债券/黄金/跨境/货币 ETF 支持 T+0，
    **股票 ETF 实施 T+1**。池子里的 510/512/588/1599xx 全是股票型。
    这条用例存在的意义就是：以后谁再把 ETF 一律当 T+0，这里立刻红。
    """
    with sandbox():
        acc = broker.load_account()
        dt = datetime(2026, 10, 20, 10, 0)

        # ① 债券 ETF：T+0，当天买当天能卖（先测，测完就清仓，
        #    否则会占住「持仓上限 1 只」让后面的买入被拒）
        broker.buy(acc, "511010", "国债ETF", 10.0, 100, dt)
        try:
            broker.sell(acc, "511010", "国债ETF", 10.01, 100, dt)
        except broker.Reject as e:
            check(False, "债券 ETF 应当支持 T+0，却被拒：%s" % e)

        # ② 股票型 ETF：当天买当天卖必须被拒
        broker.buy(acc, "159915", "创业板ETF", 2.7, 1000, dt)
        try:
            broker.sell(acc, "159915", "创业板ETF", 2.71, 1000, dt)
        except broker.Reject as e:
            check("T+1" in str(e), "拒绝理由不是 T+1：%s" % e)
        else:
            check(False, "当日买入的股票型 ETF 竟然卖掉了（T+1 校验失效）")


@case("券商：0 股 / 负股数卖出必须被拒")
def t_sell_invalid_shares():
    """0 股卖出曾经能过：0 > avail 不成立，一路走到收 5 元佣金下限，
    账本多一笔「SELL 0 股 / 现金 -5.00」；负股数更糟，现金直接变少。
    两者都不报错，只有对账时才会发现现金对不上。"""
    with sandbox():
        acc = broker.load_account()
        dt = datetime(2026, 10, 19, 10, 0)
        broker.buy(acc, "510300", "a", 4.0, 1000, dt)
        cash = broker.load_account()["cash"]
        for bad in (0, -100):
            try:
                broker.sell(acc, "510300", "a", 4.0, bad, dt)
            except broker.Reject:
                pass
            else:
                raise AssertionError("%d 股卖出竟然成交" % bad)
        check(broker.load_account()["cash"] == cash,
              "被拒的卖出动过现金：%.2f → %.2f"
              % (cash, broker.load_account()["cash"]))


@case("分时：盘后 tick（15:05+）必须裁掉")
def t_trim_afterhours():
    """东财 ETF 分时含 15:05-15:30 的盘后固定价格成交（实测 2026-10-22
    588000/510300/159915 三只最后一根都落在 15:30）。

    危害路径：时间轴取全池并集，tlist[-1] 变成 15:30；一旦某个持仓标的
    尾盘缺数据，兜底强平就拿 15:30 去下单，broker 判「非连续竞价时段」
    拒单 —— 仓位安静过夜，账本里只留一条 REJECT。盘后价不属于交易时段，
    必须在进模型之前裁掉，而不是指望撮合层兜。
    """
    raw = [{"t": "2026-10-22 09:30", "price": 4.0},
           {"t": "2026-10-22 11:30", "price": 4.0},
           {"t": "2026-10-22 12:00", "price": 4.0},     # 午休
           {"t": "2026-10-22 14:49", "price": 4.0},
           {"t": "2026-10-22 15:00", "price": 4.0},
           {"t": "2026-10-22 15:06", "price": 4.0},     # 盘后定价
           {"t": "2026-10-22 15:30", "price": 4.0}]
    kept = [t["t"][11:] for t in runner._trim_to_session(raw)]
    check(kept == ["09:30", "11:30", "14:49", "15:00"],
          "盘后/午休 tick 没裁干净：%s" % kept)
    # 15:30 本来就撮合不了——这条断言锁住「为什么必须裁」而不是「裁了好看」
    check(not broker.in_trading_session(datetime(2026, 10, 22, 15, 30)),
          "15:30 竟然算连续竞价时段")


@case("分时：接口返回 0 根不能当成功")
def t_empty_trends():
    """2026-10-20 自审抓到的静默兜底。

    东财对 ndays>1 的请求会返回 200 + 空 trends。原逻辑把 {"ticks": []}
    当正常结果**写进缓存并返回**，调用方看到的是「拉取成功、0 点 0 天」，
    而且接下来 ttl 秒内所有读取都会命中这份空缓存。
    空必须等于失败：降级到旧缓存，或者抛错。
    """
    old_dir = market.CACHE_DIR
    old_get = market._get
    tmp = tempfile.mkdtemp(prefix="mkt_")
    market.CACHE_DIR = tmp
    market._get = lambda *a, **k: {"data": {"trends": []}}
    try:
        try:
            market.fetch_trends("510300", ndays=6, ttl=0)
        except RuntimeError:
            pass
        else:
            check(False, "空分时被当成了成功返回")
        left = [f for f in os.listdir(tmp) if f.endswith(".json")]
        check(not left, "空响应不该写进缓存，却留下了 %s" % left)
    finally:
        market._get = old_get
        market.CACHE_DIR = old_dir
        shutil.rmtree(tmp, ignore_errors=True)


@case("云端：账本写入但推送失败 → 退出码 4")
def t_cloud_push_fail():
    """账本对了、看板没更新，是最容易漏掉的一种状态：workflow 全绿、
    日志里只有一行 print。2026-10-22 的 SIM_CLOUD_URL 指向已下线域名
    就是这样一轮都没推出去。这条用例把「必须能看见」钉死。"""
    import cloud_run
    old_run, old_push, old_build = runner.run, runner.LAST_PUSH, cloud_run.dashboard.build
    try:
        runner.run = lambda **k: {"snapshot": {}, "events": [], "pool": [], "acc": {}}
        runner.LAST_PUSH = ("fail", "URLError: tunnel")
        cloud_run.dashboard.build = lambda quiet=False: "/tmp/x.html"
        code = cloud_run.main(["--force"])
    finally:
        runner.run, runner.LAST_PUSH, cloud_run.dashboard.build = old_run, old_push, old_build
    check(code == 4, "推送失败时退出码应为 4，实际 %s" % code)


@case("成交后资金恒等式成立")
def t_identity():
    with sandbox():
        acc = broker.load_account()
        dt = datetime(2026, 10, 19, 10, 0)
        dt2 = datetime(2026, 10, 20, 10, 0)
        broker.buy(acc, "510300", "a", 4.0, 1000, dt)
        broker.buy(acc, "510300", "a", 3.9, 500, dt)
        broker.sell(acc, "510300", "a", 4.1, 700, dt2)   # 次日卖，符合 T+1
        rows = doctor._read_trades()
        cash, pos, realized, probs = doctor.replay(rows, 10000.0)
        check(not probs, "重放报错：%s" % probs[:2])
        left, right, diff = doctor.ledger_identity(acc, cash, pos, realized)
        check(abs(diff) < 0.02, "恒等式破了：%.2f ≠ %.2f" % (left, right))
        rest = sum(l["shares"] for l in pos.get("510300", []))
        check(rest == 800, "剩余数量 %d，应为 800" % rest)


@case("snapshot(write=False) 不碰净值表")
def t_snapshot_nowrite():
    with sandbox():
        acc = broker.load_account()
        acc["today"] = "2026-10-16"
        broker.save_account(acc)
        broker.snapshot(acc, {}, note="昨天")
        before = open(broker.EQUITY_FILE, encoding="utf-8-sig").read()
        broker.snapshot(acc, {}, note="dry", write=False)
        after = open(broker.EQUITY_FILE, encoding="utf-8-sig").read()
        check(before == after, "write=False 仍然改了文件（dry 污染回归）")


@case("runner --dry 不改任何账本文件")
def t_dry_no_side_effect():
    with sandbox():
        broker.load_account()
        day = today()
        px = flat_prices(242, 4.0, -0.02)
        ticks = mk_ticks(day, px)
        q = {"510300": mk_quote("510300", px[-1])}
        old = install_fake_market(
            {"510300": {"ticks": ticks, "preclose": 4.0, "name": "沪深300ETF"}}, q)
        try:
            snap_before = {f: open(os.path.join(runner.STATE, f), "rb").read()
                           for f in ("account.json", "trades.csv", "equity.csv")
                           if os.path.exists(os.path.join(runner.STATE, f))}
            runner.run(verbose=False, execute=False)
            snap_after = {f: open(os.path.join(runner.STATE, f), "rb").read()
                          for f in ("account.json", "trades.csv", "equity.csv")
                          if os.path.exists(os.path.join(runner.STATE, f))}
        finally:
            restore_market(old)
        check(snap_before == snap_after, "dry 运行改动了账本文件（回归）")


@case("幂等：同一天跑两次，账户完全一致")
def t_idempotent():
    with sandbox():
        broker.load_account()
        day = today()
        n = len(session_minutes())
        # 早盘下跌制造偏离，中段反弹触发买入，尾盘拉升触发止盈
        px = []
        for k in range(n):
            if k < 40:
                px.append(4.0 - 0.0009 * k)
            elif k < 60:
                px.append(px[-1] + 0.0016)
            else:
                px.append(px[-1] + 0.00012)
        ticks = mk_ticks(day, px)
        q = {"510300": mk_quote("510300", px[-1])}
        old = install_fake_market(
            {"510300": {"ticks": ticks, "preclose": 4.0, "name": "沪深300ETF"}}, q)
        try:
            r1 = runner.run(verbose=False, execute=True)
            a1 = broker.load_account()
            r2 = runner.run(verbose=False, execute=True)
            a2 = broker.load_account()
        finally:
            restore_market(old)
        check(a1["cash"] == a2["cash"], "两次运行现金不同：%.2f vs %.2f"
              % (a1["cash"], a2["cash"]))
        check(a1["trade_seq"] == a2["trade_seq"], "两次运行流水条数不同")
        check(len(r1["events"]) == len(r2["events"]), "两次运行事件数不同")


@case("过夜仓：不开新仓，且按日内规则平掉")
def t_overnight():
    with sandbox():
        acc = broker.load_account()
        seed_position(acc, "510300", 1000, 4.0, days_before(7))
        day = today()
        n = len(session_minutes())
        px = flat_prices(n, 4.0, -0.05)          # 一路阴跌，制造买入诱惑
        ticks = mk_ticks(day, px)
        q = {"510300": mk_quote("510300", px[-1]),
             "510500": mk_quote("510500", 6.7, 7.0)}
        ticks2 = mk_ticks(day, flat_prices(n, 6.7, -0.05))
        old = install_fake_market(
            {"510300": {"ticks": ticks, "preclose": 4.0, "name": "沪深300ETF"},
             "510500": {"ticks": ticks2, "preclose": 7.0, "name": "中证500ETF"}}, q)
        try:
            runner.run(verbose=False, execute=True)
            a = broker.load_account()
        finally:
            restore_market(old)
        check(len(a["positions"]) <= 1, "过夜仓期间又开了第二只：%s" % list(a["positions"]))
        check(not a["positions"], "尾盘没平掉，仓位过夜了：%s" % list(a["positions"]))


@case("尾盘缺数据：兜底强平必须生效")
def t_missing_tail():
    with sandbox():
        acc = broker.load_account()
        seed_position(acc, "510300", 1000, 4.0, days_before(7))
        day = today()
        mins = session_minutes()
        # 持仓标的的 tick 到 14:10 就断了；另一只正常走到 14:55
        cut = next(k for k, (h, m) in enumerate(mins) if h == 14 and m == 10)
        # 价格全程走平：不触发止盈止损，只剩「尾盘强平」这一条出口，
        # 否则会在上午被止损掉，测不到缺数据场景
        ticks = mk_ticks(day, flat_prices(cut + 1, 4.0), mins[:cut + 1])
        ticks2 = mk_ticks(day, flat_prices(cut + 46, 6.7), mins[:cut + 46])
        q = {"510300": mk_quote("510300", 3.9), "510500": mk_quote("510500", 6.7, 7.0)}
        old = install_fake_market(
            {"510300": {"ticks": ticks, "preclose": 4.0, "name": "沪深300ETF"},
             "510500": {"ticks": ticks2, "preclose": 7.0, "name": "中证500ETF"}}, q)
        try:
            # max_hold 放宽，让它不会在上午被时间止损掉，
            # 这样才能测到「尾盘缺数据」这一条路径
            runner.run(verbose=False, execute=True,
                       params=dict(strategy.PARAMS, max_hold_min=400))
            a = broker.load_account()
        finally:
            restore_market(old)
        check(not a["positions"], "缺数据导致仓位没平掉（会过夜）")
        rows = doctor._read_trades()
        check(any(r["side"] == "SELL" and "强平" in r["reason"] for r in rows),
              "兜底强平没触发")


@case("无未来函数：决策不看 i 之后的数据")
def t_no_future():
    day = "2026-10-19"
    n = 120
    px = flat_prices(n, 4.0, -0.03)
    ticks = mk_ticks(day, px)
    bad = 0
    for i in (30, 31, 35, 60, 90):
        ctx = {"trades": 0, "market_pct": 0.0, "preclose": 4.0}
        a = strategy.decide(ticks, i, None, dict(ctx))
        b = strategy.decide(ticks[:i + 1], i, None, dict(ctx))
        if a[0] != b[0] or a[2] != b[2]:
            bad += 1
    check(bad == 0, "%d 个决策点用到了未来数据" % bad)


@case("检查器自测：篡改账本能被抓到")
def t_doctor_selftest():
    fails = doctor.selftest_replay()
    check(not fails, "检查器自测失败：%s" % fails)


# ==========================================================================
def main():
    global VERBOSE
    if "-v" in sys.argv:
        VERBOSE = True

    print("=" * 70)
    print("  端到端自测   %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)
    passed, failed = 0, []
    for name, fn in CASES:
        try:
            fn()
            passed += 1
            print("  [PASS] %s" % name)
        except Exception as e:  # noqa: BLE001
            failed.append((name, str(e)))
            print("  [FAIL] %s" % name)
            print("         → %s" % str(e)[:160])
    print("-" * 70)
    print("  通过 %d / %d" % (passed, len(CASES)))
    if failed:
        print("  失败 %d 项" % len(failed))
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
