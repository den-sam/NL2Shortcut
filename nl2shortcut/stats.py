"""NL2Shortcut Agent API 的进程内执行统计。

跟踪各层（per-tier）计数器、各命令计数器、错误码分布、延迟百分位以及
降级（fallback）事件。所有状态均保存在进程内，并在 GUI / 服务器重启时
重置（这是有意为之 —— 全新启动即全新基线）。

专用于 `/v1/stats` 接口，以及一个可选的 `/v1/stats/reset`
（仅限管理员权限范围）。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, asdict
from typing import Any, Deque, Dict, List, Optional


@dataclass
class TierCounters:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    total_latency_ms: float = 0.0
    last_used_at: float = 0.0

    @property
    def success_rate(self) -> float:
        return (self.successes / self.requests) if self.requests else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return (self.total_latency_ms / self.requests) if self.requests else 0.0


@dataclass
class FallbackEvent:
    at: float
    from_tier: str
    to_tier: str
    intent: str
    error_code: str
    target: str


class StatsCollector:
    """线程安全的进程内统计聚合器。"""

    def __init__(self, max_recent: int = 200):
        self._lock = threading.Lock()
        self._max_recent = max_recent

        # 各层计数器
        self.tier_counters: Dict[str, TierCounters] = defaultdict(TierCounters)

        # 各命令计数器（键为 "<tier>:<command>"）
        self.command_counters: Dict[str, TierCounters] = defaultdict(TierCounters)

        # 错误码分布
        self.error_codes: Dict[str, int] = defaultdict(int)

        # 各层的延迟采样（使用 deque 以便计算百分位）
        self.latency_samples: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self._max_recent)
        )

        # 降级事件
        self.fallback_events: Deque[FallbackEvent] = deque(maxlen=self._max_recent)

        # 会话开始时间
        self.started_at: float = time.time()

        # 请求日志（最近 N 条请求，FIFO）
        self.recent_requests: Deque[Dict[str, Any]] = deque(maxlen=self._max_recent)

    # ── 记录 API ──

    def record_request(
        self,
        tier: str,
        command: str,
        latency_ms: float,
        success: bool,
        error_code: str = "ok",
    ) -> None:
        with self._lock:
            tc = self.tier_counters[tier]
            tc.requests += 1
            tc.total_latency_ms += latency_ms
            tc.last_used_at = time.time()
            if success:
                tc.successes += 1
            else:
                tc.failures += 1

            key = f"{tier}:{command}"
            cc = self.command_counters[key]
            cc.requests += 1
            cc.total_latency_ms += latency_ms
            if success:
                cc.successes += 1
            else:
                cc.failures += 1

            if not success:
                self.error_codes[error_code] += 1

            self.latency_samples[tier].append(latency_ms)

            self.recent_requests.append({
                "at": time.time(),
                "tier": tier,
                "command": command,
                "latency_ms": latency_ms,
                "success": success,
                "error_code": error_code,
            })

    def record_fallback(
        self,
        from_tier: str,
        to_tier: str,
        intent: str,
        error_code: str,
        target: str,
    ) -> None:
        with self._lock:
            self.fallback_events.append(
                FallbackEvent(
                    at=time.time(),
                    from_tier=from_tier,
                    to_tier=to_tier,
                    intent=intent,
                    error_code=error_code,
                    target=target,
                )
            )

    # ── 读取 API ──

    def _percentile(self, samples: Deque[float], p: float) -> float:
        if not samples:
            return 0.0
        sorted_s = sorted(samples)
        idx = int(len(sorted_s) * p)
        idx = max(0, min(idx, len(sorted_s) - 1))
        return sorted_s[idx]

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            uptime_sec = now - self.started_at
            total_requests = sum(t.requests for t in self.tier_counters.values())
            total_successes = sum(t.successes for t in self.tier_counters.values())
            total_failures = sum(t.failures for t in self.tier_counters.values())

            tiers_out: Dict[str, Any] = {}
            for tier, tc in self.tier_counters.items():
                samples = self.latency_samples.get(tier, deque())
                tiers_out[tier] = {
                    "requests": tc.requests,
                    "successes": tc.successes,
                    "failures": tc.failures,
                    "success_rate": round(tc.success_rate, 4),
                    "avg_latency_ms": round(tc.avg_latency_ms, 2),
                    "p50_latency_ms": round(self._percentile(samples, 0.5), 2),
                    "p95_latency_ms": round(self._percentile(samples, 0.95), 2),
                    "p99_latency_ms": round(self._percentile(samples, 0.99), 2),
                    "last_used_at": tc.last_used_at,
                }

            # 热门命令（按请求数量排序）
            top_commands = sorted(
                (
                    {
                        "key": k,
                        "tier": k.split(":", 1)[0],
                        "command": k.split(":", 1)[1],
                        "requests": v.requests,
                        "successes": v.successes,
                        "failures": v.failures,
                        "success_rate": round(v.success_rate, 4),
                    }
                    for k, v in self.command_counters.items()
                ),
                key=lambda x: x["requests"],
                reverse=True,
            )[:10]

            return {
                "ok": True,
                "uptime_sec": round(uptime_sec, 1),
                "total_requests": total_requests,
                "total_successes": total_successes,
                "total_failures": total_failures,
                "overall_success_rate": (
                    round(total_successes / total_requests, 4) if total_requests else 0.0
                ),
                "tiers": tiers_out,
                "top_commands": top_commands,
                "error_codes": dict(self.error_codes),
                "fallback_events": [
                    {
                        "at": e.at,
                        "from_tier": e.from_tier,
                        "to_tier": e.to_tier,
                        "intent": e.intent,
                        "error_code": e.error_code,
                        "target": e.target,
                    }
                    for e in list(self.fallback_events)[-20:]
                ],
                "recent_requests": list(self.recent_requests)[-20:],
            }

    def reset(self) -> None:
        with self._lock:
            self.tier_counters.clear()
            self.command_counters.clear()
            self.error_codes.clear()
            self.latency_samples.clear()
            self.fallback_events.clear()
            self.recent_requests.clear()
            self.started_at = time.time()


# 模块级单例
_stats: Optional[StatsCollector] = None


def get_stats() -> StatsCollector:
    global _stats
    if _stats is None:
        _stats = StatsCollector()
    return _stats
