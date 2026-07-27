"""nl2shortcut MCP 服务器 — 基于 Model Context Protocol 的工具服务器。

参考：https://modelcontextprotocol.io/

两种传输模式：
  --transport stdio   基于 stdin/stdout 的 JSON-RPC（MCP CLI 工具，OpenClaw 原生支持）
  --transport http    HTTP + Server-Sent Events（Web 客户端，OpenClaw 插件）

用法：
  python -m nl2shortcut mcp-server
  python -m nl2shortcut mcp-server --port 7791
  python -m nl2shortcut mcp-server --transport http --port 7791

OpenClaw 集成（stdio 模式 —— 即插即用的 MCP 工具）：
  在 OpenClaw 的配置 JSON 中，向 mcpServers 添加：
    "nl2shortcut": {
      "command": "python",
      "args": ["-m", "nl2shortcut", "mcp-server", "--transport", "stdio"]
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional
from pathlib import Path

# ── 本地导入 ──────────────────────────────────────────────────────
from .agent import ShortcutAgent
from .models import Platform, AppContext

logger = logging.getLogger("nl2shortcut.mcp")


# ═══════════════════════════════════════════════════════════════════════
#  JSON-RPC 2.0 Helpers
# ═══════════════════════════════════════════════════════════════════════

def rpc_response(req_id: Any, result: Any) -> dict:
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> dict:
    """Build a JSON-RPC 2.0 error response."""
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


# ── MCP 错误码 ──────────────────────────────────────────────────
class MCPErrorCode:
    PARSE_ERROR          = -32700
    INVALID_REQUEST      = -32600
    METHOD_NOT_FOUND     = -32601
    INVALID_PARAMS       = -32602
    INTERNAL_ERROR       = -32603


# ═══════════════════════════════════════════════════════════════════════
#  Tool Definitions
# ═══════════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: List[dict] = [
    {
        "name": "nl2shortcut_execute",
        "description": (
            "Execute a natural-language keyboard shortcut command. "
            "Parses the intent, resolves the correct key combination for the "
            "current platform (Windows/macOS/Linux), and presses it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "Natural language intent, e.g. 'copy this text', 'save file', 'undo'",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "If true, only return the resolved key combination without pressing.",
                    "default": False,
                },
                "timeout": {
                    "type": "number",
                    "description": "Execution timeout in seconds.",
                    "default": 5.0,
                },
            },
            "required": ["intent"],
        },
    },
    {
        "name": "nl2shortcut_plan",
        "description": (
            "Decompose a complex goal into an ordered step-by-step plan "
            "(no execution, just planning). Use nl2shortcut_execute_plan "
            "to run the plan."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "Complex goal in natural language, e.g. 'open file, copy content, paste into new file'",
                },
                "context": {
                    "type": "object",
                    "description": "Optional execution context (app, window_title, platform)",
                    "properties": {
                        "app": {"type": "string"},
                        "window_title": {"type": "string"},
                        "platform": {"type": "string"},
                    },
                },
            },
            "required": ["goal"],
        },
    },
    {
        "name": "nl2shortcut_execute_plan",
        "description": "Execute a previously generated plan by plan_id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "description": "Plan ID returned by nl2shortcut_plan",
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["plan_id"],
        },
    },
    {
        "name": "nl2shortcut_list_patterns",
        "description": (
            "List all learned operation patterns / instruction bundles stored "
            "in NL2Shortcut's memory. Each pattern maps a high-level goal "
            "to a sequence of key commands."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "按类别筛选 (编辑, 文件, 视图, 导航, 代码, 系统, 通用, Windows徽标键, 文件资源管理器, 命令提示符, 虚拟桌面, 任务栏, 设置, 对话框)",
                },
                "app": {
                    "type": "string",
                    "description": "Filter by application name (e.g. 'vscode', 'chrome')",
                },
            },
        },
    },
    {
        "name": "nl2shortcut_suggest",
        "description": (
            "Actively suggest a faster keyboard shortcut for the current "
            "mouse / manual action. Call periodically or after detecting "
            "inefficient user actions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Current active application",
                },
                "current_action": {
                    "type": "string",
                    "description": "Description of the action just performed (e.g. 'used mouse to copy text')",
                },
            },
            "required": ["app", "current_action"],
        },
    },
    {
        "name": "nl2shortcut_record",
        "description": (
            "Record an operation into NL2Shortcut's memory so it learns "
            "the pattern for future use."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Application where the action occurred",
                },
                "action": {
                    "type": "string",
                    "description": "Natural-language description of the action (e.g. 'switch to the previous tab')",
                },
                "goal": {
                    "type": "string",
                    "description": "High-level goal this action accomplishes",
                },
                "key_combination": {
                    "type": "string",
                    "description": "Optional: the actual key combination used (NL2Shortcut will infer if omitted)",
                },
            },
            "required": ["app", "action"],
        },
    },
    {
        "name": "nl2shortcut_context",
        "description": "Return the current active application context (window, app name, platform).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "nl2shortcut_stats",
        "description": "Return execution statistics (total, success rate, avg time, top commands).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# ═══════════════════════════════════════════════════════════════════════
#  Resource Definitions
# ═══════════════════════════════════════════════════════════════════════

RESOURCE_DEFINITIONS: List[dict] = [
    {
        "uri": "shortcuts://all",
        "name": "All Shortcuts",
        "description": "Complete list of all registered keyboard shortcuts",
        "mimeType": "application/json",
    },
    {
        "uri": "shortcuts://stats",
        "name": "Execution Statistics",
        "description": "Current NL2Shortcut execution statistics",
        "mimeType": "application/json",
    },
    {
        "uri": "context://current",
        "name": "Current Context",
        "description": "Current active window/application context",
        "mimeType": "application/json",
    },
]


# ═══════════════════════════════════════════════════════════════════════
#  NL2ShortcutMCPServer
# ═══════════════════════════════════════════════════════════════════════

class NL2ShortcutMCPServer:
    """MCP Server exposing NL2Shortcut capabilities via JSON-RPC 2.0."""

    def __init__(
        self,
        agent: Optional[ShortcutAgent] = None,
        port: int = 7791,
        suggest_callback: Optional[Callable[[str], None]] = None,
    ):
        self.port = port
        self._agent = agent
        self._suggest_callback = suggest_callback
        self._initialized = False
        self._client_info: dict = {}
        # 内存中的计划存储：plan_id -> plan dict
        self._plans: Dict[str, dict] = {}
        # 内存中的模式存储（用于 suggest/record）
        self._patterns: List[dict] = []
        # 延迟初始化 OperationMemory，用于基于机器学习的建议
        self._op_memory = None

        # Server capabilities (MCP spec)
        self._server_capabilities: dict = {
            "tools": {"listChanged": True},
            "resources": {
                "subscribe": True,
                "listChanged": True,
            },
        }

    # ── Lazy agent ─────────────────────────────────────────────────

    @property
    def agent(self) -> ShortcutAgent:
        if self._agent is None:
            self._agent = ShortcutAgent()
        return self._agent

    # ── Tool Handlers ───────────────────────────────────────────────

    def _handle_nl2shortcut_execute(self, arguments: dict) -> dict:
        intent = arguments.get("intent", "")
        dry_run = arguments.get("dry_run", False)
        timeout = arguments.get("timeout", 5.0)

        result = self.agent.execute(intent, dry_run=dry_run, timeout=timeout)

        return {
            "success": result.success,
            "intent": result.intent,
            "command": result.command,
            "key_combination": result.key_combination,
            "platform": result.platform,
            "confidence": result.confidence,
            "processing_time_ms": round(result.processing_time * 1000, 2),
            "mode": result.mode,
            "error": result.error,
            "dry_run": dry_run,
        }

    def _handle_nl2shortcut_plan(self, arguments: dict) -> dict:
        goal = arguments.get("goal", "")
        context = arguments.get("context", {})

        # 使用 agent 的意图识别 + LLM 进行拆解
        intent_result = self.agent.recognize_intent(goal)

        if intent_result.command == "__composite__" and intent_result.composite_plan:
            plan_obj = intent_result.composite_plan
            steps = []
            for i, step in enumerate(plan_obj.steps or []):
                steps.append({
                    "index": i,
                    "command": step.command,
                    "reason": getattr(step, "reason", ""),
                    "key": getattr(step, "key", ""),
                })
            plan_id = str(uuid.uuid4())
            plan_data = {
                "plan_id": plan_id,
                "goal": goal,
                "context": context,
                "mode": "composite",
                "steps": steps,
                "reasoning": getattr(plan_obj, "reasoning", ""),
            }
        elif intent_result.alternatives:
            # LLM multi-step plan
            steps = []
            for i, alt in enumerate(intent_result.alternatives):
                steps.append({
                    "index": i,
                    "command": alt.command,
                    "reason": getattr(alt, "reason", ""),
                    "key": "",
                })
            plan_id = str(uuid.uuid4())
            plan_data = {
                "plan_id": plan_id,
                "goal": goal,
                "context": context,
                "mode": "llm_plan",
                "steps": steps,
                "reasoning": getattr(intent_result, "reasoning", ""),
            }
        else:
            # Single-step fallback
            plan_id = str(uuid.uuid4())
            steps = [{
                "index": 0,
                "command": intent_result.command,
                "reason": f"Direct shortcut: {intent_result.intent}",
                "key": "",
            }]
            plan_data = {
                "plan_id": plan_id,
                "goal": goal,
                "context": context,
                "mode": "single",
                "steps": steps,
                "reasoning": f"Single-step plan (confidence={intent_result.confidence:.2f})",
            }

        self._plans[plan_id] = plan_data
        return plan_data

    def _handle_nl2shortcut_execute_plan(self, arguments: dict) -> dict:
        plan_id = arguments.get("plan_id", "")
        dry_run = arguments.get("dry_run", False)

        plan_data = self._plans.get(plan_id)
        if not plan_data:
            return {"error": f"Plan not found: {plan_id}", "success": False}

        executed_steps = []
        all_errors = []
        platform = Platform.detect()

        for step in plan_data["steps"]:
            cmd = step.get("command", "")
            if not cmd:
                continue
            shortcut = self.agent._db.get_by_command(cmd)  # type: ignore[attr-defined]
            if shortcut is None:
                all_errors.append(f"Unknown command: {cmd}")
                continue
            key = shortcut.get_key(platform)
            if not key:
                all_errors.append(f"No key for '{cmd}' on {platform.value}")
                continue
            err = None
            if not dry_run:
                try:
                    self.agent.adapter.send_keys(key)
                    self.agent._db.increment_frequency(cmd)  # type: ignore[attr-defined]
                except Exception as e:
                    err = str(e)
                    all_errors.append(f"{cmd}: {e}")
            executed_steps.append({
                "command": cmd,
                "key": key,
                "success": err is None,
                "error": err,
            })

        return {
            "success": len(all_errors) == 0,
            "plan_id": plan_id,
            "executed_steps": executed_steps,
            "errors": all_errors,
            "dry_run": dry_run,
        }

    def _handle_nl2shortcut_list_patterns(self, arguments: dict) -> dict:
        category = arguments.get("category")
        app = arguments.get("app")

        shortcuts = self.agent.list_shortcuts(category=category)
        items = []
        for s in shortcuts:
            if app and s.application and s.application.lower() != app.lower():
                continue
            items.append({
                "command": s.command,
                "description": s.description,
                "windows_key": s.windows_key,
                "mac_key": s.mac_key,
                "linux_key": s.linux_key,
                "category": s.category,
                "app": s.application,
                "frequency": s.frequency,
            })

        # Merge in-memory patterns
        all_items = items + self._patterns

        return {
            "patterns": all_items,
            "count": len(all_items),
            "filters": {"category": category, "app": app},
        }

    def _handle_nl2shortcut_suggest(self, arguments: dict) -> dict:
        app = arguments.get("app", "")
        current_action = arguments.get("current_action", "")

        suggestions: List[dict] = []

        # 主要路径：使用 OperationMemory 基于机器学习的建议引擎
        if app and current_action:
            try:
                if self._op_memory is None:
                    from .operation_memory import OperationMemory
                    self._op_memory = OperationMemory()
                advice = self._op_memory.get_suggestion(goal=current_action, app=app)
                if advice:
                    suggestions.append({
                        "type": "keyboard_shortcut",
                        "action_type": "memory_suggestion",
                        "suggested_key": advice,
                        "description": advice,
                        "confidence": 0.9,
                        "app": app,
                    })
            except Exception:
                pass

        # 回退路径：基于规则的关键词引擎（作为安全网保留）

        # ── 基于规则的简单建议 ──
        action_keywords = {
            "copy": {
                "pattern": ["mouse", "right-click", "context menu", "手动复制", "鼠标复制"],
                "shortcut": "Ctrl+C",
                "description": "Use Ctrl+C to copy selected text",
            },
            "paste": {
                "pattern": ["mouse", "right-click", "context menu", "手动粘贴", "鼠标粘贴"],
                "shortcut": "Ctrl+V",
                "description": "Use Ctrl+V to paste",
            },
            "select all": {
                "pattern": ["mouse", "select all", "全选", "鼠标全选"],
                "shortcut": "Ctrl+A",
                "description": "Use Ctrl+A to select all",
            },
            "undo": {
                "pattern": ["undo", "撤销", "撤回"],
                "shortcut": "Ctrl+Z",
                "description": "Use Ctrl+Z to undo",
            },
            "redo": {
                "pattern": ["redo", "重做", "恢复"],
                "shortcut": "Ctrl+Y",
                "description": "Use Ctrl+Y to redo",
            },
            "save": {
                "pattern": ["save", "保存", "保存文件"],
                "shortcut": "Ctrl+S",
                "description": "Use Ctrl+S to save",
            },
            "find": {
                "pattern": ["find", "搜索", "查找"],
                "shortcut": "Ctrl+F",
                "description": "Use Ctrl+F to search",
            },
            "new file": {
                "pattern": ["new file", "新建文件", "新建"],
                "shortcut": "Ctrl+N",
                "description": "Use Ctrl+N for new file/window",
            },
            "close tab": {
                "pattern": ["close tab", "关闭标签", "关闭页面"],
                "shortcut": "Ctrl+W",
                "description": "Use Ctrl+W to close current tab",
            },
            "refresh": {
                "pattern": ["refresh", "reload", "刷新", "重载"],
                "shortcut": "Ctrl+R",
                "description": "Use Ctrl+R to refresh",
            },
            "switch tab": {
                "pattern": ["switch tab", "切换标签", "切换页面"],
                "shortcut": "Ctrl+Tab / Ctrl+Shift+Tab",
                "description": "Use Ctrl+Tab to cycle tabs",
            },
            "bold": {
                "pattern": ["bold", "加粗"],
                "shortcut": "Ctrl+B",
                "description": "Use Ctrl+B to bold",
            },
        }

        action_lower = current_action.lower()
        for action_type, info in action_keywords.items():
            if any(kw in action_lower for kw in info["pattern"]):
                suggestions.append({
                    "type": "keyboard_shortcut",
                    "action_type": action_type,
                    "suggested_key": info["shortcut"],
                    "description": info["description"],
                    "confidence": 0.95,
                    "app": app,
                })

        # ── 若已注册，调用建议回调 ──
        if self._suggest_callback and suggestions:
            try:
                self._suggest_callback(json.dumps(suggestions, ensure_ascii=False))
            except Exception as e:
                logger.warning("Suggestion callback error: %s", e)

        return {
            "suggestions": suggestions,
            "app": app,
            "current_action": current_action,
            "platform": Platform.detect().value,
        }

    def _handle_nl2shortcut_record(self, arguments: dict) -> dict:
        app = arguments.get("app", "")
        action = arguments.get("action", "")
        goal = arguments.get("goal", action)
        key_combination = arguments.get("key_combination")

        pattern = {
            "id": str(uuid.uuid4()),
            "app": app,
            "action": action,
            "goal": goal,
            "key_combination": key_combination,
        }
        self._patterns.append(pattern)

        # 若已知按键组合，同时记录到数据库
        if key_combination:
            from .models import Shortcut
            shortcut = Shortcut(
                command=action,
                description=goal,
                windows_key=key_combination,
                mac_key=key_combination,
                linux_key=key_combination,
                application=app,
                category="custom",
            )
            self.agent.add_shortcut(shortcut)

        return {
            "success": True,
            "pattern_id": pattern["id"],
            "message": f"Recorded: [{app}] {action} -> {key_combination or '(infer later)'}",
        }

    def _handle_nl2shortcut_context(self, arguments: dict) -> dict:
        ctx = self.agent.get_context()
        return {
            "window_title": ctx.window_title,
            "process_name": ctx.process_name,
            "app_name": ctx.app_name,
            "platform": ctx.platform or Platform.detect().value,
        }

    def _handle_nl2shortcut_stats(self, arguments: dict) -> dict:
        stats = self.agent.get_stats()
        return {
            "total_executions": stats.total_executions,
            "successful": stats.successful,
            "failed": stats.failed,
            "success_rate": round(stats.success_rate, 2),
            "avg_processing_time_ms": round(stats.avg_processing_time * 1000, 2),
            "top_commands": [
                {"command": cmd, "description": desc, "frequency": freq}
                for cmd, desc, freq in (stats.top_commands or [])
            ],
        }

    # ── Resource Content ────────────────────────────────────────────

    def _get_resource_content(self, uri: str) -> tuple[str, Any]:
        """返回资源 URI 对应的 (mimeType, content)。"""
        if uri == "shortcuts://all":
            shortcuts = self.agent.list_shortcuts()
            data = [{
                "command": s.command,
                "description": s.description,
                "windows_key": s.windows_key,
                "mac_key": s.mac_key,
                "linux_key": s.linux_key,
                "category": s.category,
                "application": s.application,
            } for s in shortcuts]
            return "application/json", data
        elif uri == "shortcuts://stats":
            return "application/json", self._handle_nl2shortcut_stats({})
        elif uri == "context://current":
            return "application/json", self._handle_nl2shortcut_context({})
        return "text/plain", f"Unknown resource: {uri}"

    # ── MCP Request Dispatcher ──────────────────────────────────────

    async def handle_request(self, method: str, params: dict, req_id: Any) -> dict:
        """JSON-RPC 请求主分发器。"""
        # ── Core MCP Methods ──
        if method == "initialize":
            return await self._handle_initialize(params, req_id)

        if method == "notifications/initialized":
            # 客户端已就绪；无需响应（通知）
            return rpc_response(req_id, None)

        if method == "tools/list":
            return rpc_response(req_id, {"tools": TOOL_DEFINITIONS})

        if method == "tools/call":
            return await self._handle_tools_call(params, req_id)

        if method == "resources/list":
            return rpc_response(req_id, {"resources": RESOURCE_DEFINITIONS})

        if method == "resources/read":
            return await self._handle_resources_read(params, req_id)

        if method == "resources/subscribe":
            # 订阅资源变更事件（当前为空操作）
            return rpc_response(req_id, {"subscribed": True})

        if method == "ping":
            return rpc_response(req_id, {"pong": True})

        # Unknown method
        return rpc_error(
            req_id,
            MCPErrorCode.METHOD_NOT_FOUND,
            f"Method not found: {method}",
        )

    async def _handle_initialize(self, params: dict, req_id: Any) -> dict:
        """Handle MCP initialize — exchange capabilities."""
        self._client_info = params.get("clientInfo", {})
        self._initialized = True
        logger.info(
            "MCP initialized. Client: %s v%s",
            self._client_info.get("name", "unknown"),
            self._client_info.get("version", "?"),
        )
        return rpc_response(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": self._server_capabilities,
            "serverInfo": {
                "name": "nl2shortcut",
                "version": "0.4.0",
            },
        })

    async def _handle_tools_call(self, params: dict, req_id: Any) -> dict:
        """Execute a named tool with arguments."""
        name = params.get("name", "")
        arguments = params.get("arguments", {})

        handlers: Dict[str, Callable] = {
            "nl2shortcut_execute":       self._handle_nl2shortcut_execute,
            "nl2shortcut_plan":          self._handle_nl2shortcut_plan,
            "nl2shortcut_execute_plan":  self._handle_nl2shortcut_execute_plan,
            "nl2shortcut_list_patterns": self._handle_nl2shortcut_list_patterns,
            "nl2shortcut_suggest":       self._handle_nl2shortcut_suggest,
            "nl2shortcut_record":        self._handle_nl2shortcut_record,
            "nl2shortcut_context":       self._handle_nl2shortcut_context,
            "nl2shortcut_stats":         self._handle_nl2shortcut_stats,
        }

        handler = handlers.get(name)
        if not handler:
            return rpc_error(
                req_id,
                MCPErrorCode.METHOD_NOT_FOUND,
                f"Unknown tool: {name}",
            )

        try:
            result = handler(arguments)
            return rpc_response(req_id, {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, ensure_ascii=False, indent=2),
                    }
                ],
                "isError": result.get("error") is not None or result.get("success") is False,
            })
        except Exception as e:
            logger.exception("Tool %s failed: %s", name, e)
            return rpc_error(
                req_id,
                MCPErrorCode.INTERNAL_ERROR,
                f"Tool execution failed: {e}",
                data={"tool": name},
            )

    async def _handle_resources_read(self, params: dict, req_id: Any) -> dict:
        """Read resource content by URI."""
        uri = params.get("uri", "")
        mime_type, content = self._get_resource_content(uri)
        return rpc_response(req_id, {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": mime_type,
                    "text": json.dumps(content, ensure_ascii=False, indent=2)
                    if isinstance(content, (dict, list))
                    else str(content),
                }
            ]
        })


# ═══════════════════════════════════════════════════════════════════════
#  Transport: STDIO
# ═══════════════════════════════════════════════════════════════════════

async def _run_stdio(server: NL2ShortcutMCPServer) -> None:
    """
    Run MCP server over stdio (JSON-RPC 2.0).

    Uses a dedicated thread for blocking stdin reads so the asyncio event loop
    stays responsive.  This works reliably on Windows.
    """
    loop = asyncio.get_event_loop()
    request_queue: asyncio.Queue = asyncio.Queue()
    eof_received = False

    def _stdin_reader_thread() -> None:
        """
        Blocking thread: reads lines from stdin and puts them in the async queue.
        Exits on EOF or I/O error.
        """
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                # Schedule the line put into the async queue from the event loop thread
                asyncio.run_coroutine_threadsafe(
                    request_queue.put(line), loop
                )
        except Exception as exc:
            logger.debug("stdin reader thread ended: %s", exc)
        finally:
            asyncio.run_coroutine_threadsafe(request_queue.put(None), loop)

    # Start the blocking stdin reader in a daemon thread
    reader_thread = threading.Thread(target=_stdin_reader_thread, daemon=True)
    reader_thread.start()

    async def _dispatch(req: Any, srv: NL2ShortcutMCPServer) -> None:
        try:
            method = req.get("method", "")
            params = req.get("params", {})
            req_id = req.get("id")
            resp = await srv.handle_request(method, params, req_id)
            # notifications/initialized → no response needed
            if resp.get("id") is not None or method == "ping":
                print(json.dumps(resp, ensure_ascii=False), flush=True)
        except Exception as e:
            logger.exception("STDIO dispatch error: %s", e)
            print(
                json.dumps(rpc_error(req.get("id"), MCPErrorCode.INTERNAL_ERROR, str(e))),
                flush=True,
            )

    # Process lines as they arrive
    while True:
        line = await request_queue.get()
        if line is None:
            # EOF / stdin closed
            break
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(
                json.dumps(
                    rpc_error(None, MCPErrorCode.PARSE_ERROR, f"Invalid JSON: {line[:100]}")
                ),
                flush=True,
            )
            continue
        await _dispatch(req, server)


# ═══════════════════════════════════════════════════════════════════════
#  Transport: HTTP + SSE
# ═══════════════════════════════════════════════════════════════════════

async def _run_http(server: NL2ShortcutMCPServer) -> None:
    """Run MCP server over HTTP + Server-Sent Events (SSE)."""
    from aiohttp import web

    # Active SSE client connections for proactive push
    sse_clients: List[web.StreamResponse] = []
    sse_lock = asyncio.Lock()

    async def sse_handler(request: web.Request) -> web.StreamResponse:
        """SSE endpoint: clients connect here to receive proactive suggestions."""
        nonlocal sse_clients
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        async with sse_lock:
            sse_clients.append(resp)
        logger.info("SSE client connected (total: %d)", len(sse_clients))

        try:
            # Send initial ping
            await resp.write(b"event: ping\ndata: {\"type\":\"connected\"}\n\n")
            # Keep-alive heartbeat every 30s
            while True:
                await asyncio.sleep(30)
                try:
                    await resp.write(b"event: ping\ndata: {}\n\n")
                except Exception:
                    break
        finally:
            async with sse_lock:
                sse_clients = [c for c in sse_clients if c is not resp]
            logger.info("SSE client disconnected")
            await resp.write_eof()

    async def push_suggestion(message: str) -> None:
        """Push a suggestion to all connected SSE clients."""
        nonlocal sse_clients
        async with sse_lock:
            clients = list(sse_clients)
        for client in clients:
            try:
                await client.write(f"event: suggestion\ndata: {json.dumps(message)}\n\n".encode())
            except Exception:
                pass

    # Wire push capability into server
    server._suggest_callback = lambda msg: asyncio.create_task(push_suggestion(msg))

    async def json_rpc_handler(request: web.Request) -> web.Response:
        """POST endpoint for JSON-RPC 2.0 requests."""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                rpc_error(None, MCPErrorCode.PARSE_ERROR, "Invalid JSON body"),
                status=400,
            )

        # Batch request support
        if isinstance(body, list):
            results = []
            for req in body:
                r = await server.handle_request(
                    req.get("method", ""),
                    req.get("params", {}),
                    req.get("id"),
                )
                if r.get("id") is not None:
                    results.append(r)
            return web.json_response(results)
        else:
            method = body.get("method", "")
            params = body.get("params", {})
            req_id = body.get("id")
            resp = await server.handle_request(method, params, req_id)
            return web.json_response(resp)

    async def get_handler(request: web.Request) -> web.Response:
        """GET /mcp/info — server info for health checks."""
        return web.json_response({
            "server": "nl2shortcut",
            "version": "0.4.0",
            "transport": "http+sse",
            "endpoints": {
                "rpc": "/mcp/rpc",
                "sse": "/mcp/events",
                "info": "/mcp/info",
            },
        })

    app = web.Application()
    app.router.add_get("/mcp/info", get_handler)
    app.router.add_post("/mcp/rpc", json_rpc_handler)
    app.router.add_get("/mcp/events", sse_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", server.port)
    await site.start()
    logger.info("HTTP MCP server listening on http://0.0.0.0:%d/mcp/rpc", server.port)
    logger.info("SSE endpoint: http://0.0.0.0:%d/mcp/events", server.port)

    # Keep running
    while True:
        await asyncio.sleep(3600)


# ═══════════════════════════════════════════════════════════════════════
#  Entry Points
# ═══════════════════════════════════════════════════════════════════════

async def run_server(
    transport: str = "stdio",
    port: int = 7791,
    agent: Optional[ShortcutAgent] = None,
) -> None:
    """Start the MCP server with the specified transport."""
    server = NL2ShortcutMCPServer(agent=agent, port=port)

    if transport == "stdio":
        logger.info("Starting MCP server in stdio mode")
        await _run_stdio(server)
    elif transport == "http":
        logger.info("Starting MCP server in HTTP+SSE mode on port %d", port)
        await _run_http(server)
    else:
        raise ValueError(f"Unknown transport: {transport!r}. Use 'stdio' or 'http'.")


def run(
    transport: str = "stdio",
    port: int = 7791,
    agent: Optional[ShortcutAgent] = None,
) -> None:
    """Synchronous entry point for CLI use."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        asyncio.run(run_server(transport=transport, port=port, agent=agent))
    except KeyboardInterrupt:
        logger.info("MCP server stopped by user")
        sys.exit(0)


# ═══════════════════════════════════════════════════════════════════════
#  CLI Command
# ═══════════════════════════════════════════════════════════════════════

def cmd_mcp_server(agent, args) -> int:
    """CLI handler for 'nl2shortcut mcp-server'."""
    transport = getattr(args, "transport", "stdio")
    port = getattr(args, "port", 7791)

    print(
        f"[nl2shortcut] MCP server starting — "
        f"transport={transport} port={port}",
        flush=True,
    )

    if transport == "stdio":
        print(
            "[nl2shortcut] stdio mode: connect OpenClaw MCP client to stdin/stdout",
            flush=True,
        )
    else:
        print(
            f"[nl2shortcut] HTTP mode: POST /mcp/rpc | SSE /mcp/events | "
            f"info http://127.0.0.1:{port}/mcp/info",
            flush=True,
        )

    run(transport=transport, port=port)
    return 0
