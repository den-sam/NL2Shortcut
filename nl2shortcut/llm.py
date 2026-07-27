"""DeepSeek LLM 意图识别引擎。

借助 DeepSeek 的自然语言理解能力实现智能快捷键匹配。
当 LLM 不可用或未配置 API Key 时，会优雅地回退到关键词引擎。

配置方式：
  环境变量：  DEEPSEEK_API_KEY
  配置文件：  ~/.nl2shortcut/config.json  {"deepseek_api_key": "sk-..."}
"""

import json
import time
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Dict, List, Any
from functools import lru_cache

from .models import IntentResult
from .database import DatabaseManager


DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_CHAT_MODEL = "deepseek-v4-pro"
REQUEST_TIMEOUT = 15  # seconds


def _load_api_key() -> Optional[str]:
    """从环境变量或配置文件加载 DeepSeek API Key。"""
    # 1. 环境变量
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key:
        return key

    # 2. 配置文件
    config_path = Path.home() / ".nl2shortcut" / "config.json"
    try:
        if config_path.exists():
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            return cfg.get("deepseek_api_key")
    except Exception:
        pass

    return None


def _save_api_key(key: str) -> None:
    """将 API Key 持久化保存到配置文件。"""
    config_dir = Path.home() / ".nl2shortcut"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"

    cfg: Dict[str, Any] = {}
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    cfg["deepseek_api_key"] = key
    config_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_system_prompt(shortcuts: List[Dict[str, str]]) -> str:
    """构建系统提示词，列出所有可用快捷键。"""
    lines = [
        "你是一个快捷键匹配助手。用户用自然语言描述想做的事情，你从列表中匹配最合适的命令。",
        "",
        "可用命令列表：",
    ]
    done = set()
    for s in shortcuts:
        cmd = s["command"]
        if cmd in done:
            continue
        done.add(cmd)
        desc = s.get("description", "")
        cn = s.get("command_cn", "")
        lines.append(f"  - {cmd}: {desc}" + (f" (中文:{cn})" if cn else ""))

    lines.extend([
        "",
        "规则：",
        "1. 简单匹配：单个操作直接返回 JSON: {\"command\": \"命令名\", \"confidence\": 0.0-1.0, \"reasoning\": \"简短原因\"}",
        "2. 复杂/多步操作：返回分步计划 JSON:",
        "   {\"plan\": [{\"command\": \"命令名\", \"reason\": \"为什么这步\"}, ...], \"reasoning\": \"整体逻辑\", \"confidence\": 0.0-1.0}",
        "   每步命令必须来自命令列表。步数不超过5步。",
        "3. 如果用户输入匹配不上任何命令，command 设为空字符串，confidence 设为 0",
        "4. confidence 表示匹配确信度，完全匹配=0.95+，模糊匹配=0.6-0.8",
        "5. 只选择列表中的命令，不要编造",
        "6. 不要返回 JSON 以外的任何内容",
    ])
    return "\n".join(lines)


class DeepSeekEngine:
    """基于 DeepSeek API 的 LLM 意图识别引擎。

    用法：
        engine = DeepSeekEngine(db)
        if engine.available:
            result = engine.recognize("复制这段文字")
            print(result.command)  # "copy"
    """

    def __init__(self, db: DatabaseManager, api_key: Optional[str] = None):
        self._db = db
        self._api_key = api_key or _load_api_key()
        self._available = bool(self._api_key)
        self._last_error: Optional[str] = None
        self._total_calls = 0
        self._total_latency = 0.0
        self._failed_calls = 0
        self._cache: Dict[str, IntentResult] = {}
        self._cache_hits = 0
        self._cache_misses = 0

        # 一次性构建快捷键列表
        self._shortcut_list: List[Dict[str, str]] = []
        self._prompt: str = ""
        self._refresh_shortcuts()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def avg_latency(self) -> float:
        if self._total_calls == 0:
            return 0.0
        return self._total_latency / self._total_calls

    @property
    def cache_stats(self) -> Dict[str, int]:
        """LLM 缓存命中统计（hits/misses/size）。"""
        return {"hits": self._cache_hits, "misses": self._cache_misses, "size": len(self._cache)}

    def configure(self, api_key: str) -> bool:
        """设置 API Key。设置成功返回 True。"""
        if not api_key or not api_key.startswith("sk-"):
            self._last_error = "无效的 API Key（应以 sk- 开头）"
            return False
        self._api_key = api_key
        self._available = True
        self._cache.clear()
        _save_api_key(api_key)
        self._last_error = None
        return True

    def _refresh_shortcuts(self):
        """从数据库重新构建快捷键列表。"""
        self._cache.clear()
        all_shortcuts = self._db.get_all()
        self._shortcut_list = [
            {"command": s.command, "description": s.description,
             "command_cn": s.command_cn}
            for s in all_shortcuts
        ]
        self._prompt = _build_system_prompt(self._shortcut_list)

    def recognize(self, text: str) -> Optional[IntentResult]:
        """通过 DeepSeek API 识别意图。

        返回：
            简单匹配：返回带 command 的 IntentResult
            复杂计划：返回 command="__plan__" 的 IntentResult，
              其 alternatives 中包含拆解出的分步
        """
        if not self._available or not text.strip():
            return None

        # ── LLM 结果缓存：相同意图直接命中，零网络延迟 ──
        cache_key = text.strip().lower()
        if cache_key in self._cache:
            self._cache_hits += 1
            return self._cache[cache_key]
        self._cache_misses += 1

        start = time.perf_counter()
        try:
            result = self._call_api(text)
            elapsed = time.perf_counter() - start
            self._total_calls += 1
            self._total_latency += elapsed

            if result:
                # 检查是否为多步计划
                plan_steps = result.get("plan")
                if plan_steps and isinstance(plan_steps, list) and len(plan_steps) > 1:
                    # 多步计划：扁平化为带 alternatives 的 IntentResult
                    primary_cmd = plan_steps[0].get("command", "")
                    plan_alternatives = []
                    for i, step in enumerate(plan_steps):
                        plan_alternatives.append(IntentResult(
                            intent=step.get("command", ""),
                            command=step.get("command", ""),
                            confidence=result.get("confidence", 0.7) * (1.0 - i * 0.05),
                            matched_keyword=f"DeepSeek Plan: {step.get('reason', '')}",
                        ))
                    parsed = IntentResult(
                        intent=primary_cmd,
                        command="__plan__",
                        confidence=result.get("confidence", 0.7),
                        matched_keyword=f"DeepSeek Plan: {result.get('reasoning', '')}",
                        alternatives=plan_alternatives,
                    )
                    self._cache[cache_key] = parsed
                    return parsed
                # 简单单步匹配（保持向后兼容）
                parsed = IntentResult(
                    intent=result.get("command", ""),
                    command=result.get("command", ""),
                    confidence=float(result.get("confidence", 0)),
                    matched_keyword=f"DeepSeek: {result.get('reasoning', '')}",
                )
                self._cache[cache_key] = parsed
                return parsed
        except Exception as e:
            self._failed_calls += 1
            self._last_error = str(e)
            if self._failed_calls >= 3:
                print(
                    f"[nl2shortcut] DeepSeek API 连续 {self._failed_calls} 次失败，"
                    f"已自动回退到离线引擎。错误: {e}",
                    file=sys.stderr,
                )
                self._available = False

        return None

    def _call_api(self, text: str) -> Optional[Dict[str, Any]]:
        """向 DeepSeek 发起一次 API 调用。"""
        payload = json.dumps({
            "model": DEEPSEEK_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": self._prompt},
                {"role": "user", "content": text},
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

        # 解析响应中的 JSON（处理 markdown 代码围栏）
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # 尝试从混杂文本中提取 JSON
            import re
            match = re.search(r'\{[^}]+\}', content)
            if match:
                return json.loads(match.group())
            raise RuntimeError(f"无法解析 DeepSeek 响应: {content[:100]}")

    def process_text(
        self,
        instruction: str,
        clipboard_text: str,
        timeout: float = 30.0,
    ) -> Optional[str]:
        """自由文本处理：将剪贴板内容 + 用户指令发给 LLM，返回处理结果。

        用于剪贴板触发模式：选中文本 → 快捷键 → LLM 原地处理 → 粘贴写回。

        Args:
            instruction: 用户指令，如「翻译成英文」「总结这段文字」「格式化为代码」
            clipboard_text: 剪贴板中的原始文本
            timeout: API 超时秒数

        Returns:
            处理后的文本；若失败返回 None
        """
        if not self._available or not instruction.strip():
            return None

        system_prompt = (
            "你是一个高效的文本处理助手。"
            "用户会提供一段原始文本和一条处理指令。"
            "请只返回处理后的结果文本，不要加任何解释、前缀或后缀。"
            "如果是翻译任务，只输出译文。"
            "如果是总结任务，只输出总结。"
            "如果是格式化任务，只输出格式化后的文本。"
        )

        payload = json.dumps({
            "model": DEEPSEEK_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": (
                    f"指令：{instruction}\n\n"
                    f"原始文本：\n{clipboard_text}"
                )},
            ],
            "temperature": 0.3,
            "max_tokens": 4096,
            "stream": False,
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                f"{DEEPSEEK_BASE_URL}/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"].strip()
            return content
        except Exception as e:
            self._last_error = str(e)
            return None

    @staticmethod
    def check_connectivity() -> tuple[bool, str]:
        """快速检测：能否连接 DeepSeek API？"""
        key = _load_api_key()
        if not key:
            return False, "未设置 API Key（设置环境变量 DEEPSEEK_API_KEY 或在设置中配置）"

        try:
            req = urllib.request.Request(
                f"{DEEPSEEK_BASE_URL}/models",
                headers={"Authorization": f"Bearer {key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return True, "连接正常 ✓"
                return False, f"API 返回状态码 {resp.status}"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False, "API Key 无效（401 Unauthorized）"
            return False, f"API 错误 {e.code}"
        except Exception as e:
            return False, f"无法连接: {e}"
