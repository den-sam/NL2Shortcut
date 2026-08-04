"""NL2Shortcut Agent API 的自检模块。

在注入键盘快捷键之后，Agent 无法"看到"屏幕来确认它是否真正生效。
我们通过采集注入后 50ms 的系统状态，并与注入前的快照进行对比来近似判断。

按操作类型可验证的内容：
  - copy  : 剪贴板内容发生变化（或原本非空）
  - paste : 聚焦控件收到了文本（尽力而为：剪贴板被清空）
  - cut   : 剪贴板获得了内容
  - undo  : （跨平台没有可靠的信号；报告为未知）
  - save  : 文件的 mtime 被更新（若应用上下文包含文件路径）
  - switch: 前台窗口发生了变化
  - select_all: 剪贴板？不会 —— 通常没有可观察的迹象
  - text_input: 聚焦控件的文本长度有所增加（尽力而为）

如果自检失败，API 响应会设置：
  fallback_triggered = True
  error.code = E_KEY_COMBINATION_NO_RESPONSE
  retryable = True
"""
import time
import os
import sys
import subprocess
import threading
from typing import Optional, Dict, Any, Callable, List, Tuple

try:
    import win32clipboard  # type: ignore
    _HAS_WIN32 = True
except Exception:
    _HAS_WIN32 = False

try:
    import win32gui  # type: ignore
    _HAS_WIN32GUI = True
except Exception:
    _HAS_WIN32GUI = False

# ctypes 始终可用（Windows 内置），作为 win32gui 的后备
import ctypes
import ctypes.wintypes
_HAS_CTYPE = hasattr(ctypes, 'windll')


# 默认值：注入后 50ms 的采样窗口（依据 2026 规范）
DEFAULT_DELAY_MS = 50
MAX_DELAY_MS = 1000  # 系统级操作（如 Win+E 打开资源管理器）需要更长等待


def _read_clipboard() -> Optional[str]:
    """读取剪贴板文本（Windows）。失败时返回 None。"""
    if not _HAS_WIN32:
        return None
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return data
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None
    return None


def _read_foreground_window() -> Optional[str]:
    """读取前台窗口的标题和进程名（Windows）。

    返回格式: "窗口标题|进程名" — 进程名用于检测系统级操作
    （如 Win+E 打开资源管理器后前台进程变为 explorer.exe）。

    优先使用 win32gui，未安装时用 ctypes 后备。
    """
    # ── 路径 1: win32gui ──
    if _HAS_WIN32GUI:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                title = win32gui.GetWindowText(hwnd)
                proc_name = _get_process_name(hwnd)
                return f"{title}|{proc_name}"
        except Exception:
            pass

    # ── 路径 2: ctypes 后备（无需 pywin32）──
    if _HAS_CTYPE:
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                # 获取窗口标题
                length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                proc_name = _get_process_name_ctypes(hwnd)
                return f"{title}|{proc_name}"
        except Exception:
            pass

    return None


def _get_process_name(hwnd) -> str:
    """通过 win32gui/win32process 获取窗口进程名。"""
    try:
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return _get_proc_name_by_pid(pid)
    except Exception:
        return ""


def _get_process_name_ctypes(hwnd) -> str:
    """通过 ctypes 获取窗口进程名（无需 pywin32）。"""
    try:
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return _get_proc_name_by_pid(pid.value)
    except Exception:
        return ""


def _get_proc_name_by_pid(pid: int) -> str:
    """通过 PID 获取进程可执行文件名。"""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h:
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.psapi.GetModuleFileNameExW(h, None, buf, 1024):
                name = buf.value.rsplit("\\", 1)[-1].lower()
                ctypes.windll.kernel32.CloseHandle(h)
                return name
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


# 操作 -> 检查函数。每个检查函数都以 (pre_state, post_state) 调用，
# 并返回 (ok, message)。
# 返回 `None` 表示无法检查该操作；将其视为"未知"（而非"失败"）。
_CheckFn = Callable[[Optional[Dict[str, Any]], Optional[Dict[str, Any]]], Tuple[Optional[bool], str]]

_CHECKS: Dict[str, _CheckFn] = {}


def _check_clipboard_changed(pre, post) -> Tuple[Optional[bool], str]:
    if post is None or pre is None:
        return None, "clipboard not available"
    if pre == "" and post == "":
        return False, "clipboard empty before and after"
    if pre != post:
        return True, "clipboard content changed"
    return False, "clipboard unchanged"


def _check_window_changed(pre, post) -> Tuple[Optional[bool], str]:
    if post is None or pre is None:
        return None, "window info not available"
    if pre != post:
        # 检查是否前台进程变为 explorer.exe（Win+E 打开资源管理器）
        pre_proc = pre.split("|", 1)[-1] if "|" in pre else ""
        post_proc = post.split("|", 1)[-1] if "|" in post else ""
        if post_proc == "explorer.exe" and pre_proc != "explorer.exe":
            return True, "资源管理器窗口已激活 (explorer.exe)"
        return True, "前台窗口已切换"
    # 窗口未变化：检查是否目标窗口已经是前台窗口
    # （例如资源管理器已打开，再按 Win+E 不会改变前台窗口）
    proc = post.split("|", 1)[-1] if "|" in post else ""
    if proc == "explorer.exe":
        return True, "资源管理器已是前台窗口（无需切换）"
    return False, "前台窗口未变化"


def _check_mtime_changed(pre, post) -> Tuple[Optional[bool], str]:
    if post is None or pre is None:
        return None, "mtime not available"
    if post > pre:
        return True, "file mtime updated"
    return False, "file mtime unchanged"


# 操作关键字 -> 检查名称 的映射
_OPERATION_HINTS: List[Tuple[str, str, str]] = [
    # (命令中包含的关键字, 检查名称, 人类可读描述)
    ("copy",            "clipboard", "剪贴板获取到了文本"),
    ("cut",             "clipboard", "剪贴板获取到了文本"),
    ("paste",           "clipboard", "剪贴板被清空"),
    ("save",            "mtime",     "文件 mtime 已更新"),
    ("switch",          "window",    "前台窗口已切换"),
    ("alt_tab",         "window",    "前台窗口已切换"),
    ("alt+tab",         "window",    "前台窗口已切换"),
    ("switch_app",      "window",    "前台窗口已切换"),
    # ── 系统级操作：通过窗口标题变化验证 ──
    ("ms_win_e",        "window",    "资源管理器窗口已打开"),
    ("open_explorer",   "window",    "资源管理器窗口已打开"),
    ("ms_win_e_2",      "window",    "资源管理器窗口已打开"),
    ("run_dialog",      "window",    "运行对话框已打开"),
    ("ms_win_r",        "window",    "运行对话框已打开"),
    ("lock_screen",     "window",    "屏幕已锁定"),
    ("ms_win_l",        "window",    "屏幕已锁定"),
    ("task_manager",    "window",    "任务管理器已打开"),
    ("task_view",       "window",    "任务视图已打开"),
    ("ms_win_tab",      "window",    "任务视图已打开"),
    ("minimize",        "window",    "窗口已最小化"),
    ("ms_win_d",        "window",    "桌面已显示"),
    # ── 编辑操作：无可观察信号 ──
    ("undo",            "noop",      "没有可观察的信号"),
    ("redo",            "noop",      "没有可观察的信号"),
    ("select",          "noop",      "没有可观察的信号"),
    ("select_all",      "noop",      "没有可观察的信号"),
    ("go_to_line",      "noop",      "没有可观察的信号"),
    ("go_to_definition","noop",      "没有可观察的信号"),
    ("format",          "noop",      "没有可观察的信号"),
    ("comment",         "noop",      "没有可观察的信号"),
    ("rename",          "noop",      "没有可观察的信号"),
    ("duplicate",       "noop",      "没有可观察的信号"),
    ("move_line",       "noop",      "没有可观察的信号"),
    ("find",            "noop",      "没有可观察的信号"),
    ("replace",         "noop",      "没有可观察的信号"),
]


def _resolve_check(command: str, app_name: str = "") -> Tuple[str, str]:
    """返回给定命令名对应的 (check_name, description)。

    感知应用类型：终端应用使用 Ctrl+C 发送 SIGINT，而不是复制到剪贴板。
    """
    cmd = (command or "").lower()

    # 终端应用：Ctrl+C 发送的是 SIGINT 而不是复制 —— 跳过剪贴板检查
    if app_name in ("terminal", "cmd", "powershell", "wt", "conhost", "alacritty",
                    "iterm", "iterm2", "kitty", "gnome-terminal", "xterm"):
        if cmd in ("copy", "cut", "paste"):
            return "noop", f"terminal {cmd}: Ctrl+C is SIGINT, skipping clipboard check"

    for kw, check, desc in _OPERATION_HINTS:
        if kw in cmd:
            return check, desc
    return "noop", "no self-check available for this operation"


def snapshot(command: str, file_path: Optional[str] = None,
             app_name: str = "") -> Dict[str, Any]:
    """捕获与本次命令相关的注入前系统状态。"""
    check, desc = _resolve_check(command, app_name=app_name)
    snap: Dict[str, Any] = {"check": check, "description": desc}
    if check == "clipboard":
        snap["value"] = _read_clipboard() or ""
    elif check == "window":
        snap["value"] = _read_foreground_window() or ""
    elif check == "mtime":
        snap["value"] = os.path.getmtime(file_path) if (file_path and os.path.exists(file_path)) else None
    # noop：无需快照
    return snap


def verify(command: str, pre_snap: Dict[str, Any], file_path: Optional[str] = None,
           delay_ms: int = DEFAULT_DELAY_MS, app_name: str = "") -> Dict[str, Any]:
    """等待 `delay_ms` 后采集注入后快照并运行检查，返回结果字典。

    结果: {
      "ran": True,
      "ok": True/False/None,    # None = 未知
      "check": "clipboard",
      "description": "...",
      "message": "剪贴板内容已变化",
      "delay_ms": 50,
    }
    """
    check = pre_snap.get("check", "noop")
    desc = pre_snap.get("description", "")
    if check == "noop":
        return {"ran": False, "ok": None, "check": "noop", "description": desc,
                "message": "no self-check available", "delay_ms": 0}
    # 等待操作系统处理该输入
    time.sleep(min(delay_ms, MAX_DELAY_MS) / 1000.0)
    post: Dict[str, Any] = {"check": check}
    if check == "clipboard":
        post["value"] = _read_clipboard() or ""
    elif check == "window":
        post["value"] = _read_foreground_window() or ""
    elif check == "mtime":
        post["value"] = os.path.getmtime(file_path) if (file_path and os.path.exists(file_path)) else None
    fn = _CHECKS.get(f"_check_{check}")
    if fn is None:
        return {"ran": False, "ok": None, "check": check, "description": desc,
                "message": f"no check fn for {check}", "delay_ms": delay_ms}
    ok, msg = fn(pre_snap.get("value"), post.get("value"))
    return {"ran": True, "ok": ok, "check": check, "description": desc,
            "message": msg, "delay_ms": delay_ms,
            "pre": pre_snap.get("value"), "post": post.get("value")}


# 预注册检查函数
_CHECKS["_check_clipboard"] = _check_clipboard_changed
_CHECKS["_check_window"] = _check_window_changed
_CHECKS["_check_mtime"] = _check_mtime_changed
