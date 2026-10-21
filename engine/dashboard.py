# -*- coding: utf-8 -*-
"""桌面看板生成器：把账户、持仓、交易、策略监控渲染成一个自包含 HTML。

用法：python engine/dashboard.py
产物：dashboard/index.html（双击即可打开，无需联网）
"""
import csv
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker      # noqa: E402
import market      # noqa: E402
import strategy    # noqa: E402
import theme       # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "dashboard")


# --------------------------------------------------------------------------
def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_state():
    acc = broker.load_account()
    trades = read_csv(broker.TRADE_FILE)
    equity = read_csv(broker.EQUITY_FILE)
    return acc, trades, equity


def pool_status(total_budget=13.0):
    """拉取池内标的实时状态，计算与入场条件的距离。

    双击看板是交互场景，不能让人对着黑窗口等几分钟。这里给所有联网动作
    设一个总预算：先花最多 5 秒拿批量报价，剩下的时间逐只补分时；预算一
    用完立刻停止联网，剩余标的改用本地缓存拼（连缓存都没有就标「无数据」）。
    """
    t0 = time.time()
    rows = []
    src = {"live": 0, "cache": 0, "miss": 0, "offline": total_budget <= 0}
    quote_live = False

    market.set_budget(min(5.0, total_budget))
    try:
        quotes = market.fetch_quotes(strategy.CORE_POOL)
        quote_live = bool(quotes)
    except Exception:  # noqa: BLE001
        quotes = {}
    finally:
        market.set_budget(None)
    if not quotes:
        quotes, _ = market.load_cached_quotes()

    for code in strategy.CORE_POOL:
        q = quotes.get(code)
        if not q:
            src["miss"] += 1
            continue
        item = {"code": code, "name": q["name"], "price": q["price"],
                "pct": q["pct"], "amount": q["amount"] / 1e8,
                "vwap": None, "dev": None, "rebound": None, "mom3": None,
                "bars": 0,
                "day_ret": q["pct"] / 100, "state": "无分时", "stale": False}

        tr = None
        left = total_budget - (time.time() - t0)
        if quote_live and left > 2.6:
            market.set_budget(left)
            try:
                tr = market.fetch_trends(code, 1, ttl=120)
            except Exception:  # noqa: BLE001
                tr = None
            finally:
                market.set_budget(None)

        if tr and tr.get("ticks"):
            src["live"] += 1
        else:
            data, _ts = market.load_cached_trends(code, 1)
            if data and data.get("ticks"):
                tr = data
                item["stale"] = True
                src["cache"] += 1
            else:
                src["miss"] += 1

        if tr and tr.get("ticks"):
            m = metrics_from_ticks(tr["ticks"])
            if m:
                item.update({"vwap": m["vwap"], "dev": m["dev"],
                             "rebound": m["rebound"], "mom3": m["mom3"],
                             "bars": m["bars"]})
                item["state"] = judge(item, strategy.PARAMS)

        rows.append(item)

    rows.sort(key=lambda x: (x["dev"] if x["dev"] is not None else 99))
    src["elapsed"] = time.time() - t0
    return rows, src


MIN_JUDGE_BARS = 20    # 与 strategy.PARAMS["earliest_entry"]="09:50" 对齐


def metrics_from_ticks(tk):
    """从分时序列算 VWAP 偏离、低点反弹、3 分钟动量。

    只要有数据就算，能算多少算多少；**完全没数据**才返回 None。

    早先这里是「不足 20 根一律返回 None」，后果是每天开盘前 20 分钟十只标的
    全显示「无分时」——数据明明拉到了（报价在跳、时间戳在走），只是不够做
    入场判断。把「没数据」和「数据不够」混成同一句话，最容易被读成接口挂了。

    样本够不够做判断，交给 judge() 按 bars 决定；这里只负责如实算。
    """
    if not tk:
        return None
    i = len(tk) - 1
    low = min(x["price"] for x in tk[max(0, i - 15):i + 1])
    return {
        "price": tk[i]["price"],
        "vwap": tk[i]["avg"],
        "dev": strategy.dev_from_vwap(tk, i) * 100,
        "rebound": (tk[i]["price"] / low - 1) * 100 if low else 0.0,
        # 动量要回看 3 根。样本不足给 None，不能拿 0.0 冒充「动量持平」——
        # 0.0 会走进 ok_mom 的比较，等于凭空宣称一个不存在的读数。
        "mom3": (strategy.momentum(tk, i, 3) * 100) if i >= 3 else None,
        "bars": len(tk),
        "t": tk[i]["t"],
    }


def judge(it, p):
    """判断距离入场还有多远。"""
    if it.get("dev") is None:
        return "无分时"
    bars = it.get("bars") or 0
    if bars < MIN_JUDGE_BARS:
        # 开盘前 20 分钟 VWAP 不稳（策略的 earliest_entry 也是 09:50），
        # 这时报「差 0.72%」等于给一个不能用的数，不如直说还没到时间。
        return "开盘%d分" % bars
    dev_need = -p["dev_enter"] * 100
    if it["dev"] <= dev_need:
        ok_reb = it["rebound"] >= p["rebound"] * 100
        ok_mom = it["mom3"] >= p["mom_turn"] * 100
        ok_day = it["day_ret"] <= -p["min_day_drop"]
        if ok_reb and ok_mom and ok_day:
            return "可入场"
        miss = []
        if not ok_reb:
            miss.append("待反弹")
        if not ok_mom:
            miss.append("待拐头")
        if not ok_day:
            miss.append("跌幅不足")
        return "·".join(miss)
    gap = it["dev"] - dev_need
    return f"差{gap:.2f}%"


# --------------------------------------------------------------------------
def _human_min(sec):
    """秒 -> 「3 小时 21 分」这种人话。"""
    m = int(sec // 60)
    if m < 60:
        return "%d 分钟" % m
    return "%d 小时 %d 分" % (m // 60, m % 60)


def _in_session(now=None):
    t = (now or datetime.now()).strftime("%H:%M")
    return ("09:30" <= t <= "11:30") or ("13:00" <= t <= "15:00")


def exec_note(pool, acc=None):
    """执行状态条：把「这一刻的条件」和「账本执行到哪」摊开讲。

    为什么必须有它：页面算的是**此刻**四个条件是否满足，账本记的是
    **上一次 runner 跑完**的结果。两个时间基准不一样，不说明就会长成
    「明明写着可入场，持仓却是空」这种看起来像系统坏了的矛盾。

    而实际上策略没坏、成交也没丢——runner 是从当日开盘重放全天的，
    漏掉的信号下一轮会补上，补记价还是信号出现那一分钟的真实价。
    真正有问题的是**延迟**：延迟多长，成交就晚多久被确认。
    """
    hb = {}
    try:
        with open(os.path.join(broker.STATE, "heartbeat.json"),
                  "r", encoding="utf-8") as f:
            hb = json.load(f)
    except (OSError, ValueError):
        pass

    last = hb.get("last_run") or ""

    # 已持仓的标的即使条件还满足也不会再开新仓（单票 97% 仓位），
    # 把它算进"没成交"会平白吓人一跳，所以要先排掉。
    held = set((acc or {}).get("positions") or {})
    hot_new = [r for r in pool
               if r.get("state") == "可入场" and r.get("code") not in held]
    if hot_new:
        if held:
            hot_txt = ("　｜　<b>%d 只条件已满足</b>，但当前已有持仓（单票 97%% 仓位），"
                       "不会再开新仓" % len(hot_new))
        else:
            hot_txt = ("　｜　<b>%d 只条件已满足，但账本里还没有这笔</b>："
                       "下一轮重放会补上（按信号出现那一分钟的真实价成交）。"
                       "想让执行跟上实时，双击「启动自动交易.bat」。"
                       % len(hot_new))
    else:
        hot_txt = ""

    if not last:
        return ('<div class="execnote warn">账本还没有任何执行记录</div>')

    try:
        dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ('<div class="execnote warn">账本最后执行：%s</div>' % last)

    sec = (datetime.now() - dt).total_seconds()

    # 收盘后延迟一直涨，没有意义，只报事实不告警
    if not _in_session():
        return ('<div class="execnote ok">账本最后执行 %s　｜　当前非交易时段，'
                '策略不在跑</div>' % last[11:])

    if sec <= 120:
        return ('<div class="execnote ok">执行正常 · 账本最后执行 %s（%d 秒前）%s</div>'
                % (last[11:], int(sec), hot_txt))

    cls = "warn" if sec <= 900 else "alert"
    return ('<div class="execnote %s">账本最后执行 %s，已延迟 <b>%s</b>%s</div>'
            % (cls, last[11:], _human_min(sec), hot_txt))


# --------------------------------------------------------------------------
def svg_curve(equity, width=1000, height=220):
    pts = [(r["date"], float(r["equity"])) for r in equity if r.get("equity")]
    if len(pts) < 2:
        val = pts[0][1] if pts else 10000.0
        hh = 150                    # 空状态不需要占满高度
        return (f'<svg viewBox="0 0 {width} {hh}" class="curve">'
                f'<line x1="40" y1="{hh/2}" x2="{width-20}" y2="{hh/2}" '
                f'stroke="rgba(120,180,255,.20)" stroke-width="1.5" '
                f'stroke-dasharray="5 6"/>'
                f'<text x="{width/2}" y="{hh/2-12}" text-anchor="middle" '
                f'fill="#5f7fa8" font-size="13" letter-spacing="1.5">'
                f'净值曲线积累中 · 当前 ¥{val:,.0f}</text>'
                f'</svg>')
    ys = [p[1] for p in pts]
    lo, hi = min(ys), max(ys)
    span = max(hi - lo, hi * 0.004)
    lo, hi = lo - span * 0.25, hi + span * 0.25
    w, h = width - 60, height - 50
    step = w / max(len(pts) - 1, 1)

    def px(i):
        return 40 + i * step

    def py(v):
        return 20 + h - (v - lo) / (hi - lo) * h

    path = " ".join(f"{'M' if i == 0 else 'L'}{px(i):.1f},{py(v):.1f}"
                    for i, (_, v) in enumerate(pts))
    area = f"{path} L{px(len(pts)-1):.1f},{20+h} L40,{20+h} Z"
    up = ys[-1] >= ys[0]
    color = "#ff4d6a" if up else "#22e6a0"          # 涨红跌绿，霓虹化

    grid = ""
    for k in range(5):
        gy = 20 + h * k / 4
        gv = hi - (hi - lo) * k / 4
        grid += (f'<line x1="40" y1="{gy:.1f}" x2="{width-20}" y2="{gy:.1f}" '
                 f'stroke="rgba(120,180,255,.075)" stroke-width="1"/>'
                 f'<text x="34" y="{gy+4:.1f}" text-anchor="end" fill="#5f7fa8" '
                 f'font-size="10.5">{gv:,.0f}</text>')
    dots = "".join(f'<circle cx="{px(i):.1f}" cy="{py(v):.1f}" r="3" '
                   f'fill="#080d16" stroke="{color}" stroke-width="2"/>'
                   for i, (_, v) in enumerate(pts))
    labels = (f'<text x="40" y="{height-8}" fill="#5f7fa8" font-size="10.5">'
              f'{pts[0][0]}</text>'
              f'<text x="{width-20}" y="{height-8}" text-anchor="end" '
              f'fill="#5f7fa8" font-size="10.5">{pts[-1][0]}</text>')
    return (f'<svg viewBox="0 0 {width} {height}" class="curve">{grid}'
            f'<path d="{area}" fill="{color}" opacity="0.10"/>'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="7" '
            f'stroke-linejoin="round" opacity="0.13"/>'
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round"/>{dots}{labels}</svg>')


def pnl_cls(v):
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def fmt_money(v, sign=False):
    s = f"{v:+,.2f}" if sign else f"{v:,.2f}"
    return f"¥{s}"


# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 资金流场动画
#
# 语义很简单，只做一件事：持仓盈利 → 粒子从节点流向中心；持仓亏损 → 粒子从
# 中心流向节点；未持仓的节点不发光，保持「无资金往来」的视觉含义。
# 用 canvas 而不是 DOM 动画，是因为几十个粒子每秒重绘，DOM 会掉帧。
# --------------------------------------------------------------------------
FLOW_JS = r"""<script>
(function(){
  var cv = document.getElementById('flow');
  if (!cv || !cv.getContext) return;
  var ctx = cv.getContext('2d');
  var box = document.getElementById('orbit');
  var DPR = Math.min(window.devicePixelRatio || 1, 2);
  var W = 0, H = 0, cx = 0, cy = 0;
  var info = [], parts = [];
  var IN_C = '255,77,106', OUT_C = '22,230,160';   // 涨红跌绿

  function collect(){
    var ns = [].slice.call(box.querySelectorAll('.node'));
    info = ns.map(function(n){
      return {x: n.offsetLeft + n.offsetWidth / 2,
              y: n.offsetTop + n.offsetHeight / 2,
              dir: n.getAttribute('data-dir') || 'none'};
    });
  }

  function spawn(){
    parts = [];
    info.forEach(function(o, i){
      if (o.dir === 'none') return;
      var n = o.dir === 'in' ? 18 : 15;
      for (var k = 0; k < n; k++){
        parts.push({ i: i,
          t: k / n + Math.random() * 0.05,               // 均匀铺开，形成连续流
          v: 0.0015 + Math.random() * 0.0011,            // 每毫秒推进量
          w: (Math.random() - 0.5) * 16,                 // 侧向偏移，避免一条直线
          r: 1.1 + Math.random() * 1.6 });
      }
    });
  }

  function resize(){
    W = box.clientWidth; H = box.clientHeight;
    cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
    cv.style.width = W + 'px'; cv.style.height = H + 'px';
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    cx = W / 2; cy = H / 2;
    collect(); spawn();
  }

  function at(o, t, w){
    // t=0 在节点，t=1 在中心；侧向偏移在两端收敛、中间最大
    var x = o.x + (cx - o.x) * t;
    var y = o.y + (cy - o.y) * t;
    var dx = cx - o.x, dy = cy - o.y, len = Math.hypot(dx, dy) || 1;
    var k = Math.sin(t * Math.PI);
    return [x - dy / len * w * k, y + dx / len * w * k];
  }

  var last = 0;
  function frame(now){
    if (!last) last = now;
    var dt = Math.min(now - last, 60);           // 切后台回来别一次跳很远
    last = now;

    ctx.clearRect(0, 0, W, H);

    for (var n = 0; n < parts.length; n++){
      var p = parts[n], o = info[p.i];
      if (!o) continue;
      var step = p.v * dt;
      p.t += (o.dir === 'in' ? step : -step);
      if (p.t > 1){ p.t -= 1; }                   // 流进中心后从节点侧重生
      else if (p.t < 0){ p.t += 1; }

      var a = at(o, p.t, p.w);
      var tail = Math.max(0, Math.min(1, p.t - (o.dir === 'in' ? 0.11 : -0.11)));
      var b = at(o, tail, p.w * 0.55);

      var col = o.dir === 'in' ? IN_C : OUT_C;
      var fade = 0.35 + 0.65 * Math.sin(Math.PI * p.t);   // 两端弱、中间强

      // 拖尾：从上一位置到现在，形成「流动」而不是散点
      ctx.beginPath();
      ctx.strokeStyle = 'rgba(' + col + ',' + (0.50 * fade) + ')';
      ctx.lineWidth = p.r * 1.7;
      ctx.lineCap = 'round';
      ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]);
      ctx.stroke();

      // 外层光晕
      ctx.beginPath();
      ctx.fillStyle = 'rgba(' + col + ',' + (0.16 * fade) + ')';
      ctx.arc(a[0], a[1], p.r * 4.6, 0, 6.2832); ctx.fill();

      // 粒子核
      ctx.beginPath();
      ctx.fillStyle = 'rgba(' + col + ',' + (0.95 * fade) + ')';
      ctx.arc(a[0], a[1], p.r, 0, 6.2832); ctx.fill();
    }

    // 中心内核随流入/流出强度呼吸，让「钱进来了 / 漏出去了」有反馈
    var inflow = 0, outflow = 0;
    for (var q = 0; q < info.length; q++){
      if (info[q].dir === 'in') inflow++; else if (info[q].dir === 'out') outflow++;
    }
    if (inflow || outflow){
      var pulse = 0.5 + 0.5 * Math.sin(now / 620);
      var tot = inflow + outflow;
      var glow = function(col, ratio){
        if (ratio <= 0) return;
        var g = ctx.createRadialGradient(cx, cy, 64, cx, cy, 168);
        g.addColorStop(0, 'rgba(' + col + ',' + (0.155 * ratio + 0.055 * pulse * ratio) + ')');
        g.addColorStop(1, 'rgba(' + col + ',0)');
        ctx.fillStyle = g;
        ctx.beginPath(); ctx.arc(cx, cy, 168, 0, 6.2832); ctx.fill();
      };
      glow(IN_C, inflow / tot);
      glow(OUT_C, outflow / tot);
    }

    // 空仓时没有任何流向，用一条缓慢旋转的雷达弧保持「系统在跑」的观感，
    // 又不会让人误以为有资金在流动
    if (!parts.length){
      var a0 = (now / 2600) % (Math.PI * 2);
      var rx2 = W * 0.305, ry2 = H * 0.295;
      for (var k = 0; k < 30; k++){
        var an = a0 - k * 0.030;
        ctx.beginPath();
        ctx.strokeStyle = 'rgba(34,211,238,' + (0.34 * (1 - k / 30)) + ')';
        ctx.lineWidth = 2;
        ctx.ellipse(cx, cy, rx2, ry2, 0, an, an + 0.035);
        ctx.stroke();
      }
    }

    requestAnimationFrame(frame);
  }

  window.addEventListener('resize', resize, {passive: true});
  // 节点方向/数量可能被实时推送改动，这里暴露一个刷新入口
  window.__flowReset = function(){ collect(); spawn(); };

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches){
    resize();                       // 尊重系统「减少动态效果」，只画静态底
  } else {
    resize();
    requestAnimationFrame(frame);
  }
})();
</script>"""


LIVE_JS = r"""<script>
(function(){
  var TICK = 4000;
  function num(v, n){ return (v === null || v === undefined) ? '\u2014' : v.toFixed(n); }
  function pct(v){ return (v > 0 ? '+' : '') + v.toFixed(2) + '%'; }
  function sgn(v){ return v > 0 ? 'up' : (v < 0 ? 'down' : 'flat'); }
  function setText(id, v){ var e = document.getElementById(id); if(e && e.textContent !== v) e.textContent = v; }

  var lastDir = {};

  function apply(d){
    setText('stamp', d.ts);
    var sn = document.getElementById('srcnote');
    if (sn) sn.innerHTML = d.note || '';
    var dot = document.getElementById('dot');
    if (dot) {
      var c = d.fresh ? '#22e6a0' : '#f5a524';
      dot.style.background = c;
      dot.style.boxShadow = '0 0 9px ' + c;
    }

    // 中心球
    setText('core-val', d.equity);
    var day = document.getElementById('core-day');
    if (day) { day.textContent = '\u4eca\u65e5 ' + d.day; day.className = 'core-day ' + d.day_cls; }
    var tot = document.getElementById('core-tot');
    if (tot) { tot.textContent = '\u7d2f\u8ba1 ' + d.total_pct; tot.className = 'core-tot ' + d.total_cls; }

    // 环形统计
    setText('st-mv', d.mv);
    setText('st-cash', d.cash);
    setText('st-trades', d.n_trades + ' \u7b14');
    setText('st-pos', d.n_pos + ' / 1');

    var dirty = false;
    (d.pool || []).forEach(function(p){
      // 表格行
      var tr = document.querySelector('tr[data-code="' + p.code + '"]');
      if (tr) {
        var q = function(k){ return tr.querySelector('[data-k="' + k + '"]'); };
        var el;
        el = q('price'); if (el) el.textContent = num(p.price, 3);
        el = q('pct');   if (el) { el.textContent = pct(p.pct); el.className = 'num ' + sgn(p.pct); }
        el = q('vwap');  if (el) el.textContent = num(p.vwap, 3);
        el = q('dev');   if (el) { el.textContent = p.dev == null ? '\u2014' : pct(p.dev);
                                    el.className = 'num ' + sgn(-(p.dev || 0)); }
        el = q('reb');   if (el) el.textContent = p.reb == null ? '\u2014' : pct(p.reb);
        el = q('mom');   if (el) el.textContent = p.mom == null ? '\u2014' : pct(p.mom);
        el = q('amt');   if (el) el.textContent = num(p.amt, 1) + '\u4ebf';
        el = q('state'); if (el) { el.textContent = p.state;
                                   el.className = 'pill ' + (p.state === '\u53ef\u5165\u573a'
                                     ? 'hot' : (p.state.indexOf('\u5dee') === 0 ? 'cold' : 'warm')); }
      }
      // 环形节点
      var nd = document.querySelector('.node[data-code="' + p.code + '"]');
      if (nd) {
        var g = function(k){ return nd.querySelector('[data-k="' + k + '"]'); };
        var e2;
        e2 = g('px');  if (e2) e2.textContent = num(p.price, 3);
        e2 = g('pct'); if (e2) { e2.textContent = pct(p.pct); e2.className = 'nd-pct ' + sgn(p.pct); }
        e2 = g('dev'); if (e2) e2.textContent = p.dev == null ? '\u2014' : pct(p.dev);
        var dir = p.dir || 'none';
        if (nd.getAttribute('data-dir') !== dir) {
          nd.setAttribute('data-dir', dir);
          dirty = true;
        }
        var cls = 'node' + (dir !== 'none' ? ' held' : (p.state === '\u53ef\u5165\u573a' ? ' sig' : ''));
        if (nd.className !== cls) nd.className = cls;
      }
    });
    if (dirty && window.__flowReset) window.__flowReset();
  }

  // ---- 接口地址解析 ----
  // 页面可能从两种地方打开：
  //   1) http://127.0.0.1:8899/   —— 服务直出，用相对地址
  //   2) file:///E:/.../index.html —— 双击打开，相对地址必然失败，必须打绝对地址
  // 自动逐个试，谁通用谁。这样即便用户双击的是静态文件，只要后台服务活着，
  // 页面依然会自己连上去变成实时页。
  var URLS = ['/api/state',
              'http://127.0.0.1:8899/api/state',
              'http://127.0.0.1:8900/api/state',
              'http://127.0.0.1:8901/api/state',
              'http://127.0.0.1:8902/api/state'];
  var cur = 0;
  var failStreak = 0;

  function banner(txt){
    var b = document.getElementById('livebar');
    if (!b) return;
    if (!txt) { b.style.display = 'none'; return; }
    b.style.display = 'block';
    b.innerHTML = txt;
  }

  function dead(msg){
    var d = document.getElementById('dot');
    if (d) { d.style.background = '#ff4d6a'; d.style.boxShadow = '0 0 9px #ff4d6a'; }
    var s = document.getElementById('stamp');
    if (s && !msg) s.textContent = '\u5df2\u65ad\u5f00';
  }

  function poll(){
    fetch(URLS[cur], {cache: 'no-store'})
      .then(function(r){
        if (!r.ok) throw new Error('http ' + r.status);
        return r.json();
      })
      .then(function(d){
        failStreak = 0;
        banner('');
        apply(d);
      })
      .catch(function(){
        failStreak++;
        dead();
        cur = (cur + 1) % URLS.length;
        if (failStreak >= URLS.length){
          banner('<b>\u26a0 \u672a\u8fde\u63a5\u5b9e\u65f6\u670d\u52a1</b>'
               + ' \u2014 \u9875\u9762\u4e0a\u662f\u51b7\u5feb\u7167\uff0c\u6570\u5b57\u4e0d\u4f1a\u53d8\u3002'
               + ' \u8bf7\u53cc\u51fb <b>\u6253\u5f00\u6a21\u62df\u76d8.bat</b>'
               + '\uff08\u5b83\u4f1a\u542f\u52a8\u540e\u53f0\u670d\u52a1\u5e76\u6253\u5f00\u5b9e\u65f6\u9875\uff09');
        }
      })
      .then(function(){ setTimeout(poll, failStreak ? 2500 : TICK); });
  }
  poll();
})();
</script>"""


def render(acc, trades, equity, pool, src=None, live=False):
    now = datetime.now()
    src = src or {}
    quotes = {r["code"]: r for r in pool}

    pos_rows, mv, cost_total = [], 0.0, 0.0
    for code, pos in acc["positions"].items():
        sh = broker.position_shares(pos)
        avg = broker.avg_cost(pos)
        q = quotes.get(code) or {}
        px = q.get("price") or avg
        val = sh * px
        mv += val
        cost_total += sh * avg
        pnl = val - sh * avg
        pct = pnl / (sh * avg) * 100 if sh * avg else 0
        pos_rows.append(f"""
        <tr>
          <td><span class="code">{code}</span><span class="nm">{pos.get('name','')}</span></td>
          <td class="num">{sh:,}</td>
          <td class="num">{avg:.3f}</td>
          <td class="num">{px:.3f}</td>
          <td class="num">{val:,.2f}</td>
          <td class="num {pnl_cls(pnl)}">{pnl:+,.2f}</td>
          <td class="num {pnl_cls(pnl)}">{pct:+.2f}%</td>
        </tr>""")
    if not pos_rows:
        pos_rows.append('<tr><td colspan="7" class="empty">当前空仓 · '
                        '策略在等待满足条件的信号</td></tr>')

    trade_rows = []
    for t in list(reversed(trades))[:12]:
        pnl = t.get("pnl") or ""
        pnl_cell = (f'<span class="{pnl_cls(float(pnl))}">{float(pnl):+,.2f}</span>'
                    if pnl not in ("", None) else '<span class="muted">—</span>')
        side = ('<span class="tag buy">买入</span>' if t["side"] == "BUY"
                else '<span class="tag sell">卖出</span>')
        trade_rows.append(f"""
        <tr>
          <td class="mono">{(t.get('datetime') or '')[5:16]}</td>
          <td>{side}</td>
          <td><span class="code">{t['code']}</span> {t.get('name','')}</td>
          <td class="num">{float(t['price']):.3f}</td>
          <td class="num">{int(float(t['shares'])):,}</td>
          <td class="num">{float(t['total_fee']):.2f}</td>
          <td class="num">{pnl_cell}</td>
          <td class="reason">{t.get('reason','')}</td>
        </tr>""")
    if not trade_rows:
        trade_rows.append('<tr><td colspan="8" class="empty">暂无成交记录</td></tr>')

    pool_rows = []
    for r in pool:
        dev = f"{r['dev']:+.2f}%" if r["dev"] is not None else "—"
        reb = f"{r['rebound']:+.2f}%" if r["rebound"] is not None else "—"
        mom = f"{r['mom3']:+.2f}%" if r["mom3"] is not None else "—"
        vwap = f"{r['vwap']:.3f}" if r["vwap"] else "—"
        state = r["state"]
        cls = "hot" if state == "可入场" else ("cold" if state.startswith("差") else "warm")
        day_cls = pnl_cls(r["pct"])
        pool_rows.append(f"""
        <tr data-code="{r['code']}">
          <td><span class="code">{r['code']}</span> <span class="nm">{r['name'][:12]}</span></td>
          <td class="num" data-k="price">{r['price']:.3f}</td>
          <td class="num {day_cls}" data-k="pct">{r['pct']:+.2f}%</td>
          <td class="num" data-k="vwap">{vwap}</td>
          <td class="num {pnl_cls(-(r['dev'] or 0))}" data-k="dev">{dev}</td>
          <td class="num" data-k="reb">{reb}</td>
          <td class="num" data-k="mom">{mom}</td>
          <td class="num" data-k="amt">{r['amount']:.1f}亿</td>
          <td><span class="pill {cls}" data-k="state">{state}</span></td>
        </tr>""")

    # 环形节点：每只标的到中心的资金流向
    #
    # 定位用「百分比 + 角度」，不用固定像素：容器宽度随窗口变，百分比保证
    # 圆周始终贴合容器（横竖半径不同 → 实际是椭圆，正好填满 680x620 的框）。
    # 不做 JS 定位，静态 HTML 也能正确定位。
    import math
    pos_map = acc.get("positions") or {}
    node_html = []
    n_node = max(len(pool), 1)
    RX, RY = 37.0, 35.0                  # 横/竖半径（容器百分比）
    for i, r in enumerate(pool):
        held = r["code"] in pos_map
        direction = "none"
        if held:
            _pos = pos_map[r["code"]]
            _avg = broker.avg_cost(_pos)
            direction = "in" if r["price"] >= _avg else "out"
        state_cls = ""
        if r["state"] == "可入场":
            state_cls = " sig"
        elif held:
            state_cls = " held"
        dev_txt = "—" if r["dev"] is None else f"{r['dev']:+.2f}%"
        ang = -math.pi / 2 + 2 * math.pi * i / n_node          # 从正上方开始顺时针
        lx = 50.0 + RX * math.cos(ang)
        ly = 50.0 + RY * math.sin(ang)
        node_html.append(f"""
      <div class="node{state_cls}" data-code="{r['code']}" data-dir="{direction}"
           style="left:{lx:.2f}%;top:{ly:.2f}%">
        <div class="nd-code">{r['code']}</div>
        <div class="nd-px" data-k="px">{r['price']:.3f}</div>
        <div class="nd-pct {pnl_cls(r['pct'])}" data-k="pct">{r['pct']:+.2f}%</div>
        <div class="nd-dev" data-k="dev">{dev_txt}</div>
      </div>""")

    equity_val = acc["cash"] + mv
    init = acc["meta"]["init_capital"]
    total_pnl = equity_val - init
    total_pct = total_pnl / init * 100
    day_pnl = float(equity[-1]["day_pnl"]) if equity else 0.0
    n_trades = len([t for t in trades if t["side"] == "SELL"])

    p = strategy.PARAMS

    n_live = int(src.get("live", 0) or 0)
    n_cache = int(src.get("cache", 0) or 0)
    # 提示要分得清「我们自己没联网」和「联网了但没拿到」——两者的处置完全不同
    if src.get("offline"):
        src_note = ' <em class="warn">· 离线快照（未联网）</em>'
    elif n_live == 0 and n_cache > 0:
        src_note = ' <em class="warn">· 全部读本地缓存（本轮联网未成功）</em>'
    elif n_cache > 0:
        src_note = f' <em class="warn">· {n_cache} 只读缓存</em>'
    else:
        src_note = ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A股模拟盘 · ¥10,000</title>
<style>{theme.PAGE_CSS}</style></head><body><div class="wrap">

<div id="livebar" style="display:none"></div>
<header>
  <h1>A股模拟盘 <span>初始资金 ¥10,000 · 策略 v1.2</span></h1>
  <div class="stamp"><span class="dot" id="dot"></span>数据更新 <span id="stamp">{now:%Y-%m-%d %H:%M:%S}</span><span id="srcnote">{src_note}</span></div>
</header>

<div class="card orbit-card">
  <h2>资金流场 <em>中心 = 账户净额 · 环形 = 标的池 · 盈利流入 / 亏损流出</em></h2>
  <div class="orbit" id="orbit">
    <canvas id="flow"></canvas>
    <div class="core">
      <div class="core-halo"></div>
      <div class="core-tick"></div>
      <div class="core-tick2"></div>
      <div class="core-inner">
        <div class="core-lab">TOTAL ASSETS</div>
        <div class="core-val" id="core-val">{fmt_money(equity_val)}</div>
        <div class="core-day {pnl_cls(day_pnl)}" id="core-day">今日 {day_pnl:+,.2f}</div>
        <div class="core-tot {pnl_cls(total_pnl)}" id="core-tot">累计 {total_pct:+.2f}%</div>
      </div>
    </div>
    {''.join(node_html)}
  </div>
  <div class="orbit-stats">
    <div><span>持仓市值</span><b id="st-mv">{fmt_money(mv)}</b></div>
    <div><span>可用现金</span><b id="st-cash">{fmt_money(acc['cash'])}</b></div>
    <div><span>已完成交易</span><b id="st-trades">{n_trades} 笔</b></div>
    <div><span>持仓数量</span><b id="st-pos">{len(acc['positions'])} / 1</b></div>
    <div><span>每日上限</span><b>{p['max_trades']} 笔</b></div>
  </div>
</div>

<div class="card"><h2>净值曲线 <em>equity.csv</em></h2>{svg_curve(equity)}</div>

<div class="card"><h2>持仓 <em>成本按先进先出核销</em></h2>
<div class="tscroll"><table><thead><tr><th>标的</th><th class="num">份额</th><th class="num">成本</th>
<th class="num">现价</th><th class="num">市值</th><th class="num">浮动盈亏</th>
<th class="num">收益率</th></tr></thead>
<tbody>{''.join(pos_rows)}</tbody></table></div></div>

<div class="card"><h2>策略监控 <em>距 R2 入场条件还差多少</em></h2>
{exec_note(pool, acc)}
<div class="tscroll"><table><thead><tr><th>标的</th><th class="num">现价</th><th class="num">涨跌</th>
<th class="num">VWAP</th><th class="num">偏离</th><th class="num">低点反弹</th>
<th class="num">3min动量</th><th class="num">成交额</th><th>状态</th></tr></thead>
<tbody>{''.join(pool_rows)}</tbody></table></div></div>

<div class="two">
  <div class="card"><h2>规则 v1.2</h2>
    <div class="rule">
      入场 <b>R2</b>：偏离 <span>≤ -{p['dev_enter']*100:.2f}%</span> + 反弹 <span>≥ {p['rebound']*100:.2f}%</span>
      + 动量 <span>&gt; {p['mom_turn']*100:.2f}%</span> + 当日跌 <span>≥ {p['min_day_drop']*100:.1f}%</span><br>
      止盈 <span>+{p['take_profit']*100:.2f}%</span> · 止损 <span>-{p['stop_loss']*100:.2f}%</span> ·
      时间止损 <span>{p['max_hold_min']} 分钟</span><br>
      时段 <span>{p['earliest_entry']}–{p['latest_entry']}</span> 开仓 ·
      <span>{p['force_exit']}</span> 强平<br>
      仓位 <span>97%</span> 单票 · 每日 <span>≤ {p['max_trades']} 笔</span> ·
      大盘熔断 <span>{p['market_filter']*100:.1f}%</span>
    </div></div>
  <div class="card"><h2>策略迭代 <em>10/13–10/16 回放 · 非实盘</em></h2>
    <div class="tscroll"><table><thead><tr><th>版本</th><th>关键改动</th><th class="num">收益</th>
    <th class="num">笔数</th><th class="num">胜率</th></tr></thead><tbody>
      <tr><td class="muted">v1.0</td><td>偏离 0.55% 入场，回 VWAP 止盈</td>
        <td class="num down">-1.69%</td><td class="num">6</td>
        <td class="num down">0%</td></tr>
      <tr><td class="muted">v1.1</td><td>偏离提到 0.8%，按入场价止盈止损</td>
        <td class="num down">-1.19%</td><td class="num">6</td>
        <td class="num">16.7%</td></tr>
      <tr><td><b>v1.2</b></td><td>加「脱离15分钟低点 0.3%」确认</td>
        <td class="num up">+0.59%</td><td class="num">2</td>
        <td class="num up">50%</td></tr>
    </tbody></table></div>
    <div style="font-size:11px;color:var(--dim);margin-top:10px;line-height:1.8;letter-spacing:.4px">
      关键证据：偏离 0.55% 的 5 分钟胜率仅 48.8%，扣 0.15% 成本后剩 39%
      ——入场门槛本身就选错了，不是参数没调好。
    </div>
  </div>
</div>

<div class="card"><h2>交易流水 <em>最近 12 条</em></h2>
<div class="tscroll"><table><thead><tr><th>时间</th><th>方向</th><th>标的</th><th class="num">价格</th>
<th class="num">数量</th><th class="num">费用</th><th class="num">盈亏</th>
<th>理由</th></tr></thead>
<tbody>{''.join(trade_rows)}</tbody></table></div></div>

<footer>模拟盘 · 非真实交易 · 数据源：东方财富 / 腾讯自选股</footer>
</div>{FLOW_JS}{LIVE_JS}</body></html>"""


def build(quiet=False, budget=13.0):
    """生成看板。budget 为联网取数的总时间上限（秒），0 = 完全离线用缓存。"""
    t_start = time.time()
    acc, trades, equity = load_state()
    if not quiet:
        print("  正在获取行情（最多 %.0f 秒，超时自动用缓存）..." % budget
              if budget > 0 else "  离线模式：直接读取本地缓存...")
    pool, src = pool_status(total_budget=budget)
    html = render(acc, trades, equity, pool, src)
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "index.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    if not quiet:
        print(f"看板已生成: {path}")
        print(f"  标的 {len(pool)} 只 · 持仓 {len(acc['positions'])} 只 · "
              f"流水 {len(trades)} 条")
        print(f"  数据来源: 实时 {src['live']} 只 / 缓存 {src['cache']} 只 / "
              f"缺失 {src['miss']} 只 · 耗时 {time.time() - t_start:.1f}s")
        hot = [r for r in pool if r["state"] == "可入场"]
        if hot:
            print(f"  ⚡ 可入场信号: {', '.join(r['name'] for r in hot)}")
    return path


if __name__ == "__main__":
    budget = 13.0
    for a in sys.argv[1:]:
        if a in ("--fast", "--offline"):
            budget = 0.0
        elif a.startswith("--budget="):
            budget = float(a.split("=", 1)[1])
    build(budget=budget)
