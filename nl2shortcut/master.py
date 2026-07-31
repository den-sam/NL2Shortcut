"""NL2Shortcut Keyboard Master Agent — unified facade.

把整套能力收敛成一个主动式 Agent：

  · ShortcutAgent     —— 自然语言 → 键盘/鼠标指令（核心执行）
  · GoalPlanner       —— 目标 → 多步计划（DeepSeek 推理）
  · OperationMemory   —— 操作记忆 + 模式学习 + 主动建议
  · KeyboardPrimitives —— 24 个原子键盘原语（Tab/Alt+字母/Shell 兜底）
  · Agent API Server  —— 自管理 HTTP 服务（原 Skill 的 start_server / execute）

原本由 QClaw Skill（nl2shortcut-executor）提供的「启动 server + 执行指令」胶水
能力，现在内建进软件本身，无需外部桥接即可作为一个完整 Agent 独立运行。

典型用法
--------
    from nl2shortcut import KeyboardMasterAgent

    master = KeyboardMasterAgent()
    # 1) 直接执行自然语言指令（带记忆回写）
    r = master.execute("复制这段文字")
    # 2) 让 Agent 自己拆解目标
    plan = master.plan("帮我把这份报告发出去")
    # 3) 主动建议
    hint = master.suggest(app="outlook")
    # 4) 自管理 server（供外部 Agent / MCP 调用）
    master.start_server()          # 后台常驻 127.0.0.1:7770
    master.execute_via_server("粘贴")
"""

from __future__ import annotations

import sys
import time
import json
import uuid
import subprocess
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, List, Dict, Any

from .agent import ShortcutAgent
from .models import (
    Platform, ExecutionResult, Plan,
)
from .planner import GoalPlanner, plan_to_workflow
from .operation_memory import OperationMemory
from .keyboard_primitives import KeyboardPrimitives
from .workflow_matcher import WorkflowMatcher, MatchResult
from .workflow import WorkflowEngine
from . import agent_api


DEFAULT_PORT = 7770
DEFAULT_HOST = "127.0.0.1"
HEALTH_URL_TMPL = "http://{host}:{port}/v1/health"
EXECUTE_URL_TMPL = "http://{host}:{port}/v1/execute"


class KeyboardMasterAgent:
    """NL2Shortcut Keyboard Master Agent — 统一对外入口。

    一个会理解目标、自己拆步、记住你的操作习惯、并主动给建议的键盘大师。
    """

    # ── 身份 ────────────────────────────────────────────────────────────
    NAME = "NL2Shortcut Keyboard Master Agent"
    VERSION = "1.0.0"

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        enable_llm: bool = True,
        enable_spacy: bool = False,
        server_host: str = DEFAULT_HOST,
        server_port: int = DEFAULT_PORT,
    ):
        self.config_dir = config_dir or (Path.home() / ".nl2shortcut")
        self.server_host = server_host
        self.server_port = server_port

        # 核心执行引擎
        self.agent = ShortcutAgent(
            config_dir=self.config_dir,
            enable_llm=enable_llm,
            enable_spacy=enable_spacy,
        )

        # 目标规划器
        self.planner = GoalPlanner()

        # 操作记忆（持久化到 config_dir/operations.db）
        self.memory = OperationMemory(
            db_path=str(self.config_dir / "operations.db")
        )

        # 原子键盘原语
        self.primitives = KeyboardPrimitives()

        # 工作流匹配器（LLM 语义搜索已有 YAML 工作流）
        self.workflow_matcher = WorkflowMatcher(
            workflows_dir=self.config_dir / "workflows",
        )

        # 工作流引擎（加载 / 执行 YAML 工作流）
        self.workflow_engine = WorkflowEngine(
            agent=self.agent,
            workflows_dir=self.config_dir / "workflows",
        )

        # server 进程句柄
        self._server_proc: Optional[subprocess.Popen] = None

    # ═══════════════════════════════════════════════════════════════════
    # 核心：自然语言 → 执行（带记忆回写）
    # ═══════════════════════════════════════════════════════════════════
    def execute(
        self,
        intent: str,
        dry_run: bool = False,
        timeout: float = 5.0,
        learn: bool = True,
        user_goal: str = "",
    ) -> ExecutionResult:
        """执行一条自然语言指令，并在成功后回写操作记忆。

        Args:
            intent: 自然语言指令，如「复制这段文字」「保存文件」
            dry_run: 仅识别不实际按键
            learn: 执行成功后是否记录到操作记忆
            user_goal: 上层目标（用于记忆聚类，可选）
        """
        result = self.agent.execute(intent, dry_run=dry_run, timeout=timeout)

        if learn and result.success and result.key_combination:
            try:
                app = self.detect_app()
                self.memory.record(
                    app=app or "common",
                    action_type="shortcut",
                    action_detail=result.key_combination,
                    duration_ms=int(result.processing_time * 1000),
                    user_goal=user_goal or intent,
                    sequence_id=str(uuid.uuid4()),
                )
            except Exception:
                pass

        # 将每次执行自动保存为工作流（无论成功或失败）
        try:
            self.agent.auto_save_workflow(result, intent, dry_run=dry_run)
        except Exception:
            pass

        return result

    def recognize(self, intent: str):
        """仅识别意图（不执行）。"""
        return self.agent.recognize_intent(intent)

    # ═══════════════════════════════════════════════════════════════════
    # 智能执行：工作流匹配 → 执行 / 拆解 → 执行 → 自动保存
    # ═══════════════════════════════════════════════════════════════════
    def smart_execute(
        self,
        intent: str,
        dry_run: bool = False,
        timeout: float = 30.0,
        learn: bool = True,
        auto_save: bool = True,
    ) -> Dict[str, Any]:
        """智能执行管道 — 先查已有工作流，没有再让 LLM 拆解。

        流程
        ----
        1. **工作流匹配**：用 LLM 语义搜索 ``~/.nl2shortcut/workflows/``
           下所有 YAML 文件，找最匹配的。
        2. **命中** → 直接加载并执行该工作流（< 500ms，零 LLM 拆解）。
        3. **未命中** → 调用 GoalPlanner 把自然语言拆成步骤序列 → 执行。
        4. **自动保存**（auto_save=True）→ 把第 3 步生成的 Plan 保存为 YAML，
           下次同样的指令就能直接命中。

        Args:
            intent: 自然语言指令，如「帮我把报告发出去」
            dry_run: 仅预览不实际按键
            timeout: 整体超时（秒）
            learn: 执行成功后是否记录到操作记忆
            auto_save: 是否自动保存新生成的计划为 YAML 工作流

        Returns:
            dict::
                {
                    "ok": True/False,
                    "pipeline": "workflow_match" | "planner_generated" | "single_shortcut" | "error",
                    "matched_workflow": str or None,     # 命中工作流名
                    "match_confidence": float,             # 匹配置信度
                    "auto_saved": bool,                    # 是否自动保存为新工作流
                    "auto_saved_path": str or None,        # 保存路径
                    "plan": dict or None,                  # Plan.to_dict() 或 None
                    "steps_executed": int,                 # 执行步数
                    "results": list[dict],                 # 每步结果
                    "elapsed_ms": float,
                    "intent": str,
                    "error": str or None,
                }
        """
        start = time.perf_counter()
        result: Dict[str, Any] = {
            "ok": False,
            "pipeline": "error",
            "matched_workflow": None,
            "match_confidence": 0.0,
            "auto_saved": False,
            "auto_saved_path": None,
            "plan": None,
            "steps_executed": 0,
            "results": [],
            "elapsed_ms": 0.0,
            "intent": intent,
            "error": None,
        }

        if not intent.strip():
            result["error"] = "intent 为空"
            result["elapsed_ms"] = (time.perf_counter() - start) * 1000
            return result

        # ── Step 1: 工作流语义匹配 ──────────────────────────────────
        match_result: Optional[MatchResult] = None
        try:
            match_result = self.workflow_matcher.match(intent)
        except Exception as e:
            # 匹配失败不阻塞，直接进入规划
            match_result = None

        # ── Step 2: 命中 → 加载并执行工作流 ─────────────────────────
        if match_result and match_result.matched and match_result.workflow:
            wf_name = match_result.workflow.name
            result["pipeline"] = "workflow_match"
            result["matched_workflow"] = wf_name
            result["match_confidence"] = match_result.workflow.confidence

            try:
                wf_result = self.workflow_engine.run(
                    wf_name, dry_run=dry_run,
                )
                result["ok"] = wf_result.success
                result["steps_executed"] = len(wf_result.steps)
                result["results"] = [
                    {"step": s.step_name, "success": s.success,
                     "output": s.output or "", "error": s.error or ""}
                    for s in wf_result.steps
                ]
                if wf_result.error:
                    result["error"] = wf_result.error
            except Exception as e:
                result["error"] = f"执行工作流失败: {e}"

            result["elapsed_ms"] = (time.perf_counter() - start) * 1000

            # 记录到操作记忆
            if learn and result["ok"]:
                try:
                    self.memory.record(
                        app=self.detect_app() or "common",
                        action_type="workflow",
                        action_detail=wf_name,
                        duration_ms=int(result["elapsed_ms"]),
                        user_goal=intent,
                        sequence_id=str(uuid.uuid4()),
                    )
                except Exception:
                    pass

            return result

        # ── Step 3: 未命中 → LLM 拆解为目标计划 ──────────────────────
        result["pipeline"] = "planner_generated"

        try:
            plan = self.plan(intent)
        except Exception as e:
            result["error"] = f"规划失败: {e}"
            result["elapsed_ms"] = (time.perf_counter() - start) * 1000
            return result

        result["plan"] = plan.to_dict()

        # 如果计划为空（LLM 也搞不定）
        if not plan.steps:
            # 回退到传统单个快捷键执行
            result["pipeline"] = "single_shortcut"
            try:
                exec_result = self.execute(intent, dry_run=dry_run,
                                           timeout=timeout, learn=learn)
                result["ok"] = exec_result.success
                result["steps_executed"] = 1
                result["results"] = [{
                    "step": intent,
                    "success": exec_result.success,
                    "output": exec_result.key_combination or "",
                    "error": exec_result.error or "",
                }]
                if exec_result.error:
                    result["error"] = exec_result.error
            except Exception as e:
                result["error"] = f"回退执行失败: {e}"
            result["elapsed_ms"] = (time.perf_counter() - start) * 1000
            return result

        # ── Step 4: 执行计划 ─────────────────────────────────────────
        step_results = self.execute_plan(plan, dry_run=dry_run)
        all_ok = all(r.success for r in step_results)

        result["ok"] = all_ok
        result["steps_executed"] = len(step_results)
        result["results"] = [
            {
                "step": r.intent or f"step {i+1}",
                "success": r.success,
                "output": r.key_combination or "",
                "error": r.error or "",
                "elapsed_ms": r.processing_time * 1000,
            }
            for i, r in enumerate(step_results)
        ]

        # ── Step 5: 自动保存为新工作流 ───────────────────────────────
        if auto_save and all_ok and plan.source != "fallback":
            try:
                saved_path = plan_to_workflow(plan)
                if saved_path:
                    result["auto_saved"] = True
                    result["auto_saved_path"] = str(saved_path)
            except Exception:
                pass

        result["elapsed_ms"] = (time.perf_counter() - start) * 1000
        return result

    # ═══════════════════════════════════════════════════════════════════
    # 规划：目标 → 多步计划
    # ═══════════════════════════════════════════════════════════════════
    def plan(self, goal: str, context: Optional[dict] = None) -> Plan:
        """让 Agent 把目标拆成可执行的步骤序列。

        自动注入两件事：
        (1) 当前应用上下文（detect_context）
        (2) 历史操作记忆建议（OperationMemory）
        """
        # 1. 自动检测应用上下文
        if context is None:
            context = {}
        if not context.get("app_name"):
            try:
                app_ctx = self.detect_app()
                if app_ctx:
                    ctx = self.get_context()
                    ctx_dict: dict = {}
                    if hasattr(ctx, 'app_name') and ctx.app_name:
                        ctx_dict['app_name'] = ctx.app_name
                    if hasattr(ctx, 'window_title') and ctx.window_title:
                        ctx_dict['window_title'] = ctx.window_title
                    if hasattr(ctx, 'process_name') and ctx.process_name:
                        ctx_dict['process_name'] = ctx.process_name
                    # merge without overriding caller-supplied values
                    for k, v in ctx_dict.items():
                        if k not in context:
                            context[k] = v
            except Exception:
                pass

        # 2. 获取操作记忆建议
        memory_hints = ""
        try:
            app = (context.get("app_name") or "")
            suggestion = self.memory.get_suggestion(goal=goal, app=app)
            if suggestion:
                memory_hints = suggestion
            # 同时把所有已学习 pattern 注入为参考
            patterns = self.memory.list_patterns(app=app if app else None)
            if patterns:
                pat_lines = []
                for p in patterns[:10]:  # top 10 by frequency
                    if p.steps:
                        step_descs = ", ".join(
                            s.get("key", s.get("cmd", "?")) for s in p.steps[:5]
                        )
                        pat_lines.append(
                            f"• [{p.app}] {p.name}: {step_descs} "
                            f"(freq={p.frequency}, conf={p.confidence:.0%})"
                        )
                if pat_lines:
                    memory_hints += "\n\n已学习模式：\n" + "\n".join(pat_lines)
        except Exception:
            pass

        return self.planner.plan(goal, context=context,
                                 memory_hints=memory_hints)

    def execute_plan(self, plan: Plan, dry_run: bool = False) -> List[ExecutionResult]:
        """顺序执行计划中的每一步。"""
        results = self.planner.execute_plan(plan, dry_run=dry_run)
        plan_seq_id = str(uuid.uuid4())
        for r in results:
            if r.success and r.key_combination:
                try:
                    self.memory.record(
                        app=self.detect_app() or "common",
                        action_type="shortcut",
                        action_detail=r.key_combination,
                        duration_ms=int(r.processing_time * 1000),
                        user_goal=plan.reasoning or "",
                        sequence_id=plan_seq_id,
                    )
                except Exception:
                    pass
        return results

    @property
    def planner_available(self) -> bool:
        return self.planner.available()

    # ═══════════════════════════════════════════════════════════════════
    # 记忆：学习 & 主动建议
    # ═══════════════════════════════════════════════════════════════════
    def learn_patterns(self, min_frequency: int = 3) -> List[Any]:
        """从已记录操作中聚类出可复用模式。"""
        return self.memory.learn_patterns(min_frequency=min_frequency)

    def list_patterns(self, app: str = None) -> List[Any]:
        return self.memory.list_patterns(app=app)

    def suggest(self, goal: str = "", app: str = "") -> str:
        """主动给建议：基于历史操作习惯，推荐下一步动作。

        Args:
            goal: 当前目标（可选）
            app: 当前应用（可选，留空则自动检测）
        """
        if not app:
            app = self.detect_app() or ""
        try:
            return self.memory.get_suggestion(goal=goal, app=app)
        except Exception:
            return ""

    def record_operation(
        self,
        app: str,
        action_type: str,
        action_detail: str,
        duration_ms: int = 0,
        user_goal: str = "",
        context: str = "",
    ) -> int:
        """显式记录一次操作到记忆。"""
        return self.memory.record(
            app=app,
            action_type=action_type,
            action_detail=action_detail,
            duration_ms=duration_ms,
            user_goal=user_goal,
            context=context,
        )

    def export_patterns(self) -> str:
        return self.memory.export_patterns_json()

    def export_pattern_to_workflow(self, pattern_name: str,
                                   overwrite: bool = False) -> Optional[str]:
        """将一个已学习的模式导出为 YAML 工作流文件。"""
        pattern = self.memory.get_pattern(pattern_name)
        if pattern is None:
            return None
        return self.memory.export_pattern_to_workflow(pattern, overwrite=overwrite)

    def export_high_confidence_workflows(self) -> list:
        """将所有高置信度模式导出为工作流文件。"""
        return self.memory.export_high_confidence_patterns()

    # ═══════════════════════════════════════════════════════════════════
    # 环境感知
    # ═══════════════════════════════════════════════════════════════════
    def detect_app(self) -> str:
        try:
            ctx = self.agent.get_context()
            return ctx.app_name or ""
        except Exception:
            return ""

    def get_context(self):
        return self.agent.get_context()

    # ═══════════════════════════════════════════════════════════════════
    # 统计
    # ═══════════════════════════════════════════════════════════════════
    def get_stats(self):
        return self.agent.get_stats()

    def reset_stats(self):
        return self.agent.reset_stats()

    # ═══════════════════════════════════════════════════════════════════
    # Server 自管理（原 Skill 的 start_nl2shortcut_server / execute）
    # ═══════════════════════════════════════════════════════════════════
    def is_server_running(self, host: str = None, port: int = None) -> bool:
        host = host or self.server_host
        port = port or self.server_port
        try:
            req = urllib.request.Request(HEALTH_URL_TMPL.format(host=host, port=port))
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def server_info(self, host: str = None, port: int = None) -> Optional[dict]:
        host = host or self.server_host
        port = port or self.server_port
        try:
            req = urllib.request.Request(HEALTH_URL_TMPL.format(host=host, port=port))
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    def start_server(
        self, host: str = None, port: int = None, workdir: str = None
    ) -> Dict[str, Any]:
        """启动（或复用）Agent API server（后台常驻进程）。

        等效于原 Skill 的 start_nl2shortcut_server。
        """
        host = host or self.server_host
        port = port or self.server_port

        if self.is_server_running(host, port):
            info = self.server_info(host, port)
            version = info.get("version", "?") if info else "?"
            st = info.get("startup_self_test") if info else None
            st_line = (f"\n   启动自检：{'通过' if st and st['ok'] else '失败'} "
                       f"（{st['passed']}/{st['total']}）") if st else ""
            return {
                "ok": True,
                "already_running": True,
                "reply": (
                    f"✅  NL2Shortcut server 已在运行（{host}:{port}，版本 {version}）。{st_line}"
                ),
                "port": port,
            }

        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "nl2shortcut", "agent-api",
                 "--host", host, "--port", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=workdir or str(self.config_dir),
                startupinfo=self._startupinfo(),
            )
        except Exception as e:
            return {"ok": False, "reply": f"❌  启动失败：{e}", "port": port}

        self._server_proc = proc
        # 等待健康检查通过（最多 5s）
        for _ in range(10):
            time.sleep(0.5)
            if self.is_server_running(host, port):
                info = self.server_info(host, port)
                tiers = ", ".join(info.get("tiers_available", [])) if info else "?"
                st = info.get("startup_self_test") if info else None
                st_line = (f"\n   启动自检：{'通过' if st and st['ok'] else '失败'} "
                           f"（{st['passed']}/{st['total']}）") if st else ""
                return {
                    "ok": True,
                    "already_running": False,
                    "reply": (
                        f"✅  NL2Shortcut server 已启动（{host}:{port}，PID {proc.pid}）\n"
                        f"   版本：{info.get('version', '?') if info else '?'}\n"
                        f"   可用层：{tiers}{st_line}"
                    ),
                    "port": port,
                    "pid": proc.pid,
                }
        return {
            "ok": False,
            "reply": f"⚠️  server 进程已启动（PID {proc.pid}）但健康检查未通过。",
            "port": port,
            "pid": proc.pid,
        }

    def stop_server(self) -> Dict[str, Any]:
        """停止由本实例启动的 server 进程。"""
        if self._server_proc is None:
            return {"ok": True, "reply": "ℹ️  本实例未持有 server 进程。", "stopped": False}
        try:
            self._server_proc.terminate()
            self._server_proc.wait(timeout=5)
        except Exception as e:
            try:
                self._server_proc.kill()
            except Exception:
                pass
            return {"ok": False, "reply": f"⚠️  停止时出错：{e}", "stopped": False}
        self._server_proc = None
        return {"ok": True, "reply": "🛑  server 已停止。", "stopped": True}

    def execute_via_server(
        self,
        intent: str,
        dry_run: bool = False,
        timeout: float = 15.0,
        host: str = None,
        port: int = None,
    ) -> Dict[str, Any]:
        """通过 HTTP API server 执行指令（等效于原 Skill 的 nl2shortcut_execute）。

        适合跨进程 / 跨语言调用（如 QClaw、Claude Computer Use、智谱 AutoGLM）。
        """
        host = host or self.server_host
        port = port or self.server_port
        url = EXECUTE_URL_TMPL.format(host=host, port=port)
        payload = json.dumps({
            "intent": intent.strip(),
            "dry_run": bool(dry_run),
            "timeout_s": float(timeout),
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8") if e.fp else ""
            try:
                body_json = json.loads(body)
            except Exception:
                body_json = {"raw": body[:500]}
            return {
                "ok": False,
                "error": {"code": f"HTTP_{e.code}", "message": str(e.reason),
                          "detail": body_json},
                "server_online": True,
            }
        except urllib.error.URLError:
            return {
                "ok": False,
                "error": {
                    "code": "CONNECTION_REFUSED",
                    "message": (
                        f"无法连接到 NL2Shortcut server（{host}:{port}）。"
                        f"请先调用 start_server() 启动服务。"
                    ),
                },
                "server_online": False,
            }

    # ── 工具 ────────────────────────────────────────────────────────────
    @staticmethod
    def _startupinfo():
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            return si
        return None

    def __repr__(self) -> str:
        return f"<{self.NAME} v{self.VERSION} llm={'on' if self.agent.llm_available else 'off'}>"
