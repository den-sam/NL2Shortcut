"""应用上下文检测 —— 识别当前活动窗口与正在运行的应用。

平台支持：
  Windows  — ctypes + user32.dll（零依赖）
  macOS    — Quartz/CoreGraphics (AppKit)
  Linux    — xdotool 回退方案

应用指纹将进程名映射为友好名称：
  Code.exe → vscode
  chrome.exe → chrome
  WindowsTerminal.exe → terminal
"""

import sys
import subprocess
import re
from pathlib import Path
from typing import Optional

from .models import AppContext, Platform


# 已知应用指纹：(进程名小写, 友好名称)
_APP_FINGERPRINTS = [
    ("code", "vscode"),
    ("cursor", "vscode"),
    ("chrome", "chrome"),
    ("msedge", "edge"),
    ("firefox", "firefox"),
    ("windowsterminal", "terminal"),
    ("wezterm", "terminal"),
    ("alacritty", "terminal"),
    ("conhost", "terminal"),
    ("powershell", "terminal"),
    ("cmd", "terminal"),
    ("explorer", "explorer"),
    ("notepad", "notepad"),
    ("notepad++", "notepad++"),
    ("devenv", "visual_studio"),
    ("idea64", "intellij"),
    ("pycharm64", "pycharm"),
    ("webstorm64", "webstorm"),
    ("sublime_text", "sublime"),
    ("obsidian", "obsidian"),
    ("slack", "slack"),
    ("teams", "teams"),
    ("discord", "discord"),
    ("wechat", "wechat"),
    ("qq", "qq"),
    ("dingtalk", "dingtalk"),
    ("outlook", "outlook"),
    ("thunderbird", "thunderbird"),
    ("spotify", "spotify"),
    ("photoshop", "photoshop"),
    ("illustrator", "illustrator"),
    ("figma", "figma"),
    ("blender", "blender"),
    ("excel", "excel"),
    ("winword", "word"),
    ("powerpnt", "powerpoint"),
    ("acrord32", "acrobat"),
    ("foxit", "acrobat"),
    ("putty", "terminal"),
    ("mobaxterm", "terminal"),
]


def detect_context() -> AppContext:
    """检测当前活动窗口的上下文。"""
    platform = Platform.detect()
    if platform == Platform.WINDOWS:
        return _detect_windows()
    elif platform == Platform.MACOS:
        return _detect_macos()
    return _detect_linux()


def _detect_windows() -> AppContext:
    """在 Windows 上使用 ctypes 检测活动窗口。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return AppContext()

    # 获取窗口标题
    length = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buf, length + 1)
    title = title_buf.value or ""

    # 获取进程 ID
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    # 获取进程名称
    process_name = _get_process_name(pid.value)
    app_name = _fingerprint(process_name)

    return AppContext(
        window_title=title,
        process_name=process_name,
        app_name=app_name,
        platform="windows",
        parsed_file_path=parse_window_title(title, app_name),
    )


def _detect_macos() -> AppContext:
    """在 macOS 上检测活动窗口。"""
    try:
        import subprocess
        script = """
        tell application "System Events"
            set frontApp to name of first application process whose frontmost is true
        end tell
        return frontApp
        """
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        app_name = result.stdout.strip()
        title = ""
        try:
            title_result = subprocess.run(
                ["osascript", "-e",
                 'tell app "System Events" to get title of front window of '
                 f'(process "{app_name}")'],
                capture_output=True, text=True, timeout=2,
            )
            title = title_result.stdout.strip()
        except Exception:
            pass

        return AppContext(
            window_title=title,
            process_name=app_name,
            app_name=_fingerprint(app_name),
            platform="macos",
            parsed_file_path=parse_window_title(title, _fingerprint(app_name)),
        )
    except Exception:
        return AppContext(platform="macos")


def _detect_linux() -> AppContext:
    """在 Linux 上使用 xdotool 检测活动窗口。"""
    try:
        result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True, text=True, timeout=3,
        )
        title = result.stdout.strip()

        pid_result = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowpid"],
            capture_output=True, text=True, timeout=3,
        )
        pid = pid_result.stdout.strip()
        process_name = _get_process_name(int(pid)) if pid else ""

        return AppContext(
            window_title=title,
            process_name=process_name,
            app_name=_fingerprint(process_name),
            platform="linux",
            parsed_file_path=parse_window_title(title, _fingerprint(process_name)),
        )
    except Exception:
        return AppContext(platform="linux")


def _get_process_name(pid: int) -> str:
    """根据进程 ID 获取进程名称。"""
    try:
        import psutil
        return psutil.Process(pid).name()
    except ImportError:
        pass
    except Exception:
        pass

    # 回退方案：在 Windows 上使用 tasklist
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            parts = result.stdout.strip().strip('"').split('","')
            if len(parts) >= 1:
                return parts[0]
        except Exception:
            pass

    return f"pid:{pid}"


def _fingerprint(process_name: str) -> str:
    """将进程名映射为友好的应用名称。"""
    if not process_name:
        return "unknown"
    name_lower = Path(process_name).stem.lower()
    for keyword, friendly in _APP_FINGERPRINTS:
        if keyword in name_lower:
            return friendly
    return name_lower


# ── 窗口标题解析 ─────────────────────────────────────────────────────

# 用于从窗口标题中提取文件路径的正则模式。
# 格式：(正则表达式, 文件名的分组索引)
_TITLE_PATTERNS = [
    # VS Code / Cursor："main.py - NL2Shortcut - Visual Studio Code"
    (re.compile(r'^(.+?)\s*[-–—]\s*.+\s*[-–—]\s*(?:Visual Studio Code|VS Code|Code|Cursor)$'), 1),
    # 记事本："filename.txt - Notepad"
    (re.compile(r'^(.+?)\s*[-–—]\s*Notepad$'), 1),
    # IntelliJ / PyCharm："MyClass.java - [Project] - IntelliJ IDEA"
    (re.compile(r'^(.+?)\s*[-–—]\s*\[.+\]\s*[-–—]\s*\S.*$'), 1),
    # Sublime："file.js (Project) - Sublime Text"
    (re.compile(r'^(.+?)\s*\(.+\)\s*[-–—]\s*\S.*$'), 1),
    # 通用格式 "file.ext - AppName"（必须放在最后，避免误匹配）
    (re.compile(r'^(.+?\.\w{1,10})\s*[-–—]\s*\S.*$'), 1),
]


def parse_window_title(title: str, app_name: str = "") -> Optional[str]:
    """从常见的窗口标题格式中提取可能的文件路径。

    仅返回文件名（例如 'main.py'），若没有模式匹配则返回 None。
    """
    if not title:
        return None
    for pattern, group_idx in _TITLE_PATTERNS:
        m = pattern.match(title.strip())
        if m:
            return m.group(group_idx).strip()
    return None
