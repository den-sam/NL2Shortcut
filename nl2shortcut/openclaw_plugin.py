"""OpenClaw plugin for NL2Shortcut — Three-Tier Execution Endpoint (v2.0.0).

Exposes nl2shortcut as a **three-tier Action plugin** to OpenClaw agents and
compatible orchestrators (Anthropic Claude Computer Use, AutoGLM,
AutoClaw, etc.). The agent can:

  1. Discover what nl2shortcut offers  (PLUGIN_MANIFEST)
  2. Pick the right tier        (tiers.py: keyboard / api / vision)
  3. Call the right endpoint    (/v1/execute /v1/api/execute /v1/vision/execute)
  4. Get back tier_used / tier_recommended / fallback_recommendation
  5. Self-check + auto-fallback on failure

This module is self-contained — it can be imported from anywhere; it
just talks HTTP to a running nl2shortcut agent-api instance.

─── Position in the OpenClaw ecosystem ───────────────────────────────

NL2Shortcut is the **high-speed execution endpoint** in the OpenClaw
Agent-action taxonomy. The taxonomy has three slots:

  1. **API Action**   (e.g. ``composio``, ``e2b``)         — 0 token, fast
  2. **Keyboard Action** (THIS — nl2shortcut)                      — 0 token, fastest
  3. **Vision Action**  (e.g. ``computer-use``, ``cogagent``) — ~1000 token, slow

NL2Shortcut uniquely spans slot 1 + 2 + 3 with a single decision matrix:
  - "Is there a programmatic API for this command?" → /v1/api/execute
  - "Is it a real keyboard shortcut?"               → /v1/execute
  - "Both failed? Take a screenshot for vision"    → /v1/vision/execute

The agent only needs to know ONE endpoint (``http://127.0.0.1:7770``)
and call ``POST /v1/execute`` with an intent string. NL2Shortcut routes the
rest. On failure, the response includes a ``fallback_recommendation``
that tells the agent exactly which tier to try next.

─── Compatibility ──────────────────────────────────────────────────

  - **OpenClaw 1.x** (this server's host platform)         : full
  - **Anthropic Claude Computer Use** (``computer`` tool) : full (see
    ``examples/claude_cu_adapter.py`` — drop-in for any CU agent)
  - **Zhipu AutoGLM**                                    : full (see
    ``examples/autoglm_adapter.py``)
  - **AutoClaw**                                          : full (OpenClaw
    protocol, same manifest schema)
  - **Raw HTTP**                                          : full
  - **CLI / REPL** (humans)                               : `nl2shortcut exec "save"`

Usage from OpenClaw config (~/.openclaw/config.json or workspace):

.. code-block:: json

  {
    "plugins": [
      {
        "name":       "nl2shortcut-execution-endpoint",
        "module":     "scut_plugin",
        "endpoint":   "http://127.0.0.1:7770",
        "actions":    ["execute_shortcut", "execute_api", "execute_vision"],
        "tier_priority": ["keyboard", "api", "vision"]
      }
    ]
  }
"""

import json
import os
import time
from typing import Any, Dict, List, Optional
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError


# ── Client ───────────────────────────────────────────────────────────


class ScutClient:
    """Minimal client for the nl2shortcut Agent API (three-tier aware)."""

    def __init__(self, endpoint: Optional[str] = None, timeout: float = 5.0,
                 api_key: Optional[str] = None):
        # Honor env vars: NL2SHORTCUT_ENDPOINT (override), NL2SHORTCUT_API_HOST/NL2SHORTCUT_API_PORT
        if endpoint is None:
            endpoint = os.environ.get("NL2SHORTCUT_ENDPOINT")
        if endpoint is None:
            host = os.environ.get("NL2SHORTCUT_API_HOST", "127.0.0.1")
            port = os.environ.get("NL2SHORTCUT_API_PORT", "7770")
            endpoint = f"http://{host}:{port}"
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.endpoint}{path}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(url, data=body, headers=self._headers(), method="POST")
        with urlrequest.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get(self, path: str) -> Dict[str, Any]:
        url = f"{self.endpoint}{path}"
        req = urlrequest.Request(url, headers=self._headers(), method="GET")
        with urlrequest.urlopen(url, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── Tier 1: keyboard (primary — fastest, 0 token) ────────────
    def execute_keyboard(self, intent: str, *, dry_run: bool = False,
                         context: Optional[Dict[str, Any]] = None,
                         session_id: Optional[str] = None,
                         fallback_policy: str = "auto") -> Dict[str, Any]:
        """Execute via keyboard injection. Returns 200 on inject, 422 on self-check fail.

        Speed: 1-5ms p50. Tokens: 0. Reliability: medium (UI shifts can drop keys).
        """
        body: Dict[str, Any] = {"intent": intent, "dry_run": dry_run}
        if context:        body["context"] = context
        if session_id:     body["session_id"] = session_id
        if fallback_policy and fallback_policy != "auto":
            body["fallback_policy"] = fallback_policy
        return self._post("/v1/execute", body)

    # ── Tier 2: api (programmatic, 0 token, high reliability) ────
    def execute_api(self, intent: str, *,
                    context: Optional[Dict[str, Any]] = None,
                    app: Optional[str] = None,
                    session_id: Optional[str] = None) -> Dict[str, Any]:
        """Execute via programmatic API (clipboard, vscode command, OS API).

        Speed: 10-30ms p50. Tokens: 0. Reliability: high (no UI race).
        Only works for commands with ``api_equivalent`` in agent_metadata.
        """
        body: Dict[str, Any] = {"intent": intent}
        if context:    body["context"] = context
        if app:        body["app"] = app
        if session_id: body["session_id"] = session_id
        return self._post("/v1/api/execute", body)

    # ── Tier 3: vision (screenshot → CogAgent/Claude CU, last resort) ──
    def execute_vision(self, intent: str, action: str = "screenshot",
                       *, app: Optional[str] = None,
                       context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Capture screenshot + dispatch hint for a vision model.

        Returns a base64 PNG in ``data.image_b64`` plus a hint in
        ``data.hint``. The Agent should then forward the image to
        CogAgent / Claude Computer Use / OmniParser to get back
        (x, y) click coordinates or text to type.

        Speed: 70-150ms capture + 1-3s vision model round-trip.
        Tokens: ~1000. Reliability: highest (recovers from any UI state).
        """
        body: Dict[str, Any] = {"intent": intent, "action": action}
        if app:     body["app"] = app
        if context: body["context"] = context
        return self._post("/v1/vision/execute", body)

    # ── Tier-aware router — picks tier and may auto-fallback ────
    def execute(self, intent: str, *,
                tier_preference: Optional[List[str]] = None,
                max_escalations: int = 2,
                **kwargs) -> Dict[str, Any]:
        """Tier-aware execution with optional auto-escalation.

        ``tier_preference`` defaults to ``["api", "keyboard", "vision"]``
        (api first because it's faster + more reliable; vision last
        because it's expensive).

        On failure, walks the preference list and tries the next tier.
        Returns the final attempt's response, augmented with
        ``escalation_path`` (which tiers were tried) and
        ``escalation_count`` (how many hops).
        """
        prefs = tier_preference or ["api", "keyboard", "vision"]
        escalation_path = []
        last_resp: Dict[str, Any] = {}

        for tier in prefs:
            if len(escalation_path) >= max_escalations + 1:
                break
            escalation_path.append(tier)
            if tier == "keyboard":
                last_resp = self.execute_keyboard(intent, **kwargs)
            elif tier == "api":
                last_resp = self.execute_api(intent, **{k: v for k, v in kwargs.items() if k in ("context", "app", "session_id")})
            elif tier == "vision":
                # For vision, action is the only difference; default screenshot
                last_resp = self.execute_vision(intent, action=kwargs.pop("vision_action", "screenshot"), **{k: v for k, v in kwargs.items() if k in ("app", "context")})
            else:
                continue
            if last_resp.get("ok"):
                break
            # Honor server's fallback_recommendation if present
            fb = last_resp.get("fallback_recommendation")
            if fb and fb.get("tier") in prefs and fb["tier"] != tier:
                # Move recommended tier to front of remaining
                if fb["tier"] in escalation_path:
                    continue
                # Restart with the recommended tier next
                idx = prefs.index(tier)
                prefs = prefs[:idx+1] + [fb["tier"]] + [t for t in prefs[idx+1:] if t != fb["tier"]]

        last_resp["escalation_path"] = escalation_path
        last_resp["escalation_count"] = len(escalation_path) - 1
        return last_resp

    # ── Capability discovery ────────────────────────────────────
    def health(self) -> Dict[str, Any]:
        return self._get("/v1/health")

    def capabilities(self) -> Dict[str, Any]:
        return self._get("/v1/capabilities")

    def keys_index(self) -> Dict[str, Any]:
        """Compact index of all commands (designed for Agent context budget)."""
        return self._get("/v1/keys")

    def stats(self) -> Dict[str, Any]:
        """Per-tier / per-command execution stats with p50/p95/p99 latency."""
        return self._get("/v1/stats")

    def recognize(self, intent: str) -> Dict[str, Any]:
        return self._post("/v1/recognize", {"intent": intent})

    def sequence(self, steps: List[Dict[str, Any]], stop_on_error: bool = True) -> Dict[str, Any]:
        return self._post("/v1/sequence", {"steps": steps, "stop_on_error": stop_on_error})

    def session_start(self, *, app: str = "", platform: str = "") -> Dict[str, Any]:
        return self._post("/v1/session/start", {"app": app, "platform": platform})

    def session_end(self, session_id: str) -> Dict[str, Any]:
        return self._post("/v1/session/end", {"session_id": session_id})


# ── OpenClaw plugin manifest (v2.0.0 — three-tier) ─────────────────
#
# OpenClaw discovers plugins via this manifest, then calls actions
# via the JSON-RPC-ish surface. The manifest now reflects the
# three-tier architecture so OpenClaw can pick the right tier per task.

PLUGIN_MANIFEST: Dict[str, Any] = {
    "name":        "nl2shortcut-execution-endpoint",
    "version":     "2.0.0",
    "vendor":      "nl2shortcut",
    "homepage":    "https://nl2shortcut.dev",
    "tier":        "keyboard",                 # primary tier this plugin belongs to
    "tiers_offered": ["keyboard", "api", "vision"],
    "description": (
        "NL2Shortcut is the OpenClaw high-speed execution endpoint. Turn intent "
        "(NL or structured call) into keyboard / API / vision actions with "
        "<5ms p50 keyboard latency, 0 tokens, structured stability + "
        "api_equivalent + gui_fallback + tier-recommendation metadata. "
        "51 built-in shortcuts across 30+ apps, three tiers, self-check + "
        "auto-fallback. Drop-in replacement for Anthropic Claude Computer "
        "Use's keyboard leg — same interface, 100x faster, 0 tokens."
    ),

    "endpoint":    "http://127.0.0.1:7770",
    "auth":        "Bearer token (set NL2SHORTCUT_API_KEYS env or ~/.nl2shortcut/api_keys.json)",
    "scopes":      ["execute", "recognize", "plan", "admin"],

    # Three-tier action surface (one action per tier)
    "actions": [
        {
            "name":        "execute_keyboard",
            "tier":        "keyboard",
            "description": "Execute via keyboard injection (primary tier).",
            "speed":       "< 5ms p50",
            "tokens":      0,
            "reliability": "medium",
            "params": {
                "intent":     {"type": "string",  "required": True,  "description": "Natural language or command name (e.g. 'save the file')"},
                "dry_run":    {"type": "boolean", "required": False, "default": False},
                "context":    {"type": "object",  "required": False, "description": "{text, line, file_path, app, ...}"},
                "session_id": {"type": "string",  "required": False, "description": "Multi-step session id"},
                "fallback_policy": {"type": "string", "required": False, "default": "auto", "enum": ["auto", "gui_retry", "escalate", "abort"]},
            },
            "returns": {
                "ok":                  {"type": "boolean"},
                "command":             {"type": "string"},
                "key_combination":     {"type": "string"},
                "matched_keyword":     {"type": "string"},
                "executed":            {"type": "boolean"},
                "execution_time_ms":   {"type": "number"},
                "tier_used":           {"type": "string",  "enum": ["keyboard"]},
                "tier_recommended":    {"type": "string",  "enum": ["keyboard", "api", "vision"]},
                "tier_hint":           {"type": "string"},
                "fallback_triggered":  {"type": "boolean"},
                "fallback_suggested":  {"type": "string"},
                "metadata":            {"type": "object",  "description": "{api_equivalent, gui_fallback, stability}"},
            },
        },
        {
            "name":        "execute_api",
            "tier":        "api",
            "description": "Execute via programmatic API (clipboard / vscode command / OS API). 0 tokens, high reliability, ~20ms.",
            "speed":       "< 30ms p50",
            "tokens":      0,
            "reliability": "high",
            "params": {
                "intent":     {"type": "string", "required": True},
                "context":    {"type": "object", "required": False, "description": "{text, file_path, ...}"},
                "app":        {"type": "string", "required": False, "description": "Target app (vscode, chrome, ...)"},
                "session_id": {"type": "string", "required": False},
            },
            "returns": {
                "ok":                {"type": "boolean"},
                "action":            {"type": "string",  "description": "API that was called (e.g. os.clipboard.copy)"},
                "message":           {"type": "string"},
                "duration_ms":       {"type": "number"},
                "platform":          {"type": "string"},
                "tier_used":         {"type": "string",  "enum": ["api"]},
                "api_equivalent":    {"type": "string"},
            },
        },
        {
            "name":        "execute_vision",
            "tier":        "vision",
            "description": "Capture screenshot for vision-model dispatch (CogAgent / Claude CU / OmniParser). 1-3s + ~1000 tokens, highest reliability.",
            "speed":       "70-150ms capture (excluding vision model round-trip)",
            "tokens":      "~1000",
            "reliability": "highest",
            "params": {
                "intent":     {"type": "string", "required": True},
                "action":     {"type": "string", "required": False, "default": "screenshot",
                               "enum": ["screenshot", "find", "click", "ocr"]},
                "app":        {"type": "string",  "required": False},
                "context":    {"type": "object",  "required": False},
            },
            "returns": {
                "ok":              {"type": "boolean"},
                "action":          {"type": "string",  "enum": ["vision.screenshot", "vision.find", "vision.click", "vision.ocr"]},
                "data": {
                    "format":    {"type": "string"},
                    "width":     {"type": "number"},
                    "height":    {"type": "number"},
                    "size_bytes": {"type": "number"},
                    "image_b64": {"type": "string",  "description": "PNG base64 — forward to vision model"},
                    "hint":      {"type": "string",  "description": "Action-specific prompt for the vision model"},
                },
                "tier_used": {"type": "string", "enum": ["vision"]},
            },
        },
        {
            "name":        "execute_tiered",
            "tier":        "auto",
            "description": "Tier-aware execution with auto-fallback. Tries each tier in order, returns first success.",
            "params": {
                "intent":          {"type": "string", "required": True},
                "tier_preference": {"type": "array",  "required": False, "default": ["api", "keyboard", "vision"]},
                "max_escalations": {"type": "number", "required": False, "default": 2},
            },
            "returns": {
                "ok":                {"type": "boolean"},
                "tier_used":         {"type": "string"},
                "escalation_path":   {"type": "array"},
                "escalation_count":  {"type": "number"},
            },
        },
        {
            "name":        "recognize_intent",
            "tier":        "meta",
            "description": "Recognize which shortcut an intent maps to (no execution).",
            "params": {"intent": {"type": "string", "required": True}},
            "returns": {
                "command":    {"type": "string"},
                "confidence": {"type": "number"},
                "alternatives": {"type": "array"},
            },
        },
        {
            "name":        "list_capabilities",
            "tier":        "meta",
            "description": "List all 51 shortcuts + agent_metadata (api_equivalent, gui_fallback, stability).",
            "params": {},
            "returns": {"shortcuts": {"type": "array"}},
        },
        {
            "name":        "keys_index",
            "tier":        "meta",
            "description": "Compact index of all 51 commands + key combos (1.5KB, fits Agent context).",
            "params": {},
            "returns": {
                "count":  {"type": "number"},
                "index":  {"type": "array"},
            },
        },
        {
            "name":        "get_stats",
            "tier":        "meta",
            "description": "Per-tier / per-command execution stats with p50/p95/p99 latency.",
            "params": {},
            "returns": {
                "total_requests":  {"type": "number"},
                "tiers":           {"type": "object"},
                "top_commands":    {"type": "array"},
            },
        },
        {
            "name":        "session_start",
            "tier":        "meta",
            "description": "Open a multi-step session (for cross-step context).",
            "params": {"app": {"type": "string", "required": False}, "platform": {"type": "string", "required": False}},
            "returns": {"session_id": {"type": "string"}},
        },
        {
            "name":        "session_end",
            "tier":        "meta",
            "description": "Close a multi-step session.",
            "params": {"session_id": {"type": "string", "required": True}},
            "returns": {"ok": {"type": "boolean"}},
        },
        {
            "name":        "execute_sequence",
            "tier":        "keyboard",
            "description": "Execute a multi-step plan atomically (all keyboard tier).",
            "params": {
                "steps":          {"type": "array",  "required": True, "items": {"intent": "string"}},
                "stop_on_error":  {"type": "boolean", "default": True},
            },
            "returns": {"steps": {"type": "array"}, "ok": {"type": "boolean"}},
        },
    ],

    # Decision support: tell the orchestrator (OpenClaw / Claude CU / AutoGLM)
    # when to prefer nl2shortcut and when to use a complementary tool.
    "routing_hints": {
        "prefer_when": [
            "User/agent wants to execute a keyboard shortcut (copy, paste, save, undo, etc.)",
            "Task is part of a multi-step workflow on a desktop app (IDE / browser / editor)",
            "Agent needs structured metadata: 'is there a programmatic API? what's the GUI fallback?'",
            "Hot path: 10+ steps/second with < 5ms p50 latency requirement",
            "Multi-tier execution desired: try keyboard → escalate to api → escalate to vision on fail",
            "Drop-in replacement for Anthropic Claude Computer Use's keyboard leg (same interface, 100x faster, 0 tokens)",
        ],
        "fallback_when": [
            "nl2shortcut returns confidence < 0.4 in /v1/recognize",
            "nl2shortcut returns error_code 'no_match' (intent not in our 51-shortcut library)",
            "Application version is incompatible with the shortcut (rare; detect via self-check failure)",
            "User explicitly demands vision-only ('see the screen and click')",
        ],
        "complementary_to": [
            "OpenClaw action plugins (composio, e2b, etc.) — for true API-level ops",
            "Anthropic Claude Computer Use — for vision-only ops NL2Shortcut's vision tier can't do (subjective UI)",
            "Browser-automation tools (Playwright, Puppeteer) — for web-specific actions",
            "File tools (Read/Write/Edit) — for batch file ops",
        ],
    },

    # Performance envelope (measured; see benchmarks/results.json)
    "performance": {
        "keyboard_tier": {"p50_ms": 1.5, "p95_ms": 3.0, "p99_ms": 5.0, "tokens": 0, "reliability": "medium"},
        "api_tier":      {"p50_ms": 18,  "p95_ms": 30,  "p99_ms": 50,  "tokens": 0, "reliability": "high"},
        "vision_tier":   {"p50_ms": 85,  "p95_ms": 130, "p99_ms": 150, "tokens": 1000, "reliability": "highest"},
    },

    # Compatibility matrix — explicit declaration
    "compatibility": {
        "openclaw":              {"version": ">= 0.2.30",   "status": "first-class",  "auth": "Bearer token"},
        "anthropic_claude_cu":   {"version": ">= 2024-10",  "status": "drop-in",      "adapter": "examples/claude_cu_adapter.py"},
        "zhipu_autoglm":         {"version": ">= 1.0",      "status": "drop-in",      "adapter": "examples/autoglm_adapter.py"},
        "autoclaw":              {"version": ">= 1.0",      "status": "first-class",  "note": "OpenClaw protocol"},
        "raw_http":              {"status": "yes",          "schema": "/v1/schema (OpenAPI 3.0)"},
        "cli_humans":            {"command": "nl2shortcut exec 'save'", "status": "yes"},
    },
}


# ── OpenClaw entry point ─────────────────────────────────────────────


def handle_action(action: str, params: Dict[str, Any], client: Optional[ScutClient] = None) -> Dict[str, Any]:
    """OpenClaw entry point: dispatch an action call to nl2shortcut.

    Maps OpenClaw action names → ScutClient methods. The orchestrator
    calls this for each plugin action.
    """
    cli = client or ScutClient()
    try:
        if action == "execute_keyboard":
            return cli.execute_keyboard(
                intent=params["intent"],
                dry_run=params.get("dry_run", False),
                context=params.get("context"),
                session_id=params.get("session_id"),
                fallback_policy=params.get("fallback_policy", "auto"),
            )
        if action == "execute_api":
            return cli.execute_api(
                intent=params["intent"],
                context=params.get("context"),
                app=params.get("app"),
                session_id=params.get("session_id"),
            )
        if action == "execute_vision":
            return cli.execute_vision(
                intent=params["intent"],
                action=params.get("action", "screenshot"),
                app=params.get("app"),
                context=params.get("context"),
            )
        if action == "execute_tiered":
            return cli.execute(
                intent=params["intent"],
                tier_preference=params.get("tier_preference"),
                max_escalations=params.get("max_escalations", 2),
                context=params.get("context"),
                app=params.get("app"),
                session_id=params.get("session_id"),
            )
        if action == "recognize_intent":
            return cli.recognize(intent=params["intent"])
        if action == "list_capabilities":
            return cli.capabilities()
        if action == "keys_index":
            return cli.keys_index()
        if action == "get_stats":
            return cli.stats()
        if action == "session_start":
            return cli.session_start(app=params.get("app", ""), platform=params.get("platform", ""))
        if action == "session_end":
            return cli.session_end(params["session_id"])
        if action == "execute_sequence":
            return cli.sequence(steps=params["steps"], stop_on_error=params.get("stop_on_error", True))
        return {"ok": False, "error": {"code": "unknown_action", "message": f"action '{action}' not handled"}}
    except HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": {"code": f"http_{e.code}", "message": str(e)}}
    except URLError as e:
        return {"ok": False, "error": {"code": "unreachable", "message": f"cannot reach nl2shortcut agent-api: {e}"}}


# ── Self-test (when run as main) ─────────────────────────────────────


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--manifest":
        print(json.dumps(PLUGIN_MANIFEST, ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "--validate":
        # Validate the manifest structure
        m = PLUGIN_MANIFEST
        required = ["name", "version", "tier", "tiers_offered", "endpoint", "actions", "routing_hints", "performance", "compatibility"]
        missing = [k for k in required if k not in m]
        if missing:
            print(f"FAIL: missing keys {missing}", file=sys.stderr)
            sys.exit(1)
        # Validate each action has params/returns
        for a in m["actions"]:
            for k in ("name", "tier", "params", "returns"):
                if k not in a:
                    print(f"FAIL: action {a.get('name', '?')} missing key {k}", file=sys.stderr)
                    sys.exit(1)
        print(f"OK: {m['name']} v{m['version']} — {len(m['actions'])} actions, {len(m['tiers_offered'])} tiers, {len(m['compatibility'])} compatible systems")
    else:
        cli = ScutClient()
        try:
            h = cli.health()
            print(f"[nl2shortcut-plugin] connected: {h.get('version', '?')} role={h.get('role', '?')} tiers={h.get('tiers_available', [])}")
        except (URLError, HTTPError) as e:
            print(f"[nl2shortcut-plugin] cannot reach nl2shortcut agent-api at {cli.endpoint}: {e}", file=sys.stderr)
            print("Start it with: nl2shortcut agent-api", file=sys.stderr)
