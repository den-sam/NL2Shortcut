"""nl2shortcut Agent API — JSON interface for OpenClaw / Claude Computer Use / 智谱 AutoGLM.

Designed for machine callers, not humans. The CLI is for humans; this is for Agents.

Endpoints (HTTP, port 7770 by default):

  POST /v1/execute        Execute a single intent (keyboard tier)
  POST /v1/recognize      Intent recognition only (no execution)
  POST /v1/sequence       Execute a multi-step plan atomically
  POST /v1/chain          Execute a low-level keyboard chain (with inline sleep waits)
  POST /v1/plan           Goal -> multi-step plan (decompose)
  POST /v1/api/execute    Execute a command via the api tier (programmatic)
  POST /v1/vision/execute Execute via vision tier (screenshot receipt)
  GET  /v1/stats          Execution stats (per-tier, per-command, latency, errors)
  POST /v1/stats/reset    Reset stats (admin scope only)
  GET  /v1/capabilities   List available shortcuts + metadata
  GET  /v1/health         Liveness + version + role/tier
  GET  /v1/keys           Compact shortcut index (for Agent context)
  GET  /v1/shortcut       Lookup by command name
  GET  /v1/session        List active sessions (admin)
  POST /v1/session/start  Open a session for stateful multi-step plans
  POST /v1/session/end    Close a session, return summary
  GET  /v1/rate_limit     Show rate-limit status for caller

Three execution tiers:
  - keyboard (this server's primary; default)
  - api (programmatic; /v1/api/execute for OS-level actions, receipt for VS Code)
  - vision (screenshot capture; /v1/vision/execute returns receipt for Agent dispatch)

Design principles:
  - Stable JSON contracts, versioned (v1)
  - Errors are structured (code, message, suggestion)
  - Capability metadata helps Agents decide when to use API vs GUI fallback
  - Idempotency via request_id / step_id (Agent can retry safely)
  - No global state mutation between calls
  - Layer 0 (auth + session + rate limit) enforced at HTTP boundary
  - Self-check verifies injection succeeded (Agent can't see screen)
"""

from __future__ import annotations

import base64
import collections
import concurrent.futures
import functools
import hashlib
import inspect
import io
import json
import os
import platform as _platform
import re
import secrets
import sys
import threading
import time
import traceback
import logging
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from .keyboard_primitives import run_keyboard_chain
from socketserver import ThreadingMixIn
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

from . import tiers
from .agent import ShortcutAgent, ExecutionResult
from .auth import authenticate as _authn_fn, is_dev_mode
from . import ratelimit as _ratelimit_mod
from .selfcheck import snapshot as take_snapshot, verify as evaluate_selfcheck
from .logger import Logger
from .llm import DeepSeekEngine
from .session import store as _session_store_fn

__version__ = "1.0.0"
__all__ = ["serve", "serve_thread", "launch_daemon", "shutdown_daemon"]

_log = Logger()

# Error code constants — stable, used by Agents
E_OK            = "ok"
E_NO_MATCH      = "no_match"
E_LOW_CONF      = "low_confidence"
E_AMBIGUOUS     = "intent_ambiguous"
E_NO_PLATFORM   = "no_platform_key"
E_KEY_NO_RESP   = "key_combination_no_response"
E_INJECT_FAIL   = "inject_failed"
E_NO_APP        = "app_not_detected"
E_TIMEOUT       = "timeout"
E_EXEC_FAIL     = "exec_failed"
E_BAD_REQUEST   = "bad_request"
E_UNAUTHORIZED  = "unauthorized"
E_FORBIDDEN     = "forbidden"
E_RATE_LIMITED  = "rate_limited"
E_INTERNAL      = "internal"
E_NOT_FOUND     = "not_found"
E_NO_API        = "no_api_equivalent"
E_NO_BACKEND    = "no_image_lib"
E_PLATFORM_UNS  = "platform_unsupported"
E_NO_KEY        = "no_api_key"


def _platform_str() -> str:
    p = _platform.system().lower()
    if p.startswith("win"):
        return "windows"
    if p == "darwin":
        return "macos"
    if p == "linux":
        return "linux"
    return p


class _Handler(BaseHTTPRequestHandler):
    """Single-class HTTP handler. No external deps (stdlib only).

    Threading: we use ThreadingHTTPServer (mix-in) so concurrent Agent
    requests don't deadlock. The handler is stateless — it re-reads
    everything from each request body, so per-thread state is safe.
    """

    server_version = "SCutAgentAPI/1.0"
    agent: ShortcutAgent  # set by serve()

    def log_message(self, fmt, *args):  # silence default access log
        pass

    def _send_json(self, status: int, payload: Dict[str, Any]):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client aborted the connection (GUI switched tab / closed / timed out /
            # retried). The response is best-effort; never let this crash the handler.
            pass

    def _send_429(self, block_info: Dict[str, Any]):
        retry_after_ms = block_info.get("retry_after_ms", 1000)
        self._send_json(429, {
            "ok": False,
            "status": "failed",
            "error": {
                "code": E_RATE_LIMITED,
                "message": f"rate limited: retry after {retry_after_ms}ms",
            },
            "retry_after_ms": retry_after_ms,
        })

    # ── Layer 0: authn / ratelimit / scope ─────────────────────────

    def _authn(self) -> Optional[Dict[str, Any]]:
        auth = self.headers.get("Authorization", "")
        key = ""
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
        return _authn_fn({"Authorization": f"Bearer {key}"} if key else {})

    def _ratelimit(self, auth_ctx: Dict[str, Any], endpoint_class: str) -> Optional[Dict[str, Any]]:
        if endpoint_class == "meta":
            return None
        identity = auth_ctx.get("identity", "anon")
        if auth_ctx.get("dev_mode"):
            return None
        result = _ratelimit_mod.check(identity, endpoint_class, cost=1.0)
        if not result.get("allowed", True):
            return result
        return None

    def _require_scope(self, auth_ctx: Dict[str, Any], required: Optional[str]) -> Optional[Dict[str, str]]:
        if not required:
            return None
        scopes = auth_ctx.get("scopes", [])
        if "admin" in scopes:
            return None
        if required in scopes:
            return None
        return {
            "code": E_FORBIDDEN,
            "message": f"scope '{required}' required (caller has {scopes})",
        }

    # ── Body parsing ──────────────────────────────────────────────

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n == 0:
                return {}
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as e:
            self._send_json(400, {
                "ok": False,
                "error": {"code": E_BAD_REQUEST, "message": f"invalid json: {e}"},
            })
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    # ── GET router ────────────────────────────────────────────────

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        # Layer 0: auth (rate-limit is meta = unlimited, skip)
        auth_ctx = self._authn()
        if auth_ctx is None:
            return self._send_json(401, {
                "ok": False,
                "status": "failed",
                "error": {
                    "code": "unauthorized",
                    "message": "missing or invalid API key (Authorization: Bearer scut_xxx)",
                },
                "hint": "set NL2SHORTCUT_API_KEYS env var or create ~/.nl2shortcut/api_keys.json, or use 'nl2shortcut_dev_local' as dev key",
            })
        if path == "/v1/health":
            try:
                from nl2shortcut.vision_executor import vision_capabilities
                vision_info = vision_capabilities()
            except Exception as e:
                vision_info = {"name": "vision", "error": str(e)}
            self._send_json(200, {
                "ok": True,
                "status": "ok",
                "version": __version__,
                "agent_api": "v1",
                "role": "keyboard_action",
                "tier": "keyboard",
                "tiers_available": ["keyboard", "api", "vision"],
                "this_tier": "keyboard",
                "tiers": tiers.tier_summary()["tiers"],
                "vision": vision_info,
                "description": "Agent Keyboard Action plugin — low-latency bridge from Agent intent to keyboard/mouse actions",
                "dev_mode": auth_ctx.get("dev_mode", False),
                "identity": auth_ctx.get("identity", ""),
                "startup_self_test": STARTUP_SELF_TEST,
            })
        elif path == "/v1/self-test":
            try:
                from nl2shortcut.self_test import run_self_test
                # 端点自身不依赖外部服务器；live 探测交由 CLI --live 负责
                report = run_self_test(include_live=False)
                self._send_json(200 if report["ok"] else 500, {
                    "ok": report["ok"],
                    "self_test": report,
                })
            except Exception as e:
                self._send_json(500, {
                    "ok": False,
                    "error": {"code": "self_test_failed", "message": str(e)},
                })
        elif path == "/v1/capabilities":
            self._capabilities()
        elif path == "/v1/schema":
            self._schema()
        elif path == "/v1/shortcut":
            self._shortcut_lookup()
        elif path == "/v1/keys":
            self._keys_index()
        elif path == "/v1/session":
            self._session_list()
        elif path == "/v1/rate_limit":
            self._rate_limit_status(auth_ctx)
        elif path == "/v1/stats":
            self._stats_get(auth_ctx)
        elif path == "/v1/suggest":
            self._suggest_get()
        else:
            self._send_json(404, {"ok": False, "error": {"code": E_BAD_REQUEST, "message": f"unknown path: {path}"}})

    # ── POST router ───────────────────────────────────────────────

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        # Layer 0: auth
        auth_ctx = self._authn()
        if auth_ctx is None:
            return self._send_json(401, {
                "ok": False,
                "status": "failed",
                "error": {
                    "code": "unauthorized",
                    "message": "missing or invalid API key (Authorization: Bearer scut_xxx)",
                },
            })
        # Layer 0: rate-limit (per endpoint class)
        endpoint_class = {
            "/v1/execute":         "execute",
            "/v1/sequence":        "execute",
            "/v1/chain":           "execute",
            "/v1/run":             "execute",
            "/v1/api/execute":     "execute",
            "/v1/vision/execute":  "execute",
            "/v1/recognize":       "recognize",
            "/v1/plan":            "plan",
            "/v1/stats/reset":     "admin",
        }.get(path, "meta")
        rl_block = self._ratelimit(auth_ctx, endpoint_class)
        if rl_block is not None:
            return self._send_429(rl_block)
        # Layer 0: scope check
        required_scope = {
            "/v1/execute":         "execute",
            "/v1/sequence":        "execute",
            "/v1/chain":           "execute",
            "/v1/run":             "execute",
            "/v1/api/execute":     "execute",
            "/v1/vision/execute":  "execute",
            "/v1/recognize":       "recognize",
            "/v1/plan":            "plan",
            "/v1/session/start":   "execute",
            "/v1/session/end":     "execute",
            "/v1/stats/reset":     "admin",
        }.get(path, None)
        scope_err = self._require_scope(auth_ctx, required_scope) if required_scope else None
        if scope_err is not None:
            return self._send_json(403, {
                "ok": False,
                "status": "failed",
                "error": scope_err,
            })
        # Parse body
        body = self._read_json()
        if body is None:
            return
        # Route
        if path == "/v1/execute":
            self._execute(body, auth_ctx)
        elif path == "/v1/recognize":
            self._recognize(body)
        elif path == "/v1/sequence":
            self._sequence(body, auth_ctx)
        elif path == "/v1/chain":
            self._chain(body, auth_ctx)
        elif path == "/v1/run":
            self._run(body, auth_ctx)
        elif path == "/v1/plan":
            self._plan(body)
        elif path == "/v1/api/execute":
            self._api_execute(body, auth_ctx)
        elif path == "/v1/vision/execute":
            self._vision_execute(body, auth_ctx)
        elif path == "/v1/stats/reset":
            self._stats_reset(auth_ctx)
        elif path == "/v1/session/start":
            self._session_start(body, auth_ctx)
        elif path == "/v1/session/end":
            self._session_end(body)
        elif path == "/v1/record":
            self._record_post(body)
        else:
            self._send_json(404, {"ok": False, "error": {"code": E_BAD_REQUEST, "message": f"unknown path: {path}"}})

    def _capabilities(self):
        # List all shortcuts + their Agent metadata
        shortcuts = self.agent.list_shortcuts()
        caps = []
        for s in shortcuts:
            caps.append({
                "command":        s.command,
                "description":    s.description,
                "command_cn":     s.command_cn,
                "category":       s.category,
                "application":    s.application,
                "keys": {
                    "windows": s.windows_key,
                    "macos":   s.mac_key,
                    "linux":   s.linux_key,
                },
                "agent_metadata": _shortcut_meta(s.command),
            })
        self._send_json(200, {
            "ok": True,
            "count": len(caps),
            "shortcuts": caps,
        })

    def _schema(self):
        # Hand-rolled OpenAPI-ish schema so Agents can introspect
        self._send_json(200, {
            "ok": True,
            "openapi": "3.0.0",
            "info": {
                "title": "NL2Shortcut Agent API",
                "version": "1.0",
                "description": (
                    "Three execution tiers: keyboard (this server), api, vision. "
                    "This server handles the keyboard tier. Agents fall back to api "
                    "and vision tiers themselves when the keyboard tier reports failure."
                ),
            },
            "components": {
                "securitySchemes": {
                    "ApiKeyAuth": {
                        "type": "apiKey",
                        "in": "header",
                        "name": "Authorization",
                        "description": "Bearer token, e.g. 'Bearer scut_xxx_…'",
                    }
                },
                "schemas": {
                    "ExecutionResult": {
                        "type": "object",
                        "required": ["ok", "error", "result"],
                        "properties": {
                            "ok":      {"type": "boolean"},
                            "status":  {"type": "string", "enum": ["ok", "failed", "closed"]},
                            "step_id": {"type": "string"},
                            "error":   {
                                "type": "object",
                                "properties": {
                                    "code":    {"type": "string"},
                                    "message": {"type": "string"},
                                },
                            },
                            "result": {
                                "type": "object",
                                "properties": {
                                    "command":           {"type": "string"},
                                    "key_combination":   {"type": "string"},
                                    "platform":          {"type": "string"},
                                    "app":               {"type": "string"},
                                    "confidence":        {"type": "number"},
                                    "matched_keyword":   {"type": "string"},
                                    "execution_time_ms": {"type": "number"},
                                    "executed":          {"type": "boolean"},
                                    "stability":         {"type": "string"},
                                },
                            },
                            "alternatives":      {"type": "array", "items": {"type": "object"}},
                            "metadata":          {"type": "object"},
                            "selfcheck":         {"type": "object"},
                            "fallback_triggered":{"type": "boolean"},
                            "fallback_suggested":{"type": "string"},
                            "fallback_recommendation": {
                                "type": "object",
                                "properties": {
                                    "tier":   {"type": "string", "enum": ["keyboard", "api", "vision"]},
                                    "action": {"type": "string", "enum": ["retry", "escalate_api", "escalate_vision", "abort"]},
                                    "target": {"type": ["string", "object", "null"]},
                                    "reason": {"type": "string"},
                                },
                            },
                            "retryable":         {"type": "boolean"},
                            "role":              {"type": "string", "enum": ["keyboard_action"]},
                            "tier":              {"type": "string", "enum": ["keyboard", "api", "vision"]},
                            "session_id":        {"type": "string"},
                            "identity":          {"type": "string"},
                        },
                    },
                    "Error": {
                        "type": "object",
                        "properties": {
                            "code": {
                                "type": "string",
                                "enum": [
                                    "ok", "no_match", "low_confidence", "intent_ambiguous",
                                    "no_platform_key", "key_combination_no_response",
                                    "inject_failed", "app_not_detected", "timeout",
                                    "exec_failed", "bad_request", "unauthorized",
                                    "forbidden", "rate_limited", "internal", "not_found",
                                    "no_api_equivalent", "no_image_lib",
                                    "platform_unsupported", "no_api_key",
                                ],
                            },
                            "message": {"type": "string"},
                        },
                    },
                },
            },
            "security": [{"ApiKeyAuth": []}],
            "paths": {
                "/v1/health":          {"get": {"summary": "Liveness + version + tier info"}},
                "/v1/capabilities":    {"get": {"summary": "All shortcuts with agent_metadata"}},
                "/v1/keys":            {"get": {"summary": "Compact shortcut index for Agent context"}},
                "/v1/shortcut":        {"get": {"summary": "Lookup a single command's full record"}},
                "/v1/recognize":       {"post": {"summary": "Recognize intent only, no execution", "scope": "recognize"}},
                "/v1/execute":         {"post": {"summary": "Execute one intent (keyboard tier)", "scope": "execute"}},
                "/v1/sequence":        {"post": {"summary": "Execute a multi-step plan", "scope": "execute"}},
                "/v1/chain":           {"post": {"summary": "Execute a low-level keyboard chain with inline sleep waits", "scope": "execute"}},
                "/v1/run":             {"post": {"summary": "One sentence → recognize → decompose into ordered steps → execute serially", "scope": "execute"}},
                "/v1/plan":            {"post": {"summary": "Decompose a goal into a step plan", "scope": "plan"}},
                "/v1/api/execute":     {"post": {"summary": "Execute a command via the api tier (programmatic, no keyboard injection)", "scope": "execute"}},
                "/v1/vision/execute":  {"post": {"summary": "Capture screenshot + receipt for vision model dispatch (CogAgent / OmniParser / Computer Use)", "scope": "execute"}},
                "/v1/stats":           {"get":  {"summary": "Execution stats (per-tier / per-command / latency / error codes / fallback events)"}},
                "/v1/stats/reset":     {"post": {"summary": "Reset stats counters", "scope": "admin"}},
                "/v1/session/start":   {"post": {"summary": "Open a session", "scope": "execute"}},
                "/v1/session/end":     {"post": {"summary": "Close a session, return summary", "scope": "execute"}},
                "/v1/session":         {"get":  {"summary": "List active sessions (admin)"}},
                "/v1/rate_limit":      {"get":  {"summary": "Show caller's rate-limit status"}},
                "/v1/schema":          {"get":  {"summary": "This OpenAPI document"}},
            },
        })

    def _shortcut_lookup(self):
        qs = parse_qs(urlparse(self.path).query)
        command = (qs.get("command") or [""])[0]
        if not command:
            self._send_json(400, {
                "ok": False,
                "error": {"code": E_BAD_REQUEST, "message": "?command=<name> required"},
            })
            return
        meta = _shortcut_meta(command)
        if meta is None:
            self._send_json(404, {
                "ok": False,
                "error": {"code": E_NOT_FOUND, "message": f"no metadata for '{command}'"},
            })
            return
        # Also include the full shortcut
        shortcut = next(
            (s for s in self.agent.list_shortcuts() if s.command == command), None
        )
        out = {
            "ok": True,
            "command": command,
            "agent_metadata": meta,
        }
        if shortcut:
            out["shortcut"] = {
                "command":        shortcut.command,
                "description":    shortcut.description,
                "command_cn":     shortcut.command_cn,
                "category":       shortcut.category,
                "application":    shortcut.application,
                "keys": {
                    "windows": shortcut.windows_key,
                    "macos":   shortcut.mac_key,
                    "linux":   shortcut.linux_key,
                },
            }
        self._send_json(200, out)

    def _execute(self, body: Dict[str, Any], auth_ctx: Dict[str, Any]):
        """POST /v1/execute  {intent, dry_run?, context?, session_id?, app?, fallback_policy?, selfcheck?, smart?}

        When ``"smart": true`` → 智能管道：匹配已有工作流 → 未命中则 LLM 拆解 → 自动保存。
        Otherwise → Execute one intent via the keyboard tier. Returns ExecutionResult.
        """
        intent = body.get("intent", "").strip()
        if not intent:
            return self._send_json(400, {
                "ok": False,
                "status": "failed",
                "error": {"code": E_BAD_REQUEST, "message": "'intent' is required"},
            })
        dry_run = bool(body.get("dry_run", False))

        # ═══════════ Smart pipeline: workflow match → plan → execute → save ═══════════
        if bool(body.get("smart", False)):
            try:
                from .master import KeyboardMasterAgent
                master = KeyboardMasterAgent()
                result = master.smart_execute(
                    intent,
                    dry_run=dry_run,
                    auto_save=not body.get("no_save", False),
                )
                return self._send_json(200 if result["ok"] else 422, {
                    "ok": result["ok"],
                    "mode": "smart",
                    "pipeline": result["pipeline"],
                    "matched_workflow": result["matched_workflow"],
                    "match_confidence": result["match_confidence"],
                    "auto_saved": result["auto_saved"],
                    "auto_saved_path": result["auto_saved_path"],
                    "plan": result["plan"],
                    "steps_executed": result["steps_executed"],
                    "results": result["results"],
                    "elapsed_ms": result["elapsed_ms"],
                    "intent": result["intent"],
                    "error": result["error"],
                })
            except Exception as e:
                return self._send_json(500, {
                    "ok": False,
                    "status": "failed",
                    "error": {"code": E_INTERNAL, "message": f"smart execute failed: {e}"},
                })

        # ═══════════ Standard execution path ═══════════
        context = body.get("context") or {}
        session_id = body.get("session_id")
        app = body.get("app", "")
        fallback_policy = body.get("fallback_policy", "gui_retry")
        selfcheck_enabled = bool(body.get("selfcheck", True))
        timeout_s = float(body.get("timeout_s", 15.0))

        # Optional session binding
        sess_ctx = None
        if session_id:
            from .session import store as _session_store
            sm = _session_store()
            sess_ctx = sm.get(session_id)
            if sess_ctx is None:
                return self._send_json(404, {
                    "ok": False,
                    "status": "failed",
                    "error": {"code": E_NOT_FOUND, "message": f"session '{session_id}' not found"},
                })
            sess_ctx.touch()

        # Pre-snapshot for selfcheck: MUST be taken BEFORE execution.
        # Previously snap_before was taken after _execute_with_timeout(),
        # which meant both pre and post snapshots saw the post-execution state
        # (e.g. clipboard already modified by Ctrl+C), causing false 422 errors.
        snap_before = None
        if selfcheck_enabled and not dry_run:
            try:
                # Resolve intent first to know which check to run (clipboard/window/mtime)
                intent_result = self.agent.recognize_intent(intent)
                snap_before = take_snapshot(intent_result.command, app_name=app)
            except Exception:
                pass

        # Resolve + execute with timeout
        resp = self._execute_with_timeout(intent, dry_run, timeout_s)

        # Self-check (if enabled and actually executed)
        if snap_before is not None and resp.success:
            try:
                check = evaluate_selfcheck(resp.command, snap_before, app_name=app)
                resp.selfcheck = check.to_dict() if hasattr(check, "to_dict") else check
                # ok=None means "no check available" (e.g. noop) — not a failure
                if check.get("ok") is False:
                    # Distinguish "no selection" (empty clipboard) from "injection failed"
                    check_desc = check.get('description', 'unknown')
                    if (check.get("check") == "clipboard"
                            and snap_before.get("value") == ""
                            and check.get("post") == ""):
                        # clipboard was empty before AND after: likely no text selected
                        resp.error = f"self-check: clipboard empty (no selection?) — {check_desc}"
                    else:
                        resp.success = False
                        resp.error = f"self-check failed: {check_desc}"
            except Exception as e:
                logging.debug(f"selfcheck error (non-fatal): {e}")

        # Build response
        out = {
            "ok": resp.success,
            "status": "ok" if resp.success else "failed",
            "step_id": body.get("step_id"),
            "error": {"code": "ok" if resp.success else "exec_failed",
                      "message": "ok" if resp.success else (resp.error or "exec failed")},
            "result": {
                "command":           resp.command,
                "key_combination":   resp.key_combination,
                "platform":          resp.platform,
                "app":               app or "",
                "confidence":        getattr(resp, "confidence", 0.0) or 0.0,
                "matched_keyword":   getattr(resp, "matched_keyword", "") or "",
                "execution_time_ms": resp.processing_time * 1000.0,
                "executed":          not dry_run and resp.success,
                "stability":         getattr(resp, "stability", "unknown") or "unknown",
                "mode":              getattr(resp, "mode", "") or "",
                "composite_plan":   (
                    resp.composite_plan.to_dict()
                    if getattr(resp, "composite_plan", None) else None
                ),
            },
            "alternatives":   [],
            "metadata":       _shortcut_meta(resp.command) or {},
            "fallback_triggered": False,
            "fallback_suggested": "gui_retry",
            "retryable":      not resp.success,
            "role":           "keyboard_action",
            "tier":           "keyboard",
            "context_echo":   context,
            "fallback_policy": fallback_policy,
            "request_id":     body.get("step_id"),
        }
        # Add tier recommendation
        meta = _shortcut_meta(resp.command)
        if meta and meta.get("api_equivalent"):
            out["tier_recommended"] = "api"
            out["tier_hint"] = (
                f"this command has api_equivalent={meta.get('api_equivalent')!r}; "
                f"consider calling it via the api tier in hot paths"
            )
        elif meta and meta.get("stability") == "low":
            out["tier_recommended"] = "vision"
            out["tier_hint"] = (
                "this is a risky command (stability=low); "
                "consider using vision tier with explicit human confirmation"
            )
        else:
            out["tier_recommended"] = "keyboard"
        out["tier_used"] = "keyboard"

        # Add fallback_recommendation on failure
        if not resp.success:
            ec = out["error"]["code"]
            from nl2shortcut.tiers import recommend_fallback as decide_fallback
            fb = decide_fallback(failed_error_code=ec, command_meta=meta)
            out["fallback_recommendation"] = fb
            out["fallback_triggered"] = True
            out["fallback_suggested"] = (
                f"{fb['action']}:{fb['target']}" if fb.get("target") else fb["action"]
            )

        # Always echo identity (for debugging)
        out["identity"] = auth_ctx.get("identity", "")

        # Record stats
        try:
            from nl2shortcut.stats import get_stats
            latency = float(out["result"]["execution_time_ms"])
            get_stats().record_request(
                tier="keyboard",
                command=out["result"]["command"] or intent,
                latency_ms=latency,
                success=out["ok"],
                error_code=out["error"]["code"],
            )
            fbrec = out.get("fallback_recommendation")
            if fbrec:
                get_stats().record_fallback(
                    from_tier="keyboard",
                    to_tier=fbrec.get("tier", "unknown"),
                    intent=intent,
                    error_code=out["error"]["code"],
                    target=str(fbrec.get("target", "")),
                )
        except Exception:
            pass

        # Auto-save every execution as a workflow
        try:
            self.agent.auto_save_workflow(resp, intent, dry_run=dry_run)
        except Exception:
            pass

        self._send_json(200 if out["ok"] else 422, out)

    def _execute_with_timeout(self, intent: str, dry_run: bool, timeout_s: float):
        """Run agent.execute() with timeout. Returns ExecutionResult."""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = executor.submit(self.agent.execute, intent, dry_run=dry_run)
            return fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            return _timeout_result(intent)
        except Exception as e:
            from .agent import ExecutionResult
            return ExecutionResult(
                success=False, command="", key_combination="", platform="",
                error=f"agent.execute raised: {e}", processing_time=0.0, dry_run=dry_run,
            )
        finally:
            executor.shutdown(wait=False)

    def _recognize(self, body: Dict[str, Any]):
        """POST /v1/recognize  {intent}

        Intent recognition only. No execution, no side effects.
        """
        intent = body.get("intent", "").strip()
        if not intent:
            return self._send_json(400, {
                "ok": False,
                "error": {"code": E_BAD_REQUEST, "message": "'intent' is required"},
            })
        try:
            matched = self.agent.recognize_intent(intent)
        except Exception as e:
            return self._send_json(500, {
                "ok": False,
                "error": {"code": E_INTERNAL, "message": f"recognize failed: {e}"},
            })
        if matched is None:
            return self._send_json(422, {
                "ok": False,
                "status": "failed",
                "error": {"code": E_NO_MATCH, "message": f"no shortcut matched intent '{intent}'"},
                "intent": intent,
            })
        # IntentResult dataclass -> dict for JSON response
        out = {
            "ok": True,
            "intent": intent,
            "intent_data": {
                "command":         getattr(matched, "command", ""),
                "confidence":      getattr(matched, "confidence", 0.0),
                "matched_keyword": getattr(matched, "matched_keyword", ""),
            },
            "alternatives": [
                {
                    "command":    getattr(a, "command", ""),
                    "confidence": getattr(a, "confidence", 0.0),
                }
                for a in getattr(matched, "alternatives", [])
            ],
            "command": getattr(matched, "command", ""),
        }
        # Tier recommendation based on the matched command
        cmd = getattr(matched, "command", "")
        if cmd:
            meta = _shortcut_meta(cmd)
            if meta and meta.get("api_equivalent"):
                out["tier_recommended"] = "api"
            elif meta and meta.get("stability") == "low":
                out["tier_recommended"] = "vision"
            else:
                out["tier_recommended"] = "keyboard"
        self._send_json(200, out)

    def _keys_index(self):
        """GET /v1/keys

        Returns a compact index of all commands + their key combos.
        Designed for Agent context budget: 51 commands fit in ~1.5KB.
        Use this to inject the full keyboard map into the Agent prompt.
        """
        shortcuts = self.agent.list_shortcuts()
        platform = _platform_str()
        idx = []
        for s in shortcuts:
            idx.append({
                "command": s.command,
                "command_cn": getattr(s, "command_cn", "") or "",
                "keys": s.get_key(platform),
            })
        self._send_json(200, {
            "ok": True,
            "platform": platform,
            "count": len(idx),
            "index": idx,
        })

    def _session_start(self, body: Dict[str, Any], auth_ctx: Dict[str, Any]):
        """POST /v1/session/start  {app, platform?, identity?}

        Open a session that subsequent /v1/execute calls can bind to via
        session_id. Returns a session_id and any captured context.
        """
        try:
            from .session import store as _session_store
            sm = _session_store()
            sess = sm.start(
                identity=auth_ctx.get("identity", "anon"),
                app=body.get("app", ""),
                platform=body.get("platform", ""),
            )
            return self._send_json(200, {
                "ok":         True,
                "session_id": sess.session_id,
                "identity":   sess.identity,
                "app":        sess.context.get("app", ""),
                "platform":   sess.context.get("platform", ""),
                "created_at": sess.created_at,
            })
        except Exception as e:
            return self._send_json(500, {
                "ok": False,
                "error": {"code": E_INTERNAL, "message": f"session start failed: {e}"},
            })

    def _session_end(self, body: Dict[str, Any]):
        """POST /v1/session/end  {session_id}

        Close a session. Subsequent calls with this session_id return 404.
        """
        sid = body.get("session_id", "").strip()
        if not sid:
            return self._send_json(400, {
                "ok": False,
                "error": {"code": E_BAD_REQUEST, "message": "'session_id' is required"},
            })
        try:
            from .session import store as _session_store
            sm = _session_store()
            summary = sm.end(sid)
            if summary is None:
                return self._send_json(404, {
                    "ok": False,
                    "error": {"code": E_NOT_FOUND, "message": f"session '{sid}' not found or already ended"},
                })
            return self._send_json(200, {
                "ok": True,
                **summary,
            })
        except Exception as e:
            return self._send_json(500, {
                "ok": False,
                "error": {"code": E_INTERNAL, "message": f"session end failed: {e}"},
            })

    def _suggest_get(self):
        """GET /v1/suggest?app=X&goal=Y

        Return smart key suggestions from OperationMemory (ML-based) with
        hardcoded keyword fallback.
        """
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        app = (qs.get("app") or [""])[0]
        goal = (qs.get("goal") or [""])[0]

        # Auto-detect app if not provided
        if not app:
            try:
                ctx = self.agent.get_context()
                app = ctx.app_name or ""
            except Exception:
                app = ""

        from .operation_memory import OperationMemory
        memory = OperationMemory()
        suggestion = memory.get_suggestion(goal=goal, app=app)

        self._send_json(200, {
            "ok": True,
            "app": app,
            "goal": goal,
            "suggestion": suggestion,
            "has_suggestion": bool(suggestion),
        })

    def _record_post(self, body: Dict[str, Any]):
        """POST /v1/record  {app, action_type?, action_detail, user_goal?}

        Record an operation into the OperationMemory database so learned
        patterns improve over time.
        """
        app = body.get("app", "").strip()
        action_type = body.get("action_type", "shortcut").strip()
        action_detail = body.get("action_detail", "").strip()
        user_goal = body.get("user_goal", "").strip()

        if not app or not action_detail:
            return self._send_json(400, {
                "ok": False,
                "error": {
                    "code": E_BAD_REQUEST,
                    "message": "app and action_detail are required",
                },
            })

        from .operation_memory import OperationMemory
        memory = OperationMemory()
        record_id = memory.record(
            app=app,
            action_type=action_type,
            action_detail=action_detail,
            user_goal=user_goal,
        )

        self._send_json(200, {
            "ok": True,
            "record_id": record_id,
            "message": f"Recorded: [{app}] {action_detail}",
        })

    def _plan(self, body: Dict[str, Any]):
        """POST /v1/plan  {goal, context?}

        Decompose a natural-language goal into a step plan that the
        Agent can then POST to /v1/sequence.

        Uses GoalPlanner (LLM-powered) instead of heuristic regex splitting,
        so it handles sentences like "复制这段文字然后粘贴到记事本" correctly.
        Falls back to heuristic splitting when LLM is unavailable.
        """
        goal = body.get("goal", "").strip()
        if not goal:
            return self._send_json(400, {
                "ok": False,
                "error": {"code": E_BAD_REQUEST, "message": "'goal' is required"},
            })
        context = body.get("context") or {}

        # ── 优先走 GoalPlanner（LLM 智能分解）──
        try:
            from .planner import GoalPlanner
            planner = GoalPlanner()
            plan = planner.plan(goal, context=context)
            if plan and plan.steps:
                plan_steps = []
                for i, step in enumerate(plan.steps):
                    # Derive a usable intent string for /v1/execute or /v1/sequence
                    if step.action == "shortcut":
                        derived_intent = step.description
                    elif step.action == "type":
                        derived_intent = f"输入 {step.text}" if step.text else step.description
                    elif step.action == "composite":
                        derived_intent = step.composite_hint or step.description
                    elif step.action == "shell":
                        derived_intent = f"执行 {step.command}" if step.command else step.description
                    elif step.action == "tab":
                        derived_intent = f"按 {step.direction} {step.n}次" if step.n > 1 else f"按 {step.direction}"
                    elif step.action == "wait":
                        derived_intent = step.description
                    else:
                        derived_intent = step.description

                    plan_steps.append({
                        "step":            i + 1,
                        "intent":          derived_intent,
                        "description":     step.description,
                        "action":          step.action,
                        "key_combination": step.key_combination or "",
                        "command":         step.command or "",
                        "text":            step.text or "",
                        "composite_hint":  step.composite_hint or "",
                        "n":               step.n,
                        "direction":       step.direction,
                        "wait_ms":         step.wait_ms,
                        "confidence":      step.confidence,
                        "reasoning":       step.reasoning,
                        "role":            "keyboard_action",
                        "tier":            "keyboard",
                    })
                return self._send_json(200, {
                    "ok":     True,
                    "status": "ok",
                    "goal":   goal,
                    "source": "goal_planner_v1",
                    "plan":   plan_steps,
                    "role":   "keyboard_action",
                    "tier":   "keyboard",
                    "next_step": "POST /v1/sequence with body.steps = this plan",
                })
        except Exception as e:
            # GoalPlanner failed — fall back to heuristic
            pass

        # ── 降级：启发式正则切分 + 本地意图识别 ──
        import re as _re
        segments = _re.split(r"[，。.;；\n]+|然后|接着|之后|再|and then|then|next", goal)
        segments = [s.strip() for s in segments if s.strip()]
        if not segments:
            segments = [goal]

        plan_steps = []
        all_ok = True
        for i, seg in enumerate(segments):
            matched = self.agent.recognize_intent(seg)
            if matched is not None and matched.command:
                if hasattr(matched, "to_dict"):
                    md = matched.to_dict()
                else:
                    md = {
                        "command": getattr(matched, "command", ""),
                        "confidence": getattr(matched, "confidence", 0.0),
                    }
                plan_steps.append({
                    "step":       i + 1,
                    "intent":     seg,
                    "command":    md.get("command", ""),
                    "app":        context.get("app", ""),
                    "dry_run":    False,
                    "confidence": md.get("confidence", 0.0),
                })
            else:
                all_ok = False
                plan_steps.append({
                    "step":    i + 1,
                    "intent":  seg,
                    "command": None,
                    "error":   f"could not recognize: '{seg}'",
                })
        source = "heuristic_v1"
        return self._send_json(200 if all_ok else 422, {
            "ok":      all_ok,
            "status":  "ok" if all_ok else "partial",
            "goal":    goal,
            "source":  source,
            "plan":    plan_steps,
            "role":    "keyboard_action",
            "tier":    "keyboard",
            "next_step": "POST /v1/sequence with body.steps = this plan",
        })

    # ── NEW (Batch 4): api tier execution ─────────────────────────

    def _api_execute(self, body: Dict[str, Any], auth_ctx: Dict[str, Any]):
        """POST /v1/api/execute  {intent, context, app}

        Execute the api tier for a command (the programmatic equivalent
        of a keyboard shortcut). This is what the Agent should call when
        the keyboard tier reports failure and the fallback_recommendation
        says tier='api'.

        Behavior:
          - If the command has api_equivalent='os.<x>' and <x> is implemented
            in api_executor.py, run it directly (no keyboard injection).
          - If api_equivalent is a VS Code command ({"vscode_command":...}),
            return a receipt that the Agent can use to dispatch the call
            via VS Code IPC.
          - If api_equivalent is None, return 422 no_api_equivalent and
            tell the Agent to fall back to the vision tier.
        """
        t0 = time.time()
        intent = body.get("intent", "").strip()
        if not intent:
            return self._send_json(400, {
                "ok": False,
                "status": "failed",
                "error": {"code": E_BAD_REQUEST, "message": "'intent' is required"},
            })
        context = body.get("context") or {}
        app = body.get("app", "")
        session_id = body.get("session_id")
        dry_run = bool(body.get("dry_run", False))

        from nl2shortcut.api_executor import execute as api_execute
        meta = _shortcut_meta(intent); result = api_execute(meta, context=context)

        resp = {
            "ok":       result.success,
            "status":   "ok" if result.success else "failed",
            "intent":   intent,
            "command":  intent,  # for symmetry with /v1/execute
            "tier":     "api",
            "result": {
                "ok":       result.success,
                "action":       result.action,
                "message":      result.message,
                "duration_ms":  result.duration_ms,
                "platform":     result.platform,
                "data":         result.data,
                "error_code":   result.error_code,
            },
            "role":             "keyboard_action",
            "tier_used":        "api",
            "tier_recommended": "api",
            "identity":         auth_ctx.get("identity", "unknown"),
            "error":            {"code": "ok" if result.success else (result.error_code or "exec_failed"),
                                "message":       result.message},
            "api_equivalent":   result.action,
        }
        if not result.success:
            # Recommend next tier
            ec = result.error_code
            from nl2shortcut.tiers import recommend_fallback as decide_fallback
            meta = _shortcut_meta(intent)
            fb = decide_fallback(failed_error_code=ec, command_meta=meta)
            resp["fallback_recommendation"] = fb
            resp["fallback_triggered"] = True
            resp["fallback_suggested"] = (
                f"{fb['action']}:{fb['target']}" if fb.get("target") else fb["action"]
            )
            resp["retryable"] = True
        if session_id:
            from .session import store as _session_store
            sm = _session_store()
            sess = sm.get(session_id)
            if sess is not None:
                sess.touch()
                sess.add_history({
                    "intent": intent,
                    "tier": "api",
                }, success=result.success)
            resp["session_id"] = session_id

        # Record stats
        try:
            from nl2shortcut.stats import get_stats
            latency = float(resp["result"]["duration_ms"])
            get_stats().record_request(
                tier="api",
                command=resp["command"] or intent,
                latency_ms=latency,
                success=result.success,
                error_code=resp["error"]["code"],
            )
        except Exception:
            pass

        self._send_json(200 if result.success else 422, resp)


# ── Per-shortcut Agent metadata ───────────────────────────────────────
#
# This is the metadata Agents consult to decide:
#   - tier (keyboard / api / vision)
#   - fallback (when keyboard tier fails)
#   - stability (high / low / unknown)
#
# Format: command -> {api_equivalent, gui_fallback, stability, scheme}
#
# Only the well-known shortcuts are pre-annotated; the rest default to
# "no programmatic API, no GUI fallback recipe known, stability unknown".

_AGENT_META: Dict[str, Dict[str, str]] = {
    # VS Code
    "save": {
        "stability": "high",
        "api_equivalent": json.dumps({
            "vscode_command": "workbench.action.files.save",
            "scheme": "vscode",
        }),
        "gui_fallback": "File menu → Save",
    },
    "save_all": {
        "stability": "high",
        "api_equivalent": json.dumps({
            "vscode_command": "workbench.action.files.saveAll",
            "scheme": "vscode",
        }),
        "gui_fallback": "File menu → Save All",
    },
    "open_file":  {"stability": "high", "api_equivalent": "os.file.open",      "gui_fallback": "File menu → Open"},
    "new_file":   {"stability": "high", "api_equivalent": "os.file.create",    "gui_fallback": "File menu → New File"},
    "close_file": {"stability": "high", "api_equivalent": "os.file.close",     "gui_fallback": "Ctrl+W on tab"},
    # Browser
    "new_tab":      {"stability": "high", "api_equivalent": "os.browser.newTab",   "gui_fallback": "Ctrl+T"},
    "close_tab":    {"stability": "high", "api_equivalent": "os.browser.closeTab", "gui_fallback": "Ctrl+W"},
    "reload":       {"stability": "high", "api_equivalent": "os.browser.reload",  "gui_fallback": "F5"},
    "go_back":      {"stability": "high", "api_equivalent": "os.browser.back",     "gui_fallback": "Alt+Left"},
    "go_forward":   {"stability": "high", "api_equivalent": "os.browser.forward",  "gui_fallback": "Alt+Right"},
    # Clipboard / edit
    "copy":   {"stability": "high", "api_equivalent": "os.clipboard.copy",  "gui_fallback": "Right-click → Copy"},
    "paste":  {"stability": "high", "api_equivalent": "os.clipboard.paste", "gui_fallback": "Right-click → Paste"},
    "cut":    {"stability": "high", "api_equivalent": "os.clipboard.cut",   "gui_fallback": "Right-click → Cut"},
    "undo":   {"stability": "high", "api_equivalent": "os.app.undo",         "gui_fallback": "Edit menu → Undo"},
    "redo":   {"stability": "high", "api_equivalent": "os.app.redo",         "gui_fallback": "Edit menu → Redo"},
    "select_all": {"stability": "high", "api_equivalent": "os.app.selectAll", "gui_fallback": "Edit menu → Select All"},
    # Risky
    "delete_file":  {"stability": "low",  "api_equivalent": None, "gui_fallback": "Select file → Right-click → Delete → Confirm"},
    "force_quit":   {"stability": "low",  "api_equivalent": None, "gui_fallback": "Task Manager → End task"},
    "format_disk":  {"stability": "low",  "api_equivalent": None, "gui_fallback": "DO NOT EXECUTE without explicit user confirmation"},
    "screenshot":   {"stability": "high", "api_equivalent": "os.screenshot",   "gui_fallback": "PrintScreen key"},
}


def _shortcut_meta(command: str) -> Optional[Dict[str, str]]:
    """Return Agent metadata for a command, or None if not found."""
    if not command:
        return None
    return _AGENT_META.get(command, {
        "api_equivalent": None,
        "gui_fallback":   None,
        "stability":      "unknown",
    })


# ── NEW (Batch 5+): multi-step sequence execution ───────────
# Defined as a free function so we don't need to touch _Handler's
# indent level. Monkey-patched on below.

def _sequence(self, body: Dict[str, Any], auth_ctx: Dict[str, Any]):
    """POST /v1/sequence  {steps: [...], stop_on_error?: bool}

    Each step is the same payload accepted by /v1/execute:
        {intent, context?, dry_run?, app?}

    Returns aggregated results keyed by 0-based step index.
    """
    steps = body.get("steps") or []
    stop_on_error = bool(body.get("stop_on_error", True))
    if not isinstance(steps, list) or not steps:
        return self._send_json(400, {"ok": False, "error": {"code": "bad_request", "message": "steps must be non-empty list"}})

    t0 = time.time()
    results = []
    all_ok = True
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            results.append({"index": i, "ok": False, "error": {"code": "bad_step", "message": "step must be object"}})
            all_ok = False
            if stop_on_error:
                break
            continue
        # Each step is a /v1/execute payload
        step_body = {
            "intent":  step.get("intent", ""),
            "context": step.get("context", {}) or {},
            "dry_run": step.get("dry_run", False),
            "app":     step.get("app", ""),
        }
        # Capture response by writing into a temp buffer (a fresh BaseHTTPResponse).
        captured = {"status": None, "body": None}

        class _CaptureResp:
            def __init__(self):
                self._buf = []
            def write(self, data):
                self._buf.append(data)
                return len(data)

        orig_send_json = self._send_json
        def _capture(status, payload):
            captured["status"] = status
            captured["body"] = payload
            # don't actually send
            return None

        try:
            self._send_json = _capture
            self._execute(step_body, auth_ctx)
        except Exception as e:
            captured["status"] = 500
            captured["body"] = {"ok": False, "error": {"code": "exception", "message": str(e)}}
        finally:
            self._send_json = orig_send_json

        ok = bool((captured["body"] or {}).get("ok"))
        results.append({
            "index":     i,
            "ok":        ok,
            "intent":    step_body["intent"],
            "status":    captured["status"],
            "result":    (captured["body"] or {}).get("result"),
            "error":     (captured["body"] or {}).get("error"),
            "fallback":  (captured["body"] or {}).get("fallback_recommendation"),
        })
        if not ok:
            all_ok = False
            if stop_on_error:
                break

    self._send_json(200 if all_ok else 207, {
        "ok":         all_ok,
        "steps":      results,
        "executed":   sum(1 for r in results if r["ok"]),
        "total":      len(results),
        "stopped_at": None if all_ok else (results[-1]["index"] if not results[-1]["ok"] else None),
        "elapsed_ms": round((time.time() - t0) * 1000, 2),
    })


def _chain(self, body: Dict[str, Any], auth_ctx: Dict[str, Any]):
    """POST /v1/chain  {keys: [...], variables?: {...}, dry_run?: bool,
                        auto_search_wait?: bool, search_wait_ms?: int}

    按序执行一组底层键盘链步骤，支持内联 ``["sleep", ms]`` 确定性等待。

    用于解决资源管理器等目标应用因加载延迟，导致后续按键（如 ``Ctrl+A``
    全选、右键菜单）落空、未真正选中文件的竞态。与 /v1/execute 不同，
    这里直接驱动按键原语，不经过意图识别。

    步格式（详见 ``nl2shortcut.keyboard_primitives.run_keyboard_chain``）：
      - ``["Alt+D"]`` / ``"Ctrl+C"``  按键组合（支持 ``{变量}`` 替换）
      - ``["sleep", 300]``           暂停 300ms，等待 UI 稳定
      - ``["type", "text"]``         逐字符输入文本

    auto_search_wait / search_wait_ms：启用「搜索触发后自动补间隔」（默认开启，
    详见 ``run_keyboard_chain`` 文档）。即便调用方忘了写 sleep，搜索后也保证有
    间隔，且不会与已有的显式 ``["sleep", ...]`` 叠加。传 ``auto_search_wait:false``
    可关闭。
    """
    keys = body.get("keys")
    if not isinstance(keys, list) or not keys:
        return self._send_json(400, {
            "ok": False,
            "error": {"code": "bad_request", "message": "keys must be a non-empty list"},
        })
    variables = body.get("variables")
    if not isinstance(variables, dict):
        variables = {}
    dry_run = bool(body.get("dry_run", False))
    auto_search_wait = bool(body.get("auto_search_wait", True))
    search_wait_ms = body.get("search_wait_ms")
    if search_wait_ms is not None:
        try:
            search_wait_ms = max(0, int(search_wait_ms))
        except (TypeError, ValueError):
            search_wait_ms = None
    t0 = time.time()
    try:
        steps = run_keyboard_chain(
            keys, variables=variables, dry_run=dry_run,
            auto_search_wait=auto_search_wait, search_wait_ms=search_wait_ms)
    except Exception as exc:
        return self._send_json(500, {
            "ok": False,
            "error": {"code": "exception", "message": str(exc)},
        })
    ok = all(bool(s.get("success")) for s in steps)
    self._send_json(200 if ok else 207, {
        "ok": ok,
        "tier": "keyboard",
        "steps": steps,
        "executed": sum(1 for s in steps if s.get("success")),
        "total": len(steps),
        "dry_run": dry_run,
        "elapsed_ms": round((time.time() - t0) * 1000, 2),
    })


def _run(self, body: Dict[str, Any], auth_ctx: Dict[str, Any]):
    """POST /v1/run  {goal, dry_run?: bool, context?: dict}

    一句话 → 识别层拆成有序步骤 → 执行器一步步串行跑。

    统一入口，把 NL2Shortcut 的「识别 → 分解 → 串行执行」管线做成端到端可用：

      1. 确定性识别层（IntentEngine）：命中已知复合意图（如资源管理器搜索
         /复制/移动，含 1500ms 等待的键盘链）或高置信单快捷键时，直接走对应
         执行器串行跑。
      2. 自由句式 / 未命中：交给 GoalPlanner（LLM，或离线 fallback）分解成
         有序 PlanStep 列表，再由 ``execute_plan`` 一步步串行执行。

    响应显式返回「分解出的有序步骤」与「每步串行执行结果」，便于观察整条管线。
    """
    goal = (body.get("goal") or body.get("intent") or "").strip()
    if not goal:
        return self._send_json(400, {
            "ok": False,
            "error": {"code": "bad_request", "message": "'goal' is required"},
        })
    dry_run = bool(body.get("dry_run", False))
    context = body.get("context") or {}

    agent = self.agent
    t0 = time.time()
    try:
        intent_result = agent.recognize_intent(goal)
    except Exception as exc:
        return self._send_json(500, {
            "ok": False,
            "error": {"code": "exception", "message": str(exc)},
        })

    cmd = getattr(intent_result, "command", None)
    composite_plan = getattr(intent_result, "composite_plan", None)

    # ── 通道 1：已知复合意图（资源管理器搜索/复制/移动等，含 1500ms 等待）──
    if composite_plan is not None:
        # 复合操作（如：把报告复制文件到桌面）→ 识别层已拆成有序步骤，
        # 执行器（CompositeExecutor）按 steps 顺序一步步串行执行，这里逐条回显。
        from .composites import CompositeExecutor
        executor = CompositeExecutor(adapter=agent.adapter)
        step_results = executor.execute(composite_plan, dry_run=dry_run)
        steps = [s.to_dict() for s in composite_plan.steps]
        ok_all = all(r.get("success", False) for r in step_results)
        results_list = [{
            "step": i,
            "action": r.get("kind") or r.get("action") or "composite",
            "success": bool(r.get("success", False)),
            "message": r.get("message") or "executed",
        } for i, r in enumerate(step_results)]
        return self._send_json(200 if ok_all else 422, {
            "ok": ok_all,
            "goal": goal,
            "source": "intent",
            "mode": "composite",
            "steps": steps,
            "results": results_list,
            "steps_executed": len(step_results),
            "all_success": ok_all,
            "elapsed_ms": round((time.time() - t0) * 1000, 2),
        })

    # ── 通道 2：已知单快捷键 ──
    if cmd and cmd not in (None, "", "unknown", "__unknown__"):
        result = agent.execute(goal, dry_run=dry_run)
        steps = [{
            "step_id": 1,
            "description": goal,
            "action": "shortcut",
            "key_combination": result.key_combination or "",
        }]
        return self._send_json(200 if result.success else 422, {
            "ok": bool(result.success),
            "goal": goal,
            "source": "intent",
            "mode": result.mode,
            "steps": steps,
            "results": [{
                "step": 0,
                "action": "shortcut",
                "success": bool(result.success),
                "message": result.error or (result.key_combination or "executed"),
            }],
            "elapsed_ms": round((time.time() - t0) * 1000, 2),
        })

    # ── 通道 3：自由句式 → GoalPlanner 分解成有序步骤 → 串行执行 ──
    from .planner import GoalPlanner
    try:
        planner = GoalPlanner()
        plan = planner.plan(goal, context=context)
        exec_results = planner.execute_plan(plan, dry_run=dry_run)
    except Exception as exc:
        return self._send_json(500, {
            "ok": False,
            "error": {"code": "exception", "message": str(exc)},
        })

    all_ok = all(bool(r.success) for r in exec_results)
    return self._send_json(200 if all_ok else 422, {
        "ok": all_ok,
        "goal": goal,
        "source": plan.source,
        "mode": "goal_planner",
        "steps": plan.to_dict().get("steps", []),
        "results": [{
            "success": bool(r.success),
            "intent": r.intent,
            "command": r.command,
            "mode": r.mode,
            "error": r.error,
            "key_combination": r.key_combination,
        } for r in exec_results],
        "elapsed_ms": round((time.time() - t0) * 1000, 2),
    })


# ── Vision tier + stats endpoint impls (defined outside _Handler) ─────
#
# Why outside the class: keeps the diff minimal and avoids indentation
# issues. These are monkey-patched onto _Handler immediately after the
# class is defined, so route dispatch (do_GET / do_POST) can call them
# just like any other method.

def _vision_execute_impl(self, body, auth_ctx):
    """POST /v1/vision/execute  {intent, action?, app?, context?}

    Last-resort tier. NL2Shortcut captures a screenshot; the calling Agent
    is expected to dispatch it to a vision model (CogAgent /
    OmniParser / Claude Computer Use) for interpretation.

    action ∈ {screenshot, find, click, ocr}  (default: screenshot)
    context.region = "x,y,w,h"  (optional)
    context.encode_b64 = true    (default: true)
    """
    t0 = time.time()
    from nl2shortcut.vision_executor import (
        vision_screenshot, vision_find, vision_click, vision_ocr,
    )
    intent = (body.get("intent") or "").strip()
    if not intent:
        return self._send_json(400, {
            "ok": False,
            "status": "failed",
            "error": {"code": E_BAD_REQUEST, "message": "'intent' is required"},
        })
    action = (body.get("action") or "").strip().lower()
    app = (body.get("app") or "").strip()
    ctx = body.get("context") or {}
    region = None
    if "region" in ctx and isinstance(ctx["region"], (list, tuple)) and len(ctx["region"]) == 4:
        try:
            region = (int(ctx["region"][0]), int(ctx["region"][1]),
                      int(ctx["region"][2]), int(ctx["region"][3]))
        except Exception:
            region = None
    encode_b64 = bool(ctx.get("encode_b64", True))

    if action in ("screenshot", ""):
        result = vision_screenshot(intent=intent, app=app, region=region, encode_b64=encode_b64)
    elif action == "find":
        result = vision_find(intent=intent, app=app, encode_b64=encode_b64)
    elif action == "click":
        result = vision_click(intent=intent, app=app, encode_b64=encode_b64)
    elif action == "ocr":
        result = vision_ocr(intent=intent or "read visible text", app=app, encode_b64=encode_b64)
    else:
        return self._send_json(400, {
            "ok": False,
            "status": "failed",
            "error": {"code": E_BAD_REQUEST,
                      "message": f"unknown action '{action}'; use screenshot|find|click|ocr"},
        })

    latency_ms = (time.time() - t0) * 1000

    try:
        from nl2shortcut.stats import get_stats
        get_stats().record_request(
            tier="vision",
            command=result.action,
            latency_ms=latency_ms,
            success=result.ok,
            error_code=result.error_code,
        )
        if not result.ok:
            get_stats().record_fallback(
                from_tier="vision",
                to_tier="human",
                intent=intent,
                error_code=result.error_code,
                target="escalate",
            )
    except Exception:
        pass

    payload = result.to_dict()
    payload.update({
        "status":      "ok" if result.ok else "failed",
        "role":        "keyboard_action",
        "tier":        "vision",
        "tier_used":   "vision",
        "tier_recommended": "vision",
        "identity":    auth_ctx.get("identity", "unknown"),
        "fallback_suggested": payload.get("data", {}).get(
            "fallback_suggested", "escalate to human"
        ),
        "fallback_triggered": not result.ok,
        "retryable":   not result.ok,
    })
    status_code = 200 if result.ok else 422
    return self._send_json(status_code, payload)


def _stats_get_impl(self, auth_ctx):
    """GET /v1/stats  — execution statistics (per-tier, per-command, latency, errors)."""
    from nl2shortcut.stats import get_stats
    summary = get_stats().summary()
    summary["identity"] = auth_ctx.get("identity", "unknown")
    summary["scopes"] = auth_ctx.get("scopes", [])
    return self._send_json(200, summary)


def _stats_reset_impl(self, auth_ctx):
    """POST /v1/stats/reset  — reset all counters (requires 'admin' scope)."""
    if "admin" not in auth_ctx.get("scopes", []):
        return self._send_json(403, {
            "ok": False,
            "status": "failed",
            "error": {"code": E_FORBIDDEN,
                      "message": "admin scope required to reset stats"},
        })
    from nl2shortcut.stats import get_stats
    get_stats().reset()
    return self._send_json(200, {
        "ok": True,
        "status": "ok",
        "message": "stats reset",
        "reset_by": auth_ctx.get("identity", "unknown"),
    })


def _rate_limit_status_impl(self, auth_ctx):
    """GET /v1/rate_limit  — show caller's rate-limit status (per endpoint class)."""
    from . import ratelimit as _rl
    out = {
        "ok":       True,
        "identity": auth_ctx.get("identity", "unknown"),
        "limits":   _rl.status(auth_ctx.get("identity", "anon")),
    }
    return self._send_json(200, out)


# Monkey-patch vision + stats + rate_limit methods onto _Handler so
# do_GET/do_POST can call them as if they were defined inside the class.
_Handler._vision_execute     = _vision_execute_impl
_Handler._stats_get          = _stats_get_impl
_Handler._stats_reset        = _stats_reset_impl
_Handler._rate_limit_status  = _rate_limit_status_impl
_Handler._sequence           = _sequence
_Handler._chain              = _chain
_Handler._run                = _run


# ── Server lifecycle ──────────────────────────────────────────────────

# 启动自检结果快照（供 /v1/health 与外部观测）
STARTUP_SELF_TEST = None


def _run_startup_self_test(host: str, port: int) -> None:
    """服务器启动时跑一次内置自检，输出健康快照并存日志。

    失败不会阻止服务器启动（自检失败 ≠ 服务不可用）。
    """
    global STARTUP_SELF_TEST
    try:
        from .self_test import run_self_test
        st = run_self_test(include_live=False)
        STARTUP_SELF_TEST = st
        tag = "[PASS]" if st["ok"] else "[FAIL]"
        print(f"[nl2shortcut agent-api] startup self-test {tag} "
              f"({st['passed']}/{st['total']} passed, {st['failed']} failed, "
              f"{st['runtime_s']}s)", flush=True)
        _persist_startup_self_test(st, host, port)
    except Exception as e:  # 自检本身异常也不应阻断启动
        print(f"[nl2shortcut agent-api] startup self-test error: {e}",
              file=sys.stderr, flush=True)


def _persist_startup_self_test(st: dict, host: str, port: int) -> None:
    """把启动自检完整报告写入 ~/.nl2shortcut/self_test_startup.json。"""
    try:
        import os, json
        from pathlib import Path
        d = Path(os.path.expanduser("~")) / ".nl2shortcut"
        d.mkdir(parents=True, exist_ok=True)
        payload = {"host": host, "port": port, **st}
        with open(d / "self_test_startup.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that handles each request in a new thread."""
    daemon_threads = True


def serve(host: str = "127.0.0.1", port: int = 7770, agent: Optional[ShortcutAgent] = None) -> _ThreadingHTTPServer:
    """Start the Agent API HTTP server. Blocks."""
    if agent is None:
        agent = ShortcutAgent()
    handler_cls = type("_BoundHandler", (_Handler,), {"agent": agent})
    server = _ThreadingHTTPServer((host, port), handler_cls)
    # 启动时自检：输出健康快照（失败不阻止启动）
    _run_startup_self_test(host, port)
    print(f"[nl2shortcut agent-api] listening on http://{host}:{port}", flush=True)
    print(f"[nl2shortcut agent-api] endpoints: /v1/health /v1/schema /v1/capabilities /v1/keys /v1/shortcut /v1/session /v1/rate_limit /v1/stats /v1/execute /v1/recognize /v1/sequence /v1/plan /v1/api/execute /v1/vision/execute /v1/stats/reset /v1/session/start /v1/session/end", flush=True)
    server.serve_forever()
    return server


def serve_thread(host: str = "127.0.0.1", port: int = 7770, agent: Optional[ShortcutAgent] = None) -> threading.Thread:
    """Start the Agent API server in a background thread."""
    def _run():
        try:
            serve(host=host, port=port, agent=agent)
        except OSError as e:
            print(f"[nl2shortcut agent-api] failed to bind {host}:{port}: {e}", file=sys.stderr)
    t = threading.Thread(target=_run, name="nl2shortcut-agent-api", daemon=True)
    t.start()
    return t


def launch_daemon(host: str = "127.0.0.1", port: int = 7770,
                  log_to_stderr: bool = False):
    """Launch the Agent API server in the current process.

    This is the *primary* entry point: NL2Shortcut exists to serve Agents.
    Returns the (server, thread) pair; caller is responsible for stopping.
    """
    import threading
    agent = ShortcutAgent()
    handler_cls = type("_BoundHandler", (_Handler,), {"agent": agent})
    server = _ThreadingHTTPServer((host, port), handler_cls)
    if not log_to_stderr:
        import logging
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
    # 启动时自检：输出健康快照（失败不阻止启动）
    _run_startup_self_test(host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="nl2shortcut-agent-api")
    thread.start()
    return server, thread


def shutdown_daemon(server) -> None:
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass


def _timeout_result(intent: str) -> ExecutionResult:
    return ExecutionResult(
        success=False, command="", key_combination="", platform="",
        error=f"timeout while resolving intent: '{intent}'", processing_time=0.0,
    )


if __name__ == "__main__":
    import sys
    host = "127.0.0.1"
    port = 7770
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    serve(host=host, port=port)





