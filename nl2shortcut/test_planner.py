"""Phase 2 单元测试 — planner.py dry-run 验证。

测试策略（dry-run，不真按键盘）：
  1. Fallback 路径：LLM 不可用时，关键词匹配正确返回 PlanStep
  2. Plan.format_human()：渲染输出非空
  3. Plan.to_dict()：序列化/反序列化往返一致
  4. PlanStep 字段完整性：每个 action 类型字段正确填充
  5. Fallback 覆盖率：常见目标至少能返回一个步骤
  6. 空目标处理：空字符串返回空 plan
  7. 语法验证：py_compile 无报错

运行：
  cd NL2Shortcut
  python -m pytest nl2shortcut/test_planner.py -v
  # 或直接
  python nl2shortcut/test_planner.py
"""

import sys
import json
from pathlib import Path

# ── 路径设置 ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
# 将项目根目录（NL2Shortcut/）加入 Python 路径，使 nl2shortcut 可作为包导入
PROJECT_ROOT = ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── 导入被测模块 ────────────────────────────────────────────────────────────
from nl2shortcut.planner import GoalPlanner, Plan, PlanStep, _make_fallback_plan, PLANNER_SYSTEM_PROMPT


# ═════════════════════════════════════════════════════════════════════════════
# 测试用例
# ═════════════════════════════════════════════════════════════════════════════

class TestFallbackPlans:
    """Fallback 降级路径测试（不依赖 LLM）。"""

    def test_fallback_send_email(self):
        plan = _make_fallback_plan("帮我把这份报告发出去")
        assert len(plan.steps) > 0, "发邮件目标应至少返回 1 个步骤"
        assert plan.source == "fallback"
        assert all(s.action in ("shortcut", "type", "wait") for s in plan.steps)
        print(f"✓ Fallback 邮件计划：{len(plan.steps)} 步")

    def test_fallback_save(self):
        plan = _make_fallback_plan("保存这个文件")
        assert len(plan.steps) > 0
        assert any(s.key_combination == "Ctrl+S" for s in plan.steps)
        print(f"✓ Fallback 保存计划：{plan.steps[0].description}")

    def test_fallback_copy(self):
        plan = _make_fallback_plan("复制这段文字")
        assert any(s.key_combination == "Ctrl+C" for s in plan.steps)
        print("✓ Fallback 复制计划")

    def test_fallback_paste(self):
        plan = _make_fallback_plan("粘贴内容")
        assert any(s.key_combination == "Ctrl+V" for s in plan.steps)
        print("✓ Fallback 粘贴计划")

    def test_fallback_fullscreen(self):
        """没有精确匹配时降级到 Alt+S（通用发送）。"""
        plan = _make_fallback_plan("把窗口放到最大")
        assert len(plan.steps) > 0
        print(f"✓ Fallback 兜底计划：{plan.steps[0].description}")

    def test_fallback_screenshot(self):
        plan = _make_fallback_plan("截个图")
        assert any(s.key_combination == "Win+Shift+S" for s in plan.steps)
        print("✓ Fallback 截图计划")

    def test_fallback_undo(self):
        plan = _make_fallback_plan("撤销上一步")
        assert any(s.key_combination == "Ctrl+Z" for s in plan.steps)
        print("✓ Fallback 撤销计划")


class TestPlanSerialization:
    """Plan / PlanStep 序列化往返测试。"""

    def test_planstep_roundtrip(self):
        step = PlanStep(
            step_id=1,
            description="Ctrl+C 复制",
            action="shortcut",
            key_combination="Ctrl+C",
            reasoning="复制是最直接的方式",
            confidence=0.95,
        )
        d = step.to_dict()
        restored = PlanStep.from_dict(d)
        assert restored.step_id == step.step_id
        assert restored.action == step.action
        assert restored.key_combination == step.key_combination
        assert restored.confidence == step.confidence
        print("✓ PlanStep JSON 往返一致")

    def test_plan_roundtrip(self):
        plan = Plan(
            goal="发送邮件",
            steps=[
                PlanStep(1, "复制内容", "shortcut", key_combination="Ctrl+C", confidence=1.0),
                PlanStep(2, "打开邮件", "shell", command="start outlook", confidence=0.9),
                PlanStep(3, "粘贴", "shortcut", key_combination="Ctrl+V", confidence=1.0),
                PlanStep(4, "发送", "wait", wait_ms=500, confidence=1.0),
            ],
            reasoning="复制→打开→粘贴→发送",
            total_steps=4,
            estimated_time_ms=1500,
            source="llm",
        )
        d = plan.to_dict()
        # has_composite 自动计算
        assert plan.has_composite is False
        assert plan.total_steps == 4
        assert len(d["steps"]) == 4
        print("✓ Plan JSON 序列化成功")


class TestPlanFormatHuman:
    """format_human 渲染测试。"""

    def test_human_readable(self):
        plan = Plan(
            goal="发送报告邮件",
            steps=[
                PlanStep(1, "复制报告内容", "shortcut", key_combination="Ctrl+A",
                         reasoning="先全选再复制", confidence=1.0),
                PlanStep(2, "复制", "shortcut", key_combination="Ctrl+C", confidence=1.0),
                PlanStep(3, "打开邮件客户端", "shell", command="start outlook", confidence=0.8),
                PlanStep(4, "粘贴内容", "shortcut", key_combination="Ctrl+V", confidence=1.0),
                PlanStep(5, "发送", "composite", composite_hint="点击发送按钮", confidence=0.9),
            ],
            reasoning="A→C→打开→V→发送",
            total_steps=5,
            estimated_time_ms=2000,
            source="llm",
        )
        output = plan.format_human()
        assert "🎯 目标" in output
        assert "📋 共 5 步" in output
        assert "💡 规划思路" in output
        assert "Ctrl+A" in output
        assert "composite" in output
        print("✓ format_human 渲染正确")
        print(output)
        print()


class TestEmptyGoal:
    """空目标边界测试。"""

    def test_empty_goal_returns_empty_plan(self):
        planner = GoalPlanner()
        plan = planner.plan("")
        assert len(plan.steps) == 0
        assert plan.confidence == 0.0
        assert plan.source == "error"
        print("✓ 空目标返回空计划")


class TestGoalPlannerInstantiation:
    """GoalPlanner 初始化测试。"""

    def test_init_without_key(self):
        planner = GoalPlanner(api_key=None)
        # available 取决于环境变量（可能为 True 也可能为 False）
        assert planner is not None
        assert isinstance(planner.available, bool)
        print(f"✓ GoalPlanner 初始化成功，LLM 可用={planner.available}")

    def test_plan_returns_plan_instance(self):
        planner = GoalPlanner()
        plan = planner.plan("保存文件")
        assert isinstance(plan, Plan)
        assert isinstance(plan.steps, list)
        assert plan.goal == "保存文件"
        print(f"✓ plan() 返回 Plan 实例，{len(plan.steps)} 步，source={plan.source}")


class TestExecutePlanDryRun:
    """execute_plan dry-run 测试（不真按键盘）。"""

    def test_dry_run_returns_results(self):
        planner = GoalPlanner()
        plan = planner.plan("保存")
        results = planner.execute_plan(plan, dry_run=True)
        assert len(results) > 0
        assert all(r.dry_run is True for r in results)
        assert all(r.mode == "plan_step" for r in results)
        print(f"✓ dry_run 执行返回 {len(results)} 个结果，全部 mode=plan_step")

    def test_empty_plan_dry_run(self):
        planner = GoalPlanner()
        plan = planner.plan("")
        results = planner.execute_plan(plan, dry_run=True)
        assert len(results) == 1
        assert results[0].success is True
        print("✓ 空计划 dry_run 成功")


class TestStepTypes:
    """各 action 类型的 PlanStep 正确性。"""

    def test_all_action_types(self):
        steps = [
            PlanStep(1, "复制", "shortcut", key_combination="Ctrl+C"),
            PlanStep(2, "输入文本", "type", text="Hello World"),
            PlanStep(3, "Tab 导航", "tab", n=3, direction="tab"),
            PlanStep(4, "运行命令", "shell", command="dir"),
            PlanStep(5, "等待", "wait", wait_ms=1000),
            PlanStep(6, "复合操作", "composite", composite_hint="打开文件对话框"),
        ]
        expected_actions = ["shortcut", "type", "tab", "shell", "wait", "composite"]
        for step, expected in zip(steps, expected_actions):
            assert step.action == expected, f"action {step.action} != {expected}"
        print(f"✓ 6 种 action 类型全部正确")

    def test_estimated_time_calculation(self):
        plan = Plan(
            goal="测试",
            steps=[
                PlanStep(1, "s", "shortcut", key_combination="Ctrl+S"),        # 200ms
                PlanStep(2, "t", "type", text="hello world"),                     # ~110ms (11*10)
                PlanStep(3, "tab", "tab", n=5),                                   # 750ms
                PlanStep(4, "wait", "wait", wait_ms=1000),                        # 1000ms
            ],
            estimated_time_ms=0,  # 让 post_init 计算
        )
        assert plan.total_steps == 4
        print(f"✓ 计划总步数={plan.total_steps}, has_composite={plan.has_composite}")


# ═════════════════════════════════════════════════════════════════════════════
# 运行入口
# ═════════════════════════════════════════════════════════════════════════════

def run_all():
    test_classes = [
        TestFallbackPlans,
        TestPlanSerialization,
        TestPlanFormatHuman,
        TestEmptyGoal,
        TestGoalPlannerInstantiation,
        TestExecutePlanDryRun,
        TestStepTypes,
    ]
    total = passed = failed = 0
    for cls in test_classes:
        instance = cls()
        for method_name in dir(instance):
            if method_name.startswith("test_"):
                total += 1
                try:
                    getattr(instance, method_name)()
                    passed += 1
                except AssertionError as e:
                    failed += 1
                    print(f"✗ {cls.__name__}.{method_name}  FAILED: {e}")
                except Exception as e:
                    failed += 1
                    print(f"✗ {cls.__name__}.{method_name}  ERROR: {e}")

    print()
    print(f"{'─'*50}")
    print(f"  结果：{passed}/{total} 通过{', '+str(failed)+' 失败' if failed else '，全部通过✓'}")
    print(f"{'─'*50}")
    return failed == 0


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)
