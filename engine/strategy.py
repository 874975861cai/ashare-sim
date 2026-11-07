# -*- coding: utf-8 -*-
"""策略层：T+0 日内均值回归（VWAP 偏离）。

设计前提（用数学而非直觉）：
  1 万本金单笔摩擦 ≈ tick 0.05% + 佣金 0.10% ≈ 0.15%，
  因此只做「偏离足够大 + 有拐头确认」的机会，单笔目标价差 ≥ 0.4%。

核心假设：
  ETF 是成分股的加权，日内缺乏独立趋势，价格围绕当日 VWAP 波动；
  当偏离 VWAP 超过一定阈值且出现动量拐头时，回归概率占优。
"""

from datetime import datetime

# ---------------- 默认参数（v2.0 组合持有） ----------------
# 校准依据：engine/analyze.py 信号生存分析 + engine/backtest.py 路径回放
#           + engine/hiret.py 高收益结构研究（11 年日线、18 只标的）
PARAMS = {
    # ==================================================================
    # 2026-11-07 第三次转向：mode="mix"（跨市场配比组合）
    # ==================================================================
    # 为什么从「单标的持有红利」换掉：
    #   红利十年年化 4.88%、回撤 -20%，是这套池子 + 1 万本金的收益天花板。
    #   用户要「高收益高风险 + 资金加到 10 万」，那就必须换资产本身，
    #   而不是继续在低波动资产上找买卖点（130 个择时组合已全部证伪）。
    #
    # 换什么，用 11 年日线实测（engine/hiret.py 第一层）：
    #   只换 A 股高 beta 是错的方向——风险翻倍、收益几乎不动：
    #     创业板 年化 4.81% 回撤 -69.59%（对照红利 4.78% / -43.29%）
    #     中证1000 -0.06% / -51.00%；证券 0.14% / -45.47%
    #     科创50 -2.68% / -59.69%；医疗 0.78% / -71.79%
    #   唯一同时做到「收益翻倍、回撤不放大」的是跨市场组合（第七层）：
    #     纳4金3创3   年化 18.00% 回撤 -21.30% 夏普 1.08
    #     纳4金3中500 3 年化 16.77% 回撤 -16.28% 夏普 1.12
    #     纯A股高beta  年化  9.35% 回撤 -46.40% 夏普 0.48  <- 对照组，明显更差
    #   且样本内外高度一致（第八层）：纳4金3创3 样本内 17.63% / 样本外 18.42%。
    #
    # 为什么这套组合不需要择时：
    #   它赚的不是「预测」，是「低相关性 + 再平衡」——三份资产各自漂，
    #   每年拉回一次目标权重，等于自动高卖低买。日频分钟线择时在这里
    #   是纯损耗（第三/四层：所有轮动与单标的时序动量，样本外都衰减）。
    #   唯一有真实贡献的是「防守资产换成黄金/国债」，那是资产配置，
    #   不是信号——所以直接写进配比，不写进决策逻辑。
    #
    # 风险披露（必须写在策略里，不能只在报告里说）：
    #   18% 的历史年化里，纳指（11 年 19.03%）和黄金（11 年 10.99%）
    #   贡献了绝大部分。这是「美股 + 黄金」两段历史大牛的延续假设，
    #   不是 A 股能给的。若两者均值回归，实际年化会明显低于 18%。
    #   能确定的是回撤结构：三资产相关性低，-21% 是历史最差，不是常态。
    #
    # 10 万本金的算术变了（这是本次能落地的关键）：
    #   佣金 5 元下限，单笔 3.3 万 = 0.015%，三笔往返 0.09%；
    #   1 万本金下同样三笔 = 0.30%，分散的成本是现在的三倍多。
    "mode": "mix",
    # 权重和不为 1 时按比例归一（plan_mix 会做），这里保持整数便于读。
    "mix_weights": {
        "513100": 0.40,   # 纳指ETF：与 A 股相关性最低，收益引擎
        "518880": 0.30,   # 黄金ETF：危机期的对手盘，回撤缓冲
        "159915": 0.30,   # 创业板ETF：A 股高 beta 敞口，避免全押海外
    },
    "mix_time": "09:35",      # 开盘 5 分钟后建仓/调仓，避开集合竞价噪音
    "mix_rebal_days": 244,    # 约一年（交易日）拉回一次目标权重
    # 偏离带只作极端脱靶兜底。正常年份内不动——频繁再平衡会把
    # 「低相关 + 再平衡」的收益磨成手续费。
    "mix_band": 0.12,

    # ---- 以下为旧持有型参数，mode="mix" 时不生效，保留以便对照 ----
    "hold_target": "510880",
    "hold_entry_time": "09:35",
    "hold_stop": -0.25,
    "hold_cooldown_days": 30,

    # ---- 以下为旧日内策略参数，mode="hold" 时不生效，保留以便对照 ----
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
    "513100",   # 纳指ETF国泰（跨境，~10 亿）—— mix 组合的收益引擎
    "518880",   # 黄金ETF华安（商品，~28 亿）—— mix 组合的回撤缓冲
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


def held_minutes(ticks, entry_idx, i):
    """从入场那一根到当前这一根，实际过去了多少分钟。

    为什么不能直接用 i - entry_idx：只有连续竞价时段内索引才等于分钟数。
    跨午休时 11:30 与 13:01 在索引上相邻，实际差 91 分钟——「持有 20 分钟」
    会在 11:25 建仓后一路拖到 13:20 才触发（真实持有 115 分钟），而日志上
    仍写着「时间止损(20分钟)」。2026-11-04 在 analyze.py 里抓到同一类错
    （60 分档 40 个样本里 8 个实际是 150 分钟），这里是它的上游。
    entry_idx < 0 是「隔夜续管」的老约定，按今日第一根算（同 runner）。
    """
    try:
        k0 = int(entry_idx)
    except (TypeError, ValueError):
        k0 = 0
    if k0 < 0:
        k0 = 0
    try:
        a = ticks[k0]["t"]
        b = ticks[i]["t"]
        d = (datetime.strptime(b, "%Y-%m-%d %H:%M")
             - datetime.strptime(a, "%Y-%m-%d %H:%M"))
        return int(d.total_seconds() // 60)
    except (KeyError, IndexError, TypeError, ValueError):
        # 时间戳坏了就退回索引差：宁可少一分精度，也不要抛异常把整轮回放打断
        return i - k0


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

    # ---- 持有型（mode="hold"）：不猜买卖点，只建仓并持有 ----
    # 这一段是整个策略的核心，逻辑必须短到能一眼看完。它成立不是因为
    # 它聪明，而是因为 3 年数据证明「在这个池子上做任何择时都更差」。
    if params.get("mode") == "hold":
        if pos:
            if not can_sell:
                # T+1：今天买的，今天卖不掉。熔断也只能等明天。
                return "HOLD", "", {"locked": True}
            pnl = price / pos["entry"] - 1
            if pnl <= params["hold_stop"]:
                return "SELL", "熔断(浮亏%.1f%%)" % (pnl * 100), \
                       {"pnl": pnl, "hold": i - pos["entry_idx"]}
            # 没有止盈、没有止损、没有时间止损、没有尾盘强平。
            # 这不是漏写——加上任何一条，样本内收益就从 +30% 变成负数。
            return "HOLD", "", {"hold": True}
        if tm < params["hold_entry_time"]:
            return "HOLD", "", {}
        return "BUY", "建仓持有", {"dev": 1.0}

    # ---- 持仓中：先判出场 ----
    if pos:
        if not can_sell:
            # T+1 锁定：今天买的卖不掉，止盈止损时间止损全部无从执行，
            # 只能等下一个交易日。这里必须明说，不能假装"HOLD 是策略选择"。
            return "HOLD", "", {"locked": True}
        entry = pos["entry"]
        # 持有期按真实分钟算，不按「第几根 K 线」算（跨午休会差 90 分钟）
        hold = held_minutes(ticks, pos.get("entry_idx"), i)
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


# ---------------- 组合层决策（mode="mix"） ----------------
def _mix_weights(params=PARAMS):
    """归一化后的目标权重。归一这一步旧注释承诺过、代码却没做：
    权重和 1.2 时会按 1.2 倍下单，最后一只必然被现金裁掉。"""
    raw = {}
    for c, v in (params.get("mix_weights") or {}).items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            raw[c] = fv
    tot = sum(raw.values())
    return {c: v / tot for c, v in raw.items()} if tot > 0 else {}


def mix_on_target(equity, holdings, prices, params=PARAMS):
    """所有目标标的的权重是否都已在带内。

    用来决定「这次调仓算不算完成」。以前只看「有没有下过单」就记
    `mix_last_rebal`：某只标的当时没报价（QDII 常有）、或被涨停/现金
    挡掉，组合会带着一块没投出去的钱，而下次再平衡要等 342 天。
    没有报价一律算「判不了」= 未到位，宁可明日重试，也不要假装完成。
    """
    w = _mix_weights(params)
    band = float(params.get("mix_band", 0.12))
    if not w or not equity or equity <= 0:
        return False
    for c, wt in w.items():
        p = prices.get(c)
        if not p or p <= 0:
            return False
        cur_w = (int(holdings.get(c) or 0) * p) / equity
        if abs(cur_w - wt) >= band:
            return False
    return True


def plan_mix(equity, holdings, prices, params=PARAMS):
    """按目标权重生成调仓指令。返回 (orders, diag)。

    这是 mix 模式全部的交易逻辑：没有买卖点、没有止损、没有尾盘强平，
    只有「现在各持多少 vs 该持多少」。它成立的理由在 hiret.py 第七/八层：
    跨市场低相关资产的年度再平衡，样本内外收益一致；而任何日频择时
    在样本外都衰减。所以这里刻意不做任何预测。

    holdings : {code: 股数}（含旧策略遗留的、不在目标里的标的）
    prices   : {code: 现价}
    orders   : 卖出在前、买入在后 —— 先腾出现金再买，顺序不能反。

    目标市值必须按 **equity** 算（目标权重 × 总权益），不能按「可用现金」算。
    2026-11-07 实测的旧写法就是拿现金算的：满仓时可用现金≈0，于是每个标的
    的目标股数都被算成 0，再平衡变成「把脱靶的那只全部清仓」——
    31600 份 513100 该减到 40%，实际下了 31600 份的卖单；
    而低配到 15% 的 159915 也被「减至 30%(现15%)」卖掉了 4450 份。
    两个方向都错，且都以「减至/补至 XX%」的措辞落账，事后看完全正常。
    现金只约束**买入**：卖出腾出的钱先到账，再按比例分配给要买的标的。
    """
    # 权重归一（和=1）后统一使用；旧代码没归一，权重和 1.2 时会按 1.2 倍下单。
    w = _mix_weights(params)
    band = float(params.get("mix_band", 0.12))
    try:
        equity = float(equity)
    except (TypeError, ValueError):
        equity = 0.0
    hold = {}
    for c, sh in (holdings or {}).items():
        try:
            n = int(sh)
        except (TypeError, ValueError):
            continue
        if n > 0:
            hold[c] = n
    orders, diag = [], []
    if not w:
        return [], ["mix_weights 为空或全为非正数，不调仓"]
    if equity <= 0:
        # 权益非正时按比例算出来的目标全是 0，会把仓位「合法地」清零。
        # 这是账本异常，不是调仓信号，必须停下来交给人看。
        return [], ["权益 %.2f 非正，不调仓" % equity]

    # 1 不在目标里的旧持仓：全额卖出。
    #   2026-11-07 这次从 510880 换到组合，第一次运行就靠这条清掉红利仓。
    evict = []
    for c in sorted(hold):
        if c in w:
            continue
        if not prices.get(c):
            diag.append("%s 无报价，暂不卖出（市值按 0 计入预算）" % c)
            continue
        evict.append({"code": c, "side": "SELL", "shares": hold[c],
                      "reason": "移出组合"})

    # 2 目标标的：先算「要不要动」，再算「动谁」。
    #
    # 带（mix_band）只决定**触发**，不决定**只动哪一只**。这个区分是必须的：
    # 只减超配那一只、其余照带内不动，剩下的现金会躺到下一次再平衡（约一年），
    # 组合实际变成「40/21/21+18%现金」——比不调仓还偏。
    # 2026-11-07 实测：纳指翻倍触发减仓，卖出后 2.4 万现金闲置。
    # 所以一旦有标的脱靶，就把所有标的拉回目标（经典 band rebalance：
    # 触发频率低、单次全量、总成本反而更低）。
    plans = []
    trigger = ""
    for c in sorted(w):
        wt = w[c]
        p = prices.get(c)
        if not p or p <= 0:
            diag.append("%s 无报价，跳过" % c)
            continue
        cur = hold.get(c, 0)
        cur_w = cur * p / equity
        # 目标股数按整手向下取整：宁可少买一手，不要为凑整超配
        tgt = int(equity * wt / p / 100) * 100
        plans.append({"code": c, "wt": wt, "cur": cur, "cur_w": cur_w,
                      "tgt": tgt, "diff": tgt - cur})
        if not trigger and abs(cur_w - wt) >= band:
            trigger = "%s 权重 %.1f%% 偏离目标 %.0f%% 超过带宽 %.0f%%" % (
                c, cur_w * 100, wt * 100, band * 100)

    trims, buys = [], []
    if not trigger:
        for x in plans:
            diag.append("%s 权重%.1f%%(目标%.1f%%)在带内"
                        % (x["code"], x["cur_w"] * 100, x["wt"] * 100))
    else:
        diag.append("触发全量再平衡：%s" % trigger)
        for x in plans:
            if x["diff"] >= 100:
                buys.append({"code": x["code"], "side": "BUY",
                             "shares": x["diff"],
                             "reason": "补至%.0f%%(现%.0f%%)"
                                       % (x["wt"] * 100, x["cur_w"] * 100)})
            elif x["diff"] <= -100:
                trims.append({"code": x["code"], "side": "SELL",
                              "shares": -x["diff"],
                              "reason": "减至%.0f%%(现%.0f%%)"
                                        % (x["wt"] * 100, x["cur_w"] * 100)})
            else:
                diag.append("%s 已在目标附近，不动" % x["code"])

    # 3 现金只约束买入：可用 = 现有现金 + 卖出所得，留 0.5% 给佣金与整手零头。
    mv_priced = sum(sh * prices[c] for c, sh in hold.items() if prices.get(c))
    lost = [c for c in hold if not prices.get(c)]
    if lost:
        # 没报价的持仓既卖不掉也无法估值，equity 里却已经算过它（调用方按成本估），
        # 于是 cash_now 会被高估。必须说出来，否则以为「现金够」，实际被券商拒单。
        diag.append("%s 无报价，可用现金可能被高估" % "、".join(sorted(lost)))
    cash_now = max(equity - mv_priced, 0.0)
    sell_in = sum(o["shares"] * prices[o["code"]]
                  for o in evict + trims if prices.get(o["code"]))
    avail = (cash_now + sell_in) * 0.995
    need = sum(o["shares"] * prices[o["code"]] for o in buys)
    if need > avail:
        scale = (avail / need) if need else 0.0
        scaled = []
        for o in buys:
            sh = int(o["shares"] * scale) // 100 * 100
            if sh >= 100:
                # 缩量到 99.x% 是常态（留了佣金与整手零头），别为此改写指令理由，
                # 否则每条买单都挂一句「按可用现金缩量」，真正的缩量反而看不出来。
                why = o["reason"] + ("，按可用现金缩量" if scale < 0.95 else "")
                scaled.append({**o, "shares": sh, "reason": why})
        diag.append("买入需 %.0f 元 > 可用 %.0f 元，按 %.1f%% 缩量"
                    % (need, avail, scale * 100))
        buys = scaled

    return evict + trims + buys, diag
    # 3 现金只约束买入：可用 = 现有现金 + 卖出所得，留 0.5% 给佣金与整手零头。
    mv_priced = sum(sh * prices[c] for c, sh in hold.items() if prices.get(c))
    lost = [c for c in hold if not prices.get(c)]
    if lost:
        # 没报价的持仓既卖不掉也无法估值，equity 里却已经算过它（调用方按成本估），
        # 于是 cash_now 会被高估。必须说出来，否则以为「现金够」，实际被券商拒单。
        diag.append("%s 无报价，可用现金可能被高估" % "、".join(sorted(lost)))
    cash_now = max(equity - mv_priced, 0.0)
    sell_in = sum(o["shares"] * prices[o["code"]]
                  for o in evict + trims if prices.get(o["code"]))
    avail = (cash_now + sell_in) * 0.995
    need = sum(o["shares"] * prices[o["code"]] for o in buys)
    if need > avail:
        scale = (avail / need) if need else 0.0
        scaled = []
        for o in buys:
            sh = int(o["shares"] * scale) // 100 * 100
            if sh >= 100:
                # 缩量到 99.x% 是常态（留了佣金与整手零头），别为此改写指令理由，
                # 否则每条买单都挂一句「按可用现金缩量」，真正的缩量反而看不出来。
                why = o["reason"] + ("，按可用现金缩量" if scale < 0.95 else "")
                scaled.append({**o, "shares": sh, "reason": why})
        diag.append("买入需 %.0f 元 > 可用 %.0f 元，按 %.1f%% 缩量"
                    % (need, avail, scale * 100))
        buys = scaled

    return evict + trims + buys, diag


def mix_rebal_due(last_day, day, params=PARAMS):
    """距上次调仓是否到期。244 个交易日 ≈ 342 个自然日。

    首次运行（last_day 为空）永远到期——否则新策略会因为账本里没有
    调仓记录而永远不建仓。
    """
    if not last_day:
        return True
    try:
        d0 = datetime.strptime(str(last_day)[:10], "%Y-%m-%d")
        d1 = datetime.strptime(str(day)[:10], "%Y-%m-%d")
    except ValueError:
        return True
    need = int(params.get("mix_rebal_days", 244)) * 7 // 5
    return (d1 - d0).days >= need
