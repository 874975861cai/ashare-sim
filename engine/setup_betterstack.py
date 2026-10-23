# -*- coding: utf-8 -*-
"""用 Better Stack 的免费 uptime monitor 当云端派工扳机。

为什么需要它
------------
GitHub 自带的 schedule 对新仓库不可靠：2026-10-21 实测 6 个计划点
（03:35/03:40/03:45/03:50/03:55/04:00 UTC）零触发，另用纯 ASCII 名 +
`*/5 * * * *` 的独立探针验证同样为零。社区证据：新仓库首次触发要约 5 小时，
之后投递率是「每 4 小时一次」。所以必须有一台不依赖用户电脑的机器去按扳机。

为什么是 Better Stack
--------------------
  Cloudflare   —— dash.cloudflare.com 在国内打不开，用户进不去控制台
  HetrixTools  —— HTTP 检查只支持 GET/HEAD，不能自定义请求头、不能发请求体
  CronAlert    —— 支持 POST 和自定义 header，但官方明说「不发请求体」
而 GitHub 派工接口的硬要求是 POST + Authorization + {"ref":"main"}，
实测空 body 与 {} 都返回 422（nil is not an object / "ref" wasn't supplied）。
Better Stack 官方文档明确支持请求方法、请求体、请求头三样。

做法
----
建一个 HTTP 监控，URL 直接指向 GitHub 的 workflow dispatch 接口，
让 Better Stack 每隔几分钟替我们 POST 一次。每次成功返回 204，
失败（401/422/5xx）就会被当成 down 并邮件告警 —— 顺带还成了派工链路的
健康检查，这比单纯的定时器多一层保障。

用法
----
    python engine/setup_betterstack.py --create
    python engine/setup_betterstack.py --list
    python engine/setup_betterstack.py --pause <id>
    python engine/setup_betterstack.py --resume <id>
    python engine/setup_betterstack.py --delete <id>

token 来源：环境变量 BETTERSTACK_API_TOKEN，或文件
E:\\TradingSim\\.betterstack_token（已在 .gitignore 里，不会进版本库）。
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(ROOT, ".betterstack_token")
BASE = "https://uptime.betterstack.com/api/v2"

REPO = "874975861cai/ashare-sim"
WORKFLOW = "trade.yml"
DISPATCH_URL = ("https://api.github.com/repos/%s/actions/workflows/%s/dispatches"
                % (REPO, WORKFLOW))
MONITOR_NAME = "A股模拟盘·云端派工"

# 5 分钟一次。这套引擎是「跑一次补齐全天」：从当日开盘用当日分时重演到最后
# 一根，时间判断全取 tick 时间戳，所以晚几分钟执行不影响成交价与成交时刻。
# 频率只决定账本更新快慢，不决定成交结果。5 分钟 = 288 次/天，够用且温和。
CHECK_FREQUENCY = 300


def token():
    t = os.environ.get("BETTERSTACK_API_TOKEN") or ""
    if not t:
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                t = f.read().strip()
        except OSError:
            pass
    if not t:
        raise SystemExit(
            "没有 Better Stack token。\n"
            "把 token 存到 %s（一行，前后不要有空行），\n"
            "或设环境变量 BETTERSTACK_API_TOKEN。" % TOKEN_FILE)
    return t


def api(path, method="GET", body=None, timeout=30):
    """直连 api。本机有出口代理，带上反而被拦成 501。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": "Bearer %s" % token(),
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:400]}


def gh_token():
    p = os.path.join(ROOT, ".gh_token.txt")
    if not os.path.exists(p):
        p = os.path.join(ROOT, ".gh_token")
    with open(p, "r", encoding="utf-8") as f:
        return f.read().strip()


def build_payload(freq=CHECK_FREQUENCY):
    return {
        "monitor_type": "status",
        "url": DISPATCH_URL,
        "pronounceable_name": MONITOR_NAME,
        "check_frequency": freq,
        "request_timeout": 20,
        "confirmation_period": 0,
        # 只要一个区域：多区域会让同一个计划点被触发多次，
        # 白白放大 Actions 运行次数。
        "regions": ["us"],
        "request_headers": [
            {"name": "Authorization", "value": "Bearer %s" % gh_token()},
            {"name": "Accept", "value": "application/vnd.github+json"},
            {"name": "Content-Type", "value": "application/json"},
            {"name": "User-Agent", "value": "ashare-sim-ticker"},
        ],
        "request_body": json.dumps({"ref": "main"}),
        # 派工成功是 204（无响应体）。不写死会被当成"没内容"而误判。
        "expected_status_codes": [204],
        "verify_ssl": True,
        "email": True,
    }


def list_monitors():
    st, d = api("/monitors")
    if st != 200:
        print("取监控列表失败 %s %s" % (st, d))
        return []
    return d.get("data") or []


def find_by_name(name):
    for m in list_monitors():
        a = m.get("attributes") or {}
        if a.get("pronounceable_name") == name:
            return m
    return None


def create(freq=CHECK_FREQUENCY):
    exist = find_by_name(MONITOR_NAME)
    if exist:
        mid = exist["id"]
        print("已存在同名监控 id=%s，改为更新频率 %ds" % (mid, freq))
        st, d = api("/monitors/%s" % mid, "PATCH",
                    {"check_frequency": freq})
        print("  更新结果 %s %s" % (st, "OK" if st == 200 else d))
        return mid

    payload = build_payload(freq)
    # 字段名在不同版本里可能是 http_method / request_method，逐个试。
    last = None
    for key in ("http_method", "request_method"):
        body = dict(payload)
        body[key] = "POST"
        st, d = api("/monitors", "POST", body)
        if st in (200, 201):
            mid = d["data"]["id"]
            print("已创建监控 id=%s  每 %d 秒检查一次" % (mid, freq))
            print("  目标 %s" % DISPATCH_URL)
            print("  期望状态 204，方法 POST（%s）" % key)
            return mid
        last = (st, d)
        print("  用 %s 创建失败 %s：%s" % (key, st, str(d)[:300]))
    print("创建失败，最后一次错误：%s %s" % last)
    return None


def show(item):
    a = item.get("attributes") or {}
    print("  id=%-8s %-22s status=%-10s freq=%-5ss url=%s"
          % (item.get("id"), a.get("pronounceable_name", "")[:22],
             a.get("status", "?"), a.get("check_frequency"), a.get("url")))


def main(argv):
    if "--list" in argv:
        ms = list_monitors()
        print("共 %d 个监控：" % len(ms))
        for m in ms:
            show(m)
        return 0

    if "--create" in argv:
        mid = create()
        return 0 if mid else 1

    for flag, method in (("--pause", "POST"), ("--resume", "POST")):
        if flag in argv:
            mid = argv[argv.index(flag) + 1]
            action = "pause" if flag == "--pause" else "resume"
            st, d = api("/monitors/%s/%s" % (mid, action), method)
            print("%s %s → %s" % (action, mid, st))
            return 0 if st in (200, 204) else 1

    if "--delete" in argv:
        mid = argv[argv.index("--delete") + 1]
        st, d = api("/monitors/%s" % mid, "DELETE")
        print("删除 %s → %s" % (mid, st))
        return 0 if st in (200, 204) else 1

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
