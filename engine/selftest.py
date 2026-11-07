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
import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest    # noqa: E402
import analyze     # noqa: E402
import broker      # noqa: E402
import doctor      # noqa: E402
import market
import weekly     # noqa: E402      # noqa: E402
import push_repo   # noqa: E402
import runner      # noqa: E402
import strategy    # noqa: E402

VERBOSE = False
CASES = []
# 用例总数基线（见 main() 里的说明：少了要红，不是"通过 41 项"）。
# 2026-10-19 建 1 条起算，每加一条用例都要同步改这里。
EXPECTED_CASES = 45


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


@case("涨跌停价：四舍五入按交易所口径，封板不误拒")
def t_limit_half_up():
    """内置 round() 是银行家舍入，交易所规则是四舍五入——差一档就会误拒。

    实例：510300 昨收 2.335，涨停价 = 2.335 × 1.1 = 2.5685 → 交易所取 2.569。
    旧代码 `round(2568.5)` 取偶给 2.568，于是「报价 2.568、买方吃一档到 2.569」
    这笔**正好封在涨停价上**的真实成交，被判成「吃一档后 2.569 超过涨停价 2.568」
    而拒单——方向保守，但它是错单：本可以在涨停价成交的仓位没了。
    跌停侧同理（3.045 × 0.9 = 2.7405 → 2.741，旧代码给 2.740）。
    """
    up, down = broker.limit_prices("510300", 2.335)
    check(up == 2.569, "ETF 涨停价算错：%s，应为 2.569" % up)
    check(down == 2.102, "ETF 跌停价算错：%s，应为 2.102" % down)

    # 股票按 0.01 取整：10.15 × 1.1 = 11.165 → 11.17（旧代码 11.16）
    up, down = broker.limit_prices("600000", 10.15)
    check(up == 11.17, "股票涨停价算错：%s，应为 11.17" % up)
    check(down == 9.14, "股票跌停价算错：%s，应为 9.14" % down)

    # 行为验证：报价 2.568 → 吃一档 2.569 恰好等于涨停价，必须成交
    with sandbox():
        acc = broker.load_account()
        dt = datetime(2026, 10, 19, 10, 0)
        r = broker.buy(acc, "510300", "沪深300ETF", 2.568, 100, dt, preclose=2.335)
        check(r["price"] == 2.569, "涨停价上的成交价不对：%s" % r["price"])
        # 而报价 2.569 再吃一档 2.570 就越过涨停价，封板拒单是对的
        try:
            broker.buy(acc, "510300", "沪深300ETF", 2.569, 100, dt, preclose=2.335)
        except broker.Reject as e:
            check("封板" in str(e), "拒绝理由不是封板：%s" % e)
        else:
            check(False, "越过涨停价的成交竟然通过了")


@case("涨跌幅限制：创业板/科创板 ST 仍是 20%，只有主板 ST 是 5%")
def t_limit_ratio_board():
    """2020 年创业板注册制后，《创业板交易特别规定》把创业板股票
    （含风险警示股票）的涨跌幅统一为 20%；科创板同理。

    旧代码先判 `"ST" in name` 再判板块，创业板 ST 被算成 5%：涨停价按
    昨收 ×1.05 封，实际可以涨到 1.20 —— 池子换成创业板个股后，一笔
    20% 涨停日的合法买入会被拒，跌停价则会算高一倍，凭空报出「已跌停」。
    """
    check(broker.limit_ratio("300750", "ST宁德") == 0.20, "创业板 ST 应为 20%")
    check(broker.limit_ratio("301001", "*ST某某") == 0.20, "创业板 ST 应为 20%")
    check(broker.limit_ratio("688001", "ST科创") == 0.20, "科创板 ST 应为 20%")
    check(broker.limit_ratio("600000", "ST某某") == 0.05, "主板 ST 应为 5%")
    check(broker.limit_ratio("830799", "ST北交") == 0.30, "北交所风险警示股应为 30%")
    check(broker.limit_ratio("510300", "沪深300ETF") == 0.10, "宽基 ETF 应为 10%")
    check(broker.limit_ratio("159915", "创业板ETF") == 0.20, "创业板 ETF 应为 20%")


@case("买入数量：小数股/布尔被拒，整数 float 归一")
def t_buy_shares_type():
    """`shares=100.5` 曾被 `% 100` 放过去，账本里出现半份持仓，
    之后所有成本、可卖数量、估值全部跟着脏；`True` 更隐蔽（bool 是 int）。
    runner 里 `int(budget / px / 100) * 100` 本身是 int，但 100.0 这种
    整数值 float 不该被拒——两种都要覆盖。
    """
    with sandbox():
        acc = broker.load_account()
        dt = datetime(2026, 10, 19, 10, 0)
        cash = acc["cash"]
        for bad in (100.5, True, 0, -100):
            try:
                broker.buy(acc, "510300", "a", 4.0, bad, dt)
            except broker.Reject:
                pass
            else:
                raise AssertionError("%r 买入竟然成交" % (bad,))
        check(broker.load_account()["cash"] == cash, "被拒的买入动过现金")
        r = broker.buy(acc, "510300", "a", 4.0, 100.0, dt)
        check(r["shares"] == 100, "整数值 float 被误拒或归一失败：%r" % (r["shares"],))
        # 归一必须落到账本上：旧实现会把 100.0 原样写进 lots，
        # 于是一个「份数」在账户里是 float，之后任何 int 比较都要靠运气。
        lot = broker.load_account()["positions"]["510300"]["lots"][0]
        check(type(lot["shares"]) is int,
              "账本里的份数不是 int：%r" % (lot["shares"],))


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


@case("持有型：过夜仓必须保留，且不能开第二只")
def t_overnight():
    """mode="hold" 下仓位就是要过夜，这是设计目标，不是故障。

    2026-10-28 换策略前，这条断言是「尾盘必须平掉」——那时策略是日内。
    换结构后如果还这么断言，测试就在拒绝正确行为，所以同步改成
    「持有必须保持」。同时仍然守住老 bug：不能因为空仓判断错而开第二只。
    """
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
            rows = doctor._read_trades()
        finally:
            restore_market(old)
        check(len(a["positions"]) <= 1,
              "过夜仓期间又开了第二只：%s" % list(a["positions"]))
        check("510300" in a["positions"],
              "持有型策略把仓位平掉了，等于退回日内：%s" % list(a["positions"]))
        check(not any(r["side"] == "SELL" for r in rows),
              "持有型策略不该产生任何卖出：%s"
              % [r["reason"] for r in rows if r["side"] == "SELL"])


@case("日内模式仍可切换：daytrade 下兜底强平必须生效")
def t_missing_tail():
    """切到 hold 之后，旧的日内能力不能丢。

    这是给未来的自己留的回归闸门：如果哪天想切回日内（或跑对照实验），
    尾盘强平必须还在。两处强平（循环内 + 收盘兜底）都要覆盖到，
    只改一处就会出现「白天平了、缺数据时过夜」的半吊子状态。
    """
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
                       params=dict(strategy.PARAMS, mode="daytrade",
                                   max_hold_min=400))
            a = broker.load_account()
        finally:
            restore_market(old)
        check(not a["positions"], "缺数据导致仓位没平掉（会过夜）")
        rows = doctor._read_trades()
        check(any(r["side"] == "SELL" and "强平" in r["reason"] for r in rows),
              "兜底强平没触发")


@case("回测：跌停封板不能假装卖出")
def t_bt_limit_down():
    """2026-10-28 补：回测原来不校验涨跌停。

    一字跌停日照样按「让一档」成交，等于让回测在最极端的行情里拿到
    实盘拿不到的价格——系统性低估尾部亏损。这里把 decide 换成桩，
    只测撮合层：策略怎么想的不该影响「跌停到底能不能卖出」这个事实。
    """
    n = len(session_minutes())
    d1, d2 = "2026-10-19", "2026-10-20"
    data = {"510050": {"name": "上证50ETF华夏",
                       "days": {d1: mk_ticks(d1, flat_prices(n, 4.0)),
                                d2: mk_ticks(d2, [3.60] * n)},
                       "preclose": {d1: 4.0, d2: 4.0},
                       "first_day_no_preclose": d1}}

    def fake_decide(ticks, i, pos, ctx, params, can_sell=True):
        if pos is None:
            return "BUY", "测试入场", {"dev": -0.01}
        if can_sell:
            return "SELL", "测试出场", {}
        return "HOLD", "", {}

    old = strategy.decide
    strategy.decide = fake_decide
    try:
        # 用 daytrade 跑：这条用例测的是「撮合层能不能被跌停挡住」，
        # 与持有型选哪只票无关。若沿用 PARAMS 的 mode="hold"，
        # hold_target 会把它锁在 510880 上，数据里没有 510880 就不建仓，
        # 用例会以「没构造出入场」失败——那测的是另一件事。
        r = backtest.run_backtest(data, dict(strategy.PARAMS, mode="daytrade"),
                                  [d1, d2])
    finally:
        strategy.decide = old

    check(any(t["side"] == "BUY" for t in r["trades"]), "用例没构造出入场，测不到卖出")
    check(not [t for t in r["trades"] if t["side"] == "SELL"],
          "一字跌停日居然成交了卖出（实盘是 Reject，仓位会封在里面）")
    check(r["open_pos"] is not None, "跌停卖不掉，仓位应当还在，不该凭空消失")
    check(any("跌停" in b["reason"] for b in r["blocked"]),
          "跌停拒单没有留痕，等于又变成静默失败")

    # 涨停买不进：再吃一档就超出涨停价
    why = backtest._limit_block("510050", "上证50ETF华夏", 4.0, "BUY", 4.40, 4.401)
    check(why and "涨停" in why, "涨停封板买入没被拒")
    # 20% 品种不能按 10% 判，否则大涨日照样误拒（broker 里踩过同一个坑）
    why2 = backtest._limit_block("588000", "科创50ETF华夏", 4.0, "BUY", 4.40, 4.401)
    check(why2 is None, "588000 是 20% 品种，涨停价 4.80，4.40 不该被拒")
    why3 = backtest._limit_block("510050", "上证50ETF华夏", 4.0, "BUY", 4.00, 4.001)
    check(why3 is None, "正常价被误拒")


@case("回测：标的缺数据要能报出来")
def t_bt_load_report():
    rep = {}
    data = backtest.load_pool(["510300", "999999"], 5, verbose=False,
                              offline=True, report=rep)
    check(rep.get("requested") == 2, "report 没回填 requested")
    check(rep.get("ok") == len(data), "report 的 ok 和实际返回不一致")
    check(any(c == "999999" for c, _ in (rep.get("failed") or [])),
          "不存在的代码没进 failed，调用方无从知道池子不完整")


@case("回测：持有型必须只买 hold_target")
def t_bt_hold_target():
    """2026-10-29 发现的静默错误：回测买入的不是实盘那只 ETF。

    strategy.decide() 的签名里没有标的，hold 分支只要过了 hold_entry_time
    就返回 BUY——「买哪只」只能由调用方钉住。runner 钉了（scan=[hold_target]），
    回测没钉，于是买 data 里第一只：CORE_POOL 顺序下是 510300，而实盘买 510880。
    两只票三年差 30 个百分点，而回归探针一路报「与基线一致」。
    """
    n = len(session_minutes())
    d1, d2 = "2026-10-19", "2026-10-20"

    def mk(c, px):
        return {"name": c,
                "days": {d1: mk_ticks(d1, flat_prices(n, px)),
                         d2: mk_ticks(d2, flat_prices(n, px))},
                "preclose": {d1: px, d2: px}, "first_day_no_preclose": d1}

    # 510300 故意排在 dict 前面：旧代码会优先买它
    data = {"510300": mk("510300", 4.0), "510880": mk("510880", 3.5)}
    p = dict(strategy.PARAMS, mode="hold", hold_target="510880")
    r = backtest.run_backtest(data, p, [d1, d2])
    buys = [t for t in r["trades"] if t["side"] == "BUY"]
    check(len(buys) == 1, "持有型应当只建仓一次，实际 %d 次" % len(buys))
    check(buys and buys[0]["code"] == "510880",
          "买成了 %s，不是 hold_target" % (buys[0]["code"] if buys else "无"))

    # 标的缺数据时必须留痕：以前「没数据」和「今天不买」长得一模一样
    r2 = backtest.run_backtest({"510300": mk("510300", 4.0)}, p, [d1])
    check(any(b.get("code") == "510880" for b in r2["blocked"]),
          "hold_target 缺数据没有留痕，等于静默不建仓")


@case("回测：持有型熔断后要冷却，不能当日买回")
def t_bt_hold_cooldown():
    """熔断只有配上冷却才是「离场」。

    runner 在卖出后写 acc["hold_paused_until"]，回测没有对应物：
    熔断当天下一分钟就买回来，回测里的 -25% 变成「多交两次手续费」。
    """
    seq = [("2026-10-19", 4.00), ("2026-10-20", 3.64), ("2026-10-21", 3.31),
           ("2026-10-22", 3.01), ("2026-10-23", 2.74), ("2026-10-26", 2.74)]
    n = len(session_minutes())
    days = [d for d, _ in seq]
    data = {"510880": {"name": "红利ETF",
                       "days": {d: mk_ticks(d, flat_prices(n, px))
                                for d, px in seq},
                       "preclose": {d: px for d, px in seq},
                       "first_day_no_preclose": days[0]}}
    p = dict(strategy.PARAMS, mode="hold", hold_target="510880")
    r = backtest.run_backtest(data, p, days)
    sells = [t for t in r["trades"] if t["side"] == "SELL"]
    buys = [t for t in r["trades"] if t["side"] == "BUY"]
    check(len(sells) == 1 and "熔断" in sells[0]["reason"],
          "没触发熔断：%s" % [(t["day"], t["reason"]) for t in sells])
    check(len(buys) == 1 and buys[0]["day"] == days[0],
          "熔断后在冷却期内又建仓了：%s" % [(t["day"]) for t in buys])


@case("持有型：09:35 就该建仓，不该被 22 根预热门槛推到 09:52")
def t_hold_entry_time():
    """22 根预热门槛是给日内指标用的（vol_ratio 要 20 根）。

    它套在持有型头上没有任何理由，后果是 hold_entry_time="09:35" 实际变成
    09:52——参数写的和实际做的不一样，而且永远不会报错。
    """
    with sandbox():
        day = today()
        n = len(session_minutes())
        px = flat_prices(n, 3.5)
        ticks = mk_ticks(day, px)
        q = {"510880": mk_quote("510880", 3.5, 3.5)}
        old = install_fake_market(
            {"510880": {"ticks": ticks, "preclose": 3.5, "name": "红利ETF"}}, q)
        try:
            runner.run(verbose=False, execute=True,
                       params=dict(strategy.PARAMS, mode="hold",
                                   hold_target="510880"))
            rows = doctor._read_trades()
        finally:
            restore_market(old)
        buys = [r for r in rows if r["side"] == "BUY"]
        check(len(buys) == 1, "应当建仓一次，实际 %d 次" % len(buys))
        hm = buys[0]["datetime"][11:16] if buys else "无"
        check("09:35" <= hm <= "09:40", "建仓时间 %s，偏离 hold_entry_time" % hm)


@case("盘中换策略：必须拒绝重放，不许用早盘价格执行新决定")
def t_strategy_changed():
    """2026-10-28 真实事故的回归闸门。

    那天上午按旧策略止损卖出，下午改完持有型策略再触发云端，
    引擎从 09:52 重放建仓：账面上像是提前几小时知道要买（前视偏差），
    同时把早上真实发生的止损重放掉了（成交 10 笔退回 9 笔）。

    幂等重放的前提是「策略不变」。变了就必须拦，宁可当天不交易。
    """
    with sandbox():
        day = today()
        n = len(session_minutes())
        px = flat_prices(n, 4.0, -0.03)
        ticks = mk_ticks(day, px)
        q = {"510300": mk_quote("510300", px[-1])}
        old = install_fake_market(
            {"510300": {"ticks": ticks, "preclose": 4.0, "name": "沪深300ETF"}}, q)
        try:
            p1 = dict(strategy.PARAMS, mode="daytrade", entry_enabled=True,
                      dev_enter=0.002, rebound=0.0, mom_turn=-1.0)
            runner.run(verbose=False, execute=True, params=p1)
            before = broker.load_account()
            n_before = len(open(broker.TRADE_FILE, encoding="utf-8-sig").readlines())

            # 盘中把策略改成持有型并指向另一只标的
            p2 = dict(p1, mode="hold", hold_target="510880")
            r = runner.run(verbose=False, execute=True, params=p2)
            after = broker.load_account()
            n_after = len(open(broker.TRADE_FILE, encoding="utf-8-sig").readlines())
        finally:
            restore_market(old)
        check(r is None, "策略变更后仍然执行了重放（返回了结果）")
        check(runner.LAST_SKIP == "strategy_changed",
              "跳过原因不对：%r" % runner.LAST_SKIP)
        check(n_before == n_after,
              "重放改写了当日成交：%d 行 → %d 行" % (n_before, n_after))
        check(before["cash"] == after["cash"],
              "重放改写了现金：%.2f → %.2f" % (before["cash"], after["cash"]))


@case("回测：大盘涨跌要和决策同一分钟，不能提前一分钟看到下跌")
def t_bt_market_pct_no_lookahead():
    """2026-11-05 发现：ctx["market_pct"] 取的是「成交那一分钟」的指数价，
    而 decide 读的是 ticks[k-1]，于是 09:58 的决策里已经含着 09:59 的暴跌。

    实测：让 510300 在 09:59 才崩到 -2.94%，09:58 那一格的 market_pct
    就已经是 -2.94%。这里改成每分钟涨 0.1%，每一分钟的 market_pct 都唯一，
    再逐条核对「决策那一刻看到的数」是否等于「那一分钟的指数价」。
    """
    n = 30
    d1, d2 = "2026-10-19", "2026-10-20"
    idx_px = [3.40 * (1 + 0.001 * k) for k in range(n)]
    data = {
        "510300": {"name": "沪深300ETF",
                   "days": {d1: mk_ticks(d1, flat_prices(5, 3.40)),
                            d2: mk_ticks(d2, idx_px)},
                   "preclose": {d1: 3.40, d2: 3.40}, "first_day_no_preclose": d1},
        "510880": {"name": "红利ETF",
                   "days": {d1: mk_ticks(d1, flat_prices(5, 3.40)),
                            d2: mk_ticks(d2, flat_prices(n, 3.40))},
                   "preclose": {d1: 3.40, d2: 3.40}, "first_day_no_preclose": d1},
    }
    seen = []

    def spy(ticks, i, pos, ctx, params=strategy.PARAMS, can_sell=True):
        if ctx.get("preclose"):
            seen.append((ticks[i]["t"], round(ctx.get("market_pct", 0.0), 4)))
        return "HOLD", "", {}

    old = strategy.decide
    strategy.decide = spy
    try:
        backtest.run_backtest(data, dict(strategy.PARAMS, mode="daytrade",
                                         entry_enabled=True), [d1, d2])
    finally:
        strategy.decide = old

    expect = {t["t"]: round((t["price"] / 3.40 - 1) * 100, 4)
              for t in data["510300"]["days"][d2]}
    check(seen, "用例没触发任何决策点")
    bad = [(t, p, expect[t]) for t, p in seen
           if t in expect and abs(p - expect[t]) > 1e-6]
    check(not bad, "决策看到的大盘涨跌不是同一分钟（决策时刻, 看到, 应为）：%s"
                   % bad[:3])


@case("回测：持有型标的当日停牌不能静默不建仓")
def t_bt_hold_target_suspended():
    """池子里有这只、但这一天整日没有一根 tick（停牌 / 数据缺口）。

    以前回测整天不动、blocked 为空，报告读起来就是「策略今天不想买」。
    注意这两个字要分得清：缺数据（不在池里）vs 停牌（在池里但没 tick）。
    """
    n = len(session_minutes())
    d1, d2 = "2026-10-19", "2026-10-20"

    def mk(day, px):
        return mk_ticks(day, flat_prices(n, px))

    # 510880 只有 d2 有数据，d1 整日停牌
    data = {"510880": {"name": "红利ETF", "days": {d2: mk(d2, 3.5)},
                       "preclose": {d1: 3.5, d2: 3.5},
                       "first_day_no_preclose": d1}}
    p = dict(strategy.PARAMS, mode="hold", hold_target="510880")
    r = backtest.run_backtest(data, p, [d1, d2])
    buys = [t for t in r["trades"] if t["side"] == "BUY"]
    check(len(buys) == 1 and buys[0]["day"] == d2,
          "停牌日不该建仓：%s" % [(t["day"], t["side"]) for t in r["trades"]])
    rec = [b for b in r["blocked"] if b["day"] == d1]
    check(rec, "停牌日没有留痕，等于把「没数据」报成「策略不买」")
    check("停牌" in rec[0]["reason"], "留痕原因不清楚：%s" % rec[0]["reason"])


@case("策略：时间止损按真实分钟，不按第几根K线")
def t_hold_minutes_not_bars():
    """跨午休时 11:30 与 13:01 在索引上相邻，实际差 91 分钟。

    「持有 20 分钟」若按索引算，11:25 建仓要拖到 13:15 才触发（真实 110 分钟），
    日志上却仍写着「时间止损(20分钟)」。analyze.py 2026-11-04 抓到同一类错
    （60 分档里 8 个样本实际是 150 分钟），这里是它的上游。
    """
    mins = []
    for h, m0, m1 in ((11, 0, 30), (13, 0, 60)):
        for m in range(m0, m1):
            mins.append((h, m))
    d = "2026-10-19"
    ticks = [{"t": "%s %02d:%02d" % (d, h, m), "price": 3.40,
              "vol": 100.0, "avg": 3.40} for h, m in mins]
    ctx = {"trades": 0, "market_pct": 0.0, "preclose": 3.40}
    p = dict(strategy.PARAMS, mode="daytrade", entry_enabled=True,
             max_hold_min=20, take_profit=0.5, stop_loss=0.5)

    # 不跨午休的对照：11:00 建仓，11:10 才持有 10 分钟 → 不该平
    a = strategy.decide(ticks, 10, {"entry": 3.40, "entry_idx": 0}, dict(ctx), p)
    check(a[0] == "HOLD", "持有 10 分钟就触发了时间止损：%s" % (a,))
    b = strategy.decide(ticks, 20, {"entry": 3.40, "entry_idx": 0}, dict(ctx), p)
    check(b[0] == "SELL" and "时间止损" in b[1], "持有 20 分钟该平：%s" % (b,))

    # 跨午休：11:25 建仓，13:00 已经真实持有 95 分钟 → 必须平（索引差只有 5）
    c = strategy.decide(ticks, 30, {"entry": 3.40, "entry_idx": 25}, dict(ctx), p)
    check(c[0] == "SELL" and "时间止损" in c[1],
          "跨午休的持有期还在按 K 线根数算：%s" % (c,))
    check("95分钟" in c[1], "持有分钟数报错了：%s" % c[1])


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


@case("护栏：改任何一个策略参数都必须让当日指纹变，否则会前视重放")
def t_params_sig_covers_every_key():
    """2026-11-07 的真实漏洞：指纹用的是手写白名单，漏了 hold_cooldown_days /
    min_amount / min_price——三个真影响成交的参数。改了它们，当日护栏不响，
    引擎会拿新参数把当天从 09:30 重放一遍（用早盘价格执行刚刚才做出的决定）。
    这里逐个扰动 PARAMS 的每一个键：只要有一个键扰动后指纹不变，就是漏网。"""
    base = dict(strategy.PARAMS)

    def mutate(v):
        if isinstance(v, bool):
            return not v
        if isinstance(v, (int, float)):
            return v + 1
        if isinstance(v, str):
            return v + "_x"
        return "__changed__"

    ref = runner.params_sig(base)
    missed = []
    for k, v in base.items():
        p = dict(base)
        p[k] = mutate(v)
        if runner.params_sig(p) == ref:
            missed.append(k)
    check(not missed, "这些参数改了但当日指纹不变（护栏会放行前视重放）：%s" % missed)
    # 键顺序不该影响指纹（dict 排序后哈希），否则改一处书写顺序就白停一天
    rev = {k: base[k] for k in reversed(list(base))}
    check(runner.params_sig(rev) == ref, "指纹受键顺序影响了，等价参数会误拦")


@case("云端退出码：没报价=3、有报价没过门槛=6，不能都报成数据源故障")
def t_cloud_pool_vs_no_quotes():
    """「接口全挂」和「早盘成交额没到门槛」原来都是 no_pool → 退出码 3 →
    workflow 每天开盘准时打一条「数据源可能故障」。狼来了喊久了，真故障那天
    就和日常噪音长得一样。这里在沙箱里用假报价把两条路分别走一遍。"""
    import cloud_run
    day = datetime.now().strftime("%Y-%m-%d")
    old_q, old_win = market.fetch_quotes, cloud_run.in_window
    code_map = {}
    try:
        with sandbox():
            market.fetch_quotes = lambda codes, **k: {}
            runner.run(verbose=False, execute=False)
            code_map["skip_no_quotes"] = runner.LAST_SKIP

            low = {c: dict(mk_quote(c, 3.0), amount=1e6) for c in strategy.CORE_POOL}
            market.fetch_quotes = lambda codes, **k: low
            runner.run(verbose=False, execute=False)
            code_map["skip_pool_filtered"] = runner.LAST_SKIP

        cloud_run.in_window = lambda now=None: (True, "10:00")
        old_run = runner.run
        try:
            runner.run = lambda **k: None
            runner.LAST_SKIP = "no_quotes"
            code_map["code_no_quotes"] = cloud_run.main([])
            runner.LAST_SKIP = "pool_filtered"
            code_map["code_pool_filtered"] = cloud_run.main([])
        finally:
            runner.run = old_run
    finally:
        market.fetch_quotes, cloud_run.in_window = old_q, old_win

    check(code_map["skip_no_quotes"] == "no_quotes",
          "一只报价都没有时应报 no_quotes，实际 %s" % code_map["skip_no_quotes"])
    check(code_map["skip_pool_filtered"] == "pool_filtered",
          "有报价但没过门槛应报 pool_filtered，实际 %s" % code_map["skip_pool_filtered"])
    check(code_map["code_no_quotes"] == 3,
          "没报价是数据源故障，应返回 3，实际 %s" % code_map["code_no_quotes"])
    check(code_map["code_pool_filtered"] == 6,
          "没过门槛属正常跳过，应返回 6，实际 %s" % code_map["code_pool_filtered"])


# ==========================================================================
def main():
    global VERBOSE
    if "-v" in sys.argv:
        VERBOSE = True

    print("=" * 70)
    print("  端到端自测   %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)
    # 用例总数是硬基线，必须等于 EXPECTED_CASES。2026-11-06 的真实事故：
    # 一次编辑吞掉了某条用例的 `@case(...)` 和 `def` 两行，函数体退化成
    # 上一支用例的尾巴——**编译通过、导入通过、跑完不报错**，这条用例就此
    # 消失。自测照旧「全部通过」，只是从 42 变成 41，而 doctor 只数 [PASS]，
    # 它没有任何办法知道"应该有 42 个"。**少一条比红一条危险得多**。
    # 所以这里钉死数量：故意增删用例时，改这个常数就是一次显式确认。
    if len(CASES) != EXPECTED_CASES:
        print("  [FAIL] 用例登记：应有 %d 条，实际注册 %d 条（用例被吞或未登记）"
              % (EXPECTED_CASES, len(CASES)))
        print("-" * 70)
        print("  通过 0 / %d" % EXPECTED_CASES)
        print("  失败 1 项")
        print("=" * 70)
        return 1
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


@case("腾讯K线：字段顺序是 开/收/高/低（不是 OHLC）")
def t_tx_row_order():
    """腾讯 K 线行的字段顺序反直觉：[时间, 开, 收, 高, 低, 量]。

    这不是「另一种写法」，抄成 OHLC 会把收盘价当最高价用，
    回撤、跳空、止盈止损全部偏移，而且不报错、曲线也画得出来。
    """
    row = market._tx_row(
        ["2026-10-28", "3.400", "3.383", "3.412", "3.355", "100"], False)
    check(row is not None, "行解析返回 None")
    check(abs(row["open"] - 3.400) < 1e-9, "open 取错列：%s" % row["open"])
    check(abs(row["close"] - 3.383) < 1e-9, "close 取错列：%s" % row["close"])
    check(abs(row["high"] - 3.412) < 1e-9, "high 取错列：%s" % row["high"])
    check(abs(row["low"] - 3.355) < 1e-9, "low 取错列：%s" % row["low"])
    check(row["high"] >= row["low"], "high/low 反了：%s/%s" % (row["high"], row["low"]))
    # 分钟线：时间戳是 YYYYMMDDhhmm，要切成 datetime 与 date
    m = market._tx_row(
        ["202610281500", "3.376", "3.383", "3.393", "3.375", "1236706.0"], True)
    check(m["datetime"] == "2026-10-28 15:00", "分钟时间解析错：%s" % m.get("datetime"))
    check(m["date"] == "2026-10-28", "分钟日期解析错：%s" % m.get("date"))


@case("腾讯K线：首个入口失败必须自动换下一个")
def t_tx_fallback():
    """三个入口是同一后端、限流各自独立，任何一个挂了都要静默降级。

    单入口实测约 600 次后返回空 JSON，若不做降级，复盘脚本会在
    「数据抓了一半」的地方停住——这比直接报错更危险。
    """
    import json as _json

    class _Resp:
        def __init__(self, txt):
            self._txt = txt

        def read(self):
            return self._txt.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def __init__(self):
            self.hosts = []

        def open(self, req, timeout=None):
            host = req.full_url.split("/appstock")[0]
            self.hosts.append(host)
            if len(self.hosts) == 1:
                raise OSError("模拟入口被限流")
            return _Resp(_json.dumps({"data": {"sh510880": {"day": [
                ["2026-10-28", "3.400", "3.383", "3.412", "3.355", "100"]]}}}))

    fake = _Opener()
    old_opener = market._TX_OPENER
    old_down = dict(market._TX_HOST_DOWN)
    market._TX_OPENER = fake
    market._TX_HOST_DOWN.clear()
    try:
        rows = market.fetch_kline_tx("510880", "day", "", count=5)
    finally:
        market._TX_OPENER = old_opener
        market._TX_HOST_DOWN.clear()
        market._TX_HOST_DOWN.update(old_down)
    check(len(fake.hosts) == 2, "没有切换入口，只试了 %d 个：%s" % (len(fake.hosts), fake.hosts))
    check(len(rows) == 1 and abs(rows[0]["close"] - 3.383) < 1e-9,
          "降级后数据不对：%s" % rows)


@case("报价降级：腾讯与东财都挂时要能用本地快照，不能直接抛错")
def t_quote_snapshot_fallback():
    """2026-10-28 自审：`_stale()` 只认 {"data": {"ticks": …}} 这一种形状，
    而报价缓存是**扁平**的 {code: quote}。于是 fetch_quotes 的第三层降级
    （「退回本地快照」）永远拿不到东西——那句 `[降级] 报价使用缓存快照`
    是不可达代码，两个源都挂时直接抛错，与注释承诺的行为相反。
    """
    tmp = tempfile.mkdtemp(prefix="mkt_")
    old_dir, old_get = market.CACHE_DIR, market._get
    old_tx, old_pref = market.fetch_quotes_tx, market.PREFER_TX_INTRADAY
    codes = ["510300"]
    sig = hashlib.md5("|".join(sorted(codes)).encode()).hexdigest()[:8]
    snapshot = {"510300": mk_quote("510300", 3.999, preclose=4.0)}
    path = os.path.join(tmp, "q_%s_20260101_0930.json" % sig)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f)
    market.CACHE_DIR = tmp
    market.PREFER_TX_INTRADAY = True
    market.fetch_quotes_tx = lambda cs: (_ for _ in ()).throw(RuntimeError("腾讯挂了"))

    def _dead(url, retries=5, timeout=20):
        raise RuntimeError("东财也挂了")

    market._get = _dead
    try:
        q = market.fetch_quotes(codes)
    finally:
        market.CACHE_DIR, market._get = old_dir, old_get
        market.fetch_quotes_tx, market.PREFER_TX_INTRADAY = old_tx, old_pref
        shutil.rmtree(tmp, ignore_errors=True)
    check("510300" in q, "没退回快照，返回了 %s" % q)
    check(abs(q["510300"]["price"] - 3.999) < 1e-9,
          "快照价格不对：%s" % q["510300"].get("price"))


@case("报价：腾讯只给一半时必须补东财，不能把残缺结果当完整")
def t_quote_partial_fill():
    """腾讯返回 200 但其中一只字段不全时，原逻辑 out 非空就当成成功返回，
    缺的那只静默消失；下游 validate_pool 对「没报价」和「不达标」都是
    continue，两者无法区分。"""
    tmp = tempfile.mkdtemp(prefix="mkt_")
    old_dir, old_tx, old_em = market.CACHE_DIR, market.fetch_quotes_tx, market._quotes_em
    market.CACHE_DIR = tmp
    market.fetch_quotes_tx = lambda cs: {"510300": mk_quote("510300", 4.0)}
    market._quotes_em = lambda cs: {"510500": mk_quote("510500", 6.0)}
    try:
        q = market.fetch_quotes(["510300", "510500"])
    finally:
        market.CACHE_DIR, market.fetch_quotes_tx, market._quotes_em = old_dir, old_tx, old_em
        shutil.rmtree(tmp, ignore_errors=True)
    check(set(q) == {"510300", "510500"}, "缺的那只没补齐，只有 %s" % sorted(q))
    check(abs(q["510500"]["price"] - 6.0) < 1e-9, "补来的价格不对")


@case("分时降级：用旧缓存必须带 stale 标记，否则故障会被伪装成正常数据")
def t_trends_degrade_marked():
    tmp = tempfile.mkdtemp(prefix="mkt_")
    old_dir, old_get = market.CACHE_DIR, market._get
    old_tx, old_pref = market.fetch_trends_tx, market.PREFER_TX_INTRADAY
    yday = days_before(1)
    blob = {"_ts": 0, "data": {"code": "510300", "name": "沪深300ETF",
                               "preclose": 4.0,
                               "ticks": mk_ticks(yday, flat_prices(5, 4.0))}}
    with open(os.path.join(tmp, "trends_510300_1_20260101.json"), "w",
              encoding="utf-8") as f:
        json.dump(blob, f)
    market.CACHE_DIR = tmp
    market.PREFER_TX_INTRADAY = False
    market.fetch_trends_tx = lambda c: None

    def _dead(url, retries=5, timeout=20):
        raise RuntimeError("接口挂了")

    market._get = _dead
    try:
        tr = market.fetch_trends("510300", ndays=1, ttl=0)
    finally:
        market.CACHE_DIR, market._get = old_dir, old_get
        market.fetch_trends_tx, market.PREFER_TX_INTRADAY = old_tx, old_pref
        shutil.rmtree(tmp, ignore_errors=True)
    check(tr.get("stale") is True, "降级数据没有 stale 标记")
    check((tr.get("as_of") or "").startswith(yday),
          "as_of 应指向缓存里的那天 %s，实际 %s" % (yday, tr.get("as_of")))


@case("交易日守卫：只有旧缓存时算数据故障，不算非交易日")
def t_stale_is_fault_not_holiday():
    """守卫原来只看「tick 日期是不是今天」，两个完全不同的原因报同一句话：
    日历原因（周末/节假日）和数据源故障。前者退出码 2 正常跳过，后者是故障。
    混在一起 → 接口挂一整天，workflow 全绿、日志干净。"""
    yq = {"510300": mk_quote("510300", 4.0)}
    yday = days_before(1)
    with sandbox():
        broker.load_account()
        ticks = mk_ticks(yday, flat_prices(20, 4.0))
        old = install_fake_market(
            {"510300": {"ticks": ticks, "preclose": 4.0, "name": "沪深300ETF",
                        "stale": True, "as_of": ticks[-1]["t"]}}, yq)
        try:
            res = runner.run(verbose=False, execute=False)
            reason = runner.LAST_SKIP
        finally:
            restore_market(old)
        check(res is None, "旧缓存不该产出账本")
        check(reason == "stale_data", "跳过原因应为 stale_data，实际 %r" % reason)

    # 反向：正常的旧日期行情（没有 stale 标记）= 真的非交易日，必须仍是原原因
    with sandbox():
        broker.load_account()
        ticks = mk_ticks(yday, flat_prices(20, 4.0))
        old = install_fake_market(
            {"510300": {"ticks": ticks, "preclose": 4.0, "name": "沪深300ETF"}}, yq)
        try:
            runner.run(verbose=False, execute=False)
            reason = runner.LAST_SKIP
        finally:
            restore_market(old)
        check(reason == "not_trading_day", "非交易日原因被改坏了：%r" % reason)


@case("云端退出码：策略当日变更 → 5，不混进数据故障 3")
def t_cloud_strategy_changed_code():
    import cloud_run
    old_run, old_skip, old_win = runner.run, runner.LAST_SKIP, cloud_run.in_window
    try:
        cloud_run.in_window = lambda now=None: (True, "10:00")
        runner.run = lambda **k: None
        runner.LAST_SKIP = "strategy_changed"
        code_changed = cloud_run.main([])
        runner.LAST_SKIP = "stale_data"
        code_stale = cloud_run.main([])
        runner.LAST_SKIP = "not_trading_day"
        code_holiday = cloud_run.main([])
    finally:
        runner.run, runner.LAST_SKIP, cloud_run.in_window = old_run, old_skip, old_win
    check(code_changed == 5, "策略变更加护栏应返回 5，实际 %s" % code_changed)
    check(code_stale == 3, "旧缓存应算数据故障 3，实际 %s" % code_stale)
    check(code_holiday == 2, "非交易日应仍是 2，实际 %s" % code_holiday)


@case("同步脚本：缺文件不能报「一致」，写入默认被锁")
def t_sync_cloud_states():
    """三条语义，每一条都对应一次「安静的错结论」。

    1. 本地少了 trades.csv，云端也没有 → 旧代码算出 None == None = 「无差异」→
       打印「一致」。账本根本没在同步，而指令是「什么都不用做」。
    2. 云端副本目录不在 → 旧代码也打印 NEED_PUBLISH=0，和「一致」长得一样。
       10-31 时那个副本停在 10-28（空仓）却天天报 0。
    3. 写入方向在新拓扑下是反的（本地是只读镜像），必须默认拒绝。
    """
    import io
    from contextlib import redirect_stdout
    import sync_cloud

    tmp = tempfile.mkdtemp(prefix="tsim_sc_")
    src_state = os.path.join(tmp, "src_state")
    src_eng = os.path.join(tmp, "src_engine")
    dst_state = os.path.join(tmp, "dst_state")
    dst_eng = os.path.join(tmp, "dst_engine")
    for d in (src_state, src_eng, dst_state, dst_eng):
        os.makedirs(d)

    def w(path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    for state in (src_state, dst_state):
        w(os.path.join(state, "account.json"), '{"cash": 100.0, "positions": {}}')
        w(os.path.join(state, "trades.csv"), "time,code,act\n2026-10-30,510880,BUY\n")
        w(os.path.join(state, "equity.csv"), "date,equity\n2026-10-30,100.0\n")
    for eng in (src_eng, dst_eng):
        for n in sync_cloud.SHARED:
            w(os.path.join(eng, n), "# 同一份代码\n")

    saved = (sync_cloud.SRC_STATE, sync_cloud.SRC_ENGINE, sync_cloud.DST_ROOT,
             sync_cloud.DST_STATE, sync_cloud.DST_ENGINE, sync_cloud.MARK)
    saved_argv = sys.argv[:]
    saved_env = os.environ.pop(sync_cloud.WRITE_ENV, None)

    def run(argv):
        buf = io.StringIO()
        sys.argv = ["sync_cloud"] + argv
        with redirect_stdout(buf):
            code = sync_cloud.main()
        return code, buf.getvalue()

    try:
        sync_cloud.SRC_STATE, sync_cloud.SRC_ENGINE = src_state, src_eng
        sync_cloud.DST_ROOT = tmp
        sync_cloud.DST_STATE, sync_cloud.DST_ENGINE = dst_state, dst_eng
        sync_cloud.MARK = os.path.join(dst_state, ".published")

        # 1) 两边都缺 trades.csv —— 「都没有」不是「一致」
        os.remove(os.path.join(src_state, "trades.csv"))
        os.remove(os.path.join(dst_state, "trades.csv"))
        code, out = run(["--check"])
        check(code == 6, "缺文件应返回 6，实际 %s" % code)
        check("NEED_PUBLISH=UNKNOWN" in out, "缺文件时必须给 UNKNOWN，实际输出：%s" % out)
        check("NEED_PUBLISH=0" not in out, "缺文件被报成了「一致」：%s" % out)
        w(os.path.join(src_state, "trades.csv"), "time,code,act\n2026-10-30,510880,BUY\n")
        w(os.path.join(dst_state, "trades.csv"), "time,code,act\n2026-10-30,510880,BUY\n")

        # 2) 云端副本不在 —— 判不了就说判不了，别报 0
        sync_cloud.DST_ROOT = os.path.join(tmp, "nope")
        sync_cloud.DST_STATE = os.path.join(sync_cloud.DST_ROOT, "state")
        sync_cloud.DST_ENGINE = os.path.join(sync_cloud.DST_ROOT, "engine")
        code, out = run(["--check"])
        check(code == 2, "副本不存在应返回 2，实际 %s" % code)
        check("NEED_PUBLISH=0" not in out, "副本不在却报了「无需发布」：%s" % out)
        sync_cloud.DST_ROOT = tmp
        sync_cloud.DST_STATE, sync_cloud.DST_ENGINE = dst_state, dst_eng

        # 3) 本地账本与云端不同 → 默认拒绝写入，且一个字节都不许改
        w(os.path.join(src_state, "account.json"), '{"cash": 50.0, "positions": {}}')
        before = open(os.path.join(dst_state, "account.json"), encoding="utf-8").read()
        code, out = run([])
        after = open(os.path.join(dst_state, "account.json"), encoding="utf-8").read()
        check(code == 7, "默认应拒绝写入并返回 7，实际 %s" % code)
        check(before == after, "拒绝写入时仍然改了云端账本")

        # 4) 显式授权后才真写，且写完结论是「需要发布」
        os.environ[sync_cloud.WRITE_ENV] = "1"
        code, out = run([])
        os.environ.pop(sync_cloud.WRITE_ENV, None)
        after = open(os.path.join(dst_state, "account.json"), encoding="utf-8").read()
        check(code == 0, "授权写入应返回 0，实际 %s" % code)
        check(after == '{"cash": 50.0, "positions": {}}', "授权后没同步账本：%s" % after)
        check("NEED_PUBLISH=1" in out, "同步后应提示重新发布：%s" % out)
    finally:
        (sync_cloud.SRC_STATE, sync_cloud.SRC_ENGINE, sync_cloud.DST_ROOT,
         sync_cloud.DST_STATE, sync_cloud.DST_ENGINE, sync_cloud.MARK) = saved
        sys.argv = saved_argv
        if saved_env is None:
            os.environ.pop(sync_cloud.WRITE_ENV, None)
        else:
            os.environ[sync_cloud.WRITE_ENV] = saved_env
        shutil.rmtree(tmp, ignore_errors=True)


@case("预热：降级/不完整/窗口错开都不能算「成功」")
def t_warmup_states():
    """warm 存在的意义是「把缓存刷到今天」，而 market.fetch_trends 在接口挂掉时
    不抛错，只是**降级**返回更早写下的缓存。原实现把这种返回值一律算成功：
    2026-11-02 实测 13 只标的里 11 只是 10-20 的旧文件，脚本仍打印
    「完成：成功 15 / 失败 0」——唯一的观测点把「数据陈旧」抹掉了，
    而 backtest / weekly / exitstudy 全吃这份缓存。
    """
    import io
    from contextlib import redirect_stdout
    import warmup

    full = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]

    def mk(days, stale=False):
        ticks = [{"t": d + " 09:30", "price": 1.0, "vol": 1.0, "avg": 1.0} for d in days]
        d = {"code": "X", "name": "X", "ticks": ticks}
        if stale:
            d.update({"stale": True, "stale_reason": "接口失败，用更早的缓存"})
        return d

    def run(fake, codes):
        old_t, old_w, old_s = market.fetch_trends, warmup.WAIT_BASE, warmup.SPACING
        market.fetch_trends = fake
        warmup.WAIT_BASE = warmup.SPACING = 0
        try:
            with redirect_stdout(io.StringIO()):
                return warmup.main(codes=codes)
        finally:
            market.fetch_trends = old_t
            warmup.WAIT_BASE, warmup.SPACING = old_w, old_s

    check(run(lambda c, ndays=5, ttl=0: mk(full), ["A", "B"]) == 0,
          "全部新鲜应返回 0")
    check(run(lambda c, ndays=5, ttl=0: mk(full, True), ["A", "B"]) == 2,
          "降级缓存被报成了成功：这正是把旧数据当「已刷新」的那个 bug")
    check(run(lambda c, ndays=5, ttl=0: mk(full[:2]), ["A", "B"]) == 2,
          "日数只有 2/5 却被报成成功")
    check(run(lambda c, ndays=5, ttl=0: mk(full) if c == "A" else mk(full[:1]),
              ["A", "B"]) == 2,
          "各标的窗口错开（10-30 / 10-26）却没报出来")
    out = warmup.warm(codes=[], verbose=False)
    check(out["requested"] == 0,
          "传空列表被当成了整池：requested=%s" % out["requested"])


@case("回测：离线用旧缓存必须标「降级」，窗口错开必须能量出来")
def t_backtest_offline_degraded():
    """离线回测退回「最近一次」缓存是对的（要能断网跑），但原来直接取
    blob["data"]，不带 stale / as_of，于是 report["degraded"] 永远为空：
    2026-11-02 实测 13 只标的全是 10-13~10-30 的旧文件，报告写
    「ok 13/13、degraded 0」，doctor 据此认为数据齐了。

    更重的一条：trading_days() 取各标的日期的并集，不同标的写在不同日期时，
    并集会拼出横跨数周的假窗口，后段只剩一两只标的——回测照跑、指标照算、
    基线照比，全是绿的。这里把两条都钉住。
    """
    tmp = tempfile.mkdtemp(prefix="tsim_cache_")
    old_dir = market.CACHE_DIR
    try:
        market.CACHE_DIR = tmp
        plan = {
            "AAA": ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20"],
            "BBB": ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"],
        }
        for code, ds in plan.items():
            ticks = [{"t": d + " 09:30", "price": 1.0, "vol": 1.0, "avg": 1.0}
                     for d in ds]
            path = os.path.join(tmp, "trends_%s_5_20261020.json" % code)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"_ts": 1, "data": {"code": code, "name": code,
                                              "ticks": ticks}}, f)
        rep = {}
        data = backtest.load_pool(["AAA", "BBB"], ndays=5, verbose=False,
                                  offline=True, report=rep)
        check(len(data) == 2, "旧缓存应能读到，实际 %d 只" % len(data))
        check(len(rep.get("degraded") or []) == 2,
              "离线用旧缓存没标降级，degraded=%r" % (rep.get("degraded"),))
        h = backtest.dataset_health(data, ndays=5)
        check(h["span"] > h["ndays"],
              "10 个交易日被拼成「5 天窗口」却没量出来：span=%s" % h["span"])
        check(h["thin"], "后段只有 1 只标的的交易日没被标记")
    finally:
        market.CACHE_DIR = old_dir
        shutil.rmtree(tmp, ignore_errors=True)


@case("心跳：云端跑过但无成交未提交，不能报「云端没跑」")
def t_cloud_run_evidence():
    """账本提交策略是「有新成交才提交，无成交只在收盘后提交一次」。

    于是「盘中缺今日净值行」是常态，而旧逻辑一律报「云端自动化可能没跑」：
    每个无成交的早上都误报一次，真没跑那天反而淹在噪音里（2026-11-03 实测
    09:32 云端跑成功过一轮，只因当日无成交未提交，体检照样报可能没跑）。
    这里把「从 Actions 记录认出云端跑过」这条证据钉住：只认今天的、
    已完成的、成功的；北京时间要换算对，否则会数成别的日子。
    """
    today = "2026-11-03"
    runs = [
        {"created_at": "2026-11-03T01:32:49Z", "status": "completed",
         "conclusion": "success"},
        {"created_at": "2026-11-03T02:10:00Z", "status": "completed",
         "conclusion": "failure"},
        {"created_at": "2026-11-03T02:20:00Z", "status": "in_progress",
         "conclusion": None},
        {"created_at": "2026-11-02T12:55:42Z", "status": "completed",
         "conclusion": "success"},
    ]
    n, last = doctor.cloud_run_evidence(runs, today)
    check(n == 1, "只该数到今天那 1 轮成功运行，实际 %d 轮" % n)
    check(last == "09:32", "UTC→北京时间换算错了：%s" % last)
    check(doctor.cloud_run_evidence([], today)[0] == 0, "空记录应得 0 轮")
    check(doctor.cloud_run_evidence(None, today)[0] == 0, "None 应得 0 轮")


@case("同步闸门：工作区已等于云端那份，不能算「本地有改动」")
def t_split_conflict():
    """本地推了代码、却因 github.com 不通拉不下来，会留下一种状态：
    工作区字节已经和云端完全一致，但相对 HEAD 是脏的。旧判定只看
    「脏 ∩ 待合入」，于是永远报「云端有提交待同步，但本地有改动」——
    实际一个字节都不会丢，白等一次人工确认（2026-11-04 实测）。

    只有「脏 ∩ 待合入 ∩ 与云端不同」才算真冲突，其余可以让路。
    """
    dirty = {"engine/doctor.py", "engine/selftest.py", "state/baseline.json"}
    incoming = {"engine/doctor.py", "engine/selftest.py"}
    same = {"engine/doctor.py", "engine/selftest.py"}
    block, align = doctor.split_conflict(dirty, incoming, same)
    check(not block, "都被云端版本覆盖了，不该阻塞：%s" % sorted(block))
    check(align == same, "应整体放行，实际 %s" % sorted(align))

    # 真改过（内容与云端不同）的那只必须留下，等着人看
    block2, align2 = doctor.split_conflict(dirty, incoming, {"engine/doctor.py"})
    check(block2 == {"engine/selftest.py"}, "真冲突被放行了：%s" % sorted(block2))
    check(align2 == {"engine/doctor.py"}, "可对齐的没挑出来：%s" % sorted(align2))

    # baseline.json 脏但云端没动它，与本函数无关
    block3, align3 = doctor.split_conflict(dirty, set(), same)
    check(not block3 and not align3, "云端没动的脏文件不该进冲突集")


@case("信号诊断：跨午休的持有期不是持有 h 分钟，必须剔除")
def t_analyze_horizon():
    """分时是一天 241 根，午休前后索引相邻但时间不连续。

    旧写法用 ticks[i + h] 当「h 分钟后」：11:20 的第 60 根是 13:50，
    实际持有 2.5 小时。2026-11-04 实测 60 分钟档 40 个样本里 8 个是这种，
    它们的均值 0.924%（胜率 100%）对 0.178%（胜率 72%）——足以把
    「持有 60 分钟很赚」这个结论凭空造出来，而策略根本拿不到这个收益。
    """
    def mk(times):
        return [{"t": "2026-11-03 " + t, "price": 4.0} for t in times]

    day = mk(["09:58", "09:59", "10:00", "10:01", "10:02", "10:03"])
    check(analyze.same_session_ahead(day, 2, 1), "10:00 到 10:01 是 1 分钟，不该被剔")
    check(analyze.same_session_ahead(day, 2, 3), "10:00 到 10:03 是 3 分钟，应保留")

    # 11:29 的下一根是 13:01，索引相邻、时间差 92 分钟
    lunch = mk(["11:29", "13:01"])
    check(not analyze.same_session_ahead(lunch, 0, 1),
          "11:29 -> 13:01 被当成 1 分钟持有期")

    # 跨日更不行
    cross_day = [{"t": "2026-11-03 14:59", "price": 4.0},
                 {"t": "2026-11-04 09:30", "price": 4.0}]
    check(not analyze.same_session_ahead(cross_day, 0, 1), "跨日样本被当成 1 分钟")

    # 中位数：偶数样本取中间两个的均值，旧写法直接取偏大的那个
    check(analyze._median([1, 2, 3]) == 2, "奇数样本中位数算错")
    check(abs(analyze._median([1, 2, 3, 4]) - 2.5) < 1e-9,
          "偶数样本中位数该是 2.5，旧写法会给 3")

    # 一整天都凑不出一个样本的短分时，不能被当成「可用但没信号」
    check(analyze.MIN_TICKS == 22 + max(analyze.HORIZONS) + 1,
          "MIN_TICKS 应与 22 预热 + 最长持有期 + 1 对齐，实际 %d" % analyze.MIN_TICKS)
    check(len(range(22, analyze.MIN_TICKS - 1 - max(analyze.HORIZONS))) == 0,
          "MIN_TICKS-1 根的日子仍能进循环，门槛定低了")


@case("推送守卫：只差换行符不算「改了没提交」")
def t_push_same_content():
    """core.autocrlf=true 时磁盘是 CRLF、git 里存 LF，字节永远不相等。

    旧守卫拿 disk != head 当「改了没提交」，于是同一个文件被 git 检出
    一遍之后就被判成脏的——默认模式对任何文件都拒绝推送，真正该拦的
    情况（内容真的不同）反而被这堆假警报淹没。2026-11-04 实测 selftest.py
    磁盘 1202 个 CRLF、HEAD 里 0 个，而 git diff 是空的。
    """
    crlf = bytes([13, 10])
    lf = bytes([10])
    check(push_repo.same_content(b"a" + crlf + b"b" + crlf,
                                 b"a" + lf + b"b" + lf),
          "只差换行符被判成了内容不同")
    check(not push_repo.same_content(b"a" + lf + b"b" + lf,
                                     b"a" + lf + b"c" + lf),
          "内容真的不同却被放行，会把旧版本推上去")
    check(push_repo.same_content(None, None), "两边都没有应算一致")
    check(not push_repo.same_content(None, b"x"), "一边有一边没有不算一致")


@case("评审门槛：持有型按交易日算，不按成交笔数算")
def t_review_gate():
    """2026-10-19~11-07 两周无人复核的根因回归。

    原门槛是「20 笔 且 10 个交易日」。持有型一年成交 1~2 笔，20 笔要等几十年，
    于是周报天天报「样本不足、继续观察」——**警报永远不会响，也就永远没人看**。
    代码从 10-30 就在报告里写「门槛需要人工重新定义」，但没人改，两周就过去了。
    """
    # 持有型：0 笔成交，但已 20 个交易日没复核 → 必须判「到期」
    g = weekly.gate(0, 30, hold_mode=True, days_since=20, last_review="2026-10-09")
    check(g["ok"], "持有型连续 20 个交易日未复核却仍判「未到期」，门槛又废了")
    check(g["mode"] == "hold", "持有型没走 hold 分支，还在用笔数门槛")
    # 刚复核过就不该报到期，否则会逼出无意义的机械复核
    g2 = weekly.gate(0, 30, hold_mode=True, days_since=3, last_review="2026-11-04")
    check(not g2["ok"], "刚复核完就判到期，会把复核变成走过场")
    # 日内模式不能被顺手放宽：2 笔就是样本不足
    g3 = weekly.gate(2, 60, hold_mode=False)
    check(not g3["ok"], "日内模式 2 笔就放行，等于废掉样本门槛")
    check(g3["need_trades"] == weekly.MIN_TRADES - 2, "缺口算错，会让人以为快够了")

    with sandbox() as tmp:
        old = weekly.REVIEW_FILE
        weekly.REVIEW_FILE = os.path.join(tmp, "review.json")
        try:
            eq = [{"date": "2026-11-%02d" % d} for d in (2, 3, 4, 5, 6)]
            # 没有登记文件 → 全部交易日都算「未评审」，不能默认已评审
            last, n = weekly.review_age(eq)
            check(last == "" and n == len(eq),
                  "没有评审记录时没把全部天数算成未评审（会假装看过）")
            with open(weekly.REVIEW_FILE, "w", encoding="utf-8") as f:
                json.dump({"last_review": "2026-11-04"}, f)
            last2, n2 = weekly.review_age(eq)
            check(last2 == "2026-11-04" and n2 == 2,
                  "评审后剩余交易日算错：last=%r n=%r（应为 11-04 / 2）"
                  % (last2, n2))
        finally:
            weekly.REVIEW_FILE = old


if __name__ == "__main__":
    sys.exit(main())
