"""工作流引擎 —— 由 YAML 定义的多步骤自动化。

工作流 DSL：
  name: "Deploy"
  description: "Git commit + push"
  variables:
    message: "auto update"
  steps:
    - name: Stage all
      action: shell
      command: git add .
    - name: Commit
      action: shell
      command: git commit -m "$message"

支持的动作：
  shortcut   — 经由适配器发送键盘快捷键
  type       — 输入原始文本
  click      — 鼠标点击（左键/右键/中键）
  scroll     — 鼠标滚轮滚动
  screenshot — 屏幕截图
  shell      — 执行 shell 命令，捕获 stdout
  http       — REST API 调用（GET/POST）
  file       — 读取/写入文件
  python     — 执行 Python 表达式
  wait       — 休眠 N 秒
  condition  — if/else 分支
"""

import re
import time
import yaml
from pathlib import Path
from typing import Optional, Dict, Any

from .models import (
    WorkflowStep,
    WorkflowDefinition,
    StepResult,
    WorkflowResult,
)


class WorkflowEngine:
    """解析并执行 YAML 工作流定义。

    Usage:
        engine = WorkflowEngine(agent, workflows_dir=Path("~/.nl2shortcut/workflows"))
        result = engine.run("deploy", variables={"message": "fix bug"})
    """

    def __init__(self, agent, workflows_dir: Optional[Path] = None):
        from .agent import ShortcutAgent
        self._agent: ShortcutAgent = agent
        self._workflows_dir = workflows_dir or (Path.home() / ".nl2shortcut" / "workflows")
        self._workflows_dir.mkdir(parents=True, exist_ok=True)
        # Also scan the project-local .nl2shortcut/workflows/ directory
        self._local_dir = Path.cwd() / ".nl2shortcut" / "workflows"
        self._tools = _ToolRegistry(agent)

    def _all_dirs(self) -> list:
        """返回所有要扫描的工作流目录（本地优先）。"""
        dirs = []
        if self._local_dir.exists():
            dirs.append(self._local_dir)
        if self._workflows_dir.exists() and self._workflows_dir != self._local_dir:
            dirs.append(self._workflows_dir)
        return dirs

    def list_workflows(self) -> list:
        """列出所有可用的工作流名称（合并本地与全局目录）。"""
        names = set()
        for d in self._all_dirs():
            for p in d.glob("*.yaml"):
                names.add(p.stem)
        return list(names)

    def load(self, name: str) -> Optional[WorkflowDefinition]:
        """按名称加载工作流（不含 .yaml 扩展名）。优先从本地目录加载。"""
        path = None
        for d in self._all_dirs():
            for ext in (".yaml", ".yml"):
                p = d / f"{name}{ext}"
                if p.exists():
                    path = p
                    break
            if path:
                break
        if path is None:
            return None

        import sys

        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                raw = yaml.safe_load(f)
        except Exception as e:
            print(f"[nl2shortcut] Failed to parse workflow {name}: {e}", file=sys.stderr)
            return None

        if not raw:
            print(f"[nl2shortcut] Workflow {name} parsed as None/empty (check for YAML syntax errors)", file=sys.stderr)
            return None
        if "steps" not in raw:
            print(f"[nl2shortcut] Workflow {name} has no 'steps' key", file=sys.stderr)
            return None

        steps = []
        for s in raw.get("steps", []):
            steps.append(WorkflowStep(
                name=s.get("name", "unnamed"),
                action=s.get("action", "shortcut"),
                command=s.get("command", ""),
                args=s.get("args", {}),
                capture=s.get("capture", ""),
                condition=s.get("condition", ""),
                retry=s.get("retry", 0),
                timeout=s.get("timeout", 10.0),
            ))

        return WorkflowDefinition(
            name=raw.get("name", name),
            description=raw.get("description", ""),
            version=str(raw.get("version", "1.0")),
            steps=steps,
            variables=raw.get("variables", {}),
            source_path=str(path),
        )

    def run(
        self,
        name: str,
        variables: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> WorkflowResult:
        """按名称执行一个工作流。"""
        wf = self.load(name)
        if wf is None:
            return WorkflowResult(
                workflow_name=name,
                success=False,
                error=f"Workflow not found: {name}",
            )

        return self.execute(wf, variables=variables, dry_run=dry_run)

    def execute(
        self,
        wf: WorkflowDefinition,
        variables: Optional[Dict[str, Any]] = None,
        dry_run: bool = False,
    ) -> WorkflowResult:
        """执行一个已加载的工作流定义。"""
        start = time.perf_counter()
        ctx: Dict[str, Any] = dict(wf.variables)
        if variables:
            ctx.update(variables)

        step_results: list[StepResult] = []
        overall_success = True

        for step in wf.steps:
            # 检查条件
            if step.condition:
                try:
                    # 条件求值：暴露 ctx 字典 + 安全内置函数
                    _safe_builtins = {"int": int, "str": str, "bool": bool, "float": float, "len": len, "ctx": ctx}
                    if not eval(step.condition, {"__builtins__": _safe_builtins}, ctx):
                        step_results.append(StepResult(
                            step_name=step.name,
                            success=True,
                            output="skipped (condition false)",
                        ))
                        continue
                except Exception as e:
                    step_results.append(StepResult(
                        step_name=step.name,
                        success=False,
                        error=f"Condition eval failed: {e}",
                    ))
                    overall_success = False
                    break

            # 在 command 与 args.content 中做变量插值
            cmd = self._interpolate(step.command, ctx)
            args = {k: self._interpolate(v, ctx) if isinstance(v, str) else v
                    for k, v in step.args.items()}

            # 带重试地执行
            result = None
            for attempt in range(step.retry + 1):
                result = self._execute_step(step, cmd, dry_run, args)
                if result.success:
                    break
                if attempt < step.retry:
                    time.sleep(0.5)

            if result:
                if result.output and step.capture and not dry_run:
                    ctx[step.capture] = result.output.strip()
                step_results.append(result)
                if not result.success:
                    overall_success = False
                    # 继续下一步（尽力而为），不要中断

        elapsed = (time.perf_counter() - start) * 1000
        return WorkflowResult(
            workflow_name=wf.name,
            success=overall_success,
            steps=step_results,
            variables=ctx,
            total_duration_ms=elapsed,
        )

    def _execute_step(
        self, step: WorkflowStep, cmd: str, dry_run: bool,
        args: Optional[Dict[str, Any]] = None,
    ) -> StepResult:
        """执行单个工作流步骤。"""
        start = time.perf_counter()
        try:
            if step.action == "shortcut":
                output = self._tools.shortcut(cmd, dry_run)
            elif step.action == "type":
                interval = (step.args or {}).get("interval", 0.0)
                output = self._tools.type_text(cmd, dry_run, interval)
            elif step.action == "click":
                output = self._tools.click(cmd, dry_run, step.args or {})
            elif step.action == "scroll":
                output = self._tools.scroll(cmd, dry_run)
            elif step.action == "screenshot":
                output = self._tools.screenshot(cmd, dry_run)
            elif step.action == "shell":
                output = self._tools.shell(cmd, dry_run, step.timeout)
            elif step.action == "http":
                output = self._tools.http(cmd, step.args, dry_run)
            elif step.action == "file":
                output = self._tools.file_io(cmd, args or step.args, dry_run)
            elif step.action == "python":
                output = self._tools.python_eval(cmd, dry_run)
            elif step.action == "wait":
                output = self._tools.wait(cmd, dry_run)
            elif step.action == "condition":
                output = self._tools.condition(cmd, dry_run)
            else:
                return StepResult(
                    step_name=step.name,
                    success=False,
                    error=f"Unknown action: {step.action}",
                )
        except Exception as e:
            return StepResult(
                step_name=step.name,
                success=False,
                error=str(e),
                duration_ms=(time.perf_counter() - start) * 1000,
            )

        return StepResult(
            step_name=step.name,
            success=True,
            output=output or "",
            duration_ms=(time.perf_counter() - start) * 1000,
        )

    def _interpolate(self, text: str, ctx: dict) -> str:
        """将 $var、${var} 以及 ${ENV_VAR} 替换为对应的值。

        解析顺序：工作流 ctx → 环境变量 → 原始文本。
        """
        import os as _os

        mix = {**ctx}  # ctx wins over env
        def replacer(m):
            var = m.group(1)
            # Check ctx first, then environment
            if var in ctx:
                return str(ctx[var])
            env_val = _os.environ.get(var)
            if env_val is not None:
                return env_val
            return m.group(0)
        return re.sub(r'\$\{?(\w+)\}?', replacer, text)


# ═══════════════════════════════════════════════════════════════════════
# 工具注册表 —— 可插拔的动作处理器
# ═══════════════════════════════════════════════════════════════════════

class _ToolRegistry:
    """工作流步骤注册的工具动作。"""

    def __init__(self, agent):
        self._agent = agent

    def shortcut(self, cmd: str, dry_run: bool) -> str:
        """直接通过适配器发送键盘快捷键（不经过 NL 意图识别）。"""
        if dry_run:
            return f"[dry-run] shortcut: {cmd}"
        self._agent.adapter.send_keys(cmd)
        return f"Pressed {cmd}"

    def type_text(self, text: str, dry_run: bool, interval: float = 0.0) -> str:
        """经由适配器输入原始文本。"""
        if dry_run:
            return f"[dry-run] type: {repr(text)}"
        return self._agent.type_text(text, interval=interval)

    def click(self, cmd: str, dry_run: bool, args: dict = None) -> str:
        """鼠标点击。cmd 为 'left'/'right'/'middle'，或 'left 2' 表示双击。"""
        args = args or {}
        parts = cmd.strip().split()
        button = parts[0] if parts else "left"
        clicks = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        x = args.get("x")
        y = args.get("y")
        if dry_run:
            pos = f"at ({x},{y})" if x is not None else "at current"
            return f"[dry-run] click {button} x{clicks} {pos}"
        return self._agent.click(x=x, y=y, button=button, clicks=clicks)

    def scroll(self, amount: str, dry_run: bool) -> str:
        """滚动鼠标滚轮。"""
        try:
            n = int(amount)
        except ValueError:
            n = 3
        if dry_run:
            return f"[dry-run] scroll {n}"
        return self._agent.scroll(n)

    def screenshot(self, path: str, dry_run: bool) -> str:
        """截屏。"""
        if dry_run:
            return f"[dry-run] screenshot -> {path or '<clipboard>'}"
        p = path.strip() if path and path.strip() else None
        result = self._agent.screenshot(path=p)
        return f"screenshot saved to {result}" if result else "screenshot captured"

    def shell(self, cmd: str, dry_run: bool, timeout: float = 10) -> str:
        """执行 shell 命令。"""
        if dry_run:
            return f"[dry-run] shell: {cmd}"
        import subprocess
        try:
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=timeout,
            )
            return r.stdout or r.stderr or f"exit {r.returncode}"
        except subprocess.TimeoutExpired:
            return f"timeout after {timeout}s"

    def http(self, url: str, args: dict, dry_run: bool) -> str:
        """发起一个 HTTP 请求。"""
        if dry_run:
            return f"[dry-run] http: {args.get('method','GET')} {url}"

        import urllib.request
        import json as _json

        method = args.get("method", "GET").upper()
        headers = args.get("headers", {})
        body = args.get("body")

        data = _json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=args.get("timeout", 10)) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return f"HTTP error: {e}"

    def file_io(self, path: str, args: dict, dry_run: bool) -> str:
        """读取或写入文件。"""
        import os as _os
        mode = args.get("mode", "r")
        # 解析路径中的环境变量（${TEMP} → C:\Users\...\Temp）
        resolved = _os.path.expandvars(path)
        if dry_run:
            return f"[dry-run] file {mode}: {resolved}"

        p = Path(resolved).expanduser()
        if mode == "r" or mode == "read":
            return p.read_text(encoding=args.get("encoding", "utf-8"))
        elif mode in ("w", "write", "a", "append"):
            content = args.get("content", "")
            p.parent.mkdir(parents=True, exist_ok=True)
            if mode in ("a", "append"):
                with open(p, "a", encoding=args.get("encoding", "utf-8")) as f:
                    f.write(content)
            else:
                p.write_text(content, encoding=args.get("encoding", "utf-8"))
            return f"wrote {len(content)} bytes to {path}"
        return "unknown file mode"

    def python_eval(self, expr: str, dry_run: bool) -> str:
        """执行一个 Python 表达式。"""
        if dry_run:
            return f"[dry-run] python: {expr}"
        try:
            import json, re, datetime, math
            safe = {
                "json": json, "re": re, "datetime": datetime,
                "math": math, "len": len, "str": str, "int": int,
                "float": float, "list": list, "dict": dict,
                "sum": sum, "ord": ord, "chr": chr, "range": range,
                "min": min, "max": max, "abs": abs, "round": round,
                "bool": bool, "True": True, "False": False, "None": None,
            }
            result = eval(expr, {"__builtins__": safe}, {})
            return str(result)
        except Exception as e:
            return f"python eval error: {e}"

    def wait(self, seconds: str, dry_run: bool) -> str:
        """休眠 N 秒。支持 '1.5'、'1500ms'、'2s' 等格式。"""
        s_raw = str(seconds).strip().lower()
        # Strip common suffixes
        for suffix in ("ms", "s"):
            if s_raw.endswith(suffix):
                s_raw = s_raw[:-len(suffix)]
                break
        try:
            s = float(s_raw)
        except ValueError:
            s = 1.0
        # If original had "ms" suffix, convert from milliseconds
        s_original = str(seconds).strip().lower()
        if s_original.endswith("ms"):
            s = s / 1000.0
        if dry_run:
            return f"[dry-run] wait {s:.3f}s"
        time.sleep(s)
        return f"waited {s:.3f}s"

    def condition(self, expr: str, dry_run: bool) -> str:
        """求值一个条件（返回 'true' 或 'false'）。"""
        return str(bool(eval(expr, {"__builtins__": {}}, {})))
