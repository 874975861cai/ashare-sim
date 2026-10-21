# -*- coding: utf-8 -*-
"""回放引擎：多标的、统一时间轴、逐分钟决策。

防未来函数约定：
  * 第 i 分钟的信号只用 ticks[0..i] 的信息；
  * 成交发生在第 i+1 分钟的价格（下一根分钟 bar 的市价单）。
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker       # noqa: E402
import market      # noqa: E402
import strategy    # noqa: E402

CACHE = {}


def load_pool(codes, ndays=5, verbose=True, offline=False):
    """加载分时，按交易日分组。preclose 用前一交易日最后价格。

    offline=True 时只读本地缓存，不发起网络请求（限流时用）。
    """
    out = {}
    today = market.time.strftime("%Y%m%d")
    for c in codes:
        try:
            if offline:
                path = market._cpath(f"trends_{c}_{ndays}_{today}.json")
                blob = market._read(path)
                if not blob:
                    old = market._stale(f"trends_{c}_{ndays}_")
                    blob = old
                if not blob:
                    if verbose:
                        print(f"  - {c} 无缓存")
                    continue
                tr = blob["data"]
            else:
                tr = market.fetch_trends(c, ndays=ndays)
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"  ! {c} 拉取失败: {e}")
            continue
        # 0 根也要当失败：否则会建出 {"days": {}} 这种「存在但什么都没有」
        # 的条目，打印成「+ 588000 / 0 点 0 天」，看着像成功。
        if not tr.get("ticks"):
            if verbose:
                print(f"  - {c} 无分时数据（0 根）")
            continue
        by_day = defaultdict(list)
        for t in tr["ticks"]:
            by_day[t["t"][:10]].append(t)
        days = sorted(by_day)
        pre = {}
        for k, d in enumerate(days):
            if k == 0:
                pre[d] = by_day[d][0]["price"]
            else:
                pre[d] = by_day[days[k - 1]][-1]["price"]
        out[c] = {"name": tr["name"] or c, "days": {d: by_day[d] for d in days},
                  "preclose": pre}
        if verbose:
            print(f"  + {c} {(tr['name'] or c)[:14]:<16} {len(tr['ticks'])} 点 / {len(days)} 天")
    return out


def trading_days(data):
    ds = set()
    for v in data.values():
        ds |= set(v["days"].keys())
    return sorted(ds)


def run_backtest(data, params, days, capital=10000.0, verbose=False):
    """组合回放：单票集中，全池扫描。

    与实盘 broker 对齐的三条撮合规则（2026-10-20 代码自审补齐）：
      * 滑点：买入 +1 tick / 卖出 -1 tick，与 broker.buy/sell 一致；
      * R6 大盘熔断：每分钟按 510300（缺则取池内首个标的）重算 market_pct，
        以前这里恒为 0.0，等于回测里根本没有熔断这条规则；
      * 停牌日权益：用最后一个已知价计市值，不能把持仓当消失了。
    参数 max_trades_per_day 曾在此声明但从未生效（真实约束在
    strategy.decide 里读 params["max_trades"]），已删除，避免读代码的人
    误以为 R8 在这一层被强制。
    """
    cash = capital
    equity_val = capital
    start_equity = capital
    pos = None            # {code, entry, entry_idx, t, shares, day, overnight}
    trades = []
    day_pnls = []
    last_px = {}          # code -> 最后一个已知成交价，停牌日用来计市值

    # 大盘代理：实盘 runner 用 510300，池里没有就退化为首个标的
    mkt = "510300" if "510300" in data else (next(iter(data)) if data else None)
    if verbose and mkt != "510300":
        print(f"  ! 无 510300 数据，大盘熔断参照退化为 {mkt}")

    for day in days:
        # 隔夜仓：昨天买入的 T+1 标的，今天才解锁。
        # entry_idx 记的是"当天第几根分钟线"，昨天的索引放到今天没有意义，
        # 置 -1 等价于「一开盘就已超过持有上限」，即次日开盘按市价了结。
        if pos and pos.get("day") != day:
            pos["entry_idx"] = -1
            pos["overnight"] = True
        ctx = {"trades": 0, "market_pct": 0.0, "preclose": None}
        # 时间轴：取当日所有标的的时间并集（同一交易时段，基本一致）
        tl = set()
        for v in data.values():
            if day in v["days"]:
                tl |= {t["t"] for t in v["days"][day]}
        tlist = sorted(tl)
        # 索引：code -> {t: idx}
        idxmap = {}
        for c, v in data.items():
            if day in v["days"]:
                idxmap[c] = {t["t"]: k for k, t in enumerate(v["days"][day])}

        for i, t in enumerate(tlist):
            tm = t[11:16]
            if i == 0:
                continue

            # 大盘涨跌：和 runner 一样逐分钟更新，否则 R6 熔断在回测里形同虚设
            if mkt and day in data[mkt]["days"]:
                mk = idxmap.get(mkt, {}).get(t)
                mpre = data[mkt]["preclose"].get(day)
                if mk is not None and mpre:
                    ctx["market_pct"] = (data[mkt]["days"][day][mk]["price"]
                                         / mpre - 1) * 100

            # 持仓：出场判定
            if pos:
                c = pos["code"]
                if day in data[c]["days"] and t in idxmap[c]:
                    ticks = data[c]["days"][day]
                    k = idxmap[c][t]
                    if k < 2:
                        continue
                    # 用 k-1 决策，k 成交
                    pctx = dict(ctx)
                    pctx["preclose"] = data[c]["preclose"][day]
                    can_sell = bool(pos.get("overnight")) or broker.is_t0_etf(c)
                    act, reason, detail = strategy.decide(
                        ticks, k - 1,
                        {"entry": pos["entry"], "entry_idx": pos["entry_idx"]},
                        pctx, params, can_sell=can_sell)
                    if act == "SELL":
                        # 卖方让一档，与 broker.sell 的撮合口径一致
                        px = broker.round_price(
                            c, ticks[k]["price"] - broker.price_tick(c))
                        amt = px * pos["shares"]
                        fee = broker.calc_fee(c, "SELL", amt)
                        proceeds = amt - fee["total"]
                        cost = pos["entry"] * pos["shares"] + pos["fee_buy"]
                        pnl = proceeds - cost
                        cash += proceeds
                        trades.append({"day": day, "code": c,
                                       "name": data[c]["name"], "side": "SELL",
                                       "t": t, "price": px, "shares": pos["shares"],
                                       "pnl": pnl, "reason": reason})
                        pos = None
                        ctx["trades"] += 1
                continue

            # 空仓：全池扫描，选偏离最深的
            best = None
            for c, v in data.items():
                if day not in v["days"] or t not in idxmap[c]:
                    continue
                ticks = v["days"][day]
                k = idxmap[c][t]
                if k < 22:
                    continue
                cctx = dict(ctx)
                cctx["preclose"] = v["preclose"][day]
                act, reason, detail = strategy.decide(ticks, k - 1, None, cctx, params)
                if act == "BUY":
                    score = abs(detail.get("dev", 0))
                    if not best or score > best[0]:
                        best = (score, c, ticks, k, reason)

            if best:
                _, c, ticks, k, reason = best
                # 买方吃一档，与 broker.buy 的撮合口径一致
                px = broker.round_price(
                    c, ticks[k]["price"] + broker.price_tick(c))
                # 用 97% 资金，整百份
                budget = (cash - 5.5) * 0.97
                shares = int(budget / px / 100) * 100
                if shares >= 100:
                    amt = px * shares
                    fee = broker.calc_fee(c, "BUY", amt)
                    if amt + fee["total"] <= cash:
                        cash -= amt + fee["total"]
                        pos = {"code": c, "entry": px, "entry_idx": k,
                               "shares": shares, "fee_buy": fee["total"], "t": t,
                               "day": day, "overnight": False}
                        trades.append({"day": day, "code": c,
                                       "name": data[c]["name"], "side": "BUY",
                                       "t": t, "price": px, "shares": shares,
                                       "pnl": 0.0, "reason": reason})

        # 收盘仍未平仓：T+0 标的按最后价强平；T+1 标的当天买的根本卖不掉，带着过夜
        if pos:
            c = pos["code"]
            can_close = bool(pos.get("overnight")) or broker.is_t0_etf(c)
            if can_close and day in data[c]["days"]:
                last = data[c]["days"][day][-1]
                # 强平同样是卖出，同样让一档
                px = broker.round_price(
                    c, last["price"] - broker.price_tick(c))
                amt = px * pos["shares"]
                fee = broker.calc_fee(c, "SELL", amt)
                proceeds = amt - fee["total"]
                cost = pos["entry"] * pos["shares"] + pos["fee_buy"]
                cash += proceeds
                trades.append({"day": day, "code": c, "name": data[c]["name"],
                               "side": "SELL", "t": last["t"], "price": px,
                               "shares": pos["shares"], "pnl": proceeds - cost,
                               "reason": "收盘强平"})
                pos = None

        # 权益必须把持仓市值算进去：T+1 过夜仓只算现金的话，
        # 日盈亏会凭空少掉持仓那一大块，看着像"今天亏了"，其实是没结算。
        # 停牌（当日无 tick）时用最后一个已知价，不能让持仓凭空消失。
        equity_val = cash
        if pos:
            c = pos["code"]
            if day in data[c]["days"]:
                px_end = data[c]["days"][day][-1]["price"]
                last_px[c] = px_end
            else:
                px_end = last_px.get(c, pos["entry"])
            equity_val += px_end * pos["shares"]
        day_pnl = round(equity_val - start_equity, 2)
        day_pnls.append(day_pnl)
        start_equity = equity_val
        if verbose:
            # 曾在这里用 equity_val - start_equity 打印，而上一行刚把
            # start_equity 赋成 equity_val，于是每天都印 +0.00 —— 一个
            # 看着完全正常的 0，比报错更难发现。改用先算好的 day_pnl。
            print(f"  {day}  日盈亏 {day_pnl:+8.2f}  权益 {equity_val:9.2f}")

    return {"final": round(equity_val, 2), "trades": trades,
            "day_pnls": day_pnls, "open_pos": pos}


def metrics(res, capital=10000.0):
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    wins = [t for t in sells if t["pnl"] > 0]
    n = len(sells)
    return {
        "ret_pct": round((res["final"] / capital - 1) * 100, 3),
        "pnl": round(res["final"] - capital, 2),
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_pnl": round(sum(t["pnl"] for t in sells) / n, 2) if n else 0.0,
        "best": round(max([t["pnl"] for t in sells], default=0), 2),
        "worst": round(min([t["pnl"] for t in sells], default=0), 2),
    }
