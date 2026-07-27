"""NL2Shortcut 自我测试套件（self-test）。

让 NL2Shortcut 自己验证自身是否正常：

  1. 模块导入        —— 核心模块是否都能成功 import
  2. 识别层          —— 自然语言 → 通道路由（简单 / 复合）+ 步骤拆分
  3. 复合计划工厂     —— 有序步骤结构 + 复制/移动语义正确性
  4. 键盘原语结构     —— AST 静态校验 keyboard_primitives（不依赖 GUI 运行时）
  5. selfcheck 模块   —— 执行后验证（_resolve_check / snapshot 安全调用）
        6. 端到端 dry-run   —— 经 ShortcutAgent 跑 dry_run，确认执行链不真正按键
        7. 等待时序（实跑） —— 非 dry-run 执行 wait 步骤，实测休眠时长≈配置值
        8. 实时端点（可选） —— 若服务器在跑，ping 健康/keys/capabilities/schema
        9. 实跑冒烟（可选, --smoke）—— 真正启动 server，经 HTTP 验证 wait 在服务端真的 sleep

设计原则：默认全部以 dry-run / 静态分析方式运行，安全、无副作用
（不会真正往系统注入任何按键）。第 9 类默认只做无害验证（server 健康 +
dry_run:true 下确认 plan 含 1500ms 等待）；真正 dry_run:false 发键并测量
服务端时延的部分默认关闭，需设置环境变量 NL2SHORTCUT_REAL_SMOKE=1 才开启
（开启后会真实发送 Win+E/输入/Enter 到当前桌面，请先保存工作）。
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import time
from dataclasses import dataclass, field, asdict

from typing import List, Dict, Any, Optional


@dataclass
class TestCase:
    name: str
    category: str
    ok: bool
    message: str = ""
    detail: Dict[str, Any] = field(default_factory=dict)


# ───────────────────────────────────────────────────────────────────────
# 公开入口
# ───────────────────────────────────────────────────────────────────────

def run_self_test(
    include_live: bool = False,
    host: str = "127.0.0.1",
    port: int = 7770,
    include_smoke: bool = False,
    smoke_port: int = 7799,
) -> Dict[str, Any]:
    """运行整套内置自检，返回结构化报告。

    {
      "ok": bool, "passed": int, "failed": int, "total": int,
      "runtime_s": float, "generated_at": str,
      "tests": [ {name, category, ok, message, detail}, ... ],
    }
    """
    tests: List[TestCase] = []
    t0 = time.perf_counter()

    tests += _test_imports()

    agent = _get_agent()
    if agent is not None:
        tests += _test_recognize(agent._intent)
        tests += _test_dryrun_exec(agent)
    else:
        tests.append(TestCase(
            "识别层/执行链: agent 不可用", "setup", False,
            "无法构建 ShortcutAgent（依赖是否完整？）"))

    tests += _test_composite_factory()
    tests += _test_wait_timing()
    tests += _test_primitives_structure()
    tests += _test_selfcheck()

    if include_live:
        tests += _test_live_endpoints(host, port)

    if include_smoke:
        tests += _test_smoke_server(host, smoke_port)

    passed = sum(1 for t in tests if t.ok)
    failed = sum(1 for t in tests if not t.ok)
    report = {
        "ok": failed == 0,
        "passed": passed,
        "failed": failed,
        "total": len(tests),
        "runtime_s": round(time.perf_counter() - t0, 3),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tests": [asdict(t) for t in tests],
    }
    return report


# ───────────────────────────────────────────────────────────────────────
# 1. 模块导入
# ───────────────────────────────────────────────────────────────────────

def _test_imports() -> List[TestCase]:
    out: List[TestCase] = []
    modules = [
        "nl2shortcut.intent",
        "nl2shortcut.composites",
        "nl2shortcut.selfcheck",
        "nl2shortcut.agent",
        "nl2shortcut.agent_api",
        "nl2shortcut.tiers",
        "nl2shortcut.planner",
        "nl2shortcut.vision_executor",
        "nl2shortcut.api_executor",
        "nl2shortcut.workflow",
        "nl2shortcut.operation_memory",
        "nl2shortcut.adapter",
        "nl2shortcut.models",
        "nl2shortcut.database",
        "nl2shortcut.cli",
        "nl2shortcut.self_test",
    ]
    for m in modules:
        try:
            importlib.import_module(m)
            out.append(TestCase(f"import: {m}", "import", True, "ok"))
        except Exception as e:
            out.append(TestCase(f"import: {m}", "import", False,
                                f"{type(e).__name__}: {e}"))
    return out


# ───────────────────────────────────────────────────────────────────────
# 2. 识别层（本地引擎）
# ───────────────────────────────────────────────────────────────────────

def _get_agent():
    """构造一个不启用 LLM 的 agent（无网络依赖，安全）。"""
    try:
        from .agent import ShortcutAgent
        return ShortcutAgent(enable_llm=False)
    except Exception:
        return None


def _test_recognize(engine) -> List[TestCase]:
    """验证自然语言 → 通道路由 + 复合步骤拆分。"""
    out: List[TestCase] = []
    # (输入文本, 是否期望复合通道)
    cases = [
        ("复制", False),                 # 简单通道 → copy
        ("保存", False),                 # 简单通道 → save
        ("把报告复制文件到桌面", True),    # 复合（有源）
        ("复制到桌面", True),            # 复合（无源，源补全为 *）
        ("把文件移动到下载", True),        # 复合（移动）
    ]
    for text, expect_composite in cases:
        try:
            r = engine.recognize(text)
        except Exception as e:
            out.append(TestCase(f"识别: {text}", "recognize", False, f"exception: {e}"))
            continue
        is_composite = (r.command == "__composite__")
        if is_composite != expect_composite:
            out.append(TestCase(
                f"识别: {text}", "recognize", False,
                f"期望 composite={expect_composite}, 实际 command={r.command!r}",
                {"command": r.command}))
            continue
        if is_composite:
            n = len(r.composite_plan.steps) if r.composite_plan else 0
            ok = r.composite_plan is not None and n > 0
            out.append(TestCase(
                f"识别: {text}", "recognize", ok,
                f"composite, steps={n}", {"steps": n}))
        else:
            ok = bool(r.command)
            out.append(TestCase(
                f"识别: {text}", "recognize", ok,
                f"command={r.command!r}, confidence={r.confidence:.2f}",
                {"command": r.command, "confidence": round(r.confidence, 3)}))
    return out


# ───────────────────────────────────────────────────────────────────────
# 3. 复合计划工厂
# ───────────────────────────────────────────────────────────────────────

def _test_composite_factory() -> List[TestCase]:
    from . import composites
    out: List[TestCase] = []
    valid_kinds = {"key", "type", "wait", "shell",
                   "vision_find", "click", "vision_ocr"}

    # 目标文件夹别名解析
    try:
        d1 = composites._resolve_dest_folder("桌面")
        d2 = composites._resolve_dest_folder("下载")
        ok = (d1.rstrip("\\/").endswith("Desktop")
              and d2.rstrip("\\/").endswith("Downloads"))
        out.append(TestCase(
            "工厂: _resolve_dest_folder 路径解析", "composite", ok,
            f"桌面→{d1} | 下载→{d2}"))
    except Exception as e:
        out.append(TestCase("工厂: _resolve_dest_folder", "composite", False, f"{e}"))

    # 复制工厂：真实 cmd 窗口 copy 工作流（无等待）
    # Win+R → 输入 cmd → Enter → 输入 copy 源 目标 → Enter
    try:
        plan = composites.make_copy_to_folder("报告", "桌面")
        steps = plan.steps
        kinds_ok = all(s.kind in valid_kinds for s in steps)
        desc_ok = all((s.description or "").strip() for s in steps)
        type_steps = [s for s in steps if s.kind == "type"]
        has_wait = any(s.kind == "wait" for s in steps)
        key_seq = [s.keys for s in steps if s.kind == "key"]
        has_cmd_open = any((s.text or "") == "cmd" for s in type_steps)
        has_command = any((s.text or "").startswith('copy ') for s in type_steps)
        last = steps[-1] if steps else None
        copy_ok = (len(steps) == 5 and kinds_ok and desc_ok
                   and has_cmd_open and has_command and not has_wait
                   and key_seq == ["Win+R", "Enter", "Enter"]
                   and last is not None and last.kind == "key"
                   and last.keys == "Enter")
        out.append(TestCase(
            "工厂: make_copy_to_folder 步骤结构", "composite", copy_ok,
            f"steps={len(steps)}, last={getattr(last, 'keys', None)}, "
            f"cmd_open={has_cmd_open}, command={has_command}, key_seq={key_seq}",
            {"n": len(steps), "kinds": sorted({s.kind for s in steps})}))
    except Exception as e:
        out.append(TestCase("工厂: make_copy_to_folder", "composite", False, f"{e}"))

    # 移动工厂：真实 cmd 窗口 move 工作流（无等待）
    # Win+R → 输入 cmd → Enter → 输入 move 源 目标 → Enter
    try:
        plan = composites.make_move_to_folder("报告", "下载")
        steps = plan.steps
        kinds_ok = all(s.kind in valid_kinds for s in steps)
        desc_ok = all((s.description or "").strip() for s in steps)
        type_steps = [s for s in steps if s.kind == "type"]
        has_wait = any(s.kind == "wait" for s in steps)
        key_seq = [s.keys for s in steps if s.kind == "key"]
        has_cmd_open = any((s.text or "") == "cmd" for s in type_steps)
        has_command = any((s.text or "").startswith('move ') for s in type_steps)
        last = steps[-1] if steps else None
        move_ok = (len(steps) == 5 and kinds_ok and desc_ok
                   and has_cmd_open and has_command and not has_wait
                   and key_seq == ["Win+R", "Enter", "Enter"]
                   and last is not None and last.kind == "key"
                   and last.keys == "Enter")
        out.append(TestCase(
            "工厂: make_move_to_folder 步骤结构", "composite", move_ok,
            f"steps={len(steps)}, last={getattr(last, 'keys', None)}, "
            f"cmd_open={has_cmd_open}, command={has_command}, key_seq={key_seq}",
            {"n": len(steps), "kinds": sorted({s.kind for s in steps})}))
    except Exception as e:
        out.append(TestCase("工厂: make_move_to_folder", "composite", False, f"{e}"))

    # 查找并复制工厂（两段式：先导航到目录 → 再按文件名搜索并复制）
    # 输入带目录的完整路径时，期望 13 步：
    #   Win+E → wait800 → Alt+D → Ctrl+A → type 目录 → wait800 → Enter
    #   → Ctrl+E → type 文件名 → Enter → wait800 → Space → Ctrl+C
    try:
        import os as _os
        fp = r"C:\Users\Deng2\Desktop\英语单词表.docx"
        folder, filename = _os.path.split(fp)
        plan = composites.make_find_and_copy(fp)
        steps = plan.steps
        kinds_ok = all(s.kind in valid_kinds for s in steps)
        desc_ok = all((s.description or "").strip() for s in steps)
        type_steps = [s for s in steps if s.kind == "type"]
        wait_steps = [s for s in steps
                      if s.kind == "wait" and getattr(s, "wait_ms", None) == 800]
        key_seq = [s.keys for s in steps if s.kind == "key"]
        typed_folder = any((s.text or "") == folder for s in type_steps)
        typed_filename = any((s.text or "") == filename for s in type_steps)
        last = steps[-1] if steps else None
        find_copy_ok = (len(steps) == 13 and kinds_ok and desc_ok
                        and typed_folder and typed_filename and bool(wait_steps)
                        and key_seq == ["Win+E", "Alt+D", "Ctrl+A", "Enter",
                                        "Ctrl+E", "Enter", "Space", "Ctrl+C"]
                        and last is not None and last.kind == "key"
                        and last.keys == "Ctrl+C")
        out.append(TestCase(
            "工厂: make_find_and_copy 步骤结构", "composite", find_copy_ok,
            f"steps={len(steps)}, typed_folder={typed_folder}, "
            f"typed_filename={typed_filename}, key_seq={key_seq}",
            {"n": len(steps), "kinds": sorted({s.kind for s in steps})}))
    except Exception as e:
        out.append(TestCase("工厂: make_find_and_copy", "composite", False, f"{e}"))

    return out


# ───────────────────────────────────────────────────────────────────────
# 3b. 等待时序（非 dry-run 实跑，验证真的会 sleep）
# ───────────────────────────────────────────────────────────────────────

def _test_wait_timing() -> List[TestCase]:
    """验证 wait 步骤在 非 dry-run 下真的会休眠指定时长（dry-run 只记成功不 sleep）。

    不依赖 GUI / 服务器：wait 步骤本身只调用 time.sleep，无需真实键盘 adapter。
    """
    from . import composites
    out: List[TestCase] = []
    wait_ms = 1500

    # A. 结构：make_copy_to_folder / make_move_to_folder 已改为「无等待」工作流，
    #    这里仅断言二者都不再包含任何 wait 步骤（反向验证需求：删除等待时间）。
    try:
        c_steps = composites.make_copy_to_folder("报告", "桌面").steps
        m_steps = composites.make_move_to_folder("报告", "下载").steps
        no_wait_ok = (not any(s.kind == "wait" for s in c_steps)
                      and not any(s.kind == "wait" for s in m_steps))
        out.append(TestCase(
            "时序: 复制/移动工作流不含任何 wait 步骤(已删除等待)",
            "timing", no_wait_ok,
            f"copy_wait={any(s.kind == 'wait' for s in c_steps)}, "
            f"move_wait={any(s.kind == 'wait' for s in m_steps)}"))
    except Exception as e:
        out.append(TestCase("时序: 复制/移动无等待结构", "timing", False, f"{e}"))

    # B. 真实计时：非 dry-run 执行一个 wait 步骤，测量实际 elapsed。
    try:
        plan = composites.CompositePlan(
            name="__selftest_wait__",
            description="wait timing",
            steps=[composites.CompositeStep(
                kind="wait", wait_ms=wait_ms, description="计时测试")])
        executor = composites.CompositeExecutor(adapter=None)
        t0 = time.perf_counter()
        res = executor.execute(plan, dry_run=False)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        # 容忍调度抖动：>= 0.7*wait 视为确实等待；<= 2*wait 防止异常挂起
        lower, upper = wait_ms * 0.7, wait_ms * 2.0
        step_ok = (isinstance(res, list) and len(res) == 1
                   and res[0].get("success") is True)
        timed_ok = (step_ok and lower <= elapsed_ms <= upper)
        out.append(TestCase(
            "时序: wait 步骤真实休眠(非 dry-run)",
            "timing", timed_ok,
            f"配置 {wait_ms}ms, 实测 {elapsed_ms:.0f}ms, success={step_ok}",
            {"configured_ms": wait_ms, "elapsed_ms": round(elapsed_ms, 1)}))
    except Exception as e:
        out.append(TestCase("时序: wait 步骤真实休眠", "timing", False, f"{e}"))

    return out


# ───────────────────────────────────────────────────────────────────────
# 4. 键盘原语结构（AST 静态校验，不 import GUI 运行时）
# ───────────────────────────────────────────────────────────────────────

def _test_primitives_structure() -> List[TestCase]:
    out: List[TestCase] = []
    try:
        spec = importlib.util.find_spec("nl2shortcut.keyboard_primitives")
        if spec is None or not spec.origin:
            out.append(TestCase("原语: keyboard_primitives 定位", "primitives",
                                False, "module spec not found"))
            return out
        with open(spec.origin, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        classes = [c.name for c in tree.body if isinstance(c, ast.ClassDef)]
        methods: set = set()
        for c in tree.body:
            if isinstance(c, ast.ClassDef):
                for m in c.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.add(m.name)
        has_class = "KeyboardPrimitives" in classes
        required_methods = {"type_text", "hotkey_combo", "enter",
                            "arrow", "tab", "shift_tab", "select_all"}
        has_methods = required_methods.issubset(methods)
        ok = has_class and has_methods
        out.append(TestCase(
            "原语: keyboard_primitives 结构", "primitives", ok,
            f"classes={classes[:4]}, methods={len(methods)}"))
    except SyntaxError as e:
        out.append(TestCase("原语: keyboard_primitives 语法", "primitives",
                            False, f"SyntaxError: {e}"))
    except Exception as e:
        out.append(TestCase("原语: keyboard_primitives 结构", "primitives",
                            False, f"{e}"))
    return out


# ───────────────────────────────────────────────────────────────────────
# 5. selfcheck 模块
# ───────────────────────────────────────────────────────────────────────

def _test_selfcheck() -> List[TestCase]:
    out: List[TestCase] = []
    try:
        import nl2shortcut.selfcheck as sc
        c1, _ = sc._resolve_check("copy")
        c2, _ = sc._resolve_check("undo")
        c3, _ = sc._resolve_check("paste", app_name="wt")  # 终端 Ctrl+C 是 SIGINT → noop
        ok1 = c1 == "clipboard"
        ok2 = c2 == "noop"
        ok3 = c3 == "noop"
        snap = sc.snapshot("copy")  # 不抛异常即可
        out.append(TestCase("自检: _resolve_check copy→clipboard", "selfcheck",
                            ok1, f"check={c1}"))
        out.append(TestCase("自检: _resolve_check undo→noop", "selfcheck",
                            ok2, f"check={c2}"))
        out.append(TestCase("自检: _resolve_check 终端paste→noop", "selfcheck",
                            ok3, f"check={c3}"))
        out.append(TestCase("自检: snapshot 安全调用", "selfcheck",
                            isinstance(snap, dict), f"check={snap.get('check')}"))
    except Exception as e:
        out.append(TestCase("自检: 模块/函数", "selfcheck", False,
                            f"{type(e).__name__}: {e}"))
    return out


# ───────────────────────────────────────────────────────────────────────
# 6. 端到端 dry-run 执行链
# ───────────────────────────────────────────────────────────────────────

def _test_dryrun_exec(agent) -> List[TestCase]:
    out: List[TestCase] = []
    # (输入文本, 期望模式)
    cases = [
        ("复制", "simple"),
        ("把报告复制文件到桌面", "composite"),
    ]
    for text, expect_mode in cases:
        try:
            r = agent.execute(text, dry_run=True)
            ok = bool(r.success) and bool(r.dry_run)
            if expect_mode == "composite":
                ok = ok and r.composite_plan is not None
            out.append(TestCase(
                f"执行(dry-run): {text}", "execute", ok,
                f"mode={r.mode}, success={r.success}",
                {"mode": r.mode}))
        except Exception as e:
            out.append(TestCase(
                f"执行(dry-run): {text}", "execute", False,
                f"{type(e).__name__}: {e}"))
    return out


# ───────────────────────────────────────────────────────────────────────
# 7. 实时端点（可选）
# ───────────────────────────────────────────────────────────────────────

def _live_auth_header() -> Dict[str, str]:
    keys = os.environ.get("NL2SHORTCUT_API_KEYS", "")
    key = keys.split(",")[0].strip() if keys else "nl2shortcut_dev_local"
    return {"Authorization": f"Bearer {key}"}


def _test_live_endpoints(host: str, port: int) -> List[TestCase]:
    import urllib.request
    import json
    out: List[TestCase] = []
    base = f"http://{host}:{port}"
    endpoints = [
        ("GET", "/v1/health"),
        ("GET", "/v1/keys"),
        ("GET", "/v1/capabilities"),
        ("GET", "/v1/schema"),
    ]
    for method, path in endpoints:
        try:
            req = urllib.request.Request(
                base + path, method=method, headers=_live_auth_header())
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read())
            ok = isinstance(data, dict)
            out.append(TestCase(f"端点: {method} {path}", "live", ok,
                                f"status={resp.status}"))
        except Exception as e:
            out.append(TestCase(f"端点: {method} {path}", "live", False,
                                f"{type(e).__name__}: {e}"))
    return out


# ───────────────────────────────────────────────────────────────────────
# 9. 实跑冒烟（可选, --smoke）：真正启动 server，经 HTTP 验证 wait 在服务端真的 sleep
# ───────────────────────────────────────────────────────────────────────

_DEV_KEY = "nl2shortcut_dev_local"


def _test_smoke_server(host: str = "127.0.0.1", port: int = 7799) -> List[TestCase]:
    """实跑冒烟：启动真实 server，经 HTTP 证明「输入路径→回车前」的 1500ms 等待
    在服务端确实执行了 time.sleep，而非仅 dry-run 占位。

    安全分层：
      · 默认（无害）：server 健康 + dry_run:true 下确认 resolved plan 含 1500ms 等待。
        dry_run 不会向系统注入任何按键。
      · 真实发键（需 NL2SHORTCUT_REAL_SMOKE=1）：POST dry_run:false，测量服务端
        真实耗时（execution_time_ms）与客户端 RTT，断言 >= 0.7×1500ms，从而证明
        server 进程里那段 time.sleep 真的跑了。开启前请保存工作、关闭敏感窗口。
    """
    import subprocess
    import sys
    import urllib.request
    import urllib.error
    import json

    out: List[TestCase] = []
    base = f"http://{host}:{port}"
    wait_ms = 1500
    proc = None
    started_by_us = False

    def _hdr() -> Dict[str, str]:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {_DEV_KEY}"}

    def _health():
        try:
            req = urllib.request.Request(base + "/v1/health", headers=_hdr())
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read())
        except Exception:
            return None

    def _post(payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            base + "/v1/execute", data=data, method="POST", headers=_hdr())
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read()), e.code
            except Exception:
                return {}, e.code

    try:
        # 1) 启动 server（端口已有人在跑则复用，绝不杀别人的进程）
        if _health() is None:
            proc = subprocess.Popen(
                [sys.executable, "-m", "nl2shortcut", "agent-api",
                 "--host", host, "--port", str(port)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started_by_us = True
            for _ in range(20):
                time.sleep(0.5)
                if _health() is not None:
                    break
        info = _health()
        out.append(TestCase(
            "冒烟: server 启动/健康", "smoke", info is not None,
            f"port={port}, version={info.get('version') if info else None}"))
        if info is None:
            return out

        # 2) 无害验证：dry_run:true 下，确认某复合意图的 plan 含 1500ms 等待步骤
        candidate_intents = ["打开桌面", "打开此电脑", "把报告复制文件到桌面"]
        chosen = None
        plan_wait = None
        for intent in candidate_intents:
            try:
                body, _ = _post({"intent": intent, "dry_run": True})
            except Exception:
                continue
            plan = body.get("composite_plan")
            if plan is None and isinstance(body.get("result"), dict):
                plan = body["result"].get("composite_plan")
            steps = (plan or {}).get("steps", []) if isinstance(plan, dict) else []
            wm = [s.get("wait_ms") for s in steps
                  if s.get("kind") == "wait" and s.get("wait_ms") == wait_ms]
            if wm:
                chosen, plan_wait = intent, wm
                break
        out.append(TestCase(
            "冒烟: 服务端 plan 含 1500ms 等待(dry_run)", "smoke",
            chosen is not None,
            f"intent={chosen}, wait_steps={plan_wait}"))

        # 3) 真实发键 + 测量服务端时延（默认关闭，需 NL2SHORTCUT_REAL_SMOKE=1）
        if os.environ.get("NL2SHORTCUT_REAL_SMOKE") == "1":
            if chosen is None:
                out.append(TestCase(
                    "冒烟: 真实 sleep 服务端生效(dry_run:false)", "smoke",
                    False, "未找到可用的复合意图，无法实跑"))
            else:
                t0 = time.perf_counter()
                body, _ = _post({"intent": chosen, "dry_run": False})
                rtt_ms = (time.perf_counter() - t0) * 1000
                exec_ms = body.get("execution_time_ms")
                if exec_ms is None and isinstance(body.get("result"), dict):
                    exec_ms = body["result"].get("execution_time_ms")
                server_ms = exec_ms if exec_ms else rtt_ms
                lower = wait_ms * 0.7
                ok_real = server_ms >= lower
                out.append(TestCase(
                    "冒烟: 真实 sleep 服务端生效(dry_run:false)", "smoke",
                    ok_real,
                    f"intent={chosen}, 服务端耗时={server_ms:.0f}ms, "
                    f"客户端RTT={rtt_ms:.0f}ms (期望≥{lower:.0f}ms)",
                    {"rtt_ms": round(rtt_ms, 1),
                     "server_ms": round(server_ms, 1)}))
        else:
            out.append(TestCase(
                "冒烟: 真实 sleep(可选, 未开启)", "smoke", True,
                "设置 NL2SHORTCUT_REAL_SMOKE=1 可开启真实发键+服务端时延测量"))
        return out
    except Exception as e:
        out.append(TestCase("冒烟: 异常", "smoke", False,
                            f"{type(e).__name__}: {e}"))
        return out
    finally:
        if proc is not None and started_by_us:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


# ───────────────────────────────────────────────────────────────────────
# 友好打印（供 CLI / __main__ 使用）
# ───────────────────────────────────────────────────────────────────────

def format_report(report: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"NL2Shortcut self-test — {report['generated_at']}")
    verdict = "PASS" if report["ok"] else "FAIL"
    lines.append(
        f"[{verdict}] {report['passed']}/{report['total']} passed, "
        f"{report['failed']} failed, {report['runtime_s']}s\n")
    for t in report["tests"]:
        mark = "✓" if t["ok"] else "✗"
        lines.append(f"  {mark} [{t['category'].ljust(10)}] {t['name']}")
        if not t["ok"]:
            lines.append(f"        -> {t['message']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    include_smoke = "--smoke" in sys.argv
    rep = run_self_test(include_smoke=include_smoke)
    print(format_report(rep))
    raise SystemExit(0 if rep["ok"] else 1)
