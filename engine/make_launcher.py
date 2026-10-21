#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Windows 启动脚本。

关键约束（踩过的坑）：
  1. 必须是 CRLF 换行。cmd.exe 对纯 LF 的 .bat 解析会截断行，产生
     "'1' 不是内部或外部命令" 这类碎片错误。
  2. 必须是纯 ASCII。文件里混入 UTF-8 中文时，cmd 按单字节读取会错位，
     把路径从中截断。提示语一律用英文。
  3. 不用 chcp 65001 —— 那玩意儿在批处理里反而会打乱后续行的解码。
"""
import os

PY = r"C:\Users\abc\.workbuddy\binaries\python\versions\3.13.12\python.exe"
PYW = r"C:\Users\abc\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
ROOT = r"E:\TradingSim"

LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim Live",
    'cd /d "' + ROOT + '"',
    'if not exist "' + ROOT + r'\engine\serve.py" goto no_script',
    "echo Starting live dashboard...",
    "echo.",
    "echo   The server runs in the background (no window to close).",
    "echo   Browser opens by itself; page refreshes every few seconds.",
    "echo   To stop it, run: StopServer.bat",
    "echo.",
    "start \"\" \"" + PYW + "\" engine\\serve.py",
    "exit /b 0",
    "",
    ":no_script",
    "echo [ERROR] engine\\serve.py not found.",
    "pause",
    "exit /b 1",
]

# 停止后台服务：读 state/serve.pid，杀进程
STOP_LINES = [
    "@echo off",
    "setlocal",
    'cd /d "' + ROOT + '"',
    'if not exist "state\\serve.pid" goto nopid',
    'set /p SPID=<"state\\serve.pid"',
    "taskkill /PID %SPID% /F >nul 2>&1",
    'del /q "state\\serve.pid" >nul 2>&1',
    "echo Service stopped (pid %SPID%).",
    "pause",
    "exit /b 0",
    "",
    ":nopid",
    "echo No pid file. Service is probably not running.",
    "pause",
    "exit /b 1",
]

# 一键装开机自启 + 桌面快捷方式
INSTALL_LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim - install autostart",
    'cd /d "' + ROOT + '"',
    '"' + PY + '" engine\\install_autostart.py',
    "echo.",
    "pause",
    "exit /b 0",
]

# 秒开版：不联网，直接拿本地缓存重绘，通常 <1 秒
FAST_LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim (offline)",
    'cd /d "' + ROOT + '"',
    'if not exist "' + ROOT + r'\engine\dashboard.py" goto no_script',
    "echo [1/2] Rendering from local cache (no network)...",
    '"' + PY + '" engine\\dashboard.py --offline',
    'if not exist "' + ROOT + r'\dashboard\index.html" goto no_html',
    "echo [2/2] Opening dashboard...",
    'start "" "' + ROOT + r'\dashboard\index.html"',
    "exit /b 0",
    "",
    ":no_script",
    "echo [ERROR] engine\\dashboard.py not found.",
    "pause",
    "exit /b 1",
    "",
    ":no_html",
    "echo [ERROR] dashboard\\index.html not generated.",
    "pause",
    "exit /b 1",
]


# 电源策略：笔记本插电时常开（合盖不睡眠），需管理员权限
POWER_LINES = [
    "@echo off",
    "setlocal",
    "title Fix Power Policy (Admin required)",
    "net session >nul 2>&1",
    "if errorlevel 1 goto need_admin",
    "",
    "echo Applying AC power policy...",
    "echo.",
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0 >nul 2>&1",
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP HIBERNATEIDLE 0 >nul 2>&1",
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP UNATTENDSLEEP 0 >nul 2>&1",
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_DISK DISKIDLE 0 >nul 2>&1",
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 >nul 2>&1",
    "powercfg /setacvalueindex SCHEME_CURRENT SUB_VIDEO VIDEOIDLE 900 >nul 2>&1",
    "powercfg /setactive SCHEME_CURRENT >nul 2>&1",
    "echo.",
    "echo === Check: both AC values below should be 0x00000000 ===",
    "echo [1] Sleep timeout:",
    "powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | findstr /C:\"0x\"",
    "echo [2] Lid close action:",
    "powercfg /q SCHEME_CURRENT SUB_BUTTONS LIDACTION | findstr /C:\"0x\"",
    "echo.",
    "echo Done. Laptop keeps running with lid closed (on AC power).",
    "echo No reboot needed. Unplug power and it may still sleep by design.",
    "echo.",
    "pause",
    "exit /b 0",
    "",
    ":need_admin",
    "echo [ERROR] Administrator rights required.",
    "echo Right-click this file and choose: Run as administrator",
    "echo.",
    "pause",
    "exit /b 1",
]


def _emit(name, lines):
    payload = ("\r\n".join(lines) + "\r\n").encode("ascii")
    path = os.path.join(ROOT, name)
    with open(path, "wb") as f:
        f.write(payload)
    raw = open(path, "rb").read()
    lf_only = raw.count(b"\n") - raw.count(b"\r\n")
    print("%-24s %4d bytes  CRLF=%s  bare_LF=%d  ascii=%s"
          % (name, len(raw), b"\r\n" in raw, lf_only,
             all(c < 128 for c in raw)))


def main():
    # 实时版：后台起本地服务（无窗口），页面每 4 秒同步一次
    for name in ("OpenDashboard.bat", "\u6253\u5f00\u6a21\u62df\u76d8.bat"):
        _emit(name, LINES)
    # 停止后台服务
    for name in ("StopServer.bat", "\u505c\u6b62\u5b9e\u65f6\u670d\u52a1.bat"):
        _emit(name, STOP_LINES)
    # 装开机自启 + 桌面快捷方式
    for name in ("InstallAutostart.bat", "\u5b89\u88c5\u5f00\u673a\u81ea\u542f.bat"):
        _emit(name, INSTALL_LINES)
    # 秒开版：只用本地缓存重绘静态页
    for name in ("QuickView.bat", "\u5feb\u901f\u67e5\u770b.bat"):
        _emit(name, FAST_LINES)
    # 电源策略：合盖不睡眠（需管理员）
    for name in ("FixPower.bat", "\u8bbe\u7f6e\u4e0d\u4f11\u7720.bat"):
        _emit(name, POWER_LINES)


if __name__ == "__main__":
    main()
