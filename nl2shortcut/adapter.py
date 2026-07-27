"""Keyboard adapter — platform-specific key/mouse/text input.

Extended from the original adapter.py to add:
  - Text typing (typewrite)
  - Mouse control (click, scroll, drag, move)
  - Window operations (activate, minimize, maximize)
  - Screenshot capture

Primary backend (Windows):  pydirectinput  (scan-code SendInput, DirectInput-compatible)
Secondary backend:          pyautogui      (cross-platform fallback)
Tertiary fallback:          pynput         (universal)
"""

import time
import sys
from abc import ABC, abstractmethod
from typing import List, Tuple, Optional, Union

from .models import Platform

# ── pydirectinput (Windows DirectInput backend) ──────────────────────
try:
    import pydirectinput as _pydi
    _pydi.FAILSAFE = False
    _PYDI_AVAILABLE = True
except ImportError:
    _pydi = None  # type: ignore
    _PYDI_AVAILABLE = False


def _pydi_hotkey(*keys: str) -> None:
    """Send a hotkey combo via pydirectinput (keyDown → press → keyUp).

    pydirectinput has no ``hotkey()`` built in; this implements the
    canonical pattern: hold modifiers → tap main key → release modifiers.
    """
    if not _PYDI_AVAILABLE:
        raise RuntimeError("pydirectinput not available")
    modifiers = list(keys[:-1])
    main = keys[-1]
    for m in modifiers:
        _pydi.keyDown(m)
    _pydi.press(main)
    for m in reversed(modifiers):
        _pydi.keyUp(m)


def _pydi_button(button: str) -> str:
    """Map our button names to pydirectinput's naming convention.

    pydirectinput uses 'primary'/'secondary' instead of 'left'/'right'.
    """
    return {"left": "primary", "right": "secondary"}.get(button, button)

# ═══════════════════════════════════════════════════════════════════════
# Virtual Key Code tables (Windows native, unchanged)
# ═══════════════════════════════════════════════════════════════════════

VK_MAP = {
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B,
    "cmd": 0x5B,
    "option": 0x12,
}

CHAR_VK_MAP = {
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
    "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59,
    "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "tab": 0x09,
    "enter": 0x0D,
    "space": 0x20,
    "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "left": 0x25,
    "right": 0x27,
    "up": 0x26,
    "down": 0x28,
    "insert": 0x2D,
}

SHIFT_CHAR_MAP = {
    "!": 0x31, "@": 0x32, "#": 0x33, "$": 0x34,
    "%": 0x35, "^": 0x36, "&": 0x37, "*": 0x38,
    "(": 0x39, ")": 0x30, "_": 0xBD, "+": 0xBB,
    "{": 0xDB, "}": 0xDD, "|": 0xDC, ":": 0xBA,
    '"': 0xDE, "<": 0xBC, ">": 0xBE, "?": 0xBF,
    "~": 0xC0, "`": 0xC0, "-": 0xBD, "=": 0xBB,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA,
    "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}

KEYEVENTF_KEYDOWN = 0x0000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_UNICODE = 0x0004

# VK codes that require KEYEVENTF_EXTENDEDKEY flag
_EXTENDED_KEYS = frozenset({
    0x5B,  # VK_LWIN — Left Windows
    0x5C,  # VK_RWIN — Right Windows
    0x5D,  # VK_APPS — Menu/Apps
    0x21,  # VK_PRIOR — Page Up
    0x22,  # VK_NEXT — Page Down
    0x23,  # VK_END
    0x24,  # VK_HOME
    0x25,  # VK_LEFT
    0x26,  # VK_UP
    0x27,  # VK_RIGHT
    0x28,  # VK_DOWN
    0x2D,  # VK_INSERT
    0x2E,  # VK_DELETE
    0xA1,  # VK_RSHIFT
    0xA3,  # VK_RCONTROL
    0xA5,  # VK_RMENU — Right Alt
})


def _to_vk(key: str) -> int:
    """Convert a key string to a Windows virtual key code."""
    key_lower = key.lower().strip()
    if key_lower in CHAR_VK_MAP:
        return CHAR_VK_MAP[key_lower]
    if len(key_lower) == 1 and key_lower.isascii():
        char_code = ord(key_lower)
        if "a" <= key_lower <= "z":
            return char_code - 32
        if "0" <= key_lower <= "9":
            return char_code
        return SHIFT_CHAR_MAP.get(key_lower, char_code)
    return 0


def parse_key_string(key_str: str) -> Tuple[List[str], str]:
    """Parse 'Ctrl+Shift+C' into (['ctrl','shift'], 'C')."""
    parts = [p.strip() for p in key_str.split("+")]
    modifier_names = {"ctrl", "alt", "shift", "win", "cmd", "option",
                      "control", "meta", "super"}
    modifiers = []
    main_keys = []
    for part in parts:
        part_lower = part.lower()
        if part_lower in modifier_names:
            if part_lower in ("control", "meta", "super"):
                part_lower = "ctrl"
            elif part_lower == "option":
                part_lower = "alt"
            modifiers.append(part_lower)
        else:
            main_keys.append(part)
    main_key = "+".join(main_keys) if main_keys else ""
    return modifiers, main_key


# ═══════════════════════════════════════════════════════════════════════
# Abstract base + unified adapter
# ═══════════════════════════════════════════════════════════════════════

class KeyboardAdapter(ABC):
    """Abstract base for platform-specific keyboard/page adapters.

    Subclasses must implement:
      - send_keys(key_combination)       # hotkey combinations
      - type_text(text, interval)        # type raw text
      - click(x, y, button)              # mouse click
      - scroll(amount)                   # mouse scroll
      - platform property
    """

    @abstractmethod
    def send_keys(self, key_combination: str) -> None:
        ...

    @abstractmethod
    def type_text(self, text: str, interval: float = 0.0) -> None:
        ...

    @abstractmethod
    def click(self, x: Optional[int] = None, y: Optional[int] = None,
              button: str = "left", clicks: int = 1) -> None:
        ...

    @abstractmethod
    def scroll(self, amount: int, x: Optional[int] = None,
               y: Optional[int] = None) -> None:
        ...

    @abstractmethod
    def move(self, x: int, y: int, duration: float = 0.0) -> None:
        ...

    @abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int,
             button: str = "left", duration: float = 0.5) -> None:
        """Drag from (x1,y1) to (x2,y2)."""
        ...

    def screenshot(self, region: Optional[Tuple[int, int, int, int]] = None,
                   path: Optional[str] = None) -> Optional[str]:
        """Capture a screenshot. Returns path if saved, or None."""
        import pyautogui
        img = pyautogui.screenshot(region=region)
        if path:
            img.save(path)
            return path
        return None

    @property
    @abstractmethod
    def platform(self) -> Platform:
        ...


# ═══════════════════════════════════════════════════════════════════════
# SendInput structures (module-level, built once)
# ═══════════════════════════════════════════════════════════════════════

import ctypes
from ctypes import wintypes

# Pointer-sized unsigned long (not in wintypes)
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

INPUT_KEYBOARD = 1
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk",     wintypes.WORD),
        ("wScan",   wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time",    wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",         wintypes.LONG),
        ("dy",         wintypes.LONG),
        ("mouseData",  wintypes.DWORD),
        ("dwFlags",    wintypes.DWORD),
        ("time",       wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg",    wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", _KEYBDINPUT),
        ("mi", _MOUSEINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_u",   _INPUT_UNION),
    ]


# ═══════════════════════════════════════════════════════════════════════
# Windows Adapter (SendInput via user32.dll)
# ═══════════════════════════════════════════════════════════════════════

class WindowsAdapter(KeyboardAdapter):
    """Windows native adapter using SendInput for hotkeys,
    pydirectinput for mouse / text (DirectInput scan-code compatible),
    pyautogui for scroll / screenshot fallback."""

    def __init__(self):
        self._dll = ctypes.WinDLL("user32", use_last_error=True)
        # Set up SendInput argtypes
        self._dll.SendInput.argtypes = (
            wintypes.UINT,                    # cInputs
            ctypes.POINTER(_INPUT),           # pInputs
            wintypes.INT,                     # cbSize
        )
        self._dll.SendInput.restype = wintypes.UINT
        # MapVirtualKeyW: UINT uCode, UINT uMapType → UINT
        self._dll.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        self._dll.MapVirtualKeyW.restype = wintypes.UINT

    @property
    def platform(self) -> Platform:
        return Platform.WINDOWS

    def _key_event(self, vk: int, keydown: bool):
        """Send a single keyboard event via SendInput using scan codes."""
        flags = KEYEVENTF_SCANCODE
        if not keydown:
            flags |= KEYEVENTF_KEYUP
        if vk in _EXTENDED_KEYS:
            flags |= KEYEVENTF_EXTENDEDKEY

        scan = self._dll.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)

        inp = _INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = 0           # scan-code mode: VK not used
        inp.ki.wScan = scan
        inp.ki.dwFlags = flags
        inp.ki.time = 0
        inp.ki.dwExtraInfo = 0

        result = self._dll.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        if result == 0:
            raise OSError(
                f"SendInput failed for VK {vk:#04x} "
                f"({'press' if keydown else 'release'}): {ctypes.get_last_error()}"
            )

    def send_keys(self, key_combination: str) -> None:
        modifiers, main_key = parse_key_string(key_combination)
        is_upper = main_key.isupper() and len(main_key) == 1

        main_vk = _to_vk(main_key)
        if main_vk == 0 and main_key:
            raise ValueError(f"Unknown key: '{main_key}'")

        # Auto-add Shift for uppercase letters ONLY when typing solo
        # (no other modifiers).  In hotkey combos like Win+E / Ctrl+E, the
        # letter case is cosmetic — it refers to the physical key, not a
        # Shift state.
        solo_upper = is_upper and "shift" not in modifiers and not modifiers
        if solo_upper:
            self._key_event(VK_MAP["shift"], True)
        for mod in modifiers:
            vk = VK_MAP.get(mod)
            if vk:
                self._key_event(vk, True)

        # Press main key
        if main_vk:
            self._key_event(main_vk, True)
            self._key_event(main_vk, False)

        # Release modifiers in reverse
        for mod in reversed(modifiers):
            vk = VK_MAP.get(mod)
            if vk:
                self._key_event(vk, False)
        if solo_upper:
            self._key_event(VK_MAP["shift"], False)

    def _send_unicode(self, ch: str) -> None:
        """Send a single Unicode character via SendInput KEYEVENTF_UNICODE.

        This is the robust, clipboard-free way to inject any Unicode char
        (Chinese, emoji, etc.) on Windows. The character's UTF-16 code unit
        goes into wScan; wVk must be 0.
        """
        code = ord(ch)
        inp = _INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = 0
        inp.ki.wScan = code
        inp.ki.dwFlags = KEYEVENTF_UNICODE
        inp.ki.time = 0
        inp.ki.dwExtraInfo = 0
        # key down
        res = self._dll.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        if res == 0:
            raise OSError(f"SendInput(unicode) failed: {ctypes.get_last_error()}")
        # key up
        inp.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
        res = self._dll.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        if res == 0:
            raise OSError(f"SendInput(unicode up) failed: {ctypes.get_last_error()}")

    def _type_via_clipboard(self, text: str) -> None:
        """Type non-ASCII text (Chinese, etc.) via clipboard + Ctrl+V."""
        import ctypes
        import time

        GMEM_MOVEABLE = 0x0002
        CF_UNICODETEXT = 13

        OpenClipboard = ctypes.windll.user32.OpenClipboard
        CloseClipboard = ctypes.windll.user32.CloseClipboard
        GetClipboardData = ctypes.windll.user32.GetClipboardData
        SetClipboardData = ctypes.windll.user32.SetClipboardData
        EmptyClipboard = ctypes.windll.user32.EmptyClipboard
        GlobalAlloc = ctypes.windll.kernel32.GlobalAlloc
        GlobalLock = ctypes.windll.kernel32.GlobalLock
        GlobalUnlock = ctypes.windll.kernel32.GlobalUnlock

        old_text = None
        OpenClipboard(None)
        try:
            try:
                old_handle = GetClipboardData(CF_UNICODETEXT)
                if old_handle:
                    locked = GlobalLock(old_handle)
                    old_text = ctypes.c_wchar_p(locked).value
                    GlobalUnlock(old_handle)
            except Exception:
                old_text = None

            EmptyClipboard()
            text_bytes = (text + "\x00").encode("utf-16-le")
            h_data = GlobalAlloc(GMEM_MOVEABLE, len(text_bytes))
            locked = GlobalLock(h_data)
            ctypes.memmove(locked, text_bytes, len(text_bytes))
            GlobalUnlock(h_data)
            SetClipboardData(CF_UNICODETEXT, h_data)
        finally:
            CloseClipboard()

        self.send_keys("Ctrl+V")
        time.sleep(0.05)

        if old_text is not None:
            try:
                OpenClipboard(None)
                try:
                    EmptyClipboard()
                    text_bytes = (old_text + "\x00").encode("utf-16-le")
                    h_data = GlobalAlloc(GMEM_MOVEABLE, len(text_bytes))
                    locked = GlobalLock(h_data)
                    ctypes.memmove(locked, text_bytes, len(text_bytes))
                    GlobalUnlock(h_data)
                    SetClipboardData(CF_UNICODETEXT, h_data)
                finally:
                    CloseClipboard()
            except Exception:
                pass

    def type_text(self, text: str, interval: float = 0.0) -> None:
        import time
        # Tier 1: clipboard paste — IME-independent, exact text, no dropped chars.
        # Restores clipboard afterwards.
        try:
            import pyperclip
            old = pyperclip.paste()
            pyperclip.copy(text)
            time.sleep(0.05)
            self.send_keys("Ctrl+V")
            time.sleep(max(interval, 0.12))
            try:
                pyperclip.copy(old)
            except Exception:
                pass
            return
        except Exception:
            pass
        # Tier 2: pydirectinput.write — scan-code based, DirectInput-compatible,
        # works with games and apps that ignore virtual-key input.
        if _PYDI_AVAILABLE:
            try:
                _pydi.write(text, interval=interval)
                return
            except Exception:
                pass
        # Tier 3: pyautogui.write — reliable for ASCII/symbols
        try:
            import pyautogui
            pyautogui.write(text, interval=interval)
            return
        except Exception:
            pass
        # Tier 4: per-char Unicode SendInput (last resort, IME may mangle)
        for ch in text:
            if ch == "\n":
                self.send_keys("Enter")
            elif ch == "\t":
                self.send_keys("Tab")
            else:
                self._send_unicode(ch)
            time.sleep(interval)

    def click(self, x=None, y=None, button="left", clicks=1) -> None:
        if _PYDI_AVAILABLE:
            pydi_button = _pydi_button(button)
            _pydi.click(x=x, y=y, button=pydi_button, clicks=clicks)
        else:
            import pyautogui
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)

    def scroll(self, amount: int, x=None, y=None) -> None:
        # pydirectinput has no scroll() — keep pyautogui
        import pyautogui
        pyautogui.scroll(amount, x=x, y=y)

    def move(self, x: int, y: int, duration: float = 0.0) -> None:
        if _PYDI_AVAILABLE:
            _pydi.moveTo(x, y, duration=duration)
        else:
            import pyautogui
            pyautogui.moveTo(x, y, duration=duration)

    def drag(self, x1: int, y1: int, x2: int, y2: int,
             button: str = "left", duration: float = 0.5) -> None:
        if _PYDI_AVAILABLE:
            pydi_button = _pydi_button(button)
            _pydi.moveTo(x1, y1, duration=0.0)
            _pydi.mouseDown(button=pydi_button)
            _pydi.moveTo(x2, y2, duration=duration)
            _pydi.mouseUp(button=pydi_button)
        else:
            import pyautogui
            pyautogui.moveTo(x1, y1, duration=0.0)
            pyautogui.drag(x2 - x1, y2 - y1, button=button, duration=duration)

    def screenshot(self, region=None, path=None):
        # pydirectinput has no screenshot() — keep pyautogui
        import pyautogui
        img = pyautogui.screenshot(region=region)
        if path:
            img.save(path)
            return path
        return None


# ═══════════════════════════════════════════════════════════════════════
# PyAutoGUI Adapter (cross-platform, tries pydirectinput on Windows)
# ═══════════════════════════════════════════════════════════════════════

class PyAutoGUIAdapter(KeyboardAdapter):
    """Cross-platform adapter using pydirectinput (Windows) with pyautogui fallback.

    On Windows, mouse operations (click / move / drag) use pydirectinput for
    DirectInput compatibility.  Text typing prefers clipboard paste, then
    pydirectinput.write, then pyautogui.write.  Screenshots always use
    pyautogui (pydirectinput has no capture capability).
    """

    # PyAutoGUI key name mapping for special keys in hotkey()
    _KEY_MAP = {
        "ctrl": "ctrl", "control": "ctrl",
        "alt": "alt", "option": "alt",
        "shift": "shift",
        "win": "win", "cmd": "win", "meta": "win", "super": "win",
        "tab": "tab", "enter": "enter", "space": "space",
        "esc": "esc", "escape": "esc",
        "backspace": "backspace", "delete": "delete",
        "home": "home", "end": "end",
        "pageup": "pageup", "pagedown": "pagedown",
        "left": "left", "right": "right", "up": "up", "down": "down",
        "insert": "insert",
        "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
        "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8",
        "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
        "printscreen": "printscreen",
        "capslock": "capslock",
        "numlock": "numlock",
        "scrolllock": "scrolllock",
        "pause": "pause",
        "volumeup": "volumeup",
        "volumedown": "volumedown",
        "volumemute": "volumemute",
    }

    @property
    def platform(self) -> Platform:
        return Platform.detect()

    def send_keys(self, key_combination: str) -> None:
        modifiers, main_key = parse_key_string(key_combination)

        # On Windows, prefer pydirectinput (scan-code based, DirectInput compatible)
        if _PYDI_AVAILABLE:
            mod_keys = [self._KEY_MAP.get(m, m) for m in modifiers]
            key_lower = main_key.lower().strip()
            mapped_main = self._KEY_MAP.get(key_lower, main_key)
            _pydi_hotkey(*(mod_keys + [mapped_main]))
            return

        import pyautogui
        mod_keys = [self._KEY_MAP.get(m, m) for m in modifiers]
        key_lower = main_key.lower().strip()
        mapped_main = self._KEY_MAP.get(key_lower, main_key)
        all_keys = mod_keys + [mapped_main]
        pyautogui.hotkey(*all_keys)

    def _send_unicode(self, ch: str) -> None:
        """Send a single Unicode char via SendInput KEYEVENTF_UNICODE.
        Clipboard-free, IME-free, crash-free."""
        import ctypes
        from ctypes import wintypes
        dll = ctypes.WinDLL("user32", use_last_error=True)
        dll.SendInput.argtypes = (
            wintypes.UINT, ctypes.POINTER(_INPUT), wintypes.INT)
        dll.SendInput.restype = wintypes.UINT
        code = ord(ch)
        inp = _INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki.wVk = 0
        inp.ki.wScan = code
        inp.ki.dwFlags = KEYEVENTF_UNICODE
        inp.ki.time = 0
        inp.ki.dwExtraInfo = 0
        dll.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
        inp.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
        dll.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


    def _type_via_clipboard(self, text: str) -> None:
        """Type non-ASCII text (Chinese, etc.) via clipboard + Ctrl+V."""
        import ctypes
        import time

        GMEM_MOVEABLE = 0x0002
        CF_UNICODETEXT = 13

        OpenClipboard = ctypes.windll.user32.OpenClipboard
        CloseClipboard = ctypes.windll.user32.CloseClipboard
        GetClipboardData = ctypes.windll.user32.GetClipboardData
        SetClipboardData = ctypes.windll.user32.SetClipboardData
        EmptyClipboard = ctypes.windll.user32.EmptyClipboard
        GlobalAlloc = ctypes.windll.kernel32.GlobalAlloc
        GlobalLock = ctypes.windll.kernel32.GlobalLock
        GlobalUnlock = ctypes.windll.kernel32.GlobalUnlock

        old_text = None
        OpenClipboard(None)
        try:
            try:
                old_handle = GetClipboardData(CF_UNICODETEXT)
                if old_handle:
                    locked = GlobalLock(old_handle)
                    old_text = ctypes.c_wchar_p(locked).value
                    GlobalUnlock(old_handle)
            except Exception:
                old_text = None

            EmptyClipboard()
            text_bytes = (text + "\x00").encode("utf-16-le")
            h_data = GlobalAlloc(GMEM_MOVEABLE, len(text_bytes))
            locked = GlobalLock(h_data)
            ctypes.memmove(locked, text_bytes, len(text_bytes))
            GlobalUnlock(h_data)
            SetClipboardData(CF_UNICODETEXT, h_data)
        finally:
            CloseClipboard()

        self.send_keys("Ctrl+V")
        time.sleep(0.05)

        if old_text is not None:
            try:
                OpenClipboard(None)
                try:
                    EmptyClipboard()
                    text_bytes = (old_text + "\x00").encode("utf-16-le")
                    h_data = GlobalAlloc(GMEM_MOVEABLE, len(text_bytes))
                    locked = GlobalLock(h_data)
                    ctypes.memmove(locked, text_bytes, len(text_bytes))
                    GlobalUnlock(h_data)
                    SetClipboardData(CF_UNICODETEXT, h_data)
                finally:
                    CloseClipboard()
            except Exception:
                pass

    def type_text(self, text: str, interval: float = 0.0) -> None:
        import time
        # Tier 1: clipboard paste — IME-independent, exact text, no dropped chars.
        try:
            import pyperclip
            old = pyperclip.paste()
            pyperclip.copy(text)
            time.sleep(0.05)
            self.send_keys("Ctrl+V")
            time.sleep(max(interval, 0.12))
            try:
                pyperclip.copy(old)
            except Exception:
                pass
            return
        except Exception:
            pass
        # Tier 2: pydirectinput.write — scan-code based, DirectInput-compatible
        if _PYDI_AVAILABLE:
            try:
                _pydi.write(text, interval=interval)
                return
            except Exception:
                pass
        # Tier 3: pyautogui.write — reliable for ASCII/symbols
        try:
            import pyautogui
            pyautogui.write(text, interval=interval)
            return
        except Exception:
            pass
        # Tier 4: per-char Unicode SendInput (last resort)
        for ch in text:
            if ch == "\n":
                self.send_keys("Enter")
            elif ch == "\t":
                self.send_keys("Tab")
            else:
                self._send_unicode(ch)
            time.sleep(interval)

    def click(self, x=None, y=None, button="left", clicks=1) -> None:
        if _PYDI_AVAILABLE:
            pydi_button = _pydi_button(button)
            _pydi.click(x=x, y=y, button=pydi_button, clicks=clicks)
        else:
            import pyautogui
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)

    def scroll(self, amount: int, x=None, y=None) -> None:
        # pydirectinput has no scroll() — keep pyautogui
        import pyautogui
        pyautogui.scroll(amount, x=x, y=y)

    def move(self, x: int, y: int, duration: float = 0.0) -> None:
        if _PYDI_AVAILABLE:
            _pydi.moveTo(x, y, duration=duration)
        else:
            import pyautogui
            pyautogui.moveTo(x, y, duration=duration)

    def drag(self, x1: int, y1: int, x2: int, y2: int,
             button: str = "left", duration: float = 0.5) -> None:
        if _PYDI_AVAILABLE:
            pydi_button = _pydi_button(button)
            _pydi.moveTo(x1, y1, duration=0.0)
            _pydi.mouseDown(button=pydi_button)
            _pydi.moveTo(x2, y2, duration=duration)
            _pydi.mouseUp(button=pydi_button)
        else:
            import pyautogui
            pyautogui.moveTo(x1, y1, duration=0.0)
            pyautogui.drag(x2 - x1, y2 - y1, button=button, duration=duration)

    def screenshot(self, region=None, path=None):
        # pydirectinput has no screenshot() — keep pyautogui
        import pyautogui
        img = pyautogui.screenshot(region=region)
        if path:
            img.save(path)
            return path
        return None


# ═══════════════════════════════════════════════════════════════════════
# Pynput Adapter (cross-platform fallback)
# ═══════════════════════════════════════════════════════════════════════

class PynputAdapter(KeyboardAdapter):
    """Cross-platform fallback using pynput (keyboard) + pydirectinput / pyautogui (mouse)."""

    def __init__(self):
        from pynput.keyboard import Controller, Key
        self._controller = Controller()
        self._key_map = {
            "ctrl": Key.ctrl, "alt": Key.alt, "shift": Key.shift,
            "win": Key.cmd, "cmd": Key.cmd,
            "tab": Key.tab, "enter": Key.enter, "space": Key.space,
            "esc": Key.esc, "escape": Key.esc,
            "backspace": Key.backspace, "delete": Key.delete,
            "home": Key.home, "end": Key.end,
            "pageup": Key.page_up, "pagedown": Key.page_down,
            "left": Key.left, "right": Key.right, "up": Key.up, "down": Key.down,
            "insert": Key.insert, "printscreen": Key.print_screen,
            "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
            "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
            "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
        }

    @property
    def platform(self) -> Platform:
        return Platform.detect()

    def send_keys(self, key_combination: str) -> None:
        modifiers, main_key = parse_key_string(key_combination)
        mod_keys = set()
        for mod in modifiers:
            key = self._key_map.get(mod)
            if key:
                mod_keys.add(key)
        main_key_obj = self._key_map.get(main_key.lower(), main_key)
        with self._controller.pressed(*mod_keys):
            self._controller.tap(main_key_obj)

    def type_text(self, text: str, interval: float = 0.0) -> None:
        self._controller.type(text)
        if interval:
            time.sleep(interval)

    def click(self, x=None, y=None, button="left", clicks=1) -> None:
        if _PYDI_AVAILABLE:
            pydi_button = _pydi_button(button)
            _pydi.click(x=x, y=y, button=pydi_button, clicks=clicks)
        else:
            import pyautogui
            pyautogui.click(x=x, y=y, button=button, clicks=clicks)

    def scroll(self, amount: int, x=None, y=None) -> None:
        import pyautogui
        pyautogui.scroll(amount, x=x, y=y)

    def move(self, x: int, y: int, duration: float = 0.0) -> None:
        if _PYDI_AVAILABLE:
            _pydi.moveTo(x, y, duration=duration)
        else:
            import pyautogui
            pyautogui.moveTo(x, y, duration=duration)

    def drag(self, x1: int, y1: int, x2: int, y2: int,
             button: str = "left", duration: float = 0.5) -> None:
        if _PYDI_AVAILABLE:
            pydi_button = _pydi_button(button)
            _pydi.moveTo(x1, y1, duration=0.0)
            _pydi.mouseDown(button=pydi_button)
            _pydi.moveTo(x2, y2, duration=duration)
            _pydi.mouseUp(button=pydi_button)
        else:
            import pyautogui
            pyautogui.moveTo(x1, y1, duration=0.0)
            pyautogui.drag(x2 - x1, y2 - y1, button=button, duration=duration)

    def screenshot(self, region=None, path=None):
        # pydirectinput has no screenshot — keep pyautogui
        import pyautogui
        img = pyautogui.screenshot(region=region)
        if path:
            img.save(path)
            return path
        return None


# ═══════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════

def create_adapter(platform: Optional[Platform] = None,
                   preferred: Optional[str] = None) -> KeyboardAdapter:
    """Factory: create the best available keyboard adapter.

    Priority (auto):
      1. WindowsAdapter — native ctypes + user32.dll (Windows only, most reliable)
      2. PyAutoGUIAdapter — cross-platform, clean API, hotkey support
      3. PynputAdapter — fallback, requires pynput

    Args:
        platform: Target platform (auto-detect if None)
        preferred: Force a specific adapter ("windows", "pyautogui", "pynput")
    """
    if platform is None:
        platform = Platform.detect()

    # Respect explicit preference
    if preferred == "windows":
        return WindowsAdapter()
    if preferred == "pyautogui":
        try:
            return PynputAdapter()
        except ImportError:
            raise RuntimeError("pynput not installed. Run: pip install pynput")

    # Auto-detect: Windows gets native adapter (most reliable key injection)
    if platform == Platform.WINDOWS:
        try:
            return WindowsAdapter()
        except Exception:
            pass

    try:
        return PyAutoGUIAdapter()
    except ImportError:
        pass

    try:
        return PynputAdapter()
    except ImportError:
        raise RuntimeError(
            f"No keyboard adapter available for {platform.value}. "
            f"Install pyautogui: pip install pyautogui"
        )
