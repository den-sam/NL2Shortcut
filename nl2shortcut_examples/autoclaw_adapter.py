"""
AutoClaw 适配器 —— 面向 AutoClaw Agent 框架的 NL2Shortcut 即插即用组件。

AutoClaw 是随 OpenClaw 一起提供的驱动型 Agent。它使用的是一套与
Claude Computer Use（其格式为 `computer: {action: "key", text: "ctrl+c"}`）
或 AutoGLM（其格式为 `{operation: "Shortcut", keys: [...]}`）
不同的动作词汇表。

OpenClaw 生态中 Agent 使用的 AutoClaw 动作面（action surface）示例::

    {
      "agent": "autoclaw",
      "actions": [
        {"verb": "press",   "args": {"combo": "ctrl+c"}},
        {"verb": "type",    "args": {"text": "hello"}},
        {"verb": "click",   "args": {"x": 100, "y": 200, "button": "left"}},
        {"verb": "screenshot", "args": {}},
        {"verb": "find",    "args": {"needle": "OK", "region": [0,0,800,600]}},
        {"verb": "wait",    "args": {"ms": 250}},
        {"verb": "scroll",  "args": {"amount": -3, "axis": "y"}},
        {"verb": "move",    "args": {"x": 400, "y": 400}},
      ]
    }

NL2Shortcut 会将每个 verb 映射到其三个层级（keyboard / api / vision）之一，
使用的是与其他适配器相同的 ScutClient，因此 AutoClaw Agent
无需改动任何代码即可接入 NL2Shortcut。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

try:
    from nl2shortcut.openclaw_plugin import ScutClient
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nl2shortcut.openclaw_plugin import ScutClient


def _resolve_api_key() -> str:
    keys = os.environ.get("NL2SHORTCUT_API_KEYS", "")
    if keys:
        return keys.split(",")[0].strip()
    return "nl2shortcut_dev_local"


def _combo_to_intent(combo: str) -> str:
    """将 AutoClaw 组合键字符串（'ctrl+c'、'cmd+v'、'ctrl+shift+t'）转换为 NL2Shortcut 意图（intent）。"""
    key = combo.lower().replace(" ", "").replace("+", "")
    table = {
        "ctrlc":  "copy",          "ctrlv":  "paste",
        "ctrlx":  "cut",           "ctrla":  "select_all",
        "ctrlz":  "undo",          "ctrly":  "redo",
        "ctrls":  "save",          "ctrlf":  "find",
        "ctrln":  "new",           "ctrlo":  "open",
        "ctrlw":  "close",         "ctrlp":  "print",
        "ctrlr":  "refresh",       "ctrlh":  "replace",
        "ctrlg":  "go_to_line",    "ctrlt":  "new_tab",
        "ctrlshiftt": "reopen_closed_tab",
        "ctrlshiftn": "new_window",
        "cmdc":   "copy",          "cmdv":   "paste",
        "cmdz":   "undo",          "cmds":   "save",
    }
    return table.get(key, key)


# AutoClaw verb → (scut_tier, intent, transform)
# 视觉（vision）层级仅支持 screenshot/find/click/ocr，因此 scroll/move
# 会被拆解为 find(needle) + click(在结果处) 或 moveTo(坐标)。
_VERB_MAP: Dict[str, Dict[str, Any]] = {
    "press":       {"tier": "keyboard", "transform": "combo_to_intent"},
    "type":        {"tier": "api",      "transform": "clipboard_paste"},
    "click":       {"tier": "vision",   "action":    "click"},
    "screenshot":  {"tier": "vision",   "action":    "screenshot"},
    "find":        {"tier": "vision",   "action":    "find"},
    "ocr":         {"tier": "vision",   "action":    "ocr"},
    "wait":        {"tier": "meta",     "transform": "sleep"},
    "scroll":      {"tier": "vision",   "action":    "find", "transform": "scroll_via_find"},
    "move":        {"tier": "vision",   "action":    "click", "transform": "move_via_click"},
}


class ScutAutoClawAdapter:
    """AutoClaw → NL2Shortcut 动作分发器。

    用法::

        from nl2shortcut.examples.autoclaw_adapter import ScutAutoClawAdapter
        a = ScutAutoClawAdapter()
        for action in autoclaw_plan["actions"]:
            r = a.dispatch(action)
            print(r["verb"], r.get("tier"), r.get("ok"))
    """

    def __init__(self, endpoint: str = None, api_key: Optional[str] = None,
                 dry_run: bool = False):
        if endpoint is None:
            endpoint = os.environ.get("NL2SHORTCUT_ENDPOINT", "http://127.0.0.1:7770")
        self.cli = ScutClient(endpoint=endpoint, api_key=api_key or _resolve_api_key())
        self.dry_run = dry_run

    def dispatch(self, action: Dict[str, Any]) -> Dict[str, Any]:
        verb = action.get("verb", "")
        args = action.get("args", {}) or {}
        mapped = _VERB_MAP.get(verb)

        t0 = time.time()
        try:
            if not mapped:
                return {
                    "verb": verb, "ok": False, "tier": None,
                    "error": f"unknown autoclaw verb '{verb}'",
                    "elapsed_ms": (time.time() - t0) * 1000,
                }

            tier = mapped["tier"]
            transform = mapped.get("transform")

            if tier == "meta":
                if transform == "sleep":
                    ms = float(args.get("ms", 0))
                    if not self.dry_run and ms > 0:
                        time.sleep(ms / 1000.0)
                    return {
                        "verb": verb, "ok": True, "tier": "meta",
                        "slept_ms": ms, "dry_run": self.dry_run,
                        "elapsed_ms": (time.time() - t0) * 1000,
                    }

            elif tier == "keyboard":
                combo = args.get("combo", "")
                intent = _combo_to_intent(combo)
                resp = self.cli.execute_keyboard(intent, dry_run=self.dry_run)
                ok = bool(resp.get("ok"))
                result = resp.get("result", {}) or {}
                return {
                    "verb": verb, "ok": ok, "tier": "keyboard",
                    "intent": intent, "combo": combo,
                    "key_combination": result.get("key_combination"),
                    "executed": result.get("executed"),
                    "elapsed_ms": result.get("execution_time_ms"),
                    "error": (None if ok else resp.get("error", {}).get("message") if isinstance(resp.get("error"), dict) else resp.get("error")),
                    "fallback": resp.get("fallback_recommendation"),
                }

            elif tier == "api":
                if transform == "clipboard_paste":
                    # 两步操作：先 copy(text) 再 paste
                    text = args.get("text", "")
                    self.cli.execute_api("copy", context={"text": text})
                    resp = self.cli.execute_api("paste")
                    ok = bool(resp.get("ok"))
                    result = resp.get("result", {}) or {}
                    return {
                        "verb": verb, "ok": ok, "tier": "api",
                        "action": f"clipboard-paste({len(text)} chars)",
                        "executed": result.get("executed"),
                        "elapsed_ms": result.get("execution_time_ms"),
                        "error": (None if ok else (resp.get("error", {}).get("message") if isinstance(resp.get("error"), dict) else resp.get("error"))),
                    }
                # 通用 API 调用
                intent = args.get("intent", verb)
                resp = self.cli.execute_api(intent, context=args.get("context", {}))
                ok = bool(resp.get("ok"))
                return {
                    "verb": verb, "ok": ok, "tier": "api",
                    "intent": intent,
                    "executed": (resp.get("result", {}) or {}).get("executed"),
                    "elapsed_ms": (resp.get("result", {}) or {}).get("execution_time_ms"),
                    "error": (None if ok else (resp.get("error", {}).get("message") if isinstance(resp.get("error"), dict) else resp.get("error"))),
                }

            elif tier == "vision":
                vision_action = mapped.get("action")
                transform = mapped.get("transform")

                if transform == "scroll_via_find":
                    # 视觉层级没有原生的 scroll 动作；给出一个提示
                    return {
                        "verb": verb, "ok": False, "tier": "vision",
                        "vision_action": vision_action,
                        "intent": verb,
                        "error": "scroll unsupported in vision tier; use pyautogui.scroll or keyboard PageDown",
                        "hint": "fallback to api tier: int(args.amount) * pyautogui.scroll",
                    }

                if transform == "move_via_click":
                    # 没有原生的 move；移动会在下一次 click 时隐式发生
                    return {
                        "verb": verb, "ok": True, "tier": "vision",
                        "vision_action": "noop",
                        "intent": verb,
                        "message": f"move is implicit; click at ({args.get('x')},{args.get('y')}) on next action",
                    }

                resp = self.cli.execute_vision(
                    intent=verb,
                    action=vision_action,
                    context={
                        "x": args.get("x"),
                        "y": args.get("y"),
                        "button": args.get("button", "left"),
                        "clicks": args.get("clicks", 1),
                        "needle": args.get("needle"),
                        "region": args.get("region"),
                    },
                )
                ok = bool(resp.get("ok"))
                return {
                    "verb": verb, "ok": ok, "tier": "vision",
                    "vision_action": vision_action,
                    "intent": verb,
                    "scut_action": resp.get("action"),
                    "message": resp.get("message"),
                    "hint": (resp.get("data") or {}).get("hint"),
                    "elapsed_ms": resp.get("duration_ms") or ((time.time() - t0) * 1000),
                    "error": (None if ok else (resp.get("error", {}).get("message") if isinstance(resp.get("error"), dict) else resp.get("error"))),
                }

            return {"verb": verb, "ok": False, "tier": None,
                    "error": f"unhandled tier '{tier}'",
                    "elapsed_ms": (time.time() - t0) * 1000}

        except Exception as e:
            return {
                "verb": verb, "ok": False, "tier": None,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": (time.time() - t0) * 1000,
            }


def main():
    """演示：分发一个示例 AutoClaw 计划。"""
    plan = {
        "agent": "autoclaw",
        "actions": [
            {"verb": "press",     "args": {"combo": "ctrl+c"}},
            {"verb": "type",      "args": {"text": "autoclaw says hi"}},
            {"verb": "press",     "args": {"combo": "ctrl+shift+t"}},
            {"verb": "click",     "args": {"x": 200, "y": 300, "button": "left"}},
            {"verb": "screenshot","args": {}},
            {"verb": "find",      "args": {"needle": "Submit"}},
            {"verb": "ocr",       "args": {}},
            {"verb": "scroll",    "args": {"amount": -3, "axis": "y"}},
            {"verb": "move",      "args": {"x": 400, "y": 400}},
            {"verb": "wait",      "args": {"ms": 50}},
        ],
    }

    print(f"[autoclaw_adapter] NL2Shortcut drop-in for AutoClaw Agent")
    a = ScutAutoClawAdapter(dry_run=True)
    dry_label = "yes" if a.dry_run else "no"
    print(f"Dispatching {len(plan['actions'])} AutoClaw actions (dry_run={dry_label}):\n")
    for action in plan["actions"]:
        r = a.dispatch(action)
        extra = ""
        if r.get("intent"):     extra = f" intent={r['intent']:>14}"
        if r.get("action"):     extra = f" {r['action']}"
        if r.get("vision_action"):
                                extra = f" vision={r['vision_action']}"
        ok = "ok" if r["ok"] else "FAIL"
        print(f"  {action['verb']:10s} {ok:>4}  tier={r.get('tier') or '-':<8}{extra}")


if __name__ == "__main__":
    main()
