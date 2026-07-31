"""三档执行引擎 —— 键盘 / API / 视觉。

2026 版 Agent 键盘动作规范定义了 Agent 可用的三种执行引擎：

  ┌─────────────┬──────────────┬───────────┬─────────────────┐
  │ 引擎        │ 速度         │ Token 数  │ 可靠性          │
  ├─────────────┼──────────────┼───────────┼─────────────────┤
  │ keyboard    │ < 100ms      │ 0         │ 中等            │
  │ api         │ < 300ms      │ 0         │ 高（程序化）    │
  │ vision      │ 1-3s         │ ~1000     │ 最高            │
  └─────────────┴──────────────┴───────────┴─────────────────┘

本模块决定某个命令应使用哪一档，并格式化响应，
让 Agent 知道下一步该做什么。

决策树（Agent 调用 /v1/plan 或 /v1/execute 时）：
  1. 该命令是否有 `api_equivalent`？  -> tier=api
  2. 是否有 `gui_fallback`？         -> tier=vision（仅此）
  3. 否则即为键盘快捷键              -> tier=keyboard

当 nl2shortcut 的键盘档失败（selfcheck=False）时，Agent 的可选方案：
  - 重试                      （若 retryable=True 且为瞬时错误）
  - 升级到 api 档             （若存在 api_equivalent）
  - 升级到 vision             （最后手段，1-3s，1000 token）
"""
from typing import Optional, Dict, Any, List, Tuple

# 引擎常量 —— 同时作为响应中的 `tier` 字段导出
TIER_KEYBOARD = "keyboard"
TIER_API      = "api"
TIER_VISION   = "vision"

# 何时建议回退到视觉档
VISION_TRIGGERS = {
    # 错误码 -> 是否建议视觉回退？
    "no_match":                    False,  # re-plan first
    "no_platform_key":             True,   # nl2shortcut can't help on this platform
    "key_combination_no_response": True,   # injection didn't take effect
    "inject_failed":               True,
    "app_not_detected":            True,   # nl2shortcut doesn't know where it is
    "low_confidence":              False,  # re-recognize first
    "intent_ambiguous":            False,  # re-recognize first
    "timeout":                     True,
    "exec_failed":                 True,
}

# 何时建议升级到 api 档（若存在 api_equivalent）
API_TRIGGERS = {
    "key_combination_no_response": True,   # shortcut broken, try API
    "inject_failed":               True,
    "no_platform_key":             True,
}


def recommend_tier_for_command(command_meta: Optional[Dict[str, Any]]) -> str:
    """根据快捷键的 agent_metadata，返回最适合的引擎档位。

    优先级：api > keyboard > vision
    （api 在可用时优先，因为它最可靠）
    """
    if not command_meta:
        return TIER_KEYBOARD
    if command_meta.get("api_equivalent"):
        return TIER_API
    if command_meta.get("gui_fallback"):
        return TIER_VISION
    return TIER_KEYBOARD


def recommend_fallback(
    *,
    failed_error_code: str,
    command_meta: Optional[Dict[str, Any]],
    fallback_policy: str = "gui_retry",
    compliance_mode: bool = False,
) -> Dict[str, Any]:
    """当某个步骤失败时，建议 Agent 下一步该怎么做。

    Args:
        compliance_mode: 合规模式。False 时 vision 降级返回 abort。
    """
    api_eq = (command_meta or {}).get("api_equivalent")
    gui_fb = (command_meta or {}).get("gui_fallback")

    # 中止策略：直接中止
    if fallback_policy == "abort":
        return {"tier": TIER_KEYBOARD, "action": "abort", "target": None,
                "reason": "fallback_policy=abort"}

    # 首选：若可用且触发条件匹配，则升级到 api 档
    if api_eq and API_TRIGGERS.get(failed_error_code, False):
        return {"tier": TIER_API, "action": "escalate_api", "target": api_eq,
                "reason": f"error '{failed_error_code}' has api_equivalent; try API tier"}

    # 其次：若触发条件匹配，则升级到视觉档（合规闸门检查）
    if VISION_TRIGGERS.get(failed_error_code, False):
        if not compliance_mode:
            return {"tier": TIER_KEYBOARD, "action": "abort",
                    "target": None,
                    "reason": f"error '{failed_error_code}' would escalate to vision, "
                              f"but compliance_mode=False blocks vision escalation"}
        return {"tier": TIER_VISION, "action": "escalate_vision",
                "target": gui_fb, "reason": f"error '{failed_error_code}' requires visual fallback"}

    # 默认：重试（瞬时错误）
    if failed_error_code in ("key_combination_no_response", "inject_failed",
                             "app_not_detected", "timeout"):
        return {"tier": TIER_KEYBOARD, "action": "retry", "target": None,
                "reason": "transient error; retry the same keyboard action"}

    # 最后手段：合规模式下才允许 vision 降级
    if not compliance_mode:
        return {"tier": TIER_KEYBOARD, "action": "abort",
                "target": None,
                "reason": "no tier available; compliance_mode blocks vision escalation"}
    return {"tier": TIER_VISION, "action": "escalate_vision",
            "target": gui_fb, "reason": "no other tier available"}


def tier_summary() -> Dict[str, Any]:
    """三档引擎的静态描述，由 /v1/health.tiers 返回。"""
    return {
        "tiers": [
            {
                "name":    TIER_KEYBOARD,
                "speed":   "< 100ms",
                "tokens":  0,
                "reliability": "medium",
                "description": "Keyboard / mouse / shortcut injection. Primary tier. "
                               "Resolves intent -> shortcut -> injects keystrokes. "
                               "Self-checks after injection; reports failure to Agent.",
            },
            {
                "name":    TIER_API,
                "speed":   "< 300ms",
                "tokens":  0,
                "reliability": "high",
                "description": "Programmatic API call. Used when an app exposes a "
                               "VS Code command, REST endpoint, or OS API that does "
                               "the same thing as a shortcut. More reliable than "
                               "keyboard, but only for commands with `api_equivalent`.",
            },
            {
                "name":    TIER_VISION,
                "speed":   "1-3s",
                "tokens":  "~1000",
                "reliability": "highest",
                "description": "GUI vision (CogAgent / OmniParser / Claude Computer Use). "
                               "Last-resort fallback. Slower, costs tokens, but can recover "
                               "from any UI state. Used when keyboard + api both fail. "
                               "Gated by compliance_mode flag (disabled by default).",
                "compliance_gated": True,
            },
        ],
        "decision_rule": "keyboard first, then api, then vision",
    }
