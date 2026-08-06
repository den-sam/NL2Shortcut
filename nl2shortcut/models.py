"""nl2shortcut 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List
from enum import Enum


class Platform(Enum):
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"

    @classmethod
    def detect(cls) -> "Platform":
        import sys
        if sys.platform == "win32":
            return cls.WINDOWS
        elif sys.platform == "darwin":
            return cls.MACOS
        return cls.LINUX


class ActionType(Enum):
    """快捷方式或动作的 execution 模式 / 来源。

    该枚举用于分类快捷方式是「如何被解析出来的」，而不是「它做了什么」。
    NL2Shortcut 按权重递增的顺序解析意图：
      1. PRIMITIVE  — 键盘基元回退（不查数据库）
      2. EXACT      — 直接来自数据库查表的快捷按键
      3. LLM_SINGLE — 由 LLM 规划出的单个快捷键
      4. LLM_PLAN   — 由 LLM 分解出的多步计划
    """

    EXACT      = "exact"       # 数据库直接匹配（windows_key / mac_key）
    LLM_SINGLE = "llm_single"  # LLM 建议的单个快捷键
    LLM_PLAN   = "llm_plan"    # LLM 分解出的多步计划
    PRIMITIVE  = "primitive"   # KeyboardPrimitives 回退（Tab/Alt/热键）


@dataclass
class Shortcut:
    """单条快捷键映射记录。"""
    id: Optional[int] = None
    command: str = ""
    description: str = ""
    windows_key: str = ""
    mac_key: str = ""
    linux_key: str = ""
    command_cn: str = ""
    description_cn: str = ""
    category: str = "通用"
    application: str = "common"
    frequency: int = 0

    def get_key(self, platform: Platform) -> str:
        mapping = {
            Platform.WINDOWS: self.windows_key,
            Platform.MACOS: self.mac_key,
            Platform.LINUX: self.linux_key,
        }
        return mapping.get(platform, "")


@dataclass
class IntentResult:
    """意图识别的结果。"""
    intent: str
    command: str
    confidence: float
    matched_keyword: str = ""
    alternatives: List["IntentResult"] = field(default_factory=list)
    composite_plan: Optional["CompositePlan"] = None


@dataclass
class ExecutionResult:
    """一次快捷键执行的结果。"""
    success: bool
    intent: str = ""
    command: str = ""
    key_combination: str = ""
    platform: str = ""
    processing_time: float = 0.0
    error: Optional[str] = None
    matched_keyword: str = ""
    mode: str = ""  # "fast_exact" | "local" | "llm_single" | "llm_plan"
    dry_run: bool = False
    confidence: float = 0.0  # 识别置信度（来自 IntentResult）
    composite_plan: Optional["CompositePlan"] = None

    def __str__(self) -> str:
        status = "\u2705" if self.success else "\u274c"
        if self.success:
            return (
                f"{status} intent={self.intent} command={self.command} "
                f"keys={self.key_combination} "
                f"platform={self.platform} "
                f"time={self.processing_time*1000:.1f}ms"
            )
        return f"{status} {self.error}"


@dataclass
class PlanStep:
    """由 DeepSeek 分解出的计划中的单个步骤。"""
    command: str     # nl2shortcut command name
    reason: str = "" # why this step
    key: str = ""    # resolved key combination (filled at execution)


@dataclass
class Plan:
    """由 DeepSeek 分解出的多步计划。"""
    steps: List[PlanStep] = field(default_factory=list)
    reasoning: str = ""  # LLM's explanation
    confidence: float = 0.0


@dataclass
class Stats:
    """执行统计信息。"""
    total_executions: int = 0
    successful: int = 0
    failed: int = 0
    avg_processing_time: float = 0.0
    top_commands: List[tuple] = field(default_factory=list)
    total_processing_time: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.total_executions == 0:
            return 0.0
        return self.successful / self.total_executions * 100


# ═══════════════════════════════════════════════════════════════════════
# Workflow Engine Models
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class WorkflowStep:
    """工作流中的单个步骤。"""
    name: str
    action: str  # "shortcut" | "shell" | "http" | "file" | "python" | "wait" | "condition"
                   # | "click_element"  (UI 元素定位，第3步)
    command: str = ""
    args: dict = field(default_factory=dict)
    capture: str = ""  # variable name to capture output
    condition: str = ""  # Python expression, step only runs if truthy
    retry: int = 0
    timeout: float = 10.0
    # ── 第2步：循环结构 ────────────────────────────────────────────
    # loop 为空时单次执行；非空时按表达式求值结果迭代。
    # 支持：
    #   - "range(5)"            → 固定次数循环
    #   - "range(1, 10)"       → 带起始的 range
    #   - "ctx['rows']"         → 遍历上下文变量（list/tuple）
    #   - "while expr"          → while 循环（expr 是 Python 表达式）
    #                              while 循环有最大次数保护（_MAX_LOOP_ITER）
    loop: str = ""
    loop_var: str = "item"  # 循环变量名，在循环体内可通过 $loop_var 或 ctx[loop_var] 访问


@dataclass
class WorkflowDefinition:
    """从 YAML 加载的完整工作流。"""
    name: str
    description: str = ""
    version: str = "1.0"
    steps: List[WorkflowStep] = field(default_factory=list)
    variables: dict = field(default_factory=dict)
    source_path: str = ""


@dataclass
class StepResult:
    """单个工作流步骤的执行结果。"""
    step_name: str
    success: bool
    output: str = ""
    error: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class WorkflowResult:
    """整个工作流的执行结果。"""
    workflow_name: str
    success: bool
    steps: List[StepResult] = field(default_factory=list)
    variables: dict = field(default_factory=dict)
    total_duration_ms: float = 0.0
    error: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════
# Application Context Model
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class AppContext:
    """当前应用上下文快照。"""
    window_title: str = ""
    process_name: str = ""
    app_name: str = ""  # friendly name like "vscode", "chrome", "terminal"
    platform: str = ""
    parsed_file_path: Optional[str] = None  # extracted from window title, e.g. "main.py"
