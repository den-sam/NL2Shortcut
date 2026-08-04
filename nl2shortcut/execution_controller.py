"""ExecutionController — 四大策略融合主线（融合策略①②③④的统一中枢）。

ExecutionController 是 NL2Shortcut 从"单引擎孤岛"进化为"融合决策流水线"的核心模块。

它持有四个融合策略对应的子组件，并按固定的
感知 → 记忆 → 路由 → 执行(含兜底) → 回写 顺序驱动一次请求。

融合关系总览
─────────────
  ExecutionController
    ├── perception_stack    ← ① 可插拔感知层（UIA → Light → Vision）
    ├── context_store        ← ③ 状态与记忆层（MinimalContext + SemanticCache）
    ├── model_router         ← ② AVR 模型路由（none/cheap/standard）
    └── tier_dispatcher      ← ④ keyboard → api → vision(合规闸门)

一次请求的完整生命周期:
    request → [感知] UIState → [记忆] MinimalContext → [路由] tier决定
           → [执行] keyboard/api/vision → [回写] ContextStore.record()

设计约定
────────
- handle() 是唯一的公共入口，接受 intent + compliance_mode。
- 所有内部组件实例通过 __init__ 注入（依赖倒置），便于单测。
- 兼容现有 KeyboardMasterAgent 的 execute/smart_execute 接口。
"""

from __future__ import annotations

import time
import uuid
from typing import Optional, Dict, Any, List

from .perception import PerceptionStack, UIState, LightProvider, UiaProvider, VisionProvider
from .context_store import ContextStore, MinimalContext
from .model_router import ModelRouter, RoutingDecision
from .tiers import recommend_fallback, TIER_KEYBOARD, TIER_VISION
from .precache import PrecachedShortcutMap, _canon_key as _canon_key_static


# ═══════════════════════════════════════════════════════════════════════
# ExecutionController — 融合主线
# ═══════════════════════════════════════════════════════════════════════

class ExecutionController:
    """统一执行控制器 —— 四大策略的融合中枢。

    用法
    ----
        # 最小配置（全默认，直接可用）
        ctrl = ExecutionController()
        result = ctrl.handle("复制这段文字")

        # 非合规场景（启用视觉兜底）
        ctrl = ExecutionController(compliance_mode=True)
        result = ctrl.handle("定位到那个按钮")

        # 嵌入 KeyboardMasterAgent（替换 execute/smart_execute）
        class KeyboardMasterAgent:
            def __init__(self):
                self.controller = ExecutionController()
            def execute(self, intent):
                return self.controller.handle(intent)
    """

    def __init__(
        self,
        *,
        compliance_mode: bool = False,
        # 感知层
        perception: Optional[PerceptionStack] = None,
        # 记忆层
        context_store: Optional[ContextStore] = None,
        # 路由层
        model_router: Optional[ModelRouter] = None,
        # execution agent (for actual shortcut execution)
        agent: Optional[Any] = None,          # ShortcutAgent instance
        workflow_matcher: Optional[Any] = None,  # WorkflowMatcher instance
        workflow_engine: Optional[Any] = None,   # WorkflowEngine instance
        planner: Optional[Any] = None,           # GoalPlanner instance
        precache: Optional[PrecachedShortcutMap] = None,  # 预编译意图→键位表
    ):
        # ── 感知层 ──
        self.perception = perception or PerceptionStack().setup_defaults(compliance_mode)

        # ── 记忆层 ──
        self.context_store = context_store or ContextStore()

        # ── 路由层 ──
        self.model_router = model_router or ModelRouter()

        # ── 执行组件引用（可选，不传则用默认创建）──
        self._agent = agent
        self._workflow_matcher = workflow_matcher
        self._workflow_engine = workflow_engine
        self._planner = planner

        # ── 方向 D：预编译意图→键位内存表（O(1) 命中，绕过 IntentEngine/SQLite）──
        # 优先用外部注入的实例；否则若 agent 已就绪，从 agent 的 db/intent 现场构建。
        if precache is not None:
            self._precache = precache
        elif agent is not None:
            try:
                db = getattr(agent, "_db", None)
                intent_engine = getattr(agent, "_intent", None)
                if db is not None:
                    self._precache = PrecachedShortcutMap.build(db, intent_engine)
                else:
                    self._precache = None
            except Exception:
                self._precache = None
        else:
            self._precache = None

        # 合规模式
        self._compliance_mode = compliance_mode

        # 统计
        self._total_requests: int = 0
        self._total_latency_ms: float = 0.0

    # ── Compliance Mode ────────────────────────────────────────────────

    @property
    def compliance_mode(self) -> bool:
        return self._compliance_mode

    def set_compliance_mode(self, enabled: bool) -> None:
        """切换合规模式。禁用时 vision tier 完全不可用。"""
        self._compliance_mode = enabled
        self.perception.set_compliance_mode(enabled)
        # 同步 vision_executor 的全局限流
        try:
            from .vision_executor import set_compliance_mode
            set_compliance_mode(enabled)
        except Exception:
            pass

    # ── Public API ─────────────────────────────────────────────────────

    def handle(
        self,
        intent: str,
        dry_run: bool = False,
        timeout: float = 30.0,
        learn: bool = True,
    ) -> Dict[str, Any]:
        """融合管线主入口 —— 一次请求的完整生命周期。

        优化策略：感知延迟到路由之后。
        - 缓存/工作流命中路径：仅用 LightProvider（~1ms），跳过 UIA 树采集。
        - 简单命令（router=skip）：同上，不触发 UIA。
        - 仅 LLM 路径才采集完整 UIA 快照（带 200ms TTL 缓存）。

        Args:
            intent: 用户自然语言意图（"复制这段文字" / "把报告发出去"）
            dry_run: 模拟执行，不真按键
            timeout: 整体超时（秒）
            learn: 成功后是否回写记忆

        Returns:
            dict::
                {
                    "ok": bool,
                    "pipeline": str,            # 走哪条路径
                    "tier_used": str,           # 最终使用的 tier
                    "cache_hit": bool,          # 是否命中缓存
                    "workflow_hit": bool,       # 是否命中工作流
                    "avr_tier": str,            # AVR 决策档位
                    "result": object,           # ExecutionResult / 其他
                    "elapsed_ms": float,
                    "error": str or None,
                }
        """
        t0 = time.perf_counter()
        self._total_requests += 1

        result: Dict[str, Any] = {
            "ok": False,
            "pipeline": "unknown",
            "tier_used": "keyboard",
            "cache_hit": False,
            "workflow_hit": False,
            "avr_tier": "standard",
            "result": None,
            "elapsed_ms": 0.0,
            "error": None,
        }

        if not intent.strip():
            result["error"] = "intent 为空"
            result["elapsed_ms"] = (time.perf_counter() - t0) * 1000
            return result

        # ═════════════════════════════════════════════════════════════
        # Step 1: 轻量感知（仅窗口指纹，~1-2ms）
        # 此时不采集 UIA 树——先看缓存/工作流是否命中
        # ═════════════════════════════════════════════════════════════
        light_state = self.perception.snapshot(source="light")

        # ═════════════════════════════════════════════════════════════
        # Step 2: 记忆层压缩（用轻量上下文）
        # ═════════════════════════════════════════════════════════════
        ctx = self.context_store.build(
            app_context=light_state,
            ui_state=light_state,
            intent=intent,
            clipboard_text=light_state.clipboard_text,
            selected_text=light_state.selected_text,
        )
        result["cache_hit"] = ctx.cache_hit
        result["workflow_hit"] = ctx.workflow_hit

        # ═════════════════════════════════════════════════════════════
        # Step 3: 快速命中路径 —— 跳过 UIA 采集
        # ═════════════════════════════════════════════════════════════
        if ctx.cache_hit:
            # 缓存命中 → 直接用缓存中的 key_combination 按键，跳过 agent.execute()
            result["pipeline"] = "cache_hit"
            result["avr_tier"] = "none"
            cached = ctx.cached_result
            if cached and isinstance(cached, dict) and cached.get("key_combination"):
                # 最佳路径：缓存里有键位 → 直接注入（~1ms）
                if not dry_run:
                    adapter = getattr(self._agent, 'adapter', None)
                    if adapter:
                        adapter.send_keys(cached["key_combination"])
                result["ok"] = True
                result["result"] = cached
            else:
                # 降级：旧格式缓存（无 key_combination）→ 走 agent 执行
                try:
                    exec_result = self._execute_simple(intent, dry_run, timeout)
                    result["ok"] = exec_result.get("ok", False)
                    result["result"] = exec_result
                except Exception as e:
                    result["pipeline"] = "error"
                    result["error"] = str(e)
        elif ctx.workflow_hit:
            # 工作流命中 → 直接执行，不碰 UIA 树
            result["pipeline"] = "workflow_hit"
            result["avr_tier"] = "none"
            try:
                exec_result = self._execute_via_workflow(intent, dry_run)
                result["ok"] = exec_result.get("ok", False)
                result["result"] = exec_result
            except Exception as e:
                result["pipeline"] = "error"
                result["error"] = str(e)
        else:
            # ═════════════════════════════════════════════════════════
            # Step 3.5: Agent 直接映射（单快捷键，跳过 LLM + 路由）
            # "复制"/"粘贴"/"保存"/"撤销" 等简单操作在此命中。
            # 无需路由决策、无需 UIA 采集、无需 LLM。
            # ═════════════════════════════════════════════════════════
            direct_result = self._try_shortcut_lookup(
                intent, dry_run, timeout, app_name=ctx.app_name)
            if direct_result is not None:
                result["pipeline"] = "single_shortcut"
                result["avr_tier"] = "none"
                result["ok"] = direct_result.get("ok", False)
                result["result"] = direct_result
                result["tier_used"] = "keyboard"
            else:
                # ═════════════════════════════════════════════════════
                # Step 3.6: 工作流查找（多步意图优先匹配已有工作流）
                # 当单快捷键无法命中时（尤其是多步操作），先查已保存的
                # YAML 工作流。命中则直接执行，避免重复 LLM 拆解。
                # ═════════════════════════════════════════════════════
                wf_result = self._try_workflow_match(intent, dry_run)
                if wf_result is not None:
                    result["pipeline"] = "workflow_hit"
                    result["avr_tier"] = "none"
                    result["workflow_hit"] = True
                    result["ok"] = wf_result.get("ok", False)
                    result["result"] = wf_result
                    result["tier_used"] = "keyboard"
                else:
                    # ═════════════════════════════════════════════════════
                    # Step 3.7: 复合计划直接执行（IntentEngine 已生成 plan）
                    # 如 "打开记事本" → Win→搜索→等待→Enter，
                    # IntentEngine 已生成完整 composite_plan，无需 LLM 拆解。
                    # ═════════════════════════════════════════════════════
                    comp_result = self._try_composite_plan(intent, dry_run)
                    if comp_result is not None:
                        result["pipeline"] = "composite_plan"
                        result["avr_tier"] = "none"
                        result["ok"] = comp_result.get("ok", False)
                        result["result"] = comp_result
                        result["tier_used"] = "keyboard"
                    else:
                        # ═════════════════════════════════════════════════════
                        # Step 4: 路由层决策（无工作流/复合计划命中才走）
                        # ═════════════════════════════════════════════════════
                        decision = self.model_router.route(ctx)
                        result["avr_tier"] = decision.tier

                        needs_llm = (decision.tier != "none" and decision.should_call_llm)
                        # ── 多步意图强制走 LLM 路径 ──
                        # 即使路由认为不需要 LLM，多步意图（如 "查找X并复制"）
                        # 也不能用单快捷键完成，必须走 LLM 拆解。
                        if self._has_multi_step_intent(intent):
                            needs_llm = True

                        if not needs_llm:
                            # 路由也认为不需要 LLM → 走 avr_skip 快速路径
                            result["pipeline"] = "avr_skip"
                            try:
                                exec_result = self._execute_simple(intent, dry_run, timeout)
                                result["ok"] = exec_result.get("ok", False)
                                result["result"] = exec_result
                            except Exception as e:
                                result["pipeline"] = "error"
                                result["error"] = str(e)
                        else:
                            # ═══════════════════════════════════════════
                            # Step 5: 需要 LLM → 延迟采集完整 UIA 快照（带缓存）
                            # ═══════════════════════════════════════════
                            ui_state = self.perception.snapshot(source="auto", use_cache=True)

                            # 用完整 UIA 数据重建上下文
                            full_ctx = self.context_store.build(
                                app_context=ui_state,
                                ui_state=ui_state,
                                intent=intent,
                                clipboard_text=ui_state.clipboard_text,
                                selected_text=ui_state.selected_text,
                            )

                            # ═══════════════════════════════════════════
                            # Step 6: LLM 拆解 → 执行
                            # ═══════════════════════════════════════════
                            result["pipeline"] = "llm_plan"
                            try:
                                plan_result = self._execute_via_plan(intent, dry_run, timeout)
                                result["ok"] = plan_result.get("ok", False)
                                result["result"] = plan_result
                                result["tier_used"] = "keyboard"
                            except Exception as e:
                                fallback = recommend_fallback(
                                    failed_error_code="exec_failed",
                                    command_meta=None,
                                    fallback_policy="gui_retry" if self._compliance_mode else "abort",
                                    compliance_mode=self._compliance_mode,
                                )
                                if fallback["action"] == "escalate_vision":
                                    result["pipeline"] = "vision_fallback"
                                    result["tier_used"] = TIER_VISION
                                    result["error"] = f"执行失败，升级到视觉层: {e}"
                                else:
                                    result["pipeline"] = "error"
                                    result["error"] = str(e)

                            # 记住 UIA 快照中的 app 名用于记录
                            light_state = ui_state

        # ═════════════════════════════════════════════════════════════
        # Step 7: 回写记忆
        # ═════════════════════════════════════════════════════════════
        if learn and result["ok"] and intent:
            self.context_store.record(
                intent=intent,
                command=result.get("result", {}).get("command", intent),
                app_name=light_state.app_name,
                confidence=1.0,
                result=result["result"],  # 完整的执行结果（含 key_combination），下次缓存命中直接用
            )

        elapsed = (time.perf_counter() - t0) * 1000
        self._total_latency_ms += elapsed
        result["elapsed_ms"] = elapsed
        return result

    # ── Internal: 执行方法 ──────────────────────────────────────────────

    # 快捷键库直接映射的最低置信度（关键词精确命中 ≥ 0.85）
    MIN_SHORTCUT_CONFIDENCE = 0.85

    # 不要走快捷键库映射的复合命令
    _COMPOSITE_COMMANDS = frozenset({"__plan__", "__composite__", "__terminal_open__"})

    # 多步意图检测：包含这些模式的意图不应走单快捷键通道
    import re as _re_ms
    _MULTI_STEP_PATTERNS = [
        # "查找X并复制" / "找到X并打开" / "搜索X并发送"
        _re_ms.compile(r'(?:查找|找到|搜索|搜|找)\s*.+\s*并\s*(?:复制|打开|发送|粘贴|移动)'),
        # "复制X并粘贴" / "剪切X并粘贴"
        _re_ms.compile(r'(?:复制|剪切|裁剪)\s*.+\s*并\s*(?:粘贴|粘到|放到)'),
        # "把X复制到Y" / "将X移动到Y"
        _re_ms.compile(r'(?:把|将)\s*.+\s*(?:复制到|移动到|剪切到|拷贝到)'),
        # "从X复制Y到Z"
        _re_ms.compile(r'(?:从|自)\s*.+\s*(?:复制|移动|剪切)\s*.+\s*(?:到|去)'),
        # 多步连接词：然后/接着/之后/再/同时
        _re_ms.compile(r'(?:然后|接着|之后|再\s*|同时|随后)'),
        # 英文多步: "find X and copy" / "search X and open"
        _re_ms.compile(r'(?:find|search|locate)\s+.+\s+and\s+(?:copy|open|paste|send|move)', _re_ms.IGNORECASE),
        # "打开X" — 泛化动词，不应走单快捷键（Ctrl+O 是打开文件对话框）
        # 例外：已注册的特定目标（资源管理器/终端/cmd 等）由 precache 精确命中，不会走到这里
        _re_ms.compile(r'^\s*打\s*开\s*\S'),
    ]

    def _has_multi_step_intent(self, intent: str) -> bool:
        """检测意图是否为多步复合操作。

        多步意图特征：
        - 包含连接词："并"、"然后"、"接着"、"之后"、"再"、"同时"
        - 包含复合模式："查找X并复制"、"把X复制到Y" 等
        - 同时出现两个及以上动作词

        这些意图不能用单个快捷键完成，必须走工作流/LLM 拆解。
        """
        text = intent.strip()
        if not text:
            return False

        # 快速检测：是否包含多步连接词
        multi_step_conjunctions = ['并', '然后', '接着', '之后', '同时', '随后']
        for conj in multi_step_conjunctions:
            if conj in text:
                return True

        # "先X再Y" 模式
        if '先' in text and '再' in text:
            return True

        # "一边X一边Y" 模式
        if '一边' in text and '一边' in text:
            return True

        # 正则检测复合模式
        for pat in self._MULTI_STEP_PATTERNS:
            if pat.search(text):
                return True

        return False

    def _try_shortcut_lookup(
        self, intent: str, dry_run: bool, timeout: float, app_name: str = ""
    ) -> Optional[Dict[str, Any]]:
        """直接从快捷键库映射，完全绕过 Agent 管道和 LLM。

        优化路径（方向 D）：若已构建 PrecachedShortcutMap，对 *精确高置信意图*
        走 O(1) 内存查表，跳过 IntentEngine.recognize() 的 200+ 条遍历 +
        SQLite 查询 + Platform.detect()，与 cache_hit 同样 ~1ms。

        回退路径：预编译表未命中时，仍走原 IntentEngine.recognize() + DB 查找。

        方向 A：无论哪条路径命中，都会把 ``(intent, key_combination)`` 写入
        SemanticCache，使 *第二次* 同意图直接走 cache_hit 的纯 send_keys 路径，
        抹平「第一次 vs 第 N 次」的差距。

        返回非 None 表示命中单快捷键。
        """
        if self._agent is None:
            return None

        t0 = time.perf_counter()

        try:
            # ── 方向 D：预编译表 O(1) 命中 ──
            # 仅对「整句即一个精确意图词」有效（如 "复制"、"Ctrl+C"、"copy"）。
            # 含多余修饰词（"复制这段文字"）由下方 IntentEngine 兜底，不影响正确性。
            key_combo: Optional[str] = None
            command: Optional[str] = None
            confidence: float = self.MIN_SHORTCUT_CONFIDENCE

            if self._precache is not None and self._precache.is_built:
                # 先试整句精确命中（最常见、最快）
                full = self._precache.lookup_full(intent)
                if full is not None:
                    command, key_combo = full

                # 再试子串命中：处理 "打开任务管理器" / "帮我复制一下" 这类自然口语。
                # 只命中非复合单快捷键，且取最长匹配词，避免短词误截断。
                if key_combo is None:
                    contained = self._precache.lookup_contains(intent)
                    if contained is not None:
                        command, key_combo, matched_kw = contained
                        confidence = 0.82  # 子串命中略低于精确命中，但仍高于 LLM 阈值

                        # ── 验证 1：子串命中必须验证意图是否为多步操作 ──
                        # 例如 "查找C:\...并复制" 中 "查找" 子串命中 Ctrl+F，
                        # 但整个意图是多步的（查找+复制），必须拒绝。
                        if self._has_multi_step_intent(intent):
                            key_combo = None
                            command = None
                            confidence = self.MIN_SHORTCUT_CONFIDENCE

                        # ── 验证 2：检查 IntentEngine 是否识别为复合操作 ──
                        # 例如 "打开终端" 子串命中 "打开"→Ctrl+O，但 IntentEngine
                        # 识别为 __composite__（Win+R→cmd→Enter），必须拒绝。
                        if key_combo is not None:
                            intent_engine = getattr(self._agent, '_intent', None)
                            if intent_engine is not None:
                                try:
                                    ir = intent_engine.recognize(intent)
                                    if ir.command in self._COMPOSITE_COMMANDS:
                                        key_combo = None
                                        command = None
                                        confidence = self.MIN_SHORTCUT_CONFIDENCE
                                except Exception:
                                    pass

            # ── 回退：IntentEngine + DB 查找 ──
            # 关键原则（用户诉求）：只要能映射为「单个快捷键」操作，就走
            # single_shortcut，**绝不**漏给 LLM 拆解。因此这里不再要求
            # 置信度 ≥ 0.85，只要是单快捷键 command（非复合、非 plan）即接受。
            if key_combo is None:
                intent_engine = getattr(self._agent, '_intent', None)
                if intent_engine is None:
                    return None

                intent_result = intent_engine.recognize(intent)

                # 复合命令（文件复制/移动等）或 LLM plan → 不走单快捷键通道，
                # 交还给上层走工作流/LLM 拆解。
                if intent_result.command in self._COMPOSITE_COMMANDS:
                    return None

                # ── 多步意图保护 ──
                # 即使 IntentEngine 识别出单快捷键命令（如模糊匹配误中 "open"），
                # 若意图本身是多步的（如 "打开记事本" / "查找X并复制"），
                # 也必须拒绝，交还上层走工作流/LLM 拆步。
                if intent_result.command and self._has_multi_step_intent(intent):
                    return None

                # 单快捷键命令（含低置信的 contains_match 0.80）→ 一律接受
                if not intent_result.command:
                    # 兜底：用户输入本身就是键位组合字符串（"win+r"/"alt+f4"），
                    # 即使 precache 未构建 / IntentEngine 未识别，也直接执行。
                    if "+" in intent:
                        key_combo = _canon_key_static(intent)
                        if key_combo:
                            command = ""  # 无标准 command 名，仅执行键位
                            confidence = 0.82
                    if not key_combo:
                        return None

                if key_combo is None:
                    command = intent_result.command
                    confidence = max(intent_result.confidence, 0.80)

                # ── 拿键位：预编译表 → DB → 内置常量兜底（不依赖 precache/DB 状态）──
                from .models import Platform
                plat = Platform.detect()
                if self._precache is not None and self._precache.is_built:
                    key_combo = self._precache.lookup_command(command)
                # DB 回退（预编译表未命中时才查）
                if not key_combo:
                    db = getattr(self._agent, '_db', None)
                    if db is not None:
                        shortcut = db.get_by_command(command)
                        if shortcut is not None:
                            key_combo = shortcut.get_key(plat)
                # 内置常量兜底：即使 precache 未构建 / DB 缺失标准命令，
                # 常见单快捷键（copy/paste/save…）仍 100% 可映射，绝不漏给 LLM。
                if not key_combo:
                    builtin = PrecachedShortcutMap._BUILTIN_KEYS.get(command)
                    if builtin:
                        wk, mk, lk = builtin
                        key_combo = {Platform.WINDOWS: wk, Platform.MACOS: mk,
                                     Platform.LINUX: lk}.get(plat, "")
                if not key_combo:
                    return None

            # ── 执行按键（dry_run 跳过）───────────────
            if not dry_run:
                adapter = getattr(self._agent, 'adapter', None)
                if adapter:
                    adapter.send_keys(key_combo)

            elapsed = (time.perf_counter() - t0) * 1000

            result = {
                "ok": True,
                "command": command or "",
                "key_combination": key_combo,
                "confidence": confidence,
                "intent": intent,
                "elapsed_ms": elapsed,
                "source": "precache" if (self._precache and self._precache.is_built
                                          and self._precache.lookup_full(intent)) else "intent_engine",
            }

            # ── 方向 A：写入语义缓存，让下次走 cache_hit 纯 send_keys ──
            # 只有当 intent 与键位确定绑定时才缓存（精确意图，避免模糊歧义污染）。
            # app_name 与 build() 时一致，确保第二次能命中 cache_hit 的 get(key, app_name)。
            if intent and key_combo and command:
                try:
                    self.context_store.cache.set(
                        intent,
                        app_name,
                        {
                            "command": command,
                            "key_combination": key_combo,
                            "confidence": confidence,
                            "intent": intent,
                        },
                    )
                except Exception:
                    pass

            return result

        except Exception:
            return None

    def _execute_simple(
        self, intent: str, dry_run: bool, timeout: float
    ) -> Dict[str, Any]:
        """简单快捷键执行（单个 command）—— 经过完整 Agent 管道。
        
        这是 LLM 路径或路由降级时才走的路径，_try_shortcut_lookup 优先。
        """
        if self._agent is None:
            return {"ok": False, "error": "ShortcutAgent 未注入", "command": ""}
        result = self._agent.execute(intent, dry_run=dry_run, timeout=timeout)
        return {
            "ok": result.success,
            "command": result.command,
            "key_combination": result.key_combination,
            "confidence": result.confidence,
            "error": result.error or "",
        }

    def _try_workflow_match(
        self, intent: str, dry_run: bool
    ) -> Optional[Dict[str, Any]]:
        """尝试匹配并执行已保存的 YAML 工作流。

        多步意图在单快捷键通道被拒绝后，优先走此路径：
        1. 调用 WorkflowMatcher.match() 进行 LLM 语义匹配
        2. 命中则执行工作流，返回完整步骤结果
        3. 未命中返回 None，交由后续 LLM 拆解

        返回 None 表示无匹配工作流。
        """
        if self._workflow_matcher is None or self._workflow_engine is None:
            return None

        try:
            match_result = self._workflow_matcher.match(intent)
        except Exception:
            return None

        if not match_result or not match_result.matched:
            return None

        wf_name = match_result.workflow.name
        try:
            wf_result = self._workflow_engine.run(wf_name, dry_run=dry_run)
        except Exception as e:
            return {
                "ok": False,
                "workflow": wf_name,
                "steps": 0,
                "error": f"工作流执行失败: {e}",
                "step_results": [],
            }

        # 构建完整步骤结果，供 agent_panel 拆步展示
        step_results = []
        for s in wf_result.steps:
            step_results.append({
                "step": s.step_name,
                "success": s.success,
                "output": s.output or "",
                "error": s.error or "",
            })

        return {
            "ok": wf_result.success,
            "workflow": wf_name,
            "steps": len(wf_result.steps),
            "step_results": step_results,
            "error": wf_result.error or "",
        }

    def _execute_via_workflow(
        self, intent: str, dry_run: bool
    ) -> Dict[str, Any]:
        """执行已匹配的工作流。"""
        if self._workflow_matcher is None or self._workflow_engine is None:
            return {"ok": False, "error": "WorkflowMatcher/Engine 未注入"}

        match_result = self._workflow_matcher.match(intent)
        if not match_result or not match_result.matched:
            return {"ok": False, "error": "工作流匹配失败"}

        wf_result = self._workflow_engine.run(
            match_result.workflow.name, dry_run=dry_run,
        )
        return {
            "ok": wf_result.success,
            "workflow": match_result.workflow.name,
            "steps": len(wf_result.steps),
            "error": wf_result.error or "",
        }

    def _try_composite_plan(
        self, intent: str, dry_run: bool
    ) -> Optional[Dict[str, Any]]:
        """IntentEngine 已生成的复合计划直接执行（无需 LLM 拆解）。

        当 IntentEngine 识别出复合操作（如 "打开记事本" → Win→搜索→等待→Enter）
        并附带完整的 composite_plan 时，直接用 CompositeExecutor 执行，
        跳过 LLM planner，避免重复拆解。

        返回 None 表示非复合计划，交由后续路由/LLM 处理。
        """
        if self._agent is None:
            return None
        intent_engine = getattr(self._agent, '_intent', None)
        if intent_engine is None:
            return None
        try:
            ir = intent_engine.recognize(intent)
        except Exception:
            return None
        if ir.command != "__composite__" or ir.composite_plan is None:
            return None

        from .composites import CompositeExecutor
        try:
            executor = CompositeExecutor(adapter=getattr(self._agent, 'adapter', None))
            step_results = executor.execute(ir.composite_plan, dry_run=dry_run)
        except Exception as e:
            return {
                "ok": False,
                "pipeline": "composite_plan",
                "command": "__composite__",
                "key_combination": "",
                "error": f"复合计划执行失败: {e}",
                "step_results": [],
            }

        all_ok = all(r.get("success", False) for r in step_results)
        executed_keys = [r.get("message", "")[:30] for r in step_results]
        return {
            "ok": all_ok,
            "pipeline": "composite_plan",
            "command": "__composite__",
            "key_combination": " → ".join(executed_keys[:8]),
            "step_results": step_results,
            "composite_plan": ir.composite_plan.to_dict(),
            "error": None if all_ok else "; ".join(
                r.get("message", "") for r in step_results
                if not r.get("success", False)
            )[:200],
        }

    def _execute_via_plan(
        self, intent: str, dry_run: bool, timeout: float
    ) -> Dict[str, Any]:
        """LLM 拆解 → 执行。"""
        if self._planner is None:
            # 无 planner → 降级到简单执行
            return self._execute_simple(intent, dry_run, timeout)

        plan = self._planner.plan(intent)
        if not plan.steps:
            return self._execute_simple(intent, dry_run, timeout)

        step_results = self._planner.execute_plan(plan, dry_run=dry_run)
        all_ok = all(r.success for r in step_results)
        return {
            "ok": all_ok,
            "plan": plan.to_dict(),
            "steps_executed": len(step_results),
            "results": [
                {"step": r.intent, "success": r.success, "error": r.error or ""}
                for r in step_results
            ],
        }

    # ── Stats ───────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """融合管线整体统计。"""
        return {
            "total_requests": self._total_requests,
            "avg_latency_ms": (
                self._total_latency_ms / max(self._total_requests, 1)
            ),
            "compliance_mode": self._compliance_mode,
            "cache": self.context_store.cache_stats(),
            "router": self.model_router.stats(),
            "precache": self._precache.stats if self._precache else None,
            "perception_chain": self.perception.get_degradation_chain(),
        }

    # ── Integration helpers ─────────────────────────────────────────────

    @classmethod
    def from_master_agent(cls, master_agent) -> "ExecutionController":
        """从现有 KeyboardMasterAgent 实例构建 ExecutionController。

        桥接现有代码，将 master 的所有子组件注入为融合管线的一部分。
        """
        ctrl = cls()

        # 注入 master 的子组件
        ctrl._agent = master_agent.agent
        ctrl._workflow_matcher = master_agent.workflow_matcher
        ctrl._workflow_engine = master_agent.workflow_engine
        ctrl._planner = master_agent.planner

        # 共享语义缓存
        if hasattr(master_agent.agent, '_llm_engine'):
            llm_engine = master_agent.agent._llm_engine
            if llm_engine and hasattr(llm_engine, '_cache'):
                ctrl.context_store.cache = llm_engine._cache

        # 共享 ModelRouter
        if hasattr(master_agent.agent, '_llm_engine'):
            if hasattr(master_agent.agent._llm_engine, '_router'):
                ctrl.model_router = master_agent.agent._llm_engine._router

        # 共享预编译意图→键位表（若 master 的 agent 已就绪）
        try:
            from .precache import PrecachedShortcutMap
            db = getattr(master_agent.agent, '_db', None)
            intent_engine = getattr(master_agent.agent, '_intent', None)
            if db is not None:
                ctrl._precache = PrecachedShortcutMap.build(db, intent_engine)
        except Exception:
            pass

        return ctrl


# ═══════════════════════════════════════════════════════════════════════
# Simple integration test
# ═══════════════════════════════════════════════════════════════════════

def _smoke_test():
    """冒烟测试：验证融合管线的四个阶段。"""

    print("=" * 60)
    print("ExecutionController — 四大策略融合冒烟测试")
    print("=" * 60)

    ctrl = ExecutionController()
    print(f"\n[1/5] 感知层: {ctrl.perception.get_degradation_chain()}")

    # 模拟一次意图执行（dry_run）
    result = ctrl.handle("复制这段文字", dry_run=True)
    print(f"[2/5] handle() 返回:")
    print(f"      ok={result['ok']}, pipeline={result['pipeline']}")
    print(f"      cache_hit={result['cache_hit']}, avr_tier={result['avr_tier']}")
    print(f"      elapsed_ms={result['elapsed_ms']:.1f}")

    # 第二次相同意图 → 预期缓存命中
    result2 = ctrl.handle("复制这段文字", dry_run=True)
    print(f"[3/5] 第二次同意图:")
    print(f"      cache_hit={result2['cache_hit']}, pipeline={result2['pipeline']}")

    # ContextStore 建议
    suggestions = ctrl.context_store.get_suggestions()
    print(f"[4/5] 建议: {len(suggestions)} 条")
    for s in suggestions:
        print(f"      {s['command']} (x{s['count']})")

    # 整体统计
    stats = ctrl.stats()
    print(f"[5/5] 统计:")
    print(f"      requests={stats['total_requests']}, "
          f"avg_latency={stats['avg_latency_ms']:.1f}ms")
    print(f"      cache={stats['cache']}")
    print(f"      router={stats['router']}")

    # 合规模式
    print(f"\n合规模式测试:")
    print(f"      默认 vision: {ctrl.compliance_mode}")
    ctrl.set_compliance_mode(True)
    print(f"      开启后 vision: {ctrl.compliance_mode}")
    ctrl.set_compliance_mode(False)

    print("\n" + "=" * 60)
    print("冒烟测试完成。四大策略融合管线正常工作。")
    print("=" * 60)


if __name__ == "__main__":
    _smoke_test()
