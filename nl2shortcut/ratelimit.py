"""NL2Shortcut Agent API 的限流器。

基于令牌桶算法，按 (身份, 端点类别) 维度限流。允许突发至 `burst` 个请求，
并以 `rate_per_sec`（每秒）的速率补充令牌。

端点类别：
  - "execute"   : /v1/execute、/v1/sequence（开销最大）
  - "recognize" : /v1/recognize
  - "plan"      : /v1/plan
  - "meta"      : /v1/health、/v1/keys、/v1/capabilities、/v1/session/*（不限流）

默认配置：
  execute:   10 次/秒，突发 20
  recognize: 30 次/秒，突发 60
  plan:      5 次/秒，突发 10

429 响应包含以下字段：
  - retry_after_ms（需等待的毫秒数）
  - limit（每秒上限）
  - burst（突发上限）
  - remaining（剩余令牌，近似值）
"""
import time
import threading
from typing import Dict, Tuple, Optional


class _Bucket:
    __slots__ = ("tokens", "last_refill", "rate", "burst")

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()

    def try_consume(self, cost: float = 1.0) -> Tuple[bool, float, int]:
        """返回 (是否放行, 需等待毫秒数, 剩余令牌近似值)。"""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.last_refill = now
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0, int(self.tokens)
            # 需要等待
        deficit = cost - self.tokens
        retry_ms = (deficit / self.rate) * 1000.0
        return False, retry_ms, 0


_LIMITS = {
    "execute":   (10.0, 20),
    "recognize": (30.0, 60),
    "plan":      (5.0,  10),
    "meta":      (1000.0, 1000),  # 基本不限流
}

_lock = threading.RLock()
_buckets: Dict[Tuple[str, str], _Bucket] = {}


def check(identity: str, endpoint_class: str, cost: float = 1.0) -> Dict:
    """检查限流状态，返回包含是否放行、retry_after_ms、limit、burst、remaining 的字典。"""
    rate, burst = _LIMITS.get(endpoint_class, _LIMITS["meta"])
    if endpoint_class == "meta":
        # 元信息类端点永不阻塞
        return {"allowed": True, "retry_after_ms": 0.0, "limit": rate, "burst": burst, "remaining": burst, "endpoint_class": endpoint_class}
    key = (identity, endpoint_class)
    with _lock:
        b = _buckets.get(key)
        if b is None:
            b = _Bucket(rate, burst)
            _buckets[key] = b
        allowed, retry_ms, remaining = b.try_consume(cost)
        return {
            "allowed":         allowed,
            "retry_after_ms":  round(retry_ms, 1),
            "limit":           rate,
            "burst":           burst,
            "remaining":       remaining,
            "endpoint_class":  endpoint_class,
        }


def status(identity: str) -> Dict:
    """返回某身份下所有端点类别的限流状态（供 /v1/rate_limit 使用）。"""
    out = {}
    for cls in ("execute", "recognize", "plan"):
        rate, burst = _LIMITS[cls]
        key = (identity, cls)
        with _lock:
            b = _buckets.get(key)
            if b is None:
                out[cls] = {"limit": rate, "burst": burst, "remaining": burst, "used": 0}
            else:
                # 不消耗令牌，仅查看当前状态
                out[cls] = {"limit": rate, "burst": burst, "remaining": int(b.tokens), "used": 0}
    return out


def reset(identity: Optional[str] = None):
    """重置令牌桶（管理员操作）。"""
    with _lock:
        if identity is None:
            _buckets.clear()
        else:
            for k in list(_buckets.keys()):
                if k[0] == identity:
                    del _buckets[k]
