"""NL2Shortcut Keyboard Master Agent.

一个会理解目标、自己拆步、记住你的操作习惯、并主动给建议的键盘大师。

自然语言 → 键盘/鼠标操作，跨平台、低延迟、可学习。
"""
# NEVER cache bytecode — prevents stale-code "调用失败" bugs
import sys as _sys
_sys.dont_write_bytecode = True

from .agent import ShortcutAgent
from .master import KeyboardMasterAgent
from .planner import GoalPlanner
from .operation_memory import OperationMemory
from .keyboard_primitives import KeyboardPrimitives
from .models import (
    Platform,
    Shortcut,
    IntentResult,
    ExecutionResult,
    Stats,
    WorkflowStep,
    WorkflowDefinition,
    WorkflowResult,
    StepResult,
    AppContext,
    Plan,
    PlanStep,
)

__version__ = "1.0.0"
__agent_name__ = "NL2Shortcut Keyboard Master Agent"

__all__ = [
    "KeyboardMasterAgent",
    "ShortcutAgent",
    "GoalPlanner",
    "OperationMemory",
    "KeyboardPrimitives",
    "Platform",
    "Shortcut",
    "IntentResult",
    "ExecutionResult",
    "Stats",
    "WorkflowStep",
    "WorkflowDefinition",
    "WorkflowResult",
    "StepResult",
    "AppContext",
    "Plan",
    "PlanStep",
    "OpRecord",
    "OpPattern",
]
