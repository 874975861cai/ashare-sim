# -*- coding: utf-8 -*-
"""数据预热：把核心池的 5 日分时一次性拉到本地缓存。

东方财富接口有分钟级限流，因此这里用「慢速串行 + 失败长退避」，
跑一次后回测与优化全部离线可用。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import market      # noqa: E402
import strategy    # noqa: E402

NDAYS = 5


def warm(codes=None, ndays=NDAYS):
    codes = codes or strategy.CORE_POOL
    ok, fail = [], []
    for i, c in enumerate(codes, 1):
        for attempt in range(3):
            try:
                tr = market.fetch_trends(c, ndays=ndays, ttl=0)
                n = len(tr["ticks"])
                days = sorted({t["t"][:10] for t in tr["ticks"]})
                print(f"[{i}/{len(codes)}] {c} {tr['name']}  {n} 点  日数 {len(days)}")
                ok.append(c)
                break
            except Exception as e:  # noqa: BLE001
                if attempt < 2:
                    wait = 25 * (attempt + 1)
                    print(f"[{i}/{len(codes)}] {c} 限流，等待 {wait}s 重试… ({e})")
                    time.sleep(wait)
                else:
                    print(f"[{i}/{len(codes)}] {c} 失败")
                    fail.append(c)
        time.sleep(6)
    print(f"\n完成：成功 {len(ok)} / 失败 {len(fail)} {fail}")
    return ok, fail


if __name__ == "__main__":
    warm()
