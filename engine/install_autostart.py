#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""装开机自启 + 桌面快捷方式。

为什么需要这个脚本：
    之前「实时行情」失效过一次，根因不是接口，而是**服务没在跑**。
    用户看到的是磁盘上的静态 index.html，时间停在生成那一刻。
    页面本身现在会自己去找本地服务（fetch + CORS），但服务得先活着。

    最省事的办法是开机自动起服务，这件事必须写到用户目录，
    而 AI 的运行沙箱只能写 E:\\TradingSim，所以做成一个双击即可的安装脚本。

做的事：
    1. %APPDATA%\\...\\Startup\\TradingSim_Live.bat  —— 登录时静默启动服务
    2. 桌面\\实时看盘.url                              —— 双击直连实时页
    3. 立刻把服务起起来，并把浏览器打开

用法：
    双击 E:\\TradingSim\\安装开机自启.bat
"""
import os
import subprocess
import sys
import time

ROOT = r"E:\TradingSim"
PYW = r"C:\Users\abc\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
SERVE = os.path.join(ROOT, "engine", "serve.py")
PIDFILE = os.path.join(ROOT, "state", "serve.pid")
URL = "http://127.0.0.1:8899/"


def _startup_dir():
    return os.path.join(os.environ["APPDATA"],
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def _desktop_dir():
    # 桌面可能被重定向到 OneDrive，先读注册表，读不到就退回 %USERPROFILE%\Desktop
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
        v, _ = winreg.QueryValueEx(k, "Desktop")
        return os.path.expandvars(v)
    except Exception:  # noqa: BLE001
        return os.path.join(os.environ["USERPROFILE"], "Desktop")


def write_startup():
    lines = [
        "@echo off",
        "rem TradingSim live dashboard - silent start at logon",
        "rem pythonw = no console window; single-instance guard is inside serve.py",
        'start "" "%s" "%s" --no-browser' % (PYW, SERVE),
    ]
    d = _startup_dir()
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "TradingSim_Live.bat")
    with open(p, "wb") as f:
        f.write(("\r\n".join(lines) + "\r\n").encode("ascii"))
    return p


def write_shortcut():
    d = _desktop_dir()
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "实时看盘.url")
    body = "[InternetShortcut]\r\nURL=%s\r\nIconIndex=0\r\n" % URL
    with open(p, "wb") as f:
        f.write(body.encode("ascii"))
    return p


def already_running():
    try:
        import urllib.request
        with urllib.request.urlopen(
                "http://127.0.0.1:8899/api/ping", timeout=0.8) as r:
            return b'"ok"' in r.read()
    except Exception:  # noqa: BLE001
        return False


def start_service():
    if already_running():
        return "already"
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    subprocess.Popen([PYW, SERVE, "--no-browser"],
                     cwd=ROOT, startupinfo=si, close_fds=True)
    for _ in range(30):
        time.sleep(0.4)
        if already_running():
            return "started"
    return "failed"


def main():
    print("=" * 58)
    print("  TradingSim 实时服务安装")
    print("=" * 58)

    try:
        p1 = write_startup()
        print("[1/3] 开机自启已写入")
        print("      %s" % p1)
    except Exception as e:  # noqa: BLE001
        print("[1/3] 开机自启失败：%s" % e)

    try:
        p2 = write_shortcut()
        print("[2/3] 桌面快捷方式已创建")
        print("      %s" % p2)
    except Exception as e:  # noqa: BLE001
        print("[2/3] 桌面快捷方式失败：%s" % e)

    st = start_service()
    if st == "already":
        print("[3/3] 服务已在运行，不重复启动")
    elif st == "started":
        print("[3/3] 服务已启动（后台，无窗口）")
    else:
        print("[3/3] 服务启动失败，请手动双击 打开模拟盘.bat 查看报错")

    print()
    print("以后每次开机，服务都会自己起来；桌面「实时看盘」直接打开。")
    print("地址   %s" % URL)
    print("停止   双击 停止实时服务.bat")
    print("=" * 58)

    if st in ("already", "started"):
        import webbrowser
        webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
