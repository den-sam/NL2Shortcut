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
        """列出所有可用的工作流（文件名 stem）。"""
        names = set()
        for d in self._all_dirs():
            for p in d.glob("*.yaml"):
                names.add(p.stem)
            for p in d.glob("*.yml"):
                names.add(p.stem)
        return list(names)

    def _resolve_path(self, name: str) -> Optional[Path]:
        """按 name 解析工作流文件路径。

        查找顺序：
          1. 直接按文件名（不含扩展名）匹配 .yaml / .yml
          2. 扫描所有工作流，匹配 YAML 内部的 ``name:`` 字段（显示名/中文名）
        返回命中的路径，找不到返回 None。
        """
        # 1) 文件名优先
        for d in self._all_dirs():
            for ext in (".yaml", ".yml"):
                p = d / f"{name}{ext}"
                if p.exists():
                    return p

        # 2) 回退：按内部 name: 字段匹配
        for d in self._all_dirs():
            for p in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
                try:
                    with open(p, "r", encoding="utf-8-sig") as f:
                        raw = yaml.safe_load(f)
                    if raw and raw.get("name") == name:
                        return p
                except Exception:
                    continue
        return None

    def load(self, name: str) -> Optional[WorkflowDefinition]:
        """按名称加载工作流。

        ``name`` 可以是文件名（不含 .yaml/.yml），也可以是 YAML 内部的
        ``name:`` 字段（如中文显示名）。优先从本地目录加载。
        """
        path = self._resolve_path(name)
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
                loop=s.get("loop", ""),
                loop_var=s.get("loop_var", "item"),
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

    # while 循环最大迭代数（防止死循环）
    _MAX_LOOP_ITER = 1000

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
            # ── 第2步：循环结构 ──────────────────────────────────────
            # step.loop 为空 → 单次执行（含 condition 检查）
            # step.loop 非空 → 按 iterable 迭代，condition 在每次迭代内部求值
            #   （此时 $item 已注入 ctx，可基于循环变量做条件跳过）
            if step.loop:
                results = self._execute_loop_step(step, ctx, dry_run)
                step_results.extend(results)
                if any(not r.success for r in results):
                    overall_success = False
                continue

            # 单次执行路径：先检查 condition（无循环变量可用）
            if step.condition:
                try:
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

            result = self._run_step_once(step, ctx, dry_run)
            step_results.append(result)
            if not result.success:
                overall_success = False

        elapsed = (time.perf_counter() - start) * 1000
        return WorkflowResult(
            workflow_name=wf.name,
            success=overall_success,
            steps=step_results,
            variables=ctx,
            total_duration_ms=elapsed,
        )

    def _run_step_once(
        self, step: WorkflowStep, ctx: Dict[str, Any], dry_run: bool,
    ) -> StepResult:
        """单次执行一个步骤（无循环）：变量插值 + 重试。"""
        cmd = self._interpolate(step.command, ctx)
        args = {k: self._interpolate(v, ctx) if isinstance(v, str) else v
                for k, v in step.args.items()}

        result = None
        for attempt in range(step.retry + 1):
            result = self._execute_step(step, cmd, dry_run, args, ctx=ctx)
            if result.success:
                break
            if attempt < step.retry:
                time.sleep(0.5)

        if result and result.output and step.capture and not dry_run:
            ctx[step.capture] = result.output.strip()
        return result

    def _execute_loop_step(
        self, step: WorkflowStep, ctx: Dict[str, Any], dry_run: bool,
    ) -> list:
        """循环执行一个步骤。

        支持：
          - "range(5)"     → for i in range(5)
          - "range(1,10)"  → for i in range(1, 10)
          - "ctx['rows']"  → for item in ctx['rows']
          - "while expr"   → while eval(expr): run step

        while 循环有 _MAX_LOOP_ITER 保护。
        每次迭代把当前循环变量写入 ctx[step.loop_var]。
        每次迭代的执行结果单独作为一个 StepResult 返回（带 [i/N] 后缀）。
        """
        loop_expr = (step.loop or "").strip()
        results: list = []

        # 解析 loop 表达式得到 iterable
        _safe_builtins = {
            "range": range, "len": len, "ctx": ctx,
            "int": int, "str": str, "list": list,
        }

        if loop_expr.startswith("while "):
            # while 循环
            cond_expr = loop_expr[len("while "):].strip()
            iter_count = 0
            while iter_count < self._MAX_LOOP_ITER:
                try:
                    keep = bool(eval(cond_expr, {"__builtins__": _safe_builtins}, ctx))
                except Exception as e:
                    results.append(StepResult(
                        step_name=step.name,
                        success=False,
                        error=f"while condition eval failed: {e}",
                    ))
                    return results
                if not keep:
                    break
                ctx[step.loop_var] = iter_count
                r = self._run_step_once(step, ctx, dry_run)
                r.step_name = f"{step.name} [{iter_count}]"
                results.append(r)
                if not r.success:
                    return results
                iter_count += 1
            if iter_count >= self._MAX_LOOP_ITER:
                results.append(StepResult(
                    step_name=step.name,
                    success=False,
                    error=f"loop exceeded _MAX_LOOP_ITER ({self._MAX_LOOP_ITER})",
                ))
            return results

        # 普通 for 循环：求值得到 iterable
        try:
            iterable = eval(loop_expr, {"__builtins__": _safe_builtins}, ctx)
        except Exception as e:
            results.append(StepResult(
                step_name=step.name,
                success=False,
                error=f"loop expression eval failed: {e}",
            ))
            return results

        try:
            n = len(iterable)
        except TypeError:
            n = None  # 生成器/迭代器，无法预知长度

        for i, item in enumerate(iterable):
            ctx[step.loop_var] = item
            # 循环内部检查 condition（此时 $item 已注入 ctx）
            if step.condition:
                try:
                    _safe_builtins = {"int": int, "str": str, "bool": bool, "float": float,
                                      "len": len, "ctx": ctx}
                    if not eval(step.condition, {"__builtins__": _safe_builtins}, ctx):
                        suffix = f" [{i+1}/{n}]" if n is not None else f" [{i+1}]"
                        results.append(StepResult(
                            step_name=f"{step.name}{suffix}",
                            success=True,
                            output="skipped (condition false)",
                        ))
                        continue
                except Exception as e:
                    results.append(StepResult(
                        step_name=step.name,
                        success=False,
                        error=f"Condition eval failed in loop: {e}",
                    ))
                    return results
            r = self._run_step_once(step, ctx, dry_run)
            suffix = f" [{i+1}/{n}]" if n is not None else f" [{i+1}]"
            r.step_name = f"{step.name}{suffix}"
            results.append(r)
            if not r.success:
                break
        return results

    def _execute_step(
        self, step: WorkflowStep, cmd: str, dry_run: bool,
        args: Optional[Dict[str, Any]] = None,
        ctx: Optional[Dict[str, Any]] = None,
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
                # 传入 ctx，让 python step 能访问工作流变量（循环计数器等）
                output = self._tools.python_eval(cmd, dry_run, ctx=ctx)
            elif step.action == "wait":
                output = self._tools.wait(cmd, dry_run)
            elif step.action == "condition":
                output = self._tools.condition(cmd, dry_run)
            elif step.action == "click_element":
                # 第3步：UI 元素定位 → 取 BoundingRectangle 中心点 → 调用现有 click
                # args 里支持 name / control_type / role / index 等选择器
                output = self._tools.click_element(args or {}, dry_run)
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

    def python_eval(self, expr: str, dry_run: bool, ctx: Optional[Dict[str, Any]] = None) -> str:
        """执行一个 Python 表达式。

        ctx 非空时，工作流变量作为 locals 注入，可在表达式里直接访问
        （如 `ctx['counter']` 或捕获到 locals 后直接 `counter`）。
        """
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
            # ctx 作为 locals，safe 作为 globals 的 __builtins__
            locals_dict = dict(ctx) if ctx else {}
            result = eval(expr, {"__builtins__": safe, "ctx": ctx or {}}, locals_dict)
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

    # ── 第3步：UI 元素定位 ──────────────────────────────────────────

    # 控件类型短名 → UIA ControlTypeName 映射（YAML 里可写短名也可写全名）
    _CT_ALIASES = {
        "button": "ButtonControl",
        "btn": "ButtonControl",
        "edit": "EditControl",
        "textbox": "EditControl",
        "input": "EditControl",
        "link": "HyperlinkControl",
        "combobox": "ComboBoxControl",
        "list": "ListControl",
        "listitem": "ListItemControl",
        "menu": "MenuControl",
        "menuitem": "MenuItemControl",
        "tab": "TabControl",
        "tabitem": "TabItemControl",
        "checkbox": "CheckBoxControl",
        "radio": "RadioButtonControl",
        "tree": "TreeControl",
        "treeitem": "TreeItemControl",
    }

    def click_element(self, selector: dict, dry_run: bool) -> str:
        """按 UI 选择器点击 UI 元素（无需知道坐标）。

        selector:
            name: 控件名称（子串匹配，大小写不敏感；空则不限）
            control_type: 控件类型（短名或全名，如 "button" 或 "ButtonControl"）
            role: 角色名（可选，进一步过滤）
            index: 第 N 个匹配（0-based，默认 0）
            button: 鼠标按钮 'left'/'right'/'middle'（默认 left）
            clicks: 点击次数（默认 1）
            double: True 时等于 clicks=2

        流程：
          1. 通过 UiaProvider.snapshot() 采集 UI 树（失败则降级到 LightProvider
             单节点窗口，此时按 name 匹配窗口后点击窗口中心）
          2. 在树中按 selector 查找匹配节点
          3. 取节点 BoundingRectangle 中心 (cx, cy)
          4. 调用现有 self._agent.click(x=cx, y=cy, ...)
        """
        name = (selector.get("name") or "").strip()
        ct_raw = (selector.get("control_type") or "").strip()
        role = (selector.get("role") or "").strip()
        index = int(selector.get("index", 0))
        button = (selector.get("button") or "left").strip()
        clicks = int(selector.get("clicks", 1))
        if selector.get("double"):
            clicks = 2

        # 控件类型标准化为 UIA ControlTypeName
        ct_filter = ""
        if ct_raw:
            ct_filter = self._CT_ALIASES.get(ct_raw.lower(), ct_raw)

        # 构造 dry-run 预览描述
        desc_parts = []
        if name: desc_parts.append(f'name~"{name}"')
        if ct_filter: desc_parts.append(f'type={ct_filter}')
        if role: desc_parts.append(f'role={role}')
        if index: desc_parts.append(f'index={index}')
        desc = " ".join(desc_parts) or "(any)"

        # 1. 采集 UI 快照（复用 PerceptionStack 的 UiaProvider）
        uistate = self._capture_uia_snapshot()
        if uistate is None or uistate.root is None:
            if dry_run:
                return f"[dry-run] click_element {desc} [UIA 不可用，将回退到坐标点击]"
            return f"click_element failed: UIA snapshot unavailable"

        # 2. 在 UI 树中查找匹配节点
        matches = []
        self._collect_matches(uistate.root, name, ct_filter, role, matches)

        if not matches:
            if dry_run:
                return f"[dry-run] click_element {desc} [未找到匹配节点，树中共 {uistate.node_count} 节点]"
            return f"click_element failed: no matching node ({desc})"

        if index >= len(matches):
            if dry_run:
                return f"[dry-run] click_element {desc} [index 超出范围，匹配到 {len(matches)} 个]"
            return f"click_element failed: index {index} out of {len(matches)} matches"

        node = matches[index]
        # 3. 取 BoundingRectangle 中心点
        if node.width <= 0 or node.height <= 0:
            if dry_run:
                return f"[dry-run] click_element {desc} [节点无 BoundingRectangle]"
            return f"click_element failed: node has no bounding rect"

        cx = node.x + node.width // 2
        cy = node.y + node.height // 2

        if dry_run:
            return (f"[dry-run] click_element {desc} "
                    f"-> ({cx},{cy}) name={node.name!r} type={node.control_type}")

        # 4. 调用现有 agent.click
        self._agent.click(x=cx, y=cy, button=button, clicks=clicks)
        return f"clicked '{node.name}' at ({cx},{cy})"

    def _capture_uia_snapshot(self):
        """采集 UIA 快照；UIA 不可用时返回 None（由调用方决定降级）。"""
        try:
            from .perception import UiaProvider
            provider = UiaProvider()
            if not provider.available():
                return None
            return provider.snapshot()
        except Exception:
            return None

    @staticmethod
    def _collect_matches(root, name: str, ct_filter: str, role: str, out: list):
        """递归收集所有匹配的节点到 out 列表。"""
        # name 子串匹配（大小写不敏感）
        if name:
            if name.lower() not in (root.name or "").lower():
                pass  # name 不匹配，但仍要遍历子节点
            else:
                if (not ct_filter or root.control_type == ct_filter) and \
                   (not role or root.role == role):
                    out.append(root)
        else:
            # 无 name 过滤，仅按类型/角色
            if (not ct_filter or root.control_type == ct_filter) and \
               (not role or root.role == role):
                out.append(root)

        for child in root.children:
            _ToolRegistry._collect_matches(child, name, ct_filter, role, out)
