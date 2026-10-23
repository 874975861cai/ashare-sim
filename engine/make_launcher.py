#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Windows 启动脚本。

关键约束（踩过的坑）：
  1. 必须是 CRLF 换行。cmd.exe 对纯 LF 的 .bat 解析会截断行，产生
     "'1' 不是内部或外部命令" 这类碎片错误。
  2. 必须是纯 ASCII。文件里混入 UTF-8 中文时，cmd 按单字节读取会错位，
     把路径从中截断。提示语一律用英文。
  3. 不用 chcp 65001 —— 那玩意儿在批处理里反而会打乱后续行的解码。

2026-10-23 改（代码自审）：
  a) ROOT / PY 不再写死。原来硬编码 E:\\TradingSim 与某个具体 Python 版本，
     换盘、换版本、克隆到别处，生成出来的脚本全都指向空气，而且照样打印成功。
     现在按本文件位置推导，Python 取当前解释器旁边的 pythonw.exe。
  b) 补齐「启动/停止自动交易」两个脚本。这两个原来是在仓库外手写的
     （*.bat 被 .gitignore 排除，永远进不了代码评审），踩了两个坑：
       - 路径写成 E://TradingSim//engine//runner.py —— cmd 的 cd 不认正斜杠；
       - 「是否已在运行」用 findstr /C:/<pid>/ 去匹配 tasklist 输出里
         根本不存在的 "/<pid>/"，永远判定「没在跑」。
     更关键的是：本地账本已由 GitHub Actions 接管，本地跑 runner.py 会造成
     双写分叉。所以启动脚本改成**默认拒绝启动**，只打印该走哪条路。
  c) 自检不再只打印。原来算出了 bare_LF / 非 ASCII 却只 print 一行，
     main() 照样返回 0 —— 「验证了，但验证结果没人消费」。现在不合规直接非零退出。
  d) 停止脚本不再盲杀。taskkill 之前先确认 pid 真的是 python 进程，
     避免 pid 被系统回收后杀掉一个毫不相干的程序；taskkill 失败也不再
     谎报「已停止」。
  e) QuickView 不再把旧页当新页打开。dashboard.py 失败时原来的流程会继续
     检查 dashboard\\index.html 是否存在 —— 而它多半是上一次留下的，
     于是崩了也照样弹出页面，看着一切正常。现在先看退出码。

用法：
    python engine/make_launcher.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# pythonw.exe 与当前解释器同目录；找不到就退回 python.exe（会有黑窗口，但能跑）
_PY = sys.executable
_PYW = os.path.join(os.path.dirname(_PY), "pythonw.exe")
if not os.path.exists(_PYW):
    _PYW = _PY

PY = _PY
PYW = _PYW

# 判断某个 pid 是否仍是 python 进程。tasklist 用两条件 AND 过滤，
# 并且在 for 里比较 token2 == pid 才认账：
#   - 匹配到时输出 "pythonw.exe   1234 Console  1  12,000 K"，token2 就是 pid；
#   - 没匹配到时输出一行本地化的 "INFO: No tasks are running..."，
#     token2 是 "No"/"没有..."，永远不等于 pid。
# 这样既不依赖英文提示语（中文 Windows 上是「信息: 没有运行的任务...」），
# 也不会因为 pid 被回收而误杀无关进程。
PID_ALIVE = [
    'set ALIVE=',
    'for /f "tokens=2" %%i in (\'tasklist /FI "PID eq %PID%" /FI "IMAGENAME eq pythonw.exe" /NH 2^>nul\') do if "%%i"=="%PID%" set ALIVE=1',
    'for /f "tokens=2" %%i in (\'tasklist /FI "PID eq %PID%" /FI "IMAGENAME eq python.exe" /NH 2^>nul\') do if "%%i"=="%PID%" set ALIVE=1',
]


def _alive_lines(var):
    return [ln.replace("%PID%", "%" + var + "%") for ln in PID_ALIVE]


# 实时版：后台起本地服务（无窗口），页面每 4 秒同步一次
LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim Live",
    'cd /d "' + ROOT + '"',
    'if not exist "' + ROOT + r'\engine\serve.py" goto no_script',
    'if not exist "' + PYW + r'" goto no_python',
    "echo Starting live dashboard...",
    "echo.",
    "echo   The server runs in the background (no window to close).",
    "echo   Browser opens by itself; page refreshes every few seconds.",
    "echo   To stop it, run: StopServer.bat",
    "echo.",
    'start "" "' + PYW + r'" engine\serve.py',
    "exit /b 0",
    "",
    ":no_python",
    "echo [ERROR] pythonw.exe not found:",
    "echo   " + PYW,
    "echo Re-run: python engine\\make_launcher.py",
    "pause",
    "exit /b 1",
    "",
    ":no_script",
    r"echo [ERROR] engine\serve.py not found.",
    "pause",
    "exit /b 1",
]

# 停止后台服务：读 state/serve.pid，确认是 python 进程再杀
STOP_LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim - stop live server",
    'cd /d "' + ROOT + '"',
    'set PIDF=' + ROOT + r'\state\serve.pid',
    'if not exist "%PIDF%" goto nopid',
    "set SPID=",
    'set /p SPID=<"%PIDF%"',
    "if not defined SPID goto nopid",
] + _alive_lines("SPID") + [
    "if not defined ALIVE goto stale",
    'taskkill /PID %SPID% /F >nul 2>&1',
    "if errorlevel 1 goto killfail",
    'del /q "%PIDF%" >nul 2>&1',
    "echo [OK] server stopped (pid %SPID%).",
    "pause",
    "exit /b 0",
    "",
    ":stale",
    'del /q "%PIDF%" >nul 2>&1',
    "echo [INFO] pid %SPID% is not a running python process.",
    "echo        Removed stale pid file. Nothing was killed.",
    "pause",
    "exit /b 0",
    "",
    ":killfail",
    "echo [ERROR] taskkill failed for pid %SPID% - pid file kept.",
    "echo         Check Task Manager; the process may belong to another user.",
    "pause",
    "exit /b 1",
    "",
    ":nopid",
    "echo [INFO] No pid file. Server is probably not running.",
    "pause",
    "exit /b 1",
]

# 一键装开机自启 + 桌面快捷方式
INSTALL_LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim - install autostart",
    'cd /d "' + ROOT + '"',
    'if not exist "' + PY + r'" goto no_python',
    'if not exist "' + ROOT + r'\engine\install_autostart.py" goto no_script',
    '"' + PY + r'" engine\install_autostart.py',
    "if errorlevel 1 goto failed",
    "echo.",
    "echo [OK] autostart installed.",
    "pause",
    "exit /b 0",
    "",
    ":failed",
    "echo.",
    "echo [ERROR] install_autostart.py failed (see messages above).",
    "echo         Autostart was NOT installed.",
    "pause",
    "exit /b 1",
    "",
    ":no_python",
    "echo [ERROR] python.exe not found:",
    "echo   " + PY,
    "echo Re-run: python engine\\make_launcher.py",
    "pause",
    "exit /b 1",
    "",
    ":no_script",
    r"echo [ERROR] engine\install_autostart.py not found.",
    "pause",
    "exit /b 1",
]

# 秒开版：不联网，直接拿本地缓存重绘，通常 <1 秒
FAST_LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim (offline)",
    'cd /d "' + ROOT + '"',
    'if not exist "' + ROOT + r'\engine\dashboard.py" goto no_script',
    'if not exist "' + PY + r'" goto no_python',
    "echo [1/2] Rendering from local cache (no network)...",
    '"' + PY + r'" engine\dashboard.py --offline',
    "if errorlevel 1 goto render_failed",
    'if not exist "' + ROOT + r'\dashboard\index.html" goto no_html',
    "echo [2/2] Opening dashboard...",
    'start "" "' + ROOT + r'\dashboard\index.html"',
    "exit /b 0",
    "",
    ":render_failed",
    "echo.",
    "echo [ERROR] dashboard.py failed. The HTML on disk was NOT rebuilt,",
    "echo         so it is stale - refusing to open it.",
    "pause",
    "exit /b 1",
    "",
    ":no_html",
    r"echo [ERROR] dashboard\index.html not generated.",
    "pause",
    "exit /b 1",
    "",
    ":no_python",
    "echo [ERROR] python.exe not found:",
    "echo   " + PY,
    "pause",
    "exit /b 1",
    "",
    ":no_script",
    r"echo [ERROR] engine\dashboard.py not found.",
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
    # 用 0x 过滤而不是英文原句：powercfg 的输出是本地化的，
    # 中文 Windows 上写 "Current AC Power Setting Index" 会一行都匹配不到。
    "echo === Check: in each block the FIRST 0x line is AC, the SECOND is DC ===",
    "echo     AC must read 0x00000000 (DC may stay non-zero) ===",
    "echo [1] Sleep timeout:",
    'powercfg /q SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | findstr /C:"0x"',
    "echo [2] Lid close action:",
    'powercfg /q SCHEME_CURRENT SUB_BUTTONS LIDACTION | findstr /C:"0x"',
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

# 本地自动交易：账本主人已经是 GitHub Actions，本地再跑就是双写分叉。
# 这个脚本不再启动任何东西，只把原因和替代方案说清楚。
TRADER_LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim - local auto-trader is DISABLED",
    'cd /d "' + ROOT + '"',
    "echo ============================================================",
    "echo  LOCAL AUTO-TRADER IS DISABLED ON PURPOSE",
    "echo ============================================================",
    "echo.",
    "echo  The ledger is owned by GitHub Actions (874975861cai/ashare-sim).",
    "echo  It replays the day and commits every 5 minutes on weekdays,",
    "echo  and that is the only writer.",
    "echo.",
    "echo  This folder is a READ-ONLY mirror. Running",
    r"echo      engine\runner.py --loop",
    "echo  here would write the same ledger a second time: two writers,",
    "echo  two diverging books, and nothing raises an error.",
    "echo.",
    "echo  If you need a local run, use the read-only form by hand:",
    'echo      cd /d "' + ROOT + '"',
    'echo      "' + PY + r'" engine\runner.py --dry',
    "echo.",
    "echo  Nothing was started.",
    "pause",
    "exit /b 1",
]

# 停止本地循环：正常情况下已经不会有本地循环了，这里只做残留清理。
# 仍然保留 taskkill，但先确认 pid 是 python 进程 —— 陈旧的 pid 文件里
# 那个号可能已经被系统分配给别的程序了。
STOP_TRADER_LINES = [
    "@echo off",
    "setlocal",
    "title TradingSim - stop local auto-trader",
    'cd /d "' + ROOT + '"',
    'set PIDF=' + ROOT + r'\state\loop.pid',
    'set LOCKF=' + ROOT + r'\state\runner.lock',
    'if not exist "%PIDF%" goto cleanup',
    "set LPID=",
    'set /p LPID=<"%PIDF%"',
    "if not defined LPID goto cleanup",
] + _alive_lines("LPID") + [
    "if not defined ALIVE goto stale",
    "echo Stopping local auto-trader (pid %LPID%) ...",
    'taskkill /PID %LPID% /F >nul 2>&1',
    "if errorlevel 1 goto killfail",
    "echo [OK] stopped pid %LPID%",
    "goto cleanup",
    "",
    ":stale",
    "echo [INFO] pid %LPID% is not a running python process (stale pid file).",
    "echo        Nothing was killed.",
    "goto cleanup",
    "",
    ":cleanup",
    'del /q "%PIDF%" >nul 2>&1',
    'del /q "%LOCKF%" >nul 2>&1',
    "echo Done.",
    "pause",
    "exit /b 0",
    "",
    ":killfail",
    "echo [ERROR] taskkill failed for pid %LPID% - pid file kept.",
    "pause",
    "exit /b 1",
]


def _emit(name, lines):
    """写一个 .bat，并且**根据自检结果决定要不要报错**。"""
    try:
        payload = ("\r\n".join(lines) + "\r\n").encode("ascii")
    except UnicodeEncodeError as e:
        raise SystemExit("%s 含非 ASCII 字符（%r）：cmd 会按单字节读，路径会被截断"
                         % (name, e.object[:40]))
    path = os.path.join(ROOT, name)
    with open(path, "wb") as f:
        f.write(payload)
    with open(path, "rb") as f:
        raw = f.read()
    lf_only = raw.count(b"\n") - raw.count(b"\r\n")
    non_ascii = any(c > 127 for c in raw)
    print("%-24s %6d bytes  CRLF=%s  bare_LF=%d  ascii=%s"
          % (name, len(raw), b"\r\n" in raw, lf_only, not non_ascii))
    # 以前这里只 print，main() 照样 exit 0 —— 生成了一个坏文件也算成功
    if not raw or lf_only or non_ascii:
        raise SystemExit("%s 自检不通过：bare_LF=%d non_ascii=%s" % (name, lf_only, non_ascii))
    return len(raw)


def main():
    groups = [
        # (说明, 中文别名, 内容)
        ("OpenDashboard.bat", "\u6253\u5f00\u6a21\u62df\u76d8.bat", LINES),
        ("StopServer.bat", "\u505c\u6b62\u5b9e\u65f6\u670d\u52a1.bat", STOP_LINES),
        ("InstallAutostart.bat", "\u5b89\u88c5\u5f00\u673a\u81ea\u542f.bat", INSTALL_LINES),
        ("QuickView.bat", "\u5feb\u901f\u67e5\u770b.bat", FAST_LINES),
        ("FixPower.bat", "\u8bbe\u7f6e\u4e0d\u4f11\u7720.bat", POWER_LINES),
        ("StartAutoTrader.bat", "\u542f\u52a8\u81ea\u52a8\u4ea4\u6613.bat", TRADER_LINES),
        ("StopAutoTrader.bat", "\u505c\u6b62\u81ea\u52a8\u4ea4\u6613.bat", STOP_TRADER_LINES),
    ]
    total = 0
    for ascii_name, cn_name, lines in groups:
        total += _emit(ascii_name, lines)
        total += _emit(cn_name, lines)
    print("-" * 60)
    print("共 %d 个脚本，%d 字节。全部 ASCII + CRLF。" % (len(groups) * 2, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
