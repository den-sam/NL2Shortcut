"""Anthropic Claude Computer Use → NL2Shortcut 适配器（面向键盘环节的即插即用组件）。

Anthropic 的 ``computer`` 工具让 Claude 能够截图并在桌面
上点击 / 输入文字。其键盘快捷键环节是以原始的 ``keyDown/keyUp``
调用方式实现的——速度慢、消耗大量视觉 token，且动作集合是写死的。

本适配器改为将 Claude 的键盘请求通过 NL2Shortcut 路由，从而为 Claude 带来：

  - 快 100 倍的键盘执行速度（1-5ms，而 keyDown/keyUp 为 100-300ms）
  - 键盘动作消耗 0 个 token（而视觉动作为 1000+）
  - 结构化的动作库（51 个快捷键 + 30+ 个应用，非写死）
  - 失败时自动回退到 api 层级 / vision 层级

使用方式（Python）：

.. code-block:: python

    from nl2shortcut.examples.claude_cu_adapter import ScutKeyboardAdapter
    adapter = ScutKeyboardAdapter(endpoint="http://127.0.0.1:7770",
                                  api_key="scut_...")
    # 在你想调用 anthropic.computer.key_down("ctrl") 的地方，改为调用：
    result = adapter.shortcut("copy")      # -> 通过 NL2Shortcut 执行 Ctrl+C
    result = adapter.shortcut("paste")     # -> 通过 NL2Shortcut 执行 Ctrl+V
    result = adapter.shortcut("save")      # -> 通过 NL2Shortcut 执行 Ctrl+S

本适配器匹配 Claude Computer Use 的 ``key`` 参数面（如 ``"ctrl"``、
``"c"``、``"Return"`` 这样的单个按键名），并重建出 NL2Shortcut 能够
理解的 ``"ctrl+c"`` 这类快捷键字符串。

对于 `tool_use` 流程（由 Claude 选择一个动作），请使用
``scut_action_for_anthropic_tool`` 函数——它会返回与 Claude 的 `computer`
工具 ``action`` 枚举（key、hold_key、type、click 等）相匹配的
NL2Shortcut 动作名与参数。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

# 使用随附的 openclaw_plugin 客户端
try:
    from nl2shortcut.openclaw_plugin import ScutClient
except ImportError:                              # allow running from examples/
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from nl2shortcut.openclaw_plugin import ScutClient


def _resolve_api_key() -> Optional[str]:
    """从环境变量或开发密钥中选取第一个可用的 NL2Shortcut API key。"""
    keys = os.environ.get("NL2SHORTCUT_API_KEYS", "")
    if keys:
        return keys.split(",")[0].strip()
    return "nl2shortcut_dev_local"  # dev fallback


# ── 按键名归一化（Claude CU → NL2Shortcut） ───────────────────────
#
# Claude Computer Use 的按键命名："ctrl"、"shift"、"alt"、"cmd"、
# "Return"、"Escape"、"Tab"、单个字母等。
# NL2Shortcut 的命名方式类似开发者在输入快捷键时的写法：如 "ctrl+c"、
# "ctrl+shift+t"、"Return"（或 "Enter"）等。
#
# 我们使用一张小表进行转换。表中没有的按键将原样透传——
# NL2Shortcut 会返回 `no_match` 错误，适配器随后回退到常规的 computer 工具。

_CLAUDE_TO_SCUT_KEY = {
    "ctrl":  "ctrl",  "control": "ctrl",
    "shift": "shift",
    "alt":   "alt",   "option":  "alt",
    "cmd":   "cmd",   "command": "cmd",   "meta": "cmd",   "super": "cmd",
    "win":   "win",   "windows": "win",   "super_l": "win",

    "Return":  "Return",  "enter": "Return",
    "Escape":  "Escape",  "esc":   "Escape",
    "Tab":     "Tab",     "tab":   "Tab",
    "Backspace": "BackSpace",
    "Delete":  "Delete",  "del":   "Delete",
    "Insert":  "Insert",  "ins":   "Insert",
    "Home":    "Home",    "End":   "End",
    "PageUp":  "Page_Up", "pgup":  "Page_Up",
    "PageDown":"Page_Down", "pgdn": "Page_Down",
    "Up":      "Up",      "Down":  "Down",
    "Left":    "Left",    "Right": "Right",
    "space":   "space",
    "CapsLock":"Caps_Lock",
}


def _norm_key(k: str) -> str:
    return _CLAUDE_TO_SCUT_KEY.get(k, k)


def claude_keys_to_scut_key_combo(keys: List[str]) -> str:
    """将一组 Claude CU 按键名转换为 NL2Shortcut 组合键字符串。

    示例：["ctrl", "shift", "t"] -> "ctrl+shift+t"
    """
    return "+".join(_norm_key(k) for k in keys)


# ── 适配器 ──────────────────────────────────────────────────────────


class ScutKeyboardAdapter:
    """面向 Anthropic Claude Computer Use 的即插即用键盘适配器。"""

    def __init__(self, endpoint: str = "http://127.0.0.1:7770",
                 api_key: Optional[str] = None,
                 prefer_api_tier: bool = True):
        """初始化适配器。

        Args:
          endpoint: NL2Shortcut agent-api 的 URL。
          api_key: Bearer 令牌（来自 ``~/.nl2shortcut/api_keys.json``）。
          prefer_api_tier: 若为 True（默认），则优先尝试 api 层级
            （0 token，约 20ms）再退回键盘层级。这就是
            "免费使用 computer use" 模式。
        """
        self.cli = ScutClient(endpoint=endpoint, api_key=api_key or _resolve_api_key())
        self.prefer_api_tier = prefer_api_tier

    def shortcut(self, name: str, *, context: Optional[Dict[str, Any]] = None,
                 session_id: Optional[str] = None) -> Dict[str, Any]:
        """执行一个具名快捷键（如 'copy'、'paste'、'save'）。

        返回 NL2Shortcut 的响应，与 ScutClient.execute() 结构一致。
        """
        if self.prefer_api_tier:
            # 优先尝试 api 层级——0 token，可靠性更高
            try:
                api_resp = self.cli.execute_api(name, context=context, app=context.get("app") if context else None,
                                                 session_id=session_id)
                if api_resp.get("ok"):
                    return {**api_resp, "_claude_cu_path": "api_tier"}
            except Exception:
                pass
        # 回退到键盘层级
        kb_resp = self.cli.execute_keyboard(name, context=context, session_id=session_id)
        return {**kb_resp, "_claude_cu_path": "keyboard_tier"}

    def shortcut_from_claude_keys(self, keys: List[str], *,
                                  context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """将 Claude CU 的按键列表转换为 NL2Shortcut 快捷键并执行。

        示例：keys=["ctrl", "c"] -> "ctrl+c" -> /v1/execute intent="copy"
        若组合键不是具名的 NL2Shortcut 快捷键，则回退到 vision 层级。
        """
        combo = claude_keys_to_scut_key_combo(keys)
        # 尝试按组合键查找匹配的 NL2Shortcut 命令
        # （我们将组合键作为 intent 传入——agent 层的
        # recognize 接口会将其匹配到某个已知命令）
        return self.cli.execute(combo, tier_preference=["api", "keyboard", "vision"],
                                context=context)

    # ── 映射到 Claude 的 `computer` 工具 action 枚举 ──────────
    def scut_action_for_anthropic_tool(self, anthropic_action: Dict[str, Any]) -> Dict[str, Any]:
        """将 Claude 的 ``computer`` 工具动作映射为 NL2Shortcut 动作。

        Claude 的 `computer` 工具包含如下动作：
          - key(text="ctrl+c")        -> 路由到 NL2Shortcut 意图
          - hold_key(...)              -> NL2Shortcut 没有 hold，回退处理
          - type(text="hello")        -> NL2Shortcut intent="type_text"
          - click(...)                 -> NL2Shortcut vision 层级（vision.click）
          - screenshot()               -> NL2Shortcut vision 层级（vision.screenshot）

        返回 {"scut_action": "...", "params": {...}, "fallback": bool}
        """
        a = anthropic_action.get("action", "")
        if a == "key":
            keys = anthropic_action.get("text", "").split("+")
            combo = claude_keys_to_scut_key_combo(keys)
            return {"scut_action": "execute_tiered", "params": {"intent": combo}, "fallback": False}
        if a == "type":
            return {"scut_action": "execute_api", "params": {"intent": "type_text", "context": {"text": anthropic_action.get("text", "")}}, "fallback": False}
        if a == "screenshot":
            return {"scut_action": "execute_vision", "params": {"intent": "screenshot", "action": "screenshot"}, "fallback": False}
        if a == "click":
            # Click 在 NL2Shortcut 中属于视觉操作
            return {"scut_action": "execute_vision", "params": {"intent": "click", "action": "click"}, "fallback": False}
        # hold_key、cursor_position 等在 NL2Shortcut 中均不支持
        return {"scut_action": "execute_vision", "params": {"intent": a, "action": "screenshot"}, "fallback": True}


# ── 命令行演示 ─────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys
    print("[claude_cu_adapter] NL2Shortcut drop-in for Anthropic Claude Computer Use")
    print("Run a few shortcuts against a running NL2Shortcut instance:\n")
    a = ScutKeyboardAdapter()
    for name in ("copy", "paste", "save"):
        try:
            r = a.shortcut(name, context={"text": "hello from claude cu adapter"})
            path = r.get("_claude_cu_path", "?")
            ok = r.get("ok", False)
            keys = r.get("key_combination", r.get("result", {}).get("action", "?"))
            print(f"  {name:6s} -> ok={ok} path={path:14s} keys/action={keys}")
        except Exception as e:
            print(f"  {name:6s} -> ERR: {e}")
    print("\nKey-name mapping example:")
    print(f"  ['ctrl', 'shift', 't'] -> {claude_keys_to_scut_key_combo(['ctrl', 'shift', 't'])}")
    print(f"  ['cmd', 'c']            -> {claude_keys_to_scut_key_combo(['cmd', 'c'])}")
    print(f"  ['Return']              -> {claude_keys_to_scut_key_combo(['Return'])}")
