"""NL2Shortcut Agent API 的会话存储。

会话是 Agent 运行所处的逻辑上下文。多步计划正是通过会话在多次 HTTP 请求之间
共享状态（当前应用、上一次动作、历史记录）。

生命周期：
  POST /v1/session/start  -> {"session_id": "sess_xxx", ...}
  POST /v1/execute        (带 session_id) -> 更新 session.last_action 与 history
  POST /v1/session/end    (带 session_id) -> 关闭会话并返回摘要

存储：内存字典（单进程）。若需多进程部署，后续可替换为 Redis。
当前关键的只是接口（API）契约。

会话过期：无操作 1 小时后过期。在下次 /v1/heartbeat 时回收，或访问时惰性回收。
"""
import time
import threading
import secrets
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

SESSION_TTL_SEC = 3600
MAX_HISTORY = 200
MAX_SESSIONS = 1000  # 硬性上限，防止内存膨胀


@dataclass
class Session:
    session_id: str
    identity: str  # 所属者（api_key_hash 或 "dev"）
    created_at: float
    last_used: float
    history: List[Dict[str, Any]] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)  # app、last_app 等
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0

    def touch(self):
        self.last_used = time.time()
        self.request_count += 1

    def add_history(self, step: Dict[str, Any], success: bool):
        self.history.append({
            "ts": time.time(),
            "intent":   step.get("intent", ""),
            "command":  step.get("command", ""),
            "keys":     step.get("key_combination", ""),
            "success":  success,
            "app":      step.get("app", "") or self.context.get("app", ""),
        })
        if success:
            self.success_count += 1
        else:
            self.failure_count += 1
        # 裁剪历史，超出上限时仅保留最近部分
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]

    def update_context(self, **kwargs):
        self.context.update(kwargs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id":     self.session_id,
            "identity":       self.identity,
            "created_at":     self.created_at,
            "last_used":      self.last_used,
            "request_count":  self.request_count,
            "success_count":  self.success_count,
            "failure_count":  self.failure_count,
            "history_len":    len(self.history),
            "context":        dict(self.context),
        }


class SessionStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: Dict[str, Session] = {}

    def start(self, identity: str, app: str = "", platform: str = "") -> Session:
        with self._lock:
            self._gc()
            if len(self._sessions) >= MAX_SESSIONS:
                # 淘汰最久未使用的会话
                oldest_id = min(self._sessions, key=lambda s: self._sessions[s].last_used)
                del self._sessions[oldest_id]
            sid = "sess_" + secrets.token_urlsafe(12)
            sess = Session(
                session_id=sid,
                identity=identity,
                created_at=time.time(),
                last_used=time.time(),
                context={"app": app, "platform": platform},
            )
            self._sessions[sid] = sess
            return sess

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                return None
            if time.time() - sess.last_used > SESSION_TTL_SEC:
                del self._sessions[session_id]
                return None
            return sess

    def end(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            sess = self._sessions.pop(session_id, None)
            if sess is None:
                return None
            return {
                "session_id":     sess.session_id,
                "identity":       sess.identity,
                "duration_sec":   round(time.time() - sess.created_at, 2),
                "request_count":  sess.request_count,
                "success_count":  sess.success_count,
                "failure_count":  sess.failure_count,
                "history_len":    len(sess.history),
            }

    def list_active(self) -> List[Dict[str, Any]]:
        with self._lock:
            self._gc()
            return [s.to_dict() for s in self._sessions.values()]

    def _gc(self):
        """移除过期的会话，调用方必须持有锁。"""
        now = time.time()
        expired = [sid for sid, s in self._sessions.items() if now - s.last_used > SESSION_TTL_SEC]
        for sid in expired:
            del self._sessions[sid]


# Module-level singleton (the HTTP handler imports this)
_store = SessionStore()


def store() -> SessionStore:
    return _store
