# -*- coding: utf-8 -*-
"""模拟券商：严格执行 A 股交易规则。

规则清单（每一条都在代码里落实）：
1. T+1：股票当日买入不可卖出。ETF 不是一律 T+0——按上交所《ETF 相关问题》，
   只有债券ETF、黄金ETF、跨境ETF、货币ETF 支持 T+0，**股票ETF 实施 T+1**。
   本项目池子里的宽基 ETF（510/512/588/1599xx）全部跟踪 A 股股票指数，一律 T+1。
2. 涨跌停：主板 ±10%，创业板/科创板 ±20%，ST ±5%，北交所 ±30%；
   买卖价格不得超出当日涨跌停价。
3. 最小交易单位：买入 100 股/份的整数倍；卖出可全部卖出（含零股）。
4. 交易时段：9:30-11:30、13:00-15:00；集合竞价 9:15-9:25、14:57-15:00 不撮合。
5. 费用：佣金万 2.5（最低 5 元，双边）；印花税 0.05%（仅卖出、仅股票）；
   过户费 0.001%（仅股票、双边）。ETF 免印花税、免过户费。
6. 滑点：按最小价格变动单位撮合（买入 +1 tick / 卖出 -1 tick），
   ETF 0.001 元 / 股票 0.01 元 —— 这是低价标的的真实摩擦。
7. 资金：卖出资金当日可用（A 股实际规则），不可提现。
"""
import csv
import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(BASE, "state")
ACCOUNT_FILE = os.path.join(STATE, "account.json")
TRADE_FILE = os.path.join(STATE, "trades.csv")
EQUITY_FILE = os.path.join(STATE, "equity.csv")

INIT_CAPITAL = 10000.0
MAX_POSITIONS = 1        # 1 万本金只够摊薄一只标的的摩擦，策略也是这么定的

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


def round_price(code, p):
    t = price_tick(code)
    return round(round(float(p) / t) * t, 3 if t == 0.001 else 2)


def limit_ratio(code, name=""):
    """涨跌幅限制比例。"""
    code = str(code)
    if "ST" in str(name).upper():
        return 0.05
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(("8", "4", "92")):      # 北交所
        return 0.30
    return 0.10


def limit_prices(code, preclose, name=""):
    r = limit_ratio(code, name)
    t = price_tick(code)
    up = round(preclose * (1 + r) / t) * t
    down = round(preclose * (1 - r) / t) * t
    return round(up, 3 if t == 0.001 else 2), round(down, 3 if t == 0.001 else 2)


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


def load_account():
    _ensure_files()
    with open(ACCOUNT_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_account(acc):
    os.makedirs(STATE, exist_ok=True)
    with open(ACCOUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(acc, f, ensure_ascii=False, indent=2)


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


def available_shares(pos, today, code):
    """可卖数量。

    股票，以及股票型 ETF：只有昨日及更早买入的份额可卖（T+1）。
    债券/黄金/跨境/货币 ETF：全部可卖（T+0）。
    """
    if is_etf(code) and is_t0_etf(code):
        return sum(l["shares"] for l in pos["lots"])
    return sum(l["shares"] for l in pos["lots"] if l["date"] < today)


def position_shares(pos):
    return sum(l["shares"] for l in pos["lots"])


def avg_cost(pos):
    s = position_shares(pos)
    if not s:
        return 0.0
    cost = sum(l["shares"] * l["price"] for l in pos["lots"])
    fees = sum(l.get("fee", 0) for l in pos["lots"])
    return (cost + fees) / s


def market_value(acc, quotes):
    mv = 0.0
    for code, pos in acc["positions"].items():
        px = (quotes.get(code) or {}).get("price") or avg_cost(pos)
        mv += position_shares(pos) * px
    return round(mv, 2)


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
    if shares <= 0 or shares % 100 != 0:
        raise Reject(f"买入必须为100的整数倍，当前 {shares}")
    # 持仓上限必须在这里挡。runner 用局部变量 pos 记当前仓位，
    # 一旦开盘就有过夜仓，那个 pos 仍是 None，于是它会在已有持仓上再开一只。
    # 资金天然不够摊薄两只标的，这条只能在券商层强制。
    if code not in acc["positions"] and len(acc["positions"]) >= MAX_POSITIONS:
        raise Reject(f"持仓上限 {MAX_POSITIONS} 只，当前已持有 "
                     f"{'、'.join(acc['positions'])}")
    if preclose:
        up, _ = limit_prices(code, preclose, name)
        if price > up + 1e-9:
            raise Reject(f"价格 {price} 高于涨停价 {up}，无法成交")
    px = round_price(code, price + price_tick(code))    # 买方吃一档
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
    pos = acc["positions"].get(code)
    if not pos:
        raise Reject("无持仓")
    total = position_shares(pos)
    avail = available_shares(pos, dt.strftime("%Y-%m-%d"), code)
    if shares > avail:
        raise Reject(f"T+1 限制：可卖 {avail} 份/股，请求 {shares}")
    if shares < total and shares % 100 != 0:
        raise Reject("部分卖出须为100整数倍")
    if preclose:
        _, down = limit_prices(code, preclose, name or pos["name"])
        if price < down - 1e-9:
            raise Reject(f"价格 {price} 低于跌停价 {down}，无法成交")
    px = round_price(code, price - price_tick(code))    # 卖方让一档
    amount = round(px * shares, 2)
    fee = calc_fee(code, "SELL", amount, pos["name"])
    proceeds = round(amount - fee["total"], 2)

    # 按先进先出核销批次，计算已实现盈亏
    cost_basis = 0.0
    left = shares
    new_lots = []
    for lot in pos["lots"]:
        if left <= 0:
            new_lots.append(lot)
            continue
        if lot["date"] == dt.strftime("%Y-%m-%d") and is_stock(code):
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
    mv = market_value(acc, quotes)
    eq = round(acc["cash"] + mv, 2)
    init = acc["meta"]["init_capital"]
    rows = []
    if os.path.exists(EQUITY_FILE):
        with open(EQUITY_FILE, "r", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    today = acc["today"]
    body = [r for r in rows[1:] if r and r[0] != today]
    day_start = init
    for r in rows[1:]:
        if r and r[0] < today:
            day_start = float(r[1])
    day_pnl = round(eq - day_start, 2)
    body.append([today, f"{eq:.2f}", f"{acc['cash']:.2f}", f"{mv:.2f}",
                 f"{day_pnl:.2f}", f"{round(eq - init, 2):.2f}", note])
    body.sort(key=lambda x: x[0])
    if write:
        with open(EQUITY_FILE, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["date", "equity", "cash", "market_value",
                        "day_pnl", "total_pnl", "note"])
            w.writerows(body)
    return {"equity": eq, "cash": acc["cash"], "market_value": mv,
            "day_pnl": day_pnl, "total_pnl": round(eq - init, 2)}
