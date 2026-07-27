"""OpenClaw 工作区 → NL2Shortcut 插件演示。

这里展示的是在加载了 NL2Shortcut 作为动作插件后，一个真实的 OpenClaw
工作区的样子。OpenClaw 的运行时会在插件模块中发现 ``PLUGIN_MANIFEST``
并在其动作注册表中注册这 11 个动作；随后 LLM 规划器在拆解用户目标时
便可以选择 ``execute_keyboard``、``execute_api``、``execute_vision``
或 ``execute_tiered``。

运行本演示以端到端地查看完整的集成效果：

.. code-block:: bash

    # 在一个终端中：启动 NL2Shortcut
    nl2shortcut agent-api

    # 在另一个终端中：
    python examples/openclaw_demo.py
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict

try:
    from nl2shortcut.openclaw_plugin import ScutClient, PLUGIN_MANIFEST, handle_action
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nl2shortcut.openclaw_plugin import ScutClient, PLUGIN_MANIFEST, handle_action


def _resolve_api_key() -> str:
    """从环境变量或开发密钥中选取第一个可用的 NL2Shortcut API key。"""
    keys = os.environ.get("NL2SHORTCUT_API_KEYS", "")
    if keys:
        return keys.split(",")[0].strip()
    return "nl2shortcut_dev_local"  # dev fallback


def simulate_openclaw_planner() -> None:
    """模拟 OpenClaw 规划器在加载 NL2Shortcut 后的行为。

    规划器流程：
      1. 加载插件清单（导入时已完成）。
      2. 针对每个用户目标，拆解为插件动作。
      3. 对每个动作调用 ``handle_action(action, params)``。
      4. 将失败路由到下一层级（依据动作的 routing_hints）。
    """
    print("=" * 70)
    print(f"OpenClaw demo — plugin: {PLUGIN_MANIFEST['name']} v{PLUGIN_MANIFEST['version']}")
    print(f"Tiers offered: {PLUGIN_MANIFEST['tiers_offered']}")
    print(f"Compatible with: {', '.join(PLUGIN_MANIFEST['compatibility'].keys())}")
    print("=" * 70)

    # 目标 1：复制一段文本
    print("\n[Goal 1] User: 'Copy the selected text'")
    print("  Planner decides: try api tier first (clipboard is free + reliable)")
    cli = ScutClient(api_key=_resolve_api_key())
    # 将注入了 api_key 的客户端传给 handle_action
    r = handle_action("execute_api", {"intent": "copy", "context": {"text": "openclaw demo copy"}}, client=cli)
    print(f"  -> {r.get('tier_used')} ok={r.get('ok')} action={r.get('action')} message={r.get('message', '')[:50]}")

    # 目标 2：用户说 'save the file' —— agent 直接调用 execute_keyboard
    print("\n[Goal 2] User: 'Save the file'")
    print("  Planner decides: execute_keyboard intent='save' (no api equivalent on most apps)")
    r = handle_action("execute_keyboard", {"intent": "save", "dry_run": True}, client=cli)
    print(f"  -> tier_used={r.get('tier_used')} key_combo={r.get('key_combination')} dry_run={r.get('executed') is False}")

    # 目标 3：多步操作 —— 先复制再粘贴
    print("\n[Goal 3] User: 'Copy this line and paste it below' (multi-step sequence)")
    print("  Planner: open session, then run sequence")
    sess = handle_action("session_start", {"app": "vscode"}, client=cli)
    sid = sess.get("session_id", "?")
    print(f"  -> session_id={sid}")
    seq = handle_action("execute_sequence", {
        "steps": [
            {"intent": "select_line", "context": {"line": 1}},
            {"intent": "copy"},
            {"intent": "move_line_down"},
            {"intent": "paste"},
        ],
        "stop_on_error": True,
    }, client=cli)
    print(f"  -> sequence ok={seq.get('ok')}")

    # 目标 4：带层级感知的自动回退
    print("\n[Goal 4] User: 'Find the Copy button' (vision-required — no shortcut)")
    print("  Planner decides: execute_tiered (tries api, then keyboard, then vision)")
    r = handle_action("execute_tiered", {
        "intent": "find Copy button",
        "tier_preference": ["api", "keyboard", "vision"],
        "max_escalations": 2,
    }, client=cli)
    print(f"  -> tier_used={r.get('tier_used')} escalation_path={r.get('escalation_path')} ok={r.get('ok')}")

    # 目标 5：能力发现
    print("\n[Goal 5] LLM context stuffing (1.5KB keys index)")
    r = handle_action("keys_index", {}, client=cli)
    print(f"  -> {r.get('count')} commands, fits in {len(json.dumps(r.get('index', [])))} bytes")
    # 打印 3 个示例命令
    for cmd in r.get("index", [])[:3]:
        print(f"     {cmd.get('command'):20s}  keys={cmd.get('keys', '?')}")

    # 目标 6：统计 / 可观测性
    print("\n[Goal 6] Operator dashboard (stats endpoint)")
    r = handle_action("get_stats", {}, client=cli)
    print(f"  -> {r.get('total_requests', 0)} total requests, "
          f"success_rate={r.get('overall_success_rate', 0)*100:.0f}%")
    for tier, data in (r.get("tiers") or {}).items():
        if data.get("requests", 0) > 0:
            print(f"     {tier:9s}: {data.get('requests')} req, "
                  f"p50={data.get('p50_latency_ms'):.1f}ms "
                  f"p95={data.get('p95_latency_ms'):.1f}ms")

    # 目标 7：兼容性声明
    print("\n[Compatibility matrix]")
    for system, info in PLUGIN_MANIFEST["compatibility"].items():
        status = info.get("status", "?")
        version = info.get("version", "")
        print(f"  {system:25s} {status:14s} {version}")

    print("\n[Performance envelope (measured)]")
    perf = PLUGIN_MANIFEST["performance"]
    for tier, p in perf.items():
        print(f"  {tier:14s} p50={p['p50_ms']:>4.1f}ms  p95={p['p95_ms']:>5.1f}ms  "
              f"tokens={p['tokens']:>4}  reliability={p['reliability']}")


if __name__ == "__main__":
    try:
        simulate_openclaw_planner()
    except Exception as e:
        print(f"\n[ERR] {e}")
        print("Make sure NL2Shortcut agent-api is running: nl2shortcut agent-api")
