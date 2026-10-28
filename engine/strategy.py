# -*- coding: utf-8 -*-
"""策略层：T+0 日内均值回归（VWAP 偏离）。

设计前提（用数学而非直觉）：
  1 万本金单笔摩擦 ≈ tick 0.05% + 佣金 0.10% ≈ 0.15%，
  因此只做「偏离足够大 + 有拐头确认」的机会，单笔目标价差 ≥ 0.4%。

核心假设：
  ETF 是成分股的加权，日内缺乏独立趋势，价格围绕当日 VWAP 波动；
  当偏离 VWAP 超过一定阈值且出现动量拐头时，回归概率占优。
"""

# ---------------- 默认参数（v1.2 数据校准 + 路径验证后） ----------------
# 校准依据：engine/analyze.py 信号生存分析 + engine/backtest.py 路径回放
#   dev 0.55% -> 5min 胜率 48.8%（噪音，已废弃）
#   dev 0.80% -> 5min 胜率 75.0%，均值 +0.36%
#   加入「脱离近期低点 0.3%」确认后，4 日回放由 -1.7% 转为 +0.59%
PARAMS = {
    # 2026-10-28：关闭入场。不是"暂时观察"，是有证据的停手——
    #   250 日日线代理回测 42 笔单仓：现行出场 -12.11%；
    #   换成最好的出场规则（回本即卖）也只有 +0.43%，等于零；
    #   趋势跟随 -41%、动量轮动 -5.9~-22%、恐慌买入样本内为负。
    #   8 个交易日实盘 -0.81%，同期买入持有 510300 是 -1.63%（略好），
    #   但把窗口拉到 3 年，任何主动规则都跑不过"什么都不做"。
    # 结论：这套信号的毛期望≈0，扣掉 0.18% 往返成本就是负的。
    # 出场仍启用，持仓可以正常离场。要重新开仓，必须先有样本外为正的证据。
    "entry_enabled": False,
    # 这条门槛调过两次，两次都是错的方向，留在这里防第三次：
    #   0.55% -> 0.80%：4 日回放由亏转正（对）
    #   0.80% -> 0.60%：只看 10/19 单日，放宽后能抓一笔 +84.50（错）
    #       5 日完整数据重跑：放宽后 2 笔变 6 笔，收益 0.59% -> 0.09%，
    #       新增的 4 笔是 -69.40 / -52.00 / -12.80 / +84.50，净亏 49.70。
    # **单日回放得出的参数结论一律不作数**，至少要 5 个交易日全量重跑。
    "dev_enter": 0.0080,
    "mom_turn": 0.0010,       # 3 分钟动量需 > 该值（拐头确认）
    "rebound": 0.0030,        # 需从最近 15 分钟低点反弹该幅度（拒绝接刀）
    "take_profit": 0.0050,    # 相对入场价止盈（不再要求回到 VWAP）
    "stop_loss": 0.0040,      # 相对入场价止损
    "max_hold_min": 20,       # 时间止损（分钟，从 40 下调）
    "force_exit": "14:50",    # 收盘前强平
    "earliest_entry": "09:50",  # 开盘 20 分钟内不交易（噪音大）
    "latest_entry": "14:20",    # 之后不开新仓（不够时间回归）
    "min_amount": 2e8,        # 成交额下限（流动性）
    "min_price": 1.2,         # 价格下限（控制 tick 摩擦）
    "max_premium": None,      # 预留：QDII 溢价过滤
    "max_trades": 3,          # 每日最多交易次数（控费）
    "market_filter": -0.012,  # 大盘跌幅超过该值则停手
    "min_day_drop": 0.004,    # 标的当日至少跌该幅度（有下跌才有回归）
}

T0_KEYWORDS = ("纳指", "纳斯达克", "标普", "道琼斯", "恒生", "港股", "H股", "中概",
               "日经", "东证", "德国", "法国", "英国", "东南亚", "越南", "沙特",
               "印度", "亚太", "美国", "全球", "海外", "恒指",
               "国债", "政金", "公司债", "信用债", "城投", "短融", "债ETF",
               "黄金", "金ETF", "货币ETF", "保证金")

EXCLUDE_KEYWORDS = ("货币ETF", "保证金", "现金", "短融", "国债", "政金", "公司债",
                    "信用债", "城投", "债ETF")

# 核心观察池：全市场成交额最高、价格足以摊薄 tick 摩擦的品种。
# 1 万本金只能同时持有一只，池子不求多而求「摩擦小、回归性强」。
#
# 池子里刻意混了两类交易规则（2026-10-20 起）：
#   T+1（股票型 ETF）—— 当天买当天卖不掉，必须过夜，次日开盘才有机会议价
#   T+0（跨境 / 商品 ETF）—— 支持日内回转，现有日内逻辑可以完整执行
# 只做 T+0 等于逃避真实约束，账面上的漂亮收益在 T+1 上根本拿不到；
# 两类一起做，回测才能反映这套逻辑在真实规则下到底站不站得住。
CORE_POOL = [
    # --- 股票型 ETF（T+1）---
    "510300",   # 沪深300ETF华泰柏瑞
    "510500",   # 中证500ETF南方
    "512100",   # 中证1000ETF南方
    "159915",   # 创业板ETF易方达
    "510050",   # 上证50ETF华夏
    "588000",   # 科创50ETF华夏
    "510880",   # 红利ETF华泰柏瑞
    "159919",   # 沪深300ETF嘉实
    "510330",   # 沪深300ETF华夏
    "159901",   # 深证100ETF
    # --- T+0 品种（跨境 / 商品）---
    "513050",   # 中概互联网ETF易方达（跨境，日均成交额 ~15 亿）
    "513180",   # 恒生科技ETF华夏（跨境，~16 亿）
    "518880",   # 黄金ETF华安（商品，~28 亿）
]


def validate_pool(quotes, params=PARAMS):
    """用实时报价校验核心池，剔除流动性或价格不达标的标的。"""
    out = []
    for code in CORE_POOL:
        q = quotes.get(code)
        if not q:
            continue
        if q["amount"] < params["min_amount"]:
            continue
        if q["price"] < params["min_price"]:
            continue
        rng = (q["high"] - q["low"]) / q["preclose"] if q["preclose"] else 0
        out.append({**q, "range_pct": round(rng * 100, 2),
                    "tick_friction": round(0.001 / q["price"] * 2 * 100, 3)})
    out.sort(key=lambda x: -(x["range_pct"] * 0.7 + min(x["amount"] / 1e9, 3.0) * 0.3))
    return out


def is_t0_eligible(name):
    """可交易标的：ETF 场内均为 T+0；排除货币/债券类（波幅不足以覆盖摩擦）。"""
    if not name:
        return False
    if any(k in name for k in EXCLUDE_KEYWORDS):
        return False
    return True


def core_name(name):
    """提取标的指数核心名：'创业板ETF易方达' -> '创业板'。"""
    n = name or ""
    for sep in ("ETF", "etf", "LOF"):
        if sep in n:
            n = n.split(sep)[0]
            break
    return n.strip("（）() ")


def select_universe(etf_rows, params=PARAMS, top=15):
    """筛选当日可交易池：流动性 + 价格 + 波动，并按标的指数去重。"""
    cand = []
    for u in etf_rows:
        if not is_t0_eligible(u["name"]):
            continue
        if u["amount"] < params["min_amount"]:
            continue
        if u["price"] < params["min_price"]:
            continue
        if not (u.get("open") and u.get("preclose")):
            continue
        rng = (u["high"] - u["low"]) / u["preclose"] if u["preclose"] else 0
        tick_fric = (0.001 / u["price"]) * 2
        cand.append({**u, "range_pct": round(rng * 100, 2),
                     "tick_friction": round(tick_fric * 100, 3),
                     "core": core_name(u["name"])})

    # 同一标的指数只留成交额最大的一只，避免重复暴露
    best = {}
    for c in cand:
        k = c["core"]
        if k not in best or c["amount"] > best[k]["amount"]:
            best[k] = c
    uniq = list(best.values())

    # 评分：波动空间为主，流动性为辅；波动需能覆盖摩擦
    for c in uniq:
        c["score"] = (c["range_pct"] * 0.6
                      + min(c["amount"] / 1e9, 3.0) * 0.4
                      - c["tick_friction"] * 3)
    uniq.sort(key=lambda x: -x["score"])
    return uniq[:top]


# ---------------- 指标 ----------------
def hhmm(t):
    return t[11:16]


def at(ticks, i):
    """取第 i 个点的收盘信息（i 从 0 开始）。"""
    p = ticks[i]
    return {"price": p["price"], "vwap": p["avg"], "vol": p["vol"], "t": p["t"]}


def dev_from_vwap(ticks, i):
    p = ticks[i]
    return (p["price"] / p["avg"] - 1) if p["avg"] else 0.0


def momentum(ticks, i, n=3):
    if i - n < 0:
        return 0.0
    return ticks[i]["price"] / ticks[i - n]["price"] - 1


def vol_ratio(ticks, i, short=5, long=20):
    if i < long + 1:
        return 1.0
    s = sum(x["vol"] for x in ticks[i - short + 1:i + 1]) / short
    l = sum(x["vol"] for x in ticks[i - long + 1:i + 1]) / long
    return s / l if l else 1.0


# ---------------- 决策 ----------------
def decide(ticks, i, pos, ctx, params=PARAMS, can_sell=True):
    """在第 i 个点之后（用 ticks[:i+1] 的信息）给出动作。

    返回 (action, reason, detail)；action ∈ BUY / SELL / HOLD。

    can_sell 是 2026-10-20 加的：股票型 ETF 实行 T+1，当天买入的份额当天
    卖不掉。所以「该不该卖」和「能不能卖」必须分开——前者是策略判断，
    后者是交易规则。混在一起就会出现「策略说卖、账上也卖了，但现实中
    根本没这个成交」的假账（这正是当天踩的坑）。
    不能卖时直接 HOLD，不产生任何卖出意图。
    """
    t = ticks[i]["t"]
    tm = hhmm(t)
    price = ticks[i]["price"]
    vwap = ticks[i]["avg"]
    day_ret = price / ctx["preclose"] - 1 if ctx.get("preclose") else 0.0

    # ---- 持仓中：先判出场 ----
    if pos:
        if not can_sell:
            # T+1 锁定：今天买的卖不掉，止盈止损时间止损全部无从执行，
            # 只能等下一个交易日。这里必须明说，不能假装"HOLD 是策略选择"。
            return "HOLD", "", {"locked": True}
        entry = pos["entry"]
        hold = i - pos["entry_idx"]
        pnl = price / entry - 1
        if pnl >= params["take_profit"]:
            return "SELL", f"止盈(+{pnl*100:.2f}%)", {"pnl": pnl, "hold": hold}
        if pnl <= -params["stop_loss"]:
            return "SELL", f"止损({pnl*100:.2f}%)", {"pnl": pnl, "hold": hold}
        if hold >= params["max_hold_min"]:
            return "SELL", f"时间止损({hold}分钟,{pnl*100:+.2f}%)", \
                   {"pnl": pnl, "hold": hold}
        if tm >= params["force_exit"]:
            return "SELL", "尾盘强平", {"pnl": pnl, "hold": hold}
        return "HOLD", "", {}

    # ---- 空仓：判入场 ----
    # 2026-10-28 关闭入场：三年日线 + 两周实盘都没有证据表明这套信号
    # 在扣除成本后有正期望（见 交易思路.md E14）。出场逻辑保留，
    # 手上还有仓位时仍能正常离场。重新打开前必须先拿出样本外为正的证据。
    if not params.get("entry_enabled", True):
        return "HOLD", "", {"disabled": True}
    if ctx["trades"] >= params["max_trades"]:
        return "HOLD", "", {}
    if ctx.get("market_pct", 0) <= params["market_filter"] * 100:
        return "HOLD", "", {}
    if tm < params["earliest_entry"] or tm > params["latest_entry"]:
        return "HOLD", "", {}
    if tm >= params["force_exit"]:
        return "HOLD", "", {}

    dev = dev_from_vwap(ticks, i)
    mom3 = momentum(ticks, i, 3)
    vr = vol_ratio(ticks, i)
    low15 = min(x["price"] for x in ticks[max(0, i - 15):i + 1])
    rebound = price / low15 - 1 if low15 else 0.0

    # 三个条件必须同时成立：偏离足够深、动量拐头、且已脱离近期低点
    if (dev <= -params["dev_enter"]
            and mom3 >= params["mom_turn"]
            and rebound >= params["rebound"]
            and day_ret <= -params["min_day_drop"]):
        detail = {"dev": dev, "mom3": mom3, "vr": vr, "day_ret": day_ret,
                  "rebound": rebound}
        return "BUY", (f"偏离VWAP {dev*100:.2f}% + 反弹 {rebound*100:.2f}% "
                       f"+ 动量 {mom3*100:.2f}%"), detail
    return "HOLD", "", {}
