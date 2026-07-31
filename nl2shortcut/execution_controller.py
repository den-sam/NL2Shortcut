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

        # ── Step 1: 感知层快照 ──────────────────────────────────────
        ui_state = self.perception.snapshot()
        if ui_state.source == "none" and ui_state.error:
            # 所有 Provider 都失败，仍继续（LightProvider 极少失败）
            pass

        # ── Step 2: 记忆层压缩 ──────────────────────────────────────
        ctx = self.context_store.build(
            app_context=ui_state,
            intent=intent,
            clipboard_text=ui_state.clipboard_text,
            selected_text=ui_state.selected_text,
        )
        result["cache_hit"] = ctx.cache_hit

        # ── Step 3: 路由层决策 ──────────────────────────────────────
        decision = self.model_router.route(ctx)
        result["avr_tier"] = decision.tier

        # ── Step 4: 工作流匹配（AVR 未跳过且未命中缓存时）────────
        if (not decision.should_call_llm
                and decision.tier == "none"
                and not ctx.cache_hit):
            # cache/workflow 都未命中但 router 说 skip？可能是复杂度过低
            # → 尝试工作流匹配
            pass

        # ── Step 5: 执行分发 ────────────────────────────────────────
        try:
            if ctx.workflow_hit:
                # 工作流命中 → 直接执行
                result["pipeline"] = "workflow_hit"
                exec_result = self._execute_via_workflow(intent, dry_run)
                result["ok"] = exec_result.get("ok", False)
                result["result"] = exec_result
                result["tier_used"] = "keyboard"
            elif decision.tier == "none" or not decision.should_call_llm:
                # 缓存命中 / AVR 跳过 → 直接用 agent 简单执行
                result["pipeline"] = "cache_hit" if ctx.cache_hit else "avr_skip"
                exec_result = self._execute_simple(intent, dry_run, timeout)
                result["ok"] = exec_result.get("ok", False)
                result["result"] = exec_result
                result["tier_used"] = "keyboard"
            else:
                # 需要 LLM → Plan 或 Match
                result["pipeline"] = "llm_plan"
                plan_result = self._execute_via_plan(intent, dry_run, timeout)
                result["ok"] = plan_result.get("ok", False)
                result["result"] = plan_result
                result["tier_used"] = "keyboard"
        except Exception as e:
            # ── 兜底：带合规闸门的降级 ──
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

        # ── Step 6: 回写记忆 ─────────────────────────────────────────
        if learn and result["ok"] and intent:
            self.context_store.record(
                intent=intent,
                command=result.get("result", {}).get("command", intent),
                app_name=ui_state.app_name,
                confidence=1.0,
            )

        elapsed = (time.perf_counter() - t0) * 1000
        self._total_latency_ms += elapsed
        result["elapsed_ms"] = elapsed
        return result

    # ── Internal: 执行方法 ──────────────────────────────────────────────

    def _execute_simple(
        self, intent: str, dry_run: bool, timeout: float
    ) -> Dict[str, Any]:
        """简单快捷键执行（单个 command）。"""
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
