# -*- coding: utf-8 -*-
"""把账本推到云端，让手机端在成交后几秒内看到。

为什么不用「重新发布」：发布是「上传整个目录 + 起服务」，只能人工点一次，
而且会把服务重启一遍。成交是随时发生的，等下一次发布才更新，手机上看到的
就是几小时前的仓位——这个错觉已经让人问过一次「为什么显示可入场却没买」。

所以云端开了个只写账本的口子（POST /api/sync），这里负责把三个文件推过去。
行情仍由云端自己实时拉，代码不动，服务不重启。

幂等：用 trades.csv 的哈希当签名，没新成交就一个请求都不发。
失败不抛异常——交易本身不能被同步拖垮，下一轮会自动补推。

用法：
    python engine/push_cloud.py          有变化才推
    python engine/push_cloud.py --force  无条件推
    python engine/push_cloud.py --check  只看会不会推
"""
import hashlib
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker      # noqa: E402

URL = (os.environ.get("SIM_CLOUD_URL")
       or "https://ashare-sim-dashboard.app.workbuddy.host").rstrip("/")
TOKEN = os.environ.get("SIM_SYNC_TOKEN") or "sim-sync-9f3a7c"

SIG_FILE = os.path.join(broker.STATE, ".pushed")
PUB_FILE = os.path.join(broker.STATE, ".published")
LOG_FILE = os.path.join(broker.STATE, "push.log")


def _sig():
    """账本签名：只看成交流水。净值表每天都会动，拿它当签名等于每次都推。"""
    h = hashlib.sha256()
    try:
        with open(broker.TRADE_FILE, "rb") as f:
            h.update(f.read())
    except OSError:
        return ""
    return h.hexdigest()[:16]


def _pub_fp():
    """当前"已发布"的指纹，由 sync_cloud.py --mark 写入。"""
    try:
        with open(PUB_FILE, "r", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        return ""


def _saved():
    """上次推送时记下的 (账本签名, 发布指纹)。"""
    try:
        with open(SIG_FILE, "r", encoding="ascii") as f:
            parts = f.read().split()
    except OSError:
        return "", ""
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def _saved_sig():
    return _saved()[0]


def _read(path):
    with open(path, "r", encoding="utf-8-sig") as f:   # 剥掉 BOM，云端会重新加
        return f.read()


def _log(line):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
    except OSError:
        pass


def push(force=False, timeout=8, verbose=False):
    """返回 (状态, 说明)。状态是 pushed / skip / fail，任何情况都不抛异常。"""
    sig = _sig()
    if not sig:
        return "fail", "账本读不出来"

    saved_sig, saved_fp = _saved()
    fp = _pub_fp()

    # 只有「账本没变」**且**「没重新发布过」才算已同步。
    # 少了后半个条件会出大问题：重新发布会把线上 state 换回部署包里的版本，
    # 而本地 trades.csv 一个字没变——于是签名相同，push 认为"推过了"，
    # 线上就永远停在旧账本，本地看起来却一切正常。
    # 2026-10-20 下午就是这么出现「手机显示空仓 9989、本地显示持仓 10068」的。
    if not force and sig == saved_sig and fp == saved_fp:
        if verbose:
            print("  账本没变化，跳过推送（签名 %s）" % sig)
        return "skip", "没变化"

    try:
        payload = {
            "account": broker.load_account(),
            "trades": _read(broker.TRADE_FILE),
            "equity": _read(broker.EQUITY_FILE),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (OSError, ValueError) as e:
        _log("fail 组装失败 %s: %s" % (type(e).__name__, e))
        return "fail", "组装失败：%s" % e

    req = urllib.request.Request(
        URL + "/api/sync", data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "X-Sim-Token": TOKEN})

    # 本机有出口代理，直连才通；带上代理反而被拦成 501
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            res = json.loads(r.read().decode("utf-8"))
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "云端拒绝")
    except Exception as e:                      # noqa: BLE001
        msg = "%s: %s" % (type(e).__name__, e)
        _log("fail %s" % msg)
        if verbose:
            print("  推送失败：%s" % msg)
        return "fail", msg

    try:
        with open(SIG_FILE, "w", encoding="ascii") as f:
            f.write("%s %s" % (sig, fp))
    except OSError:
        pass

    _log("ok %s %s" % (sig, ",".join(res.get("written") or [])))
    if verbose:
        print("  已推送 %s（签名 %s）" % ("、".join(res.get("written") or []), sig))
    return "pushed", sig


if __name__ == "__main__":
    force = "--force" in sys.argv
    if "--check" in sys.argv:
        local, saved = _sig(), _saved()
        fp, saved_fp = _pub_fp(), saved[1]
        print("本地账本签名 %s" % local)
        print("已推账本签名 %s" % saved[0])
        print("当前发布指纹 %s" % (fp or "(未发布)"))
        print("已推发布指纹 %s" % (saved_fp or "(未记录)"))
        need = (local != saved[0]) or (fp != saved_fp)
        print("结论 %s" % ("需要推送" if need else "已是最新"))
        raise SystemExit(0)
    st, msg = push(force=force, verbose=True)
    print("结果 %s  %s" % (st, msg))
