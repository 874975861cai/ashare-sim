# -*- coding: utf-8 -*-
"""模拟券商：严格执行 A 股交易规则。

规则清单（每一条都在代码里落实）：
1. T+1：股票当日买入不可卖出。ETF 不是一律 T+0——按上交所《ETF 相关问题》，
   只有债券ETF、黄金ETF、跨境ETF、货币ETF 支持 T+0，**股票ETF 实施 T+1**。
   本项目池子里的宽基 ETF（510/512/588/1599xx）全部跟踪 A 股股票指数，一律 T+1。
2. 涨跌停：主板 ±10%，创业板/科创板 ±20%，ST ±5%，北交所 ±30%；
   买卖价格不得超出当日涨跌停价。
3. 最小交易单位：买入 100 股/份的整数倍；卖出可全部卖出（含零股）。
4. 交易时段：9:30-11:30、13:00-15:00。开盘集合竞价 9:15-9:25 不撮合；
   14:57-15:00 的收盘集合竞价**故意放行**：那段没有独立撮合，但分时最后一根
   常落在 14:57-15:00，尾盘强平必须能用它成交，否则仓位会安静地过夜。
5. 费用：佣金万 2.5（最低 5 元，双边）；印花税 0.05%（仅卖出、仅股票）；
   过户费 0.001%（仅股票、双边）。ETF 免印花税、免过户费。
6. 滑点：按最小价格变动单位撮合（买入 +1 tick / 卖出 -1 tick），
   ETF 0.001 元 / 股票 0.01 元 —— 这是低价标的的真实摩擦。
7. 资金：卖出资金当日可用（A 股实际规则），不可提现。
"""
import csv
import io
import json
import os
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(BASE, "state")
ACCOUNT_FILE = os.path.join(STATE, "account.json")
TRADE_FILE = os.path.join(STATE, "trades.csv")
EQUITY_FILE = os.path.join(STATE, "equity.csv")

INIT_CAPITAL = 10000.0
MAX_POSITIONS = 1        # 默认值。10 万组合账户由 meta.max_positions=3 覆盖


def _max_positions(acc):
    """持仓只数上限：优先读账本，读不到就用默认 1。

    不做「按 mode 自动推断」——上限是资金属性，不是策略属性。
    让策略偷偷改风控参数，正是账户被开成 5 只那种事故的起点。
    """
    try:
        v = int(((acc or {}).get("meta") or {}).get("max_positions") or MAX_POSITIONS)
    except (TypeError, ValueError):
        return MAX_POSITIONS
    return max(1, v)

FEE = {
    "commission_rate": 0.00025,
    "commission_min": 5.0,
    "stamp_tax": 0.0005,
    "transfer_fee": 0.00001,
}

TRADE_HEADER = ["seq", "datetime", "code", "name", "side", "price", "shares",
                "amount", "commission", "stamp_tax", "transfer_fee", "total_fee",
                "net_cash", "cash_after", "pnl", "reason"]


# --------------------------------------------------------------------------
# 品种属性
# --------------------------------------------------------------------------
def is_etf(code):
    """场内 ETF/LOF 判定：5/15/16/159/510... 开头。"""
    code = str(code)
    return code.startswith(("51", "56", "58", "15", "16", "50", "52"))


def is_stock(code):
    return not is_etf(code)


def price_tick(code):
    return 0.001 if is_etf(code) else 0.01


def _half_up(value, tick):
    """按交易所口径把价格四舍五入到最小价格变动单位的整数倍。

    不能用内置 `round()`：它是「银行家舍入」（.5 靠偶数），而沪深交易所
    《交易规则》对涨跌幅价格的规定是「四舍五入至最小价格变动单位」。
    实测 510300 昨收 2.335 时，涨停价应为 2.569，`round()` 给 2.568
    （2.5685 落在 2568.5，取偶）——差这一档，就会把「报价 2.568、
    吃一档后 2.569 正好封在涨停价上」的真实成交判成「超过涨停价」。

    而且必须全程走 Decimal：2.335 * 1.1 在浮点里是 2.5685000000000002，
    3.045 * 0.9 是 2.7405000000000004，拿这种数去 round 只会把半档的
    误差随机放大。
    """
    q = Decimal(str(tick))
    n = (Decimal(str(value)) / q).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return n * q


def round_price(code, p):
    t = price_tick(code)
    v = _half_up(p, t)
    return round(float(v), 3 if t == 0.001 else 2)


# 跟踪创业板/科创板指数的 ETF，涨跌幅限制是 20%，不是 10%。
# 依据：上交所《科创板相关基金》页面「科创板相关基金的涨跌幅比例为 20%」；
# 深交所投教《ETF 投资问答》第 32 期「跟踪指数成份股为创业板/科创板股票的
# ETF 涨跌幅限制也为 20%」。池子里的 159915（创业板ETF）与 588xxx（科创50）
# 都在此列，按 10% 判会在大涨日把真实可成交的单子拒掉，还报「高于涨停价」。
#
# 不能按 159/588 前缀一刀切：159919 沪深300ETF、159901 深100ETF 都是 10%。
# 所以用「代码名单 + 名称关键词」两个判据，两者都不命中就按 10%（保守）。
LIMIT20_ETF = frozenset({"159915", "159949", "159952", "159958", "159977",
                         "588000", "588080"})
LIMIT20_WORDS = ("创业板", "科创", "双创", "创50", "创成长")


def _is_20pct_etf(code, name=""):
    if str(code) in LIMIT20_ETF:
        return True
    nm = str(name or "")
    return any(w in nm for w in LIMIT20_WORDS)


def limit_ratio(code, name=""):
    """涨跌幅限制比例。

    顺序是有讲究的：**板块先于 ST**。
    2020 年创业板注册制改革后，《深圳证券交易所创业板交易特别规定》把创业板
    股票（**含风险警示股票**）的涨跌幅统一为 20%；科创板同理。主板 ST 才是 5%。
    原来先判 `"ST" in name` 再判板块，创业板/科创板 ST 会被当成 5%，
    涨停价被算低三分之二、跌停价算高——不是拒掉合法成交，就是凭空造出
    「已经跌停」的假信号。北交所没有 5% 档，风险警示股仍是 30%。
    """
    code = str(code)
    if code.startswith(("8", "4", "92")):      # 北交所（含风险警示股，一律 30%）
        return 0.30
    if code.startswith(("300", "301", "688", "689")):   # 创业板/科创板，含 ST
        return 0.20
    if "ST" in str(name).upper():              # 只有主板 ST 是 5%
        return 0.05
    if is_etf(code) and _is_20pct_etf(code, name):
        return 0.20
    return 0.10


def limit_prices(code, preclose, name=""):
    r = Decimal(str(limit_ratio(code, name)))
    base = Decimal(str(preclose))
    t = price_tick(code)
    up = _half_up(base * (1 + r), t)
    down = _half_up(base * (1 - r), t)
    nd = 3 if t == 0.001 else 2
    return round(float(up), nd), round(float(down), nd)


def in_trading_session(dt):
    """是否处于连续竞价时段。"""
    hm = dt.hour * 100 + dt.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


# --------------------------------------------------------------------------
# 账户
# --------------------------------------------------------------------------
def _ensure_files():
    os.makedirs(STATE, exist_ok=True)
    if not os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(TRADE_HEADER)
    if not os.path.exists(EQUITY_FILE):
        with open(EQUITY_FILE, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["date", "equity", "cash", "market_value",
                                    "day_pnl", "total_pnl", "note"])
    if not os.path.exists(ACCOUNT_FILE):
        save_account({
            "meta": {"init_capital": INIT_CAPITAL,
                     "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "broker": "模拟盘（A股规则引擎）",
                     "fee": FEE},
            "cash": INIT_CAPITAL,
            "positions": {},
            "trade_seq": 0,
            "today": datetime.now().strftime("%Y-%m-%d"),
            "peak_equity": INIT_CAPITAL,
        })


def _atomic_write_text(path, text, encoding="utf-8", newline=None):
    """先写临时文件再 os.replace：不让「写一半」变成永久损坏。

    云端 runner 会被 timeout-minutes / concurrency 取消，进程随时可能在
    写文件中间被杀。直接 "w" 打开目标文件的话，账本会停在被截断的半截 JSON 上，
    下一轮 load_account() 抛 JSONDecodeError —— 一次取消换来整个账本打不开。
    os.replace 在同一分区上是原子的，最坏情况是「这轮没写进去」，不是「写坏」。
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding=encoding, newline=newline) as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_account():
    _ensure_files()
    with open(ACCOUNT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_account(acc):
    os.makedirs(STATE, exist_ok=True)
    _atomic_write_text(ACCOUNT_FILE,
                       json.dumps(acc, ensure_ascii=False, indent=2))


def roll_day(acc, day):
    """跨交易日处理：更新日期字段。"""
    if acc.get("today") != day:
        acc["today"] = day
    return acc


# --------------------------------------------------------------------------
# 费用与持仓
# --------------------------------------------------------------------------
def calc_fee(code, side, amount, name=""):
    commission = max(amount * FEE["commission_rate"], FEE["commission_min"])
    commission = round(commission, 2)
    if is_stock(code):
        stamp = round(amount * FEE["stamp_tax"], 2) if side == "SELL" else 0.0
        transfer = round(amount * FEE["transfer_fee"], 2)
    else:
        stamp = 0.0          # ETF 免印花税
        transfer = 0.0       # ETF 免过户费
    return {"commission": commission, "stamp_tax": stamp,
            "transfer_fee": transfer,
            "total": round(commission + stamp + transfer, 2)}


def is_t0_etf(code):
    """这只 ETF 是否支持 T+0 日内回转。

    依据上交所《ETF 相关问题》第 3 条：
        「目前，国内部分 ETF 品种支持 T+0 交易，比如债券 ETF、黄金 ETF、
          跨境 ETF、货币 ETF；但是，**股票 ETF 实施 T+1 交易**。」

    所以绝不能写成「凡是 ETF 就 T+0」——这是 2026-10-20 修掉的一个真实错误：
    原来 available_shares 对所有 ETF 都返回全部份额，导致创业板 ETF（159915）
    当天买入当天卖出，现实中根本不可能成交。

    支持 T+0 的代码段：
        511xxx  债券 ETF / 货币 ETF（国债ETF 511010、货币ETF 511880）
        513xxx  跨境 ETF（港股通、纳指等）
        518xxx  商品 ETF（黄金ETF 518880）
        1590xx  深市货币 ETF（159001 等）
        159920  恒生 ETF（跨境）
    其余（510/512/588/1599xx 等宽基与行业 ETF）一律按 T+1。
    """
    c = str(code)
    if c.startswith(("511", "513", "518", "1590")):
        return True
    return c == "159920"


def _lot_sellable(lot, today, code):
    """这一批份额在今天能不能卖。

    T+0 品种（债券/黄金/跨境/货币 ETF）全天可卖；其余只有今天之前买入的可卖。
    这个判据只能有一处：`available_shares` 用它算「能卖多少」，`sell` 的 FIFO
    核销也要用它决定跳过哪些批次。原来两处各写一套判据（一处看日期，一处看
    `is_stock`），股票型 ETF 在核销环节被当成「今天也能卖」——一旦哪天
    「可卖数量」与「核销批次」对不上，就会拿今天的份额去抵昨天的卖出，
    盈亏算出来的数是错的，而且不报错。
    """
    if is_etf(code) and is_t0_etf(code):
        return True
    return str(lot.get("date", "")) < today


def available_shares(pos, today, code):
    """可卖数量。

    股票，以及股票型 ETF：只有昨日及更早买入的份额可卖（T+1）。
    债券/黄金/跨境/货币 ETF：全部可卖（T+0）。
    """
    return sum(l["shares"] for l in pos["lots"] if _lot_sellable(l, today, code))


def position_shares(pos):
    return sum(l["shares"] for l in pos["lots"])


def avg_cost(pos):
    s = position_shares(pos)
    if not s:
        return 0.0
    cost = sum(l["shares"] * l["price"] for l in pos["lots"])
    fees = sum(l.get("fee", 0) for l in pos["lots"])
    return (cost + fees) / s


def market_value_detail(acc, quotes):
    """返回 (市值, 无行情的标的列表)。

    无行情时按成本估值是有意的兜底（停牌时总得有个数），但**必须能看见**：
    原来直接 `or avg_cost(pos)`，报价缺失和「价格没变」长得一模一样，
    净值会一整天纹丝不动，看起来像"行情没波动"，实际是没数据。
    所以把"哪些标的没吃到报价"显式返回，由调用方去告警。
    """
    mv = 0.0
    unpriced = []
    for code, pos in acc["positions"].items():
        px = (quotes.get(code) or {}).get("price")
        if not px:
            px = avg_cost(pos)
            unpriced.append(code)
        mv += position_shares(pos) * px
    return round(mv, 2), unpriced


def market_value(acc, quotes):
    return market_value_detail(acc, quotes)[0]


def equity_of(acc, quotes):
    return round(acc["cash"] + market_value(acc, quotes), 2)


# --------------------------------------------------------------------------
# 下单
# --------------------------------------------------------------------------
class Reject(Exception):
    pass


def buy(acc, code, name, price, shares, dt, reason="", preclose=None):
    code = str(code)
    if not in_trading_session(dt):
        raise Reject("非连续竞价时段")
    # 与 sell 对齐：整数值的 float 先归一化（100.0 是 runner 里除法的天然产物，
    # 不该拒），真正的小数股/布尔值才拒。否则 100.5 会被 `% 100` 放过去，
    # 账本里就出现半份持仓，后面所有成本、可卖数量、估值全跟着脏。
    if isinstance(shares, float) and shares.is_integer():
        shares = int(shares)
    if isinstance(shares, bool) or not isinstance(shares, int) or shares <= 0:
        raise Reject(f"买入数量必须为正整数，当前 {shares!r}")
    if shares % 100 != 0:
        raise Reject(f"买入必须为100的整数倍，当前 {shares}")
    # 持仓上限必须在这里挡。runner 用局部变量 pos 记当前仓位，
    # 一旦开盘就有过夜仓，那个 pos 仍是 None，于是它会在已有持仓上再开一只。
    #
    # 2026-11-07：上限从「写死 1 只」改成读账本 meta.max_positions。
    # 1 万本金时代确实只能持一只（佣金 5 元下限，分仓等于罚两次）；
    # 10 万本金按 40/30/30 建三只，单笔 3.3 万佣金 0.015%，分散是免费的。
    # 但默认值仍是 1——不改账本就意外变成多仓，比拒单危险得多。
    cap = _max_positions(acc)
    if code not in acc["positions"] and len(acc["positions"]) >= cap:
        raise Reject(f"持仓上限 {cap} 只，当前已持有 "
                     f"{'、'.join(acc['positions'])}")
    if price <= 0:
        raise Reject("报价无效（%s），不能按 0 元成交" % price)
    up = None
    if preclose:
        up, _ = limit_prices(code, preclose, name)
        if price > up + 1e-9:
            raise Reject(f"价格 {price} 高于涨停价 {up}，无法成交")
    px = round_price(code, price + price_tick(code))    # 买方吃一档
    if up is not None and px > up + 1e-9:
        # 封在涨停上时买方吃不到货：再加一档滑点就超出涨停价，现实中不可能成交。
        # 原来只校验报价、不校验加滑点后的成交价，会记出 2.896 买入涨停 2.895 的假成交。
        raise Reject(f"涨停封板：报价 {price} 吃一档后 {px} 超过涨停价 {up}，无法成交")
    amount = round(px * shares, 2)
    fee = calc_fee(code, "BUY", amount, name)
    need = amount + fee["total"]
    if need > acc["cash"] + 1e-6:
        raise Reject(f"资金不足：需要 {need:.2f}，可用 {acc['cash']:.2f}")

    acc["cash"] = round(acc["cash"] - need, 2)
    pos = acc["positions"].setdefault(code, {"name": name, "lots": [], "realized": 0.0})
    pos["name"] = name or pos.get("name")
    pos["lots"].append({"date": dt.strftime("%Y-%m-%d"), "shares": shares,
                        "price": px, "fee": fee["total"],
                        "time": dt.strftime("%H:%M")})
    acc["trade_seq"] += 1
    _log_trade(acc, acc["trade_seq"], dt, code, pos["name"], "BUY", px, shares,
               amount, fee, -need, "", reason)
    save_account(acc)
    return {"price": px, "shares": shares, "amount": amount, "fee": fee["total"],
            "cash": acc["cash"]}


def sell(acc, code, name, price, shares, dt, reason="", preclose=None):
    code = str(code)
    if not in_trading_session(dt):
        raise Reject("非连续竞价时段")
    # 0 股和负股数必须挡在门外。原来没有这道门，后果是安静的：
    # shares=0 时 0 > avail 不成立，一路走到 calc_fee，佣金按下限收 5 元，
    # 于是账本里多出一笔「SELL 0 股 / net_cash -5.00」，现金白掉 5 块；
    # shares=-100 更糟，amount 变负数、现金直接减少，还记了一笔负股数成交。
    # 两者都不报错，只有对账时才会看到现金对不上。
    if not isinstance(shares, int) or shares <= 0:
        raise Reject(f"卖出数量必须为正整数，当前 {shares!r}")
    pos = acc["positions"].get(code)
    if not pos:
        raise Reject("无持仓")
    total = position_shares(pos)
    avail = available_shares(pos, dt.strftime("%Y-%m-%d"), code)
    if avail <= 0:
        raise Reject(f"T+1 限制：今日买入的份额不可卖出，可卖 0，请求 {shares}")
    if shares > avail:
        raise Reject(f"T+1 限制：可卖 {avail} 份/股，请求 {shares}")
    if shares < total and shares % 100 != 0:
        raise Reject("部分卖出须为100整数倍")
    if price <= 0:
        raise Reject("报价无效（%s），不能按 0 元成交" % price)
    down = None
    if preclose:
        _, down = limit_prices(code, preclose, name or pos["name"])
        if price < down - 1e-9:
            raise Reject(f"价格 {price} 低于跌停价 {down}，无法成交")
    px = round_price(code, price - price_tick(code))    # 卖方让一档
    if down is not None and px < down - 1e-9:
        # 跌停封板同理：再让一档就跌破跌停价，这种成交价不存在。
        raise Reject(f"跌停封板：报价 {price} 让一档后 {px} 低于跌停价 {down}，无法成交")
    amount = round(px * shares, 2)
    fee = calc_fee(code, "SELL", amount, pos["name"])
    proceeds = round(amount - fee["total"], 2)

    # 按先进先出核销批次，计算已实现盈亏
    today = dt.strftime("%Y-%m-%d")
    cost_basis = 0.0
    left = shares
    new_lots = []
    for lot in pos["lots"]:
        if left <= 0:
            new_lots.append(lot)
            continue
        if not _lot_sellable(lot, today, code):
            new_lots.append(lot)
            continue
        take = min(lot["shares"], left)
        unit_fee = lot.get("fee", 0) * take / lot["shares"]
        unit_fee_sell = fee["total"] * take / shares
        cost_basis += take * lot["price"] + unit_fee + unit_fee_sell
        left -= take
        if take < lot["shares"]:
            new_lots.append({**lot, "shares": lot["shares"] - take,
                             "fee": round(lot.get("fee", 0) - unit_fee, 2)})
    if left > 0:
        # 走到这里说明「可卖数量」和「能核销的批次」对不上（状态被改坏、
        # 或者两个判据又分叉了）。绝不能继续：那样 cost_basis 会少算，
        # pnl 变成凭空多出来的利润，而且账本照样写下去。
        raise Reject(f"可卖份额不足：请求 {shares}，核销后仍差 {left}，拒绝成交")
    pos["lots"] = new_lots
    pnl = round(amount - cost_basis, 2)
    pos["realized"] = round(pos.get("realized", 0) + pnl, 2)

    acc["cash"] = round(acc["cash"] + proceeds, 2)
    acc["trade_seq"] += 1
    if not pos["lots"]:
        acc["positions"].pop(code, None)
    _log_trade(acc, acc["trade_seq"], dt, code, pos.get("name") or name, "SELL",
               px, shares, amount, fee, proceeds, pnl, reason)
    save_account(acc)
    return {"price": px, "shares": shares, "amount": amount, "fee": fee["total"],
            "proceeds": proceeds, "pnl": pnl, "cash": acc["cash"]}


def _log_trade(acc, seq, dt, code, name, side, price, shares, amount, fee,
               net_cash, pnl, reason):
    with open(TRADE_FILE, "a", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerow([
            seq, dt.strftime("%Y-%m-%d %H:%M:%S"), code, name, side,
            f"{price:.3f}" if is_etf(code) else f"{price:.2f}", shares,
            f"{amount:.2f}", f"{fee['commission']:.2f}", f"{fee['stamp_tax']:.2f}",
            f"{fee['transfer_fee']:.2f}", f"{fee['total']:.2f}",
            f"{net_cash:.2f}", f"{acc['cash']:.2f}",
            f"{pnl:.2f}" if pnl != "" else "", reason])


def snapshot(acc, quotes, note="", write=True):
    """写入当日净值行（同一天重复调用会覆盖最后一行）。

    write=False 时只计算不落盘 —— `runner --dry` 必须用这个。
    否则「只推演不记账」的试跑会把今天的权益写到 `acc["today"]`
    那一行上；dry 模式不 roll_day，那个日期还是昨天，等于用今天的数
    覆盖昨天的历史净值。
    """
    mv, unpriced = market_value_detail(acc, quotes)
    eq = round(acc["cash"] + mv, 2)
    init = acc["meta"]["init_capital"]
    rows = []
    if os.path.exists(EQUITY_FILE):
        with open(EQUITY_FILE, "r", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    today = acc["today"]
    body = [r for r in rows[1:] if r and r[0] != today]
    # 入金/出金：2026-11-09 追加 90,170 把资金从 1 万加到 10 万。
    # 不记这一列，「当日盈亏」会把入金算成盈利（当天显示 +90,170），
    # 周报的「最佳/最差日」「日胜率」全部被一条假记录带偏。
    #
    # 按**区间**消费，不按「日期精确等于今天」消费（2026-11-08 改）：
    # 入金日记在哪一天天生要靠估——周末迁移记在下一个工作日、节假日迁移记在
    # 休市日、收盘后迁移只能记到次日。旧写法 `flows.get(today)` 只要差一天就
    # 两头都错：当天漏扣（假盈利 N），次日又在**已经含钱**的 day_start 上再扣
    # 一次（假亏损 N）。实测 +90,000 / -90,000 两条假记录永久写进 equity.csv，
    # 而且没有任何检查会报——它长着正常数字的样子。
    # 正确口径与「哪天入金」无关：上一根净值行之后、今天之前到账的钱，都属于
    # 今天的非交易性变动。同日精确匹配只是这个区间的一个特例。
    flows = acc.get("flows") or {}
    day_start = init
    prev_day = ""
    for r in rows[1:]:
        if r and r[0] < today:
            day_start = float(r[1])
            prev_day = r[0]
    flow = 0.0
    prev_d = datetime.strptime(prev_day, "%Y-%m-%d").date() if prev_day else None
    today_d = datetime.strptime(today, "%Y-%m-%d").date()
    for k, v in flows.items():
        try:
            # 必须按日期比，不能拿字符串比大小：'2026-1-06' 这类非补零写法会被
            # 判成「早于 2026-11-06」而静默丢掉，那笔钱就从账本里消失，
            # 而 day_pnl 仍然看着正常。
            kd = datetime.strptime(k, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise ValueError("flows 的键不是 YYYY-MM-DD：%r" % (k,))
        if prev_d and kd <= prev_d:
            continue
        if kd > today_d:
            continue
        try:
            flow += float(v or 0.0)
        except (TypeError, ValueError):
            # 不能当 0 吞掉：这笔钱会凭空消失，而 day_pnl 看起来完全正常。
            raise ValueError("flows[%s] 不是数字：%r" % (k, v))
    flow = round(flow, 2)
    day_pnl = round(eq - day_start - flow, 2)
    # 历史行只有 7 列，补上 flow=0，否则 csv 列数不齐，DictReader 读出来是 None
    fixed = []
    for r in body:
        r = list(r)
        if len(r) < 7:
            r += [""] * (7 - len(r))
        if len(r) < 8:
            r.append("0")
        fixed.append(r[:8])
    fixed.append([today, f"{eq:.2f}", f"{acc['cash']:.2f}", f"{mv:.2f}",
                  f"{day_pnl:.2f}", f"{round(eq - init, 2):.2f}", note,
                  f"{flow:.2f}"])
    fixed.sort(key=lambda x: x[0])
    body = fixed
    if write:
        buf = io.StringIO(newline="")
        w = csv.writer(buf)
        w.writerow(["date", "equity", "cash", "market_value",
                    "day_pnl", "total_pnl", "note", "flow"])
        w.writerows(body)
        _atomic_write_text(EQUITY_FILE, buf.getvalue(),
                           encoding="utf-8-sig", newline="")
    return {"equity": eq, "cash": acc["cash"], "market_value": mv,
            "day_pnl": day_pnl, "total_pnl": round(eq - init, 2),
            "unpriced": unpriced}
