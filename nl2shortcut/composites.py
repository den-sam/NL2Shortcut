"""Composite (multi-step, vision-driven) plans.

A single keyboard shortcut cannot accomplish some intents — e.g.
"copy this file to that folder". For those, NL2Shortcut generates a
multi-step *composite plan* that mixes three kinds of primitives:

  • vision_find  — take a screenshot + emit a hint; the upstream Agent's
                   vision model locates the UI element and returns coords.
  • click        — mouse click (left/right) at coords (from the prior
                   vision_find step) via the keyboard adapter.
  • type         — type raw text (e.g. a destination path) via the adapter.
  • key          — send a key combination (e.g. Ctrl+L) via the adapter.
  • wait         — brief pause (ms) to let the UI settle.

NL2Shortcut supplies the primitives and the *recipe*; the upstream Agent
supplies the visual intelligence and iterates the steps. This module only
builds the plan. See `_artifact_composite_2026-07-11.md` for the Agent
execution protocol.
"""

import time
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# ── 加载延迟（毫秒）────────────────────────────────────────────
# 用于「会触发 UI 加载」的环节，消除操作过快导致的竞态（典型如资源管理器
# 搜索索引未返回、目录尚未刷新就继续操作，导致选中 / 全选落空）。
# 默认值集中于此；可通过 set_load_waits() 或环境变量在运行期覆盖，
# 便于按本地盘 / 网络盘（乃至 OneDrive）现场调参，无需改源码。
_DEFAULT_LOAD_WAITS = {
    "explorer_open": 800,   # 打开 / 聚焦资源管理器后，窗口与功能区渲染
    "search_index":  800,   # 触发搜索后，等待索引返回结果
    "folder_nav":    600,   # 地址栏 / 对话框回车跳转后，等待目录内容加载
    "dialog_load":   500,   # 文件对话框跳转后，等待列表加载
}

# 运行期覆盖值（优先于默认值与环境变量）
_LOAD_WAIT_OVERRIDES: Dict[str, int] = {}


def _load_wait_env(name: str) -> Optional[str]:
    """返回加载延迟相关的环境变量名（未识别的名称返回 None）。"""
    return {
        "explorer_open": "NL2SC_WAIT_EXPLORER_OPEN",
        "search_index":  "NL2SC_WAIT_SEARCH_INDEX",
        "folder_nav":    "NL2SC_WAIT_FOLDER_NAV",
        "dialog_load":   "NL2SC_WAIT_DIALOG_LOAD",
    }.get(name)


def set_load_waits(*, explorer_open: Optional[int] = None,
                   search_index: Optional[int] = None,
                   folder_nav: Optional[int] = None,
                   dialog_load: Optional[int] = None) -> None:
    """运行期覆盖各加载环节的等待毫秒数。

    典型用途：现场遇到网络盘 / OneDrive 等加载较慢的环境，无需改源码即可
    调大等待。例如::

        from nl2shortcut.composites import set_load_waits
        set_load_waits(search_index=2500, folder_nav=1500)

    调用后，后续生成的所有工作流立即采用新值。
    """
    mapping = {
        "explorer_open": explorer_open,
        "search_index":  search_index,
        "folder_nav":    folder_nav,
        "dialog_load":   dialog_load,
    }
    for key, val in mapping.items():
        if val is not None:
            _LOAD_WAIT_OVERRIDES[key] = max(0, int(val))


def get_load_wait(name: str) -> int:
    """返回某加载环节的等待毫秒数。

    优先级：运行期覆盖值 > 环境变量 > 默认值。
    """
    if name in _LOAD_WAIT_OVERRIDES:
        return _LOAD_WAIT_OVERRIDES[name]
    env_name = _load_wait_env(name)
    if env_name:
        raw = os.environ.get(env_name)
        if raw is not None:
            try:
                return max(0, int(raw))
            except (TypeError, ValueError):
                pass
    return _DEFAULT_LOAD_WAITS.get(name, 0)


@dataclass
class CompositeStep:
    """One step in a composite plan."""
    kind: str            # "vision_find" | "click" | "type" | "key" | "wait"
    description: str = ""  # human-readable, shown in the GUI
    # --- vision_find ---
    intent: str = ""       # what to look for on screen
    # --- click ---
    button: str = "left"   # "left" | "right" | "middle"
    clicks: int = 1
    # --- type ---
    text: str = ""         # text to type
    # --- key ---
    keys: str = ""         # e.g. "Ctrl+L"
    # --- wait ---
    wait_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "description": self.description,
            "intent": self.intent,
            "button": self.button,
            "clicks": self.clicks,
            "text": self.text,
            "keys": self.keys,
            "wait_ms": self.wait_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompositeStep":
        return cls(
            kind=d["kind"],
            description=d.get("description", ""),
            intent=d.get("intent", ""),
            button=d.get("button", "left"),
            clicks=d.get("clicks", 1),
            text=d.get("text", ""),
            keys=d.get("keys", ""),
            wait_ms=d.get("wait_ms", 0),
        )


@dataclass
class CompositePlan:
    """A multi-step plan executed by the Agent with NL2Shortcut primitives."""
    name: str
    description: str
    steps: List[CompositeStep] = field(default_factory=list)
    confidence: float = 0.85
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompositePlan":
        return cls(
            name=d.get("name", "composite"),
            description=d.get("description", ""),
            confidence=d.get("confidence", 0.85),
            reasoning=d.get("reasoning", ""),
            steps=[CompositeStep.from_dict(s) for s in d.get("steps", [])],
        )

    def format_human(self) -> str:
        """Render the plan as a numbered list (for the GUI chat)."""
        icons = {
            "vision_find": "🔍",
            "click": "🖱️",
            "type": "⌨️",
            "key": "⌨️",
            "wait": "⏱️",
        }
        lines = [f"📋  {self.description}（共 {len(self.steps)} 步，需 Agent 视觉模型分步执行）"]
        for i, s in enumerate(self.steps, 1):
            icon = icons.get(s.kind, "•")
            if s.kind == "vision_find":
                detail = f"「{s.intent}」"
            elif s.kind == "click":
                arrow = " ← 使用上一步视觉坐标" if s.button in ("left", "right") else ""
                detail = f"({s.button}, {s.clicks}x){arrow}"
            elif s.kind == "type":
                detail = f"「{s.text}」"
            elif s.kind == "key":
                detail = s.keys
            elif s.kind == "wait":
                detail = f"{s.wait_ms}ms"
            else:
                detail = ""
            lines.append(f"  {i:2d}. {icon} {s.description}  {detail}")
        return "\n".join(lines)


# ── Plan factories ──────────────────────────────────────────────────────────

def make_file_copy_context_menu(source_desc: str, dest_path: str) -> CompositePlan:
    """Copy a file via right-click context menu → navigate → paste.

    Args:
        source_desc: what the source item looks like on screen, e.g.
            'the file named "新建 DOCX 文档 (2).docx" in the file list'.
        dest_path:   destination folder path typed into the address bar, e.g.
            'C:\\Users\\Deng2\\Desktop\\新建文件夹'.
    """
    return CompositePlan(
        name="file_copy_context_menu",
        description="右键 Copy → 导航 → Paste",
        confidence=0.85,
        reasoning=(
            f"通过文件管理器右键菜单把「{source_desc}」复制到「{dest_path}」。"
            "全程需 Agent 视觉模型定位 UI 元素（源文件、右键菜单项、目标空白区）。"
        ),
        steps=[
            CompositeStep(kind="vision_find", intent=source_desc,
                          description=f"在屏幕上找到源文件「{source_desc}」"),
            CompositeStep(kind="click", button="right", clicks=1,
                          description="在源文件上右键，弹出右键菜单"),
            CompositeStep(kind="vision_find", intent='context menu "Copy" item',
                          description='在右键菜单中找到 "Copy" 项'),
            CompositeStep(kind="click", button="left", clicks=1,
                          description='点击 "Copy"，文件已复制到剪贴板'),
            CompositeStep(kind="key", keys="Ctrl+L",
                          description="Ctrl+L 聚焦地址栏（Windows 资源管理器）"),
            CompositeStep(kind="type", text=dest_path,
                          description=f"输入目标路径「{dest_path}」"),
            CompositeStep(kind="wait", wait_ms=1500,
                          description="等待路径完整写入地址栏"),
            CompositeStep(kind="key", keys="Enter",
                          description="Enter 跳转到目标文件夹"),
            CompositeStep(kind="wait", wait_ms=600,
                          description="等待资源管理器加载新目录"),
            CompositeStep(kind="vision_find",
                          intent="empty area in the file list (not on any file/icon)",
                          description="在文件列表空白区域定位（用于右键）"),
            CompositeStep(kind="click", button="right", clicks=1,
                          description="在空白区域右键，弹出右键菜单"),
            CompositeStep(kind="vision_find", intent='context menu "Paste" item',
                          description='在右键菜单中找到 "Paste" 项'),
            CompositeStep(kind="click", button="left", clicks=1,
                          description='点击 "Paste"，完成复制'),
        ],
    )


def make_file_move_context_menu(source_desc: str, dest_path: str) -> CompositePlan:
    """Move a file via right-click → Cut → navigate → Paste (shares logic)."""
    plan = make_file_copy_context_menu(source_desc, dest_path)
    plan.name = "file_move_context_menu"
    plan.description = "右键 Cut → 导航 → Paste"
    plan.reasoning = plan.reasoning.replace("复制", "移动").replace("Copy", "Cut")
    plan.steps[2] = CompositeStep(kind="vision_find", intent='context menu "Cut" item',
                                  description='在右键菜单中找到 "Cut" 项')
    plan.steps[3] = CompositeStep(kind="click", button="left", clicks=1,
                                  description='点击 "Cut"，文件已剪切到剪贴板')
    plan.steps[10] = CompositeStep(kind="vision_find", intent='context menu "Paste" item',
                                   description='在右键菜单中找到 "Paste" 项')
    plan.steps[11] = CompositeStep(kind="click", button="left", clicks=1,
                                   description='点击 "Paste"，完成移动')
    return plan


# ── Composite Executor ────────────────────────────────────────────────────────


class CompositeExecutor:
    """Executes a CompositePlan step-by-step using vision model dispatch.

    Architecture:
      - vision_find: screenshot + dispatch_vision() -> coordinates
      - click:       adapter.click(x, y)
      - type:        adapter.type_text(text)
      - key:         adapter.send_keys(keys)
      - wait:        time.sleep(wait_ms / 1000)

    Retries vision_find up to MAX_RETRIES times. Stops on first failure.
    """

    MAX_RETRIES: int = 2
    VERIFY_DELAY_MS: int = 500  # settle time after click

    def __init__(
        self,
        adapter: Any = None,  # KeyboardAdapter
        vision_model: str = "deepseek",  # "deepseek" or "claude"
        feedback_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self._adapter = adapter
        self._vision_model = vision_model
        self._feedback_callback = feedback_callback
        self._last_coords: Optional[tuple] = None  # from last vision_find

    def execute(self, plan: CompositePlan, dry_run: bool = False) -> List[Dict[str, Any]]:
        """Execute all steps in a CompositePlan sequentially.

        Returns list of {"step": int, "kind": str, "success": bool, "message": str}.
        """
        results: List[Dict[str, Any]] = []
        for i, step in enumerate(plan.steps):
            result: Dict[str, Any] = {
                "step": i,
                "kind": step.kind,
                "description": step.description,
                "success": False,
                "message": "",
            }

            if step.kind == "vision_find":
                result.update(self._exec_vision_find(step, dry_run))
            elif step.kind == "click":
                x, y = self._last_coords or (None, None)
                result.update(self._exec_click(step, x, y, dry_run))
            elif step.kind == "type":
                result.update(self._exec_type(step, dry_run))
            elif step.kind == "key":
                result.update(self._exec_key(step, dry_run))
            elif step.kind == "wait":
                result.update(self._exec_wait(step, dry_run))
            elif step.kind == "shell":
                result.update(self._exec_shell(step, dry_run))
            else:
                result["message"] = f"unknown step kind: {step.kind}"

            results.append(result)

            if self._feedback_callback:
                try:
                    self._feedback_callback(result)
                except Exception:
                    pass

            # vision_find/click failures are expected without a vision model;
            # skip them and continue to keyboard steps (type/key/wait).
            if not result["success"]:
                if step.kind in ("vision_find", "click"):
                    continue  # skip vision-dependent step, try next
                break  # hard failure on keyboard step

        return results

    def _exec_vision_find(self, step: CompositeStep, dry_run: bool) -> Dict[str, Any]:
        from .vision_executor import vision_screenshot, dispatch_vision

        if dry_run:
            return {"success": True, "message": f"[dry-run] would find: {step.intent}"}

        for attempt in range(self.MAX_RETRIES + 1):
            screenshot_result = vision_screenshot(intent=step.intent, app="", encode_b64=True)
            if not screenshot_result.ok:
                return {"success": False,
                        "message": f"screenshot failed: {screenshot_result.message}"}

            image_b64 = screenshot_result.data.get("image_b64", "")
            if not image_b64:
                return {"success": False, "message": "screenshot had no image data"}

            vision_result = dispatch_vision(
                intent=step.intent,
                screenshot_b64=image_b64,
                model=self._vision_model,
            )

            if vision_result.get("found"):
                center = vision_result.get("center")
                if center:
                    self._last_coords = (int(center[0]), int(center[1]))
                    return {
                        "success": True,
                        "message": f"found '{vision_result.get('label')}' at {center}",
                        "coords": center,
                        "bbox": vision_result.get("bbox"),
                    }

            if attempt < self.MAX_RETRIES:
                time.sleep(0.3)
                continue
            return {
                "success": False,
                "message": (
                    f"vision model could not find: {step.intent} "
                    f"(after {self.MAX_RETRIES + 1} attempts)"
                ),
            }

        return {"success": False, "message": f"vision_find exhausted: {step.intent}"}

    def _exec_click(self, step: CompositeStep, x, y, dry_run: bool) -> Dict[str, Any]:
        if dry_run:
            coord_str = f"({x},{y})" if (x is not None and y is not None) else "(from vision_find)"
            return {"success": True,
                    "message": f"[dry-run] would click {coord_str} {step.button}x{step.clicks}"}
        if x is None or y is None:
            return {"success": False, "message": "no coordinates from previous vision_find"}
        try:
            if self._adapter:
                self._adapter.click(int(x), int(y), button=step.button, clicks=step.clicks)
            time.sleep(self.VERIFY_DELAY_MS / 1000.0)
            return {"success": True,
                    "message": f"clicked ({x},{y}) {step.button}x{step.clicks}"}
        except Exception as e:
            return {"success": False, "message": f"click failed: {e}"}

    def _exec_type(self, step: CompositeStep, dry_run: bool) -> Dict[str, Any]:
        if dry_run:
            return {"success": True, "message": f"[dry-run] would type: {step.text[:30]}"}
        try:
            if self._adapter:
                self._adapter.type_text(step.text)
            return {"success": True, "message": f"typed {len(step.text)} chars"}
        except Exception as e:
            return {"success": False, "message": f"type failed: {e}"}

    def _exec_key(self, step: CompositeStep, dry_run: bool) -> Dict[str, Any]:
        if dry_run:
            return {"success": True, "message": f"[dry-run] would press: {step.keys}"}
        try:
            if self._adapter:
                self._adapter.send_keys(step.keys)
            time.sleep(0.1)
            return {"success": True, "message": f"pressed {step.keys}"}
        except Exception as e:
            return {"success": False, "message": f"key failed: {e}"}

    @staticmethod
    def _exec_wait(step: CompositeStep, dry_run: bool) -> Dict[str, Any]:
        if dry_run:
            return {"success": True, "message": f"[dry-run] would wait {step.wait_ms}ms"}
        time.sleep(max(0, step.wait_ms) / 1000.0)
        return {"success": True, "message": f"waited {step.wait_ms}ms"}

    @staticmethod
    def _exec_shell(step: CompositeStep, dry_run: bool) -> Dict[str, Any]:
        cmd = step.text or step.description
        if dry_run:
            return {"success": True, "message": f"[dry-run] would run: {cmd[:60]}"}
        try:
            import subprocess, os, re
            # Pre-check: warn if source path doesn't exist for copy/move commands
            if cmd.startswith(('copy ', 'move ', 'xcopy ', 'robocopy ')):
                m = re.search(r'"([^"]+)"', cmd)
                if m and not os.path.exists(m.group(1)):
                    return {"success": False,
                            "message": f"file not found: {m.group(1)}. Use absolute path like C:/Users/.../file.txt"}
            # start / cmd /k / powershell → visible terminal; must use Popen
            # because subprocess.run with capture_output=True hangs until the
            # spawned window closes (inherited pipe handles stay open).
            if cmd.startswith('start '):
                subprocess.Popen(cmd, shell=True)
                return {"success": True, "message": "launched visible terminal"}
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                out = r.stdout.strip()[:100]
                return {"success": True, "message": f"shell ok: {out or 'done'}"}
            else:
                err = r.stderr.strip()[:100] or r.stdout.strip()[:100]
                return {"success": False, "message": f"shell failed: {err}"}
        except Exception as e:
            return {"success": False, "message": f"shell error: {e}"}


def make_generic_composite(hint: str, text: str = "", keys: str = "") -> CompositePlan:
    """Generic vision-driven composite: find element → click → type/key.

    This is the catch-all factory for any composite step the LLM generates.
    Instead of failing with "hint not recognized", delegate to vision model.

    Args:
        hint: vision_find intent (e.g. "the Send button in Outlook")
        text: text to type after clicking (e.g. "recipient@example.com")
        keys: key combination to press after clicking (e.g. "Enter")
    """
    steps: list = [
        CompositeStep(
            kind="vision_find", intent=hint,
            description=f"在屏幕上找到「{hint[:40]}」",
        ),
        CompositeStep(
            kind="click", button="left", clicks=1,
            description="点击目标元素",
        ),
    ]
    if text:
        steps.append(CompositeStep(
            kind="type", text=text,
            description=f"输入「{text[:30]}」",
        ))
    if keys:
        steps.append(CompositeStep(
            kind="key", keys=keys,
            description=f"按下 {keys}",
        ))
    return CompositePlan(
        name="generic_composite",
        description=f"视觉定位 → 操作：{hint[:40]}",
        confidence=0.7,
        reasoning=f"通过视觉模型定位「{hint}」，然后执行操作",
        steps=steps,
    )


# 打开应用程序时的统一等待毫秒数（开始菜单搜索索引返回结果）
_OPEN_APP_SEARCH_WAIT_MS = 300


def make_open_app(app_name: str) -> CompositePlan:
    """通过开始菜单搜索统一打开应用程序：Win → 输入名称 → 等待 → Enter。

    所有「打开X」类操作（X 为应用名）均走此统一流程，不绑定 Ctrl+O。
    例外：已注册的特定目标（资源管理器=Win+E、终端=Win+R→cmd）由各自的
    精确匹配通道处理，不会走到这里。

    Args:
        app_name: 应用程序名称（如 "记事本"、"计算器"、"浏览器"）
    """
    return CompositePlan(
        name="open_app",
        description=f'开始菜单搜索并打开：「{app_name}」',
        confidence=0.90,
        reasoning=f"Win → 搜索「{app_name}」→ 等待 {_OPEN_APP_SEARCH_WAIT_MS}ms → Enter",
        steps=[
            CompositeStep(
                kind="key", keys="Win",
                description="Win 打开开始菜单搜索",
            ),
            CompositeStep(
                kind="type", text=app_name,
                description=f"输入应用名称：{app_name}",
            ),
            CompositeStep(
                kind="wait", wait_ms=_OPEN_APP_SEARCH_WAIT_MS,
                description="等待搜索结果",
            ),
            CompositeStep(
                kind="key", keys="Enter",
                description="Enter 打开首个搜索结果",
            ),
        ],
    )


def make_file_search_keyboard(pattern: str, target_path: Optional[str] = None) -> CompositePlan:
    """通过 Windows 资源管理器键盘操作查找文件（可选：找到后直接移动到目标）。

    纯键盘路径 —— 不使用任何文件系统脚本（os.walk / PowerShell）。
    完整模拟人工操作，并针对「资源管理器搜索索引未返回就继续操作」的竞态，
    在搜索执行后插入确定性等待，避免随后 ``Ctrl+A`` 选中落空。

    步序列（target_path 为 None 时执行到第 6 步，仅完成查找并选中结果）：
        1. Alt+D        → 定位源：聚焦地址栏，确认当前处于源文件夹
        2. Ctrl+E        → 聚焦搜索框（Ctrl+E 比 Ctrl+F 更可靠）
        3. type pattern  → 输入搜索关键词
        4. Enter         → 执行搜索
        5. wait 800ms    → 等待搜索索引返回结果（关键：消除加载竞态）
        6. Ctrl+A        → 选中所有搜索结果
        —— 以下仅在提供 target_path 时执行（查找并移动）——
        7. Shift+F10      → Shift+右键 打开扩展右键菜单（含「移动到文件夹」）
        8. M             → 选择「移动到文件夹(M)」
        9. type target    → 输入目标文件夹路径
       10. Enter         → 确认移动

    参数
    ----
    pattern : str
        要搜索的文件名、部分名称或扩展名。
    target_path : str, optional
        目标文件夹路径；提供时会把找到的文件移动到该路径。
    """
    steps = [
        # 1. 定位源：聚焦地址栏（确认当前处于源文件夹）
        CompositeStep(kind="key", keys="Alt+D",
                      description="Alt+D 聚焦地址栏，定位源文件夹"),
        # 2. 聚焦搜索框（Ctrl+E 在资源管理器中比 Ctrl+F 更可靠地把焦点送入搜索框）
        CompositeStep(kind="key", keys="Ctrl+E",
                      description="Ctrl+E 聚焦搜索框"),
        # 3. 输入搜索关键词
        CompositeStep(kind="type", text=pattern,
                      description=f'输入搜索关键词「{pattern}」'),
        # 4. 执行搜索
        CompositeStep(kind="key", keys="Enter",
                      description="Enter 执行搜索"),
        # 5. 等待搜索索引返回结果（关键：消除加载竞态，避免选中落空）
        CompositeStep(kind="wait", wait_ms=get_load_wait("search_index"),
                      description="等待搜索索引返回结果"),
        # 6. 把键盘焦点从搜索框移入结果列表并选中第一项
        #    （若不先进入列表，随后的 Ctrl+A 只会选中搜索框内的查询文本，
        #      这正是「搜索后没选中文件」的根因）
        CompositeStep(kind="key", keys="Down",
                      description="↓ 进入结果列表并选中第一项"),
        # 7. 选中所有搜索结果
        CompositeStep(kind="key", keys="Ctrl+A",
                      description="Ctrl+A 选中所有搜索结果"),
    ]
    if target_path:
        steps += [
            # 7. Shift+右键 → 扩展右键菜单（含「移动到文件夹」）
            CompositeStep(kind="key", keys="Shift+F10",
                          description="Shift+右键 打开扩展菜单（移动到文件夹）"),
            # 8. 选择「移动到文件夹(M)」
            CompositeStep(kind="key", keys="M",
                          description="M 选择「移动到文件夹」"),
            # 9. 输入目标路径
            CompositeStep(kind="type", text=target_path,
                          description=f'输入目标路径「{target_path}」'),
            CompositeStep(kind="wait", wait_ms=1500,
                          description="等待路径完整写入地址栏"),
            # 10. 确认移动
            CompositeStep(kind="key", keys="Enter",
                          description="Enter 确认移动"),
        ]
    return CompositePlan(
        name="file_search_keyboard",
        description=(
            f'用资源管理器搜索文件：「{pattern}」'
            + (f'并移动到「{target_path}」' if target_path else '')
        ),
        confidence=0.85,
        reasoning=(
            f"在源文件夹（Alt+D 聚焦地址栏）通过搜索框输入「{pattern}」"
            + ("，选中全部结果后经由扩展右键菜单移动到「{target_path}」。"
               if target_path else
               "，选中全部结果供后续打开/复制。")
            + "全程纯键盘，并在搜索后插入等待以规避加载竞态。"
        ),
        steps=steps,
    )


def make_file_search_copy(pattern: str) -> CompositePlan:
    """通过 Windows 资源管理器搜索文件，并复制首个匹配项到剪贴板。

    纯键盘路径，用于「搜到文件后直接复制」的场景。整条链：

        Win+E         → 打开 / 聚焦资源管理器窗口
        Ctrl+E         → 聚焦搜索框
        type pattern   → 输入搜索关键词
        Enter          → 执行搜索
        wait 800ms     → 等待搜索索引返回结果（search_index，可运行时覆盖）
        Down           → ↓ 进入结果列表并选中第一项（同时高亮选中）
        Ctrl+C         → 复制选中项到剪贴板

    设计说明
    --------
    * 用 ``↓`` 而非 ``Enter`` 进入结果列表：Enter 会直接**打开**文件而非选中。
    * ``↓`` 按下后第一项即被**选中（蓝底高亮）**，无需额外 Space 勾选。
    * Windows 的复选框功能（"使用复选框选择项目"）默认关闭，Space 在无复选框
      的文件列表中没有效果，去掉该步避免无操作或误触发。
    * 搜索执行后的 ``wait`` 步复用 ``search_index`` 加载延迟（默认 800ms，
      可用 ``set_load_waits`` / 环境变量覆盖）。
    """
    steps = [
        CompositeStep(kind="key", keys="Win+E",
                      description="Win+E 打开 / 聚焦资源管理器"),
        CompositeStep(kind="key", keys="Ctrl+E",
                      description="Ctrl+E 聚焦搜索框"),
        CompositeStep(kind="type", text=pattern,
                      description=f'输入搜索关键词「{pattern}」'),
        CompositeStep(kind="key", keys="Enter",
                      description="Enter 执行搜索"),
        # 搜索执行后等待索引返回（消除加载竞态，避免后续按键落空）
        CompositeStep(kind="wait", wait_ms=get_load_wait("search_index"),
                      description="等待搜索索引返回结果"),
        # 用 Down 进入结果列表并选中第一项（↓ 即选中，无需额外 Space）
        CompositeStep(kind="key", keys="Down",
                      description="↓ 进入结果列表并选中第一项"),
        # 复制选中项到剪贴板（此时第一项已被 ↓ 选中）
        CompositeStep(kind="key", keys="Ctrl+C",
                      description="Ctrl+C 复制选中项到剪贴板"),
    ]
    return CompositePlan(
        name="file_search_copy",
        description=f'搜索并复制「{pattern}」',
        confidence=0.85,
        reasoning=(
            f"Win+E 打开资源管理器 → 搜索框输入「{pattern}」并回车搜索，"
            "等待结果加载后 ↓ 进入结果列表（同时选中第一项）、Ctrl+C 复制。"
            "全程纯键盘，并在搜索后插入等待以规避加载竞态。"
        ),
        steps=steps,
    )


def make_find_and_copy(file_path: str) -> CompositePlan:
    """在资源管理器中先导航到目录、再按文件名搜索并复制。

    两段式工作流：
        第一段：地址栏导航到文件所在目录
            Win+E -> 打开 / 聚焦资源管理器窗口
            wait 800ms -> 等待窗口渲染
            Alt+D -> 聚焦地址栏
            Ctrl+A -> 选中地址栏原有内容
            type folder -> 输入目录
            wait 800ms -> 等待地址解析
            Enter -> 跳转到该目录
        第二段：在当前目录内按文件名搜索并复制
            Ctrl+E -> 聚焦搜索框
            type filename -> 输入文件名
            Enter -> 执行搜索
            wait 800ms -> 等待搜索索引返回
            Space -> 勾选搜索结果项
            Ctrl+C -> 复制选中项到剪贴板

    说明：
      - 资源管理器搜索框按文件名/内容在当前目录内匹配，
        直接输入完整路径通常搜不到，故先导航到目录再用文件名搜索。
      - 若只给了文件名（无目录成分），跳过第一段直接搜索。
      - 末尾 800ms 等待用于规避搜索索引未返回就继续操作的竞态。
    """
    folder, filename = os.path.split(file_path)
    has_folder = bool(folder)

    steps: List[CompositeStep] = []
    if has_folder:
        steps += [
            CompositeStep(kind="key", keys="Win+E",
                          description="Win+E 打开 / 聚焦资源管理器"),
            CompositeStep(kind="wait", wait_ms=800,
                          description="等待窗口渲染"),
            CompositeStep(kind="key", keys="Alt+D",
                          description="Alt+D 聚焦地址栏"),
            CompositeStep(kind="key", keys="Ctrl+A",
                          description="Ctrl+A 选中地址栏原有内容"),
            CompositeStep(kind="type", text=folder,
                          description=f"输入目录：{folder}"),
            CompositeStep(kind="wait", wait_ms=800,
                          description="等待地址解析"),
            CompositeStep(kind="key", keys="Enter",
                          description="Enter 跳转到该目录"),
        ]

    steps += [
        CompositeStep(kind="key", keys="Ctrl+E",
                      description="Ctrl+E 聚焦搜索框"),
        CompositeStep(kind="type", text=filename,
                      description=f"输入文件名：{filename}"),
        CompositeStep(kind="key", keys="Enter",
                      description="Enter 执行搜索"),
        CompositeStep(kind="wait", wait_ms=800,
                      description="等待搜索索引返回结果"),
        CompositeStep(kind="key", keys="Space",
                      description="Space 勾选搜索结果项"),
        CompositeStep(kind="key", keys="Ctrl+C",
                      description="Ctrl+C 复制选中项到剪贴板"),
    ]

    desc = (f"导航到 {folder} 并搜索 {filename} 复制"
            if has_folder else f"搜索 {filename} 并复制")
    return CompositePlan(
        name="find_and_copy",
        description=desc,
        confidence=0.85,
        reasoning=(
            (f"先导航到目录 {folder}，" if has_folder else "")
            + f"搜索框输入文件名 {filename} 并回车搜索，"
            "等待结果加载后 Space 勾选、Ctrl+C 复制。纯键盘，搜索后插入 800ms 等待规避竞态。"
        ),
        steps=steps,
    )

def make_type_folder_path(folder_path: str) -> CompositePlan:
    """Type a folder path into any text input (Explorer address bar, dialog path field, etc.).

    Pure keyboard path — no filesystem scripts. Simulates what a human does:

        1. Ctrl+A      → select all existing text in the field
        2. type path    → type the folder path
        3. wait 1500ms  → wait for the path to fully register in the field
        4. Enter        → confirm / navigate

    Designed for the *navigate* use case, not search:
      - Folder path starts with drive letter (C:, D:) or UNC (\\server)
      - Does NOT execute Search — just types the path string

    Works in:
      - File Explorer address bar (focus with Ctrl+L)
      - Common File Dialog (Ctrl+L focuses the path field)
      - Run dialog (Win+R)

    Args:
        folder_path: folder path to type, e.g. "C:\\Users\\Deng2\\Documents"
    """
    return CompositePlan(
        name="type_folder_path",
        description=f'输入文件夹路径：「{folder_path}」',
        confidence=0.85,
        reasoning=(
            f"全选当前输入框后输入路径「{folder_path}」并回车确认。"
            "适用于资源管理器地址栏、文件对话框路径字段、运行对话框等。"
        ),
        steps=[
            # 1. Select any existing text in the focused field
            CompositeStep(kind="key", keys="Ctrl+A",
                          description="Ctrl+A 全选清空已有内容"),
            # 2. Type the folder path
            CompositeStep(kind="type", text=folder_path,
                          description=f'输入路径「{folder_path}」'),
            # 3. 等待路径完整写入输入框后，再按 Enter（规避输入竞态）
            CompositeStep(kind="wait", wait_ms=1500,
                          description="等待路径完整写入输入框"),
            # 4. Enter to confirm / navigate
            CompositeStep(kind="key", keys="Enter",
                          description="Enter 确认并跳转"),
            # 等待目录内容加载（加载竞态：回车跳转后内容尚未就绪）
            CompositeStep(kind="wait", wait_ms=get_load_wait("folder_nav"),
                          description="等待文件夹内容加载"),
        ],
    )


def make_open_folder(folder_path: str) -> CompositePlan:
    """Open File Explorer and navigate to a specific folder.

    Full workflow: open Explorer → focus search box → type path → Enter.

    Step-by-step (mirrors a human's exact actions):
        1. Win+E       → open File Explorer (or focus existing window)
        2. wait 800ms  → let window render
        3. Ctrl+E      → focus search box
        4. Ctrl+A      → clear any leftover search text
        5. type path    → type the full folder path
        6. wait 1500ms  → wait for path to register
        7. Enter        → execute
        8. wait 600ms   → let results load

    Differs from make_type_folder_path: this ALSO opens Explorer first.
    Use when there's no existing Explorer / dialog window.

    Args:
        folder_path: absolute folder path, e.g. "C:\\Users\\Deng2\\Documents"
    """
    return CompositePlan(
        name="open_folder",
        description=f'打开文件夹：「{folder_path}」',
        confidence=0.90,
        reasoning=(
            f"Win+E 打开资源管理器 → Ctrl+E 聚焦搜索框 → 输入路径「{folder_path}」 → 回车。"
        ),
        steps=[
            # 1. Open / focus File Explorer
            CompositeStep(kind="key", keys="Win+E",
                          description="Win+E 打开资源管理器"),
            # 2. Wait for window to fully render
            CompositeStep(kind="wait", wait_ms=get_load_wait("explorer_open"),
                          description="等待资源管理器加载"),
            # 3. Ctrl+E → focus search box
            CompositeStep(kind="key", keys="Ctrl+E",
                          description="Ctrl+E 聚焦搜索框"),
            # 4. Clear existing search text
            CompositeStep(kind="key", keys="Ctrl+A",
                          description="Ctrl+A 清空搜索框"),
            # 5. Type folder path
            CompositeStep(kind="type", text=folder_path,
                          description=f'输入路径「{folder_path}」'),
            # 6. 等待路径完整写入后再 Enter
            CompositeStep(kind="wait", wait_ms=1500,
                          description="等待路径完整写入输入框"),
            # 7. Enter
            CompositeStep(kind="key", keys="Enter",
                          description="Enter 触发搜索/跳转"),
            # 7. Wait for results to load
            CompositeStep(kind="wait", wait_ms=get_load_wait("folder_nav"),
                          description="等待文件夹内容加载"),
        ],
    )


def make_open_folder_navigate(folder_hint: str, target_path: str) -> CompositePlan:
    """Route composite "open/打开/导航" hints to keyboard-only folder navigation.

    兼容 GoalPlanner 的 composite 路由表。优先用 Win+E 键盘导航而非视觉方案。
    """
    if target_path and target_path != "C:\\":
        return make_open_folder(folder_path=target_path)
    # Fallback: use the original description for the composite
    return make_open_folder(folder_path="C:\\Users")


def make_dialog_open_path(folder_path: str) -> CompositePlan:
    """Type a folder path into a Common File Dialog (e.g. Open/Save dialog).

    Designed for the case where a file dialog is ALREADY open and the user
    wants to jump to a specific folder. Mirrors how a human would do it:

        1. Ctrl+E      → focus the dialog's search box
        2. Ctrl+A      → clear existing text
        3. type path    → type folder path
        4. Enter        → confirm

    Does NOT press Win+E first because the dialog is already open.

    Args:
        folder_path: absolute folder path, e.g. "D:\\projects"
    """
    return CompositePlan(
        name="dialog_open_path",
        description=f'在对话框输入路径：「{folder_path}」',
        confidence=0.85,
        reasoning=(
            f"Ctrl+E 聚焦对话框搜索框 → 清空 → 输入「{folder_path}」 → 回车。"
            "假设文件对话框已打开。"
        ),
        steps=[
            CompositeStep(kind="key", keys="Ctrl+E",
                          description="Ctrl+E 聚焦对话框搜索框"),
            CompositeStep(kind="key", keys="Ctrl+A",
                          description="Ctrl+A 清空已有内容"),
            CompositeStep(kind="type", text=folder_path,
                          description=f'输入路径「{folder_path}」'),
            CompositeStep(kind="key", keys="Enter",
                          description="Enter 触发搜索/跳转"),
            CompositeStep(kind="wait", wait_ms=get_load_wait("dialog_load"),
                          description="等待文件夹列表加载"),
        ],
    )


# ═══════════════════════════════════════════════════════════════════════
# Terminal-based composites — `kind="shell"` executes via subprocess,
# bypassing keyboard layout issues with special chars ($ | \\ [ ] etc.)
# ═══════════════════════════════════════════════════════════════════════

import shlex


def _escape_cmd(text: str) -> str:
    """Escape special CMD characters so the string is safe inside a
    ``cmd /k "..."`` double-quoted command.  ``^`` is the CMD escape char."""
    for ch in "()&|<>^":
        text = text.replace(ch, f"^{ch}")
    return text


def _escape_ps_single(text: str) -> str:
    """Escape a string for use inside a PowerShell single-quoted literal.
    The ONLY character that needs escaping in a PS single-quoted string
    is another single quote (doubled)."""
    return text.replace("'", "''")


def make_file_search_terminal(
    pattern: str,
    search_root: str = r"%USERPROFILE%\Desktop",
) -> CompositePlan:
    """Search files by opening a visible CMD window running ``dir /s /b``.

    Uses ``kind="shell"`` → ``subprocess.run(…, shell=True)`` to launch
    ``start cmd /k …``, which opens a terminal that stays open after the
    search completes.  Zero keyboard typing — no layout issues.

    Args:
        pattern:      file / folder name (supports ``*`` wildcards).
        search_root:  directory to start the recursive search from.
    """
    if "*" not in pattern and "?" not in pattern:
        pattern = f"*{pattern}*"

    safe_pattern = _escape_cmd(pattern)
    safe_root = _escape_cmd(search_root)

    # start "title" cmd /k "commands" opens a visible, persistent window
    cmd = (
        f'start "NL2Shortcut Search" cmd /k '
        f'"cd /d {safe_root} && dir /s /b {safe_pattern} && echo. && echo ── Done ──"'
    )

    return CompositePlan(
        name="file_search_terminal",
        description=f'终端搜索「{pattern}」',
        confidence=0.95,
        reasoning=f"start cmd /k → cd /d {search_root} → dir /s /b {pattern}",
        steps=[
            CompositeStep(kind="shell", text=cmd,
                          description="启动 CMD 窗口搜索文件（结果在终端中显示）"),
        ],
    )


def _wrap_wildcards(pattern: str) -> str:
    """Wrap a search pattern in ``*`` wildcards if none are present,
    and escape double-quotes."""
    p = pattern.strip()
    if "*" not in p and "?" not in p:
        p = f"*{p}*"
    return p.replace('"', '""')


def _ps_copy_move_cmd(
    source_pattern: str,
    dest_pattern: str,
    search_root: str,
    verb: str,  # "Copy-Item" or "Move-Item"
) -> str:
    """Build a PowerShell one-liner that finds source + dest, then acts.

    Returns a ``start powershell …`` command string suitable for
    ``kind="shell"`` (``subprocess.run``).  PowerShell single-quote
    literals keep CMD from mangling ``$d`` and pipes.
    """
    src = _escape_ps_single(_wrap_wildcards(source_pattern))
    dst = _escape_ps_single(_wrap_wildcards(dest_pattern))
    root = _escape_ps_single(search_root)

    # The PowerShell script (single-quoted for safety — no CMD interpolation)
    ps_script = (
        f"$d=(ls '{root}' -r -fi '{dst}')[0].FullName;"
        f" ls '{root}' -r -fi '{src}' |"
        f" {verb} -dest \\\"$d\\\\\\\" -Fo;"
        f" Write-Host '── Done ──'"
    )

    # Wrap in `start powershell -NoExit -Command "& { ... }"`.
    # The inner script is double-quoted for CMD, with single quotes for PS.
    cmd = (
        f'start "NL2Shortcut" powershell -NoExit -Command '
        f'"& {{ {ps_script} }}"'
    )
    return cmd


def make_terminal_copy_to_folder(
    source_pattern: str,
    dest_pattern: str,
    search_root: str = r"$env:USERPROFILE\Desktop",
) -> CompositePlan:
    """PowerShell find + Copy-Item, launched in a visible terminal.

    Single ``kind="shell"`` step — no keyboard typing.
    """
    cmd = _ps_copy_move_cmd(source_pattern, dest_pattern, search_root, "Copy-Item")
    return CompositePlan(
        name="terminal_copy_to_folder",
        description=f'终端复制「{source_pattern}」→「{dest_pattern}」',
        confidence=0.93,
        reasoning=f"start powershell → find + Copy-Item",
        steps=[
            CompositeStep(kind="shell", text=cmd,
                          description="PowerShell 查找+复制（结果在终端中显示）"),
        ],
    )


def make_terminal_move_to_folder(
    source_pattern: str,
    dest_pattern: str,
    search_root: str = r"$env:USERPROFILE\Desktop",
) -> CompositePlan:
    """PowerShell find + Move-Item, launched in a visible terminal.

    Single ``kind="shell"`` step — no keyboard typing.
    """
    cmd = _ps_copy_move_cmd(source_pattern, dest_pattern, search_root, "Move-Item")
    return CompositePlan(
        name="terminal_move_to_folder",
        description=f'终端移动「{source_pattern}」→「{dest_pattern}」',
        confidence=0.93,
        reasoning=f"start powershell → find + Move-Item",
        steps=[
            CompositeStep(kind="shell", text=cmd,
                          description="PowerShell 查找+移动（结果在终端中显示）"),
        ],
    )


# ── 目标文件夹名归一 ───────────────────────────────────────────────
# 把「桌面 / 文档 / 下载 / 图片 …」等口语化目标名解析为真实文件系统路径，
# 让纯键盘导航（地址栏输入路径 / 移动对话框输入路径）能精准跳转到目标。
_DEST_ALIASES = {
    "桌面": "Desktop", "desktop": "Desktop",
    "文档": "Documents", "documents": "Documents", "我的文档": "Documents",
    "下载": "Downloads", "downloads": "Downloads",
    "图片": "Pictures", "pictures": "Pictures", "照片": "Pictures",
    "音乐": "Music", "music": "Music",
    "视频": "Videos", "videos": "Videos",
}


def _resolve_dest_folder(name: str) -> str:
    """把口语化目标名（桌面 / 下载 …）解析为真实路径；未知的按原样返回。"""
    name = (name or "").strip().strip('"').strip("'").strip()
    if not name:
        return os.path.join(os.path.expanduser("~"), "Desktop")
    en = _DEST_ALIASES.get(name) or _DEST_ALIASES.get(name.lower())
    if en:
        return os.path.join(os.path.expanduser("~"), en)
    return name


def _build_shell_file_op(verb: str, source_pattern: str,
                         resolved: str) -> str:
    """拼装在真实 cmd 窗口里执行的 move/copy 命令（无 cmd /c 包裹）。

    命令形如：`move C:\\path\\file.txt C:\\target\\folder\\`

    路径格式约定（按用户给定格式，尽量原样保留，方便指定要传输的文件）：
      - 源/目标路径按字面写入，目标末尾的 `\\`（表示目录）予以保留。
      - 仅当路径含空格时才补引号；此时若末尾是 `\\` 则再补一个 `\\`，
        避免 cmd 把 `\\"` 误解析为转义引号。
    """
    def _q(p: str) -> str:
        if " " in p and not p.startswith('"'):
            if p.endswith("\\"):
                p = p + "\\"
            return f'"{p}"'
        return p
    return f"{verb} {_q(source_pattern)} {_q(resolved)}"


def make_move_to_folder(source_pattern: str, dest_pattern: str) -> CompositePlan:
    """把文件「移动到」目标文件夹 —— 打开真实 cmd 窗口后执行 move。

    工作流（按需求）：
      Win+R → 输入 `cmd` → Enter（打开命令行窗口）→ 输入 `move 源 目标` → Enter 执行。
    不插入任何等待：依赖键鼠注入的串行执行，各步骤按顺序立即衔接。

    说明：
      - 先经「运行」对话框打开真正的 cmd 窗口，再在窗口里直接敲 move 命令，
        无需 `cmd /c` 包裹。
    """
    resolved = _resolve_dest_folder(dest_pattern)
    command = _build_shell_file_op("move", source_pattern, resolved)
    steps = [
        CompositeStep(kind="key", keys="Win+R",
                      description="Win+R 打开「运行」对话框"),
        CompositeStep(kind="type", text="cmd",
                      description="输入 cmd"),
        CompositeStep(kind="key", keys="Enter",
                      description="Enter 打开 cmd 命令行窗口"),
        CompositeStep(kind="type", text=command,
                      description=f'输入命令：{command}'),
        CompositeStep(kind="key", keys="Enter",
                      description="Enter 执行移动"),
    ]
    return CompositePlan(
        name="move_to_folder",
        description=f'cmd 窗口 move：「{source_pattern}」→「{dest_pattern}」',
        confidence=0.85,
        reasoning=(
            f"Win+R → 输入 cmd → 回车打开命令行 → 输入 `{command}`"
            f" → 回车执行移动。避开资源管理器逐键导航的竞态。"
        ),
        steps=steps,
    )


def make_copy_to_folder(source_pattern: str, dest_pattern: str) -> CompositePlan:
    """把文件「复制到」目标文件夹 —— 打开真实 cmd 窗口后执行 copy。

    工作流（按需求）：
      Win+R → 输入 `cmd` → Enter（打开命令行窗口）→ 输入 `copy 源 目标` → Enter 执行。
    不插入任何等待：依赖键鼠注入的串行执行，各步骤按顺序立即衔接。

    说明：
      - 先经「运行」对话框打开真正的 cmd 窗口，再在窗口里直接敲 copy 命令，
        无需 `cmd /c` 包裹。
    """
    resolved = _resolve_dest_folder(dest_pattern)
    command = _build_shell_file_op("copy", source_pattern, resolved)
    steps = [
        CompositeStep(kind="key", keys="Win+R",
                      description="Win+R 打开「运行」对话框"),
        CompositeStep(kind="type", text="cmd",
                      description="输入 cmd"),
        CompositeStep(kind="key", keys="Enter",
                      description="Enter 打开 cmd 命令行窗口"),
        CompositeStep(kind="type", text=command,
                      description=f'输入命令：{command}'),
        CompositeStep(kind="key", keys="Enter",
                      description="Enter 执行复制"),
    ]
    return CompositePlan(
        name="copy_to_folder",
        description=f'cmd 窗口 copy：「{source_pattern}」→「{dest_pattern}」',
        confidence=0.85,
        reasoning=(
            f"Win+R → 输入 cmd → 回车打开命令行 → 输入 `{command}`"
            f" → 回车执行复制。避开资源管理器逐键导航的竞态。"
        ),
        steps=steps,
    )

