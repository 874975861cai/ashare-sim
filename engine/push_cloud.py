# -*- coding: utf-8 -*-
"""把账本推到云端，让手机端在成交后几秒内看到。

为什么不用「重新发布」：发布是「上传整个目录 + 起服务」，只能人工点一次，
而且会把服务重启一遍。成交是随时发生的，等下一次发布才更新，手机上看到的
就是几小时前的仓位——这个错觉已经让人问过一次「为什么显示可入场却没买」。

所以云端开了个只写账本的口子（POST /api/sync），这里负责把三个文件推过去。
行情仍由云端自己实时拉，代码不动，服务不重启。

幂等：流水 + 现金/仓位/入金流 + 已结算净值行算成一个签名，
      没变化就一个请求都不发。
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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import broker      # noqa: E402

URL = (os.environ.get("SIM_CLOUD_URL")
       or "https://ashare-sim-dashboard.app.workbuddy.link").rstrip("/")
TOKEN = os.environ.get("SIM_SYNC_TOKEN") or "sim-sync-9f3a7c"
# ⚠️ 上面这个兜底 token 写在公开仓库里，等于一把人人可用的钥匙：谁都能拿它
# POST /api/sync 覆盖线上账本。保留它只是为了「secret 没配时不至于当场停服」，
# 但从现在起每次走到兜底都要留痕，否则连「线上账本被谁写的」都查不出来。
# 真正的修法是轮换 Worker 侧与 GitHub secret 里的同一把 token，那一步要人来定。
_ENV_TOKEN = os.environ.get("SIM_SYNC_TOKEN") or ""

SIG_FILE = os.path.join(broker.STATE, ".pushed")
PUB_FILE = os.path.join(broker.STATE, ".published")
LOG_FILE = os.path.join(broker.STATE, "push.log")


def settled_equity(text, day=None):
    """净值表里**已结算**的行，归一成 "日期=权益" 的列表。

    为什么只取「日期 < 今天」：
      · 今天那行每 5 分钟就被 broker.snapshot 重写一次（盘中 note/市值都在变），
        算进签名等于每轮都推一遍 —— 这正是 2026-11-09 刻意排除 today 的理由。
      · 但整张表都不算会漏掉另一种情况：**没有成交的日子，净值表每天照样新增
        一行收盘存档**，而现金/仓位/流水一个字没变 → 签名不变 → 推送跳过。
        mix 模式可以连续几周不成交，手机上的净值曲线就静静断在上一笔成交那天，
        而每一条检查都是绿的。
    已结算行的内容一旦落地就不再变，所以它最多让签名每天变一次（次日第一轮），
    代价是一天多推一次，换来曲线不会无声停更。
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    rows = []
    for line in (text or "").lstrip("\ufeff").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d = parts[0].strip()
        if not d or d >= day:            # ISO 日期，字典序即时间序
            continue
        rows.append("%s=%s" % (d, parts[1].strip()))
    return rows


def ledger_fingerprint(trades_bytes, acc, equity_text=None):
    """账本指纹：流水 + 「看板必须看到一致」的资金/仓位/入金流 + 已结算净值行。

    2026-11-09 修：旧版只对 trades.csv 取哈希。入金、迁移、分红这类
    **不动流水**却动了现金的变更，于是永远推不出去——签名没变就跳过，
    看板一直停在旧现金。当天线上显示 54.50、账本其实已是 90224.10
    （入金 90169.60），正是这么来的；再等不到成交就一直错下去。

    反过来 today / peak_equity / trade_seq 每轮都在变，算进去等于每 5
    分钟推一次，所以刻意排除：只认「钱、仓位、入金流」这三类。

    2026-11-10 补：净值表的**已结算行**（日期 < 今天）也要算，否则没成交的
    日子曲线会一直停更（见 settled_equity）。
    """
    h = hashlib.sha256()
    h.update(trades_bytes or b"")
    h.update(b"\0")
    acc = acc or {}
    meta = acc.get("meta") or {}
    try:
        init = round(float(meta.get("init_capital") or 0.0), 2)
        cash = round(float(acc.get("cash") or 0.0), 2)
    except (TypeError, ValueError):
        init = cash = 0.0
    flows = {}
    for k, v in (acc.get("flows") or {}).items():
        try:
            flows[str(k)] = round(float(v or 0.0), 2)
        except (TypeError, ValueError):
            flows[str(k)] = str(v)          # 坏值也要参与指纹，别静默丢掉
    pos = {}
    for code, p in (acc.get("positions") or {}).items():
        p = p or {}
        try:
            realized = round(float(p.get("realized") or 0.0), 2)
        except (TypeError, ValueError):
            realized = 0.0
        lots = []
        for lot in (p.get("lots") or []):
            lot = lot or {}
            try:
                lots.append([str(lot.get("date") or ""),
                             int(lot.get("shares") or 0),
                             round(float(lot.get("price") or 0.0), 4)])
            except (TypeError, ValueError):
                lots.append([str(lot.get("date") or ""), 0, 0.0])
        pos[str(code)] = {"realized": realized, "lots": lots}
    norm = {"cash": cash, "init": init, "flows": flows, "pos": pos}
    h.update(json.dumps(norm, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    h.update(b"\0eq")
    h.update("\n".join(settled_equity(equity_text)).encode("utf-8"))
    return h.hexdigest()[:16]


def _read_trades_bytes():
    """流水原始字节。返回 (字节, 错误)：字节为 None 表示真读不出来。

    文件不存在 = 一条成交都还没有，是**合法状态**（返回 b""、无错误）。
    旧版把它和「读不出来」合成同一句「账本读不出来」：全新账户（有现金、
    零成交）会永远推不出去，看板停在 0，而每轮日志只有一行看不出所以然的
    「账本读不出来」。
    """
    try:
        with open(broker.TRADE_FILE, "rb") as f:
            return f.read(), ""
    except FileNotFoundError:
        return b"", ""
    except OSError as e:
        return None, "流水读不出来（%s）" % e


def _sig(trades_bytes=b"", equity_text=None):
    """账本签名 + 读不出来的原因。返回 (签名, 错误)，签名空才算失败。"""
    try:
        acc = broker.load_account()
    except (OSError, ValueError) as e:
        # 读不出来就不要再拿 {} 去算签名：那个签名是「一个空账户」的指纹，
        # 和真账本毫无关系，写进 .pushed 会让下一轮误判成「已推过」。
        return "", "账本读不出来（%s）" % e
    return ledger_fingerprint(trades_bytes, acc, equity_text), ""


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
    if not _ENV_TOKEN:
        # 只在「真的用了硬编码兜底」时喊一声，避免每轮都刷
        _log("warn 使用硬编码兜底 token（SIM_SYNC_TOKEN 未设置）")
        if verbose:
            print("  [警告] SIM_SYNC_TOKEN 未设置，本轮用的是硬编码兜底 token")
    # 流水与净值表各只读一次：签名和 payload 必须是同一份字节，否则
    # 「签名说变了、推上去的还是旧的」这种谎话会在并发写盘时出现。
    try:
        eq_text = _read(broker.EQUITY_FILE)
    except OSError as e:
        # 缺净值表不能当空表推上去——那会用一张空曲线覆盖线上历史。
        return "fail", "净值表读不出来（%s）" % e
    trades_bytes, err = _read_trades_bytes()
    if trades_bytes is None:
        _log("fail %s" % err)
        return "fail", err
    sig, err = _sig(trades_bytes, eq_text)
    if not sig:
        _log("fail %s" % err)
        return "fail", err

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
            "trades": trades_bytes.decode("utf-8-sig"),   # 剥掉 BOM，云端会重新加
            "equity": eq_text,
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
        # 原子写：这个文件被截断/清零，下一轮会把「已推过」读成「没推过」，
        # 顶多多推一次；但反过来（内容写坏却看着有个签名）会让人误判线上是新的。
        broker._atomic_write_text(SIG_FILE, "%s %s" % (sig, fp))
    except OSError:
        pass

    _log("ok %s %s" % (sig, ",".join(res.get("written") or [])))
    if verbose:
        print("  已推送 %s（签名 %s）" % ("、".join(res.get("written") or []), sig))
    return "pushed", sig


if __name__ == "__main__":
    force = "--force" in sys.argv
    if "--check" in sys.argv:
        try:
            eq_text = _read(broker.EQUITY_FILE)
        except OSError as e:
            print("净值表读不出来：%s" % e)
            raise SystemExit(1)
        trades_bytes, terr = _read_trades_bytes()
        if trades_bytes is None:
            print(terr)
            raise SystemExit(1)
        local, serr = _sig(trades_bytes, eq_text)
        if not local:
            print(serr)
            raise SystemExit(1)
        saved = _saved()
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
