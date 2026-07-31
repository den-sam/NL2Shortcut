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

class UiaProvider(PerceptionProvider):
    """UIA 结构化感知——Microsoft UI Automation / Linux AT-SPI。

    当前为桩实现（返回 None，触发自动降级到 LightProvider）。

    规划能力
    ────────
    - Windows: 通过 uiautomation / comtypes 获取 UI 树
    - macOS: 通过 pyobjc 调用 NSAccessibility
    - Linux: 通过 python-atspi 获取 AT-SPI 树

    或通过之前设计的 Rust UIA 内核（COM → FFI → Python），
    在 <5ms 内产出完整 UI 树快照。

    UI 树产出后将自动用于：
    - 控件类型感知（Button / Edit / ListItem 等）
    - 焦点追踪（currfocused）
    - 控件模式识别（Invoke / Value / Toggle）
    - 上下文增强（当前编辑框内容、下拉列表项等）
    """

    def __init__(self):
        self._enabled = False  # 默认禁用，待 Rust 内核或 py-uia 就绪后启用

    @property
    def name(self) -> str:
        return "UiaProvider"

    def available(self) -> bool:
        return self._enabled

    def enable(self):
        """启用 UIA 感知（当底层依赖就绪时调用）。"""
        self._enabled = True

    def snapshot(self) -> Optional[UIState]:
        """采集 UIA 结构化 UI 树快照。

        当前返回 None，触发自动降级。
        """
        if not self._enabled:
            return None  # 自动降级到 LightProvider

        t0 = time.perf_counter()
        try:
            # TODO: 接入 UIA 内核，产出完整 UiNode 树
            # 示例流程：
            #   1. 获取前台窗口 HWND
            #   2. 从 HWND 构造 IUIAutomationElement
            #   3. 递归遍历子树，收集所有可见节点
            #   4. 构造 UiNode 树 + 焦点节点
            #   5. 填充 UIState
            raise NotImplementedError("UIA 内核尚未就绪")
        except Exception as e:
            elapsed = (time.perf_counter() - t0) * 1000
            return UIState(
                source="none",
                provider_name=self.name,
                snapshot_elapsed_ms=elapsed,
                error=str(e),
            )


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
        """合规闸门：compliance_mode=False → 返回 None（降级到下一个 Provider）。

        启用时，调用 VisionExecutor 截图 + 提取文本。
        """
        if not self._compliance_mode:
            return None  # 合规闸门 → 降级

        t0 = time.perf_counter()
        try:
            from .vision_executor import VisionExecutor
            executor = VisionExecutor()

            # 截图 + OCR 提取可见文本
            visible_text = ""
            screenshot_b64 = ""
            try:
                result = executor.capture_and_ocr()
                if result and result.get("text"):
                    visible_text = result["text"]
                if result and result.get("screenshot"):
                    screenshot_b64 = result["screenshot"]
            except Exception:
                pass

            # 获取基本窗口信息
            from .context import detect_context
            app_ctx = detect_context()

            # 构建简化 UI 节点
            root = UiNode(
                node_id=f"vision:{app_ctx.app_name or 'unknown'}",
                name=app_ctx.window_title or "Screenshot",
                control_type="Window",
                role="window",
                state="enabled",
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

    def setup_defaults(self, compliance_mode: bool = False) -> "PerceptionStack":
        """一键配置默认 Provider 链（UIA → Light → Vision）。"""
        if not self._default_providers_added:
            self._providers.clear()
            self.add(UiaProvider())
            self.add(LightProvider())
            self.add(VisionProvider(compliance_mode=compliance_mode))
            self._default_providers_added = True
        return self

    def snapshot(self) -> UIState:
        """按降级链采集 UI 快照。

        每一个 Provider 按序调用，第一个返回非 None 的即采纳。
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
