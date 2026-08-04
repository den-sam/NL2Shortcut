"""感知层抽象 —— 可插拔 UI 感知提供者（融合策略①的核心模块）。

提供统一的 UI 感知接口，支持三种 Provider：

  UiaProvider      ← 结构化 UI 树（优先，UIA/AT-SPI 协议）
  LightProvider     ← 窗口/进程指纹（兜底，复用 context.py）
  VisionProvider    ← 截图+视觉模型（合规兜底，受 compliance_mode 控制）

PerceptionStack 按 "UIA → Light → Vision" 链式降级，
产出统一的 UIState 快照，供 ContextStore 消费。

设计约定
────────
- 每个 Provider 实现 snapshot() → Optional[UIState]。
- 返回 None 表示"我不行，让下一个试试"。
- UIA 未实现时返回 None（自动降级到 Light）。
- VisionProvider 在 compliance_mode=False 时返回 None（合规闸门）。
"""

from __future__ import annotations

import hashlib
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any


# ═══════════════════════════════════════════════════════════════════════
# UIState — 统一 UI 快照（所有 Provider 的输出格式）
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class UiNode:
    """结构化 UI 树中的一个节点。

    对应 UIA 的 ControlType / Name / RuntimeId / BoundingRect / Patterns。
    也兼容 Light（窗口级单节点）和 Vision（截图描述）的简化表达。
    """

    node_id: str = ""                # 唯一标识（UIA runtime_id 或 hash）
    name: str = ""                   # 控件名称 / AutomationId
    control_type: str = ""           # UIA ControlType 名："Button", "Edit", "Window"...
    role: str = ""                   # 可访问性角色（button / textbox / menuitem...）
    value: str = ""                  # 当前值（文本框内容、标签文本等）
    state: str = ""                  # "focused" | "enabled" | "disabled" | "checked" | ...

    # 几何
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    # 能力
    patterns: List[str] = field(default_factory=list)  # ["invoke", "value", "toggle", ...]
    keyboard_shortcut: str = ""      # 控件自带快捷键（如 Alt+S）

    # 层级
    children: List["UiNode"] = field(default_factory=list)
    parent_id: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "name": self.name,
            "control_type": self.control_type,
            "role": self.role,
            "value": self.value,
            "state": self.state,
            "x": self.x, "y": self.y, "width": self.width, "height": self.height,
            "patterns": self.patterns,
            "keyboard_shortcut": self.keyboard_shortcut,
            "children": [c.to_dict() for c in self.children],
        }

    def find_by_name(self, name: str) -> Optional["UiNode"]:
        """遍历子树查找匹配名称的节点。"""
        if name.lower() in self.name.lower():
            return self
        for child in self.children:
            found = child.find_by_name(name)
            if found is not None:
                return found
        return None

    def find_focused(self) -> Optional["UiNode"]:
        """查找当前焦点节点。"""
        if self.state == "focused":
            return self
        for child in self.children:
            found = child.find_focused()
            if found is not None:
                return found
        return None

    def count_nodes(self) -> int:
        """递归统计节点数。"""
        return 1 + sum(c.count_nodes() for c in self.children)


@dataclass
class UIState:
    """统一 UI 快照——ContextStore 的感知输入。

    无论哪个 Provider 产出，都统一为此结构。
    """

    # ── Provider 信息 ──
    source: str = ""                 # "uia" | "light" | "vision" | "none"
    provider_name: str = ""          # 具体 Provider 类名

    # ── 窗口级信息 ──
    app_name: str = ""               # 友好名："vscode", "chrome", ...
    window_title: str = ""           # 原始窗口标题
    process_name: str = ""           # 进程名："Code.exe", "chrome.exe"
    platform: str = ""               # "windows" | "macos" | "linux"

    # ── UI 树 ──
    root: Optional[UiNode] = None    # UI 树根节点（UIA 时有值）
    focus: Optional[UiNode] = None   # 当前焦点节点
    node_count: int = 0              # 节点总数

    # ── 文本上下文 ──
    clipboard_text: str = ""         # 剪贴板内容
    selected_text: str = ""          # 当前选中文本
    visible_text: str = ""           # 可见区域文本（OCR/截图提取）

    # ── 元数据 ──
    snapshot_elapsed_ms: float = 0.0 # 快照耗时
    error: str = ""                  # 错误信息（source="none"时有值）

    def to_dict(self) -> dict:
        d = {
            "source": self.source,
            "provider_name": self.provider_name,
            "app_name": self.app_name,
            "window_title": self.window_title,
            "process_name": self.process_name,
            "platform": self.platform,
            "node_count": self.node_count,
            "snapshot_elapsed_ms": round(self.snapshot_elapsed_ms, 2),
        }
        if self.root:
            d["root"] = self.root.to_dict()
        if self.focus:
            d["focus"] = {"name": self.focus.name, "control_type": self.focus.control_type,
                          "role": self.focus.role, "patterns": self.focus.patterns}
        if self.error:
            d["error"] = self.error
        return d


# ═══════════════════════════════════════════════════════════════════════
# PerceptionProvider — 抽象基类
# ═══════════════════════════════════════════════════════════════════════

class PerceptionProvider(ABC):
    """可插拔感知提供者基类。

    子类实现 snapshot() 返回 UIState，返回 None 即降级到下一个 Provider。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider 名称（用于日志/降级链跟踪）。"""
        ...

    @abstractmethod
    def snapshot(self) -> Optional[UIState]:
        """采集当前 UI 快照。返回 None 触发降级。"""
        ...

    def available(self) -> bool:
        """此 Provider 是否就绪（不抛异常的可选检查）。"""
        return True


# ═══════════════════════════════════════════════════════════════════════
# LightProvider — 轻量窗口指纹（当前 context.py 的包装）
# ═══════════════════════════════════════════════════════════════════════

class LightProvider(PerceptionProvider):
    """轻量感知——仅获取窗口标题 + 进程名 + 应用指纹。

    这是最稳定的降级方案（零外部依赖），复用 context.py 的 detect_context()。
    """

    @property
    def name(self) -> str:
        return "LightProvider"

    def snapshot(self) -> Optional[UIState]:
        """采集窗口指纹快照。"""
        t0 = time.perf_counter()
        try:
            from .context import detect_context
            app_ctx = detect_context()

            title = app_ctx.window_title or ""
            process = app_ctx.process_name or ""
            app_name = app_ctx.app_name or ""

            # 构建单节点 UI 树（窗口级别）
            root = UiNode(
                node_id=f"window:{app_name}",
                name=title or app_name,
                control_type="Window",
                role="window",
                state="enabled",
            )

            elapsed = (time.perf_counter() - t0) * 1000
            return UIState(
                source="light",
                provider_name=self.name,
                app_name=app_name,
                window_title=title,
                process_name=process,
                platform=app_ctx.platform,
                root=root,
                focus=root,
                node_count=1,
                snapshot_elapsed_ms=elapsed,
            )
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return UIState(
                source="none",
                provider_name=self.name,
                snapshot_elapsed_ms=elapsed,
                error=str(e),
            )


# ═══════════════════════════════════════════════════════════════════════
# UiaProvider — 结构化 UI 树（优先，UIA / AT-SPI）
# ═══════════════════════════════════════════════════════════════════════

# 可交互控件类型 —— 映射 uiautomation ControlTypeName → 简化角色名
_INTERACTIVE_CONTROLS = {
    "ButtonControl":      "button",
    "EditControl":        "textbox",
    "HyperlinkControl":   "link",
    "ComboBoxControl":    "combobox",
    "ListControl":        "list",
    "ListItemControl":    "listitem",
    "TreeControl":        "tree",
    "TreeItemControl":    "treeitem",
    "MenuControl":        "menu",
    "MenuItemControl":    "menuitem",
    "TabControl":         "tab",
    "TabItemControl":     "tabitem",
    "CheckBoxControl":    "checkbox",
    "RadioButtonControl": "radio",
    "SliderControl":      "slider",
    "SpinnerControl":     "spinner",
    "SplitButtonControl": "splitbutton",
    "ToggleButtonControl":"togglebutton",
    "GroupControl":       "group",
    "ToolBarControl":     "toolbar",
    "MenuBarControl":     "menubar",
    "DataGridControl":    "datagrid",
    "DataItemControl":    "dataitem",
    "ProgressBarControl": "progressbar",
    "ScrollBarControl":   "scrollbar",
    "StatusBarControl":   "statusbar",
    "TitleBarControl":    "titlebar",
    "ToolTipControl":     "tooltip",
    "PaneControl":        "pane",
    "HeaderControl":      "header",
    "HeaderItemControl":  "headeritem",
    "ThumbControl":       "thumb",
    "CalendarControl":    "calendar",
}

# 可交互控件类型（用于筛选；不在列表中的跳过不生成节点）
_INTERACTIVE_SET = frozenset(_INTERACTIVE_CONTROLS.keys())

# 控件支持的 Pattern（模式）名映射
_PATTERN_MAP = {
    "InvokePattern":       "invoke",
    "ValuePattern":        "value",
    "TogglePattern":       "toggle",
    "ExpandCollapsePattern": "expand_collapse",
    "SelectionPattern":    "select",
    "SelectionItemPattern": "select_item",
    "ScrollPattern":       "scroll",
    "RangeValuePattern":   "range_value",
    "TextPattern":         "text",
    "WindowPattern":       "window",
    "DockPattern":         "dock",
    "TransformPattern":    "transform",
    "GridPattern":         "grid",
    "GridItemPattern":     "grid_item",
    "TablePattern":        "table",
    "TableItemPattern":    "table_item",
    "MultipleViewPattern": "multiple_view",
    "VirtualizedItemPattern": "virtualized_item",
    "LegacyIAccessiblePattern": "legacy_iaccessible",
    "DragPattern":         "drag",
    "DropTargetPattern":   "drop_target",
}


class UiaProvider(PerceptionProvider):
    """UIA 结构化感知 —— 通过 uiautomation 库获取真实 UI 树。

    Windows 上需要 `pip install uiautomation`。
    若未安装或非 Windows 平台，自动回退到 None 触发降级链。

    流程：
      1. 获取前台窗口控件（GetForegroundControl）
      2. 递归遍历子树，筛选 IsControlElement=True 的节点
      3. 仅保留可交互类型（Button / Edit / ComboBox 等）
      4. 最大深度 MAX_DEPTH=12，最大节点数 MAX_NODES=500
      5. 构建 UiNode 树 → 填充 UIState

    典型调用开销：Windows 上 50-200ms（取决于 UI 复杂度）。
    """

    MAX_DEPTH = 12       # 最大递归深度
    MAX_NODES = 500      # 最大收集节点数

    def __init__(self):
        self._enabled = True   # 默认启用；若 uiautomation 不可用则在 snapshot() 降级
        self._auto = None      # uiautomation 模块引用（惰性加载）

    @property
    def name(self) -> str:
        return "UiaProvider"

    def available(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def snapshot(self) -> Optional[UIState]:
        """采集 UIA 结构化 UI 树快照。

        返回 None 表示降级到下一个 Provider（LightProvider）。

        优先使用 Rust 原生模块 (~5-18ms)，未安装时回退到 Python uiautomation。
        """
        if not self._enabled:
            return None

        t0 = time.perf_counter()

        # ── 优先：C++ 原生 UIA 引擎 ──────────────────────────────
        try:
            from .native_loader import native as _nat
            raw = _nat.uia_snapshot(max_depth=self.MAX_DEPTH, max_nodes=self.MAX_NODES)
            if raw and raw.get("error") is None:
                return self._native_dict_to_uistate(raw, t0)
        except Exception:
            pass  # DLL 未编译或调用失败 → 回退

        # ── 回退：Python uiautomation ─────────────────────────────
        try:
            # 惰性导入 uiautomation（仅在 Windows 上有效）
            if self._auto is None:
                try:
                    import uiautomation as auto_lib
                    self._auto = auto_lib
                except ImportError:
                    # uiautomation 未安装 → 自动降级
                    return None

            # 获取前台窗口
            foreground = self._auto.GetForegroundControl()
            if foreground is None:
                return None

            # 递归构建 UiNode 树
            collected = [0]  # 用 list 做可变计数器
            root_node = self._build_node(foreground, depth=0, collected=collected)

            # 焦点节点
            focus_node = None
            for c in self._auto.WalkControl(foreground, maxDepth=6):
                if hasattr(c, 'HasKeyboardFocus') and c.HasKeyboardFocus:
                    focus_node = self._node_to_simple(c)
                    break

            # 窗口信息
            app_name = self._friendly_app_name(root_node.name, foreground)
            window_title = root_node.name or ""
            process_name = getattr(foreground, 'ProcessName', '') or ""

            elapsed = (time.perf_counter() - t0) * 1000
            return UIState(
                source="uia",
                provider_name=self.name,
                app_name=app_name,
                window_title=window_title,
                process_name=process_name,
                platform="windows",
                root=root_node,
                focus=focus_node,
                node_count=collected[0],
                snapshot_elapsed_ms=elapsed,
            )

        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            # 异常也触发降级，而非直接报错
            return UIState(
                source="none",
                provider_name=self.name,
                snapshot_elapsed_ms=elapsed,
                error=str(e),
            )

    def _native_dict_to_uistate(self, raw: dict, t0: float) -> UIState:
        """将 Rust uia_snapshot() 返回的 dict 转为 UIState。"""
        elapsed = (time.perf_counter() - t0) * 1000

        # 解析根节点树
        root = self._dict_to_uinode(raw.get("root", {}))

        # 解析焦点节点
        focus = None
        focus_raw = raw.get("focus")
        if focus_raw:
            focus = UiNode(
                node_id="",
                name=focus_raw.get("name", ""),
                control_type=focus_raw.get("control_type", ""),
                role=focus_raw.get("role", ""),
                state=focus_raw.get("state", "focused"),
            )

        return UIState(
            source="uia",
            provider_name=self.name,
            app_name=raw.get("app_name", ""),
            window_title=raw.get("window_title", ""),
            process_name=raw.get("process_name", ""),
            platform="windows",
            root=root,
            focus=focus,
            node_count=raw.get("node_count", 0),
            snapshot_elapsed_ms=elapsed,
        )

    @staticmethod
    def _dict_to_uinode(d: dict) -> Optional[UiNode]:
        """递归将 dict 转回 UiNode 树。"""
        if not d:
            return None
        node = UiNode(
            node_id="",
            name=d.get("name", ""),
            control_type=d.get("control_type", ""),
            role=d.get("role", ""),
            value=d.get("value", ""),
            state=d.get("state", ""),
            x=d.get("x", 0),
            y=d.get("y", 0),
            width=d.get("width", 0),
            height=d.get("height", 0),
            patterns=list(d.get("patterns", []) or []),
            keyboard_shortcut=d.get("keyboard_shortcut", ""),
        )
        for child_d in d.get("children", []) or []:
            child = UiaProvider._dict_to_uinode(child_d)
            if child:
                node.children.append(child)
        return node

    # ── Internal: 树构建 ───────────────────────────────────────────────

    def _build_node(
        self,
        control,
        depth: int,
        collected: list,
        parent_id: str = "",
    ) -> Optional[UiNode]:
        """递归构建 UiNode 子树。

        筛选条件：
          - 必须是 IsControlElement=True
          - ControlTypeName 必须在 _INTERACTIVE_SET 中
          - 深度不超过 MAX_DEPTH
          - 总计不超过 MAX_NODES
        """
        if depth > self.MAX_DEPTH or collected[0] >= self.MAX_NODES:
            return None

        # 只取 ControlElement
        try:
            is_ce = bool(getattr(control, 'IsControlElement', False))
        except Exception:
            is_ce = False
        if not is_ce:
            # 窗口根节点例外（总是保留）
            if depth > 0:
                return None

        ct_name = self._safe_attr(control, 'ControlTypeName', '')
        if ct_name not in _INTERACTIVE_SET and depth > 0:
            # 非交互型且非根节点 → 跳过该节点本身但继续遍历子节点（用于 Pane/Group 穿透）
            pass

        collected[0] += 1

        # 构造当前节点
        node_id = self._make_id(control)
        role = _INTERACTIVE_CONTROLS.get(ct_name, "container")
        state = self._detect_state(control)
        patterns = self._get_patterns(control)
        name = self._safe_attr(control, 'Name', '') or ""
        value = self._safe_value(control, ct_name)
        shortcut = self._safe_attr(control, 'AcceleratorKey', '') or ""
        rect = self._get_bounding_rect(control)

        node = UiNode(
            node_id=node_id,
            name=name,
            control_type=ct_name,
            role=role,
            value=value,
            state=state,
            x=rect[0], y=rect[1], width=rect[2], height=rect[3],
            patterns=patterns,
            keyboard_shortcut=shortcut,
            parent_id=parent_id,
        )

        # 递归子节点
        try:
            children = control.GetChildren()
        except Exception:
            children = []
        for child in children or []:
            child_node = self._build_node(
                child, depth + 1, collected, parent_id=node_id,
            )
            if child_node is not None:
                node.children.append(child_node)

        return node

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _safe_attr(control, attr: str, default=""):
        try:
            v = getattr(control, attr, default)
            if callable(v):
                v = v()
            return v if v is not None else default
        except Exception:
            return default

    def _make_id(self, control) -> str:
        """生成稳定短标识符。"""
        try:
            rid = getattr(control, 'RuntimeId', None)
            if rid:
                return ":".join(str(i) for i in rid)
        except Exception:
            pass
        # fallback: ControlTypeName + Name 的 sha256 前 12 位
        ct = self._safe_attr(control, 'ControlTypeName', '')
        nm = self._safe_attr(control, 'Name', '')
        raw = f"{ct}|{nm}"
        return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]

    def _detect_state(self, control) -> str:
        """检测控件状态：focused / disabled / enabled / checked / ..."""
        states = []
        try:
            if getattr(control, 'HasKeyboardFocus', False):
                states.append("focused")
        except Exception:
            pass
        try:
            if not getattr(control, 'IsEnabled', True):
                states.append("disabled")
        except Exception:
            pass
        try:
            if getattr(control, 'IsOffscreen', False):
                states.append("offscreen")
        except Exception:
            pass
        try:
            toggled = getattr(control, 'ToggleState', None)
            if toggled is not None:
                states.append("checked" if toggled else "unchecked")
        except Exception:
            pass
        if not states:
            states.append("enabled")
        return "|".join(states)

    def _get_patterns(self, control) -> List[str]:
        """获取控件支持的 UIA 模式列表。"""
        patterns = []
        try:
            raw = control.GetSupportedPatterns()
            for p in (raw or []):
                pn = self._safe_attr(p, 'PatternName', '')
                short = _PATTERN_MAP.get(pn, pn)
                if short:
                    patterns.append(short)
        except Exception:
            pass
        return patterns

    @staticmethod
    def _get_bounding_rect(control) -> tuple:
        """获取控件边界框 (x, y, w, h)，失败返回 (0,0,0,0)。"""
        try:
            br = getattr(control, 'BoundingRectangle', None)
            if br is not None:
                return (int(br.left), int(br.top),
                        int(br.right - br.left), int(br.bottom - br.top))
        except Exception:
            pass
        return (0, 0, 0, 0)

    def _safe_value(self, control, control_type_name: str) -> str:
        """获取控件的 Value 属性（TextBox 特有）。"""
        if control_type_name in ("EditControl", "ComboBoxControl"):
            try:
                vp = getattr(control, 'GetValuePattern', None)
                if callable(vp):
                    pat = vp()
                    if pat:
                        return getattr(pat, 'Value', '') or ""
            except Exception:
                pass
        return ""

    @staticmethod
    def _node_to_simple(control) -> Optional[UiNode]:
        """将 UIA control 转为轻量 UiNode（用于焦点/查找）。"""
        try:
            return UiNode(
                node_id=str(getattr(control, 'RuntimeId', '')),
                name=getattr(control, 'Name', '') or '',
                control_type=getattr(control, 'ControlTypeName', '') or '',
                role=_INTERACTIVE_CONTROLS.get(
                    getattr(control, 'ControlTypeName', ''), ''),
                state="focused",
            )
        except Exception:
            return None

    @staticmethod
    def _friendly_app_name(title: str, foreground) -> str:
        """从窗口标题 + 进程名推导友好应用名。"""
        proc = (getattr(foreground, 'ProcessName', '') or '').lower()
        # 复用已知指纹映射（与 context.py 一致）
        from .context import _APP_FINGERPRINTS
        for key, friendly in _APP_FINGERPRINTS:
            if key in proc or proc in key:
                return friendly
        # fallback: 把 exe 名去掉后缀
        if proc.endswith(".exe"):
            proc = proc[:-4]
        return proc or "unknown"


# ═══════════════════════════════════════════════════════════════════════
# VisionProvider — 截图 + 视觉模型（合规兜底）
# ═══════════════════════════════════════════════════════════════════════

class VisionProvider(PerceptionProvider):
    """视觉感知——截图 + OCR/视觉模型。

    compliance_mode=False 时 snapshot() 返回 None（合规闸门）。
    这是融合策略④"端侧纯视觉 → 合规兜底层"的落点。

    用法
    ----
        vp = VisionProvider(compliance_mode=True)
        state = vp.snapshot()  # 启用时产出 UIState
    """

    def __init__(self, compliance_mode: bool = False):
        self._compliance_mode = compliance_mode

    @property
    def name(self) -> str:
        return "VisionProvider"

    @property
    def compliance_mode(self) -> bool:
        return self._compliance_mode

    @compliance_mode.setter
    def compliance_mode(self, value: bool):
        self._compliance_mode = value

    def available(self) -> bool:
        """视觉感知仅在合规模式下可用。"""
        return self._compliance_mode

    def snapshot(self) -> Optional[UIState]:
        """合规闸门：compliance_mode=False → 返回 None（降级）。

        启用时，通过 vision_executor 独立函数截图 + OCR 提取文本。
        """
        if not self._compliance_mode:
            return None  # 合规闸门 → 降级

        t0 = time.perf_counter()
        try:
            from .vision_executor import (
                vision_screenshot, vision_ocr,
                get_compliance_mode, set_compliance_mode,
            )

            # 确保合规模式同步
            if not get_compliance_mode():
                set_compliance_mode(True)

            # 截图
            screenshot_result = vision_screenshot(
                intent="screen capture", app="", encode_b64=True,
            )
            visible_text = ""
            screenshot_b64 = ""
            img_width = 0
            img_height = 0
            if screenshot_result.ok:
                screenshot_b64 = screenshot_result.data.get("image_b64", "")
                img_width = screenshot_result.data.get("width", 0)
                img_height = screenshot_result.data.get("height", 0)

            # OCR 提取可见文本
            try:
                ocr_result = vision_ocr(intent="extract visible text", encode_b64=True)
                if ocr_result.ok:
                    visible_text = ocr_result.data.get("hint", "")
            except Exception:
                pass

            # 获取基本窗口信息
            from .context import detect_context
            app_ctx = detect_context()

            # 构建简化 UI 节点（带上截图尺寸信息）
            root = UiNode(
                node_id=f"vision:{app_ctx.app_name or 'unknown'}",
                name=app_ctx.window_title or "Screenshot",
                control_type="Window",
                role="window",
                state="enabled",
                width=img_width,
                height=img_height,
            )

            elapsed = (time.perf_counter() - t0) * 1000
            return UIState(
                source="vision",
                provider_name=self.name,
                app_name=app_ctx.app_name,
                window_title=app_ctx.window_title,
                process_name=app_ctx.process_name,
                platform=app_ctx.platform,
                root=root,
                focus=root,
                node_count=1,
                visible_text=visible_text,
                snapshot_elapsed_ms=elapsed,
            )
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return UIState(
                source="none",
                provider_name=self.name,
                snapshot_elapsed_ms=elapsed,
                error=str(e),
            )


# ═══════════════════════════════════════════════════════════════════════
# PerceptionStack — UIA → Light → Vision 链式降级
# ═══════════════════════════════════════════════════════════════════════

class PerceptionStack:
    """可插拔感知层中枢 —— 按优先级链式调用 Provider。

    融合策略①的落点："UIA 优先，失败降级"。

    降级链
    ──────
    1. UiaProvider.snapshot()    → 成功 → 返回结构化 UIState
    2. UiaProvider 返回 None     → 降级到 LightProvider
    3. LightProvider.snapshot() → 成功 → 返回窗口指纹 UIState
    4. LightProvider 失败         → 降级到 VisionProvider
    5. VisionProvider             → 合规闸门（compliance_mode=False → None）
    6. 所有 Provider 都不可用      → 返回 source="none" 的 UIState

    用法
    ----
        stack = PerceptionStack()
        stack.add(UiaProvider())
        stack.add(LightProvider())
        stack.add(VisionProvider(compliance_mode=True))
        state = stack.snapshot()  # 自动走完降级链
    """

    def __init__(self):
        self._providers: List[PerceptionProvider] = []
        self._default_providers_added = False
        # ── UIA 快照缓存：key → (UIState, timestamp_seconds) ──
        self._cache: Dict[str, Any] = {}
        self._cache_ttl_ms: float = 200.0  # 同窗口 200ms 内复用快照

    def add(self, provider: PerceptionProvider) -> "PerceptionStack":
        """添加一个 Provider（按添加顺序优先调用）。"""
        self._providers.append(provider)
        return self

    def insert_first(self, provider: PerceptionProvider) -> "PerceptionStack":
        """将 Provider 插入到链的最前面（最高优先级）。"""
        self._providers.insert(0, provider)
        return self

    def remove(self, name: str) -> bool:
        """移除指定名称的 Provider。"""
        before = len(self._providers)
        self._providers = [p for p in self._providers if p.name != name]
        return len(self._providers) < before

    def _get_provider(self, name: str) -> Optional[PerceptionProvider]:
        """按名称查找 Provider（例: "UiaProvider", "LightProvider"）。"""
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def setup_defaults(self, compliance_mode: bool = False) -> "PerceptionStack":
        """一键配置默认 Provider 链（UIA → Light → Vision）。"""
        if not self._default_providers_added:
            self._providers.clear()
            self.add(UiaProvider())
            self.add(LightProvider())
            self.add(VisionProvider(compliance_mode=compliance_mode))
            self._default_providers_added = True
        return self

    def snapshot_light(self) -> UIState:
        """仅使用 LightProvider 快速采集窗口指纹（~1-2ms）。

        不触发 UIA 树遍历，适合缓存/工作流命中快速路径。
        """
        provider = self._get_provider("LightProvider")
        if provider is not None and provider.available():
            state = provider.snapshot()
            if state is not None and state.source != "none":
                return state
        return UIState(
            source="none",
            provider_name="PerceptionStack",
            snapshot_elapsed_ms=0.0,
            error="LightProvider 不可用",
        )

    def snapshot(self, source: str = "auto", use_cache: bool = True) -> UIState:
        """采集 UI 快照（支持指定源 + UIA 缓存）。

        Args:
            source: "auto" (完整降级链), "light" (仅窗口指纹), "uia" (仅 UIA 树)
            use_cache: 是否启用 UIA 快照缓存（同窗口 {cache_ttl_ms}ms 内复用）

        典型延迟:
            source="light"         → <2ms
            source="uia" 缓存命中 → <1ms
            source="uia" 缓存未命中 → 20-200ms（取决于 UI 复杂度）
            source="auto"         → 优先走 UIA 缓存，未命中则完整降级链
        """
        if source == "light":
            return self.snapshot_light()

        if source == "uia":
            return self._snapshot_uia(use_cache=use_cache)

        # source == "auto": 完整降级链，但优先查 UIA 缓存
        if use_cache:
            cached = self._uia_cache_check()
            if cached is not None:
                return cached

        return self._snapshot_full_chain()

    # ── 缓存相关 ─────────────────────────────────────────────────────

    def _snapshot_uia(self, use_cache: bool = True) -> UIState:
        """UIA 快照（带可选缓存）。缓存未命中时实际采集 UIA 树。"""
        uia_prov = self._get_provider("UiaProvider")
        if uia_prov is None or not uia_prov.available():
            return self.snapshot_light()

        if use_cache:
            cached = self._uia_cache_check()
            if cached is not None:
                return cached

        state = uia_prov.snapshot()
        if state is None or state.source == "none":
            return self.snapshot_light()

        if use_cache:
            key = self._make_cache_key(state)
            self._cache[key] = (state, time.perf_counter())

        return state

    def _uia_cache_check(self) -> Optional[UIState]:
        """检查 UIA 缓存：同窗口且未过期则返回缓存快照。"""
        # 用 Light 快照获取当前窗口指纹作为缓存键
        light = self.snapshot_light()
        if light.source == "none":
            return None
        key = self._make_cache_key(light)
        entry = self._cache.get(key)
        if entry is None:
            return None
        state, ts = entry
        elapsed_ms = (time.perf_counter() - ts) * 1000
        if elapsed_ms < self._cache_ttl_ms:
            state.snapshot_elapsed_ms = elapsed_ms
            return state
        return None

    @staticmethod
    def _make_cache_key(state: UIState) -> str:
        """用进程名 + 窗口标题生成稳定的缓存键。"""
        return f"{state.process_name or '?'}|{state.window_title or '?'}"

    def invalidate_cache(self) -> None:
        """清空 UIA 快照缓存（窗口切换后调用）。"""
        self._cache.clear()

    # ── 完整降级链（原 snapshot 逻辑）────────────────────────────────

    def _snapshot_full_chain(self) -> UIState:
        """完整降级链：UIA → Light → Vision。

        每个 Provider 按序调用，第一个返回非 None 的非 error 结果即采纳。
        """
        t0 = time.perf_counter()
        chain_log: List[Dict[str, Any]] = []

        for provider in self._providers:
            if not provider.available():
                chain_log.append({"provider": provider.name, "result": "skipped (not available)"})
                continue

            try:
                state = provider.snapshot()
                if state is not None and state.source != "none":
                    total_elapsed = (time.perf_counter() - t0) * 1000
                    state.snapshot_elapsed_ms = total_elapsed
                    # 记录降级链
                    state._chain = chain_log  # type: ignore
                    # 写入缓存
                    key = self._make_cache_key(state)
                    self._cache[key] = (state, time.perf_counter())
                    return state
                chain_log.append({"provider": provider.name, "result": "returned None (degraded)"})
            except Exception as e:
                chain_log.append({"provider": provider.name, "result": f"error: {e}"})

        # 所有 Provider 都不可用
        total_elapsed = (time.perf_counter() - t0) * 1000
        return UIState(
            source="none",
            provider_name="PerceptionStack",
            snapshot_elapsed_ms=total_elapsed,
            error=f"所有 Provider 均不可用: {chain_log}",
        )

    def get_degradation_chain(self) -> List[str]:
        """获取当前降级链顺序（用于诊断）。"""
        return [p.name for p in self._providers]

    def set_compliance_mode(self, enabled: bool) -> None:
        """统一设置 VisionProvider 的合规模式。"""
        for p in self._providers:
            if isinstance(p, VisionProvider):
                p.compliance_mode = enabled
