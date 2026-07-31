"""Workflow Matcher — LLM 语义匹配已有 YAML 工作流。

每次收到用户自然语言指令时，先检查是否已有匹配的工作流，
有就直接执行（免拆解），没有才走 Planner → 拆解 → 执行 → 自动保存。

设计
────
WorkflowMatcher
  ├── llm.py::DeepSeekEngine          ← LLM 语义理解
  ├── workflow.py::WorkflowEngine      ← 列出 / 加载工作流
  └── planner.py::GoalPlanner          ← 回退规划（无匹配时）
"""

from __future__ import annotations

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Any

from .llm import _load_api_key, DEEPSEEK_BASE_URL, DEEPSEEK_CHAT_MODEL, REQUEST_TIMEOUT
from .workflow import WorkflowEngine
from .context_store import SemanticCache, MinimalContext
from .model_router import ModelRouter

# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MatchedWorkflow:
    """LLM 返回的工作流匹配结果。"""
    name: str
    description: str = ""
    confidence: float = 0.0
    reasoning: str = ""
    # 工作流文件路径
    source_path: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "source_path": self.source_path,
        }


@dataclass
class MatchResult:
    """一次完整的匹配查询结果。"""
    matched: bool = False
    workflow: Optional[MatchedWorkflow] = None
    # 所有候选工作流（用于调试/日志）
    candidates: List[Dict[str, str]] = field(default_factory=list)
    # 匹配耗时
    elapsed_ms: float = 0.0
    error: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# LLM System Prompt
# ─────────────────────────────────────────────────────────────────────────────

_MATCHER_SYSTEM_PROMPT = """\
你是一个智能工作流匹配器。

用户会用自然语言描述想做的事情。你的任务是：从已有的工作流列表中，
找到语义最匹配的那一个。

## 判断规则（按优先级）

1. **字面语义匹配**：用户意图与工作流描述一致 → 高置信度 (0.85-0.95)
2. **同义/近义匹配**：表述不同但意思相同 → 中置信度 (0.6-0.8)
   - 例如 "推代码" ≈ "deploy", "发报告" ≈ "send_email"
3. **部分匹配**：能覆盖部分步骤，但工作流过于宽泛 → 低置信度 (0.4-0.6)
4. **无匹配**：没有任何工作流能对应用户意图 → 返回空匹配

## 输出格式（严格 JSON，无 markdown）

有匹配时：
{
  "matched": true,
  "name": "工作流文件名（不含 .yaml 扩展名）",
  "confidence": 0.85,
  "reasoning": "为什么选这个（1句话）"
}

无匹配时：
{
  "matched": false,
  "reasoning": "为什么无法匹配（1句话）"
}

**重要**：
- 只输出 JSON，不要任何其他文字。
- name 必须是候选列表中 EXISTS 的那个名字，不要编造。
- 如果用户的意图虽然与某个 worklows 名称相近但描述完全无关，不要强行匹配。
"""


# ─────────────────────────────────────────────────────────────────────────────
# WorkflowMatcher
# ─────────────────────────────────────────────────────────────────────────────

class WorkflowMatcher:
    """通过 LLM 语义匹配已有的 YAML 工作流。

    用法
    ----
        matcher = WorkflowMatcher()
        result = matcher.match("帮我把代码推上去")
        if result.matched:
            print(f"找到工作流: {result.workflow.name}")
            # 用 WorkflowEngine 加载并执行
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        workflows_dir: Optional[Path] = None,
        shared_cache: Optional[SemanticCache] = None,
        router: Optional[ModelRouter] = None,
    ):
        self._api_key = api_key or _load_api_key()
        self._available = bool(self._api_key)
        self._last_error: Optional[str] = None

        # 语义缓存：可接受外部共享实例
        self._cache = shared_cache or SemanticCache()

        # 模型路由：AVR 调度（可选）
        self._router = router

        # WorkflowEngine — 用于列出/加载已有工作流
        self._wf_engine = WorkflowEngine.__new__(WorkflowEngine)
        self._wf_engine._workflows_dir = workflows_dir or (
            Path.home() / ".nl2shortcut" / "workflows"
        )
        self._wf_engine._workflows_dir.mkdir(parents=True, exist_ok=True)
        self._wf_engine._local_dir = Path.cwd() / ".nl2shortcut" / "workflows"

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def set_api_key(self, key: str) -> bool:
        if not key or not key.startswith("sk-"):
            return False
        self._api_key = key
        self._available = True
        self._last_error = None
        return True

    # ── Public API ─────────────────────────────────────────────────────────

    def match(self, intent: str, app_name: str = "",
              ctx: Optional[MinimalContext] = None) -> MatchResult:
        """用 LLM 在已有工作流中找语义最佳匹配。

        Args:
            intent: 用户自然语言指令，如 "把代码推上去"
            app_name: 当前应用名（用于缓存键区分）

        Returns:
            MatchResult，matched=True 时 workflow 字段非空。
        """
        start = time.perf_counter()

        # 0. 先查缓存（精确+模糊匹配，带 TTL）
        cached = self._cache.get(intent, app_name)
        if cached is not None:
            elapsed = (time.perf_counter() - start) * 1000
            cached_result = _restore_match_result(cached)
            if cached_result is not None:
                cached_result.elapsed_ms = elapsed
                return cached_result

        # ── AVR 路由决策（若配置了 router 且有 ctx）──
        if self._router and ctx:
            decision = self._router.route(ctx)
            if not decision.should_call_llm:
                elapsed = (time.perf_counter() - start) * 1000
                return MatchResult(
                    matched=False, candidates=[],
                    elapsed_ms=elapsed,
                    error=f"AVR 跳过 LLM: {decision.reason}",
                )

        # 1. 收集所有已有工作流的名称和描述
        candidates = self._collect_candidates()
        if not candidates:
            elapsed = (time.perf_counter() - start) * 1000
            return MatchResult(
                matched=False,
                candidates=candidates,
                elapsed_ms=elapsed,
                error="" if self._api_key else "LLM API Key 未配置",
            )

        # 2. 如果没有 API Key，无法做语义匹配，直接返回无匹配
        if not self._available:
            elapsed = (time.perf_counter() - start) * 1000
            return MatchResult(
                matched=False,
                candidates=candidates,
                elapsed_ms=elapsed,
                error="DeepSeek API Key 未配置（设置 DEEPSEEK_API_KEY 环境变量）",
            )

        # 3. 调用 LLM 做语义匹配
        try:
            result = self._call_llm(intent, candidates)
            elapsed = (time.perf_counter() - start) * 1000

            if result.get("matched") and result.get("name"):
                # 验证 LLM 返回的 name 是否真实存在
                name = result["name"]
                if any(c["name"] == name for c in candidates):
                    match_result = MatchResult(
                        matched=True,
                        workflow=MatchedWorkflow(
                            name=name,
                            description=self._get_description(name, candidates),
                            confidence=float(result.get("confidence", 0.7)),
                            reasoning=result.get("reasoning", ""),
                            source_path=result.get("source_path", ""),
                        ),
                        candidates=candidates,
                        elapsed_ms=elapsed,
                    )
                    # 写入缓存
                    self._cache.set(intent, app_name, match_result.to_dict() if hasattr(match_result, 'to_dict') else _match_to_cache(match_result))
                    return match_result
                else:
                    # LLM 编造了名字 → 回退为无匹配
                    no_match = MatchResult(
                        matched=False,
                        candidates=candidates,
                        elapsed_ms=elapsed,
                        error=f"LLM 返回了不存在的工作流名: {name}",
                    )
                    return no_match

            no_match = MatchResult(
                matched=False,
                candidates=candidates,
                elapsed_ms=elapsed,
                error="",
            )
            return no_match

        except Exception as e:
            self._last_error = str(e)
            elapsed = (time.perf_counter() - start) * 1000
            return MatchResult(
                matched=False,
                candidates=candidates,
                elapsed_ms=elapsed,
                error=str(e),
            )

    # ── 内部实现 ────────────────────────────────────────────────────────────

    def _collect_candidates(self) -> List[Dict[str, str]]:
        """收集所有 .yaml 工作流文件 → 名称 + 描述列表。"""
        candidates: List[Dict[str, str]] = []
        seen: set[str] = set()
        for d in self._wf_engine._all_dirs():
            for p in sorted(d.glob("*.yaml")):
                name = p.stem
                if name in seen:
                    continue
                seen.add(name)
                desc = ""
                try:
                    wf = self._wf_engine.load(name)
                    if wf:
                        desc = wf.description or ""
                except Exception:
                    pass
                candidates.append({
                    "name": name,
                    "description": desc or "(无描述)",
                    "source_path": str(p),
                })
        return candidates

    @staticmethod
    def _get_description(name: str, candidates: List[Dict[str, str]]) -> str:
        for c in candidates:
            if c["name"] == name:
                return c["description"]
        return ""

    def _call_llm(
        self,
        intent: str,
        candidates: List[Dict[str, str]],
    ) -> Dict[str, Any]:
        """调用 DeepSeek LLM 做工作流语义匹配。"""
        # 构建候选列表文本
        wf_lines = []
        for c in candidates:
            wf_lines.append(f"  - name: {c['name']}")
            if c.get("description"):
                wf_lines.append(f"    description: {c['description']}")
        wf_text = "\n".join(wf_lines) if wf_lines else "（暂无工作流）"

        user_prompt = (
            f"用户意图：{intent}\n\n"
            f"已有工作流列表：\n{wf_text}\n\n"
            f"请找出最匹配用户意图的工作流。如果没有匹配的，返回 matched=false。"
        )

        payload = json.dumps({
            "model": DEEPSEEK_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": _MATCHER_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 300,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {err_body[:200]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}")

        content = body["choices"][0]["message"]["content"].strip()

        # 解析 JSON（处理 markdown 代码块包裹）
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(
                lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
            )

        import re
        json_match = re.search(r'\{[\s\S]*\}', content)
        if not json_match:
            raise RuntimeError(f"无法从 LLM 响应中提取 JSON: {content[:100]}")

        return json.loads(json_match.group())


# ─────────────────────────────────────────────────────────────────────────
# 缓存辅助函数
# ─────────────────────────────────────────────────────────────────────────

def _match_to_cache(result: MatchResult) -> dict:
    """将 MatchResult 序列化为可缓存的 dict。"""
    d = {
        "matched": result.matched,
        "candidates": result.candidates,
        "elapsed_ms": result.elapsed_ms,
        "error": result.error,
    }
    if result.workflow:
        d["workflow"] = result.workflow.to_dict()
    return d


def _restore_match_result(d: dict) -> Optional[MatchResult]:
    """从缓存 dict 恢复 MatchResult。"""
    try:
        wf = None
        if d.get("workflow"):
            wf = MatchedWorkflow(
                name=d["workflow"].get("name", ""),
                description=d["workflow"].get("description", ""),
                confidence=d["workflow"].get("confidence", 0.0),
                reasoning=d["workflow"].get("reasoning", ""),
                source_path=d["workflow"].get("source_path", ""),
            )
        return MatchResult(
            matched=d.get("matched", False),
            workflow=wf,
            candidates=d.get("candidates", []),
            elapsed_ms=d.get("elapsed_ms", 0.0),
            error=d.get("error", ""),
        )
    except Exception:
        return None
