# -*- coding: utf-8 -*-
"""把本地仓库推上 GitHub，并当场触发一次云端验证。

为什么要有这个脚本：GitHub 的机器在美国，能不能拉到东财/腾讯的行情，
只有真跑一次才知道。与其把定时任务挂好等明天开盘，不如现在 workflow_dispatch
跑一遍，十分钟内就有答案。这个不通，整套方案就得换——花十分钟比等一天划算。

token 从 `.gh_token` 文件读（已在 .gitignore 里），也可以走环境变量 GH_TOKEN。
需要 **classic** token，勾 `repo` 和 `workflow` 两个 scope：
  * repo     —— 建仓库、推代码
  * workflow —— 推 .github/workflows/ 下的文件（少了这个会被拒）

用法：
    python engine/setup_github.py                 建库 + 推代码 + 触发验证
    python engine/setup_github.py --push-only      只推代码
    python engine/setup_github.py --status          看最近几次云端运行结果
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN_FILE = os.path.join(BASE, ".gh_token")
API = "https://api.github.com"
REPO_NAME = os.environ.get("SIM_REPO_NAME") or "ashare-sim"
WORKFLOW = "trade.yml"


def die(msg, code=1):
    print("  × %s" % msg)
    raise SystemExit(code)


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not t:
        # 用户可能存成 .gh_token / .gh_token.txt，两种都认
        for name in (".gh_token", ".gh_token.txt", "gh_token.txt"):
            try:
                with open(os.path.join(BASE, name), "r", encoding="utf-8-sig") as f:
                    t = f.read().strip()
                if t:
                    break
            except OSError:
                continue
    if not t:
        die("没有 token。把 token 存到 %s，或设环境变量 GH_TOKEN。" % TOKEN_FILE)
    return t


def api(path, method="GET", body=None, tok=None):
    url = path if path.startswith("http") else API + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer %s" % (tok or token()),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
        # GitHub 要求带 UA，否则直接 403
        "User-Agent": "ashare-sim-setup",
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=25) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"message": raw[:300]}


def git(*args, **kw):
    return subprocess.run(("git",) + args, cwd=BASE, capture_output=True,
                          text=True, **kw)


def ensure_repo(tok, login):
    st, d = api("/repos/%s/%s" % (login, REPO_NAME), tok=tok)
    if st == 200:
        print("  仓库已存在: %s" % d["html_url"])
        if d.get("private"):
            print("  ! 这是私有仓库。66 次/天会吃掉 private 的 2000 分钟免费额度，"
                  "建议改成 Public（Settings → 最下面 Danger Zone → Change visibility）")
        return d["html_url"]

    print("  创建仓库 %s/%s ..." % (login, REPO_NAME))
    st, d = api("/user/repos", "POST", {
        "name": REPO_NAME,
        "description": "A股模拟盘 · 云端定时执行（策略/账本/审计流水）",
        "private": False,
        "has_issues": False,
        "has_wiki": False,
        "auto_init": False,
    }, tok=tok)
    if st not in (200, 201):
        die("建仓库失败 %s: %s" % (st, d.get("message")))
    return d["html_url"]


def push(tok, login):
    clean = "https://github.com/%s/%s.git" % (login, REPO_NAME)
    if git("remote", "get-url", "origin").returncode != 0:
        git("remote", "add", "origin", clean)
    else:
        git("remote", "set-url", "origin", clean)

    # token 只出现在这一条命令里，不落进 .git/config
    auth = "https://x-access-token:%s@github.com/%s/%s.git" % (tok, login, REPO_NAME)
    r = git("push", auth, "main")
    if r.returncode != 0:
        print(r.stdout.strip())
        print(r.stderr.strip())
        die("推送失败")
    print("  已推送 → %s" % clean)
    return clean


def trigger(tok, login):
    print("  触发一次云端执行（--force，跳过时段判断）...")
    st, d = api("/repos/%s/%s/actions/workflows/%s/dispatches" % (login, REPO_NAME, WORKFLOW),
                "POST", {"ref": "main", "inputs": {"force": True}}, tok=tok)
    if st not in (200, 204):
        die("触发失败 %s: %s" % (st, d.get("message")))
    print("  已排队。等它跑起来再看结果。")


def status(tok, login, watch=False):
    st, d = api("/repos/%s/%s/actions/runs?per_page=5" % (login, REPO_NAME), tok=tok)
    if st != 200:
        die("读运行记录失败 %s: %s" % (st, d.get("message")))
    runs = d.get("workflow_runs") or []
    if not runs:
        print("  还没有任何运行记录。")
        return None
    for r in runs:
        print("  %s  %-10s %-18s %s"
              % (r["created_at"], r["status"], r["conclusion"] or "-",
                 r["html_url"]))
    return runs[0]


def main(argv):
    tok = token()
    st, me = api("/user", tok=tok)
    if st != 200:
        die("token 无效或权限不足（%s: %s）" % (st, me.get("message")))
    login = me["login"]
    print("  身份: %s" % login)

    if "--status" in argv:
        status(tok, login)
        return 0

    html = ensure_repo(tok, login)
    push(tok, login)
    if "--push-only" not in argv:
        trigger(tok, login)
        print()
        print("  查看运行: %s/actions" % html)
        time.sleep(8)
        status(tok, login)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(json.dumps({"ok": False, "error": "%s" % e}, ensure_ascii=False))
        raise SystemExit(1)
