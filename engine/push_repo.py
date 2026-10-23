# -*- coding: utf-8 -*-
"""用 GitHub Git Data API 推代码，绕开 `git push` 走不通的网络。

背景：本机到 github.com:443 直连超时、走系统代理返回 502 CONNECT failed，
但 api.github.com 正常。所以代码提交改走 REST：
    建 blob -> 建 tree -> 建 commit -> 移动 ref
一次调用完成多文件提交，不会把一个改动拆成七八个 commit。

提交内容取的是 **git 里已存的 blob**（`git show HEAD:<path>`），
不是工作区字节。这样绕过 core.autocrlf 的 CRLF/LF 差异——
之前就是因为本地 CRLF、云端 CRLF 不一致，导致同一份流水哈希永远不等。

用法：
    python engine/push_repo.py --list                # 看本地领先远端哪些文件
    python engine/push_repo.py engine/foo.py a.md    # 推指定文件
    python engine/push_repo.py --all                 # 推本地领先的全部
    python engine/push_repo.py --all --message "..."  # 自定义提交信息

推完之后本地引用会落后（API 造的 commit 本地没有对象），要手动对齐：
    git fetch origin main && git reset --hard origin/main
fetch 本身也可能被同一条代理打到 502，重试几次即可；
内容一定是一致的——推上去的就是 git 里已存的 blob。
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = "874975861cai/ashare-sim"
API = "https://api.github.com"
TOKEN_FILES = [
    os.path.join(BASE, "state", ".gh_token"),
    r"E:\TradingSim_cloud\state\.gh_token",
]


def token():
    for p in TOKEN_FILES:
        try:
            with open(p, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
        except OSError:
            continue
    t = (os.environ.get("SIM_GH_TOKEN") or "").strip()
    if t:
        return t
    raise SystemExit("找不到 GitHub token（state/.gh_token 或 SIM_GH_TOKEN）")


def git(*args, **kw):
    return subprocess.run(["git"] + list(args), cwd=BASE, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", **kw)


def api(path, method="GET", body=None, tok=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer %s" % tok,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "ashare-sim-push"})
    # 必须直连：系统代理会把 github 打成 502
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        msg = e.read()[:400].decode("utf-8", "replace")
        raise SystemExit("%s %s -> HTTP %s %s" % (method, path, e.code, msg))


def changed_files():
    """本地领先远端的文件（相对 origin/main）。

    core.quotepath=false 是必须的：否则中文文件名会被转义成
    "\\344\\272\\244..." 这种八进制串，推上去就是一个不存在的路径。
    """
    git("fetch", "origin", "main")
    r = git("-c", "core.quotepath=false", "diff", "--name-only",
            "origin/main", "HEAD")
    return [x.strip() for x in r.stdout.splitlines() if x.strip()]


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--all", action="store_true", help="推送本地领先远端的全部文件")
    ap.add_argument("--list", action="store_true", help="只列出差异")
    ap.add_argument("--message", default="", help="提交信息")
    args = ap.parse_args(argv)

    files = changed_files()
    if args.list:
        if not files:
            print("本地与远端一致。")
        for f in files:
            print("  " + f)
        return 0

    targets = files if args.all else args.files
    targets = [t.replace("\\", "/").lstrip("./") for t in targets]
    if not targets:
        print("没有要推送的文件。")
        return 0

    tok = token()
    ref = api("/repos/%s/git/ref/heads/main" % REPO, tok=tok)
    head = ref["object"]["sha"]
    commit = api("/repos/%s/git/commits/%s" % (REPO, head), tok=tok)
    base_tree = commit["tree"]["sha"]

    entries = []
    for path in targets:
        r = subprocess.run(["git", "show", "HEAD:%s" % path], cwd=BASE,
                           capture_output=True)
        if r.returncode != 0:
            raise SystemExit("git 里没有这个文件：%s" % path)
        blob = api("/repos/%s/git/blobs" % REPO, "POST",
                   {"content": base64.b64encode(r.stdout).decode("ascii"),
                    "encoding": "base64"}, tok=tok)
        entries.append({"path": path, "mode": "100644", "type": "blob",
                        "sha": blob["sha"]})
        print("  blob %s -> %s" % (path, blob["sha"][:8]))

    tree = api("/repos/%s/git/trees" % REPO, "POST",
               {"base_tree": base_tree, "tree": entries}, tok=tok)
    msg = args.message or ("sync: " + ", ".join(targets))
    nc = api("/repos/%s/git/commits" % REPO, "POST",
             {"message": msg, "tree": tree["sha"], "parents": [head]}, tok=tok)
    api("/repos/%s/git/refs/heads/main" % REPO, "PATCH",
        {"sha": nc["sha"], "force": False}, tok=tok)
    print("  已提交 %s  (%d 个文件)" % (nc["sha"][:8], len(targets)))
    print("  https://github.com/%s/commit/%s" % (REPO, nc["sha"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
