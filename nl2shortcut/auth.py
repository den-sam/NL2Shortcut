"""第 0 层 —— NL2Shortcut Agent API 的身份认证。

API Key 模型：
  - 通过 `Authorization: Bearer scut_xxx` 请求头传递，或
  - 通过 `X-API-Key` 请求头传递（适用于非 Bearer 客户端）
  - 通过 `?api_key=` 查询参数传递（适用于浏览器 / 开发工具，但会被记录到日志）

有效密钥来源（按优先级）：
  1. 环境变量 `NL2SHORTCUT_API_KEYS` —— 逗号分隔的白名单，例如
     "nl2shortcut_dev_local,scut_agent_openclaw"（始终允许，不校验）
  2. 文件 `~/.nl2shortcut/api_keys.json` —— JSON 格式 {"<key>": {"name": ..., "scopes": [...]}}
  3. 若两者都不存在：开发模式 —— 接受任意非空密钥（带警告），
     以及一个始终允许的专用 `nl2shortcut_dev_local` 开发密钥。

权限范围（scopes，面向未来扩展；目前尚未强制校验）：
  - "execute"    : 可调用 /v1/execute 与 /v1/sequence
  - "recognize"  : 可调用 /v1/recognize
  - "plan"       : 可调用 /v1/plan
  - "admin"      : 可调用 /v1/session/* 并轮换密钥

成功返回 AuthContext，失败返回 None。
"""
import os
import json
import hmac
import hashlib
import secrets
import threading
from typing import Optional, Dict, Any, List
from pathlib import Path

# 每个请求都会对应一个 AuthContext。开发模式请求会获得
# 一个特殊的 "dev" 身份，以便后续流水线记录其来源。

AUTH_HEADER = "Authorization"
API_KEY_HEADER = "X-API-Key"

DEV_API_KEY = "nl2shortcut_dev_local"
DEV_IDENTITY = "dev_mode"

# 加载密钥文件时使用的锁（避免首次访问时的 TOCTOU 竞态）
_KEYS_LOCK = threading.Lock()
_KEYS_CACHE: Optional[Dict[str, Dict[str, Any]]] = None


def _hash_key(key: str) -> str:
    """返回用于日志的简短指纹（不存储完整密钥）。"""
    return "scut_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _load_key_file() -> Dict[str, Dict[str, Any]]:
    """加载 ~/.nl2shortcut/api_keys.json（惰性加载，带缓存）。"""
    global _KEYS_CACHE
    with _KEYS_LOCK:
        if _KEYS_CACHE is not None:
            return _KEYS_CACHE
        p = Path.home() / ".nl2shortcut" / "api_keys.json"
        if p.exists():
            try:
                _KEYS_CACHE = json.loads(p.read_text(encoding="utf-8"))
                return _KEYS_CACHE
            except Exception:
                _KEYS_CACHE = {}
                return _KEYS_CACHE
        _KEYS_CACHE = {}
        return _KEYS_CACHE


def _load_env_keys() -> List[str]:
    """读取 NL2SHORTCUT_API_KEYS 环境变量（逗号分隔）。"""
    raw = os.environ.get("NL2SHORTCUT_API_KEYS", "").strip()
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def is_dev_mode() -> bool:
    """未配置任何鉴权来源时为 True。开发模式下接受任意非空密钥
    （或无密钥——适用于本地开发）。"""
    return not _load_env_keys() and not _load_key_file()


def authenticate(headers: Dict[str, str], query_params: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """对请求进行鉴权。

    成功返回 AuthContext 字典，失败返回 None。
    AuthContext = {"api_key_hash": ..., "identity": ..., "scopes": [...], "dev_mode": bool}
    """
    # 1. Extract candidate key
    auth = headers.get(AUTH_HEADER, "")
    key = None
    if auth:
        # "Bearer xxx" 或 "bearer xxx"
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            key = parts[1].strip()
        elif ":" in auth and auth.lower().startswith("nl2shortcut"):
            # 直接使用 "scut_xxx"（部分非 Bearer 客户端）
            key = auth.strip()
    if not key:
        key = headers.get(API_KEY_HEADER, "").strip() or None
    if not key and query_params:
        key = query_params.get("api_key", "").strip() or None

    if not key:
        if is_dev_mode():
            return {"api_key_hash": "no_key", "identity": DEV_IDENTITY, "scopes": ["execute", "recognize", "plan", "admin"], "dev_mode": True}
        return None

    # 始终允许的开发者密钥
    if hmac.compare_digest(key, DEV_API_KEY):
        return {"api_key_hash": _hash_key(key), "identity": "dev_local", "scopes": ["execute", "recognize", "plan", "admin"], "dev_mode": True}

    # 环境变量白名单
    env_keys = _load_env_keys()
    for ek in env_keys:
        if hmac.compare_digest(key, ek):
            return {"api_key_hash": _hash_key(key), "identity": f"env:{_hash_key(ek)}", "scopes": ["execute", "recognize", "plan", "admin"], "dev_mode": False}

    # 基于文件
    file_keys = _load_key_file()
    if key in file_keys:
        meta = file_keys[key]
        return {
            "api_key_hash": _hash_key(key),
            "identity":     meta.get("name", _hash_key(key)),
            "scopes":       meta.get("scopes", ["execute", "recognize", "plan"]),
            "dev_mode":     False,
        }

    return None


def has_scope(auth: Optional[Dict[str, Any]], scope: str) -> bool:
    if auth is None:
        return False
    return scope in (auth.get("scopes") or [])


def generate_api_key() -> str:
    """生成一个新的 API 密钥（供管理工具使用）。"""
    return "scut_" + secrets.token_urlsafe(24)
