"""主动录制器 —— 让业务员"做一遍就自动化"。

按下 Alt+Shift+R 开始录制，再做一遍操作（任意次按键/点击/滚动），
再按 Alt+Shift+R 结束录制 → 自动保存为 YAML 工作流。

下次说"打开 ERP 并查询今日订单"或绑定全局热键一键复用。

实现：
  - Win32 低级钩子（WH_KEYBOARD_LL / WH_MOUSE_LL）监听全局键鼠
  - 钩子运行在独立守护线程（SetWindowsHookEx + GetMessage 循环）
  - 录制期间所有事件转为 OpRecord 序列
  - 结束时调用 OperationMemory.record_sequence + export_pattern_to_workflow
  - 生成的工作流名：rec-YYYYMMDD-HHMMSS-N步

用法（CLI 单独跑）：
    python -m nl2shortcut.recorder
    # 按 Alt+Shift+R 开始/结束录制

作为模块使用：
    from nl2shortcut.recorder import Recorder
    r = Recorder()
    r.start()  # 开始录制
    ...
    r.stop()  # 结束并保存工作流
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 项目内依赖 ──────────────────────────────────────────────────────────
from .operation_memory import OperationMemory, OpRecord


# ═══════════════════════════════════════════════════════════════════════
# Win32 常量
# ═══════════════════════════════════════════════════════════════════════

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14

HC_ACTION = 0

WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_LBUTTONDOWN = 0x0200
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0206
WM_MBUTTONDOWN = 0x0208
WM_MBUTTONUP = 0x020A
WM_MOUSEWHEEL = 0x020A
WM_XBUTTONDOWN = 0x020B

# 修饰键 VK
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# 切换录制状态的热键（Alt+Shift+R）
TOGGLE_HOTKEY_ID = 0xB001
TOGGLE_MODS = 0x0001 | 0x0004  # MOD_ALT | MOD_SHIFT
TOGGLE_VK = 0x52  # 'R'


# ═══════════════════════════════════════════════════════════════════════
# VK → 可读字符串
# ═══════════════════════════════════════════════════════════════════════

_VK_NAMES = {
    0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter", 0x1B: "Esc",
    0x20: "Space", 0x21: "PageUp", 0x22: "PageDown",
    0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down",
    0x2D: "Insert", 0x2E: "Delete",
    0x70: "F1", 0x71: "F2", 0x72: "F3", 0x73: "F4", 0x74: "F5",
    0x75: "F6", 0x76: "F7", 0x77: "F8", 0x78: "F9", 0x79: "F10",
    0x7A: "F11", 0x7B: "F12",
}


def _vk_to_name(vk: int, mods: int) -> str:
    """把 VK + 修饰键转为 "Ctrl+S" / "Alt+F4" 这样的规范字符串。"""
    parts = []
    if mods & 0x0002:  # MOD_CONTROL
        parts.append("Ctrl")
    if mods & 0x0001:  # MOD_ALT
        parts.append("Alt")
    if mods & 0x0004:  # MOD_SHIFT
        parts.append("Shift")
    if mods & 0x0008:  # MOD_WIN
        parts.append("Win")

    # 主键名
    if vk in _VK_NAMES:
        key = _VK_NAMES[vk]
    elif 0x30 <= vk <= 0x39:  # 0-9
        key = chr(vk)
    elif 0x41 <= vk <= 0x5A:  # A-Z
        key = chr(vk)
    else:
        key = f"VK_{vk:02X}"

    parts.append(key)
    return "+".join(parts)


def _get_modifiers_state() -> int:
    """读取当前修饰键按下状态，返回 MOD_* 位掩码。"""
    mods = 0
    if ctypes.windll.user32.GetAsyncKeyState(VK_CONTROL) & 0x8000:
        mods |= 0x0002
    if ctypes.windll.user32.GetAsyncKeyState(VK_MENU) & 0x8000:
        mods |= 0x0001
    if ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000:
        mods |= 0x0004
    if (ctypes.windll.user32.GetAsyncKeyState(VK_LWIN) & 0x8000
            or ctypes.windll.user32.GetAsyncKeyState(VK_RWIN) & 0x8000):
        mods |= 0x0008
    return mods


# ═══════════════════════════════════════════════════════════════════════
# 录制器
# ═══════════════════════════════════════════════════════════════════════

# 钩子过程签名
HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,  # LRESULT
    ctypes.c_int,   # int code
    wintypes.WPARAM,
    wintypes.LPARAM,
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class Recorder:
    """主动录制器：监听全局键鼠，结束时导出为 YAML 工作流。

    线程模型：
      - 钩子必须运行在拥有消息循环的线程
      - 单独守护线程跑 GetMessage + DispatchMessage
      - 主线程通过 Event 控制 start/stop
    """

    def __init__(self, memory: Optional[OperationMemory] = None):
        self._memory = memory or OperationMemory()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._recording = False
        self._records: list[OpRecord] = []
        self._seq_id = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._kb_hook = None
        self._ms_hook = None
        self._toggle_hotkey_hwnd = None

    # ── 公共 API ────────────────────────────────────────────────────────

    def start_recording(self) -> None:
        """开始录制（清空旧记录）。"""
        with self._lock:
            self._records.clear()
            self._seq_id = str(uuid.uuid4())
            self._recording = True
        print("[recorder] ▶ 开始录制（Alt+Shift+R 结束并保存）")

    def stop_recording(self) -> Optional[str]:
        """结束录制，导出工作流。返回工作流路径或 None。"""
        with self._lock:
            self._recording = False
            records = list(self._records)
            self._records.clear()

        if not records:
            print("[recorder] ⏹ 未录制到任何操作")
            return None

        print(f"[recorder] ⏹ 结束录制，共 {len(records)} 步")

        # 1. 写入 OperationMemory（参与后续模式学习）
        self._memory.record_sequence(records)

        # 2. 直接导出为 YAML 工作流
        return self._export_to_yaml(records)

    @property
    def is_recording(self) -> bool:
        return self._recording

    # ── 后台钩子线程 ────────────────────────────────────────────────────

    def run(self, with_toggle_hotkey: bool = True) -> None:
        """启动钩子线程，阻塞直到 stop() 被调用。

        Args:
            with_toggle_hotkey: 是否在钩子线程里注册 Alt+Shift+R 切换热键。
                True（默认，CLI 模式）：自己管切换。
                False（被 overlay 托管时）：不注册，由外部调
                start_recording / stop_recording。
        """
        self._with_toggle_hotkey = with_toggle_hotkey
        self._thread = threading.Thread(
            target=self._hook_thread, daemon=True, name="nl2shortcut-recorder"
        )
        self._stop_event.clear()
        self._thread.start()
        # 等线程初始化
        time.sleep(0.1)

    def stop(self) -> None:
        """停止钩子线程。"""
        self._stop_event.set()
        # 给消息循环发个 WM_QUIT
        if self._thread and self._thread.is_alive():
            tid = self._thread.ident
            if tid:
                ctypes.windll.user32.PostThreadMessageW(tid, 0x0012, 0, 0)
            self._thread.join(timeout=2)
        self._thread = None

    # ── 内部：钩子线程主循环 ────────────────────────────────────────────

    def _hook_thread(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hinstance = kernel32.GetModuleHandleW(None)

        # ── 钩子过程 ──
        def _kb_callback(code, wparam, lparam):
            if code != HC_ACTION:
                return user32.CallNextHookEx(self._kb_hook, code, wparam, lparam)

            # 只在录制中处理
            if not self._recording:
                return user32.CallNextHookEx(self._kb_hook, code, wparam, lparam)

            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = KBDLLHOOKSTRUCT.from_address(lparam)
                vk = kb.vkCode
                # 过滤掉单独的修饰键按下（避免污染记录）
                if vk in (VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN):
                    return user32.CallNextHookEx(self._kb_hook, code, wparam, lparam)
                mods = _get_modifiers_state()
                key_name = _vk_to_name(vk, mods)
                self._add_record("shortcut", key_name)
            return user32.CallNextHookEx(self._kb_hook, code, wparam, lparam)

        def _ms_callback(code, wparam, lparam):
            if code != HC_ACTION:
                return user32.CallNextHookEx(self._ms_hook, code, wparam, lparam)
            if not self._recording:
                return user32.CallNextHookEx(self._ms_hook, code, wparam, lparam)

            if wparam in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
                ms = MSLLHOOKSTRUCT.from_address(lparam)
                button = {WM_LBUTTONDOWN: "left",
                          WM_RBUTTONDOWN: "right",
                          WM_MBUTTONDOWN: "middle"}.get(wparam, "left")
                self._add_record("click",
                                  f"{button}@({ms.pt.x},{ms.pt.y})")
            elif wparam == WM_MOUSEWHEEL:
                ms = MSLLHOOKSTRUCT.from_address(lparam)
                # HIWORD(mouseData) 是滚轮 delta（通常 ±120）
                delta = (ms.mouseData >> 16) & 0xFFFF
                direction = "down" if delta < 0x8000 else "up"
                self._add_record("scroll", direction)
            return user32.CallNextHookEx(self._ms_hook, code, wparam, lparam)

        kb_proc = HOOKPROC(_kb_callback)
        ms_proc = HOOKPROC(_ms_callback)

        self._kb_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, kb_proc, hinstance, 0)
        if not self._kb_hook:
            err = kernel32.GetLastError()
            print(f"[recorder] 键盘钩子注册失败 (err {err})", file=sys.stderr)
            return
        self._ms_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, ms_proc, hinstance, 0)
        if not self._ms_hook:
            err = kernel32.GetLastError()
            print(f"[recorder] 鼠标钩子注册失败 (err {err})", file=sys.stderr)
            user32.UnhookWindowsHookEx(self._kb_hook)
            return

        # ── 切换热键（仅 CLI 模式注册；被 overlay 托管时跳过）──
        if getattr(self, "_with_toggle_hotkey", True):
            class_name = f"NL2Rec_{id(self):X}"
            WNDPROC_T = ctypes.WINFUNCTYPE(
                ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM,
            )

            def _wnd_proc(hwnd, msg, wparam, lparam):
                if msg == 0x0312 and wparam == TOGGLE_HOTKEY_ID:  # WM_HOTKEY
                    if self._recording:
                        self.stop_recording()
                    else:
                        self.start_recording()
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            wnd_proc = WNDPROC_T(_wnd_proc)
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
                err = kernel32.GetLastError()
                print(f"[recorder] 窗口类注册失败 (err {err})", file=sys.stderr)
            else:
                self._toggle_hotkey_hwnd = user32.CreateWindowExW(
                    0, class_name, "NL2Shortcut Recorder", 0, 0, 0, 0, 0,
                    wintypes.HWND(-3), None, hinstance, None,
                )
                if self._toggle_hotkey_hwnd:
                    ok = user32.RegisterHotKey(
                        self._toggle_hotkey_hwnd, TOGGLE_HOTKEY_ID,
                        TOGGLE_MODS, TOGGLE_VK,
                    )
                    if not ok:
                        print("[recorder] Alt+Shift+R 热键注册失败", file=sys.stderr)

            print("[recorder] 录制器已启动 — 按 Alt+Shift+R 开始录制")
        else:
            print("[recorder] 录制器已启动（热键由外部托管）")

        # ── 消息循环 ──
        msg = wintypes.MSG()
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # ── 清理 ──
        if self._toggle_hotkey_hwnd:
            user32.UnregisterHotKey(self._toggle_hotkey_hwnd, TOGGLE_HOTKEY_ID)
            user32.DestroyWindow(self._toggle_hotkey_hwnd)
            user32.UnregisterClassW(class_name, hinstance)
        if self._kb_hook:
            user32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
        if self._ms_hook:
            user32.UnhookWindowsHookEx(self._ms_hook)
            self._ms_hook = None

    # ── 内部：写入一条记录 ──────────────────────────────────────────────

    def _add_record(self, action_type: str, action_detail: str) -> None:
        """向录制缓冲追加一条记录。"""
        # 简单获取当前前台进程名
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            title = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(hwnd, title, 256)
            app = title.value.split(" - ")[-1].split(" — ")[-1] if title.value else "unknown"
        except Exception:
            app = "unknown"

        with self._lock:
            self._records.append(OpRecord(
                app=app,
                action_type=action_type,
                action_detail=action_detail,
                duration_ms=0,
                user_goal="recorded",
                sequence_id=self._seq_id,
            ))

    # ── 导出为 YAML 工作流 ──────────────────────────────────────────────

    def _export_to_yaml(self, records: list[OpRecord]) -> Optional[str]:
        """把录制记录导出为 YAML 工作流，返回文件路径。"""
        import yaml
        from .operation_memory import canonical_key

        wf_dir = Path.home() / ".nl2shortcut" / "workflows"
        wf_dir.mkdir(parents=True, exist_ok=True)

        # 工作流名：rec-YYYYMMDD-HHMMSS-N步
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"rec-{ts}-{len(records)}步"
        # 清理非法字符
        name = name.replace(":", "-").replace("/", "-")

        filepath = wf_dir / f"{name}.yaml"

        # 转 workflow step 格式
        steps = []
        for i, rec in enumerate(records, 1):
            if rec.action_type == "shortcut":
                action = "shortcut"
                command = canonical_key(rec.action_detail)
                desc = f"按下 {command}"
            elif rec.action_type == "click":
                action = "click"
                command = rec.action_detail  # "left@(x,y)"
                desc = f"点击 {command}"
            elif rec.action_type == "scroll":
                action = "scroll"
                command = rec.action_detail  # "up" / "down"
                desc = f"滚动 {command}"
            else:
                action = "shortcut"
                command = rec.action_detail
                desc = rec.action_detail

            steps.append({
                "name": f"第{i}步：{desc}",
                "action": action,
                "command": command,
            })

        doc = {
            "name": name,
            "description": f"录制于 {ts}，共 {len(records)} 步（app={records[0].app if records else 'unknown'}）",
            "version": "1.0",
            "variables": {},
            "steps": steps,
        }

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"# 录制于 {ts}\n")
                f.write(f"# 步数: {len(records)}  |  来源: recorder.py\n")
                yaml.dump(doc, f, allow_unicode=True,
                          default_flow_style=False, sort_keys=False)
            print(f"[recorder] 💾 已保存工作流: {filepath}")
            return str(filepath)
        except Exception as e:
            print(f"[recorder] 保存失败: {e}", file=sys.stderr)
            return None


# ═══════════════════════════════════════════════════════════════════════
# CLI 入口
# ═══════════════════════════════════════════════════════════════════════

def main():
    """独立运行录制器：按 Alt+Shift+R 开始/结束录制。"""
    print("NL2Shortcut 录制器")
    print("  按 Alt+Shift+R 开始录制")
    print("  录制中再按 Alt+Shift+R 结束并保存为工作流")
    print("  按 Ctrl+C 退出程序")
    print()

    recorder = Recorder()
    recorder.run()

    # 阻塞主线程，直到 Ctrl+C
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[recorder] 退出中...")
        if recorder.is_recording:
            recorder.stop_recording()
        recorder.stop()
        print("[recorder] 已退出")


if __name__ == "__main__":
    main()
