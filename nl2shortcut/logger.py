"""日志记录与统计模块。

审计日志格式（JSON Lines, append-only）已按照等保 2.0 三级要求预留字段：
  - user_id        : 操作人标识（默认 "local_user"）
  - session_id     : 会话标识（默认 "terminal"）
  - source_ip      : 来源 IP（默认 "127.0.0.1"）
  - compliance_level: 合规等级（默认 "baseline"）
  - operation_type : 操作类型（默认 "shortcut_exec"）
  - target_resource: 目标资源（默认 ""）

─── 链式哈希审计（ChainHash Audit）──────────────────────────────────
每条日志记录携带两个哈希字段，形成防篡改链：

  prev_hash : 上一条记录的 this_hash（创世记录为 64 个 "0"）
  this_hash : SHA256(prev_hash || canonical_json(record_without_hash_fields))

任何对历史记录的篡改都会导致后续所有记录的 this_hash 校验失败。
调用 verify_chain() 可校验整条链的完整性，detect_tamper() 返回首个断裂点。
"""

import json
import time
import socket
import uuid
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Tuple, List

from .models import ExecutionResult, Stats

# 创世哈希：链首的 prev_hash 固定值
_GENESIS_HASH = "0" * 64


class Logger:
    """负责执行日志与统计信息。

    所有执行事件以 JSON Lines 追加写入每日滚动日志文件。
    每条记录携带等保合规预留字段，可在运行时通过 set_compliance_context() 设置。

    链式哈希：每条记录的 this_hash 依赖上一条的 this_hash，形成防篡改链。
    """

    def __init__(self, log_dir: Optional[Path] = None):
        if log_dir is None:
            log_dir = Path.home() / ".nl2shortcut" / "logs"
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._consecutive_failures = 0
        self._stats_cache = None
        self._stats_timestamp = 0.0

        # ── 等保合规上下文（运行时可覆盖） ──
        self._compliance_ctx: Dict[str, str] = {
            "user_id": "local_user",
            "session_id": str(uuid.uuid4()),
            "source_ip": "127.0.0.1",
            "compliance_level": "baseline",
        }

        # ── 链式哈希状态 ──
        # _chain_head 保存最后一条写入记录的 this_hash。
        # 启动时从当日日志文件恢复，确保跨进程重启后链连续。
        self._chain_head: str = _GENESIS_HASH
        self._chain_seq: int = 0
        self._recover_chain_head()

    # ── 等保合规上下文管理 ────────────────────────────────────────

    def set_compliance_context(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        source_ip: Optional[str] = None,
        compliance_level: Optional[str] = None,
    ) -> None:
        """设置等保合规上下文字段。

        调用方可按需覆盖；未传入的字段保持不变。
        典型用法：
            logger.set_compliance_context(
                user_id="admin@example.com",
                source_ip="192.168.1.100",
                compliance_level="classified",
            )
        """
        if user_id is not None:
            self._compliance_ctx["user_id"] = user_id
        if session_id is not None:
            self._compliance_ctx["session_id"] = session_id
        if source_ip is not None:
            self._compliance_ctx["source_ip"] = source_ip
        if compliance_level is not None:
            self._compliance_ctx["compliance_level"] = compliance_level

    def _build_compliance_block(self) -> dict:
        """构建等保合规字段块（每次写入日志时调用）。"""
        return {
            "user_id": self._compliance_ctx["user_id"],
            "session_id": self._compliance_ctx["session_id"],
            "source_ip": self._compliance_ctx["source_ip"],
            "compliance_level": self._compliance_ctx["compliance_level"],
        }

    # ── 链式哈希核心 ──────────────────────────────────────────────

    @staticmethod
    def _compute_hash(prev_hash: str, record: dict) -> str:
        """计算链式哈希：this_hash = SHA256(prev_hash || canonical_json(record))。

        record 应为不含 prev_hash/this_hash 字段的业务记录。
        canonical_json 通过 sorted keys 保证序列化确定性。
        """
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"))
        raw = prev_hash + canonical
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _recover_chain_head(self) -> None:
        """从当日日志文件恢复链头（跨进程重启后链连续）。

        读取最后一行的 this_hash 作为 _chain_head，并设置 _chain_seq。
        若文件不存在或为空，保持创世哈希。
        """
        try:
            log_file = self._log_file()
            if not log_file.exists():
                return
            # 只读尾部 4KB，切出最后一行（避免大文件全量读取）
            with open(log_file, "rb") as f:
                f.seek(0, 2)  # SEEK_END
                size = f.tell()
                if size == 0:
                    return
                tail_size = min(size, 4096)
                f.seek(size - tail_size)
                tail = f.read(tail_size)
            # 切最后一个非空行
            lines = tail.decode("utf-8", errors="ignore").splitlines()
            line = ""
            for ln in reversed(lines):
                if ln.strip():
                    line = ln.strip()
                    break
            if not line:
                return
            entry = json.loads(line)
            this_hash = entry.get("this_hash")
            seq = entry.get("seq", 0)
            if isinstance(this_hash, str) and len(this_hash) == 64:
                self._chain_head = this_hash
                self._chain_seq = int(seq) if isinstance(seq, int) else 0
        except Exception:
            # 恢复失败保持创世哈希，不影响主流程
            pass

    def verify_chain(self, log_file: Optional[Path] = None) -> bool:
        """校验日志链完整性。所有记录的 this_hash 重新计算后必须一致。

        Args:
            log_file: 指定日志文件；默认当日文件。
        Returns:
            True 表示链完整无篡改。
        """
        target = log_file or self._log_file()
        if not target.exists():
            return True
        prev = _GENESIS_HASH
        try:
            with open(target, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    this_hash = entry.get("this_hash")
                    prev_hash = entry.get("prev_hash")
                    if not isinstance(this_hash, str) or prev_hash != prev:
                        return False
                    # 重算 this_hash（剔除哈希字段）
                    record = {k: v for k, v in entry.items()
                              if k not in ("this_hash", "prev_hash")}
                    expected = self._compute_hash(prev, record)
                    if expected != this_hash:
                        return False
                    prev = this_hash
            return True
        except Exception:
            return False

    def detect_tamper(self, log_file: Optional[Path] = None) -> Optional[int]:
        """返回首个断裂点的行号（1-based）；链完整返回 None。"""
        target = log_file or self._log_file()
        if not target.exists():
            return None
        prev = _GENESIS_HASH
        try:
            with open(target, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    this_hash = entry.get("this_hash")
                    prev_hash = entry.get("prev_hash")
                    if not isinstance(this_hash, str) or prev_hash != prev:
                        return lineno
                    record = {k: v for k, v in entry.items()
                              if k not in ("this_hash", "prev_hash")}
                    expected = self._compute_hash(prev, record)
                    if expected != this_hash:
                        return lineno
                    prev = this_hash
            return None
        except Exception:
            return None

    # ── 日志文件 ──────────────────────────────────────────────────

    def _log_file(self) -> Path:
        date_str = datetime.now().strftime("%Y%m%d")
        return self.log_dir / f"scut_{date_str}.log"

    def log_execution(self, result: ExecutionResult) -> None:
        # 构建业务记录（不含哈希字段）
        record = {
            # 时间与身份
            "timestamp": datetime.now().isoformat(),
            "seq": self._chain_seq + 1,
            # 操作内容
            "intent": result.intent,
            "command": result.command,
            "key_combination": result.key_combination,
            "operation_type": "shortcut_exec",
            "target_resource": result.platform or "",
            # 结果
            "success": result.success,
            "processing_time_ms": round(result.processing_time * 1000, 2),
            "error": result.error,
            "mode": result.mode or "",
            "confidence": round(result.confidence, 4),
            # 等保合规字段（预留）
            **self._build_compliance_block(),
        }
        # 计算链式哈希
        this_hash = self._compute_hash(self._chain_head, record)
        entry = dict(record)
        entry["prev_hash"] = self._chain_head
        entry["this_hash"] = this_hash

        with open(self._log_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 更新链头
        self._chain_head = this_hash
        self._chain_seq += 1

        if result.success:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
        self._stats_cache = None

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def get_stats(self) -> Stats:
        now = time.time()
        if self._stats_cache is not None and (now - self._stats_timestamp) < 1.0:
            return self._stats_cache

        stats = Stats()
        processing_times = []

        for log_file in sorted(self.log_dir.glob("scut_*.log")):
            try:
                with open(log_file, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            stats.total_executions += 1
                            if entry.get("success"):
                                stats.successful += 1
                                pt = entry.get("processing_time_ms", 0) / 1000
                                processing_times.append(pt)
                            else:
                                stats.failed += 1
                        except json.JSONDecodeError:
                            continue
            except Exception:
                continue

        if processing_times:
            stats.total_processing_time = sum(processing_times)
            stats.avg_processing_time = (
                stats.total_processing_time / len(processing_times)
            )
        self._stats_cache = stats
        self._stats_timestamp = now
        return stats

    def reset_stats(self) -> None:
        import shutil
        if self.log_dir.exists():
            shutil.rmtree(self.log_dir)
            self.log_dir.mkdir(parents=True, exist_ok=True)
        self._consecutive_failures = 0
        self._stats_cache = None
