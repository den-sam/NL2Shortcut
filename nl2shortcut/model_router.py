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

# ── API 配置常量 ──
_DEEPSEEK_BASE = "https://api.deepseek.com/v1"
_REQUEST_TIMEOUT = 30  # seconds

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

    def call_llm(
        self,
        decision: RoutingDecision,
        messages: List[Dict[str, str]],
        api_key: Optional[str] = None,
        temperature: float = 0.1,
    ) -> LLMCallResult:
        """根据路由决策执行实际的 LLM API 调用，包含自动降级重试。

        调用流程:
          decision.tier="none"  → 直接返回空（缓存/工作流命中）
          decision.tier="cheap" → 调用 deepseek-chat，失败降级到缓存/keyword
          decision.tier="standard" → 调用 deepseek-v4-pro，失败降级到 cheap

        Args:
            decision: model_router.route() 的产出
            messages: [{"role":"system","content":...}, {"role":"user","content":...}]
            api_key: API Key（缺省从环境变量/配置文件加载）
            temperature: 温度参数（默认 0.1）

        Returns:
            LLMCallResult 包含 success / content / tokens / cost 等
        """
        t0 = time.time()

        if not decision.should_call_llm or decision.tier == "none":
            return LLMCallResult(
                success=False,
                tier="none",
                model="",
                call_type="skip",
                elapsed_ms=(time.time() - t0) * 1000,
                error="LLM 调用被 AVR 跳过（缓存/工作流命中）",
            )

        # 确保 API Key 可用
        if not api_key:
            try:
                from .llm import _load_api_key
                api_key = _load_api_key()
            except Exception:
                pass
        if not api_key:
            return LLMCallResult(
                success=False,
                tier=decision.tier,
                model=decision.model,
                call_type="error",
                elapsed_ms=(time.time() - t0) * 1000,
                error="API Key 未配置（设置 DEEPSEEK_API_KEY）",
            )

        # 首选 tier 调用
        primary = self._do_api_call(
            decision.model, messages, api_key,
            decision.max_tokens, temperature,
        )

        if primary.success:
            primary.elapsed_ms = (time.time() - t0) * 1000
            primary.tier = decision.tier
            primary.model = decision.model
            return primary

        # ── 降级重试 ──
        failover_tier = decision.failover_tier
        if failover_tier and failover_tier != decision.tier and failover_tier != "none":
            fmodel, fmax = _TIER_CONFIG.get(failover_tier, ("", 0))
            if fmodel:
                failover = self._do_api_call(
                    fmodel, messages, api_key,
                    fmax, temperature,
                )
                if failover.success:
                    failover.elapsed_ms = (time.time() - t0) * 1000
                    failover.tier = failover_tier
                    failover.model = fmodel
                    failover.failover_used = True
                    return failover

        # 完全失败
        primary.elapsed_ms = (time.time() - t0) * 1000
        primary.tier = decision.tier
        primary.model = decision.model
        return primary

    def _do_api_call(
        self,
        model: str,
        messages: List[Dict[str, str]],
        api_key: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMCallResult:
        """执行单次 HTTP API 调用。"""
        import json as _json
        import urllib.request
        import urllib.error

        payload = _json.dumps({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{_DEEPSEEK_BASE}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
                body = _json.loads(resp.read().decode("utf-8"))
            choice = body.get("choices", [{}])[0]
            content = choice.get("message", {}).get("content", "") or ""
            usage = body.get("usage", {})

            tokens_used = usage.get("total_tokens", 0)
            input_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)

            cost = self._estimate_cost(input_tokens, output_tokens, model) if input_tokens else 0.0

            return LLMCallResult(
                success=True,
                call_type="chat",
                result=content,
                tokens_used=tokens_used,
                cost_usd=cost,
            )
        except urllib.error.HTTPError as e:
            body_str = ""
            try:
                body_str = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            return LLMCallResult(
                success=False,
                error=f"HTTP {e.code}: {e.reason} — {body_str}",
            )
        except Exception as e:
            return LLMCallResult(
                success=False,
                error=str(e),
            )

    # ── Prompt 构建器 ──────────────────────────────────────────────────

    @staticmethod
    def build_prompt(
        call_type: str,
        intent: str,
        shortcuts: Optional[List[Dict[str, str]]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, str]]:
        """为不同 LLM 引擎类型构建 system + user 提示词。

        Args:
            call_type: "recognize" | "match" | "plan"
            intent: 用户意图文本
            shortcuts: 快捷键列表（"recognize"/"match" 时需要）
            context: 上下文信息（app_name, window_title 等）

        Returns:
            [{"role":"system","content":...}, {"role":"user","content":...}]
        """
        ctx = context or {}
        app = ctx.get("app_name", "")
        window = ctx.get("window_title", "")

        if call_type == "recognize":
            system = _build_recognize_system(shortcuts or [])
            user = _build_recognize_user(intent, app, window)
        elif call_type == "match":
            system = _build_match_system(shortcuts or [])
            user = _build_match_user(intent, app, window, ctx)
        elif call_type == "plan":
            system = _build_plan_system(ctx)
            user = _build_plan_user(intent, app, window, ctx)
        else:
            system = "You are a helpful assistant."
            user = intent

        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

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
# Prompt 构建器辅助函数
# ═══════════════════════════════════════════════════════════════════════

def _build_recognize_system(shortcuts: List[Dict[str, str]]) -> str:
    """构建意图识别的 system prompt。"""
    lines = [
        "你是一个快捷键意图识别助手。用户用自然语言描述想做的事，你匹配最合适的命令。",
        "",
        "可用命令列表：",
    ]
    for s in shortcuts:
        cmd = s.get("command", "")
        keys = s.get("key_combination", "")
        desc = s.get("description", "")
        lines.append(f"  - {cmd}: {keys}" + (f" ({desc})" if desc else ""))
    lines += [
        "",
        "输出格式（仅返回 JSON，不要其他文字）：",
        '{ "command": "命令名", "key_combination": "Ctrl+C", "confidence": 0.95 }',
        "若无法匹配，command 设为 \"unknown\"，confidence 设为 0.0。",
    ]
    return "\n".join(lines)


def _build_recognize_user(intent: str, app: str, window: str) -> str:
    """构建意图识别的 user prompt。"""
    parts = [f"用户意图：「{intent}」"]
    if app:
        parts.append(f"当前应用：{app}")
    if window:
        parts.append(f"窗口标题：{window}")
    parts.append("请匹配最合适的命令。")
    return "\n".join(parts)


def _build_match_system(shortcuts: List[Dict[str, str]]) -> str:
    """构建工作流匹配的 system prompt。"""
    lines = [
        "你是一个工作流语义匹配助手。根据用户意图从候选工作流中选出最匹配的一个。",
        "",
        "候选工作流列表：",
    ]
    for s in shortcuts:
        lines.append(f"  - {s.get('command', '')}: {s.get('description', '')}")
    lines += [
        "",
        "输出格式（仅返回 JSON，不要其他文字）：",
        '{ "matched": true, "workflow": "工作流名", "confidence": 0.85, "reason": "匹配理由" }',
        "若不匹配，matched 设为 false。",
    ]
    return "\n".join(lines)


def _build_match_user(
    intent: str, app: str, window: str, ctx: Dict[str, Any],
) -> str:
    """构建工作流匹配的 user prompt。"""
    recent = ctx.get("recent_actions", [])
    parts = [f"用户意图：「{intent}」"]
    if app:
        parts.append(f"当前应用：{app}")
    if window:
        parts.append(f"窗口标题：{window}")
    if recent:
        parts.append("最近操作：" + ", ".join(
            a.get("intent", "") for a in recent[:5]
        ))
    parts.append("请匹配最合适的工作流。")
    return "\n".join(parts)


def _build_plan_system(ctx: Dict[str, Any]) -> str:
    """构建目标规划的 system prompt。"""
    shortcuts_info = ""
    if ctx.get("shortcuts"):
        shortcuts_info = "\n".join(
            f"  - {s.get('command', '?')}: {s.get('key_combination', '?')}"
            for s in ctx["shortcuts"][:30]
        )
    return (
        "你是一个目标分解助手。将用户的复杂目标拆解为逐步的快捷键操作序列。\n"
        "\n"
        "规则：\n"
        "1. 每一步必须对应一个已知快捷键，不可凭空生成\n"
        "2. 多步之间用「→」连接\n"
        "3. 若目标无法用快捷键完成，设为 can_execute=false\n"
        "\n"
        f"可用快捷键：\n{shortcuts_info or '(使用内置快捷键库)'}\n"
        "\n"
        "输出格式（仅返回 JSON，不要其他文字）：\n"
        '{\n'
        '  "goal": "用户原始目标",\n'
        '  "can_execute": true,\n'
        '  "steps": [\n'
        '    {"step": 1, "intent": "全选", "command": "select_all", '
        '"key_combination": "Ctrl+A"},\n'
        '    {"step": 2, "intent": "复制", "command": "copy", '
        '"key_combination": "Ctrl+C"}\n'
        '  ],\n'
        '  "reasoning": "规划理由"\n'
        '}'
    )


def _build_plan_user(
    intent: str, app: str, window: str, ctx: Dict[str, Any],
) -> str:
    """构建目标规划的 user prompt。"""
    recent = ctx.get("recent_actions", [])
    parts = [f"目标：「{intent}」"]
    if app:
        parts.append(f"当前应用：{app}")
    if window:
        parts.append(f"窗口标题：{window}")
    if recent:
        parts.append("历史操作：" + " → ".join(
            a.get("intent", "") for a in recent[:5]
        ))
    parts.append("请将此目标拆解为逐步快捷键序列。")
    return "\n".join(parts)


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
