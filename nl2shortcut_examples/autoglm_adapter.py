"""智谱 AutoGLM → NL2Shortcut 适配器（面向 keyboard / API 动作层的即插即用组件）。

AutoGLM（智谱 AutoGLM）是一个将用户目标拆解为手机 / 桌面动作的 Agent。
它的动作面与 Claude CU 的 ``computer`` 工具相似，但底层传输采用的是基于
HTTP 的 JSON。

本适配器将 AutoGLM 的 ``operation`` 字段通过 NL2Shortcut 路由：

  - AutoGLM ``operation: "Tap" / "Click"``            -> NL2Shortcut 视觉（vision）层级
  - AutoGLM ``operation: "Type"``                      -> NL2Shortcut api 层级（剪贴板）
  - AutoGLM ``operation: "Key" / "Shortcut"``          -> NL2Shortcut 键盘（keyboard）层级
  - AutoGLM ``operation: "Back" / "Home" / "LongPress"`` -> NL2Shortcut 视觉层级（系统导航）

本适配器以 AutoGLM 期望的结构返回响应，因此你可以在不改动 Agent 逻辑的前提下
替换底层传输方式。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

try:
    from nl2shortcut.openclaw_plugin import ScutClient
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nl2shortcut.openclaw_plugin import ScutClient


def _resolve_api_key() -> Optional[str]:
    """从环境变量或开发密钥中选取第一个可用的 NL2Shortcut API key。"""
    keys = os.environ.get("NL2SHORTCUT_API_KEYS", "")
    if keys:
        return keys.split(",")[0].strip()
    return "nl2shortcut_dev_local"  # dev fallback


    # 将 AutoGLM 操作名映射到 NL2Shortcut 意图 + 层级。
    # AutoGLM 操作（可映射到 NL2Shortcut 的子集）：
    #   Tap, Click, Type, Key, Shortcut, Back, Home, LongPress, Swipe
    #
    # Type => 通过剪贴板的 api 层级（paste）：先将文本复制到剪贴板再粘贴
    # Shortcut => 键盘层级（识别 ctrl+c -> copy）
    # Tap/Click => 视觉层级（截图 + 点击提示）
    # Back/Home => 视觉层级（系统导航，仅视觉）
_OP_MAP: Dict[str, Dict[str, Any]] = {
    "Tap":     {"scut_intent": "click",      "scut_tier": "vision",  "scut_action": "click"},
    "Click":   {"scut_intent": "click",      "scut_tier": "vision",  "scut_action": "click"},
    "Type":    {"scut_intent": "paste",      "scut_tier": "api",     "scut_action": None,
                "transform": "clipboard_paste"},
    "Key":     {"scut_intent": "press",      "scut_tier": "keyboard","scut_action": None},
    "Shortcut":{"scut_intent": "shortcut",   "scut_tier": "keyboard","scut_action": None,
                "transform": "key_combo_to_intent"},
    "Back":    {"scut_intent": "back",       "scut_tier": "vision",  "scut_action": "screenshot"},
    "Home":    {"scut_intent": "home",       "scut_tier": "vision",  "scut_action": "screenshot"},
}


def _key_combo_to_intent(keys: List[str]) -> str:
    """将 ['ctrl', 'c'] 映射为 'copy'（或最匹配的 NL2Shortcut 意图）。"""
    combo = "".join(k.lower() for k in keys)
    # 常见映射
    table = {
        "ctrlc":  "copy",
        "ctrlv":  "paste",
        "ctrlx":  "cut",
        "ctrla":  "select_all",
        "ctrlz":  "undo",
        "ctrly":  "redo",
        "ctrls":  "save",
        "ctrlf":  "find",
        "ctrln":  "new",
        "ctrlo":  "open",
        "ctrlw":  "close",
        "ctrlp":  "print",
        "ctrlr":  "refresh",
        "ctrlh":  "replace",
        "ctrlg":  "go_to_line",
        "ctrlt":  "new_tab",
        "cmdc":   "copy",
        "cmdv":   "paste",
        "cmdx":   "cut",
        "cmds":   "save",
    }
    return table.get(combo, combo)


class ScutAutoGLMAdapter:
    """即插即用适配器：AutoGLM 操作 JSON <-> NL2Shortcut agent-api 互转。"""

    def __init__(self, endpoint: str = "http://127.0.0.1:7770",
                 api_key: Optional[str] = None):
        self.cli = ScutClient(endpoint=endpoint, api_key=api_key or _resolve_api_key())

    def execute_operation(self, op: Dict[str, Any]) -> Dict[str, Any]:
        """接收一个 AutoGLM ``operation`` 字典，返回 AutoGLM 形态的响应。

        AutoGLM 操作结构（子集）：
          {"operation": "Tap",     "x": 100, "y": 200}
          {"operation": "Type",    "text": "hello"}
          {"operation": "Key",     "key": "Enter"}
          {"operation": "Shortcut","keys": ["ctrl", "c"]}
        """
        op_name = op.get("operation", "")
        mapped = _OP_MAP.get(op_name)
        t0 = time.time()
        try:
            if not mapped:
                # 未知操作：截图，以便 AutoGLM 重新决策
                resp = self.cli.execute_vision(intent=op_name, action="screenshot")
                return self._wrap(resp, ok=False, reason=f"unknown op '{op_name}', screenshot taken", elapsed_ms=(time.time()-t0)*1000)

            tier = mapped["scut_tier"]
            intent = mapped["scut_intent"]

            if tier == "api":
                context: Dict[str, Any] = {}
                if op_name == "Type":
                    # 两步操作：先将文本复制到剪贴板，再粘贴
                    text = op.get("text", "")
                    self.cli.execute_api("copy", context={"text": text})
                    resp = self.cli.execute_api("paste")
                    resp["action"] = f"clipboard-paste({len(text)} chars)"
                else:
                    resp = self.cli.execute_api(intent, context=context)
            elif tier == "keyboard":
                if op_name == "Shortcut":
                    keys = op.get("keys", [])
                    intent = _key_combo_to_intent(keys)
                # 出于安全考虑默认使用 dry_run=True（真实的 AutoGLM
                # 生产环境 Agent 会显式传入 dry_run=False）。
                resp = self.cli.execute_keyboard(intent, dry_run=True)
            elif tier == "vision":
                resp = self.cli.execute_vision(intent, action=mapped.get("scut_action") or "screenshot")
            else:
                resp = {"ok": False, "error": {"code": "no_tier", "message": f"no tier for {op_name}"}}

            return self._wrap(resp, ok=resp.get("ok", False),
                              reason=resp.get("error", {}).get("message", "ok"),
                              elapsed_ms=(time.time()-t0)*1000)
        except Exception as e:
            return self._wrap({"ok": False, "error": {"code": "exception", "message": str(e)}},
                              ok=False, reason=str(e), elapsed_ms=(time.time()-t0)*1000)
        finally:
            # 短暂间隔，避免 Windows 上出现 TCP 高频发送问题
            time.sleep(0.05)

    @staticmethod
    def _wrap(scut_resp: Dict[str, Any], *, ok: bool, reason: str, elapsed_ms: float) -> Dict[str, Any]:
        """将 NL2Shortcut 的响应包装为 AutoGLM 形态的封装。"""
        return {
            "ok":           ok,
            "reason":       reason,
            "elapsed_ms":   round(elapsed_ms, 2),
            "scut_tier":    scut_resp.get("tier_used") or scut_resp.get("tier"),
            "scut_result":  scut_resp.get("result", {}),
            "scut_resp":    scut_resp,
        }


# ── 命令行演示 ─────────────────────────────────────────────────────────


if __name__ == "__main__":
    print("[autoglm_adapter] NL2Shortcut drop-in for Zhipu AutoGLM")
    print("Simulating 5 AutoGLM operations:\n")
    a = ScutAutoGLMAdapter()
    ops = [
        {"operation": "Type",     "text": "hello from autoglm"},
        {"operation": "Shortcut", "keys": ["ctrl", "c"]},
        {"operation": "Shortcut", "keys": ["ctrl", "v"]},
        {"operation": "Tap",      "x": 100, "y": 200},
        {"operation": "Back"},
    ]
    for op in ops:
        r = a.execute_operation(op)
        ok = r["ok"]
        tier = r.get("scut_tier", "?")
        elapsed = r.get("elapsed_ms", 0)
        print(f"  {op['operation']:10s} {str(op.get('keys') or op.get('text') or op):30s} -> ok={ok} tier={str(tier):9s} {elapsed:.1f}ms")
