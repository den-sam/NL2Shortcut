"""ModelRouter — AVR 统一模型路由层（融合策略②的核心模块）。

AVR = Adaptive Value Router，自适应价值路由。

将 llm.py / workflow_matcher.py / planner.py 三处各自独立的 LLM 调用
统一为单一入口，依据 MinimalContext 的复杂度/风险/预算自动选择模型档位：

  ┌────────────┬───────────────┬─────────────┬──────────────────────────┐
  │ Tier       │ 模型          │ 成本/1K tok │ 触发条件                 │
  ├────────────┼───────────────┼─────────────┼──────────────────────────┤
  │ none       │ (不调 LLM)    │ $0          │ 缓存命中 / 工作流命中    │
  │ cheap      │ deepseek-chat │ $0.00014    │ low complexity, low risk │
  │ standard   │ deepseek-v4-pr│ $0.00055    │ medium/high complexity   │
  └────────────┴───────────────┴─────────────┴──────────────────────────┘

核心优化逻辑（省 52-78% 成本的来源）：
  - 缓存命中 → 100% 成本节省（tier=none）
  - 工作流命中 → 100% 成本节省（tier=none）
  - 简单意图用 cheap 模型 → ~60% 成本节省 vs standard
  - 总节省 = cache_hit_rate * 100% + (1 - cache_hit_rate) * cheap_ratio * 60%

设计约定
────────
- route() 只决定"用不用 LLM + 用哪个模型"，不执行调用。
  实际调用仍由各引擎执行（保持向后兼容）。
- 所有 cost 估算基于公开 DeepSeek API 定价，仅作参考。
- failover: standard 降级为 cheap，cheap 降级为 none（回退到本地）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Literal

from .context_store import MinimalContext

# ───────────────────────────────────────────────────────────────────────
# DeepSeek 模型常量（公开定价，仅作成本估算参考）
# ───────────────────────────────────────────────────────────────────────

# 模型名 → (每 1K input token 美元单价, 每 1K output token 美元单价)
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "deepseek-chat":      (0.00014, 0.00028),   # cheap tier
    "deepseek-v4-pro":    (0.00055, 0.00110),   # standard tier (current default)
}

# AVR 档位 → 模型名 + 最大 tokens
_TIER_CONFIG: dict[str, tuple[str, int]] = {
    "cheap":    ("deepseek-chat",   300),
    "standard": ("deepseek-v4-pro", 1200),
}

# ───────────────────────────────────────────────────────────────────────
# RoutingDecision
# ───────────────────────────────────────────────────────────────────────

@dataclass
class RoutingDecision:
    """一次路由决策的结果，供各引擎查询用哪个模型。"""

    tier: str = "standard"          # "none" | "cheap" | "standard"
    model: str = ""                 # 实际模型名（tier="none"时为空）
    max_tokens: int = 300           # 最大输出 token 数
    reason: str = ""                # 决策理由
    estimated_cost_usd: float = 0.0 # 预估成本（USD）
    should_call_llm: bool = True   # 是否需要调 LLM？
    failover_tier: str = ""         # 若失败，回退到的 tier

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "reason": self.reason,
            "estimated_cost_usd": f"${self.estimated_cost_usd:.6f}",
            "should_call_llm": self.should_call_llm,
            "failover_tier": self.failover_tier,
        }


# ───────────────────────────────────────────────────────────────────────
# ModelRouter
# ───────────────────────────────────────────────────────────────────────

class ModelRouter:
    """统一模型路由 —— AVR 调度层核心。

    用法
    ----
        router = ModelRouter()
        ctx = context_store.build(app_ctx, intent="把文件移到桌面")
        decision = router.route(ctx)

        if not decision.should_call_llm:
            # 缓存/工作流命中，直接执行
            ...
        else:
            # 用 decision.model 调 LLM
            ...
    """

    def __init__(
        self,
        cheap_model: str = "deepseek-chat",
        standard_model: str = "deepseek-v4-pro",
        # 复杂度阈值
        low_complexity_threshold: float = 0.3,
        high_complexity_threshold: float = 0.7,
        # 风险阈值（高风险强制 standard 或直接告警）
        high_risk_threshold: float = 0.7,
        # cheap 模型仅用于单步意图（token_budget < 此值）
        cheap_token_limit: int = 600,
        # 成本估算开关
        track_costs: bool = True,
    ):
        self._cheap_model = cheap_model
        self._standard_model = standard_model
        self._low_complexity_threshold = low_complexity_threshold
        self._high_complexity_threshold = high_complexity_threshold
        self._high_risk_threshold = high_risk_threshold
        self._cheap_token_limit = cheap_token_limit
        self._track_costs = track_costs

        # 统计
        self._total_routes: int = 0
        self._tier_counts: dict[str, int] = {"none": 0, "cheap": 0, "standard": 0}
        self._total_cost_usd: float = 0.0
        self._total_latency_ms: float = 0.0
        self._route_history: list[dict] = []  # 最近 100 条路由决策

    # ── Public API ────────────────────────────────────────────────────

    def route(self, ctx: MinimalContext) -> RoutingDecision:
        """依据 MinimalContext 做出路由决策。

        决策树
        ──────
        1. cache_hit or workflow_hit → tier=none（不调 LLM）
        2. 高风险 + 高复杂度 → tier=standard（最强模型，保证质量）
        3. 低复杂度 + 低 token 预算 → tier=cheap（廉价模型，省成本）
        4. 中复杂度 → tier=standard（当前默认）
        5. 所有其他情况 → tier=standard

        Args:
            ctx: ContextStore.build_context() 产出的 MinimalContext。

        Returns:
            RoutingDecision，包含 tier / model / reason / estimated_cost。
        """
        t0 = time.perf_counter()
        self._total_routes += 1

        # ── 规则 1：缓存或工作流命中 → 完全跳过 LLM ──
        if ctx.cache_hit:
            decision = RoutingDecision(
                tier="none",
                model="",
                max_tokens=0,
                reason=f"缓存命中（intent='{ctx.raw_intent[:30]}'），跳过 LLM",
                estimated_cost_usd=0.0,
                should_call_llm=False,
                failover_tier="cheap",
            )
            self._record(decision, t0)
            return decision

        if ctx.workflow_hit:
            decision = RoutingDecision(
                tier="none",
                model="",
                max_tokens=0,
                reason=f"工作流命中，跳过 LLM",
                estimated_cost_usd=0.0,
                should_call_llm=False,
                failover_tier="cheap",
            )
            self._record(decision, t0)
            return decision

        # ── 规则 2：高风险 + 高复杂度 → standard（保证质量）──
        if ctx.risk >= self._high_risk_threshold and ctx.complexity >= self._high_complexity_threshold:
            model, max_tok = _TIER_CONFIG["standard"]
            cost = self._estimate_cost(ctx.token_budget, max_tok, model)
            decision = RoutingDecision(
                tier="standard",
                model=model,
                max_tokens=max_tok,
                reason=(
                    f"高风险({ctx.risk:.0%})+高复杂度({ctx.complexity:.0%})，"
                    f"使用最强模型确保质量"
                ),
                estimated_cost_usd=cost,
                should_call_llm=True,
                failover_tier="cheap",
            )
            self._record(decision, t0)
            return decision

        # ── 规则 3：高风险（但复杂度不高）→ standard ──
        if ctx.risk >= self._high_risk_threshold:
            model, max_tok = _TIER_CONFIG["standard"]
            cost = self._estimate_cost(ctx.token_budget, max_tok, model)
            decision = RoutingDecision(
                tier="standard",
                model=model,
                max_tokens=max_tok,
                reason=f"高风险操作({ctx.risk:.0%})，使用 standard 模型确保准确",
                estimated_cost_usd=cost,
                should_call_llm=True,
                failover_tier="cheap",
            )
            self._record(decision, t0)
            return decision

        # ── 规则 4：低复杂度 + 小 token 预算 → cheap ──
        if (ctx.complexity <= self._low_complexity_threshold
                and ctx.token_budget <= self._cheap_token_limit):
            model, max_tok = _TIER_CONFIG["cheap"]
            cost = self._estimate_cost(ctx.token_budget, max_tok, model)
            decision = RoutingDecision(
                tier="cheap",
                model=model,
                max_tokens=max_tok,
                reason=(
                    f"低复杂度({ctx.complexity:.0%})+小预算({ctx.token_budget}tok)，"
                    f"使用 cheap 模型省成本"
                ),
                estimated_cost_usd=cost,
                should_call_llm=True,
                failover_tier="standard",
            )
            self._record(decision, t0)
            return decision

        # ── 规则 5：中低复杂度但标准预算 → cheap ──
        if ctx.complexity <= 0.5 and ctx.token_budget <= self._cheap_token_limit * 1.5:
            model, max_tok = _TIER_CONFIG["cheap"]
            cost = self._estimate_cost(ctx.token_budget, max_tok, model)
            decision = RoutingDecision(
                tier="cheap",
                model=model,
                max_tokens=max_tok,
                reason=f"中低复杂度({ctx.complexity:.0%})，优先尝试 cheap 模型",
                estimated_cost_usd=cost,
                should_call_llm=True,
                failover_tier="standard",
            )
            self._record(decision, t0)
            return decision

        # ── 规则 6：默认 → standard ──
        model, max_tok = _TIER_CONFIG["standard"]
        cost = self._estimate_cost(ctx.token_budget, max_tok, model)
        decision = RoutingDecision(
            tier="standard",
            model=model,
            max_tokens=max_tok,
            reason=f"中高复杂度({ctx.complexity:.0%})，使用 standard 模型",
            estimated_cost_usd=cost,
            should_call_llm=True,
            failover_tier="cheap",
        )
        self._record(decision, t0)
        return decision

    # ── 统计查询 ──────────────────────────────────────────────────────

    def stats(self) -> dict:
        """AVR 路由统计数据。"""
        total = max(self._total_routes, 1)
        return {
            "total_routes": self._total_routes,
            "tier_distribution": {
                tier: {
                    "count": cnt,
                    "ratio": f"{cnt / total:.1%}",
                }
                for tier, cnt in self._tier_counts.items()
            },
            "total_estimated_cost_usd": f"${self._total_cost_usd:.6f}",
            "avg_cost_per_route_usd": f"${self._total_cost_usd / total:.6f}",
            "cheap_savings_ratio": (
                f"{(self._tier_counts.get('none', 0) + self._tier_counts.get('cheap', 0)) / total:.1%}"
            ),
            "avg_latency_ms": f"{self._total_latency_ms / total:.1f}" if self._total_routes > 0 else "0",
        }

    def recent_routes(self, limit: int = 10) -> list[dict]:
        """最近 N 条路由决策记录。"""
        return list(self._route_history[-limit:])

    def reset_stats(self) -> None:
        """重置所有统计。"""
        self._total_routes = 0
        self._tier_counts = {"none": 0, "cheap": 0, "standard": 0}
        self._total_cost_usd = 0.0
        self._total_latency_ms = 0.0
        self._route_history.clear()

    # ── Internal ──────────────────────────────────────────────────────

    def _record(self, decision: RoutingDecision, start_time: float) -> None:
        """记录一次路由决策。"""
        elapsed = (time.perf_counter() - start_time) * 1000
        self._tier_counts[decision.tier] = self._tier_counts.get(decision.tier, 0) + 1
        self._total_cost_usd += decision.estimated_cost_usd
        self._total_latency_ms += elapsed

        if self._track_costs:
            self._route_history.append({
                "tier": decision.tier,
                "model": decision.model,
                "reason": decision.reason,
                "estimated_cost_usd": decision.estimated_cost_usd,
                "latency_ms": round(elapsed, 2),
            })
            # 只保留最近 100 条
            if len(self._route_history) > 100:
                self._route_history = self._route_history[-100:]

    def _estimate_cost(self, input_tokens: int, output_tokens: int,
                       model: str) -> float:
        """估算单次调用成本（USD）。"""
        if not self._track_costs or not model:
            return 0.0
        pricing = _MODEL_PRICING.get(model)
        if not pricing:
            return 0.0
        input_price, output_price = pricing
        # input_tokens 包含 system prompt + user prompt
        total = (input_tokens / 1000) * input_price + (output_tokens / 1000) * output_price
        return round(total, 8)


# ═══════════════════════════════════════════════════════════════════════
# AVR 成本节省分析（离线用）
# ═══════════════════════════════════════════════════════════════════════

def avr_savings_analysis(
    cache_hit_rate: float = 0.4,
    cheap_ratio: float = 0.3,
    routes_per_day: int = 1000,
) -> dict:
    """估算 AVR 路由带来的成本节省。

    Args:
        cache_hit_rate: 预计缓存命中率（0.0-1.0）
        cheap_ratio: 非缓存请求中使用 cheap 模型的比例（0.0-1.0）
        routes_per_day: 日均请求数

    Returns:
        dict: 包含 daily/monthly cost 对比
    """
    standard_cpk = 0.00055 + 0.00110  # input + output per 1K tokens
    cheap_cpk = 0.00014 + 0.00028

    # 假定平均每次调用消耗 800 input + 200 output tokens
    avg_tokens_10k = (800 + 200) / 1000

    # 无 AVR 时的成本（全部走 standard）
    cost_without_avr = routes_per_day * avg_tokens_10k * standard_cpk

    # 有 AVR 时的成本
    non_cache = routes_per_day * (1 - cache_hit_rate)
    cheap_calls = non_cache * cheap_ratio
    standard_calls = non_cache * (1 - cheap_ratio)

    cost_with_avr = (
        cheap_calls * avg_tokens_10k * cheap_cpk
        + standard_calls * avg_tokens_10k * standard_cpk
    )

    saving = cost_without_avr - cost_with_avr
    saving_pct = saving / cost_without_avr * 100 if cost_without_avr > 0 else 0

    return {
        "routes_per_day": routes_per_day,
        "cache_hit_rate": f"{cache_hit_rate:.0%}",
        "cheap_ratio": f"{cheap_ratio:.0%}",
        "without_avr_daily_usd": f"${cost_without_avr:.4f}",
        "without_avr_monthly_usd": f"${cost_without_avr * 30:.4f}",
        "with_avr_daily_usd": f"${cost_with_avr:.4f}",
        "with_avr_monthly_usd": f"${cost_with_avr * 30:.4f}",
        "savings_pct": f"{saving_pct:.1f}%",
        "savings_daily_usd": f"${saving:.4f}",
        "savings_monthly_usd": f"${saving * 30:.4f}",
    }


# ═══════════════════════════════════════════════════════════════════════
# LLM 调用结果类型（统一抽象）
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class LLMCallResult:
    """一次 AVR 路由的 LLM 调用结果，统一封装三种引擎的返回值。"""

    success: bool = False
    tier: str = "none"                  # 实际使用的 tier
    model: str = ""                     # 实际使用的模型
    call_type: str = ""                 # "recognize" | "match" | "plan"
    result: object = None              # IntentResult | MatchResult | Plan
    elapsed_ms: float = 0.0
    tokens_used: int = 0
    cost_usd: float = 0.0
    error: str = ""
    cache_hit: bool = False
    failover_used: bool = False

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "tier": self.tier,
            "model": self.model,
            "call_type": self.call_type,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "tokens_used": self.tokens_used,
            "cost_usd": f"${self.cost_usd:.6f}",
            "cache_hit": self.cache_hit,
            "failover_used": self.failover_used,
            "error": self.error,
        }
