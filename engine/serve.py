#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实时看板服务。

为什么需要它：
    dashboard.py 生成的是**静态 HTML**，浏览器打开后那个文件不会自己变，
    所以页面上永远停在生成那一刻的时间。要做到「11:23 打开就是 11:23 的数」，
    必须有一个常驻进程在中间转数据。

架构：
    后台线程  —— 按接口限流能承受的节奏，循环刷新 10 只标的
    内存状态  —— 刷新结果存放处，页面只读这里，不直接打接口
    HTTP 服务 —— / 返回看板 HTML；/api/state 返回 JSON
    页面 JS   —— 每 4 秒拉一次 /api/state，就地更新数字，不整页刷新

取数节奏（东财全局节流 2.2s，不能打太狠）：
    批量报价   每 20 秒一次（一次请求拿全部 10 只）
    分时       轮转，每轮一只，10 只约 35 秒走完一圈
    页面轮询   每 4 秒（只读内存，零额外请求）

用法：
    python engine/serve.py                # 默认 8899 端口，自动开浏览器
    python engine/serve.py --port=9000
    python engine/serve.py --no-browser   # 只起服务
"""
import argparse
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# pythonw.exe 启动时 sys.stdout 是 None，任何 print() 都可能炸。
# 开机自启用的就是 pythonw（不弹黑窗），所以这里兜一层。
for _s in ("stdout", "stderr"):
    if getattr(sys, _s, None) is None:
        setattr(sys, _s, open(os.devnull, "w", encoding="utf-8"))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker       # noqa: E402
import dashboard    # noqa: E402
import market       # noqa: E402
import strategy     # noqa: E402

POOL = list(strategy.CORE_POOL)
QUOTE_EVERY = 10.0      # 批量报价刷新间隔（秒）——腾讯一次请求拿全部，很便宜
TREND_IDLE = 0.2        # 分时轮转额外间隔（叠加腾讯 0.5s 节流后 ≈0.7s/只）
FRESH_WINDOW = 60.0     # 分时在这一窗口内刷过就算「实时」
STATIC_EVERY = 30.0     # 每 30 秒把快照回写到 dashboard/index.html

STATE = {
    "quotes": {},
    "trends": {},       # code -> {"ticks": [...], "ts": float}
    "quote_ts": 0.0,
    "last_ok": 0.0,
    "errors": 0,
    "rounds": 0,
}
LOCK = threading.Lock()
STOP = threading.Event()


# --------------------------------------------------------------------------
# 状态初始化与刷新
# --------------------------------------------------------------------------
def bootstrap():
    """启动瞬间用本地缓存把画面填满，不让人对着空表等。

    批量报价缓存可能不完整（上次只拉到几只），此时用分时缓存的最后一笔
    补一个临时报价，保证表格一开始就是 10 行，几秒后被真实报价覆盖。
    """
    q, ts = market.load_cached_quotes()
    quotes = dict(q or {})
    trends = {}
    for code in POOL:
        data, cts = market.load_cached_trends(code, 1)
        if not (data and data.get("ticks")):
            continue
        trends[code] = {"ticks": data["ticks"], "ts": float(cts or 0)}
        if code not in quotes:
            tk = data["ticks"]
            last = tk[-1]["price"]
            prev = data.get("preclose") or tk[0]["price"]
            quotes[code] = {
                "code": code,
                "name": data.get("name") or code,
                "price": last,
                "pct": (last / prev - 1) * 100 if prev else 0.0,
                "amount": 0.0,
            }
    with LOCK:
        STATE["quotes"] = quotes
        STATE["trends"] = trends
        STATE["quote_ts"] = float(ts or 0)


def _refresh_quotes():
    market.set_budget(10)
    try:
        q = market.fetch_quotes(POOL)
    except Exception:  # noqa: BLE001
        q = {}
    finally:
        market.set_budget(None)
    if q:
        with LOCK:
            STATE["quotes"].update(q)
            STATE["quote_ts"] = time.time()
            STATE["last_ok"] = time.time()
        return True
    return False


def _refresh_trend(code):
    market.set_budget(10)
    try:
        tr = market.fetch_trends(code, 1, ttl=0)
    except Exception:  # noqa: BLE001
        return False
    finally:
        market.set_budget(None)
    tk = (tr or {}).get("ticks") or []
    if not tk:
        return False
    with LOCK:
        STATE["trends"][code] = {"ticks": tk, "ts": time.time()}
        STATE["last_ok"] = time.time()
    return True


def write_static():
    """把当前快照回写到 dashboard/index.html。

    为什么服务已经在跑还要写静态文件：
    用户习惯直接双击 index.html，而不是每次都走 bat。那份文件是磁盘上的
    快照，不重写就永远停在生成那一刻。这里每 30 秒覆盖一次，所以即便
    后台服务哪天挂了，F5 一下也能看到不超过半分钟的数，而不是几十分钟前。
    写失败不影响服务（例如文件被占用），静默跳过。
    """
    try:
        rows, src = snapshot()
        acc, trades, equity = dashboard.load_state()
        html = dashboard.render(acc, trades, equity, rows, src, live=False)
        out = os.path.join(dashboard.BASE, "dashboard", "index.html")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(html)
        os.replace(tmp, out)
        return True
    except Exception:  # noqa: BLE001
        return False


def refresher():
    """后台刷新循环。报价优先，其余时间轮流补分时。"""
    idx = 0
    last_q = 0.0
    last_write = 0.0
    while not STOP.is_set():
        try:
            if time.time() - last_q >= QUOTE_EVERY:
                ok = _refresh_quotes()
                last_q = time.time()
                if not ok:
                    with LOCK:
                        STATE["errors"] += 1
                    STOP.wait(4.0)
                    continue
            else:
                code = POOL[idx % len(POOL)]
                idx += 1
                if not _refresh_trend(code):
                    with LOCK:
                        STATE["errors"] += 1
                else:
                    with LOCK:
                        STATE["rounds"] += 1
                STOP.wait(TREND_IDLE)

            if time.time() - last_write >= STATIC_EVERY:
                write_static()
                last_write = time.time()
        except Exception:  # noqa: BLE001
            with LOCK:
                STATE["errors"] += 1
            STOP.wait(3.0)


# --------------------------------------------------------------------------
# 快照
# --------------------------------------------------------------------------
def snapshot():
    """整理成 render / API 共用的结构。"""
    with LOCK:
        quotes = dict(STATE["quotes"])
        trends = dict(STATE["trends"])
        quote_ts = STATE["quote_ts"]
        errors = STATE["errors"]

    fresh_cut = time.time() - FRESH_WINDOW
    rows = []
    live = cached = miss = 0
    for code in POOL:
        q = quotes.get(code)
        if not q or q.get("price") in (None, 0):
            miss += 1
            continue
        item = {"code": code, "name": q.get("name") or code,
                "price": float(q["price"]), "pct": float(q.get("pct") or 0),
                "amount": float(q.get("amount") or 0) / 1e8,
                "vwap": None, "dev": None, "rebound": None, "mom3": None,
                "bars": 0,
                "day_ret": float(q.get("pct") or 0) / 100,
                "state": "无分时", "stale": False}
        t = trends.get(code)
        if t:
            m = dashboard.metrics_from_ticks(t["ticks"])
            if m:
                item.update({"vwap": m["vwap"], "dev": m["dev"],
                             "rebound": m["rebound"], "mom3": m["mom3"],
                             "bars": m["bars"]})
                item["state"] = dashboard.judge(item, strategy.PARAMS)
            if t["ts"] >= fresh_cut:
                live += 1
            else:
                cached += 1
                item["stale"] = True
        else:
            miss += 1
        rows.append(item)

    rows.sort(key=lambda x: (x["dev"] if x["dev"] is not None else 99))
    src = {"live": live, "cache": cached, "miss": miss,
           "quote_ts": quote_ts, "errors": errors}
    return rows, src


def account_numbers(acc, rows):
    qmap = {r["code"]: r for r in rows}
    mv = 0.0
    for code, pos in acc["positions"].items():
        sh = broker.position_shares(pos)
        avg = broker.avg_cost(pos)
        px = (qmap.get(code) or {}).get("price") or avg
        mv += sh * px
    equity_val = acc["cash"] + mv
    init = acc["meta"]["init_capital"]
    total = equity_val - init
    return equity_val, mv, total, (total / init * 100 if init else 0.0)


def _money(v, sign=False):
    s = f"{v:+,.2f}" if sign else f"{v:,.2f}"
    return f"¥{s}"


def api_state():
    rows, src = snapshot()
    acc, trades, equity = dashboard.load_state()
    equity_val, mv, total, total_pct = account_numbers(acc, rows)
    day_pnl = float(equity[-1]["day_pnl"]) if equity else 0.0

    # 资金流方向：持仓且现价高于成本 → 流入中心（盈利），低于成本 → 流出
    pos_map = acc.get("positions") or {}
    for r in rows:
        pos = pos_map.get(r["code"])
        if not pos:
            r["dir"] = "none"
        else:
            r["dir"] = "in" if r["price"] >= broker.avg_cost(pos) else "out"

    n_live, n_cache = src["live"], src["cache"]
    if n_live == 0 and n_cache > 0:
        note = ' <em class="warn">· 全部读本地缓存（后台刷新未成功）</em>'
    elif n_cache > 0:
        note = f' <em class="warn">· {n_cache} 只缓存超 60 秒</em>'
    else:
        note = ""

    qts = src["quote_ts"] or time.time()
    fresh = (time.time() - qts) < 90

    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(qts)),
        "fresh": fresh,
        "note": note,
        "equity": _money(equity_val),
        "mv": _money(mv),
        "cash": _money(acc["cash"]),
        "n_trades": len([t for t in trades if t["side"] == "SELL"]),
        "n_pos": len(acc["positions"]),
        "total": _money(total, True),
        "total_pct": f"{total_pct:+.2f}%",
        "total_cls": dashboard.pnl_cls(total),
        "day": _money(day_pnl, True),
        "day_cls": dashboard.pnl_cls(day_pnl),
        "live": n_live, "cache": n_cache, "miss": src["miss"],
        "pool": [{
            "code": r["code"], "price": r["price"], "pct": r["pct"],
            "vwap": r["vwap"], "dev": r["dev"], "reb": r["rebound"],
            "mom": r["mom3"], "amt": r["amount"], "state": r["state"],
            "dir": r["dir"],
        } for r in rows],
    }


def html_page():
    rows, src = snapshot()
    acc, trades, equity = dashboard.load_state()
    return dashboard.render(acc, trades, equity, rows, src, live=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "TradingSim/1.0"

    def log_message(self, fmt, *args):   # 静默，别刷屏
        return

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        # 关键：允许 file:// 打开的静态页跨源读 /api/state
        # （file:// 的 Origin 是 null，不加这行浏览器会直接拦掉请求，
        #   页面就只能永远停在快照时间上）
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/ping":
                body = json.dumps({"ok": True, "pid": os.getpid(),
                                   "port": self.server.server_address[1]}).encode()
                self._send(body, "application/json; charset=utf-8")
            elif path == "/api/state":
                body = json.dumps(api_state(), ensure_ascii=False).encode("utf-8")
                self._send(body, "application/json; charset=utf-8")
            elif path in ("/", "/index.html"):
                self._send(html_page().encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:  # noqa: BLE001
            self._send(json.dumps({"error": str(e)}).encode("utf-8"),
                       "application/json; charset=utf-8")


def _probe(port, timeout=0.6):
    """探测某端口上是否已经有本程序在跑。"""
    try:
        import urllib.request
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/ping" % port, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
            return bool(d.get("ok"))
    except Exception:  # noqa: BLE001
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    # 单实例：已经有服务在跑，就别再起第二个
    # （两个实例会各自拉一遍行情，白白加倍请求，还可能在两个端口上给出不同画面）
    for p in range(args.port, args.port + 20):
        if _probe(p):
            url = "http://127.0.0.1:%d/" % p
            print("  实时服务已经在运行：%s" % url)
            print("  直接打开这个地址即可，不再重复启动。")
            if not args.no_browser:
                webbrowser.open(url)
            return 0

    bootstrap()
    threading.Thread(target=refresher, daemon=True).start()

    httpd = None
    port = args.port
    for p in range(args.port, args.port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print("  端口 %d-%d 全被占用，放弃。" % (args.port, args.port + 19))
        return 1

    url = f"http://127.0.0.1:{port}/"

    # 写 pid 文件：停止脚本靠它精确杀掉，不用按名字乱杀 python
    pid_path = os.path.join(dashboard.BASE, "state", "serve.pid")
    try:
        os.makedirs(os.path.dirname(pid_path), exist_ok=True)
        with open(pid_path, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except Exception:  # noqa: BLE001
        pid_path = None

    print("=" * 58)
    print("  实时看板已启动")
    print("  地址   %s" % url)
    print("  刷新   后台自动拉行情，页面每 4 秒同步一次")
    print("  关闭   按 Ctrl+C 或直接关掉这个窗口")
    print("=" * 58)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        STOP.set()
        httpd.server_close()
        if pid_path:
            try:
                os.remove(pid_path)
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
