# -*- coding: utf-8 -*-
"""批量拉取个股历史日线并缓存（个股因子研究的数据准备）。

为什么单独成文件
----------------
个股因子研究（factorstock.py）需要 200+ 只个股 10 年以上的日线。一次性
全量拉取很慢（腾讯源约 0.6s/请求，每只还要翻页补更早历史），所以把
「取数」和「研究」拆开：

  * 本脚本只负责把 universe 里每只票的日线拉到本地缓存，断点续拉；
  * factorstock.py 只读本地缓存，跑得快、可重复，不碰网络。

数据源：market.fetch_daily_bars_tx（腾讯，零鉴权，与 market.py 同源）。
复权：hfq（后复权）。理由：个股分红送转频繁，不复权会在除权日制造假
-10% 跳变，污染动量/反转因子；前复权是等差口径，长区间会把早年减成
负数。后复权序列整体等比缩放，区间内的收益率（比值）是干净的。

末根处理：腾讯 hfq 末根（当天）常未按复权因子更新，market.py 已注明有
假跳变。我们去掉最后 2 根，因子研究的打分点也不会落在「今天」这种
边界上，不影响结论。

用法
----
  python engine/bulkpull.py            # 拉取 universe 全部（断点续拉）
  python engine/bulkpull.py status     # 只报告缓存覆盖率，不联网
  python engine/bulkpull.py resect    # 重新从 universe 读取并校验缓存完整性
"""
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "engine"))
import market

UNIVERSE = os.path.join(BASE, "state", "universe_hs300.json")
CACHE_DIR = os.path.join(BASE, "state", "cache", "stocks")
YEARS = 11
TRIM_TAIL = 2          # 去掉 hfq 末根脏数据
MIN_BARS = 250         # 少于约 1 年的票视为拉取失败，不入研究池


def load_codes():
    with open(UNIVERSE, "r", encoding="utf-8") as f:
        u = json.load(f)
    return u["stocks"]            # [{"code","setcode"}, ...]


def cache_path(code):
    return os.path.join(CACHE_DIR, "%s.json" % code)


def pull_one(code, log=True, retries=6):
    """拉一只，缓存到本地。返回 (bars, note)。失败返回 (None, err)。

    腾讯 K 线源在持续高频请求后会临时限流（三个入口同时返回空），
    此时 market.py 会把三个 host 置 120s 冷却。脚本若立刻重试只会反复踩
    冷却。这里做指数退避：30/60/90/120/150/180s 后才放弃，给限流窗口
    留出恢复时间。
    """
    last_err = "未知失败"
    for attempt in range(retries):
        try:
            rows = market.fetch_daily_bars_tx(code, years=YEARS, adjust="hfq")
        except Exception as e:
            last_err = "fetch失败:%s" % e
            if attempt < retries - 1:
                time.sleep(30 * (attempt + 1))
                continue
            return None, last_err
        break
    else:
        return None, last_err
    if not rows:
        return None, "0根"
    if len(rows) > TRIM_TAIL:
        rows = rows[:-TRIM_TAIL]
    if len(rows) < MIN_BARS:
        return None, "仅%d根(<%d)" % (len(rows), MIN_BARS)
    out = [[r["date"], round(r["open"], 4), round(r["close"], 4),
            round(r["high"], 4), round(r["low"], 4)] for r in rows]
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = cache_path(code) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f)
    os.replace(tmp, cache_path(code))
    if log:
        print("  [OK] %s  %d 根  %s~%s" % (code, len(out),
                                          out[0][0], out[-1][0]))
    return out, "ok"


def cached_codes():
    if not os.path.isdir(CACHE_DIR):
        return set()
    return {fn[:-5] for fn in os.listdir(CACHE_DIR) if fn.endswith(".json")}


def cmd_pull():
    stocks = load_codes()
    done = cached_codes()
    todo = [s for s in stocks if s["code"] not in done]
    print("universe=%d  已缓存=%d  待拉=%d" % (len(stocks), len(done), len(todo)))
    ok = skip = fail = 0
    for i, s in enumerate(todo):
        c = s["code"]
        bars, note = pull_one(c, log=(i % 10 == 0 or note != "ok"))
        if bars is not None:
            ok += 1
        else:
            fail += 1
            print("  [FAIL] %s  %s" % (c, note))
        if (i + 1) % 25 == 0:
            print("  ...进度 %d/%d (本批成功%d 失败%d)" % (i + 1, len(todo), ok, fail))
        time.sleep(0.5)
    print("完成：成功%d 失败%d  缓存目录 %s" % (ok, fail, CACHE_DIR))


def cmd_status():
    stocks = load_codes()
    done = cached_codes()
    miss = [s["code"] for s in stocks if s["code"] not in done]
    print("universe=%d  已缓存=%d  缺失=%d" % (len(stocks), len(done), len(miss)))
    if miss:
        print("缺失样例:", miss[:20])


def cmd_resect():
    """校验已缓存文件是否完整（非空、可解析、≥MIN_BARS）。"""
    stocks = load_codes()
    done = cached_codes()
    bad = []
    for s in stocks:
        c = s["code"]
        p = cache_path(c)
        if c not in done:
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = json.load(f)
            if not isinstance(v, list) or len(v) < MIN_BARS:
                bad.append((c, "len=%s" % len(v) if isinstance(v, list) else "非列表"))
        except Exception as e:
            bad.append((c, "解析失败:%s" % e))
    if bad:
        print("发现 %d 个损坏缓存：" % len(bad))
        for c, why in bad:
            print("  %s  %s" % (c, why))
    else:
        print("缓存全部完整（%d 只，均 ≥%d 根）" % (len(done), MIN_BARS))


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "pull"
    if arg == "status":
        cmd_status()
    elif arg == "resect":
        cmd_resect()
    else:
        cmd_pull()
