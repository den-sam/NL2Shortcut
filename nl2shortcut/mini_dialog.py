"""NL2Shortcut 迷你对话窗 — 全局热键唤起的浮动命令面板。

按下 Ctrl+Alt+M 从任意应用程序唤出一个置顶的迷你对话窗：
  - 自动检测并显示当前前台窗口
  - 「跳转」按钮：切换到该窗口（隐藏对话窗）
  - 输入自然语言意图 → Enter 执行快捷键 → 自动隐藏
  - Esc 隐藏不执行

设计理念：
  不需要切回 NL2Shortcut GUI，在任何应用里都能随时用一句话执行快捷键。
  "跳转到当前页面" 意味着对话窗知道当前活跃窗口是谁，
  并能在执行后把焦点还给那个窗口。

用法：
    nl2shortcut mini                        # 独立运行（系统托盘 + 全局热键）
    nl2shortcut mini --no-tray              # 不显示托盘图标
    nl2shortcut mini --hotkey ctrl+alt+m    # 自定义热键

作为模块嵌入 GUI：
    from nl2shortcut.mini_dialog import launch_mini_dialog
    launch_mini_dialog()  # 在后台进程中启动
"""

import sys
import subprocess
import ctypes
import threading
import time
from ctypes import wintypes, WINFUNCTYPE
from pathlib import Path

from PyQt5.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QApplication, QWidget, QLineEdit, QLabel,
    QSystemTrayIcon, QMenu, QAction,
    QGraphicsDropShadowEffect, QMessageBox,
    QScrollArea, QFrame, QSizePolicy,
)
from PyQt5.QtCore import (
    Qt, QTimer, QRect, pyqtSignal, QObject,
    QPropertyAnimation, QEasingCurve, QSize,
)
from PyQt5.QtGui import (
    QFont, QColor, QIcon, QPainter, QBrush,
    QPen, QPainterPath, QCursor,
)

# ═══════════════════════════════════════════════════════════════════════
# Design Tokens
# ═══════════════════════════════════════════════════════════════════════

DIALOG_W = 420
DIALOG_H = 380
DIALOG_RADIUS = 14
SHADOW_BLUR = 32
SHADOW_OFFSET = (0, 10)

BG_BASE = "#FAFBFC"
BG_CARD = "#FFFFFF"
BG_INPUT = "#F0F2F5"
TEXT_MAIN = "#1A1A2E"
TEXT_DIM = "#6B7280"
TEXT_MUTED = "#9CA3AF"
ACCENT = "#6366F1"
ACCENT_H = "#4F46E5"
ACCENT_P = "#4338CA"
SUCCESS = "#10B981"
DANGER = "#EF4444"
BORDER = "#E5E7EB"
BORDER_FOCUS = "#6366F1"

DEFAULT_HOTKEY = "ctrl+alt+m"

# ═══════════════════════════════════════════════════════════════════════
# Hotkey Parser (same as overlay.py)
# ═══════════════════════════════════════════════════════════════════════

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

_HOTKEY_MOD_MAP = {
    "alt": MOD_ALT, "<alt>": MOD_ALT,
    "ctrl": MOD_CONTROL, "<ctrl>": MOD_CONTROL,
    "shift": MOD_SHIFT, "<shift>": MOD_SHIFT,
    "win": MOD_WIN, "<win>": MOD_WIN,
    "cmd": MOD_WIN, "<cmd>": MOD_WIN,
}
_HOTKEY_VK_MAP = {}
for _ch in "abcdefghijklmnopqrstuvwxyz0123456789":
    _HOTKEY_VK_MAP[_ch] = ord(_ch.upper())
for _name, _vk in [
    ("space", 0x20), ("tab", 0x09), ("enter", 0x0D), ("esc", 0x1B),
    ("escape", 0x1B), ("backspace", 0x08), ("delete", 0x2E),
    ("f1", 0x70), ("f2", 0x71), ("f3", 0x72), ("f4", 0x73),
    ("f5", 0x74), ("f6", 0x75), ("f7", 0x76), ("f8", 0x77),
    ("f9", 0x78), ("f10", 0x79), ("f11", 0x7A), ("f12", 0x7B),
    ("left", 0x25), ("right", 0x27), ("up", 0x26), ("down", 0x28),
    ("home", 0x24), ("end", 0x23), ("pageup", 0x21), ("pagedown", 0x22),
    ("insert", 0x2D), ("printscreen", 0x2C), ("pause", 0x13),
    (",", 0xBC), (".", 0xBE), ("/", 0xBF),
]:
    _HOTKEY_VK_MAP[_name] = _vk


def _parse_hotkey(combo: str):
    parts = [p.strip().lower() for p in combo.split("+")]
    mods = 0
    vk = None
    for p in parts:
        if p in _HOTKEY_MOD_MAP:
            mods |= _HOTKEY_MOD_MAP[p]
        elif p in _HOTKEY_VK_MAP:
            vk = _HOTKEY_VK_MAP[p]
        elif len(p) == 1:
            vk = ord(p.upper())
        else:
            raise ValueError(f"Unknown hotkey component: '{p}'")
    if vk is None:
        raise ValueError(f"No key in hotkey combo: '{combo}'")
    return mods, vk


# ═══════════════════════════════════════════════════════════════════════
# HotkeyWorker — Win32 message-only window (same pattern as overlay.py)
# ═══════════════════════════════════════════════════════════════════════

_ctypes_user32 = ctypes.windll.user32
_ctypes_user32.DefWindowProcW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)
_ctypes_user32.DefWindowProcW.restype = wintypes.LPARAM


class HotkeyWorker(QObject):
    activated = pyqtSignal()

    _WM_HOTKEY = 0x0312
    _HWND_MESSAGE = wintypes.HWND(-3)

    def __init__(self, hotkey_combo: str = DEFAULT_HOTKEY, parent=None):
        super().__init__(parent)
        self._hotkey_combo = hotkey_combo
        self._mods, self._vk = _parse_hotkey(hotkey_combo)
        self._hotkey_id = 1
        self._registered = False
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="nl2shortcut-mini-hotkey"
        )
        self._thread.start()
        time.sleep(0.1)
        return True

    def stop(self):
        self._stop_event.set()
        self._registered = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self):
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"MniDlgHK_{id(self):X}"

        activated = self.activated
        hotkey_id = self._hotkey_id
        WM_HOTKEY = self._WM_HOTKEY

        @WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                      wintypes.WPARAM, wintypes.LPARAM)
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_HOTKEY and wparam == hotkey_id:
                activated.emit()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        WNDPROC_T = WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC_T),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]
        wc = WNDCLASSW()
        wc.lpfnWndProc = wnd_proc
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, "MiniDialog Hotkey", 0, 0, 0, 0, 0,
            self._HWND_MESSAGE, None, hinstance, None,
        )
        if not hwnd:
            user32.UnregisterClassW(class_name, hinstance)
            return

        ok = user32.RegisterHotKey(hwnd, hotkey_id, self._mods, self._vk)
        if not ok:
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, hinstance)
            return
        self._registered = True

        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._registered:
            user32.UnregisterHotKey(hwnd, hotkey_id)
            self._registered = False
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, hinstance)


# ═══════════════════════════════════════════════════════════════════════
# Execution worker — runs smart_execute in background
# ═══════════════════════════════════════════════════════════════════════

class ExecuteWorker(QObject):
    finished = pyqtSignal(object)  # Dict result

    def __init__(self, intent: str, dry_run: bool = False, parent=None):
        super().__init__(parent)
        self._intent = intent
        self._dry_run = dry_run

    def run(self):
        try:
            from .master import KeyboardMasterAgent
            master = KeyboardMasterAgent()
            r = master.smart_execute(self._intent, dry_run=self._dry_run, timeout=15, learn=True)
            self.finished.emit(r)
        except Exception as e:
            self.finished.emit({"ok": False, "pipeline": "error",
                                "intent": self._intent, "error": str(e)})


# ═══════════════════════════════════════════════════════════════════════
# Mini Dialog Window
# ═══════════════════════════════════════════════════════════════════════

class MiniDialog(QWidget):
    """Frameless, always-on-top mini chat dialog.

    Shows current foreground window info, accepts NL intents,
    executes shortcuts, and auto-hides after execution.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._history: list[tuple[str, str, bool]] = []  # (intent, result, ok)
        self._worker: ExecuteWorker | None = None
        self._worker_thread: threading.Thread | None = None
        self._setup_ui()
        self._setup_animations()
        self._position_near_cursor()

    # ── UI setup ──────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle("NL2Shortcut Mini")
        self.setFixedSize(DIALOG_W, DIALOG_H)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # ── Root container ──
        root = QWidget(self)
        root.setObjectName("miniRoot")
        root.setGeometry(0, 0, DIALOG_W, DIALOG_H)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 12, 16, 12)
        root_layout.setSpacing(8)

        # ── Title bar ──
        title_row = QHBoxLayout()
        title_icon = QLabel("\u26a1")
        title_icon.setFont(QFont("Segoe UI", 18))
        title_icon.setFixedWidth(32)
        title_icon.setStyleSheet(f"color: {ACCENT}; background: transparent; border: none;")
        title_row.addWidget(title_icon)

        title_label = QLabel("NL2Shortcut Mini")
        title_label.setFont(QFont("Microsoft YaHei", 13, QFont.Bold))
        title_label.setStyleSheet(f"color: {TEXT_MAIN}; background: transparent; border: none;")
        title_row.addWidget(title_label)

        title_row.addStretch()

        # Close button
        close_btn = QPushButton("\u2715")
        close_btn.setFixedSize(28, 28)
        close_btn.setFont(QFont("Segoe UI", 12))
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {TEXT_DIM};
                border: none;
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background: #F3F4F6;
                color: {TEXT_MAIN};
            }}
        """)
        close_btn.clicked.connect(self.hide_dialog)
        title_row.addWidget(close_btn)
        root_layout.addLayout(title_row)

        # ── Active window card ──
        self._window_card = QFrame()
        self._window_card.setStyleSheet(f"""
            QFrame {{
                background: {BG_INPUT};
                border: 1px solid {BORDER};
                border-radius: 10px;
            }}
        """)
        card_layout = QVBoxLayout(self._window_card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)

        # Header row
        win_header = QHBoxLayout()
        win_icon = QLabel("\U0001f5a5")
        win_icon.setFont(QFont("Segoe UI", 14))
        win_icon.setFixedWidth(24)
        win_icon.setStyleSheet("background: transparent; border: none;")
        win_header.addWidget(win_icon)

        self._win_title_label = QLabel("检测中...")
        self._win_title_label.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        self._win_title_label.setStyleSheet(f"color: {TEXT_MAIN}; background: transparent; border: none;")
        self._win_title_label.setWordWrap(True)
        win_header.addWidget(self._win_title_label, stretch=1)
        card_layout.addLayout(win_header)

        # Process info row
        info_row = QHBoxLayout()
        self._win_proc_label = QLabel("")
        self._win_proc_label.setFont(QFont("Consolas", 10))
        self._win_proc_label.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent; border: none;")
        info_row.addWidget(self._win_proc_label)
        info_row.addStretch()

        # Jump button
        jump_btn = QPushButton("\u2197 跳转到此窗口")
        jump_btn.setFont(QFont("Microsoft YaHei", 10))
        jump_btn.setCursor(Qt.PointingHandCursor)
        jump_btn.setFixedHeight(28)
        jump_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 4px 14px;
            }}
            QPushButton:hover {{
                background: {ACCENT_H};
            }}
            QPushButton:pressed {{
                background: {ACCENT_P};
            }}
        """)
        jump_btn.clicked.connect(self._on_jump)
        info_row.addWidget(jump_btn)
        card_layout.addLayout(info_row)

        root_layout.addWidget(self._window_card)

        # ── Input row ──
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self._input = QLineEdit()
        self._input.setPlaceholderText("输入意图，如：复制 / Git 提交并推送...")
        self._input.setFont(QFont("Microsoft YaHei", 12))
        self._input.setMinimumHeight(42)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_INPUT};
                color: {TEXT_MAIN};
                border: 1.5px solid {BORDER};
                border-radius: 10px;
                padding: 8px 14px;
                selection-background-color: #C7D2FE;
            }}
            QLineEdit:focus {{
                border-color: {BORDER_FOCUS};
            }}
        """)
        self._input.returnPressed.connect(self._on_execute)
        input_row.addWidget(self._input, stretch=1)

        send_btn = QPushButton("\u25b6")
        send_btn.setFixedSize(42, 42)
        send_btn.setFont(QFont("Segoe UI", 14))
        send_btn.setCursor(Qt.PointingHandCursor)
        send_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT};
                color: white;
                border: none;
                border-radius: 10px;
            }}
            QPushButton:hover {{
                background: {ACCENT_H};
            }}
            QPushButton:pressed {{
                background: {ACCENT_P};
            }}
            QPushButton:disabled {{
                background: #D1D5DB;
                color: #9CA3AF;
            }}
        """)
        send_btn.clicked.connect(self._on_execute)
        self._send_btn = send_btn
        input_row.addWidget(send_btn)
        root_layout.addLayout(input_row)

        # ── History area ──
        history_header = QLabel("执行历史")
        history_header.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        history_header.setStyleSheet(f"color: {TEXT_DIM}; background: transparent; border: none; padding-top: 4px;")
        root_layout.addWidget(history_header)

        self._history_list = QTextEdit()
        self._history_list.setReadOnly(True)
        self._history_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._history_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._history_list.setStyleSheet(f"""
            QTextEdit {{
                background: {BG_CARD};
                color: {TEXT_MAIN};
                border: 1px solid {BORDER};
                border-radius: 10px;
                padding: 6px 10px;
                font-size: 12px;
                font-family: 'Microsoft YaHei';
            }}
            QScrollBar:vertical {{
                width: 0px;
                background: transparent;
            }}
        """)
        root_layout.addWidget(self._history_list, stretch=1)

        # Status bar
        self._status_label = QLabel("")
        self._status_label.setFont(QFont("Microsoft YaHei", 9))
        self._status_label.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent; border: none;")
        root_layout.addWidget(self._status_label)

        # Drop shadow
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setColor(QColor(0, 0, 0, 60))
        shadow.setOffset(*SHADOW_OFFSET)
        self.setGraphicsEffect(shadow)

    def _setup_animations(self):
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(120)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)

    # ── Positioning ───────────────────────────────────────────────

    def _position_near_cursor(self):
        """Place dialog near the mouse cursor, clamped to screen bounds."""
        screen = QApplication.primaryScreen()
        if not screen:
            self.move(200, 200)
            return
        geo = screen.availableGeometry()
        cursor = QCursor.pos()
        x = cursor.x() - DIALOG_W // 2
        y = cursor.y() - DIALOG_H // 2

        # Clamp
        x = max(geo.left(), min(x, geo.right() - DIALOG_W))
        y = max(geo.top(), min(y, geo.bottom() - DIALOG_H))
        self.move(x, y)

    # ── Show / Hide ───────────────────────────────────────────────

    def show_dialog(self):
        """Show dialog: refresh window info, position, fade in."""
        self._refresh_window_info()
        self._position_near_cursor()
        self._input.clear()
        self._input.setFocus()
        self.show()
        self.raise_()
        self.activateWindow()
        try:
            self._fade_anim.start()
        except Exception:
            self.setWindowOpacity(1.0)

    def hide_dialog(self):
        self.hide()

    def toggle(self):
        if self.isVisible():
            self.hide_dialog()
        else:
            self.show_dialog()

    # ── Window info ───────────────────────────────────────────────

    def _refresh_window_info(self):
        """Detect current foreground window via native C++ DLL."""
        try:
            from .native_loader import native
            if native.available:
                info = native.foreground_window()
                if info:
                    title = info.get("title", "") or "(无标题)"
                    pname = info.get("process_name", "") or ""
                    if len(title) > 50:
                        title = title[:47] + "..."
                    self._win_title_label.setText(title)
                    self._win_proc_label.setText(pname)
                    return
        except Exception:
            pass
        self._win_title_label.setText("(检测失败)")
        self._win_proc_label.setText("")

    # ── Jump action ───────────────────────────────────────────────

    def _on_jump(self):
        """Jump to the current foreground window: hide dialog, focus that window."""
        self.hide_dialog()

    # ── Execute ───────────────────────────────────────────────────

    def _on_execute(self):
        text = self._input.text().strip()
        if not text:
            self.hide_dialog()
            return

        self._input.setEnabled(False)
        self._send_btn.setEnabled(False)
        self._input.clear()
        self._status_label.setText("\u23f3 正在识别意图...")
        self._status_label.repaint()

        self._worker = ExecuteWorker(text)
        self._worker.finished.connect(self._on_result)
        self._worker_thread = threading.Thread(
            target=self._worker.run, daemon=True
        )
        self._worker_thread.start()

    def _on_result(self, result):
        self._input.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._input.setFocus()

        ok = result.get("ok", False)
        intent = result.get("intent", "")
        pipeline = result.get("pipeline", "error")
        steps = result.get("steps_executed", 0)
        error = result.get("error", "")

        # Build a readable result string
        if ok:
            if steps == 1:
                r = result.get("results", [{}])[0] if result.get("results") else {}
                output = r.get("output", "") or "OK"
                brief = output
                full = f"\u2705 {intent} \u2192 {output}"
            else:
                brief = f"{steps} steps OK"
                full = f"\u2705 {intent} \u2192 {steps} 步完成 ({pipeline})"
        else:
            brief = f"FAIL: {error[:30]}" if error else "未识别"
            full = f"\u274c {intent} \u2192 {error or '无法识别'}"

        # Add to history
        self._history.append((intent, brief, ok))
        self._render_history(highlight_last=True)

        # Update status
        icon = "\u2714" if ok else "\u274c"
        self._status_label.setText(f"{icon} {brief}")

        # Auto-hide after 1.5s if successful, 3s if failed
        delay = 1500 if ok else 3000
        QTimer.singleShot(delay, self.hide_dialog)

    def _render_history(self, highlight_last=False):
        """Render history as simple HTML."""
        parts = []
        n = len(self._history)
        for i, (intent, brief, ok) in enumerate(self._history):
            is_last = highlight_last and i == n - 1
            icon = "\u2705" if ok else "\u274c"
            color = SUCCESS if ok else DANGER
            style = "font-weight: 600;" if is_last else ""
            parts.append(
                f'<span style="color:{color};{style}">{icon}</span> '
                f'<span style="color:{TEXT_MAIN};{style}">{intent}</span>'
                f'<span style="color:{TEXT_MUTED};font-size:10px;"> \u2192 {brief}</span>'
            )
        html = "<br>".join(parts) if parts else (
            f'<span style="color:{TEXT_MUTED};">暂无执行记录</span>'
        )
        self._history_list.setHtml(
            f'<div style="line-height:1.8;">{html}</div>'
        )

    # ── Paint: rounded rect ──

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(BG_BASE)))
        painter.drawRoundedRect(
            QRect(0, 0, self.width(), self.height()),
            DIALOG_RADIUS, DIALOG_RADIUS,
        )
        painter.end()

    # ── Keyboard ──

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide_dialog()
        else:
            super().keyPressEvent(event)


# ═══════════════════════════════════════════════════════════════════════
# Tray App / Entry Point
# ═══════════════════════════════════════════════════════════════════════

class MiniDialogApp(QObject):
    """System tray + global hotkey + mini dialog."""

    def __init__(self, show_tray: bool = True, hotkey: str = DEFAULT_HOTKEY):
        super().__init__()
        self._dialog = MiniDialog()
        self._hotkey_worker = HotkeyWorker(hotkey)
        self._hotkey_worker.activated.connect(self._on_hotkey)
        self._tray = None
        if show_tray:
            self._setup_tray()

    def _setup_tray(self):
        from PyQt5.QtGui import QPixmap
        self._tray = QSystemTrayIcon(self)
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(ACCENT))
        p.setPen(Qt.NoPen)
        p.drawEllipse(2, 2, 28, 28)
        p.setPen(QPen(QColor("white")))
        font = QFont("Segoe UI", 14, QFont.Bold)
        p.setFont(font)
        p.drawText(QRect(0, 0, 32, 32), Qt.AlignCenter, "M")
        p.end()
        self._tray.setIcon(QIcon(pixmap))
        self._tray.setToolTip(
            f"NL2Shortcut Mini — 迷你对话窗 (Ctrl+Alt+M)"
        )
        menu = QMenu()
        show_action = QAction("唤出迷你对话窗", menu)
        show_action.triggered.connect(self._dialog.show_dialog)
        menu.addAction(show_action)
        menu.addSeparator()
        gui_action = QAction("打开完整界面", menu)
        gui_action.triggered.connect(self._open_gui)
        menu.addAction(gui_action)
        menu.addSeparator()
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(
            lambda r: self._dialog.toggle()
            if r == QSystemTrayIcon.DoubleClick else None
        )
        self._tray.show()

    def _on_hotkey(self):
        self._dialog.toggle()

    def _open_gui(self):
        subprocess.Popen(
            [sys.executable, "-m", "nl2shortcut", "gui"],
            creationflags=0x00000008,
        )

    def _quit(self):
        self._hotkey_worker.stop()
        self._dialog.hide_dialog()
        if self._tray:
            self._tray.hide()
        QApplication.instance().quit()

    def run(self):
        self._hotkey_worker.start()
        QApplication.instance().setQuitOnLastWindowClosed(False)


def main(show_tray: bool = True, hotkey: str = DEFAULT_HOTKEY):
    qt_app = QApplication.instance()
    if qt_app is None:
        qt_app = QApplication(sys.argv)
        # Ensure QApplication quits when last window closed is False for tray
    qt_app.setApplicationName("NL2Shortcut Mini")
    qt_app.setQuitOnLastWindowClosed(False)
    app = MiniDialogApp(show_tray=show_tray, hotkey=hotkey)
    app.run()
    sys.exit(qt_app.exec_())


def launch_mini_dialog():
    """Launch mini dialog in a background process. Safe to call from GUI."""
    subprocess.Popen(
        [sys.executable, "-m", "nl2shortcut", "mini"],
        creationflags=0x00000008,
    )
