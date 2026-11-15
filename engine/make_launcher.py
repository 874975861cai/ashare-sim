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

2026-11-15 改（代码自审）：
  f) **校验挪到落盘之前**。原实现先写文件、再读回来体检，不通过才
     SystemExit —— 失败路径上磁盘留下的正好是那个坏文件，而坏文件就是
     用户下次双击要执行的东西。「检查」的意义是不让它落盘，不是事后抱怨。
     现在 payload() 先校验（ASCII / 无孤立 CR / 非空），落盘后只做回读比对，
     比对不上就删掉半成品（杀软改写、磁盘占用都会造成这种残局）。
  g) **解释器变更要说出来**。生成的脚本把 `sys.executable` 的绝对路径写死，
     谁跑生成器谁说了算：在 .workbuddy 的 python 下跑一遍，14 个脚本就
     全指向那个可能随升级消失的路径；doctor 的自动修复也是用当时的
     sys.executable 重跑本脚本，等于「体检顺手换了所有启动脚本的解释器」。
     现在覆盖前先比对旧文件里嵌的解释器，变了就单独打一行。
  h) **停止脚本不再把「pid 文件内容不是数字」当成「进程不存在」**。
     读出来是垃圾（NUL 字节、半截 JSON、手改过的行）时，旧流程一路走到
     stale：删掉 pid 文件、宣告「Nothing was killed」、exit 0 —— 真跑着的
     服务就此变成再也定位不到的孤儿。现在三态：没有 / 不是数字 / 是数字。
  i) **StopAutoTrader 不再无条件删 runner.lock**。原来「没有 pid 文件」也
     一路走到 cleanup 删锁；而锁是 O_EXCL 互斥、10 分钟才自愈——删掉一个
     正在生效的锁等于放第二个 runner 进来同时写账本（lost update，
     不报错，第二天 doctor 才发现）。现在只有「确认没有本地 runner 活着」
     才删。

用法：
    python engine/make_launcher.py
"""
import os
import re
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
    # 走到这一行说明**文件在**，只是读不出内容（0 字节 / NUL / 首行为空）。
    # 报「No pid file」是假的：文件明明在，服务可能正在跑。
    "if not defined SPID goto badpid",
    # 数字闸门：pid 文件是程序写的，但会被 NUL 字节/半截写入/手改弄脏。
    # 脏内容既不能拿去 taskkill（`1 & del ...` 会被 cmd 当成两条命令执行），
    # 也不能当成「进程不存在」而删掉线索 —— 那是三态混成一态。
    # 从**文件**里校验而不是 echo %SPID%|：变量一旦展开，& | 就已经生效了。
    'findstr /R "^[0-9][0-9]*$" "%PIDF%" >nul 2>&1',
    "if errorlevel 1 goto badpid",
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
    ":badpid",
    "echo [ERROR] pid file does not contain a plain number:",
    "echo   %PIDF%",
    "echo        Refusing to guess whether the server is running.",
    "echo        The pid file was KEPT. Check it by hand, or delete it if",
    "echo        you are sure no server is running.",
    "pause",
    "exit /b 1",
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
    'if not exist "%PIDF%" goto nopid',
    "set LPID=",
    'set /p LPID=<"%PIDF%"',
    # 文件在但读不出内容（0 字节/NUL/首行空）≠ 没有 pid 文件；报「Nothing to
    # stop」会把一个可能还活着的孤儿说成没事。
    "if not defined LPID goto badpid",
    'findstr /R "^[0-9][0-9]*$" "%PIDF%" >nul 2>&1',
    "if errorlevel 1 goto badpid",
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
    # 只有走到这里才确定「没有本地 runner 活着」，锁才可以删。
    # 没有 pid 文件不等于没有 runner：一次性 runner 不写 loop.pid，只建
    # runner.lock（O_EXCL，10 分钟自愈）。旧版从「没有 pid 文件」直接
    # 落到这里删锁，等于在别人写字的时候把门打开 —— 两个 runner 同时
    # 写账本就是 lost update，不报错，第二天 doctor 才发现。
    ":cleanup",
    'del /q "%PIDF%" >nul 2>&1',
    'del /q "%LOCKF%" >nul 2>&1',
    "echo Done.",
    "pause",
    "exit /b 0",
    "",
    ":nopid",
    "echo [INFO] No pid file. Nothing to stop.",
    "echo        state\\runner.lock was left alone on purpose:",
    "echo        a one-shot runner may be writing the ledger right now,",
    "echo        and that lock is what keeps a second writer out.",
    "echo        A dead lock expires on its own after 10 minutes.",
    "pause",
    "exit /b 0",
    "",
    ":badpid",
    "echo [ERROR] pid file does not contain a plain number:",
    "echo   %PIDF%",
    "echo        Refusing to guess; pid file and lock were kept.",
    "pause",
    "exit /b 1",
    "",
    ":killfail",
    "echo [ERROR] taskkill failed for pid %LPID% - pid file kept.",
    "pause",
    "exit /b 1",
]


def payload(lines):
    """把行列表做成一个**已在内存里校验过**的 .bat 字节流。

    校验必须在落盘之前。否则失败路径上留下的就是那个坏文件本身——

    三个硬约束（每一条都踩过坑）：
      · 纯 ASCII：混进中文时 cmd 按单字节读会错位并截断路径；
      · 只有 CRLF：纯 LF 的 .bat 会被 cmd 截断成 "'1' 不是内部或外部命令"；
      · 非空。
    """
    if not [ln for ln in lines if str(ln).strip()]:
        # 只有 "\r\n".join([]) 的话会变成单个空行 —— 那是个能「生成成功」的空脚本
        raise ValueError("内容为空")
    try:
        raw = ("\r\n".join(lines) + "\r\n").encode("ascii")
    except UnicodeEncodeError as e:
        raise ValueError("含非 ASCII 字符（%r）：cmd 会按单字节读，路径会被截断"
                         % (e.object[:40],))
    if not raw:
        raise ValueError("内容为空")
    naked = raw.replace(b"\r\n", b"")
    if b"\n" in naked or b"\r" in naked:
        # 孤立 CR 同样会把行切开，而老实现的 lf_only 只数 \n，看不见它
        raise ValueError("出现孤立的 LF/CR（cmd 会在这里截断行）")
    return raw


_PY_RE = re.compile(rb"[A-Za-z]:\\[^\"\r\n]*pythonw?\.exe", re.I)


def _embedded_python(raw):
    """从已有脚本里抠出它写死的解释器路径，没有就返回空串。"""
    m = _PY_RE.search(raw or b"")
    return m.group(0).decode("ascii", "replace") if m else ""


def _emit(name, lines):
    """校验 → 落盘 → 回读比对。任何一步不过都不留下坏文件。"""
    try:
        data = payload(lines)
    except ValueError as e:
        raise SystemExit("%s 未写入：%s" % (name, e))
    path = os.path.join(ROOT, name)
    old = b""
    try:
        with open(path, "rb") as f:
            old = f.read()
    except OSError:
        old = b""
    if old and old != data:
        old_py, new_py = _embedded_python(old), _embedded_python(data)
        if old_py and new_py and old_py.lower() != new_py.lower():
            # 不拦，但必须让人看见：这条路径可能随 IDE/虚拟环境消失
            print("  ! %-21s 解释器变更  %s  ->  %s" % (name, old_py, new_py))
    with open(path, "wb") as f:
        f.write(data)
    with open(path, "rb") as f:
        back = f.read()
    if back != data:
        try:
            os.remove(path)          # 半成品比没文件更糟：它会被双击执行
        except OSError:
            pass
        raise SystemExit("%s 写入后回读不一致（磁盘被占用/被改写？），已删除半成品。"
                         % name)
    print("%-24s %6d bytes  CRLF=True  bare_LF=0  ascii=True" % (name, len(back)))
    return len(back)


GROUPS = [
    # (说明, 中文别名, 内容)
    ("OpenDashboard.bat", "\u6253\u5f00\u6a21\u62df\u76d8.bat", LINES),
    ("StopServer.bat", "\u505c\u6b62\u5b9e\u65f6\u670d\u52a1.bat", STOP_LINES),
    ("InstallAutostart.bat", "\u5b89\u88c5\u5f00\u673a\u81ea\u542f.bat", INSTALL_LINES),
    ("QuickView.bat", "\u5feb\u901f\u67e5\u770b.bat", FAST_LINES),
    ("FixPower.bat", "\u8bbe\u7f6e\u4e0d\u4f11\u7720.bat", POWER_LINES),
    ("StartAutoTrader.bat", "\u542f\u52a8\u81ea\u52a8\u4ea4\u6613.bat", TRADER_LINES),
    ("StopAutoTrader.bat", "\u505c\u6b62\u81ea\u52a8\u4ea4\u6613.bat", STOP_TRADER_LINES),
]


def all_payloads():
    """14 个脚本的「应该长什么样」，供 doctor 比对磁盘。

    为什么要暴露出来：.bat 在 .gitignore 里，模板改了但没人重跑生成器时，
    磁盘上的一直是旧脚本 —— 编码检查永远发现不了这种漂移（10-23 漂过一次：
    16:04 生成、16:06 改模板）。doctor 拿这份清单才能既查「缺没缺」，
    也查「内容与模板一致吗」。
    """
    out = {}
    for ascii_name, cn_name, lines in GROUPS:
        out[ascii_name] = payload(lines)
        out[cn_name] = payload(lines)
    return out


def main():
    total = 0
    for ascii_name, cn_name, lines in GROUPS:
        total += _emit(ascii_name, lines)
        total += _emit(cn_name, lines)
    print("-" * 60)
    print("共 %d 个脚本，%d 字节。全部 ASCII + CRLF。" % (len(GROUPS) * 2, total))
    print("解释器：%s" % PYW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
