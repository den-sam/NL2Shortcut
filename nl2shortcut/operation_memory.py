"""操作记忆与模式学习 — Operation Memory & Pattern Learning

记录用户操作序列，自动聚类生成可复用指令包。
NL2Shortcut 执行操作时自动记录（通过 agent.py hook）；
用户也可以主动说「记住我这个操作，叫它 XX」。
"""

from __future__ import annotations

import json
import sys as _sys

# Ensure nl2shortcut package is on the path for standalone runs
# (when executed directly as: python operation_memory.py)
if __name__ != "nl2shortcut.operation_memory":
    import pathlib as _pl
    _me = __file__
    _parent_pkg = str(_pl.Path(_me).parent)
    _grandparent = str(_pl.Path(_me).parent.parent)
    if _parent_pkg not in _sys.path:
        _sys.path.insert(0, _parent_pkg)
    if _grandparent not in _sys.path:
        _sys.path.insert(0, _grandparent)

import sqlite3
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from .models import ExecutionResult
except ImportError:
    # Standalone run (e.g. python operation_memory.py)
    from models import ExecutionResult  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Data Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OpRecord:
    """单次操作记录"""
    id: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    app: str = ""                          # 当前应用：notepad / outlook / chrome 等
    action_type: str = ""                   # "shortcut" | "composite" | "shell" | "primitive"
    action_detail: str = ""                 # "Ctrl+C" / "copy_file_to_folder" 等
    context: str = ""                       # 操作前后剪贴板 / 焦点状态
    duration_ms: int = 0
    user_goal: str = ""                    # 用户记录的目标（可选）
    sequence_id: str = ""                  # 所属操作序列标识（同一次连续操作的 UUID）


@dataclass
class OpPattern:
    """从 OpRecord 聚类生成的指令包"""
    id: int = 0
    name: str = ""                          # "send_email_report" / "format_and_save"
    description: str = ""
    app: str = ""
    steps: list[dict] = field(default_factory=list)
    #   每步: {"type": "shortcut", "key": "Ctrl+A", "action": "select_all"}
    #        {"type": "shell",    "cmd": "outlook", ...}
    #        {"type": "composite","name": "open_outlook_and_paste"}
    frequency: int = 1                      # 被使用次数
    avg_duration_ms: int = 0
    last_used: datetime = field(default_factory=datetime.now)
    confidence: float = 0.5               # 置信度（样本量决定）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "app": self.app,
            "steps": self.steps,
            "frequency": self.frequency,
            "avg_duration_ms": self.avg_duration_ms,
            "last_used": self.last_used.isoformat(),
            "confidence": self.confidence,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "OpPattern":
        steps = json.loads(row["steps"]) if row["steps"] else []
        last_used = datetime.fromisoformat(row["last_used"]) if row["last_used"] else datetime.now()
        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            app=row["app"] or "",
            steps=steps,
            frequency=row["frequency"] or 1,
            avg_duration_ms=row["avg_duration_ms"] or 0,
            last_used=last_used,
            confidence=row["confidence"] or 0.5,
        )


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionResult re-export for pattern execution (convenience)
# ─────────────────────────────────────────────────────────────────────────────

class _DummyAdapter:
    """占位 adapter，execute_pattern 使用 agent 的真实 adapter 执行。"""
    def __getattr__(self, _):
        raise NotImplementedError("Dummy adapter — use ShortcutAgent.execute_pattern()")


# ─────────────────────────────────────────────────────────────────────────────
# 按键规范化（让"写法不同但功能相同"的操作自动合并成一个 pattern）
# ─────────────────────────────────────────────────────────────────────────────
#
# 设计约束（不改变任何按键的执行语义，不影响既有工作流效果）：
#   * 仅做字符串归一 + 公认无歧义的语义等价合并（如 Windows 下 Ctrl+Insert == Ctrl+C）。
#   * 幂等：对已经是规范写法的键（如 "Ctrl+C"）原样返回，绝不改变其功能。
#   * 绝不把功能不同的操作误并（Ctrl+S 与 Ctrl+A 规范后依旧不同）。
#   * 纯函数，不依赖 database.py，避免循环 import。

# 修饰键固定顺序，消除 "Ctrl+Shift+S" / "Shift+Ctrl+S" 的差异
_MODIFIER_ORDER = {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}

# 写法别名 -> 规范写法（纯语法，不改变功能）
_KEY_ALIASES = {
    "control": "Ctrl", "ctl": "Ctrl",
    "altgr": "AltGr", "option": "Alt", "opt": "Alt",
    "windows": "Win", "super": "Win", "meta": "Win",
    "return": "Enter", "esc": "Esc", "escape": "Esc",
    "space": "Space", "pageup": "PageUp", "pagedown": "PageDown",
    "del": "Delete", "ins": "Insert",
}

# 语义等价的按键组：同一组内不同写法合并为第一个（规范）写法。
# 仅含公认无歧义的 Windows 等价关系，避免误并。
_SEMANTIC_GROUPS = (
    ("Ctrl+C", "Ctrl+Insert"),       # 复制
    ("Ctrl+V", "Shift+Insert"),      # 粘贴
    ("Ctrl+X", "Shift+Delete"),      # 剪切
    ("Ctrl+Z", "Alt+Backspace"),     # 撤销
)


def _normalize_key_syntax(detail: str) -> str:
    """纯语法归一：大小写 / 别名 / 修饰键顺序 / 多余空格。幂等。"""
    s = (detail or "").strip()
    if not s:
        return s
    toks = [t.strip() for t in s.split("+")]
    norm: list[str] = []
    for t in toks:
        tl = t.lower()
        if tl in _KEY_ALIASES:
            norm.append(_KEY_ALIASES[tl])
        elif len(t) == 1:
            norm.append(t.upper())
        else:
            norm.append(t[:1].upper() + t[1:].lower())
    mods = sorted(
        (k for k in norm if k.lower() in _MODIFIER_ORDER),
        key=lambda k: _MODIFIER_ORDER[k.lower()],
    )
    rest = [k for k in norm if k.lower() not in _MODIFIER_ORDER]
    return "+".join(mods + rest)


# 语义等价表：把组内其它写法映射到规范写法
_SEMANTIC_MAP: dict[str, str] = {}
for _grp in _SEMANTIC_GROUPS:
    _canon = _normalize_key_syntax(_grp[0])
    for _k in _grp:
        _SEMANTIC_MAP[_normalize_key_syntax(_k)] = _canon


def canonical_key(detail: str) -> str:
    """把按键字符串规范化为唯一形式：语法归一 + 语义等价合并。

    幂等、纯函数；只改变"相同功能的不同写法"的分组键，
    不改变任何按键的按下语义，因此不影响既有工作流的执行效果。
    """
    if not detail:
        return detail
    normalized = _normalize_key_syntax(detail)
    return _SEMANTIC_MAP.get(normalized, normalized)


def _steps_signature(steps: list[dict], app: str = "") -> str:
    """为一组 pattern steps 生成规范签名（用于跨次 / 跨历史合并去重）。"""
    parts = []
    for s in steps:
        key = (
            s.get("key") or s.get("cmd")
            or s.get("name") or s.get("detail") or ""
        )
        if (s.get("type") or "") == "shortcut":
            key = canonical_key(key)
        parts.append(f"{s.get('type')}:{key}")
    return f"{app}|" + "|".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# OperationMemory
# ─────────────────────────────────────────────────────────────────────────────

class OperationMemory:
    """SQLite 持久化的操作记忆库

    记录用户每次操作，自动聚类生成可复用指令包，
    并在 Planner 执行前提供 pattern 建议。

    示例（agent hook）：
        agent = ShortcutAgent()
        agent.operation_memory.record(
            app="outlook",
            action_type="shortcut",
            action_detail="Ctrl+A",
            duration_ms=80,
            user_goal="全选邮件正文"
        )
        # 检测到重复 → 自动生成 pattern
        patterns = agent.operation_memory.learn_patterns()
    """

    # 时间序列聚类参数
    _SEQUENCE_GAP_SECONDS: int = 30    # 间隔 < 30s 视为同一序列
    _MIN_SEQUENCE_LENGTH: int = 1      # 单步即可参与聚类（执行一次就能加入工作流）
    _DEFAULT_DB: str = ".nl2shortcut/operation_memory.db"

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            config_dir = Path.home() / ".nl2shortcut"
            config_dir.mkdir(parents=True, exist_ok=True)
            db_path = str(config_dir / "operation_memory.db")
        self.db_path = Path(db_path)
        self._records_since_learn: int = 0
        # 单例连接 + 锁：避免每次方法调用都新建 SQLite 连接（原实现每次 1-5ms 开销）
        self._conn_lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """返回复用的单例 SQLite 连接（线程安全）。

        原实现每次方法调用都新建连接 + PRAGMA，开销 1-5ms。
        改为模块实例级单例 + RLock 保护，所有方法共用一个连接。
        WAL 模式支持并发读 + 单写，多线程环境下安全。
        """
        with self._conn_lock:
            if self._conn is None:
                conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                self._conn = conn
            return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        with self._conn_lock:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS op_records (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp    TEXT    NOT NULL,
                    app          TEXT    NOT NULL DEFAULT '',
                    action_type  TEXT    NOT NULL DEFAULT '',
                    action_detail TEXT   NOT NULL DEFAULT '',
                    context      TEXT    NOT NULL DEFAULT '',
                    duration_ms  INTEGER NOT NULL DEFAULT 0,
                    user_goal    TEXT    NOT NULL DEFAULT '',
                    sequence_id  TEXT    NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_records_timestamp ON op_records(timestamp);
                CREATE INDEX IF NOT EXISTS idx_records_app       ON op_records(app);
                CREATE INDEX IF NOT EXISTS idx_records_sequence  ON op_records(sequence_id);

                CREATE TABLE IF NOT EXISTS op_patterns (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    name            TEXT    UNIQUE NOT NULL,
                    description     TEXT    NOT NULL DEFAULT '',
                    app             TEXT    NOT NULL DEFAULT '',
                    steps           TEXT    NOT NULL DEFAULT '[]',
                    frequency       INTEGER NOT NULL DEFAULT 1,
                    avg_duration_ms INTEGER NOT NULL DEFAULT 0,
                    last_used       TEXT    NOT NULL,
                    confidence      REAL    NOT NULL DEFAULT 0.5,
                    created_at      TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_patterns_app  ON op_patterns(app);
                CREATE INDEX IF NOT EXISTS idx_patterns_name ON op_patterns(name);
            """)
            conn.commit()

    def _now_iso(self) -> str:
        return datetime.now().isoformat()

    def _parse_iso(self, s: str) -> datetime:
        return datetime.fromisoformat(s)

    def _record_to_row(self, rec: OpRecord) -> dict:
        return {
            "timestamp": rec.timestamp.isoformat(),
            "app": rec.app,
            "action_type": rec.action_type,
            "action_detail": rec.action_detail,
            "context": rec.context,
            "duration_ms": rec.duration_ms,
            "user_goal": rec.user_goal,
            "sequence_id": rec.sequence_id,
        }

    def _row_to_record(self, row: sqlite3.Row) -> OpRecord:
        return OpRecord(
            id=row["id"],
            timestamp=self._parse_iso(row["timestamp"]),
            app=row["app"] or "",
            action_type=row["action_type"] or "",
            action_detail=row["action_detail"] or "",
            context=row["context"] or "",
            duration_ms=row["duration_ms"] or 0,
            user_goal=row["user_goal"] or "",
            sequence_id=row["sequence_id"] or "",
        )

    def _normalize_step(self, action_type: str, action_detail: str) -> dict:
        """将 action 转换为标准化的 pattern step dict。"""
        if action_type == "shortcut":
            return {"type": "shortcut", "key": action_detail}
        elif action_type == "shell":
            return {"type": "shell", "cmd": action_detail}
        elif action_type == "composite":
            return {"type": "composite", "name": action_detail}
        else:
            return {"type": action_type, "detail": action_detail}

    # ── 记录 ─────────────────────────────────────────────────────────────────

    def record(
        self,
        app: str,
        action_type: str,
        action_detail: str,
        context: str = "",
        duration_ms: int = 0,
        user_goal: str = "",
        sequence_id: str = "",
    ) -> int:
        """记录一次操作（含时间戳）。

        Returns:
            新记录的 id。

        Example:
            memory.record(
                app="outlook",
                action_type="shortcut",
                action_detail="Ctrl+A",
                duration_ms=80,
                user_goal="全选邮件正文"
            )
        """
        with self._conn_lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO op_records
                    (timestamp, app, action_type, action_detail,
                     context, duration_ms, user_goal, sequence_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                self._now_iso(),
                app,
                action_type,
                action_detail,
                context,
                duration_ms,
                user_goal,
                sequence_id,
            ))
            conn.commit()
            record_id = cursor.lastrowid

            # Auto-trigger pattern learning after accumulating enough records
            self._records_since_learn += 1
            if self._records_since_learn >= 10:
                try:
                    self.learn_patterns(min_frequency=1)
                except Exception:
                    pass
                self._records_since_learn = 0

            return record_id

    def record_sequence(self, records: list[OpRecord]) -> list[int]:
        """记录一段连续操作序列。
        如果 records 中没有 sequence_id，自动分配一个 UUID。

        Returns:
            各记录的 id 列表。
        """
        if not records:
            return []
        import uuid
        seq_id = records[0].sequence_id or str(uuid.uuid4())
        with self._conn_lock:
            conn = self._get_conn()
            ids = []
            for rec in records:
                rec.sequence_id = seq_id
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO op_records
                        (timestamp, app, action_type, action_detail,
                         context, duration_ms, user_goal, sequence_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rec.timestamp.isoformat(),
                    rec.app,
                    rec.action_type,
                    rec.action_detail,
                    rec.context,
                    rec.duration_ms,
                    rec.user_goal,
                    seq_id,
                ))
                ids.append(cursor.lastrowid)
            conn.commit()
            return ids

    # ── 模式学习 ─────────────────────────────────────────────────────────────

    def learn_patterns(self, min_frequency: int = 3) -> list[OpPattern]:
        """扫描历史记录，聚类出重复模式（frequency ≥ min_frequency），
        生成 OpPattern 指令包。

        聚类逻辑（简化版）：
        1. 按 sequence_id 分组（sequence_id 由 record_sequence 自动分配）
        2. 相同 app + 相邻时间（< 30s 间隔）→ 同一条 pattern
        3. 相同 action_sequence（标准化 key 顺序）→ 归为同类
        4. frequency ≥ min_frequency → 提升为 pattern
        """
        with self._conn_lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM op_records ORDER BY timestamp ASC"
            ).fetchall()

        if not rows:
            return []

        records = [self._row_to_record(r) for r in rows]

        # ── 步骤1：构建时间序列段 ──────────────────────────────────────────
        #   按 app 分组，时间间隔 < 30s 的连续记录归为一段 sequence
        segments: list[list[OpRecord]] = []
        current_seg: list[OpRecord] = []
        current_app = ""
        prev_ts: Optional[datetime] = None

        for rec in records:
            gap = 0
            if prev_ts is not None:
                gap = (rec.timestamp - prev_ts).total_seconds()

            if rec.app != current_app or gap > self._SEQUENCE_GAP_SECONDS:
                if len(current_seg) >= self._MIN_SEQUENCE_LENGTH:
                    segments.append(current_seg)
                current_seg = [rec]
                current_app = rec.app
            else:
                current_seg.append(rec)
            prev_ts = rec.timestamp

        if len(current_seg) >= self._MIN_SEQUENCE_LENGTH:
            segments.append(current_seg)

        if not segments:
            return []

        # ── 步骤2：将序列标准化为 "action_signature" ───────────────────────
        #   例如: "notepad|shortcut:select_all→shortcut:copy→..."
        def make_signature(seg: list[OpRecord]) -> str:
            parts = [f"{seg[0].app}|"]
            for r in seg:
                detail = (canonical_key(r.action_detail)
                          if r.action_type == "shortcut" else r.action_detail)
                parts.append(f"{r.action_type}:{detail}")
                parts.append("→")
            return "".join(parts)

        sig_map: dict[str, list[list[OpRecord]]] = {}
        for seg in segments:
            sig = make_signature(seg)
            sig_map.setdefault(sig, []).append(seg)

        # ── 步骤3：frequency ≥ min_frequency → 生成 pattern ────────────────
        patterns: list[OpPattern] = []
        for sig, segs in sig_map.items():
            freq = len(segs)
            if freq < min_frequency:
                continue

            # 取第一条序列作为 steps 模板
            template = segs[0]
            app = template[0].app
            steps = [
                self._normalize_step(
                    r.action_type,
                    canonical_key(r.action_detail)
                    if r.action_type == "shortcut" else r.action_detail,
                )
                for r in template
            ]
            total_dur = sum(r.duration_ms for r in template)
            avg_dur = total_dur // len(template)

            # 置信度 = min(1.0, 样本量 / 10)
            confidence = min(1.0, freq / 10.0)

            # Use a stable hash of the signature for the pattern name
            import hashlib as _hm
            _sig_hash = _hm.md5(sig.encode()).hexdigest()[:8]
            pattern = OpPattern(
                name=f"auto_{app}_{_sig_hash}",
                description=f"自动学习：从 {freq} 次重复操作中提取（app={app}）",
                app=app,
                steps=steps,
                frequency=freq,
                avg_duration_ms=avg_dur,
                last_used=template[-1].timestamp,
                confidence=round(confidence, 2),
            )
            patterns.append(pattern)

        # ── 步骤4：与既有 pattern 归并（自愈历史脏数据）──────────────────────
        #   相同规范步骤签名（含 app）-> 累加频率、保留既有 name/id，避免重复 pattern。
        #   这样早期以非规范写法（如 "Ctrl+c"）学到的 pattern 会与新学的规范 pattern 自动合并。
        with self._conn_lock:
            conn2 = self._get_conn()
            existing_rows = conn2.execute("SELECT * FROM op_patterns").fetchall()

        existing_by_sig: dict[str, OpPattern] = {}
        for row in existing_rows:
            ep = OpPattern.from_row(row)
            existing_by_sig.setdefault(_steps_signature(ep.steps, ep.app), ep)

        final_patterns: list[OpPattern] = []
        seen_sigs: set[str] = set()
        for p in patterns:
            sig = _steps_signature(p.steps, p.app)
            if sig in seen_sigs:
                continue  # 本次学习内已合并
            seen_sigs.add(sig)
            if sig in existing_by_sig:
                old = existing_by_sig[sig]
                p.name = old.name          # 保留既有 name，save_pattern 会覆盖更新
                p.id = old.id
                p.frequency += old.frequency
                p.confidence = min(1.0, max(p.confidence, old.confidence))
                if old.last_used > p.last_used:
                    p.last_used = old.last_used
            final_patterns.append(p)
        patterns = final_patterns

        # 保存到数据库
        for p in patterns:
            self.save_pattern(p)

        # 自动导出高置信度 pattern → YAML workflow
        for p in patterns:
            if p.confidence >= self._AUTO_EXPORT_CONFIDENCE_THRESHOLD:
                try:
                    self.export_pattern_to_workflow(p)
                except Exception:
                    pass

        return patterns

    def suggest_next(
        self,
        current_app: str,
        recent_actions: list[str],
    ) -> Optional[OpPattern]:
        """基于当前上下文预测用户下一步操作（给 Planner 建议）。

        Args:
            current_app:   当前应用名，如 "outlook"
            recent_actions: 最近执行的 action_detail 列表（按顺序）

        Returns:
            匹配度最高的 OpPattern，或 None。
        """
        with self._conn_lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM op_patterns WHERE app=? ORDER BY confidence DESC, frequency DESC",
                (current_app,),
            ).fetchall()

        best: Optional[OpPattern] = None
        best_score = 0.0

        for row in rows:
            pattern = OpPattern.from_row(row)
            steps = pattern.steps

            # 前缀匹配：recent_actions 与 pattern 的前 N 步匹配
            n = len(recent_actions)
            if n >= len(steps):
                continue

            match = True
            for i, ra in enumerate(recent_actions):
                step = steps[i]
                step_key = step.get("key") or step.get("cmd") or step.get("name") or ""
                if canonical_key(ra) != canonical_key(step_key):
                    match = False
                    break

            if match:
                score = pattern.confidence * pattern.frequency
                if score > best_score:
                    best_score = score
                    best = pattern

        return best

    # ── 指令包管理 ───────────────────────────────────────────────────────────

    def save_pattern(self, pattern: OpPattern) -> int:
        """手动或自动保存一个指令包。

        Returns:
            pattern id（新建时为新增 id，覆盖时为原 id）。
        """
        with self._conn_lock:
            conn = self._get_conn()
            # 尝试覆盖已有（按 name）
            cursor = conn.execute(
                "SELECT id FROM op_patterns WHERE name=?",
                (pattern.name,),
            ).fetchone()

            steps_json = json.dumps(pattern.steps, ensure_ascii=False)

            if cursor:
                pid = cursor["id"]
                conn.execute("""
                    UPDATE op_patterns SET
                        description=?, app=?, steps=?, frequency=?,
                        avg_duration_ms=?, last_used=?, confidence=?
                    WHERE id=?
                """, (
                    pattern.description,
                    pattern.app,
                    steps_json,
                    pattern.frequency,
                    pattern.avg_duration_ms,
                    pattern.last_used.isoformat(),
                    pattern.confidence,
                    pid,
                ))
            else:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO op_patterns
                        (name, description, app, steps, frequency,
                         avg_duration_ms, last_used, confidence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    pattern.name,
                    pattern.description,
                    pattern.app,
                    steps_json,
                    pattern.frequency,
                    pattern.avg_duration_ms,
                    pattern.last_used.isoformat(),
                    pattern.confidence,
                    self._now_iso(),
                ))
                pid = cursor.lastrowid
            conn.commit()
            return pid  # type: ignore

    # ── Pattern → Workflow YAML export ────────────────────────────────────

    # Map OpPattern step types to workflow action types
    _STEP_TYPE_TO_ACTION = {
        "shortcut":  "shortcut",
        "shell":     "shell",
        "type":      "type",
        "click":     "click",
        "scroll":    "scroll",
        "wait":      "wait",
        "composite": "shortcut",  # composite → shortcut placeholder
    }

    _AUTO_EXPORT_CONFIDENCE_THRESHOLD = 0.0  # 执行一次就自动加入工作流

    def export_pattern_to_workflow(self, pattern: OpPattern,
                                   overwrite: bool = False) -> Optional[str]:
        """将一个已学习的 OpPattern 导出为可复用的 YAML 工作流文件。

        写入位置：``~/.nl2shortcut/workflows/{pattern.name}.yaml``。
        若文件已存在则跳过（除非 overwrite=True）。

        成功时返回文件路径，跳过或失败时返回 None。
        """
        import yaml

        workflows_dir = Path.home() / ".nl2shortcut" / "workflows"
        workflows_dir.mkdir(parents=True, exist_ok=True)

        filepath = workflows_dir / f"{pattern.name}.yaml"
        if filepath.exists() and not overwrite:
            return None  # respect user-edited workflows

        # Convert OpPattern steps to workflow step dicts
        wf_steps = []
        for i, step in enumerate(pattern.steps):
            step_type = (step.get("type") or "shortcut").lower()
            action = self._STEP_TYPE_TO_ACTION.get(step_type, "shortcut")

            if step_type == "shortcut":
                command = step.get("key", "")
            elif step_type == "shell":
                command = step.get("cmd", "")
            elif step_type == "type":
                command = step.get("text", "")
            elif step_type == "wait":
                ms = step.get("ms", 0)
                command = str(ms / 1000.0) if ms else "1"
            elif step_type == "click":
                command = step.get("button", "left")
            elif step_type == "scroll":
                command = str(step.get("amount", 1))
            elif step_type == "composite":
                command = f"[composite] {step.get('name', '')}"
            else:
                command = step.get("detail", step.get("key", step.get("cmd", "")))

            wf_steps.append({
                "name": step.get("description", f"Step {i+1}"),
                "action": action,
                "command": command,
            })

        # Build workflow YAML document
        doc = {
            "name": pattern.name,
            "description": pattern.description or f"Auto-generated from {pattern.frequency} repetitions in {pattern.app}",
            "version": "1.0",
            "variables": {},
            "steps": wf_steps,
        }

        try:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"# Auto-generated from learned pattern '{pattern.name}'\n")
                f.write(f"# App: {pattern.app}  |  Frequency: {pattern.frequency}  |  Confidence: {pattern.confidence:.0%}\n")
                yaml.dump(doc, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            return str(filepath)
        except Exception:
            return None

    def export_high_confidence_patterns(self) -> list[str]:
        """将所有置信度 ≥ 阈值的模式导出为 YAML 工作流。

        返回已创建的文件路径列表。
        """
        with self._conn_lock:
            conn = self._get_conn()
            threshold = int(self._AUTO_EXPORT_CONFIDENCE_THRESHOLD * 100)
            rows = conn.execute(
                "SELECT * FROM op_patterns WHERE CAST(confidence * 100 AS INTEGER) >= ?",
                (threshold,),
            ).fetchall()
            patterns = [OpPattern.from_row(r) for r in rows]

        exported = []
        for p in patterns:
            path = self.export_pattern_to_workflow(p)
            if path:
                exported.append(path)
        return exported

    # ── Pattern query ─────────────────────────────────────────────────────

    def get_pattern(self, name: str) -> Optional[OpPattern]:
        """按名称获取指令包。"""
        with self._conn_lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM op_patterns WHERE name=?",
                (name,),
            ).fetchone()
            return OpPattern.from_row(row) if row else None

    def list_patterns(self, app: str = None) -> list[OpPattern]:
        """列出所有或指定应用的指令包。"""
        with self._conn_lock:
            conn = self._get_conn()
            if app:
                rows = conn.execute(
                    "SELECT * FROM op_patterns WHERE app=? ORDER BY frequency DESC",
                    (app,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM op_patterns ORDER BY frequency DESC, confidence DESC"
                ).fetchall()
            return [OpPattern.from_row(r) for r in rows]

    def delete_pattern(self, name: str) -> bool:
        """删除指定名称的指令包。"""
        with self._conn_lock:
            conn = self._get_conn()
            cursor = conn.execute(
                "DELETE FROM op_patterns WHERE name=?",
                (name,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def execute_pattern(
        self,
        pattern: OpPattern,
        adapter: object = None,
    ) -> list[ExecutionResult]:
        """顺序执行一个指令包的每个 step。

        Args:
            pattern: 待执行的指令包。
            adapter: KeyboardAdapter 实例。如果为 None，尝试从 ShortcutAgent 传入。

        Returns:
            每步的 ExecutionResult 列表。
        """
        results: list[ExecutionResult] = []
        for step in pattern.steps:
            step_type = step.get("type", "")
            try:
                if step_type == "shortcut":
                    key = step.get("key", "")
                    if adapter and hasattr(adapter, "press"):
                        ok = adapter.press(key)
                    else:
                        ok = False
                    results.append(ExecutionResult(
                        success=ok,
                        command=key,
                        key_combination=key,
                        mode="pattern",
                        confidence=1.0,
                    ))

                elif step_type == "shell":
                    cmd = step.get("cmd", "")
                    import subprocess
                    proc = subprocess.run(
                        cmd, shell=True,
                        capture_output=True, timeout=30,
                    )
                    results.append(ExecutionResult(
                        success=proc.returncode == 0,
                        command=cmd,
                        mode="pattern",
                        confidence=1.0,
                    ))

                elif step_type == "composite":
                    name = step.get("name", "")
                    results.append(ExecutionResult(
                        success=False,
                        command=name,
                        mode="pattern",
                        error=f"composite step '{name}' 需要由 Planner 解析",
                    ))

                else:
                    results.append(ExecutionResult(
                        success=False,
                        command=str(step),
                        mode="pattern",
                        error=f"unknown step type: {step_type}",
                    ))

            except Exception as exc:
                results.append(ExecutionResult(
                    success=False,
                    command=str(step),
                    mode="pattern",
                    error=str(exc),
                ))

        # 更新 pattern 使用统计
        if results:
            with self._conn_lock:
                conn = self._get_conn()
                conn.execute("""
                    UPDATE op_patterns
                    SET frequency = frequency + 1,
                        last_used  = ?
                    WHERE name=?
                """, (self._now_iso(), pattern.name))
                conn.commit()

        return results

    # ── 建议引擎 ─────────────────────────────────────────────────────────────

    # 低效操作规则库（可扩展）
    INEFFICIENT_MAP: dict[str, list[tuple[str, str]]] = {
        # app → [(检测到的低效操作, 建议快捷键)]
        "notepad": [
            ("全选后复制", "Ctrl+A → Ctrl+C（可简化为 Ctrl+C）"),
            ("手动删除整行", "Ctrl+Shift+Home → Delete"),
        ],
        "outlook": [
            ("新建邮件", "Ctrl+N"),
            ("附件", "Ctrl+Shift+A"),
            ("发送", "Ctrl+Enter"),
        ],
        "vscode": [
            ("鼠标格式化", "Shift+Alt+F"),
            ("手动保存", "Ctrl+S"),
        ],
        "chrome": [
            ("鼠标刷新", "Ctrl+R"),
            ("手动复制标签页", "Ctrl+Shift+T"),
        ],
    }

    def get_suggestion(self, goal: str, app: str) -> str:
        """NL2Shortcut 主动建议。

        主路径：基于历史学习模式的 suggest_next()（SQLite 模式匹配）
        回退路径：硬编码 INEFFICIENT_MAP 关键词匹配
        """
        # PRIMARY PATH: ML-based suggestion from learned patterns
        if app and goal:
            try:
                recent: list[str] = []
                goal_lower = goal.lower()
                for keyword, _ in self.INEFFICIENT_MAP.get(app, []):
                    if keyword in goal_lower:
                        recent.append(keyword)
                        break
                if recent:
                    pattern = self.suggest_next(app, recent)
                    if pattern and pattern.steps:
                        next_step = pattern.steps[0]
                        step_key = next_step.get("key") or next_step.get("cmd") or ""
                        if step_key:
                            return (
                                f"[💡] 检测到您正在 {app}。"
                                f"基于历史学习（频率={pattern.frequency}，"
                                f"置信度={pattern.confidence:.0%}）：\n"
                                f"  • 建议：{step_key}（{pattern.description}）"
                            )
            except Exception:
                pass

        # FALLBACK: hardcoded keyword match
        suggestions: list[str] = []
        inefficient = self.INEFFICIENT_MAP.get(app, [])

        goal_lower = goal.lower()
        for (keyword, advice) in inefficient:
            if keyword in goal_lower:
                suggestions.append(f"  • 建议改用键盘快捷键：{advice}")

        if suggestions:
            header = f"[💡] 检测到您正在 {app}，优化建议："
            return "\n".join([header] + suggestions)

        return ""

    # ── 调试 / 工具 ─────────────────────────────────────────────────────────

    def get_records(
        self,
        app: str = None,
        limit: int = 100,
    ) -> list[OpRecord]:
        """获取最近记录（调试用）。"""
        with self._conn_lock:
            conn = self._get_conn()
            if app:
                rows = conn.execute(
                    "SELECT * FROM op_records WHERE app=? ORDER BY timestamp DESC LIMIT ?",
                    (app, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM op_records ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def clear_all_records(self) -> None:
        """清除所有操作记录（谨慎使用）。"""
        with self._conn_lock:
            conn = self._get_conn()
            conn.execute("DELETE FROM op_records")
            conn.commit()

    def close(self) -> None:
        """显式关闭单例连接（可选；进程退出时 SQLite 也会自动释放）。"""
        with self._conn_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def export_patterns_json(self) -> str:
        """导出所有 pattern 为 JSON 字符串。"""
        patterns = self.list_patterns()
        return json.dumps(
            [p.to_dict() for p in patterns],
            ensure_ascii=False,
            indent=2,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 演示 / 示例
# ─────────────────────────────────────────────────────────────────────────────

def _demo() -> None:
    """示例：记录 5 条 mock 数据，learn_patterns 识别出 1 个 pattern。"""
    # Ensure nl2shortcut package is importable in standalone runs
    import sys as _sys
    _me = __file__
    _parent = __import__('pathlib', fromlist=['']).Path(_me).parent.parent
    if str(_parent) not in _sys.path:
        _sys.path.insert(0, str(_parent))

    import uuid
    import tempfile

    # 使用临时数据库，不影响生产数据
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = tmp.name
    tmp.close()

    print("=" * 60)
    print("OperationMemory 演示")
    print("=" * 60)

    mem = OperationMemory(db_path=db_path)

    # ── Mock 数据：用户在 notepad 中执行「全选→复制→粘贴」操作 3 次 ──────
    #   模拟每次操作间隔 < 30s（同一 sequence）
    base_time = datetime.now()

    mock_sequences = [
        # 第 1 次序列
        [
            OpRecord(timestamp=base_time, app="notepad", action_type="shortcut",
                     action_detail="Ctrl+A", context="", duration_ms=80,
                     user_goal="全选文本", sequence_id=""),
            OpRecord(timestamp=base_time + timedelta(seconds=1), app="notepad",
                     action_type="shortcut", action_detail="Ctrl+C",
                     context="clipboard=empty", duration_ms=70,
                     user_goal="复制文本", sequence_id=""),
            OpRecord(timestamp=base_time + timedelta(seconds=2), app="notepad",
                     action_type="shortcut", action_detail="Ctrl+V",
                     context="clipboard='hello'", duration_ms=90,
                     user_goal="粘贴文本", sequence_id=""),
        ],
        # 第 2 次序列（相同模式）
        [
            OpRecord(timestamp=base_time + timedelta(minutes=5), app="notepad",
                     action_type="shortcut", action_detail="Ctrl+A",
                     context="", duration_ms=80, user_goal="", sequence_id=""),
            OpRecord(timestamp=base_time + timedelta(minutes=5, seconds=1),
                     app="notepad", action_type="shortcut", action_detail="Ctrl+C",
                     context="clipboard=empty", duration_ms=65, user_goal="", sequence_id=""),
            OpRecord(timestamp=base_time + timedelta(minutes=5, seconds=2),
                     app="notepad", action_type="shortcut", action_detail="Ctrl+V",
                     context="clipboard='world'", duration_ms=88, user_goal="", sequence_id=""),
        ],
        # 第 3 次序列（相同模式）
        [
            OpRecord(timestamp=base_time + timedelta(minutes=10), app="notepad",
                     action_type="shortcut", action_detail="Ctrl+A",
                     context="", duration_ms=82, user_goal="", sequence_id=""),
            OpRecord(timestamp=base_time + timedelta(minutes=10, seconds=1),
                     app="notepad", action_type="shortcut", action_detail="Ctrl+C",
                     context="clipboard=empty", duration_ms=71, user_goal="", sequence_id=""),
            OpRecord(timestamp=base_time + timedelta(minutes=10, seconds=2),
                     app="notepad", action_type="shortcut", action_detail="Ctrl+V",
                     context="clipboard='!'", duration_ms=91, user_goal="", sequence_id=""),
        ],
    ]

    print("\n📝 记录 3 条 mock 操作序列（各 3 步）...")
    for seq_idx, seq in enumerate(mock_sequences):
        ids = mem.record_sequence(seq)
        print(f"   序列 {seq_idx + 1}: 记录 ids={ids}")

    # ── 自动学习 ─────────────────────────────────────────────────────────────
    print("\n🔍 执行 learn_patterns(min_frequency=2)...")
    patterns = mem.learn_patterns(min_frequency=2)

    if patterns:
        print(f"\n✅ 学习到 {len(patterns)} 个 pattern：\n")
        for p in patterns:
            print(f"   名称      : {p.name}")
            print(f"   描述      : {p.description}")
            print(f"   应用      : {p.app}")
            print(f"   频率      : {p.frequency} 次")
            print(f"   置信度    : {p.confidence:.0%}")
            print(f"   步骤      : {p.steps}")
            print(f"   平均耗时  : {p.avg_duration_ms:.0f} ms")
            print()
    else:
        print("   未识别到重复 pattern（可能是间隔超过 30s）")

    # ── suggest_next 演示 ───────────────────────────────────────────────────
    print("💡 模拟 suggest_next(current_app='notepad', recent_actions=['Ctrl+A'])...")
    suggestion = mem.suggest_next("notepad", ["Ctrl+A"])
    if suggestion:
        print(f"   建议执行: {suggestion.name}")
        print(f"   下一步  : {suggestion.steps[1]}")
    else:
        print("   暂无建议")

    # ── 建议引擎演示 ─────────────────────────────────────────────────────────
    print("\n🔔 get_suggestion 演示...")
    for goal, app in [
        ("我想全选这个文件的内容", "notepad"),
        ("帮我新建一封邮件", "outlook"),
        ("打开文件夹", "explorer"),
    ]:
        advice = mem.get_suggestion(goal, app)
        print(f"\n   目标: {goal}  (app={app})")
        if advice:
            print(f"   {advice}")
        else:
            print("   (无建议)")

    # ── save_pattern 演示 ───────────────────────────────────────────────────
    print("\n💾 手动保存用户命名的 pattern...")
    user_pattern = OpPattern(
        name="快速复制粘贴",
        description="notepad 中全选→复制→粘贴的快捷方式",
        app="notepad",
        steps=[
            {"type": "shortcut", "key": "Ctrl+A"},
            {"type": "shortcut", "key": "Ctrl+C"},
            {"type": "shortcut", "key": "Ctrl+V"},
        ],
        frequency=10,
        avg_duration_ms=250,
        last_used=datetime.now(),
        confidence=0.95,
    )
    pid = mem.save_pattern(user_pattern)
    print(f"   保存成功，id={pid}")

    # ── list_patterns 演示 ───────────────────────────────────────────────────
    print("\n📋 所有已保存的 pattern：")
    all_p = mem.list_patterns()
    for p in all_p:
        print(f"   [{p.id}] {p.name}  (app={p.app}, freq={p.frequency}, conf={p.confidence:.0%})")

    # ── get_pattern 演示 ────────────────────────────────────────────────────
    print("\n🔎 get_pattern('快速复制粘贴')...")
    found = mem.get_pattern("快速复制粘贴")
    if found:
        print(f"   找到: {found.name}, steps={found.steps}")

    # ── 数据库路径确认 ───────────────────────────────────────────────────────
    print(f"\n📂 数据库路径: {db_path}")
    print("演示完成 ✓")

    # 清理临时文件
    Path(db_path).unlink(missing_ok=True)


if __name__ == "__main__":
    _demo()
