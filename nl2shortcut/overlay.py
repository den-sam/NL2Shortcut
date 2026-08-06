"""NL2Shortcut 的全局热键 + 浮动输入覆盖层。

按下 Alt+Shift+S（可配置）即可从任意应用程序唤起一个浮动输入栏。
输入你的意图，按 Enter，NL2Shortcut 便会把对应快捷键注入之前聚焦的窗口。

架构：
  - 系统托盘图标让 NL2Shortcut 在后台持续运行
  - 在守护线程中使用 Win32 仅消息窗口来监听全局热键
    （QAbstractNativeEventFilter 在 PyQt5 中无法拦截 WM_HOTKEY）
  - pyqtSignal 将 Win32 线程桥接到 Qt 主线程
  - FloatingInputBar（无边框、置顶的 QWidget）弹出显示
  - 按 Enter：识别意图 -> 发送按键 -> 隐藏输入栏
  - 按 Esc：隐藏输入栏（不执行任何操作）

用法：
    nl2shortcut overlay              # 启动系统托盘 + 全局热键
    nl2shortcut overlay --no-tray    # 仅全局热键（无托盘图标）

作为模块使用：
    from nl2shortcut.overlay import main
    main()
"""

import sys
import subprocess
import ctypes
import threading
from ctypes import wintypes, WINFUNCTYPE
from pathlib import Path

from PyQt5.QtWidgets import (
    QVBoxLayout, QTextEdit, QPushButton,
    QApplication, QWidget, QHBoxLayout,
    QLineEdit, QLabel, QSystemTrayIcon, QMenu,
    QAction, QGraphicsDropShadowEffect, QMessageBox,
)
from PyQt5.QtCore import (
    Qt, QTimer, QRect, pyqtSignal, QObject,
    QPropertyAnimation, QEasingCurve,
)
from PyQt5.QtGui import (
    QFont, QColor, QIcon, QPainter, QBrush,
    QPen,
)

from .agent import ShortcutAgent

# ═══════════════════════════════════════════════════════════════════════
# Design constants
# ═══════════════════════════════════════════════════════════════════════

BAR_W = 480
BAR_H = 72
BAR_RADIUS = 12
SHADOW_BLUR = 30
SHADOW_OFFSET = (0, 8)

BG_GLASS = "#1A1A2ECC"
BG_INPUT = "#2D2D44"
TEXT_MAIN = "#EAEAEA"
TEXT_DIM = "#8888AA"
ACCENT = "#6C63FF"

DEFAULT_HOTKEY = "ctrl+alt+s"
DEFAULT_CLIPBOARD_HOTKEY = "ctrl+alt+v"

# Clipboard mode accent
ACCENT_CLIP = "#10B981"  # green for clipboard/AI mode

# ═══════════════════════════════════════════════════════════════════════
# Hotkey parser
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
]:
    _HOTKEY_VK_MAP[_name] = _vk


def _parse_hotkey(combo: str):
    """Parse 'alt+shift+s' or '<alt>+<shift>+s' -> (mod_flags, vk_code)."""
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
# Global hotkey via independent Win32 message-only window (background thread)
#
# Why: PyQt5's QAbstractNativeEventFilter does NOT forward WM_HOTKEY.
#      Qt's Windows platform plugin swallows hotkey messages internally.
# Solution: Create a Win32 HWND_MESSAGE (message-only) window in a
#      daemon thread with its own GetMessage pump. RegisterHotKey
#      targets THAT window, so Qt never sees the message.
#      pyqtSignal.emit() bridges to the Qt main thread (thread-safe).
# ═══════════════════════════════════════════════════════════════════════

# Fix DefWindowProcW argtypes early (before any Win32 API calls)
_ctypes_user32 = ctypes.windll.user32
_ctypes_user32.DefWindowProcW.argtypes = (
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)
_ctypes_user32.DefWindowProcW.restype = wintypes.LPARAM

class HotkeyWorker(QObject):
    """Global hotkey via independent Win32 message-only window thread."""

    activated = pyqtSignal()

    # Windows constants
    _WM_HOTKEY = 0x0312
    _HWND_MESSAGE = wintypes.HWND(-3)

    def __init__(self, hotkey_combo: str = DEFAULT_HOTKEY,
                 hotkey_id: int = 1, parent=None):
        super().__init__(parent)
        self._hotkey_combo = hotkey_combo
        self._mods, self._vk = _parse_hotkey(hotkey_combo)
        # 每个实例用独立 ID，避免多 worker 同时注册时冲突
        self._hotkey_id = hotkey_id
        self._registered = False
        self._thread = None
        self._stop_event = threading.Event()

    def start(self):
        """Start background thread with Win32 message-only window + hotkey."""
        if self._thread and self._thread.is_alive():
            return True

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="nl2shortcut-hotkey"
        )
        self._thread.start()
        # Wait briefly for thread to initialize
        import time
        time.sleep(0.1)
        return True

    def stop(self):
        """Signal the background thread to exit and wait."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            # Unregister hotkey first (releases GetMessage block)
            self._registered = False
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self):
        """Background thread: create message-only window, register hotkey,
        run GetMessage loop."""
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"SCutHK_{id(self):X}"

        # Define window procedure
        activated = self.activated
        hotkey_id = self._hotkey_id
        WM_HOTKEY = self._WM_HOTKEY

        @WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                      wintypes.WPARAM, wintypes.LPARAM)
        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_HOTKEY and wparam == hotkey_id:
                # Thread-safe signal emission to Qt main thread
                activated.emit()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        # Register window class (plain ctypes struct)
        # wnd_proc's WINFUNCTYPE must match the struct field type exactly
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
            print(f"[nl2shortcut overlay] 热键窗口类注册失败 (err {kernel32.GetLastError()})",
                  file=sys.stderr)
            return

        # Create message-only window
        hwnd = user32.CreateWindowExW(
            0, class_name, "NL2Shortcut Hotkey", 0, 0, 0, 0, 0,
            self._HWND_MESSAGE, None, hinstance, None,
        )
        if not hwnd:
            print(f"[nl2shortcut overlay] 热键窗口创建失败 (err {kernel32.GetLastError()})",
                  file=sys.stderr)
            user32.UnregisterClassW(class_name, hinstance)
            return

        # Register hotkey with this window
        ok = user32.RegisterHotKey(hwnd, hotkey_id, self._mods, self._vk)
        if not ok:
            err = kernel32.GetLastError()
            msgs = {1409: "热键已被占用", 5: "权限不足(需管理员)"}
            msg = msgs.get(err, f"错误码 {err}")
            print(f"[nl2shortcut overlay] 全局热键注册失败: {msg} ({self._hotkey_combo})",
                  file=sys.stderr)
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, hinstance)
            return

        self._registered = True

        # Message loop
        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup
        if self._registered:
            user32.UnregisterHotKey(hwnd, hotkey_id)
            self._registered = False
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, hinstance)


# ═══════════════════════════════════════════════════════════════════════
# Floating input bar
# ═══════════════════════════════════════════════════════════════════════

class FloatingInputBar(QWidget):
    """Frameless, always-on-top, centered input popup.

    Supports two modes:
      - Normal mode: NL → shortcut execution
      - Clipboard mode: selected text + NL instruction → AI processing → paste back
    """

    executed = pyqtSignal(str, str)
    BAR_H_NORMAL = 72
    BAR_H_CLIPBOARD = 170  # taller with preview area

    def __init__(self, agent: ShortcutAgent, parent=None):
        super().__init__(parent)
        self._agent = agent
        self._dry_run = False
        self._clipboard_mode = False
        self._clipboard_text = ""
        self._setup_ui()
        self._setup_animations()
        self._center_on_screen()

    def _setup_ui(self):
        self.setWindowTitle("NL2Shortcut")
        self.setFixedSize(BAR_W, BAR_H)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        # ── Root: vertical layout (clipboard preview + input row) ──
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Clipboard preview section (hidden in normal mode) ──
        self._clip_preview_container = QWidget()
        self._clip_preview_container.setStyleSheet(
            f"background: {BG_INPUT}; border-radius: {BAR_RADIUS}px; "
            f"border-bottom-left-radius: 0px; border-bottom-right-radius: 0px; "
            f"border: 1.5px solid #3D3D5C; border-bottom: none;"
        )
        cp_layout = QVBoxLayout(self._clip_preview_container)
        cp_layout.setContentsMargins(16, 10, 16, 6)
        cp_layout.setSpacing(4)

        self._clip_mode_label = QLabel("\U0001f4cb 剪贴板处理模式")
        self._clip_mode_label.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
        self._clip_mode_label.setStyleSheet(
            f"color: {ACCENT_CLIP}; background: transparent; border: none;"
        )
        cp_layout.addWidget(self._clip_mode_label)

        self._clip_preview = QTextEdit()
        self._clip_preview.setReadOnly(True)
        self._clip_preview.setMaximumHeight(55)
        self._clip_preview.setFont(QFont("Consolas", 9))
        self._clip_preview.setStyleSheet(f"""
            QTextEdit {{
                background: {BG_GLASS};
                color: {TEXT_DIM};
                border: none;
                border-radius: 6px;
                padding: 4px 6px;
            }}
        """)
        self._clip_preview.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        cp_layout.addWidget(self._clip_preview)

        self._clip_preview_container.setVisible(False)
        root.addWidget(self._clip_preview_container)

        # ── Input row: icon | input | cancel | ok ──
        input_row = QWidget()
        input_row.setStyleSheet("background: transparent;")
        layout = QHBoxLayout(input_row)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self._icon_label = QLabel("\u26a1")
        self._icon_label.setFont(QFont("Segoe UI", 20))
        self._icon_label.setFixedWidth(36)
        self._icon_label.setAlignment(Qt.AlignCenter)
        self._icon_label.setStyleSheet("color: #6C63FF; background: transparent;")
        layout.addWidget(self._icon_label)

        self._input = QLineEdit()
        self._input.setPlaceholderText("描述你想做的事\u2026  (Esc 取消)")
        self._input.setFont(QFont("Microsoft YaHei", 13))
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_INPUT};
                border: 1.5px solid #3D3D5C;
                border-radius: 8px;
                padding: 10px 14px;
                color: {TEXT_MAIN};
                selection-background-color: {ACCENT}40;
            }}
            QLineEdit:focus {{
                border-color: {ACCENT};
            }}
        """)
        self._input.returnPressed.connect(self._on_execute)
        layout.addWidget(self._input, stretch=1)

        # ── Cancel button ──
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setFont(QFont("Microsoft YaHei", 11))
        self._cancel_btn.setFixedSize(56, 34)
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: #3D3D5C;
                color: {TEXT_DIM};
                border: none;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: #4D4D6C;
                color: {TEXT_MAIN};
            }}
            QPushButton:pressed {{
                background: #5D5D7C;
            }}
        """)
        self._cancel_btn.clicked.connect(self.hide_bar)
        layout.addWidget(self._cancel_btn)

        # ── OK / Execute button ──
        self._ok_btn = QPushButton("确定")
        self._ok_btn.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        self._ok_btn.setFixedSize(56, 34)
        self._ok_btn.setCursor(Qt.PointingHandCursor)
        self._ok_btn.setDefault(True)
        self._ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT};
                color: white;
                border: none;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: {ACCENT_H};
            }}
            QPushButton:pressed {{
                background: {ACCENT_P};
            }}
        """)
        self._ok_btn.clicked.connect(self._on_execute)
        layout.addWidget(self._ok_btn)

        root.addWidget(input_row)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(SHADOW_BLUR)
        shadow.setColor(QColor(0, 0, 0, 120))
        shadow.setOffset(*SHADOW_OFFSET)
        self.setGraphicsEffect(shadow)

        self._input.textChanged.connect(self._on_text_changed)

    def _setup_animations(self):
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self._fade_anim.setDuration(150)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)

    def _center_on_screen(self):
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.center().x() - BAR_W // 2
            y = int(geo.top() + geo.height() * 0.28)
        else:
            x, y = 400, 200
        self.move(x, y)

    def show_bar(self, clipboard_text: str = ""):
        self._clipboard_mode = bool(clipboard_text)
        self._clipboard_text = clipboard_text

        if self._clipboard_mode:
            # Switch to clipboard mode UI
            self._enter_clipboard_mode()
        else:
            self._exit_clipboard_mode()

        self._input.clear()
        self._status_label.setText("")
        self._center_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        self._input.setFocus()
        self._fade_anim.start()

    def hide_bar(self):
        self.hide()
        self._exit_clipboard_mode()

    def toggle(self):
        if self.isVisible():
            self.hide_bar()
        else:
            self.show_bar()

    def _on_text_changed(self, text: str):
        if len(text) < 1:
            self._status_label.setText("")
            return
        try:
            result = self._agent._intent.recognize(text)
            if result.confidence >= 0.6 and result.command:
                shortcut = self._agent._db.get_by_command(result.command)
                if shortcut:
                    from .models import Platform
                    key = shortcut.get_key(Platform.detect())
                    self._status_label.setText(f"\u2192 {key}")
                    return
        except Exception:
            pass
        self._status_label.setText("")

    def _on_execute(self):
        text = self._input.text().strip()
        if not text:
            self.hide_bar()
            return

        # ── Clipboard mode: LLM processing path ──
        if self._clipboard_mode and self._clipboard_text:
            self._execute_clipboard(text)
            return

        # ── Normal mode: shortcut execution ──
        self._status_label.setText("\u23f3")
        self._status_label.repaint()
        try:
            result = self._agent.execute(text, dry_run=self._dry_run)
        except Exception as e:
            self._status_label.setText(f"\u274c {e}")
            QTimer.singleShot(2000, self.hide_bar)
            return
        if result.success and result.key_combination:
            self._status_label.setText(f"\u2713 {result.key_combination}")
            self.executed.emit(result.command or text, result.key_combination)
        else:
            self._status_label.setText(f"\u274c {result.error or '未识别'}")
        QTimer.singleShot(1200, self.hide_bar)

    # ── Clipboard mode helpers ─────────────────────────────────────

    def _enter_clipboard_mode(self):
        """Switch to clipboard-mode UI: preview area + green accent."""
        self._clipboard_mode = True
        self._input.setPlaceholderText(
            "对选中内容做什么？"
            "翻译/总结/格式化…  (Esc 取消)"
        )
        self._icon_label.setText("\U0001f4cb")
        self._icon_label.setStyleSheet(
            f"color: {ACCENT_CLIP}; background: transparent;"
        )
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_INPUT};
                border: 1.5px solid #3D3D5C;
                border-radius: 8px;
                padding: 10px 14px;
                color: {TEXT_MAIN};
                selection-background-color: {ACCENT_CLIP}40;
            }}
            QLineEdit:focus {{
                border-color: {ACCENT_CLIP};
            }}
        """)
        self._status_label.setStyleSheet(
            f"color: {ACCENT_CLIP}; background: transparent; min-width: 80px;"
        )
        # Switch OK button to clipboard accent
        self._ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT_CLIP};
                color: white;
                border: none;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: #059669;
            }}
            QPushButton:pressed {{
                background: #047857;
            }}
        """)
        # Show preview
        preview = self._clipboard_text
        if len(preview) > 300:
            preview = preview[:300] + "…"
        self._clip_preview.setPlainText(preview)
        self._clip_preview_container.setVisible(True)
        self.setFixedSize(BAR_W, self.BAR_H_CLIPBOARD)

    def _exit_clipboard_mode(self):
        """Restore normal-mode UI."""
        self._clipboard_mode = False
        self._clipboard_text = ""
        self._input.setPlaceholderText(
            "描述你想做的事…  (Esc 取消)"
        )
        self._icon_label.setText("\u26a1")
        self._icon_label.setStyleSheet("color: #6C63FF; background: transparent;")
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: {BG_INPUT};
                border: 1.5px solid #3D3D5C;
                border-radius: 8px;
                padding: 10px 14px;
                color: {TEXT_MAIN};
                selection-background-color: {ACCENT}40;
            }}
            QLineEdit:focus {{
                border-color: {ACCENT};
            }}
        """)
        self._status_label.setStyleSheet(
            f"color: {TEXT_DIM}; background: transparent; min-width: 80px;"
        )
        # Restore normal OK button color
        self._ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT};
                color: white;
                border: none;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background: {ACCENT_H};
            }}
            QPushButton:pressed {{
                background: {ACCENT_P};
            }}
        """)
        self._clip_preview_container.setVisible(False)
        self.setFixedSize(BAR_W, BAR_H)

    def _execute_clipboard(self, instruction: str):
        """Execute clipboard-mode: LLM process → paste result."""
        self._status_label.setText("\u23f3 AI…")
        self._status_label.repaint()
        QApplication.processEvents()

        try:
            result = self._agent.process_clipboard(
                instruction=instruction,
                clipboard_text=self._clipboard_text,
            )
        except Exception as e:
            self._status_label.setText(f"\u274c {e}")
            QTimer.singleShot(2500, self.hide_bar)
            return

        if result is None:
            self._status_label.setText(
                "\u274c LLM 不可用（请配置 DEEPSEEK_API_KEY）"
            )
            QTimer.singleShot(2500, self.hide_bar)
            return

        if not result.strip():
            self._status_label.setText("\u274c 返回空内容")
            QTimer.singleShot(2000, self.hide_bar)
            return

        # Paste back to active window
        self._status_label.setText("\u2713 已写回")
        self._status_label.repaint()
        try:
            self._agent.paste_text(result)
        except Exception:
            pass

        self.executed.emit(instruction, f"AI: {result[:50]}…")
        QTimer.singleShot(1500, self.hide_bar)

    def paintEvent(self, event):
        from PyQt5.QtGui import QPainterPath
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRect(0, 0, self.width(), self.height()),
                            BAR_RADIUS, BAR_RADIUS)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(BG_GLASS)))
        painter.drawPath(path)
        painter.setPen(QPen(QColor("#3D3D5C"), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        painter.end()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide_bar()
        else:
            super().keyPressEvent(event)


# ═══════════════════════════════════════════════════════════════════════
# System Tray app
# ═══════════════════════════════════════════════════════════════════════

# 工作流热键绑定（Ctrl+Alt+1 ~ Ctrl+Alt+9）—— 一键触发前 9 个已保存的工作流
WORKFLOW_HOTKEYS = [f"ctrl+alt+{i}" for i in range(1, 10)]
# 录制器开关热键（Alt+Shift+R）
RECORDER_HOTKEY = "alt+shift+r"


class OverlayApp(QObject):
    """Main overlay: tray icon + global hotkey + floating bar."""

    def __init__(self, show_tray: bool = True, hotkey: str = DEFAULT_HOTKEY):
        super().__init__()
        self._agent = ShortcutAgent()
        self._bar = FloatingInputBar(self._agent)
        self._hotkey_worker = HotkeyWorker(hotkey, hotkey_id=1)
        self._hotkey_worker.activated.connect(self._on_hotkey_activated)
        # Second hotkey for clipboard trigger mode
        self._clip_hotkey_worker = HotkeyWorker(DEFAULT_CLIPBOARD_HOTKEY, hotkey_id=2)
        self._clip_hotkey_worker.activated.connect(self._on_clipboard_hotkey)

        # ── 工作流热键绑定（Ctrl+Alt+1~9） ──
        # 每个 worker 用独立 ID（3~11），触发时调对应工作流
        self._workflow_workers: list[HotkeyWorker] = []
        for idx, combo in enumerate(WORKFLOW_HOTKEYS):
            worker = HotkeyWorker(combo, hotkey_id=3 + idx)
            worker.activated.connect(lambda i=idx: self._run_workflow_by_index(i))
            self._workflow_workers.append(worker)

        # ── 录制器开关（Alt+Shift+R） ──
        self._recorder = None
        self._recorder_worker = HotkeyWorker(RECORDER_HOTKEY, hotkey_id=12)
        self._recorder_worker.activated.connect(self._toggle_recorder)

        self._tray = None
        if show_tray:
            self._setup_tray()

    # ── 工作流热键执行 ──────────────────────────────────────────────────

    def _run_workflow_by_index(self, idx: int) -> None:
        """Ctrl+Alt+1~9 触发已保存的第 idx 个工作流。"""
        try:
            wf_names = self._agent.workflow_engine.list_workflows()
            if not wf_names:
                self._show_tray_msg("暂无已保存的自动流程",
                                     "请先录制或在工作流目录添加 YAML 文件")
                return
            sorted_names = sorted(wf_names)
            if idx >= len(sorted_names):
                self._show_tray_msg(
                    f"没有第 {idx+1} 个工作流",
                    f"目前只有 {len(sorted_names)} 个：{', '.join(sorted_names)}")
                return
            name = sorted_names[idx]
            self._show_tray_msg(f"▶ 执行工作流: {name}", "正在执行…")
            # 在独立线程跑，避免阻塞 Qt 主线程
            threading.Thread(
                target=self._exec_workflow_thread, args=(name,), daemon=True
            ).start()
        except Exception as e:
            self._show_tray_msg("工作流执行失败", str(e))

    def _exec_workflow_thread(self, name: str) -> None:
        try:
            result = self._agent.workflow_engine.run(name)
            ok = result.success
            msg = (f"{'✅ 成功' if ok else '❌ 失败'}：{name}\n"
                   f"步骤: {len(result.steps)}  耗时: {result.elapsed_ms:.0f}ms")
            if result.error:
                msg += f"\n错误: {result.error}"
            self._show_tray_msg("工作流执行完成", msg)
        except Exception as e:
            self._show_tray_msg("工作流执行异常", str(e))

    # ── 录制器开关 ──────────────────────────────────────────────────────

    def _toggle_recorder(self) -> None:
        """Alt+Shift+R 切换录制状态。"""
        try:
            from .recorder import Recorder
        except ImportError:
            self._show_tray_msg("录制器不可用", "recorder.py 模块加载失败")
            return

        if self._recorder is None:
            # 首次启动录制器（不注册自己的切换热键，由本 overlay 托管）
            self._recorder = Recorder(memory=self._agent.operation_memory)
            self._recorder.run(with_toggle_hotkey=False)
            self._show_tray_msg("录制器已启动",
                                 "按 Alt+Shift+R 开始录制\n再做一遍操作\n再按一次结束并保存")
        else:
            # 录制器已在跑，按一次切换录制状态
            if self._recorder.is_recording:
                path = self._recorder.stop_recording()
                if path:
                    self._show_tray_msg("💾 工作流已保存", path)
                else:
                    self._show_tray_msg("未保存", "没有录制到任何操作")
            else:
                self._recorder.start_recording()
                self._show_tray_msg("▶ 开始录制", "按 Alt+Shift+R 结束并保存")

    def _show_tray_msg(self, title: str, body: str) -> None:
        """托盘气泡通知（如未启用托盘则降级为 print）。"""
        if self._tray:
            self._tray.showMessage(title, body,
                                    QSystemTrayIcon.Information, 3000)
        else:
            print(f"[overlay] {title}: {body}")

    def _setup_tray(self):
        self._tray = QSystemTrayIcon(self)
        icon = self._make_tray_icon()
        self._tray.setIcon(icon)
        self._tray.setToolTip(
            "NL2Shortcut — 输入栏(Alt+Shift+S) | "
            "剪贴板AI(Ctrl+Alt+V) | "
            "录制流程(Alt+Shift+R) | "
            "工作流(Ctrl+Alt+1~9)"
        )

        menu = QMenu()
        show_action = QAction("唤出输入栏", menu)
        show_action.triggered.connect(self._bar.show_bar)
        menu.addAction(show_action)
        menu.addSeparator()

        # ── 录制器 ──
        rec_action = QAction("录制新流程 (Alt+Shift+R)", menu)
        rec_action.triggered.connect(self._toggle_recorder)
        menu.addAction(rec_action)

        # ── 工作流热键入口（Ctrl+Alt+1~9）──
        wf_menu = menu.addMenu("已保存的流程 (Ctrl+Alt+1~9)")
        try:
            wf_names = sorted(self._agent.workflow_engine.list_workflows())
        except Exception:
            wf_names = []
        if not wf_names:
            none_act = QAction("（暂无 — 先录制一个）", wf_menu)
            none_act.setEnabled(False)
            wf_menu.addAction(none_act)
        else:
            for i, name in enumerate(wf_names[:9]):
                act = QAction(f"{i+1}. {name}  (Ctrl+Alt+{i+1})", wf_menu)
                act.triggered.connect(lambda n=name: self._run_workflow_by_name(n))
                wf_menu.addAction(act)

        menu.addSeparator()
        gui_action = QAction("打开完整界面", menu)
        gui_action.triggered.connect(self._open_gui)
        menu.addAction(gui_action)
        menu.addSeparator()
        quit_action = QAction("退出 NL2Shortcut", menu)
        quit_action.triggered.connect(self._confirm_quit)
        menu.addAction(quit_action)

        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    def _run_workflow_by_name(self, name: str) -> None:
        """从托盘菜单点击执行某个工作流。"""
        self._show_tray_msg(f"▶ 执行工作流: {name}", "正在执行…")
        threading.Thread(
            target=self._exec_workflow_thread, args=(name,), daemon=True
        ).start()

    def _make_tray_icon(self) -> QIcon:
        from PyQt5.QtGui import QPixmap
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QBrush(QColor("#6C63FF")))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(2, 2, 28, 28)
        painter.setPen(QPen(QColor("#FFFFFF")))
        painter.setFont(QFont("Segoe UI", 14, QFont.Bold))
        painter.drawText(QRect(0, 0, 32, 32), Qt.AlignCenter, "S")
        painter.end()
        return QIcon(pixmap)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self._bar.toggle()

    def _on_hotkey_activated(self):
        if self._bar.isVisible():
            self._bar.hide_bar()
        else:
            self._bar.show_bar()

    def _on_clipboard_hotkey(self):
        """Clipboard trigger: copy selection → read clipboard → show bar in AI mode."""
        import time
        # 1. Copy selection to clipboard
        try:
            self._agent.adapter.send_keys("Ctrl+C")
            time.sleep(0.15)
        except Exception:
            pass
        # 2. Read clipboard
        clip_text = self._agent.read_clipboard()
        if not clip_text:
            self._bar.show_bar()  # fallback to normal mode
            return
        # 3. Show bar in clipboard mode
        if self._bar.isVisible():
            self._bar.hide_bar()
        self._bar.show_bar(clipboard_text=clip_text)

    def _open_gui(self):
        subprocess.Popen(
            [sys.executable, "-m", "nl2shortcut", "gui"],
            creationflags=0x00000008,
        )

    def _confirm_quit(self):
        """退出前确认：弹出取消/确定对话框。"""
        reply = QMessageBox.question(
            None,
            "退出 NL2Shortcut",
            "确定要退出 NL2Shortcut 吗？\n\n"
            "退出后全局热键将失效，需重新运行 nl2shortcut overlay 启动。",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply == QMessageBox.Ok:
            self._quit()

    def _quit(self):
        self._hotkey_worker.stop()
        self._clip_hotkey_worker.stop()
        # 停掉所有工作流热键
        for w in self._workflow_workers:
            w.stop()
        self._recorder_worker.stop()
        # 停掉录制器（如果在跑）
        if self._recorder is not None:
            try:
                self._recorder.stop()
            except Exception:
                pass
            self._recorder = None
        self._bar.hide_bar()
        if self._tray:
            self._tray.hide()
        QApplication.instance().quit()

    def run(self):
        ok = self._hotkey_worker.start()
        if not ok:
            print("[nl2shortcut overlay] 提示: 热键注册失败，可通过托盘菜单唤出输入栏",
                  file=sys.stderr)
            # 不退出 — 托盘模式仍然可用
        ok2 = self._clip_hotkey_worker.start()
        if not ok2:
            print("[nl2shortcut overlay] 剪贴板热键注册失败（Ctrl+Alt+V），"
                  "可通过托盘菜单唤出输入栏后手动粘贴内容",
                  file=sys.stderr)
        # 工作流热键（Ctrl+Alt+1~9）—— 失败不致命，只是少几个绑定
        for i, w in enumerate(self._workflow_workers):
            ok = w.start()
            if not ok:
                print(f"[nl2shortcut overlay] 工作流热键 Ctrl+Alt+{i+1} 注册失败",
                      file=sys.stderr)
        # 录制器开关（Alt+Shift+R）—— 失败不致命
        ok = self._recorder_worker.start()
        if not ok:
            print("[nl2shortcut overlay] 录制器热键 Alt+Shift+R 注册失败",
                  file=sys.stderr)
        QApplication.instance().setQuitOnLastWindowClosed(False)


# ═══════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════

def main(show_tray: bool = True, hotkey: str = DEFAULT_HOTKEY):
    """Run NL2Shortcut overlay mode."""
    qt_app = QApplication.instance()
    if qt_app is None:
        qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("NL2Shortcut Overlay")
    qt_app.setQuitOnLastWindowClosed(False)
    overlay = OverlayApp(show_tray=show_tray, hotkey=hotkey)
    overlay.run()
    sys.exit(qt_app.exec_())


if __name__ == "__main__":
    main()
