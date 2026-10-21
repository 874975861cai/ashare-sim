# -*- coding: utf-8 -*-
"""行情数据层：通过东方财富公开接口获取 ETF / 股票 / 指数数据。

所有函数返回纯 Python 结构，不依赖第三方库（urllib + json 即可）。
"""
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
}

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

_LAST_TS = {}          # 每个数据源独立的节流时间戳
MIN_GAP_EM = 2.2       # 东方财富：限流严格，慢点走
MIN_GAP_TX = 0.6       # 腾讯：宽松得多，可以快

# ---- 时间预算 ----------------------------------------------------------
# 交互式场景（双击看板）不能容忍几分钟的等待。设置预算后，任何会超出
# 预算的节流等待或重试都立即抛 BudgetError，让调用方降级到缓存。
_DEADLINE = [None]


class BudgetError(RuntimeError):
    """请求会超出时间预算，已主动放弃。"""


def set_budget(seconds):
    """设置本次流程的联网时间预算（秒）。传 None 取消限制。"""
    _DEADLINE[0] = None if seconds is None else time.monotonic() + float(seconds)


def budget_left():
    d = _DEADLINE[0]
    if d is None:
        return None
    return d - time.monotonic()


def _out_of_budget(need=0.0):
    left = budget_left()
    return left is not None and left <= need


def _throttle(key="em"):
    """按数据源分别节流。东财限流严格，腾讯宽松，没必要一起用 2.2 秒。"""
    gap = MIN_GAP_TX if key == "tx" else MIN_GAP_EM
    last = _LAST_TS.get(key, 0.0)
    dt = time.time() - last
    wait = gap - dt
    if wait > 0:
        # 等到天亮也不够预算，就别等了
        if _out_of_budget(wait):
            raise BudgetError("节流等待 %.1fs 超出时间预算" % wait)
        time.sleep(wait)
    _LAST_TS[key] = time.time()


def _get(url, retries=5, timeout=20):
    last = None
    for i in range(retries):
        if _out_of_budget(0):
            raise BudgetError("重试前已无预算: %s" % url[:60])
        try:
            _throttle()
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except BudgetError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                back = 2.0 * (i + 1)
                if _out_of_budget(back):
                    raise BudgetError("退避 %.1fs 超出预算" % back) from e
                time.sleep(back)
    raise RuntimeError(f"请求失败: {url[:80]} -> {last}")


# ---------------------------- 本地缓存 ----------------------------
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "state", "cache")


def _cpath(name):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _write(path, obj):
    """原子写缓存。

    原来直接 open(path, "w") 覆写：serve.py 每约 8 秒回写一次，runner /
    dashboard / doctor 同时在读同一批文件，读端有概率命中写了一半的中间态。
    _read() 的 except 会把它变成 None，后果是**静默降级到更旧的缓存**——
    不崩溃、不算错账，但那一轮拿到的价格不是最新的，而且没有任何痕迹。

    改成「写临时文件 + os.replace 原子替换」：读端要么看到旧的完整文件，
    要么看到新的完整文件，不存在中间态。临时文件名带 pid，避免并发写冲突。
    """
    tmp = None
    try:
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(tmp, path)
        tmp = None
    except Exception:  # noqa: BLE001
        pass
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _stale(pattern_prefix):
    """按前缀取最近的缓存文件（限流时的降级数据）。

    跳过 0 根的缓存：那是历史上把「接口返回空」当成功写下的脏文件，
    拿它当降级数据等于把空结果一路传到下游，还不如直接返回 None。
    """
    try:
        files = [f for f in os.listdir(CACHE_DIR) if f.startswith(pattern_prefix)]
        if not files:
            return None
        files.sort(reverse=True)
        for name in files:
            blob = _read(os.path.join(CACHE_DIR, name))
            if not blob:
                continue
            tk = (blob.get("data") or {}).get("ticks")
            if tk:            # 有内容的才算数
                return blob
        return None
    except Exception:  # noqa: BLE001
        return None


def load_cached_trends(code, ndays=1):
    """只读缓存，绝不联网。返回 (data, 缓存时间戳) 或 (None, 0)。

    看板在预算耗尽时用它拼出「离线版本」，保证双击一定有东西可看。
    """
    code = str(code)
    today = time.strftime("%Y%m%d")
    hit = _read(os.path.join(CACHE_DIR, f"trends_{code}_{ndays}_{today}.json"))
    if not hit:
        hit = _stale(f"trends_{code}_{ndays}_")
    if not hit:
        return None, 0
    return hit.get("data"), hit.get("_ts", 0)


def load_cached_quotes():
    """只读取最近一次批量报价缓存，绝不联网。返回 (quotes, 缓存时间戳)。"""
    try:
        files = [f for f in os.listdir(CACHE_DIR) if f.startswith("q_")]
    except Exception:  # noqa: BLE001
        return {}, 0
    if not files:
        return {}, 0
    files.sort(reverse=True)
    path = os.path.join(CACHE_DIR, files[0])
    obj = _read(path) or {}
    if not isinstance(obj, dict) or "_ts" in obj:
        # 兼容带 _ts 包装的旧格式
        obj = obj.get("data") if isinstance(obj, dict) else {}
        obj = obj or {}
    ts = 0.0
    try:
        ts = os.path.getmtime(path)
    except Exception:  # noqa: BLE001
        pass
    return obj, ts


def secid(code):
    """代码 -> 东方财富 secid。沪市前缀 1，深市前缀 0。"""
    code = str(code)
    if code.startswith(("15", "16", "12", "00", "30", "20")):   # 深市 ETF/股票/转债
        return "0." + code
    return "1." + code


# --------------------------------------------------------------------------
# 标的池
# --------------------------------------------------------------------------
def _clist(fs, pages=1, pz=200, fid="f6"):
    """分页拉取板块成分行情。fid=f6 按成交额排序。

    接口分页不稳定（pn>1 易被限流），单页失败即停止，已取到的数据仍然可用。
    """
    out = []
    for pn in range(1, pages + 1):
        url = ("https://push2.eastmoney.com/api/qt/clist/get?"
               f"pn={pn}&pz={pz}&po=1&np=1&fltt=2&invt=2&fid={fid}&fs={fs}"
               "&fields=f2,f3,f4,f5,f6,f8,f10,f12,f14,f15,f16,f17,f18")
        try:
            d = _get(url, retries=3)
        except Exception:
            break
        data = (d or {}).get("data") or {}
        diff = data.get("diff") or []
        if not diff:
            break
        out.extend(diff)
        time.sleep(1.8)
    return out


def _norm(x):
    """东财空值返回 '-', 统一转 None。"""
    if x in ("-", "", None):
        return None
    return x


_ETFCACHE = {"ts": 0.0, "data": None}
ETF_TTL = 240       # 秒


def fetch_etf_universe(max_pages=14, use_cache=True):
    """全部 ETF（约 1300+ 只），按成交额降序。"""
    if use_cache and _ETFCACHE["data"] and time.time() - _ETFCACHE["ts"] < ETF_TTL:
        return _ETFCACHE["data"]
    rows = _clist("b:MK0021", pages=max_pages)
    res = []
    for r in rows:
        price = _norm(r.get("f2"))
        if price is None:
            continue
        res.append({
            "code": str(r.get("f12")),
            "name": r.get("f14"),
            "price": float(price),
            "pct": float(_norm(r.get("f3")) or 0),
            "vol": float(_norm(r.get("f5")) or 0),        # 手
            "amount": float(_norm(r.get("f6")) or 0),     # 元
            "high": _norm(r.get("f15")),
            "low": _norm(r.get("f16")),
            "open": _norm(r.get("f17")),
            "preclose": _norm(r.get("f18")),
            "turnover": _norm(r.get("f8")),
            "volratio": _norm(r.get("f10")),
        })
    _ETFCACHE["data"] = res
    _ETFCACHE["ts"] = time.time()
    return res


def fetch_stock_universe(max_pages=2):
    """沪深 A 股，按成交额降序（仅用于观察，策略不用）。"""
    rows = _clist("m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23", pages=max_pages)
    res = []
    for r in rows:
        if _norm(r.get("f2")) is None:
            continue
        res.append({
            "code": str(r.get("f12")), "name": r.get("f14"),
            "price": float(r.get("f2")), "pct": float(_norm(r.get("f3")) or 0),
            "amount": float(_norm(r.get("f6")) or 0),
        })
    return res


def fetch_index(code="000001", sid=None):
    """指数快照。指数必须显式给 secid（如 1.000001 上证 / 0.399001 深成）。"""
    d = _get("https://push2.eastmoney.com/api/qt/stock/get?"
             f"secid={sid or ('1.' + code)}&fields=f43,f57,f58,f60,f169,f170")
    x = (d or {}).get("data") or {}
    if not x:
        return None
    return {
        "code": x.get("f57"), "name": x.get("f58"),
        "price": (x.get("f43") or 0) / 100.0,
        "preclose": (x.get("f60") or 0) / 100.0,
        "pct": (x.get("f170") or 0) / 100.0,
    }


# --------------------------------------------------------------------------
# 备用源：腾讯
#
# 东财接口有分钟级限流，单请求还有条数上限。腾讯这两个接口宽松得多，
# 而且分时格式（HHMM 价格 累计量 累计额）与东财同构，上层逻辑可直接复用。
# 当日分时（ndays=1）优先走腾讯；历史多日仍只能靠东财 trends2。
# --------------------------------------------------------------------------
TX_MINUTE = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code="
TX_QUOTE = "http://qt.gtimg.cn/q="

# True = 当日分时优先腾讯；False = 仍以东财为主
PREFER_TX_INTRADAY = True


def tx_symbol(code):
    """代码 -> 腾讯符号（sh510300 / sz159915）。"""
    code = str(code)
    if code.startswith("11") or code[0] in "56":
        return "sh" + code
    return "sz" + code


def _get_text(url, retries=2, timeout=12):
    """取纯文本（GBK）。腾讯报价接口返回的就是这种，不是 JSON。"""
    last = None
    for i in range(retries):
        if _out_of_budget(0):
            raise BudgetError("已无预算: %s" % url[:50])
        try:
            _throttle("tx")
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read().decode("gbk", "ignore")
        except BudgetError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(0.8)
    raise RuntimeError(f"请求失败: {url[:60]} -> {last}")


def fetch_trends_tx(code):
    """腾讯当日分时，返回与 fetch_trends 同构的结构。仅支持当日。"""
    sym = tx_symbol(code)
    d = json.loads(_get_text(TX_MINUTE + sym))
    node = ((d or {}).get("data") or {}).get(sym) or {}
    inner = node.get("data") or {}
    arr = inner.get("data") or []
    if not arr:
        return None

    ds = str(inner.get("date") or time.strftime("%Y%m%d"))
    if len(ds) == 8:
        day = "%s-%s-%s" % (ds[:4], ds[4:6], ds[6:8])
    else:
        day = time.strftime("%Y-%m-%d")

    qt = (node.get("qt") or {}).get(sym) or []
    name = qt[1] if len(qt) > 1 else None
    try:
        preclose = float(qt[4]) if len(qt) > 4 and qt[4] else None
    except (TypeError, ValueError):
        preclose = None

    ticks = []
    prev = 0.0
    for line in arr:
        p = line.split()
        if len(p) < 4:
            continue
        hm = p[0]
        if len(hm) < 4:
            continue
        try:
            price = float(p[1])
            cvol = float(p[2])
            camt = float(p[3])
        except (TypeError, ValueError):
            continue
        # 累计额 / (累计量*100股) = 当日 VWAP
        avg = camt / (cvol * 100) if cvol else price
        ticks.append({
            "t": "%s %s:%s" % (day, hm[:2], hm[2:]),
            "price": price,
            "vol": max(0.0, cvol - prev),
            "avg": round(avg, 4),
        })
        prev = cvol

    if not ticks:
        return None
    return {"code": str(code), "name": name, "preclose": preclose,
            "ticks": ticks, "source": "tx"}


def fetch_quotes_tx(codes):
    """腾讯批量报价，返回与 fetch_quotes 同构的 dict。"""
    if not codes:
        return {}
    txt = _get_text(TX_QUOTE + ",".join(tx_symbol(c) for c in codes))
    out = {}
    for line in txt.splitlines():
        line = line.strip()
        if not line.startswith("v_") or "=" not in line:
            continue
        body = line.split("=", 1)[1].strip().rstrip(";").strip('"')
        f = body.split("~")
        if len(f) < 36:
            continue
        code = f[2]
        try:
            price = float(f[3])
            preclose = float(f[4])
        except (TypeError, ValueError):
            continue
        try:
            openp = float(f[5]) if f[5] else preclose
            high = float(f[33]) if f[33] else price
            low = float(f[34]) if f[34] else price
        except (TypeError, ValueError):
            openp, high, low = preclose, price, price
        # f[35] = "价格/成交量(手)/成交额(元)"
        amount = 0.0
        seg = f[35].split("/") if f[35] else []
        if len(seg) >= 3:
            try:
                amount = float(seg[2])
            except (TypeError, ValueError):
                amount = 0.0
        out[code] = {
            "code": code, "name": f[1],
            "price": price,
            "pct": round((price / preclose - 1) * 100, 2) if preclose else 0.0,
            "amount": amount,
            "high": high, "low": low, "open": openp, "preclose": preclose,
        }
    return out


# --------------------------------------------------------------------------
# 分时
# --------------------------------------------------------------------------
def fetch_trends(code, ndays=1, ttl=600):
    """分钟级分时: [{t, price, vol, avg}]  avg = 当日累计均价(VWAP)。

    当天缓存 ttl 秒；限流时降级使用最近一次缓存。
    """
    code = str(code)
    today = time.strftime("%Y%m%d")
    path = _cpath(f"trends_{code}_{ndays}_{today}.json")
    cached = _read(path)
    if cached and time.time() - cached.get("_ts", 0) < ttl:
        return cached["data"]

    # 当日分时优先走腾讯：更快，且不像东财那样动不动限流
    if ndays == 1 and PREFER_TX_INTRADAY:
        try:
            tx = fetch_trends_tx(code)
            if tx and tx.get("ticks"):
                _write(path, {"_ts": time.time(), "data": tx})
                return tx
        except Exception:  # noqa: BLE001
            pass      # 腾讯不可用，落回东财

    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
           f"secid={secid(code)}&fields1=f1,f2,f3,f4,f5,f6,f7,f8"
           "&fields2=f51,f53,f56,f58&iscr=0&ndays=%d" % ndays)
    try:
        d = _get(url)
    except Exception:
        # 东财也挂了，最后一搏试腾讯（只在非优先模式下需要）
        if ndays == 1 and not PREFER_TX_INTRADAY:
            try:
                tx = fetch_trends_tx(code)
                if tx and tx.get("ticks"):
                    _write(path, {"_ts": time.time(), "data": tx})
                    return tx
            except Exception:  # noqa: BLE001
                pass
        if cached:
            return cached["data"]
        old = _stale(f"trends_{code}_{ndays}_")
        if old:
            return old["data"]
        raise
    data = (d or {}).get("data") or {}
    ticks = []
    for line in (data.get("trends") or []):
        p = line.split(",")
        if len(p) < 4:
            continue
        try:
            ticks.append({
                "t": p[0],                      # "2026-10-19 09:30"
                "price": float(p[1]),
                "vol": float(p[2]),             # 该分钟成交量(手)
                "avg": float(p[3]),             # 当日累计均价
            })
        except ValueError:
            continue
    out = {
        "code": data.get("code") or code,
        "name": data.get("name"),
        "preclose": data.get("preClose"),
        "ticks": ticks,
    }
    # 0 根不能当成功返回，更不能写进缓存。
    # 2026-10-20 自审实测：东财对 ndays=6 的请求会返回 200 + 空 trends，
    # 原逻辑把 {"ticks": []} 当成正常结果写盘并返回，于是调用方看到的是
    # 「拉取成功、0 点 / 0 天」，既掩盖了接口故障，又污染了接下来 ttl 秒内
    # 的所有读取（缓存里躺着的是空数据）。空 = 这一路失败，应该降级或抛错。
    if not ticks:
        if cached:
            return cached["data"]
        old = _stale("trends_%s_%d_" % (code, ndays))
        if old:
            return old["data"]
        raise RuntimeError("%s 分时返回 0 根（接口无数据）" % code)
    _write(path, {"_ts": time.time(), "data": out})
    return out


def fetch_quotes(codes):
    """批量实时报价，分钟级缓存 + 多源降级（腾讯 → 东财 → 本地快照）。"""
    if not codes:
        return {}
    codes = [str(c) for c in codes]
    sig = hashlib.md5("|".join(sorted(codes)).encode()).hexdigest()[:8]
    stamp = time.strftime("%Y%m%d_%H%M")
    path = _cpath(f"q_{sig}_{stamp}.json")
    cached = _read(path)
    if cached:
        return cached

    out = {}

    # 1) 首选腾讯：一次请求拿全部，响应快且不像东财那样限流
    if PREFER_TX_INTRADAY:
        try:
            out = fetch_quotes_tx(codes)
        except Exception:  # noqa: BLE001
            out = {}

    # 2) 东财兜底
    if not out:
        ids = ",".join(secid(c) for c in codes)
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
               f"&secids={ids}&fields=f2,f3,f5,f6,f12,f14,f15,f16,f17,f18")
        try:
            d = _get(url)
            got = {}
            for r in (((d or {}).get("data") or {}).get("diff") or []):
                if _norm(r.get("f2")) is None:
                    continue
                got[str(r.get("f12"))] = {
                    "code": str(r.get("f12")), "name": r.get("f14"),
                    "price": float(r.get("f2")),
                    "pct": float(_norm(r.get("f3")) or 0),
                    "amount": float(_norm(r.get("f6")) or 0),
                    "high": float(_norm(r.get("f15")) or 0),
                    "low": float(_norm(r.get("f16")) or 0),
                    "open": float(_norm(r.get("f17")) or 0),
                    "preclose": float(_norm(r.get("f18")) or 0),
                }
            out = got
        except Exception:  # noqa: BLE001
            out = {}

    if out:
        _write(path, out)
        return out

    # 3) 都失败，退回本地快照
    old = _stale(f"q_{sig}_")
    if old:
        print(f"  [降级] 报价使用缓存快照（{len(old)} 只）")
        return {k: old[k] for k in codes if k in old}
    raise RuntimeError("报价失败：腾讯与东财均不可用，且无本地快照")


if __name__ == "__main__":
    idx = fetch_index()
    print("指数:", idx)
    uni = fetch_etf_universe(1)
    print("ETF 条数:", len(uni))
    for u in uni[:5]:
        print(u)
    tr = fetch_trends("510300")
    print("分时点数:", len(tr["ticks"]), tr["ticks"][:2], tr["ticks"][-1] if tr["ticks"] else None)
