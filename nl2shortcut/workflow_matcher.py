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
import os
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
  "name": "工作流标识（候选列表中某个 name 或 display_name，二选一即可）",
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
- name 必须是候选列表中 EXISTS 的标识：可以用 ``name``（文件名），
  也可以用 ``display_name``（工作流的中文/显示名）；二者等价，不要编造。
- 用户可能用工作流的「中文显示名」来指代它（例如用户说"Git 提交并推送"，
  而候选里 display_name 正是 "Git 提交并推送"），此时应返回该 display_name。
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

        # 候选列表内存缓存 + mtime 失效判断
        # 原实现每次 match() 都 glob + load 所有 YAML，每个 yaml.safe_load ~1-5ms
        # 改为缓存 candidates 列表 + 记录每个文件的 mtime，仅当文件变化时重新加载
        self._candidates_cache: List[Dict[str, str]] = []
        self._candidates_sig: Dict[str, float] = {}  # path -> mtime
        self._candidates_dirty: bool = True

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

        # 1.5 快速文件名预匹配（免 LLM）
        # 当用户意图直接匹配工作流文件名（或去掉 -ok/-failed 后缀后匹配）时，
        # 跳过 LLM 直接返回，降低延迟。
        intent_lower = intent.strip().lower()
        for c in candidates:
            wf_name = c["name"]  # 文件名（不含扩展名）
            # 去掉常见后缀：-ok, -failed, -success
            stripped = wf_name
            for suffix in ("-ok", "-failed", "-success", "-ok", " ok", " failed"):
                if stripped.lower().endswith(suffix):
                    stripped = stripped[: -len(suffix)]
                    break
            # 精确匹配（忽略大小写）
            if intent_lower == wf_name.lower() or intent_lower == stripped.lower():
                elapsed = (time.perf_counter() - start) * 1000
                return MatchResult(
                    matched=True,
                    workflow=MatchedWorkflow(
                        name=wf_name,
                        description=c.get("description", ""),
                        confidence=0.95,
                        reasoning="文件名精确匹配",
                        source_path=c.get("source_path", ""),
                    ),
                    candidates=candidates,
                    elapsed_ms=elapsed,
                )
            # 意图包含完整工作流名（"帮我打开终端" 包含 "打开终端"）
            if len(stripped) >= 3 and stripped.lower() in intent_lower:
                elapsed = (time.perf_counter() - start) * 1000
                return MatchResult(
                    matched=True,
                    workflow=MatchedWorkflow(
                        name=wf_name,
                        description=c.get("description", ""),
                        confidence=0.88,
                        reasoning="意图包含工作流名",
                        source_path=c.get("source_path", ""),
                    ),
                    candidates=candidates,
                    elapsed_ms=elapsed,
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
                # 验证 LLM 返回的 name 是否真实存在（可能是文件名或显示名）
                raw_name = result["name"]
                hit = None
                for c in candidates:
                    if c["name"] == raw_name or c["display_name"] == raw_name:
                        hit = c
                        break
                if hit:
                    # 统一用文件名作为规范 name（load/run 两者都支持）
                    name = hit["name"]
                    match_result = MatchResult(
                        matched=True,
                        workflow=MatchedWorkflow(
                            name=name,
                            description=self._get_description(name, candidates),
                            confidence=float(result.get("confidence", 0.7)),
                            reasoning=result.get("reasoning", ""),
                            source_path=result.get("source_path", "") or hit.get("source_path", ""),
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
                        error=f"LLM 返回了不存在的工作流名: {raw_name}",
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
        """收集所有工作流 → 文件名 + 显示名(name 字段) + 描述列表。

        带内存缓存 + mtime 失效判断：仅当工作流文件被修改/新增/删除时
        才重新扫描 + 加载 YAML，否则直接返回缓存。
        """
        # ── 检查 mtime 是否变化 ──────────────────────────────────────
        current_sig: Dict[str, float] = {}
        all_files: List[Path] = []
        for d in self._wf_engine._all_dirs():
            all_files.extend(sorted(d.glob("*.yaml")))
            all_files.extend(sorted(d.glob("*.yml")))

        dirty = self._candidates_dirty
        if not dirty:
            # 快速路径：比对每个文件的 mtime，有变化才置 dirty
            current_paths = set()
            for p in all_files:
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    continue
                current_paths.add(str(p))
                cached_mtime = self._candidates_sig.get(str(p))
                if cached_mtime is None or cached_mtime != mtime:
                    dirty = True
                current_sig[str(p)] = mtime
            # 文件被删除也视为 dirty
            if not dirty and set(self._candidates_sig.keys()) != current_paths:
                dirty = True
        else:
            for p in all_files:
                try:
                    current_sig[str(p)] = os.path.getmtime(p)
                except OSError:
                    pass

        if not dirty:
            return self._candidates_cache

        # ── 重新加载 ──────────────────────────────────────────────────
        candidates: List[Dict[str, str]] = []
        seen: set[str] = set()
        for p in all_files:
            name = p.stem
            if name in seen:
                continue
            seen.add(name)
            desc = ""
            display_name = ""
            try:
                wf = self._wf_engine.load(name)
                if wf:
                    desc = wf.description or ""
                    display_name = wf.name or ""
            except Exception:
                pass
            candidates.append({
                "name": name,
                "display_name": display_name or name,
                "description": desc or "(无描述)",
                "source_path": str(p),
            })

        self._candidates_cache = candidates
        self._candidates_sig = current_sig
        self._candidates_dirty = False
        return candidates

    def invalidate_candidates_cache(self) -> None:
        """显式标记候选缓存失效（外部修改工作流目录后调用）。"""
        self._candidates_dirty = True

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
        # 构建候选列表文本（同时给出文件名 name 与显示名 display_name，
        # 让 LLM 能按中文/语义名称匹配）
        wf_lines = []
        for c in candidates:
            wf_lines.append(f"  - name: {c['name']}  display_name: {c['display_name']}")
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
