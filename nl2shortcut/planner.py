"""Goal → Step Plan 引擎 (Phase 2).

将用户的自然语言目标分解为可执行的键盘动作序列。

核心设计
─────────
• 用户的意图常常是「目标导向」而非「指令导向」
  — "把这份报告发出去" → 不是找「发送邮件」快捷键
  — 而是「复制内容 → 打开邮箱 → 粘贴 → 发送」

• Planner 用 LLM 推理：用户的真实目标是什么，最高效的路径是什么，
  哪些步骤有快捷键，哪些需要 Tab 导航，哪些需要 Agent 视觉介入。

• 结果为独立的 Plan / PlanStep 结构（区别于 models.py 的 nl2shortcut
  命令级别 PlanStep），每个 step 直接映射到 adapter.py 的原子动作。

集成关系
─────────
  GoalPlanner
    ├── llm.py::DeepSeekEngine._call_api()   ← LLM 推理
    ├── adapter.py::create_adapter()          ← 原子动作执行
    └── composites.py::CompositePlan           ← 复合操作委托
"""

from __future__ import annotations

import json
import time
import sys
import urllib.request
import urllib.error
import subprocess
from dataclasses import dataclass, field
from typing import Optional

from .models import ExecutionResult, Platform
from .llm import DeepSeekEngine, _load_api_key, DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL, REQUEST_TIMEOUT
from .context_store import SemanticCache, MinimalContext
from .model_router import ModelRouter

# ─────────────────────────────────────────────────────────────────────────────
# Data Classes — Keyboard-Level Plan
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PlanStep:
    """一个可执行的键盘动作步骤。

    Attributes
    ----------
    step_id : int
        步骤序号（1-based），全局唯一。
    description : str
        人类可读描述，如"全选当前文本"。
    action : str
        动作类型：

        - ``"shortcut"`` → 按下键位组合（Ctrl+C 等）
        - ``"type"``     → 输入一段文本
        - ``"tab"``      → 按 Tab / Shift+Tab / 方向键（n 次）
        - ``"shell"``    → 执行一条 shell 命令（Windows cmd）
        - ``"wait"``     → 等待（wait_ms 毫秒）
        - ``"composite"`` → 复合操作，委托 composites.py 执行

    key_combination : str
        ``action="shortcut"`` 时填写，如 ``"Ctrl+C"``。
    text : str
        ``action="type"`` 时填写。
    n : int
        ``action="tab"`` 时按压次数（默认 1）。
    direction : str
        ``action="tab"`` 时方向（``"tab"`` | ``"shift_tab"`` | ``"left"`` | ``"right"``
        | ``"up"`` | ``"down"``）。
    command : str
        ``action="shell"`` 时填写 cmd 命令。
    wait_ms : int
        ``action="wait"`` 时的等待毫秒数。
    composite_hint : str
        ``action="composite"`` 时传给 composites.py 的描述，如文件名、路径。
    reasoning : str
        LLM 为什么选择这个动作。
    confidence : float
        步骤置信度 0.0–1.0。
    """
    step_id: int
    description: str
    action: str
    # shortcut
    key_combination: str = ""
    # type
    text: str = ""
    # tab / arrow
    n: int = 1
    direction: str = "tab"
    # shell
    command: str = ""
    # wait
    wait_ms: int = 0
    # composite
    composite_hint: str = ""
    # meta
    reasoning: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "action": self.action,
            "key_combination": self.key_combination,
            "text": self.text,
            "n": self.n,
            "direction": self.direction,
            "command": self.command,
            "wait_ms": self.wait_ms,
            "composite_hint": self.composite_hint,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PlanStep":
        return cls(
            step_id=d["step_id"],
            description=d["description"],
            action=d["action"],
            key_combination=d.get("key_combination", ""),
            text=d.get("text", ""),
            n=d.get("n", 1),
            direction=d.get("direction", "tab"),
            command=d.get("command", ""),
            wait_ms=d.get("wait_ms", 0),
            composite_hint=d.get("composite_hint", ""),
            reasoning=d.get("reasoning", ""),
            confidence=d.get("confidence", 1.0),
        )


@dataclass
class Plan:
    """完整的目标执行计划。

    Attributes
    ----------
    goal : str
        用户原始目标原文。
    steps : list[PlanStep]
        分解后的步骤列表（有序）。
    reasoning : str
        整体规划思路（LLM 输出）。
    total_steps : int
        步骤总数（len(steps) 的别名，方便访问）。
    estimated_time_ms : int
        LLM 估算的耗时（毫秒），包含各步骤 wait_ms。
    has_composite : bool
        是否包含需要 Agent 视觉介入的复合操作。
    confidence : float
        整体计划置信度。
    source : str
        计划来源：``"llm"`` | ``"fallback"`` | ``"error"``。
    error : str
        LLM 调用失败时的错误信息（仅 source="error" 时有值）。
    """
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    reasoning: str = ""
    total_steps: int = 0
    estimated_time_ms: int = 0
    has_composite: bool = False
    confidence: float = 1.0
    source: str = "llm"
    error: str = ""

    def __post_init__(self):
        if self.total_steps == 0:
            self.total_steps = len(self.steps)
        self.has_composite = any(s.action == "composite" for s in self.steps)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "reasoning": self.reasoning,
            "total_steps": self.total_steps,
            "estimated_time_ms": self.estimated_time_ms,
            "has_composite": self.has_composite,
            "confidence": self.confidence,
            "source": self.source,
        }

    def format_human(self) -> str:
        """渲染为人类可读的计划摘要（用于 GUI / 日志）。"""
        lines = [
            f"🎯 目标：{self.goal}",
            f"📋 共 {self.total_steps} 步（置信度 {self.confidence:.0%}，估算 {self.estimated_time_ms}ms）",
            "",
        ]
        if self.source == "error":
            lines.append(f"⚠️  LLM 调用失败：{self.error}")
            lines.append("▶  降级为简单快捷键方案（Fallback）：")
        elif self.source == "fallback":
            lines.append("🔧 LLM 不可用，使用启发式 Fallback：")
        else:
            lines.append(f"💡 规划思路：{self.reasoning}")
            lines.append("")

        for s in self.steps:
            icon = {
                "shortcut": "⌨️ ",
                "type":     "⌦ ",
                "tab":      "⇥ ",
                "shell":    "⚡",
                "wait":     "⏱️ ",
                "composite":"🔍",
            }.get(s.action, "• ")
            conf = f"[{s.confidence:.0%}]" if s.confidence < 1.0 else ""
            detail = self._step_detail(s)
            lines.append(f"  {s.step_id:2d}. {icon} {s.description} {conf}")
            if s.reasoning:
                lines.append(f"       └ {s.reasoning}")
            if detail:
                lines.append(f"       → {detail}")

        return "\n".join(lines)

    def _step_detail(self, s: PlanStep) -> str:
        if s.action == "shortcut" and s.key_combination:
            return s.key_combination
        if s.action == "type" and s.text:
            return f'输入「{s.text[:30]}{"…" if len(s.text) > 30 else ""}」'
        if s.action == "tab":
            return f"方向={s.direction}, 次数={s.n}"
        if s.action == "shell":
            return s.command[:60]
        if s.action == "wait":
            return f"{s.wait_ms}ms"
        if s.action == "composite":
            hint = s.composite_hint[:50]
            return f"[composite] {hint}" if hint else "[composite]"
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# System Prompt — Goal Decomposition
# ─────────────────────────────────────────────────────────────────────────────

PLANNER_SYSTEM_PROMPT = """\
你是一个智能键盘动作规划器。

用户给出一个「目标」而非「指令」。你的任务是将目标拆解为 **原子键盘动作序列**。

## 可用动作类型

| action | 说明 | 字段 |
|--------|------|------|
| shortcut | 按键组合（Ctrl+C 等） | key_combination |
| type | 输入文本 | text |
| tab | Tab / Shift+Tab / 方向键导航（n 次） | direction, n |
| shell | Windows cmd 命令 | command |
| wait | 等待（毫秒） | wait_ms |
| composite | 需要视觉模型的复合操作（委托 Agent） | composite_hint |

## 规划原则（按优先级）

1. **字面 → 真实意图**：用户说"把这份报告发出去"，不是找"发送"快捷键，
   而是「复制内容 → 打开邮箱 → 粘贴 → 发送」。
2. **优先快捷键**：能用单个 Ctrl+xx 完成的，就不要拆成 Tab 序列。
3. **主动建议替代**：如果有更快的路径（比如 Win+R 替代开始菜单点击），要标注。
4. **最短 Tab 路径**：必须 Tab 导航时，估算次数（n）。
5. **composite 标记**：涉及文件复制/移动、跨窗口 UI 交互的，用 composite。
   composite_hint 中**必须包含目标路径**，例如 "复制文件到桌面"、"移动图片到下载文件夹"。
6. **效率第一**：总步数尽量少，估算耗时尽量准确。
7. **标注置信度**：单一步骤不确定时 confidence < 1.0 并写明原因。
8. **上下文感知**：若提供了当前应用，优先使用该应用特有快捷键（如 Excel 中删除用 Alt+E+D）。
9. **记忆驱动**：若提供了历史学习建议，应结合用户操作习惯调整步骤。

## 输出格式（严格 JSON，无 markdown）

{
  "reasoning": "整体思路（1-2句）",
  "confidence": 0.0-1.0,
  "estimated_time_ms": 总估算耗时（整数）,
  "steps": [
    {
      "step_id": 1,
      "description": "人类可读描述",
      "action": "shortcut|type|tab|shell|wait|composite",
      "key_combination": "Ctrl+C",
      "text": "",
      "direction": "tab",
      "n": 1,
      "command": "",
      "wait_ms": 0,
      "composite_hint": "",
      "reasoning": "为什么选这个动作",
      "confidence": 1.0
    }
  ]
}

**重要**：
- 只用 JSON，返回对象必须有 reasoning, confidence, estimated_time_ms, steps。
- steps 为空数组表示无法规划（confidence=0）。
- estimated_time_ms = sum(各步骤实际耗时 + wait_ms)。
  shortcut≈200ms, type≈50ms/字, tab≈150ms/次, shell≈500ms, wait=指定值。
- 不要在 steps 中嵌套数组，steps 是扁平的一维数组。
- 只输出 JSON，不要任何其他文字。
"""


# ─────────────────────────────────────────────────────────────────────────────
# Fallback Plans — LLM 不可用时的降级策略
# ─────────────────────────────────────────────────────────────────────────────

_FALLBACK_PATTERNS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    # (goal_fragment, [(description, action, field, value), ...])
    ("发邮件", [
        ("Ctrl+C 复制选中内容", "shortcut", "key_combination", "Ctrl+C"),
        ("Win+R 打开运行对话框", "shortcut", "key_combination", "Win+R"),
        ("输入 mailto: 协议打开邮件客户端", "type", "text", "mailto:"),
        ("Enter 打开邮件窗口", "shortcut", "key_combination", "Enter"),
        ("等待邮件窗口加载", "wait", "wait_ms", "800"),
        ("Ctrl+V 粘贴内容", "shortcut", "key_combination", "Ctrl+V"),
        ("Ctrl+Enter 发送", "shortcut", "key_combination", "Ctrl+Enter"),
    ]),
    ("保存", [
        ("Ctrl+S 保存当前文件", "shortcut", "key_combination", "Ctrl+S"),
    ]),
    ("复制", [
        ("Ctrl+C 复制选中内容", "shortcut", "key_combination", "Ctrl+C"),
    ]),
    ("粘贴", [
        ("Ctrl+V 粘贴剪贴板内容", "shortcut", "key_combination", "Ctrl+V"),
    ]),
    ("全选", [
        ("Ctrl+A 全选", "shortcut", "key_combination", "Ctrl+A"),
    ]),
    ("关闭", [
        ("Alt+F4 关闭窗口", "shortcut", "key_combination", "Alt+F4"),
    ]),
    ("截", [
        ("Win+Shift+S 打开截图工具", "shortcut", "key_combination", "Win+Shift+S"),
    ]),
    ("新建", [
        ("Ctrl+N 新建窗口/文件", "shortcut", "key_combination", "Ctrl+N"),
    ]),
    ("撤销", [
        ("Ctrl+Z 撤销", "shortcut", "key_combination", "Ctrl+Z"),
    ]),
    ("重做", [
        ("Ctrl+Y 重做", "shortcut", "key_combination", "Ctrl+Y"),
    ]),
]


def _resolve_folder_path(hint: str) -> str:
    """从自然语言提示中提取目标文件夹路径（Windows）。

    支持中文/英文文件夹名：桌面/desktop、下载/downloads、文档/documents 等。
    返回完整的 Windows 路径字符串。
    """
    import os as _os
    hint_lower = hint.lower()

    # 已知文件夹映射表（中文 → 英文 → 环境变量/路径）
    _FOLDER_MAP: list[tuple[tuple[str, ...], str]] = [
        (("桌面", "desktop"), _os.path.join(_os.environ.get("USERPROFILE", "C:\\Users"), "Desktop")),
        (("下载", "downloads", "下载文件夹"), _os.path.join(_os.environ.get("USERPROFILE", "C:\\Users"), "Downloads")),
        (("文档", "documents", "我的文档", "my documents"), _os.path.join(_os.environ.get("USERPROFILE", "C:\\Users"), "Documents")),
        (("图片", "pictures", "照片"), _os.path.join(_os.environ.get("USERPROFILE", "C:\\Users"), "Pictures")),
        (("视频", "videos"), _os.path.join(_os.environ.get("USERPROFILE", "C:\\Users"), "Videos")),
        (("音乐", "music"), _os.path.join(_os.environ.get("USERPROFILE", "C:\\Users"), "Music")),
        (("公开", "public"), "C:\\Users\\Public"),
        (("c盘", "c:\\", "c drive"), "C:\\"),
        (("d盘", "d:\\", "d drive"), "D:\\"),
        (("根目录", "root"), "C:\\"),
    ]

    for patterns, path in _FOLDER_MAP:
        for p in patterns:
            if p in hint_lower:
                return path

    # 不匹配则返回桌面（最常用的文件操作目标）
    return _os.path.join(_os.environ.get("USERPROFILE", "C:\\Users"), "Desktop")


def _make_fallback_plan(goal: str) -> Plan:
    """启发式降级计划：匹配关键词返回对应快捷键。

    支持文件操作检测：将 "复制文件到桌面" 类的表达转为 terminal composite。
    """
    import re as _re
    goal_lower = goal.lower()

    # ── 文件操作模式检测（优先，生成 composite 步骤）──
    _FILE_COPY_PATTERNS = [
        r'(复制|拷贝|copy)\s*.*到\s*(.+)',
        r'(复制|拷贝|copy)\s*至\s*(.+)',
        r'(复制|拷贝|copy)\s*.*(桌面|下载|文档|图片|视频|音乐)',
    ]
    _FILE_MOVE_PATTERNS = [
        r'(移动|剪切|剪贴|move|cut)\s*.*到\s*(.+)',
        r'(移动|剪切|剪贴|move|cut)\s*至\s*(.+)',
        r'(移动|剪切|剪贴|move|cut)\s*.*(桌面|下载|文档|图片|视频|音乐)',
    ]

    for patterns, action_type in [
        (_FILE_COPY_PATTERNS, "copy"),
        (_FILE_MOVE_PATTERNS, "move"),
    ]:
        for pat in patterns:
            m = _re.search(pat, goal)
            if m:
                groups = m.groups()
                # 跳过动词组（如 "复制"），提取目标路径描述
                dest_text = groups[-1] if groups else goal
                dest_path = _resolve_folder_path(dest_text)
                hint = (
                    f"终端复制文件到{dest_path}" if action_type == "copy"
                    else f"终端移动文件到{dest_path}"
                )
                return Plan(
                    goal=goal,
                    steps=[PlanStep(
                        step_id=1,
                        description=f"文件{'复制' if action_type == 'copy' else '移动'}到 {dest_text}",
                        action="composite",
                        composite_hint=hint,
                        reasoning=f"Fallback 文件操作检测: {pat}",
                        confidence=0.55,
                    )],
                    reasoning=f"检测到文件{'复制' if action_type == 'copy' else '移动'}操作 → terminal composite",
                    total_steps=1,
                    estimated_time_ms=800,
                    confidence=0.55,
                    source="fallback_file_op",
                )

    # ── 关键词匹配 ──
    steps: list[PlanStep] = []
    for keyword, actions in _FALLBACK_PATTERNS:
        if keyword in goal:
            for i, (desc, action, field, value) in enumerate(actions, 1):
                step = PlanStep(
                    step_id=i,
                    description=desc,
                    action=action,
                    reasoning="Fallback（LLM 不可用），基于关键词匹配",
                    confidence=0.6,
                )
                if field == "key_combination":
                    step.key_combination = value
                elif field == "text":
                    step.text = value
                elif field == "wait_ms":
                    step.wait_ms = int(value)
                steps.append(step)
            break

    if not steps:
        # 最通用的 Fallback：尝试找 Alt+S（很多程序的"发送"）
        steps = [
            PlanStep(
                step_id=1,
                description="尝试 Alt+S 发送（通用快捷键）",
                action="shortcut",
                key_combination="Alt+S",
                reasoning="Fallback：无匹配，尝试最通用的发送快捷键",
                confidence=0.3,
            )
        ]

    total_ms = sum(
        200 if s.action == "shortcut" else
        50 * len(s.text) if s.action == "type" else
        150 * s.n if s.action == "tab" else
        500 if s.action == "shell" else
        s.wait_ms
        for s in steps
    )
    return Plan(
        goal=goal,
        steps=steps,
        reasoning="Fallback（LLM 不可用）",
        total_steps=len(steps),
        estimated_time_ms=total_ms,
        confidence=0.4,
        source="fallback",
    )


# ─────────────────────────────────────────────────────────────────────────────
# GoalPlanner
# ─────────────────────────────────────────────────────────────────────────────

class GoalPlanner:
    """将自然语言目标分解为可执行步骤序列。

    **集成点**

    - LLM 推理：调用 ``llm.py`` 的 DeepSeek API（复用已配置的 key）
    - 动作执行：委托 ``adapter.py::create_adapter()`` 的 ``KeyboardAdapter``
    - 复合操作：调用 ``composites.py`` 的 ``CompositePlan`` 工厂

    **使用示例**

    .. code-block:: python

        planner = GoalPlanner()
        plan = planner.plan("把这份报告发出去")

        # 预览（dry-run）
        print(plan.format_human())

        # 执行
        results = planner.execute_plan(plan)
        for r in results:
            print(r)

    **模式返回值**

    ``execute_plan`` 返回的每个 ``ExecutionResult`` 的 ``mode`` 字段：

    ==========  ==========================================
    mode        含义
    ==========  ==========================================
    plan_step   单个 plan step 执行成功
    plan_fail   某个 step 执行失败（后续继续）
    plan_error  计划整体执行失败（adapter 未初始化等）
    dry_run     模拟执行，未真按键盘
    ==========  ==========================================
    """

    def __init__(self, api_key: Optional[str] = None,
                 shared_cache: Optional[SemanticCache] = None,
                 router: Optional[ModelRouter] = None,
                 db: Optional[object] = None):
        """
        Args:
            api_key: DeepSeek API Key。若为 None，从环境变量 / 配置文件中加载。
            shared_cache: 可选外部共享缓存实例（来自 ContextStore）。
            router: 可选 ModelRouter 实例（用于 AVR 路由）。
            db: 可选 DatabaseManager 实例。若为 None，在首次 _db_increment_shortcut
                调用时延迟创建并复用（原实现每次调用都新建 DatabaseManager，开销 5-10ms）。
        """
        self._api_key = api_key or _load_api_key()
        self._available = bool(self._api_key)
        self._last_error: Optional[str] = None
        self._adapter = None   # lazy

        # 语义缓存：可接受外部共享实例
        self._cache = shared_cache or SemanticCache()

        # 模型路由：AVR 调度（可选）
        self._router = router

        # DatabaseManager 单例：避免每次 _db_increment_shortcut 都新建实例
        # （原实现每次都 DatabaseManager(config_dir / "shortcuts.db")，含 PRAGMA + 检查）
        self._db: Optional[object] = db

    @property
    def available(self) -> bool:
        """LLM 是否可用。"""
        return self._available

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    # ── Public API ─────────────────────────────────────────────────────────

    def plan(self, goal: str, context: Optional[dict] = None,
             memory_hints: str = "", app_name: str = "",
             ctx: Optional[MinimalContext] = None) -> Plan:
        """将自然语言目标分解为执行计划。

        Args:
            goal: 用户目标，如"帮我把这份报告发出去"。
            context: 可选上下文字典，可能包含字段：

                - ``window_title``: 当前焦点窗口标题
                - ``process_name``: 当前进程名
                - ``app_name``: 当前应用名（vscode / chrome / outlook 等）
                - ``clipboard``: 剪贴板文本内容
                - ``selected_text``: 当前选中的文本
                - ``open_apps``: 当前打开的应用列表
            memory_hints: 操作记忆建议文本（来自 OperationMemory），
                注入 LLM prompt 中作为高优先级参考。
            app_name: 当前应用名（用于缓存键区分）。

        Returns:
            Plan 对象，包含 steps、reasoning、estimated_time_ms 等。
        """
        if not goal.strip():
            return Plan(
                goal=goal,
                steps=[],
                reasoning="空目标，无步骤",
                total_steps=0,
                estimated_time_ms=0,
                confidence=0.0,
                source="error",
                error="目标为空",
            )

        # 从 context 中提取 app_name（若未显式传入）
        resolved_app = app_name or (context or {}).get("app_name", "")

        # ── 跨请求语义缓存检查 ──
        cached = self._cache.get(goal, resolved_app)
        if cached is not None:
            restored = _restore_plan_from_cache(cached)
            if restored is not None:
                return restored

        # ── AVR 路由决策 ──
        if self._router and ctx:
            decision = self._router.route(ctx)
            if not decision.should_call_llm:
                fallback = _make_fallback_plan(goal)
                fallback.error = f"AVR 跳过 LLM: {decision.reason}"
                return fallback

        try:
            plan_result = self._call_llm(goal, context or {}, memory_hints)
            # 缓存成功的 LLM 规划结果
            if plan_result.source == "llm" and plan_result.steps:
                self._cache.set(goal, resolved_app, plan_result.to_dict())
            return plan_result
        except Exception as e:
            self._last_error = str(e)
            fallback = _make_fallback_plan(goal)
            fallback.error = str(e)
            return fallback

    def execute_plan(
        self,
        plan: Plan,
        dry_run: bool = False,
    ) -> list[ExecutionResult]:
        """顺序执行计划中的每个步骤。

        Args:
            plan: 已生成的 Plan 对象。
            dry_run: True 时只记录，不真按键盘（用于预览）。

        Returns:
            每个 step 对应一个 ExecutionResult 的列表。
            注意：即使部分步骤失败也会继续执行，错误记录在对应 result 中。
        """
        results: list[ExecutionResult] = []
        platform = Platform.detect()

        if not plan.steps:
            return [
                ExecutionResult(
                    success=True,
                    intent=plan.goal,
                    command="noop",
                    mode="plan_step",
                    dry_run=dry_run,
                    confidence=1.0,
                )
            ]

        prev_action = None
        for step in plan.steps:
            # 输入文本后、回车前自动等待 1500ms：确保文件地址完整写入输入框，
            # 避免输入竞态导致 Enter 提前触发（规避“没等到 1500ms 就回车”）。
            if (step.action == "shortcut"
                    and (step.key_combination or "").strip().lower() == "enter"
                    and prev_action == "type"):
                if not dry_run:
                    time.sleep(1.5)
            result = self._map_to_keyboard_action(step, platform, dry_run)
            results.append(result)
            prev_action = step.action

        return results

    # ── LLM 推理 ────────────────────────────────────────────────────────────

    def _call_llm(self, goal: str, context: dict,
                  memory_hints: str = "") -> Plan:
        """调用 DeepSeek LLM 做目标分解推理。"""
        if not self._api_key:
            self._available = False
            raise RuntimeError("DeepSeek API Key 未配置（设置 DEEPSEEK_API_KEY 环境变量）")

        # 构建上下文信息字符串
        ctx_parts = []
        for key in ("window_title", "process_name", "app_name", "clipboard",
                    "selected_text", "open_apps"):
            val = context.get(key)
            if val:
                ctx_parts.append(f"- {key}: {val}")

        # 注入操作记忆建议（高优先级，在上下文信息之后）
        memory_parts: list[str] = []
        if memory_hints:
            memory_parts.append(
                f"## 用户历史操作习惯（请优先采纳，可覆盖默认快捷键选择）\n{memory_hints}"
            )

        user_prompt = (
            f"目标：{goal}\n"
            + (f"上下文信息：\n" + "\n".join(ctx_parts) + "\n" if ctx_parts else "")
            + ("\n".join(memory_parts) + "\n" if memory_parts else "")
            + "请按 JSON 格式输出计划。"
        )

        payload = json.dumps({
            "model": DEEPSEEK_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1200,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {err_body[:200]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}")
        elapsed = time.perf_counter() - start

        content = body["choices"][0]["message"]["content"].strip()

        # 解析 JSON（去除 markdown 代码块）
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            )

        import re as _re
        # 尝试提取 JSON 对象（处理可能的混入文字）
        json_match = _re.search(r'\{[\s\S]*\}', content)
        if not json_match:
            raise RuntimeError(f"无法从响应中提取 JSON：{content[:100]}")

        raw = json.loads(json_match.group())

        # 反序列化 steps
        steps: list[PlanStep] = []
        for d in raw.get("steps", []):
            try:
                steps.append(PlanStep.from_dict(d))
            except Exception:
                # 单个 step 解析失败不中断整体
                pass

        total_ms = raw.get("estimated_time_ms", 0)
        # 如果 LLM 没返回，用各步骤推算
        if not total_ms:
            total_ms = int((time.perf_counter() - start) * 1000) + sum(
                200 if s.action == "shortcut" else
                50 * len(s.text) if s.action == "type" else
                150 * s.n if s.action in ("tab",) else
                500 if s.action == "shell" else
                s.wait_ms
                for s in steps
            )

        return Plan(
            goal=goal,
            steps=steps,
            reasoning=raw.get("reasoning", ""),
            total_steps=len(steps),
            estimated_time_ms=total_ms,
            has_composite=any(s.action == "composite" for s in steps),
            confidence=float(raw.get("confidence", 0.8)),
            source="llm",
        )

    # ── 动作执行 ────────────────────────────────────────────────────────────

    def _get_adapter(self):
        """懒加载 adapter（避免 Windows 上程序启动时就初始化 GUI）。"""
        if self._adapter is None:
            from .adapter import create_adapter
            self._adapter = create_adapter()
        return self._adapter

    def _map_to_keyboard_action(
        self,
        step: PlanStep,
        platform: Platform,
        dry_run: bool,
    ) -> ExecutionResult:
        """将 PlanStep 映射为实际键盘动作并执行。"""
        start = time.perf_counter()
        success = True
        error: Optional[str] = None
        key_combo = ""

        try:
            adapter = self._get_adapter()

            if step.action == "shortcut":
                key_combo = step.key_combination
                if not dry_run:
                    adapter.send_keys(key_combo)
                    self._db_increment_shortcut(step.key_combination)

            elif step.action == "type":
                if not dry_run:
                    adapter.type_text(step.text)
                key_combo = f"[type] {step.text[:20]}{'…' if len(step.text) > 20 else ''}"

            elif step.action == "tab":
                n = max(1, step.n)
                direction = step.direction or "tab"
                if not dry_run:
                    for _ in range(n):
                        if direction == "shift_tab":
                            adapter.send_keys("Shift+Tab")
                        elif direction in ("left", "right", "up", "down"):
                            adapter.send_keys(direction.title())
                        else:
                            adapter.send_keys("Tab")
                key_combo = f"[tab {direction} x{n}]"

            elif step.action == "shell":
                cmd = step.command
                if not dry_run and cmd:
                    try:
                        if sys.platform == "win32":
                            proc = subprocess.run(
                                cmd, shell=True, capture_output=True, text=True,
                                timeout=30,
                            )
                        else:
                            proc = subprocess.run(
                                cmd, shell=True, capture_output=True, text=True,
                                timeout=30,
                            )
                        success = proc.returncode == 0
                        if not success:
                            error = f"shell 返回 {proc.returncode}: {proc.stderr[:120] or proc.stdout[:120]}"
                        key_combo = f"[shell/{'ok' if success else 'fail'}] {cmd[:40]}"
                    except subprocess.TimeoutExpired:
                        key_combo = f"[shell/timeout] {cmd[:40]}"
                        success = False
                        error = f"shell 超时: {cmd[:60]}"
                    except Exception as e:
                        key_combo = f"[shell/error] {cmd[:40]}"
                        success = False
                        error = f"shell 异常: {e}"
                else:
                    key_combo = f"[shell] {cmd[:30] if cmd else ''}"

            elif step.action == "wait":
                wait = max(0, step.wait_ms)
                if not dry_run:
                    time.sleep(wait / 1000.0)
                key_combo = f"[wait {wait}ms]"

            elif step.action == "composite":
                hint = step.composite_hint or ""
                key_combo = f"[composite] {hint[:40]}"
                if not dry_run and hint:
                    try:
                        from .composites import (
                            CompositeExecutor,
                            CompositePlan,
                            make_file_copy_context_menu,
                            make_file_move_context_menu,
                            make_file_search_keyboard,
                            make_open_folder_navigate,
                            make_terminal_copy_to_folder,
                            make_terminal_move_to_folder,
                            make_generic_composite,
                        )
                        hint_lower = hint.lower()
                        step_text = getattr(step, "text", "") or ""

                        # Extract destination path from hint
                        dest_path = _resolve_folder_path(hint)

                        plan: Optional[CompositePlan] = None

                        # ── 路由表：终端优先 → 键盘 → 视觉降级 ──
                        is_copy = "copy" in hint_lower or "复制" in hint_lower or "拷贝" in hint_lower
                        is_move = "move" in hint_lower or "移动" in hint_lower or "剪切" in hint_lower
                        is_find = "find" in hint_lower or "查找" in hint_lower or "搜索" in hint_lower or "search" in hint_lower
                        is_open  = "open" in hint_lower or "打开" in hint_lower or "导航" in hint_lower or "跳转" in hint_lower

                        if is_copy and not step_text:
                            # 1st: terminal (PowerShell, 零视觉依赖)
                            try:
                                plan = make_terminal_copy_to_folder(
                                    source_pattern="*",   # copy all selected items
                                    dest_pattern=dest_path,
                                    search_root=".",
                                )
                            except Exception:
                                pass
                            # 2nd: keyboard (右击菜单，需要视觉模型)
                            if plan is None:
                                plan = make_file_copy_context_menu(
                                    source_desc=hint,
                                    dest_path=dest_path,
                                )

                        elif is_move and not step_text:
                            try:
                                plan = make_terminal_move_to_folder(
                                    source_pattern="*",
                                    dest_pattern=dest_path,
                                    search_root=".",
                                )
                            except Exception:
                                pass
                            if plan is None:
                                plan = make_file_move_context_menu(
                                    source_desc=hint,
                                    dest_path=dest_path,
                                )

                        elif is_find:
                            plan = make_file_search_keyboard(pattern=hint)

                        elif is_open:
                            plan = make_open_folder_navigate(
                                folder_hint=hint,
                                target_path=dest_path,
                            )

                        else:
                            plan = make_generic_composite(
                                hint=hint, text=step_text,
                                keys=getattr(step, "key_combination", "") or "",
                            )

                        if plan:
                            executor = CompositeExecutor(adapter=adapter)
                            exec_results = executor.execute(plan)
                            success = all(r["success"] for r in exec_results)
                            if not success:
                                failed = [r["message"] for r in exec_results if not r["success"]]
                                error = "; ".join(failed[:3])
                    except Exception as e:
                        error = f"composite execution failed: {e}"
                        success = False

            else:
                error = f"未知 action 类型：{step.action}"
                success = False

        except Exception as e:
            error = str(e)
            success = False

        elapsed = time.perf_counter() - start

        return ExecutionResult(
            success=success,
            intent=step.description,
            command=step.action,
            key_combination=key_combo,
            platform=platform.value,
            processing_time=elapsed,
            error=error,
            mode="plan_step",
            dry_run=dry_run,
            confidence=step.confidence,
        )

    def _db_increment_shortcut(self, key_combination: str) -> None:
        """如果 key_combination 命中已注册的 shortcut 命令，累加其频率。"""
        # 复用注入或延迟初始化的 DatabaseManager 单例
        # （原实现每次调用都新建 DatabaseManager，含 PRAGMA + 检查，开销 5-10ms）
        try:
            if self._db is None:
                from .database import DatabaseManager
                from pathlib import Path
                config_dir = Path.home() / ".nl2shortcut"
                self._db = DatabaseManager(config_dir / "shortcuts.db")
            db = self._db
            # 根据 key_combination 查找对应 command 并累加频率
            # 简化：key 格式如 "Ctrl+C"，查找 command="copy" 的条目
            reverse_map = {
                "Ctrl+C": "copy", "Ctrl+V": "paste", "Ctrl+X": "cut",
                "Ctrl+S": "save", "Ctrl+Z": "undo", "Ctrl+Y": "redo",
                "Ctrl+A": "select_all", "Ctrl+F": "find",
                "Ctrl+N": "new", "Ctrl+W": "close_tab",
                "Alt+F4": "close_window", "Alt+S": "send",
                "Enter": "enter", "Tab": "tab", "Escape": "escape",
                "Backspace": "backspace",
            }
            cmd = reverse_map.get(key_combination.strip())
            if cmd:
                s = db.get_by_command(cmd)
                if s:
                    db.increment_frequency(cmd)
        except Exception:
            # 频率统计失败不影响主流程
            pass


# ─────────────────────────────────────────────────────────────────────────
# Plan → YAML Workflow 自动保存
# ─────────────────────────────────────────────────────────────────────────

def plan_to_workflow(
    plan: Plan,
    dest_dir: Optional[Path] = None,
    overwrite: bool = False,
) -> Optional[Path]:
    """将 LLM 拆解出的 Plan 自动保存为可复用的 YAML 工作流文件。

    保存位置：``~/.nl2shortcut/workflows/``（默认）或 ``dest_dir``。

    Args:
        plan: GoalPlanner 生成的执行计划。
        dest_dir: 输出目录，默认 ``~/.nl2shortcut/workflows/``。
        overwrite: 是否覆盖已有同名文件（默认跳过）。

    Returns:
        成功时返回文件路径，跳过/失败时返回 None。

    工作流名称生成
    ----------------
    从 goal 文本中提取关键词作文件名（去特殊字符、限长）。
    例如 goal="帮我把报告发出去" → 文件名 "帮我_把_报告_发_出去.yaml"
    """
    import re
    import yaml

    _ROOT = Path.home()
    target_dir = dest_dir or (_ROOT / ".nl2shortcut" / "workflows")
    target_dir.mkdir(parents=True, exist_ok=True)

    if not plan.steps:
        return None

    # 从 goal 文本中提取安全文件名
    raw = plan.goal.strip()
    # 保留中文、英文、数字、空格、下划线
    safe = re.sub(r'[^\u4e00-\u9fff\w\s]', '', raw)
    safe = re.sub(r'\s+', '_', safe)[:40]
    if not safe:
        safe = "auto_workflow"
    filepath = target_dir / f"{safe}.yaml"

    if filepath.exists() and not overwrite:
        return None

    # PlanStep → workflow step
    wf_steps = []
    for step in plan.steps:
        wf_step = {
            "name": step.description,
            "action": step.action,
        }
        if step.action == "shortcut":
            wf_step["command"] = step.key_combination
        elif step.action == "type":
            wf_step["command"] = step.text
        elif step.action == "shell":
            wf_step["command"] = step.command
        elif step.action == "tab":
            wf_step["command"] = f"{step.direction} {step.n}" if step.n > 1 else step.direction
        elif step.action in ("wait",):
            wf_step["command"] = str(step.wait_ms / 1000.0) if step.wait_ms else "1"
        elif step.action == "composite":
            wf_step["command"] = f"[composite] {step.composite_hint[:60]}"
        else:
            wf_step["command"] = step.key_combination or step.text or ""
        wf_steps.append(wf_step)

    doc = {
        "name": safe,
        "description": plan.reasoning or f"Auto-generated from: {plan.goal}",
        "version": "1.0",
        "variables": {},
        "steps": wf_steps,
    }

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Auto-generated by GoalPlanner\n")
            f.write(f"# Goal: {plan.goal}\n")
            f.write(f"# Confidence: {plan.confidence:.0%}  |  Steps: {plan.total_steps}\n")
            yaml.dump(doc, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return filepath
    except Exception:
        return None


def _restore_plan_from_cache(d: dict) -> Optional[Plan]:
    """从缓存 dict 恢复 Plan 对象。"""
    try:
        steps = [PlanStep.from_dict(s) for s in d.get("steps", [])]
        return Plan(
            goal=d.get("goal", ""),
            steps=steps,
            reasoning=d.get("reasoning", ""),
            total_steps=d.get("total_steps", len(steps)),
            estimated_time_ms=d.get("estimated_time_ms", 0),
            has_composite=d.get("has_composite", False),
            confidence=d.get("confidence", 1.0),
            source="cache",
        )
    except Exception:
        return None
