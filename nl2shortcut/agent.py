"""nl2shortcut main agent class."""

import time
import sys
from pathlib import Path
from typing import Optional, List

from .models import (
    Shortcut, IntentResult, ExecutionResult, Stats, Platform,
    WorkflowResult, AppContext,
)
from .database import DatabaseManager
from .intent import IntentEngine
from .adapter import create_adapter, KeyboardAdapter
from .logger import Logger
from .llm import DeepSeekEngine, _load_api_key
from .context import detect_context
from .workflow import WorkflowEngine
from .planner import GoalPlanner


class ShortcutAgent:
    """Natural language to keyboard shortcut agent.

    Usage:
        agent = ShortcutAgent()
        result = agent.execute("copy this text")
        print(result)

        # Dry run (shows keys without pressing)
        result = agent.execute("save file", dry_run=True)
    """

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        enable_spacy: bool = False,
        enable_llm: bool = True,
    ):
        if config_dir is None:
            config_dir = Path.home() / ".nl2shortcut"
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)

        self._db = DatabaseManager(self.config_dir / "shortcuts.db")
        self._db.seed_database()
        self._intent = IntentEngine(self._db)
        self._logger = Logger(self.config_dir / "logs")
        self._adapter: Optional[KeyboardAdapter] = None

        # DeepSeek LLM engine
        self._llm: Optional[DeepSeekEngine] = None
        self._llm_enabled = enable_llm
        if enable_llm:
            self._init_llm()

        # Lazy-loaded workflow engine
        self._workflow: Optional[WorkflowEngine] = None

        # GoalPlanner — multi-step intent decomposition
        self._planner: Optional[GoalPlanner] = None

        if enable_spacy:
            ok = self._intent.enable_spacy()
            if not ok:
                print(
                    "[nl2shortcut] spaCy models not found. "
                    "Install with: python -m spacy download en_core_web_sm",
                    file=sys.stderr,
                )

    @property
    def adapter(self) -> KeyboardAdapter:
        if self._adapter is None:
            self._adapter = create_adapter()
        return self._adapter

    @property
    def llm_available(self) -> bool:
        return self._llm is not None and self._llm.available

    @property
    def workflow(self) -> WorkflowEngine:
        if self._workflow is None:
            self._workflow = WorkflowEngine(self)
        return self._workflow

    @property
    def planner(self) -> GoalPlanner:
        """Lazy-loaded GoalPlanner for multi-step decomposition."""
        if self._planner is None:
            self._planner = GoalPlanner()
        return self._planner

    def get_context(self) -> AppContext:
        """Detect current active window / app context."""
        return detect_context()

    # ═══════════════════════════════════════════════════════════════
    # 键盘 + 鼠标 + 截图操作 (委托给 adapter)
    # ═══════════════════════════════════════════════════════════════

    def type_text(self, text: str, interval: float = 0.0) -> str:
        """Type raw text at current cursor position."""
        self.adapter.type_text(text, interval=interval)
        return f"typed {len(text)} chars"

    def click(self, x: Optional[int] = None, y: Optional[int] = None,
              button: str = "left", clicks: int = 1) -> str:
        """Mouse click at position (or current position if x/y omitted)."""
        self.adapter.click(x=x, y=y, button=button, clicks=clicks)
        pos = f"at ({x},{y})" if x is not None else "at current pos"
        return f"clicked {button} x{clicks} {pos}"

    def scroll(self, amount: int) -> str:
        """Mouse scroll (positive=up, negative=down)."""
        self.adapter.scroll(amount)
        direction = "up" if amount > 0 else "down"
        return f"scrolled {direction} {abs(amount)} clicks"

    def move_mouse(self, x: int, y: int, duration: float = 0.0) -> str:
        """Move mouse to (x,y) with optional animation duration."""
        self.adapter.move(x, y, duration=duration)
        return f"moved to ({x},{y})"

    def screenshot(self, path: Optional[str] = None) -> Optional[str]:
        """Capture full screen. Returns path if saved."""
        return self.adapter.screenshot(path=path)

    # ═══════════════════════════════════════════════════════════════
    # 剪贴板操作
    # ═══════════════════════════════════════════════════════════════

    def copy_selection(self) -> Optional[str]:
        """发送 Ctrl+C 复制当前选中内容，读取并返回剪贴板文本。

        用于剪贴板触发模式：用户选中文本 → 热键 → 自动复制到剪贴板。
        返回剪贴板文本，若剪贴板为空或非文本则返回 None。
        """
        try:
            self.adapter.send_keys("Ctrl+C")
            import time
            time.sleep(0.15)  # 等待应用响应
        except Exception:
            pass
        return self.read_clipboard()

    def read_clipboard(self) -> Optional[str]:
        """读取剪贴板中的文本内容（不改变剪贴板状态）。"""
        import subprocess
        import sys
        try:
            import pyperclip
            text = pyperclip.paste()
            if text and isinstance(text, str):
                return text.strip()
        except Exception:
            pass
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            text = root.clipboard_get()
            root.destroy()
            if text and isinstance(text, str):
                return text.strip()
        except Exception:
            pass
        try:
            # PowerShell 兜底
            r = subprocess.run(
                ["powershell", "-Command", "Get-Clipboard"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
        return None

    def paste_text(self, text: str) -> None:
        """将文本写入剪贴板并发送 Ctrl+V 粘贴。

        会先保存原始剪贴板内容，粘贴后尝试恢复。
        """
        try:
            import pyperclip
            old = pyperclip.paste()
            pyperclip.copy(text)
            import time
            time.sleep(0.05)
            self.adapter.send_keys("Ctrl+V")
            time.sleep(0.12)
            try:
                pyperclip.copy(old)
            except Exception:
                pass
        except Exception:
            # 回退：仅发送 Unicode
            self.adapter.type_text(text)

    # ═══════════════════════════════════════════════════════════════
    # 等保合规上下文（透传给 Logger）
    # ═══════════════════════════════════════════════════════════════

    def set_compliance_context(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        source_ip: Optional[str] = None,
        compliance_level: Optional[str] = None,
    ) -> None:
        """设置等保审计日志的合规上下文字段。"""
        self._logger.set_compliance_context(
            user_id=user_id,
            session_id=session_id,
            source_ip=source_ip,
            compliance_level=compliance_level,
        )

    # ═══════════════════════════════════════════════════════════════
    # LLM 文本处理（剪贴板触发模式）
    # ═══════════════════════════════════════════════════════════════

    def process_clipboard(
        self,
        instruction: str,
        clipboard_text: str,
    ) -> Optional[str]:
        """通过 LLM 处理剪贴板文本 + 用户指令，返回处理结果。

        用于剪贴板触发模式：用户选中文本 → 热键 → 输入指令 → LLM 处理。
        若 LLM 不可用返回 None。
        """
        if not self._llm_enabled or not self._llm or not self._llm.available:
            self._init_llm()
        if not self._llm or not self._llm.available:
            return None
        return self._llm.process_text(
            instruction=instruction,
            clipboard_text=clipboard_text,
        )

    def _init_llm(self) -> bool:
        """Initialize LLM engine. Returns True if LLM is ready."""
        key = _load_api_key()
        if not key:
            return False
        try:
            self._llm = DeepSeekEngine(self._db, api_key=key)
            return self._llm.available
        except Exception:
            return False

    def configure_llm(self, api_key: str) -> bool:
        """Set LLM API key and initialize engine."""
        if not self._llm:
            self._llm = DeepSeekEngine(self._db)
        return self._llm.configure(api_key)

    def execute(
        self,
        text: str,
        dry_run: bool = False,
        timeout: float = 5.0,
    ) -> ExecutionResult:
        """Execute a shortcut from natural language.

        Args:
            text: Natural language command (e.g. "copy this text")
            dry_run: Show keys without pressing
            timeout: Max execution time in seconds

        Returns:
            ExecutionResult with status and timing
        """
        start = time.perf_counter()

        # Step 1: Recognize intent
        intent_result = self.recognize_intent(text)

        # ── 复合操作通道（文件复制/移动等需视觉分步执行）──
        if intent_result.command == "__composite__" and intent_result.composite_plan:
            elapsed = time.perf_counter() - start
            plan = intent_result.composite_plan
            if dry_run:
                result = ExecutionResult(
                    success=True,
                    intent=intent_result.intent,
                    command="__composite__",
                    confidence=intent_result.confidence,
                    mode="composite",
                    composite_plan=plan,
                    processing_time=elapsed,
                    matched_keyword=intent_result.matched_keyword,
                    dry_run=True,
                )
                self._logger.log_execution(result)
                return result

            # Real execution: run CompositeExecutor step by step
            from .composites import CompositeExecutor
            try:
                executor = CompositeExecutor(adapter=self.adapter)
                step_results = executor.execute(plan, dry_run=False)
                all_ok = all(r["success"] for r in step_results)
                executed_steps = [f"{r['kind']}:{r['message'][:30]}" for r in step_results]
                result = ExecutionResult(
                    success=all_ok,
                    intent=intent_result.intent,
                    command="__composite__",
                    confidence=intent_result.confidence,
                    mode="composite",
                    composite_plan=plan,
                    key_combination=" → ".join(executed_steps[:8]),
                    processing_time=time.perf_counter() - start,
                    matched_keyword=intent_result.matched_keyword,
                    error=None if all_ok else "; ".join(
                        r["message"] for r in step_results if not r["success"]
                    )[:200],
                )
            except Exception as e:
                result = ExecutionResult(
                    success=False,
                    intent=intent_result.intent,
                    command="__composite__",
                    mode="composite",
                    composite_plan=plan,
                    processing_time=time.perf_counter() - start,
                    error=f"composite execution failed: {e}",
                )
            self._logger.log_execution(result)
            return result

        # ── 多步骤分解通道（GoalPlanner）──
        # 检测多意图标记
        _MULTI_MARKERS = {
            "并", "和", "然后", "接着", "再", "之后", "同时",
            "并且", "以及", "再把", "顺便",
            "and", "then", "after", "also", "plus",
        }
        is_multi = any(m in text for m in _MULTI_MARKERS)

        # 文件操作模式检测："X到Y" 需要上下文切换 + 复合操作
        _FOLDER_KEYWORDS = {"桌面", "下载", "文档", "图片", "视频", "音乐", "C盘", "D盘",
                           "desktop", "downloads", "documents", "pictures", "videos",
                           "music", "public", "文件夹", "folder"}
        has_file_dest = (
            ("到" in text or "至" in text)
            and any(kw in text.lower() for kw in _FOLDER_KEYWORDS)
        )

        # 触发条件：
        #   (a) LLM 返回了 __plan__（旧路径）→ 转为 GoalPlanner 分解
        #   (b) 多意图标记 + 本地置信度不够（被打折后 < 0.75）
        #   (c) 文件操作模式 + 低置信度 → 需要复合分解
        needs_decompose = (
            (intent_result.command == "__plan__" and intent_result.alternatives)
            or (is_multi and intent_result.confidence < 0.75)
            or (has_file_dest and intent_result.confidence < 0.80)
        )

        if needs_decompose:
            # 确保 LLM 已初始化
            if not self._llm_enabled or not self._llm or not self._llm.available:
                self._init_llm()

            try:
                plan = self.planner.plan(text)
                if not plan or not plan.steps:
                    # 退化为单命令执行
                    pass  # 继续走下面的普通流程
                else:
                    if dry_run:
                        step_summary = [
                            f"{s.action}({(s.description or s.key_combination or s.command or '')})"
                            for s in plan.steps
                        ]
                        elapsed = time.perf_counter() - start
                        result = ExecutionResult(
                            success=True,
                            intent=text,
                            confidence=intent_result.confidence,
                            command="__multi_step__",
                            key_combination=" → ".join(step_summary[:8]),
                            platform=Platform.detect().value,
                            processing_time=elapsed,
                            mode="goal_planner_dryrun",
                            matched_keyword=intent_result.matched_keyword,
                            dry_run=True,
                        )
                        self._logger.log_execution(result)
                        return result

                    step_results = self.planner.execute_plan(plan, dry_run=False)
                    all_ok = all(r.success for r in step_results)
                    step_descs = [
                        f"{r.command}[{r.key_combination}]"
                        for r in step_results
                    ]
                    errors = [r.error for r in step_results if r.error]

                    elapsed = time.perf_counter() - start
                    result = ExecutionResult(
                        success=all_ok,
                        intent=text,
                        confidence=intent_result.confidence,
                        command="__multi_step__",
                        key_combination=" → ".join(step_descs[:8]),
                        platform=Platform.detect().value,
                        processing_time=elapsed,
                        error="; ".join(errors) if errors else None,
                        matched_keyword=intent_result.matched_keyword,
                        mode="goal_planner",
                    )
                    self._logger.log_execution(result)
                    return result
            except Exception as e:
                elapsed = time.perf_counter() - start
                # GoalPlanner失败时尝试旧LLM fallback（兼容性）
                if intent_result.command == "__plan__" and intent_result.alternatives:
                    # 仍回退旧的__plan__逻辑：只跑已知命令
                    plan_steps = intent_result.alternatives
                    executed_steps = []
                    all_errors = []
                    platform = Platform.detect()
                    for i, step in enumerate(plan_steps):
                        cmd = step.command
                        s = self._db.get_by_command(cmd)
                        if s is None:
                            all_errors.append(f"plan step {i+1}: unknown '{cmd}'")
                            continue
                        key = s.get_key(platform)
                        if not key:
                            all_errors.append(f"plan step {i+1}: no key for '{cmd}'")
                            continue
                        if not dry_run:
                            try:
                                self.adapter.send_keys(key)
                                self._db.increment_frequency(cmd)
                            except Exception as ex:
                                all_errors.append(f"plan step {i+1}({cmd}): {ex}")
                        executed_steps.append(f"{cmd}→{key}")
                    result = ExecutionResult(
                        success=len(all_errors) == 0,
                        intent=intent_result.intent,
                        confidence=intent_result.confidence,
                        command=text,
                        key_combination=" → ".join(executed_steps),
                        platform=platform.value,
                        processing_time=elapsed,
                        error="; ".join(all_errors) if all_errors else None,
                        matched_keyword=intent_result.matched_keyword,
                        mode="llm_plan_fallback",
                    )
                else:
                    result = ExecutionResult(
                        success=False,
                        intent=text,
                        command="__multi_step__",
                        confidence=intent_result.confidence,
                        platform=Platform.detect().value,
                        processing_time=elapsed,
                        error=f"multi-step plan failed: {e}",
                        mode="goal_planner_error",
                    )
                self._logger.log_execution(result)
                return result

        # ── 鼠标左键点击（空格 / 点击）──
        # 映射到 adapter.click()，不经过数据库键位查找
        if intent_result.command == "left_click":
            elapsed = time.perf_counter() - start
            if dry_run:
                result = ExecutionResult(
                    success=True,
                    intent=intent_result.intent,
                    confidence=intent_result.confidence,
                    command="left_click",
                    key_combination="[left_click] 左键点击 (dry_run)",
                    platform=Platform.detect().value,
                    processing_time=elapsed,
                    matched_keyword=intent_result.matched_keyword,
                    mode="mouse_click",
                    dry_run=True,
                )
            else:
                try:
                    self.adapter.click(button="left", clicks=1)
                    result = ExecutionResult(
                        success=True,
                        intent=intent_result.intent,
                        confidence=intent_result.confidence,
                        command="left_click",
                        key_combination="[left_click] 鼠标左键点击",
                        platform=Platform.detect().value,
                        processing_time=elapsed,
                        matched_keyword=intent_result.matched_keyword,
                        mode="mouse_click",
                    )
                except Exception as e:
                    result = ExecutionResult(
                        success=False,
                        intent=intent_result.intent,
                        confidence=intent_result.confidence,
                        command="left_click",
                        platform=Platform.detect().value,
                        processing_time=elapsed,
                        error=f"left_click failed: {e}",
                        matched_keyword=intent_result.matched_keyword,
                        mode="mouse_click",
                    )
            self._logger.log_execution(result)
            return result

        if intent_result.confidence < 0.5:
            elapsed = time.perf_counter() - start
            err = (
                f"Low confidence ({intent_result.confidence:.2f}). "
                f"Did you mean '{intent_result.command}'?"
                if intent_result.command
                else f"Could not understand: '{text}'"
            )
            result = ExecutionResult(
                success=False,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                command=intent_result.command,
                processing_time=elapsed,
                error=err,
                matched_keyword=intent_result.matched_keyword,
            )
            self._logger.log_execution(result)
            return result

        # Step 2: Look up shortcut
        shortcut = self._db.get_by_command(intent_result.command)
        if shortcut is None:
            elapsed = time.perf_counter() - start
            result = ExecutionResult(
                success=False,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                command=intent_result.command,
                processing_time=elapsed,
                error=f"No shortcut mapping for: {intent_result.command}",
                matched_keyword=intent_result.matched_keyword,
            )
            self._logger.log_execution(result)
            return result

        platform = Platform.detect()
        key_combination = shortcut.get_key(platform)
        if not key_combination:
            elapsed = time.perf_counter() - start
            result = ExecutionResult(
                success=False,
                intent=intent_result.intent,
                confidence=intent_result.confidence,
                command=intent_result.command,
                platform=platform.value,
                processing_time=elapsed,
                error=f"No {platform.value} key mapping for: {intent_result.command}",
                matched_keyword=intent_result.matched_keyword,
            )
            self._logger.log_execution(result)
            return result

        # Step 3: Execute or dry-run
        # 复制/剪切前缀：在资源管理器等列表场景，需先用 Space 勾选/选中
        # 高亮项，再发送 Ctrl+C / Ctrl+X，否则会复制空内容或焦点所在文本。
        _SPACE_FIRST = ("copy", "cut")
        needs_space = intent_result.command in _SPACE_FIRST

        error = None
        if not dry_run:
            try:
                if needs_space and self.adapter:
                    self.adapter.send_keys("Space")
                self.adapter.send_keys(key_combination)
                self._db.increment_frequency(intent_result.command)
            except Exception as e:
                error = str(e)

        elapsed = time.perf_counter() - start
        # 展示用键位：复制/剪切时显式标出前置的 Space 勾选
        display_key = f"Space → {key_combination}" if needs_space else key_combination
        result = ExecutionResult(
            success=error is None,
            intent=intent_result.intent,
            confidence=intent_result.confidence,
            command=intent_result.command,
            key_combination=display_key,
            platform=platform.value,
            processing_time=elapsed,
            error=error,
            matched_keyword=intent_result.matched_keyword,
            dry_run=dry_run,
        )
        self._logger.log_execution(result)

        if self._logger.consecutive_failures >= 3:
            print(
                f"[nl2shortcut] Warning: {self._logger.consecutive_failures} "
                "consecutive failures.",
                file=sys.stderr,
            )

        return result

    def auto_save_workflow(self, result: ExecutionResult, text: str, dry_run: bool = False):
        """Save every execution (success or failure) as a reusable workflow."""
        import yaml, re
        from pathlib import Path

        try:
            wf_dir = Path.home() / ".nl2shortcut" / "workflows"
            wf_dir.mkdir(parents=True, exist_ok=True)

            # Sanitize name from intent text
            name = text.lower().strip()
            name = re.sub(r'[^\w\s-]', '', name)
            name = re.sub(r'[-\s]+', '-', name).strip('-')[:50]
            if not name:
                name = "unnamed"

            # Add status suffix
            status_suffix = "-ok" if result.success else "-failed"
            name = name[:45] + status_suffix

            filepath = wf_dir / f"{name}.yaml"

            # Build steps from the execution
            steps = []
            if result.mode == "llm_plan" and result.key_combination:
                for part in result.key_combination.split(" → "):
                    if "→" in part:
                        cmd, key = part.split("→", 1)
                        steps.append({"name": cmd.strip(), "action": "shortcut", "command": key.strip()})
            elif result.mode == "composite" and result.composite_plan:
                for s in result.composite_plan.steps:
                    cmd = s.text or s.keys or str(s.wait_ms) + "ms" or s.intent or ""
                    act = s.kind if s.kind in ("key", "type", "wait", "shell") else "shortcut"
                    if act == "key":
                        act = "shortcut"
                    steps.append({"name": s.description[:40], "action": act, "command": cmd})
            elif result.key_combination:
                steps.append({"name": text[:40], "action": "shortcut", "command": result.key_combination})
            else:
                steps.append({"name": text[:40], "action": "shortcut", "command": result.command or text})

            doc = {
                "name": name,
                "description": f"[{'OK' if result.success else 'FAILED'}] {text}" + (" (dry-run)" if dry_run else ""),
                "version": "1.0",
                "variables": {},
                "steps": steps,
            }

            # Always overwrite with latest execution of this intent
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"# Auto-saved from execution: {text}\n")
                f.write(f"# Status: {'success' if result.success else 'failed'}, Mode: {result.mode}\n")
                if result.error:
                    f.write(f"# Error: {result.error}\n")
                yaml.dump(doc, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        except Exception:
            pass  # never crash on workflow save

    def recognize_intent(self, text: str) -> IntentResult:
        # ── 协作模式：本地引擎优先 ──
        # 简单/明确指令 → 本地关键词+同义词+模糊匹配（< 5ms）
        # 复杂/歧义指令 → DeepSeek 兜底理解 + 分步执行

        # 多意图检测器：含连接词的复合句强制走 LLM
        _MULTI_INTENT_MARKERS = {
            "并", "和", "然后", "接着", "再", "之后", "同时",
            "并且", "以及", "然后", "再把", "顺便",
            "and", "then", "after", "also", "plus",
        }
        is_multi_intent = any(m in text for m in _MULTI_INTENT_MARKERS)

        local_result = self._intent.recognize(text)

        # 复合计划（__composite__）不需要 LLM 再分解，跳过打折
        if is_multi_intent and local_result.command != "__composite__":
            local_result.confidence *= 0.35  # 大幅打折，强制走 LLM

        if local_result.confidence >= 0.75:
            # 高置信度：本地引擎直接搞定，零 API 调用
            return local_result

        if self._llm_enabled and self._llm and self._llm.available:
            # 本地兜不住 → 投喂 DeepSeek
            llm_result = self._llm.recognize(text)
            if llm_result and llm_result.confidence >= 0.4:
                return llm_result
            # LLM 也低置信 → 尝试本地低置信结果（> 0.4 的仍可用）
            if local_result.confidence >= 0.4 and local_result.command:
                return local_result

        return local_result

    def list_shortcuts(self, category: Optional[str] = None) -> List[Shortcut]:
        return self._db.get_all(category=category)

    def list_workflows(self) -> list:
        """List available workflow names."""
        return self.workflow.list_workflows()

    def run_workflow(
        self, name: str, variables: dict = None, dry_run: bool = False
    ) -> WorkflowResult:
        """Execute a named workflow."""
        return self.workflow.run(name, variables=variables, dry_run=dry_run)

    def search_shortcuts(self, keyword: str) -> List[Shortcut]:
        return self._db.search(keyword)

    def add_shortcut(self, shortcut: Shortcut) -> bool:
        return self._db.add_shortcut(shortcut)

    def get_stats(self) -> Stats:
        log_stats = self._logger.get_stats()
        _, top_cmds = self._db.get_stats()
        log_stats.top_commands = top_cmds
        return log_stats

    def reset_stats(self) -> None:
        self._logger.reset_stats()
        self._db.reset_frequency()
