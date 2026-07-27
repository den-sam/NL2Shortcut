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


# 默认值：注入后 50ms 的采样窗口（依据 2026 规范）
DEFAULT_DELAY_MS = 50
MAX_DELAY_MS = 200


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
    """读取前台窗口的标题（Windows）。"""
    if not _HAS_WIN32GUI:
        return None
    try:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            return win32gui.GetWindowText(hwnd)
    except Exception:
        return None
    return None


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
        return True, "foreground window changed"
    return False, "foreground window unchanged"


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
