"""CLI entry point for nl2shortcut."""

import sys
import time
import argparse
import json
import csv
import io
from pathlib import Path

from .agent import ShortcutAgent
from .master import KeyboardMasterAgent


def _fmt(seconds: float) -> str:
    return f"{seconds * 1000:.1f}ms"


def cmd_exec(agent, args) -> int:
    text = " ".join(args.text)
    if not text:
        print("Error: No command text provided.", file=sys.stderr)
        return 1
    result = agent.execute(text, dry_run=args.dry_run, timeout=args.timeout)
    if args.dry_run:
        print(f"[DRY RUN] {result.key_combination}")
    if result.success:
        print(
            f"OK {result.intent} -> {result.key_combination} "
            f"({_fmt(result.processing_time)})"
        )
        return 0
    else:
        print(f"FAIL {result.error}", file=sys.stderr)
        return 1


def cmd_list(agent, args) -> int:
    shortcuts = agent.list_shortcuts(category=args.category)
    if not shortcuts:
        print("No shortcuts found.")
        return 0

    if args.format == "json":
        data = [
            {
                "command": s.command,
                "description": s.description,
                "windows": s.windows_key,
                "mac": s.mac_key,
                "linux": s.linux_key,
                "category": s.category,
                "app": s.application,
            }
            for s in shortcuts
        ]
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.format == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Command", "Description", "Windows", "macOS",
                      "Linux", "Category", "App"])
        for s in shortcuts:
            w.writerow([s.command, s.description, s.windows_key,
                         s.mac_key, s.linux_key, s.category, s.application])
        print(out.getvalue(), end="")
    else:
        header = f"{'Command':<22} {'Description':<28} {'Win':<20} {'Mac':<20} {'Cat':<10}"
        print(header)
        print("-" * len(header))
        for s in shortcuts:
            win = s.windows_key[:17] + "..." if len(s.windows_key) > 19 else s.windows_key
            mac = s.mac_key[:17] + "..." if len(s.mac_key) > 19 else s.mac_key
            print(f"{s.command:<22} {s.description:<28} {win:<20} {mac:<20} {s.category:<10}")
        print(f"\nTotal: {len(shortcuts)} shortcuts")
    return 0


def cmd_search(agent, args) -> int:
    kw = " ".join(args.keyword)
    results = agent.search_shortcuts(kw)
    if not results:
        print(f"No shortcuts found for: {kw}")
        return 0
    for s in results:
        print(f"  {s.command} ({s.description})")
        print(f"    Win: {s.windows_key}  Mac: {s.mac_key}  Linux: {s.linux_key}")
        print(f"    Category: {s.category}  App: {s.application}")
    print(f"\n{len(results)} result(s)")
    return 0


def cmd_stats(agent, args) -> int:
    if args.reset:
        agent.reset_stats()
        print("Statistics reset.")
        return 0
    stats = agent.get_stats()
    print(f"Total:       {stats.total_executions}")
    print(f"Successful:  {stats.successful}")
    print(f"Failed:      {stats.failed}")
    print(f"Success:     {stats.success_rate:.1f}%")
    print(f"Avg Time:    {_fmt(stats.avg_processing_time)}")
    if stats.top_commands:
        print("\nTop Commands:")
        for cmd, desc, freq in stats.top_commands:
            print(f"  {cmd:<22} {desc:<28} x{freq}")
    return 0


def cmd_benchmark(agent, args) -> int:
    n = args.iterations
    cmds = [
        "copy", "paste", "cut", "undo", "save",
        "bold", "italic", "find", "select all", "refresh",
    ]
    print(f"Running {n} iterations x {len(cmds)} commands (dry-run)...\n")

    tt = 0.0
    ok = 0
    fail = 0
    per_cmd = {}

    for _ in range(n):
        for c in cmds:
            r = agent.execute(c, dry_run=True)
            tt += r.processing_time
            if r.success:
                ok += 1
            else:
                fail += 1
            per_cmd.setdefault(r.command, []).append(r.processing_time)

    total = n * len(cmds)
    avg = tt / total if total else 0
    print(f"Results: {total} runs | {ok} ok | {fail} fail")
    print(f"  Avg: {_fmt(avg)} | Total: {_fmt(tt)}")
    if tt > 0:
        print(f"  Throughput: {total / tt:.0f} ops/s")
    print("\nPer-command:")
    for cmd, times in sorted(per_cmd.items()):
        ca = sum(times) / len(times)
        print(f"  {cmd:<15} avg={_fmt(ca)}  min={_fmt(min(times))}  max={_fmt(max(times))}")
    return 0


def cmd_repl(agent, args) -> int:
    print("nl2shortcut REPL v0.1.0")
    print("Commands: <text> / ? <text>=dry-run / list / stats / quit\n")
    while True:
        try:
            inp = input("nl2sc> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            return 0
        if not inp:
            continue
        if inp.lower() in ("quit", "exit", "q"):
            print("Goodbye!")
            return 0

        dry = False
        if inp.startswith("?"):
            dry = True
            inp = inp[1:].strip()
        if inp.lower() == "list":
            cmd_list(agent, argparse.Namespace(category=None, format="table"))
            continue
        if inp.lower() == "stats":
            cmd_stats(agent, argparse.Namespace(reset=False))
            continue

        r = agent.execute(inp, dry_run=dry)
        tag = "[DRY]" if dry else ""
        if r.success:
            print(f"  {tag} -> {r.key_combination}")
        else:
            print(f"  {tag} FAIL: {r.error}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nl2shortcut",
        description="NL2Shortcut Keyboard Master Agent — 自然语言驱动的自主动键盘/鼠标自动化",
    )
    sp = p.add_subparsers(dest="command")

    ep = sp.add_parser("exec", help="Execute a shortcut from natural language")
    ep.add_argument("text", nargs="+", help="Natural language command")
    ep.add_argument("--dry-run", action="store_true",
                    help="Show keys without pressing")
    ep.add_argument("--timeout", type=float, default=5.0,
                    help="Execution timeout (default: 5s)")

    # ── Unified AI Agent: one sentence → plan → execute → save workflow ──
    rp = sp.add_parser("run", help="AI Agent: 一句话自动执行（识别→规划→执行→保存工作流）")
    rp.add_argument("text", nargs="+", help="Anything: '复制', '保存并关闭', '复制X到Y'")
    rp.add_argument("--dry-run", action="store_true", help="Preview only, no execution")
    rp.add_argument("--no-save", action="store_true", help="Don't auto-save as workflow")

    lp = sp.add_parser("list", help="List registered shortcuts")
    lp.add_argument("--category", "-c",
                    help="按类别筛选 (编辑, 文件, 视图, 导航, 代码, 系统, 通用, Windows徽标键, 文件资源管理器, 命令提示符, 虚拟桌面, 任务栏, 设置, 对话框)")
    lp.add_argument("--format", "-f", choices=["table", "json", "csv"],
                    default="table", help="Output format")

    sp2 = sp.add_parser("search", help="Search shortcuts by keyword")
    sp2.add_argument("keyword", nargs="+", help="Search keyword(s)")

    stp = sp.add_parser("stats", help="Show execution statistics")
    stp.add_argument("--reset", action="store_true", help="Reset statistics")

    bp = sp.add_parser("benchmark", help="Performance benchmark")
    bp.add_argument("--iterations", "-n", type=int, default=100,
                    help="Iterations per command (default: 100)")

    sp.add_parser("repl", help="Interactive REPL mode")

    sp.add_parser("gui", help="Launch the desktop GUI (Agent 控制台)")

    # ──── Keyboard Master Agent 专属命令 ────
    mp = sp.add_parser("master",
                       help="经 Master Agent 执行自然语言指令（带操作记忆回写）")
    mp.add_argument("text", nargs="+", help="自然语言指令")
    mp.add_argument("--dry-run", action="store_true", help="仅识别不执行")
    mp.add_argument("--timeout", type=float, default=5.0, help="超时秒数")

    plp = sp.add_parser("plan", help="把目标拆成多步执行计划 (DeepSeek)")
    plp.add_argument("goal", nargs="+", help="目标，如「把这份报告发出去」")
    plp.add_argument("--dry-run", action="store_true", help="仅生成计划不执行")

    sp.add_parser("suggest", help="基于历史习惯给出主动操作建议")

    ss = sp.add_parser("start-server",
                       help="启动 Agent API server（后台常驻，供外部 Agent / MCP 调用）")
    ss.add_argument("--host", default="127.0.0.1", help="绑定地址")
    ss.add_argument("--port", type=int, default=7770, help="绑定端口")

    sps = sp.add_parser("stop-server", help="停止本实例持有的 Agent API server")
    sps.add_argument("--port", type=int, default=7770, help="端口")

    # ──── Keyboard/mouse operations ────
    tp = sp.add_parser("type", help="Type text at cursor")
    tp.add_argument("text", nargs="+", help="Text to type")
    tp.add_argument("--interval", "-i", type=float, default=0.0,
                    help="Delay between keystrokes (seconds)")

    clp = sp.add_parser("click", help="Mouse click")
    clp.add_argument("--button", "-b", choices=["left", "right", "middle"],
                     default="left", help="Mouse button")
    clp.add_argument("--count", "-c", type=int, default=1, help="Click count")
    clp.add_argument("--x", type=int, default=None, help="X coordinate")
    clp.add_argument("--y", type=int, default=None, help="Y coordinate")

    scp = sp.add_parser("scroll", help="Mouse scroll")
    scp.add_argument("amount", type=int, help="Scroll amount (+up, -down)")

    ssp = sp.add_parser("screenshot", help="Screen capture")
    ssp.add_argument("--output", "-o", type=str, default=None,
                     help="Save path (default: auto-named)")

    sp.add_parser("mouse", help="Show mouse position (via PyAutoGUI)")

    # ———— File search ————
    fp = sp.add_parser("find", help="Search files/folders by name")
    fp.add_argument("pattern", help="Search pattern (case-insensitive substring match)")
    fp.add_argument("--dir", "-d", default=None,
                    help="Start directory (default: Desktop)")
    fp.add_argument("--max", "-n", type=int, default=10,
                    help="Max results (default: 10)")
    fp.add_argument("--open", "-o", action="store_true",
                    help="Open the first result in File Explorer")
    fp.add_argument("--cmd", action="store_true",
                    help="Use CMD dir /s /b to search (fast, native)")
    fp.add_argument("--ps", action="store_true",
                    help="Use PowerShell Get-ChildItem to search")
    fp.add_argument("--terminal", "-t", action="store_true",
                    help="Open visible terminal window (CMD) to show results")

    # ———— Overlay / background mode ————
    ovp = sp.add_parser("overlay", help="Global hotkey + floating input bar (tray resident)")
    ovp.add_argument("--no-tray", action="store_true", help="Don't show system tray icon")
    ovp.add_argument("--hotkey", default="<alt>+<shift>+s",
                     help="Global hotkey combo (default: <alt>+<shift>+s)")

    # ———— MCP Server (Model Context Protocol) ————
    mp = sp.add_parser("mcp-server",
                       help="Start the MCP tool server (Model Context Protocol)")
    mp.add_argument(
        "--transport", "-t",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode: stdio (MCP CLI, OpenClaw native) or http (HTTP+SSE)",
    )
    mp.add_argument(
        "--port", "-p", type=int, default=7791,
        help="HTTP server port (only for --transport http, default: 7791)",
    )

    # ———— Agent API server (for OpenClaw / Claude Computer Use / 智谱) ————
    aap = sp.add_parser("agent-api",
                        help="Start the JSON HTTP API for AI Agents")
    aap.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    aap.add_argument("--port", type=int, default=7770, help="Bind port (default: 7770)")

    # ———— Composite (vision-driven multi-step) plans ————
    cp = sp.add_parser("composite",
                       help="Build / test composite (multi-step vision) plans")
    cp_sub = cp.add_subparsers(dest="composite_cmd")
    cp_copy = cp_sub.add_parser("file-copy",
                                help="Build a file-copy plan (right-click Copy → navigate → Paste)")
    cp_copy.add_argument("source", nargs="+", help="Source item description, e.g. '新建 DOCX 文档 (2).docx'")
    cp_copy.add_argument("dest", nargs="+", help="Destination folder path, e.g. 'C:\\Users\\Deng2\\Desktop\\新建文件夹'")
    cp_move = cp_sub.add_parser("file-move",
                               help="Build a file-move plan (right-click Cut → navigate → Paste)")
    cp_move.add_argument("source", nargs="+", help="Source item description")
    cp_move.add_argument("dest", nargs="+", help="Destination folder path")
    cp_ti = cp_sub.add_parser("test-intent",
                              help="Test composite intent recognition on raw text")
    cp_ti.add_argument("text", nargs="+", help="Natural language text, e.g. '复制X到Y'")

    # workflow subcommands
    wp = sp.add_parser("workflow", help="Workflow management")
    wp_sub = wp.add_subparsers(dest="workflow_cmd")
    wp_export = wp_sub.add_parser("export", help="Export a learned pattern as YAML workflow")
    wp_export.add_argument("pattern_name", help="Pattern name to export")
    wp_export.add_argument("--overwrite", action="store_true",
                           help="Overwrite existing workflow file")
    wp_export_all = wp_sub.add_parser("export-all",
                                      help="Export all high-confidence patterns")
    wp_list = wp_sub.add_parser("list", help="List available workflows")
    wp_run = wp_sub.add_parser("run", help="Run a workflow")
    wp_run.add_argument("name", help="Workflow name to run")
    wp_run.add_argument("--dry-run", action="store_true", help="Dry run only")
    wp_create = wp_sub.add_parser("create",
                                  help="Create workflow from natural language description")
    wp_create.add_argument("description", nargs="+",
                           help="Natural language, e.g. '保存文件后关闭窗口并刷新'")
    wp_create.add_argument("--name", help="Workflow name (auto-generated if omitted)")
    wp_create.add_argument("--dry-run", action="store_true",
                           help="Preview steps without saving")
    wp_create.add_argument("--run", action="store_true",
                           help="Create AND immediately execute the workflow")
    # workflow import: 从外部 YAML 文件导入工作流
    wp_import = wp_sub.add_parser("import", help="Import a YAML workflow file")
    wp_import.add_argument("source", help="Path to the YAML workflow file to import")
    wp_import.add_argument("--name", help="Rename the workflow on import "
                           "(default: use the filename or internal name)")
    wp_import.add_argument("--overwrite", action="store_true",
                           help="Overwrite if a workflow with the same name exists")

    # ── Self-test: let NL2Shortcut test itself ──
    stp = sp.add_parser("self-test",
                        help="Run built-in self-tests (识别/执行/原语/自检)")
    stp.add_argument("--live", action="store_true",
                     help="Also probe a running server's HTTP endpoints")
    stp.add_argument("--host", default="127.0.0.1", help="Server host (for --live)")
    stp.add_argument("--port", type=int, default=7770, help="Server port (for --live)")
    stp.add_argument("--json", action="store_true", help="Output raw JSON report")

    return p


def cmd_run(master, args) -> int:
    """Unified AI Agent: one sentence → recognize → execute → auto-save workflow."""
    import yaml, re
    from pathlib import Path

    text = " ".join(args.text)
    dry_run = getattr(args, "dry_run", False)
    no_save = getattr(args, "no_save", False)

    print(f"🤖 分析：「{text}」")

    # 1. Try direct execution first (handles composites, simple shortcuts)
    r = master.execute(text, dry_run=dry_run)

    # If it's a multi-step plan, decompose via LLM
    if r.mode == "llm_plan" or (r.command == "__plan__" and r.key_combination):
        steps_list = r.key_combination.split(" → ") if r.key_combination else []
        print(f"📋 LLM 分解为 {len(steps_list)} 步：")
        for i, s in enumerate(steps_list):
            print(f"  {i+1}. {s}")
        if dry_run:
            print("⚠️  dry-run，未执行。")
            return 0
        if r.success:
            print("✅ 全部完成")
            # Auto-save multi-step as workflow
            if len(steps_list) >= 2 and not no_save:
                _save_workflow(text, steps_list)
            return 0
        else:
            print(f"❌ {r.error}")
            return 1

    # Direct execution result
    if r.mode == "composite":
        plan = r.composite_plan
        if plan:
            steps = [f"{s.kind}:{s.description[:30]}" for s in plan.steps]
            print(f"📋 Shell 执行：{len(steps)} 步")
            for i, s in enumerate(steps):
                print(f"  {i+1}. {s}")
        if dry_run:
            print("⚠️  dry-run，未执行。")
            return 0
        if r.success:
            print("✅ 执行完成")
            return 0
        else:
            print(f"❌ {r.error}")
            return 1

    # Simple shortcut
    if r.success:
        print(f"✅ {r.key_combination}")
        return 0

    # Fallback: try planner
    print("🔄 尝试 LLM 规划...")
    plan = master.plan(text)
    if plan.steps:
        print(f"📋 分解为 {len(plan.steps)} 步：")
        for i, s in enumerate(plan.steps):
            print(f"  {i+1}. {s.key_combination or s.text or str(s.wait_ms)+'ms'}")
        if dry_run:
            return 0
        results = master.execute_plan(plan)
        for i, r2 in enumerate(results):
            print(f"  {'✅' if r2.success else '❌'} 第{i+1}步: {r2.key_combination or r2.error or ''}")
        return 0 if all(r2.success for r2 in results) else 1

    print(f"❌ 无法理解：「{text}」")
    return 1


def _save_workflow(text: str, steps: list) -> None:
    """Save a multi-step command as a reusable workflow."""
    import yaml, re
    from pathlib import Path
    wf_name = text.lower()
    wf_name = re.sub(r'[^\w\s-]', '', wf_name)
    wf_name = re.sub(r'[-\s]+', '-', wf_name).strip('-')[:40]
    wf_dir = Path.home() / ".nl2shortcut" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    filepath = wf_dir / f"{wf_name}.yaml"
    if not filepath.exists():
        wf_steps = [{"name": f"Step {i+1}", "action": "shortcut", "command": s.split("→")[-1].strip()}
                    for i, s in enumerate(steps)]
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"# Auto-generated from: '{text}'\n")
            yaml.dump({"name": wf_name, "description": text, "version": "1.0",
                       "variables": {}, "steps": wf_steps}, f,
                      allow_unicode=True, default_flow_style=False, sort_keys=False)
        print(f"💾 已保存工作流：{wf_name}")


def cmd_master(master, args) -> int:
    """经 Master Agent 执行（带操作记忆回写）。"""
    text = " ".join(args.text)
    if not text:
        print("Error: No command text provided.", file=sys.stderr)
        return 1
    r = master.execute(text, dry_run=args.dry_run, timeout=args.timeout)
    if r.success:
        print(f"OK  {r.intent} -> {r.key_combination}  ({_fmt(r.processing_time)})")
        return 0
    print(f"FAIL {r.error}", file=sys.stderr)
    return 1


def cmd_plan(master, args) -> int:
    """生成多步执行计划。"""
    goal = " ".join(args.goal)
    plan = master.plan(goal)
    print(plan.format_human())
    if not args.dry_run:
        results = master.execute_plan(plan)
        ok = sum(1 for r in results if r.success)
        print(f"\n执行完成：{ok}/{len(results)} 步成功")
    return 0


def cmd_suggest(master, args) -> int:
    """主动建议。"""
    hint = master.suggest()
    if hint:
        print("💡 主动建议:")
        print(hint)
    else:
        print("暂无足够操作历史，多用几次后会开始学习你的习惯。")
    return 0


def cmd_workflow(master, args) -> int:
    """Workflow management: export, export-all, list, run."""
    wf_cmd = getattr(args, "workflow_cmd", None)
    if wf_cmd == "export":
        path = master.export_pattern_to_workflow(
            args.pattern_name, overwrite=args.overwrite
        )
        if path:
            print(f"✅ 导出成功：{path}")
            return 0
        else:
            print(f"❌ 导出失败：pattern '{args.pattern_name}' 不存在或文件已存在")
            return 1
    elif wf_cmd == "export-all":
        paths = master.export_high_confidence_workflows()
        if paths:
            print(f"✅ 导出了 {len(paths)} 个工作流：")
            for p in paths:
                print(f"  • {p}")
        else:
            print("暂无高置信度 pattern 可导出（需 confidence >= 0.7）。")
        return 0
    elif wf_cmd == "list":
        from .workflow import WorkflowEngine
        engine = WorkflowEngine(master.agent)
        names = engine.list_workflows()
        if names:
            print(f"可用工作流（{len(names)} 个）：")
            for n in names:
                wf = engine.load(n)
                print(f"  • {n} — {wf.description}")
        else:
            print("暂无工作流。")
        return 0
    elif wf_cmd == "run":
        from .workflow import WorkflowEngine
        engine = WorkflowEngine(master.agent)
        result = engine.run(args.name, dry_run=getattr(args, "dry_run", False))
        if result.success:
            print(f"✅ 工作流 '{args.name}' 执行完成（{len(result.steps)} 步，{result.total_duration_ms:.0f}ms）")
        else:
            print(f"❌ 工作流 '{args.name}' 执行失败：{result.error}")
        return 0 if result.success else 1
    elif wf_cmd == "create":
        return _cmd_workflow_create(master, args)
    elif wf_cmd == "import":
        return _cmd_workflow_import(master, args)
    else:
        print("用法：nl2shortcut workflow {export|export-all|list|run|create|import}")
        return 1


def _cmd_workflow_create(master, args) -> int:
    """Create a YAML workflow from natural language description via LLM."""
    description = " ".join(args.description)
    dry_run = getattr(args, "dry_run", False)
    wf_name = getattr(args, "name", None) or ""

    # 1. Let LLM decompose the goal into steps
    print(f"🔍 正在分析：「{description}」...")
    plan_result = master.plan(description)

    if not plan_result.steps:
        print("❌ LLM 无法分解此描述。请尝试更具体的描述。")
        return 1

    print(f"📋 LLM 分解为 {len(plan_result.steps)} 步：")
    for i, s in enumerate(plan_result.steps):
        icon = {"shortcut": "⌨️", "wait": "⏱️", "type": "⌨️", "shell": "💻"}.get(s.action, "•")
        detail = s.key_combination or f"{s.text or ''}" or f"{s.composite_hint or ''}"
        print(f"  {i+1}. {icon} {s.action}: {detail}")

    if dry_run:
        print("\n⚠️  dry-run 模式，未保存。")
        return 0

    # 2. Convert LLM plan steps to workflow YAML
    import yaml
    from pathlib import Path

    # Auto-generate name from description
    if not wf_name:
        import re
        wf_name = description.lower()
        wf_name = re.sub(r'[^\w\s-]', '', wf_name)
        wf_name = re.sub(r'[-\s]+', '-', wf_name).strip('-')
        if len(wf_name) > 40:
            wf_name = wf_name[:40].rstrip('-')

    wf_dir = Path.home() / ".nl2shortcut" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    filepath = wf_dir / f"{wf_name}.yaml"

    if filepath.exists():
        print(f"\n⚠️  工作流 '{wf_name}' 已存在。使用 --name 指定其他名称。")
        return 1

    # Map PlanStep to workflow step
    wf_steps = []
    for i, s in enumerate(plan_result.steps):
        if s.action == "shortcut":
            cmd = s.key_combination
            act = "shortcut"
        elif s.action == "wait":
            cmd = str(s.wait_ms / 1000.0 if s.wait_ms > 0 else 1)
            act = "wait"
        elif s.action == "type":
            cmd = s.text or ""
            act = "type"
        elif s.action == "shell":
            cmd = s.key_combination or s.composite_hint or ""
            act = "shell"
        else:
            cmd = str(s.key_combination or s.composite_hint or "")
            act = "shortcut"
        step_name = s.description or f"Step {i+1}"
        wf_steps.append({"name": step_name, "action": act, "command": cmd})

    doc = {
        "name": wf_name,
        "description": description,
        "version": "1.0",
        "variables": {},
        "steps": wf_steps,
    }

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# Auto-generated from: '{description}'\n")
        yaml.dump(doc, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    print(f"\n✅ 工作流已创建：{filepath}")

    # --run: execute immediately
    if getattr(args, "run", False):
        print(f"\n🚀 正在执行...")
        from .workflow import WorkflowEngine
        engine = WorkflowEngine(master.agent)
        result = engine.run(wf_name, dry_run=False)
        if result.success:
            print(f"✅ 执行完成（{len(result.steps)} 步，{result.total_duration_ms:.0f}ms）")
            for s in result.steps:
                icon = "✅" if s.success else "❌"
                print(f"  {icon} {s.step_name}: {s.output[:50]}")
        else:
            print(f"❌ 执行失败：{result.error}")
            return 1
    else:
        print(f"   运行：nl2shortcut workflow run {wf_name}")
    return 0


def _cmd_workflow_import(master, args) -> int:
    """Import a YAML workflow file from an external path into the local store.

    Copies the file, validates its structure, and registers it so
    `nl2shortcut workflow run <name>` can execute it.
    """
    import shutil
    from pathlib import Path

    source = Path(args.source).expanduser().resolve()
    if not source.exists():
        print(f"❌ 源文件不存在：{source}")
        return 1
    if not source.is_file():
        print(f"❌ 源路径不是文件：{source}")
        return 1
    if source.suffix.lower() not in (".yaml", ".yml"):
        print(f"❌ 只支持 .yaml / .yml 文件，收到：{source.suffix}")
        return 1
    if source.stat().st_size > 2 * 1024 * 1024:
        print(f"❌ 文件过大（超过 2 MB），拒绝导入。")
        return 1

    # Determine target name
    overwrite = getattr(args, "overwrite", False)
    rename = getattr(args, "name", None) or ""

    if rename:
        target_name = rename
    else:
        # Derive from source filename (strip extension)
        target_name = source.stem

    # Validate YAML structure before copying
    import yaml
    try:
        with open(source, "r", encoding="utf-8-sig") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        print(f"❌ YAML 解析失败：{e}")
        return 1

    if not isinstance(raw, dict):
        print(f"❌ YAML 顶层必须是字典（mapping），收到：{type(raw).__name__}")
        return 1
    if "steps" not in raw:
        print(f"❌ YAML 缺少 'steps' 字段。工作流必须至少包含一个步骤。")
        return 1
    if not isinstance(raw["steps"], list) or len(raw["steps"]) == 0:
        print(f"❌ 'steps' 必须是非空列表。")
        return 1

    # Validate each step
    valid_actions = {"shortcut", "type", "click", "scroll", "screenshot",
                     "shell", "http", "file", "python", "wait", "condition"}
    for i, step in enumerate(raw["steps"]):
        if not isinstance(step, dict):
            print(f"❌ 步骤 {i+1} 必须是字典，收到：{type(step).__name__}")
            return 1
        action = step.get("action", "")
        if action not in valid_actions:
            print(f"❌ 步骤 {i+1} 的 action '{action}' 无效。"
                  f"有效值：{', '.join(sorted(valid_actions))}")
            return 1
        if not step.get("name"):
            step["name"] = f"Step {i+1}"

    # Copy to workflows directory
    wf_dir = Path.home() / ".nl2shortcut" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    dest = wf_dir / f"{target_name}.yaml"

    if dest.exists() and not overwrite:
        print(f"⚠️  工作流 '{target_name}' 已存在。")
        print(f"   使用 --overwrite 覆盖，或 --name 指定其他名称。")
        return 1

    try:
        # Preserve the original YAML structure rather than re-dumping
        if source.suffix.lower() == ".yml":
            dest = wf_dir / f"{target_name}.yml"
        shutil.copy2(source, dest)
    except Exception as e:
        print(f"❌ 复制文件失败：{e}")
        return 1

    # Re-validate by loading through the engine
    from .workflow import WorkflowEngine
    engine = WorkflowEngine(master.agent)
    wf = engine.load(target_name)
    if wf is None:
        # Clean up broken import
        try:
            dest.unlink()
        except Exception:
            pass
        print(f"❌ 导入后验证失败：工作流 '{target_name}' 加载失败。")
        return 1

    print(f"✅ 已导入工作流：{target_name}")
    print(f"   文件：{dest}")
    print(f"   描述：{wf.description or '(无)'}")
    print(f"   步骤数：{len(wf.steps)}")
    print(f"   运行：nl2shortcut workflow run {target_name}")
    return 0


def cmd_start_server(master, args) -> int:
    """启动 Agent API server（自管理，等效于 Skill 的 start_nl2shortcut_server）。"""
    res = master.start_server(host=args.host, port=args.port)
    print(res.get("reply", res))
    return 0 if res.get("ok") else 1


def cmd_stop_server(master, args) -> int:
    """停止 server。"""
    res = master.stop_server()
    print(res.get("reply", res))
    return 0


def cmd_gui(agent, args) -> int:
    """Launch the PyQt5 GUI."""
    from .gui import main as gui_main
    gui_main()
    return 0


def cmd_composite(agent, args) -> int:
    """Build or test composite (multi-step vision) plans."""
    from .composites import make_file_copy_context_menu, make_file_move_context_menu
    sub = getattr(args, "composite_cmd", None)
    if sub == "file-copy":
        plan = make_file_copy_context_menu(" ".join(args.source), " ".join(args.dest))
    elif sub == "file-move":
        plan = make_file_move_context_menu(" ".join(args.source), " ".join(args.dest))
    elif sub == "test-intent":
        text = " ".join(args.text)
        res = agent.recognize_intent(text)
        if res.command == "__composite__" and res.composite_plan:
            plan = res.composite_plan
            print(f"[composite] matched: {res.matched_keyword}  (confidence {res.confidence:.2f})")
        else:
            print(f"[not composite] command={res.command!r} intent={res.intent!r} "
                  f"confidence={res.confidence:.2f}")
            return 0
    else:
        print("Error: specify a composite subcommand (file-copy | file-move | test-intent).",
              file=sys.stderr)
        return 1
    print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    return 0


def cmd_agent_api(agent, args) -> int:
    """Start the Agent JSON API server (for OpenClaw / Claude Computer Use / 智谱 AutoGLM)."""
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 7770)
    from .agent_api import serve
    print(f"[nl2shortcut] Agent API listening on http://{host}:{port}", flush=True)
    print(f"[nl2shortcut] Try: curl http://{host}:{port}/v1/health", flush=True)
    serve(host=host, port=port, agent=agent)
    return 0

def cmd_overlay(agent, args) -> int:
    """Start global hotkey + floating input bar (overlay mode)."""
    from .overlay import main as overlay_main
    overlay_main(
        show_tray=not getattr(args, 'no_tray', False),
        hotkey=getattr(args, 'hotkey', '<alt>+<shift>+s'),
    )
    return 0


def cmd_type(agent, args) -> int:
    text = " ".join(args.text)
    print(agent.type_text(text, interval=args.interval))
    return 0


def cmd_click(agent, args) -> int:
    x = getattr(args, "x", None)
    y = getattr(args, "y", None)
    print(agent.click(x=x, y=y, button=args.button, clicks=args.count))
    return 0


def cmd_scroll(agent, args) -> int:
    print(agent.scroll(args.amount))
    return 0


def cmd_screenshot(agent, args) -> int:
    path = args.output
    if path is None:
        import pyautogui
        img = pyautogui.screenshot()
        path = str(agent.config_dir / "screenshots" / f"scut_{int(time.time())}.png")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        img.save(path)
    else:
        agent.screenshot(path=path)
    print(f"Screenshot saved: {path}")
    return 0


def cmd_mouse(agent, args) -> int:
    import pyautogui
    pos = pyautogui.position()
    print(f"Mouse at ({pos.x}, {pos.y})")
    return 0


def cmd_find(agent, args) -> int:
    """Search files/folders by name pattern. Supports --cmd (CMD) and --ps (PowerShell)."""
    import os, subprocess, re
    from pathlib import Path

    pattern = args.pattern
    start_dir = args.dir or str(Path.home() / "Desktop")
    max_results = args.max
    open_first = getattr(args, "open", False)
    use_cmd = getattr(args, "cmd", False)
    use_ps = getattr(args, "ps", False)
    use_terminal = getattr(args, "terminal", False)

    # ── Terminal mode: open visible CMD/PowerShell window ──
    if use_terminal:
        native_dir = str(Path(start_dir))
        if use_ps:
            cmd = (f'start powershell -NoExit -Command '
                   f'"Get-ChildItem -Path \'{native_dir}\' -Recurse -Filter \'*{pattern}*\' '
                   f'-ErrorAction SilentlyContinue | Format-Table Name, FullName, Length -AutoSize"')
        else:
            # CMD: visible window, stays open with /k
            cmd = f'start cmd /k "echo 搜索: {pattern} 从 {native_dir} && echo. && dir /s /b \"{native_dir}\" 2>nul | findstr /i \"{pattern}\""'
        subprocess.Popen(cmd, shell=True)
        print(f"🖥️  已打开{'PowerShell' if use_ps else 'CMD'}终端窗口搜索 \"{pattern}\"")
        return 0

    method = "Python"
    if use_cmd:
        method = "CMD"
    elif use_ps:
        method = "PowerShell"

    print(f"🔍 [{method}] 搜索 \"{pattern}\"（从 {start_dir}）...\n")

    results = []

    if use_cmd:
        # CMD: dir /s /b | findstr
        try:
            native_dir = str(Path(start_dir))  # / -> \ on Windows
            r = subprocess.run(
                f'dir /s /b "{native_dir}" 2>nul | findstr /i /c:"{pattern}"',
                shell=True, capture_output=True, text=True, timeout=30,
            )
            for line in r.stdout.strip().split('\n')[:max_results]:
                line = line.strip()
                if line and os.path.exists(line):
                    p = Path(line)
                    results.append({
                        "name": p.name, "path": str(p),
                        "is_dir": p.is_dir(),
                        "size": p.stat().st_size if p.is_file() else 0,
                    })
        except Exception as e:
            print(f"CMD 搜索失败: {e}")

    elif use_ps:
        # PowerShell: Get-ChildItem -Recurse
        try:
            native_dir = str(Path(start_dir))
            r = subprocess.run(
                ['powershell', '-Command',
                 f'Get-ChildItem -Path "{native_dir}" -Recurse -Filter "*{pattern}*" '
                 f'-ErrorAction SilentlyContinue | Select-Object -First {max_results} '
                 f'| ForEach-Object {{ $_.FullName }}'],
                capture_output=True, text=True, timeout=30,
            )
            for line in r.stdout.strip().split('\n')[:max_results]:
                line = line.strip()
                if line and os.path.exists(line):
                    p = Path(line)
                    results.append({
                        "name": p.name, "path": str(p),
                        "is_dir": p.is_dir(),
                        "size": p.stat().st_size if p.is_file() else 0,
                    })
        except Exception as e:
            print(f"PowerShell 搜索失败: {e}")

    else:
        # Python os.walk (default)
        pattern_lower = pattern.lower()
        skip_dirs = {"node_modules", ".git", "__pycache__", "AppData",
                     ".venv", "venv", ".claude", ".npm", ".cargo"}
        try:
            for root, dirs, files in os.walk(start_dir):
                dirs[:] = [d for d in dirs
                           if not d.startswith(".") and d not in skip_dirs]
                for name in dirs + files:
                    if pattern_lower in name.lower():
                        full = Path(root) / name
                        results.append({
                            "name": name, "path": str(full),
                            "is_dir": full.is_dir(),
                            "size": full.stat().st_size if full.is_file() else 0,
                        })
                        if len(results) >= max_results:
                            raise StopIteration
        except StopIteration:
            pass

    if not results:
        print(f"未找到匹配 \"{pattern}\" 的文件/文件夹。")
        print(f"提示：使用 --cmd 用终端搜索，或 -d 指定其它目录。")
        return 1

    for r in results:
        icon = "📁" if r["is_dir"] else "📄"
        size_str = f"  ({r['size']:,} bytes)" if r["size"] else ""
        print(f"  {icon} {r['name']}{size_str}")
        print(f"     {r['path']}")

    print(f"\n找到 {len(results)} 个结果。")

    if open_first and results:
        r = results[0]
        path = r["path"]
        if r["is_dir"]:
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["explorer", "/select,", path])
        print(f"📂 已在资源管理器中打开：{r['name']}")

    return 0


def cmd_self_test(agent, args) -> int:
    """Run built-in self-tests so NL2Shortcut can verify itself."""
    import json as _json
    from .self_test import run_self_test, format_report
    report = run_self_test(include_live=args.live, host=args.host, port=args.port)
    if args.json:
        print(_json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


def main(args=None):
    from .mcp_server import cmd_mcp_server  # noqa: F401 — used in handlers dict
    import sys as _sys
    # If no subcommand given, default to "run" (AI Agent mode)
    if args is None:
        args = _sys.argv[1:]
    valid_cmds = {"exec","run","list","search","stats","benchmark","repl","gui",
                  "master","plan","suggest","start-server","stop-server","type",
                  "click","scroll","screenshot","mouse","find","overlay",
                  "mcp-server","agent-api","composite","workflow","self-test"}
    if args and args[0] not in valid_cmds and not args[0].startswith("-"):
        args = ["run"] + list(args)

    parser = build_parser()
    args = parser.parse_args(args)

    # Master Agent 专属命令（需 KeyboardMasterAgent）
    master_cmds = {"master", "plan", "suggest", "start-server", "stop-server", "workflow", "run"}
    if args.command in master_cmds:
        master = KeyboardMasterAgent()
        handlers = {
            "master": cmd_master,
            "plan": cmd_plan,
            "suggest": cmd_suggest,
            "start-server": cmd_start_server,
            "stop-server": cmd_stop_server,
            "workflow": cmd_workflow,
            "run": cmd_run,
        }
        sys.exit(handlers[args.command](master, args))

    agent = ShortcutAgent()
    handlers = {
        "exec": cmd_exec,
        "list": cmd_list,
        "search": cmd_search,
        "stats": cmd_stats,
        "benchmark": cmd_benchmark,
        "repl": cmd_repl,
        "gui": cmd_gui,
        "type": cmd_type,
        "click": cmd_click,
        "scroll": cmd_scroll,
        "screenshot": cmd_screenshot,
        "mouse": cmd_mouse,
        "find": cmd_find,
        "overlay": cmd_overlay,
        "agent-api": cmd_agent_api,
        "composite": cmd_composite,
        "mcp-server": cmd_mcp_server,
        "run": cmd_run,
        "self-test": cmd_self_test,
    }
    handler = handlers.get(args.command, cmd_run)  # default: AI Agent mode
    sys.exit(handler(agent, args))


if __name__ == "__main__":
    main()
