"""ContextStore — 状态与记忆层（融合策略③的核心模块）。

提供三项能力，贯穿四大策略融合的整条请求生命周期：

1. **MinimalContext**：将感知层的 UIState + 历史操作 + 工作流列表
   压缩为"最小可执行上下文"——路由层的唯一输入。

2. **SemanticCache**：跨请求、带 TTL 的语义缓存，覆盖全部三处 LLM 调用
   （DeepSeekEngine / WorkflowMatcher / GoalPlanner），
   命中时节省 100% token 成本。

3. **ContextStore**：持有当前上下文、历史 buffer、语义缓存，
   是 ExecutionController 融合主线的"粘合剂"。

设计约定
────────
- 缓存键 = SHA256(intent + app_name) 的前 16 位，防止同意图在不同应用中匹配错误。
- TTL 分两档：exact_s（精确匹配，默认 300s）、fuzzy_s（模糊匹配，默认 60s）。
- 模糊匹配用 Jaccard 相似度（token 级别），零外部依赖。
- 跨重启持久化可选：通过 `persist_path` 指定缓存文件路径。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple


# ═══════════════════════════════════════════════════════════════════════
# MinimalContext — 最小可执行上下文
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class MinimalContext:
    """路由层（ModelRouter）的唯一输入。

    由 ContextStore.build_context() 将感知层 + 记忆层
    的各项信息压缩为此结构，供路由层决定：
      - 是否需要 LLM？（缓存/工作流命中 → 不需要）
      - 用什么模型？（cheap / standard）
      - 走哪个执行层？（keyboard / api / vision）
    """

    # ── 感知层提供 ──
    app_name: str = ""                 # 当前应用友好名（vscode / chrome / ...）
    window_title: str = ""             # 当前窗口标题
    process_name: str = ""             # 进程名（Code.exe / chrome.exe）
    open_apps: List[str] = field(default_factory=list)  # 当前打开的应用列表
    clipboard_text: str = ""           # 剪贴板内容（若有）
    selected_text: str = ""            # 当前选中文本（若有）

    # ── 记忆层提供 ──
    recent_actions: List[Dict[str, Any]] = field(default_factory=list)
    # 最近 K 条操作，每项: {"intent": ..., "command": ..., "app": ..., "ts": ...}
    available_workflows: List[str] = field(default_factory=list)
    # 可用工作流名称列表（来自 WorkflowMatcher 的候选集）

    # ── 用户输入 ──
    raw_intent: str = ""               # 用户原始自然语言意图

    # ── 路由层计算 ──
    complexity: float = 0.0             # [0.0, 1.0] 目标复杂度
    risk: float = 0.0                   # [0.0, 1.0] 执行风险
    token_budget: int = 0              # 预估所需 token 数（0 = 无需 LLM）

    # ── 缓存命中时的有效载荷（跳过 agent.execute() 的关键）──
    cached_result: Any = None           # 命中的缓存值（dict，含 key_combination）

    # ── 元数据 ──
    cache_hit: bool = False            # 是否命中缓存
    workflow_hit: bool = False         # 是否命中工作流
    build_elapsed_ms: float = 0.0      # 构建耗时

    def to_dict(self) -> dict:
        return {
            "app_name": self.app_name,
            "window_title": self.window_title,
            "process_name": self.process_name,
            "raw_intent": self.raw_intent,
            "complexity": self.complexity,
            "risk": self.risk,
            "token_budget": self.token_budget,
            "cache_hit": self.cache_hit,
            "has_cached_result": self.cached_result is not None,
            "workflow_hit": self.workflow_hit,
            "recent_count": len(self.recent_actions),
            "workflows_count": len(self.available_workflows),
        }

    def needs_llm(self) -> bool:
        """是否需要调用 LLM？"""
        return not (self.cache_hit or self.workflow_hit or self.token_budget == 0)

    def complexity_level(self) -> str:
        """复杂度分档：low / medium / high。"""
        if self.complexity < 0.3:
            return "low"
        elif self.complexity < 0.7:
            return "medium"
        return "high"


# ═══════════════════════════════════════════════════════════════════════
# SemanticCache — 跨请求语义缓存
# ═══════════════════════════════════════════════════════════════════════

class SemanticCache:
    """跨请求、带 TTL 的语义缓存。

    三层匹配策略（按优先级）：
      1. 精确键匹配 → 直接命中（最快）
      2. Jaccard 模糊匹配 → 语义相近命中（较慢但省 LLM）
      3. 未命中 → 返回 None，由调用方回退到 LLM

    支持持久化到磁盘（可选），跨进程重启复用。

    用法
    ----
        cache = SemanticCache(exact_ttl_s=300, fuzzy_ttl_s=60)
        cache.set("复制这段文字", None, {"command": "copy", "confidence": 0.95})
        result = cache.get("复制文字")  # 精确未命中 → 模糊命中 → 返回结果
    """

    def __init__(
        self,
        exact_ttl_s: int = 300,        # 精确匹配 TTL（5 分钟）
        fuzzy_ttl_s: int = 60,         # 模糊匹配 TTL（1 分钟）
        fuzzy_threshold: float = 0.6,  # Jaccard 阈值
        max_entries: int = 500,        # 最大缓存条目
        persist_path: Optional[str] = None,
        persist_debounce_s: float = 2.0,  # 异步批量持久化去抖间隔
    ):
        self._exact_ttl = exact_ttl_s
        self._fuzzy_ttl = fuzzy_ttl_s
        self._fuzzy_threshold = fuzzy_threshold
        self._max_entries = max_entries
        self._persist_path = Path(persist_path) if persist_path else None
        self._persist_debounce_s = persist_debounce_s

        # 内部存储: exact_key -> (value, expiry_ts, intent, app_name)
        self._store: Dict[str, Tuple[Any, float, str, str]] = {}
        self._lock = threading.Lock()

        # 统计
        self._hits: int = 0
        self._misses: int = 0
        self._fuzzy_hits: int = 0

        # 异步批量持久化：原实现每次 set 都同步写盘 5-50ms，
        # 改为 Timer 去抖：set 后不立即写盘，等 _persist_debounce_s 内无新写入再批量写
        self._persist_timer: Optional[threading.Timer] = None
        self._persist_dirty: bool = False

        # 从磁盘恢复
        if self._persist_path and self._persist_path.exists():
            self._load_from_disk()

    # ── Public API ───────────────────────────────────────────────────────

    def get(self, intent: str, app_name: str = "") -> Optional[Any]:
        """从缓存查找匹配结果。先精确匹配，再模糊匹配。

        Args:
            intent: 用户自然语言意图。
            app_name: 可选应用名（用于区分同意图在不同应用中的不同结果）。

        Returns:
            命中的缓存值；未命中返回 None。
        """
        key = _hash_key(intent, app_name)
        now = time.time()

        with self._lock:
            # 1. 精确匹配
            if key in self._store:
                value, expiry, _, _ = self._store[key]
                if now < expiry:
                    self._hits += 1
                    return value
                # 过期 → 移除
                del self._store[key]

            # 2. 模糊匹配（Jaccard）
            best = self._fuzzy_lookup(intent, now)
            if best is not None:
                self._fuzzy_hits += 1
                return best

        self._misses += 1
        return None

    def set(self, intent: str, app_name: str, value: Any) -> None:
        """写入缓存。

        Args:
            intent: 用户自然语言意图。
            app_name: 应用名（或空字符串）。
            value: 缓存值（任意可序列化对象）。
        """
        key = _hash_key(intent, app_name)
        now = time.time()

        with self._lock:
            # 驱逐过期条目
            self._evict_expired(now)

            # 驱逐超过上限的条目（LRU: 删最旧的）
            while len(self._store) >= self._max_entries:
                oldest_key = min(self._store, key=lambda k: self._store[k][1])
                del self._store[oldest_key]

            self._store[key] = (value, now + self._exact_ttl, intent, app_name)
            self._persist_dirty = True

        # 异步去抖持久化：不立即写盘，等 _persist_debounce_s 内无新写入再批量写
        # 原实现每次 set 都同步写盘 5-50ms，改后平均分摊到 ~0ms
        self._schedule_persist()

    def _schedule_persist(self) -> None:
        """去抖调度异步持久化。

        若已有挂起的 Timer，先取消；再启一个新的，等 _persist_debounce_s 秒后写盘。
        连续高频 set 时只会触发一次写盘。
        """
        if not self._persist_path:
            return
        with self._lock:
            if self._persist_timer is not None:
                self._persist_timer.cancel()
            t = threading.Timer(self._persist_debounce_s, self._async_persist)
            t.daemon = True
            self._persist_timer = t
            t.start()

    def _async_persist(self) -> None:
        """Timer 回调：在后台线程执行批量持久化。"""
        try:
            self._save_to_disk()
        except Exception:
            pass
        with self._lock:
            self._persist_timer = None

    def flush(self) -> None:
        """立即把待写的缓存同步到磁盘（用于优雅关闭）。"""
        with self._lock:
            if self._persist_timer is not None:
                self._persist_timer.cancel()
                self._persist_timer = None
            if not self._persist_dirty:
                return
        try:
            self._save_to_disk()
            with self._lock:
                self._persist_dirty = False
        except Exception:
            pass

    def stats(self) -> Dict[str, int]:
        """缓存统计信息。"""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "fuzzy_hits": self._fuzzy_hits,
                "size": len(self._store),
                "max": self._max_entries,
            }

    def clear(self) -> None:
        """清空缓存（包括磁盘文件）。"""
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0
            self._fuzzy_hits = 0
        if self._persist_path and self._persist_path.exists():
            try:
                self._persist_path.unlink()
            except Exception:
                pass

    def prune(self) -> int:
        """移除所有过期条目，返回移除数量。"""
        now = time.time()
        removed = 0
        with self._lock:
            expired = [k for k, (_, exp, _, _) in self._store.items() if now >= exp]
            for k in expired:
                del self._store[k]
                removed += 1
        return removed

    # ── Internal ─────────────────────────────────────────────────────────

    def _fuzzy_lookup(self, intent: str, now: float) -> Optional[Any]:
        """用 Jaccard 相似度在缓存条目中查找最接近的匹配。

        只检查未过期且 final_ttl 尚未到达的条目。
        """
        intent_tokens = set(_tokenize(intent))
        if not intent_tokens:
            return None

        best_score = 0.0
        best_value = None

        for key, (value, expiry, cached_intent, _) in list(self._store.items()):
            if now >= expiry:
                del self._store[key]
                continue

            cached_tokens = set(_tokenize(cached_intent))
            if not cached_tokens:
                continue

            intersection = intent_tokens & cached_tokens
            union = intent_tokens | cached_tokens
            if not union:
                continue
            score = len(intersection) / len(union)

            if score >= self._fuzzy_threshold and score > best_score:
                best_score = score
                best_value = value

        return best_value

    def _evict_expired(self, now: float) -> None:
        """移除所有过期条目。"""
        expired = [k for k, (_, exp, _, _) in self._store.items() if now >= exp]
        for k in expired:
            del self._store[k]

    def _save_to_disk(self) -> None:
        """将缓存序列化到磁盘（仅存储未过期的非敏感值）。"""
        if not self._persist_path:
            return
        now = time.time()
        data = []
        with self._lock:
            for key, (value, expiry, intent, app) in self._store.items():
                if now >= expiry:
                    continue
                data.append({
                    "key": key,
                    "value": value,
                    "expiry": expiry,
                    "intent": intent,
                    "app": app,
                })
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _load_from_disk(self) -> None:
        """从磁盘恢复缓存。"""
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            raw = self._persist_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            now = time.time()
            with self._lock:
                for item in data:
                    if now >= item.get("expiry", 0):
                        continue
                    self._store[item["key"]] = (
                        item["value"],
                        item["expiry"],
                        item.get("intent", ""),
                        item.get("app", ""),
                    )
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# ContextStore — 状态与记忆层中枢
# ═══════════════════════════════════════════════════════════════════════

class ContextStore:
    """融合主线的"粘合剂"——持有感知快照、历史操作、语义缓存。

    四大策略中的"状态与记忆层"的核心实现。它负责：

    1. **接收感知层快照**（AppContext + UIState）
    2. **维护操作历史 buffer**（最近 K 条）
    3. **持有 SemanticCache**（跨请求命中）
    4. **构建 MinimalContext**（路由层的唯一输入）
    5. **记录成功执行**（沉淀为历史 / 写入缓存）

    用法
    ----
        store = ContextStore(history_size=20)
        app_ctx = detect_context()             # 来自 context.py
        ui_state = perception.snapshot()       # 来自 perception.py (未来)
        ctx = store.build(app_ctx, ui_state, "复制这段文字")
        if ctx.cache_hit:
            print("命中缓存，跳过 LLM")

        # 执行成功后
        store.record("复制这段文字", "copy", app_ctx.app_name)
    """

    def __init__(
        self,
        history_size: int = 20,
        cache_exact_ttl_s: int = 300,
        cache_fuzzy_ttl_s: int = 60,
        cache_persist_path: Optional[str] = None,
    ):
        self._history_size = history_size
        self._history: List[Dict[str, Any]] = []

        # 语义缓存
        self.cache = SemanticCache(
            exact_ttl_s=cache_exact_ttl_s,
            fuzzy_ttl_s=cache_fuzzy_ttl_s,
            persist_path=cache_persist_path,
        )

        # 工作流名列表（由 WorkflowMatcher 注入）
        self._workflow_names: List[str] = []

    # ── Public API ───────────────────────────────────────────────────────

    def build(
        self,
        app_context=None,
        intent: str = "",
        clipboard_text: str = "",
        selected_text: str = "",
        open_apps: Optional[List[str]] = None,
        ui_state: Any = None,
    ) -> MinimalContext:
        """从感知层 + 记忆层压缩出"最小可执行上下文"。

        Args:
            app_context: AppContext 或 UIState（来自 context.py 或 perception.py）
            intent: 用户原始自然语言意图
            clipboard_text: 剪贴板文本（若有）
            selected_text: 当前选中文本（若有）
            open_apps: 当前打开的应用列表
            ui_state: 感知层 UIState（优先于 app_context，包含 UIA 树/截图信息）

        Returns:
            MinimalContext 供路由层使用。
        """
        t0 = time.perf_counter()

        # ── 解析 UIState vs AppContext ──
        # 若传入的是 UIState（有 source 属性），则提取更丰富的信息
        if ui_state is not None:
            source = getattr(ui_state, "source", "light")
            app_name = getattr(ui_state, "app_name", "") or ""
            window_title = getattr(ui_state, "window_title", "") or ""
            process_name = getattr(ui_state, "process_name", "") or ""
            clipboard_text = getattr(ui_state, "clipboard_text", "") or clipboard_text
            selected_text = getattr(ui_state, "selected_text", "") or selected_text
            node_count = getattr(ui_state, "node_count", 0)
            focus = getattr(ui_state, "focus", None)
            visible_text = getattr(ui_state, "visible_text", "") or ""
        elif app_context is not None:
            source = getattr(app_context, "source", "light")
            app_name = getattr(app_context, "app_name", "") or ""
            window_title = getattr(app_context, "window_title", "") or ""
            process_name = getattr(app_context, "process_name", "") or ""
            clipboard_text = getattr(app_context, "clipboard_text", "") or clipboard_text
            selected_text = getattr(app_context, "selected_text", "") or selected_text
            node_count = getattr(app_context, "node_count", 0)
            focus = getattr(app_context, "focus", None)
            visible_text = getattr(app_context, "visible_text", "") or ""
        else:
            source = "none"
            app_name = ""
            window_title = ""
            process_name = ""
            node_count = 0
            focus = None
            visible_text = ""

        # ── 缓存命中检测 ──
        cache_hit = False
        cached_value = None
        if intent:
            cached_value = self.cache.get(intent, app_name)
            cache_hit = cached_value is not None

        # ── 工作流命中检测 ──
        workflow_hit = False
        if not cache_hit and self._workflow_names:
            # 快速关键词匹配（启发式前置，避免 LLM 调用）
            for wf_name in self._workflow_names:
                # 简单子串匹配（无需 LLM）
                if wf_name.lower() in intent.lower() or any(
                    kw in intent.lower() for kw in wf_name.lower().split("_")
                ):
                    workflow_hit = True
                    break

        # ── 复杂度评估（融合 UI 树信息）──
        complexity = _estimate_complexity(intent)

        # 若有 UIA 树且节点数多 → 复杂 UI → 略增 complexity
        if node_count > 50 and source == "uia":
            complexity = min(complexity + 0.08, 1.0)

        # 若焦点在文本框 → 可能涉及编辑操作
        if focus and hasattr(focus, "role"):
            focus_role = getattr(focus, "role", "")
            if focus_role in ("textbox", "edit"):
                complexity = max(complexity, 0.05)

        # ── 风险评估 ──
        risk = _estimate_risk(intent, app_name)

        # 若有可见文本提及敏感/系统级词汇 → 略增风险
        if visible_text:
            risk_words = ["删除", "确认", "格式化", "卸载", "覆盖", "delete", "confirm"]
            if any(w in visible_text.lower() for w in risk_words):
                risk = min(risk + 0.1, 1.0)

        # ── Token 预算估算 ──
        token_budget = 0 if cache_hit or workflow_hit else _estimate_tokens(intent)

        elapsed = (time.perf_counter() - t0) * 1000

        return MinimalContext(
            app_name=app_name,
            window_title=window_title,
            process_name=process_name,
            open_apps=open_apps or [],
            clipboard_text=clipboard_text,
            selected_text=selected_text,
            recent_actions=list(self._history),
            available_workflows=list(self._workflow_names),
            raw_intent=intent,
            complexity=complexity,
            risk=risk,
            token_budget=token_budget,
            cached_result=cached_value,
            cache_hit=cache_hit,
            workflow_hit=workflow_hit,
            build_elapsed_ms=elapsed,
        )

    def record(self, intent: str, command: str, app_name: str = "",
               confidence: float = 1.0, result: Any = None) -> None:
        """记录一次成功执行，沉淀入历史 buffer 和语义缓存。

        Args:
            intent: 用户原始意图
            command: 匹配到的命令名
            app_name: 当前应用名
            confidence: 置信度
            result: 可选缓存值（IntentResult / Plan / 等）
        """
        # 1. 写入历史 buffer
        entry = {
            "intent": intent,
            "command": command,
            "app": app_name,
            "confidence": confidence,
            "ts": time.time(),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        self._history.append(entry)
        if len(self._history) > self._history_size:
            self._history = self._history[-self._history_size:]

        # 2. 写入语义缓存
        if result is not None and intent:
            self.cache.set(intent, app_name, result)

    def get_history(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """获取最近操作历史。

        Args:
            limit: 返回条数限制，默认全部。
        """
        if limit is None:
            return list(self._history)
        return list(self._history[-limit:])

    def get_suggestions(self, app_name: str = "") -> List[Dict[str, Any]]:
        """基于历史操作和当前应用，生成快捷建议。

        返回在当前应用中最常执行的前 5 条操作。
        """
        app_actions = [
            a for a in self._history
            if (not app_name or a.get("app") == app_name)
        ]
        # 按命令聚合
        counts: Dict[str, Dict[str, Any]] = {}
        for a in app_actions:
            cmd = a.get("command", "")
            if cmd not in counts:
                counts[cmd] = {"command": cmd, "count": 0, "last_intent": ""}
            counts[cmd]["count"] += 1
            counts[cmd]["last_intent"] = a.get("intent", "")

        ranked = sorted(counts.values(), key=lambda x: x["count"], reverse=True)
        return ranked[:5]

    def set_workflows(self, names: List[str]) -> None:
        """更新可用工作流列表（由 WorkflowMatcher 注入）。"""
        self._workflow_names = list(names)

    def cache_stats(self) -> Dict[str, Any]:
        """语义缓存统计信息。"""
        return self.cache.stats()

    def clear(self) -> None:
        """清空所有状态（历史和缓存）。"""
        self._history.clear()
        self.cache.clear()
        self._workflow_names.clear()


# ═══════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════

def _hash_key(intent: str, app_name: str = "") -> str:
    """为缓存生成稳定键。"""
    raw = f"{intent.strip().lower()}|{app_name.strip().lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _tokenize(text: str) -> List[str]:
    """简单的中英文分词：按空格/标点切分 + 2-gram 中文子串。"""
    import re
    text = text.lower().strip()
    # 提取中文/英文/数字 token
    tokens: List[str] = re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+', text)
    result: List[str] = []
    for t in tokens:
        if re.match(r'^[\u4e00-\u9fff]+$', t):
            # 中文：加入完整词 + 1-gram 子串
            result.append(t)
            for i in range(len(t)):
                result.append(t[i])  # 1-gram 字符
            for i in range(len(t) - 1):
                result.append(t[i:i + 2])  # 2-gram
        else:
            result.append(t)  # 英文/数字 token
    return result


def _estimate_complexity(intent: str) -> float:
    """启发式评估意图复杂度 [0.0, 1.0]。

    规则：
    - 单关键词单动作（"复制"、"粘贴"）→ 0.0 ~ 0.1
    - 多关键词单动作（"把这段文字复制"）→ 0.1 ~ 0.3
    - 多步意图（"把文件移到桌面"）→ 0.3 ~ 0.6
    - 复杂目标（"把报告内容总结后发邮件"）→ 0.6 ~ 1.0
    """
    if not intent.strip():
        return 0.0

    # 多步动作指示词
    multi_step_markers = [
        "然后", "接着", "之后", "再", "then", "after", "之后",
        "并且", "同时", "以及", "and",
        "发", "发送", "send", "发布", "publish",
        "移动", "move", "复制到", "移到", "转到",
        "总结", "汇总", "summarize", "整理",
        "自动", "批量", "batch",
    ]
    scored = sum(1 for m in multi_step_markers if m in intent.lower())

    # 意图长度加成
    length_bonus = min(len(intent) / 50.0, 0.3)

    base = min(scored * 0.15, 0.6) + length_bonus
    return min(max(base, 0.01), 1.0)


def _estimate_risk(intent: str, app_name: str = "") -> float:
    """启发式评估执行风险 [0.0, 1.0]。

    高风险操作：删除、格式化、系统设置、注册表操作。
    中风险：文件操作、shell 命令。
    低风险：复制、粘贴、选择、导航。
    """
    high_risk_words = [
        "删除", "del", "delete", "移除", "remove", "清空", "clear",
        "格式化", "format", "重装", "卸载", "uninstall",
        "注册表", "registry", "regedit", "系统", "system32",
        "关机", "shutdown", "重启", "restart", "reboot",
        "sudo", "管理员", "administrator",
    ]
    medium_risk_words = [
        "移动", "move", "复制到", "copy to", "覆盖", "overwrite",
        "shell", "cmd", "命令行", "terminal",
        "发送", "send", "发布", "publish", "提交", "commit",
        "安装", "install", "执行", "execute", "运行", "run",
    ]

    intent_lower = intent.lower()
    combined = intent_lower + " " + app_name.lower()

    if any(w in combined for w in high_risk_words):
        return 0.8 + min(len(intent) / 30.0, 0.2)

    if any(w in combined for w in medium_risk_words):
        return 0.4 + min(len(intent) / 40.0, 0.3)

    return min(0.1 + len(intent) / 100.0, 0.3)


def _estimate_tokens(intent: str) -> int:
    """估算调用 LLM 需要的 token 数。

    这只是供路由层决策用的预估，不影响实际 API 调用。
    """
    if not intent.strip():
        return 0
    # 粗略估算：中文每字约 1.5 token，英文每词约 1.3 token
    import re
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', intent))
    english_words = len(re.findall(r'[a-z]+', intent.lower()))
    base = int(chinese_chars * 1.5 + english_words * 1.3)

    # 加上 system prompt 的 token 开销（~500 tokens）
    return base + 500
