"""日志记录与统计模块。

审计日志格式（JSON Lines, append-only）已按照等保 2.0 三级要求预留字段：
  - user_id        : 操作人标识（默认 "local_user"）
  - session_id     : 会话标识（默认 "terminal"）
  - source_ip      : 来源 IP（默认 "127.0.0.1"）
  - compliance_level: 合规等级（默认 "baseline"）
  - operation_type : 操作类型（默认 "shortcut_exec"）
  - target_resource: 目标资源（默认 ""）
"""

import json
import time
import socket
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict

from .models import ExecutionResult, Stats


class Logger:
    """负责执行日志与统计信息。

    所有执行事件以 JSON Lines 追加写入每日滚动日志文件。
    每条记录携带等保合规预留字段，可在运行时通过 set_compliance_context() 设置。
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

    # ── 日志文件 ──────────────────────────────────────────────────

    def _log_file(self) -> Path:
        date_str = datetime.now().strftime("%Y%m%d")
        return self.log_dir / f"scut_{date_str}.log"

    def log_execution(self, result: ExecutionResult) -> None:
        entry = {
            # 时间与身份
            "timestamp": datetime.now().isoformat(),
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
        with open(self._log_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

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
