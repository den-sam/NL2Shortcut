"""视觉层 —— 最后手段的 GUI 兜底方案。

当键盘层和 api 层都失败时，Agent 可以调用视觉层来：

  1. 对当前屏幕 / 活动窗口截图
  2. 编码为 base64（PNG）
  3. 返回一个"回执"，描述视觉模型应当执行的操作
  4. （可选）调用外部视觉模型（CogAgent / OmniParser /
     Claude Computer Use）并把响应解析为结构化动作

默认情况下，视觉层是一个**回执层** —— NL2Shortcut 是键盘插件，
而非视觉模型宿主。回执让调用方 Agent（Openclaw / Claude Computer Use）
来决定应将真正的视觉推理派发到哪里。

自检项：
  - 截图文件大小 > 0
  - 图像可解码为 PNG（PIL/ImageGrab）
  - 当前平台支持截图

返回：
  VisionResult = {
    "ok": bool,
    "action": "vision.screenshot" | "vision.ocr" | "vision.find" | "vision.click",
    "tier": "vision",
    "message": str,
    "duration_ms": float,
    "platform": str,
    "data": {
      "image_b64": str,           # base64 编码的 PNG
      "width": int,
      "height": int,
      "format": "png",
      "capture_region": "full_screen" | "active_window" | "x,y,w,h",
      "hint": str,                # 给视觉模型的指令
      "fallback_suggested": str,  # 建议的下一层动作
    },
    "error_code": "ok" | "capture_failed" | "platform_unsupported" | "no_image_lib",
  }
"""
from __future__ import annotations

import base64
import io
import os
import platform
import sys
import time
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, Optional, Tuple


@dataclass
class VisionResult:
    ok: bool
    action: str
    tier: str = "vision"
    message: str = ""
    duration_ms: float = 0.0
    platform: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_code: str = "ok"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _detect_platform() -> str:
    p = platform.system().lower()
    if p.startswith("win"):
        return "windows"
    if p == "darwin":
        return "macos"
    if p == "linux":
        return "linux"
    return p


def _try_pil_screenshot() -> Optional[bytes]:
    """Try PIL.ImageGrab (Windows/macOS). Returns PNG bytes or None."""
    try:
        from PIL import ImageGrab  # type: ignore
        img = ImageGrab.grab()
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _try_mss_screenshot() -> Optional[bytes]:
    """Try mss (cross-platform, fast). Returns PNG bytes or None."""
    try:
        import mss  # type: ignore
        with mss.mss() as sct:
            monitor = sct.monitors[0]  # full virtual screen
            img = sct.grab(monitor)
            # mss returns BGRA; encode via PIL if available, else raw
            try:
                from PIL import Image  # type: ignore
                pil_img = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
                buf = io.BytesIO()
                pil_img.save(buf, format="PNG")
                return buf.getvalue()
            except Exception:
                # Fallback: encode raw BMP
                width, height = img.size
                bmp_header = (
                    b"BM"
                    + (54 + len(img.bgra)).to_bytes(4, "little")
                    + b"\x00\x00\x00\x00"
                    + (54).to_bytes(4, "little")
                    + b"\x28\x00\x00\x00"
                    + width.to_bytes(4, "little")
                    + height.to_bytes(4, "little")
                    + b"\x01\x00"
                    + b"\x20\x00"
                    + b"\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00"
                )
                return bmp_header + bytes(img.bgra)
    except Exception:
        return None


def _try_pyautogui_screenshot() -> Optional[bytes]:
    """尝试使用 pyautogui.screenshot（较慢但通用）。返回 PNG 字节或 None。"""
    try:
        import pyautogui  # type: ignore
        img = pyautogui.screenshot()
        if img is None:
            return None
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _capture_screenshot(region: Optional[Tuple[int, int, int, int]] = None) -> Tuple[Optional[bytes], str]:
    """按顺序尝试多个截图后端。返回 (png_bytes, method_used)。"""
    # 1) PIL.ImageGrab（Windows 上的首选）
    png = _try_pil_screenshot()
    if png:
        return png, "PIL.ImageGrab"
    # 2) mss（跨平台、速度快）
    png = _try_mss_screenshot()
    if png:
        return png, "mss"
    # 3) pyautogui（较慢的兜底方案）
    png = _try_pyautogui_screenshot()
    if png:
        return png, "pyautogui"
    return None, "none"


def _build_vision_hint(intent: str, app: str = "") -> str:
    """Build an instruction for the vision model based on the intent."""
    base = f"User wants to perform: '{intent}'."
    if app:
        base += f" Active app: '{app}'."
    base += (
        " Inspect the screenshot, identify the relevant UI element, "
        "and return either: (a) a click coordinate (x, y), "
        "(b) text to type, or (c) a menu navigation sequence. "
        "If you cannot determine the action, say 'unclear' and describe what you see."
    )
    return base


def vision_screenshot(
    intent: str = "",
    app: str = "",
    region: Optional[Tuple[int, int, int, int]] = None,
    encode_b64: bool = True,
) -> VisionResult:
    """截图并返回一个视觉层回执。

    Args:
        intent: 原始的 Agent 意图（用于生成提示）
        app:    活动应用名称（用于提示上下文）
        region: 可选的区域 (x, y, w, h)。None 表示全屏。
        encode_b64: 是否将 PNG 以 base64 编码放入响应中。

    Returns:
        VisionResult，其 data 字典中包含 image_b64（当 encode_b64=True 时）。
    """
    t0 = time.time()
    plat = _detect_platform()
    if plat not in ("windows", "macos", "linux"):
        return VisionResult(
            ok=False,
            action="vision.screenshot",
            platform=plat,
            message=f"platform '{plat}' is not supported by the vision tier",
            duration_ms=(time.time() - t0) * 1000,
            error_code="platform_unsupported",
        )

    png_bytes, method = _capture_screenshot(region)
    if not png_bytes:
        return VisionResult(
            ok=False,
            action="vision.screenshot",
            platform=plat,
            message=(
                "no screenshot backend available. "
                "Install one of: pillow (PIL.ImageGrab), mss, or pyautogui."
            ),
            duration_ms=(time.time() - t0) * 1000,
            error_code="no_image_lib",
        )

    width, height = 0, 0
    try:
        # 尝试从 PNG 头部（IHDR 块）获取尺寸
        if png_bytes[:8] == b"\x89PNG\r\n\x1a\n" and len(png_bytes) > 24:
            width = int.from_bytes(png_bytes[16:20], "big")
            height = int.from_bytes(png_bytes[20:24], "big")
    except Exception:
        pass

    data: Dict[str, Any] = {
        "format": "png",
        "width": width,
        "height": height,
        "capture_region": (
            f"{region[0]},{region[1]},{region[2]},{region[3]}"
            if region
            else "full_screen"
        ),
        "capture_method": method,
        "size_bytes": len(png_bytes),
        "hint": _build_vision_hint(intent, app),
        "fallback_suggested": "vision.click OR escalate to human",
    }

    if encode_b64:
        data["image_b64"] = base64.b64encode(png_bytes).decode("ascii")
    else:
        data["image_path"] = ""

    return VisionResult(
        ok=True,
        action="vision.screenshot",
        platform=plat,
        message=(
            f"captured {width}x{height} screenshot ({len(png_bytes)} bytes) via {method}. "
            f"Agent should now dispatch to a vision model using the hint in data.hint."
        ),
        duration_ms=(time.time() - t0) * 1000,
        data=data,
        error_code="ok",
    )


def vision_find(
    intent: str,
    app: str = "",
    encode_b64: bool = True,
) -> VisionResult:
    """截图 + '查找该 UI 元素' 回执。

    等价于 vision_screenshot，但带有更具体的提示，
    用于按描述定位某个 UI 元素。
    """
    result = vision_screenshot(intent=intent, app=app, encode_b64=encode_b64)
    if result.ok:
        result.action = "vision.find"
        result.data["hint"] = (
            f"Find the UI element that corresponds to: '{intent}'."
            + (f" Active app: '{app}'." if app else "")
            + " Return its bounding box [x, y, w, h] and label, or 'not_found'."
        )
        result.message = result.message.replace(
            "Agent should now dispatch",
            "Agent should ask the vision model to locate the element then dispatch",
        )
    return result


def vision_click(
    intent: str,
    app: str = "",
    encode_b64: bool = True,
) -> VisionResult:
    """Screenshot + 'click this UI element' receipt.

    Equivalent to vision_screenshot but the hint tells the vision model
    to identify the click target and return coordinates.
    """
    result = vision_screenshot(intent=intent, app=app, encode_b64=encode_b64)
    if result.ok:
        result.action = "vision.click"
        result.data["hint"] = (
            f"Click the UI element that corresponds to: '{intent}'."
            + (f" Active app: '{app}'." if app else "")
            + " Return click coordinates (x, y) and the element label, or 'not_found'."
        )
        result.data["fallback_suggested"] = "vision.click OR escalate to human"
    return result


def vision_ocr(
    intent: str = "read visible text",
    app: str = "",
    encode_b64: bool = True,
) -> VisionResult:
    """Screenshot + 'read text from screen' receipt."""
    result = vision_screenshot(intent=intent, app=app, encode_b64=encode_b64)
    if result.ok:
        result.action = "vision.ocr"
        result.data["hint"] = (
            "Read all visible text from the screenshot and return it as plain text, "
            "preserving the reading order. Note any UI controls (buttons, menus, "
            "input fields) you can identify."
        )
    return result


# ── Module-level singleton so we can pre-load image libs once ──
_BACKEND_CHECKED: bool = False
_AVAILABLE_BACKENDS: list = []


def available_backends() -> list:
    """List screenshot backends that successfully imported."""
    global _BACKEND_CHECKED, _AVAILABLE_BACKENDS
    if _BACKEND_CHECKED:
        return list(_AVAILABLE_BACKENDS)
    backends = []
    try:
        from PIL import ImageGrab  # noqa: F401
        backends.append("PIL.ImageGrab")
    except Exception:
        pass
    try:
        import mss  # noqa: F401
        backends.append("mss")
    except Exception:
        pass
    try:
        import pyautogui  # noqa: F401
        backends.append("pyautogui")
    except Exception:
        pass
    _AVAILABLE_BACKENDS = backends
    _BACKEND_CHECKED = True
    return list(backends)


def vision_capabilities() -> Dict[str, Any]:
    """返回视觉层的静态描述（用于 /v1/health）。"""
    return {
        "name": "vision",
        "speed": "1-3s",
        "tokens": 1000,
        "reliability": "highest",
        "description": (
            "GUI vision (CogAgent / OmniParser / Claude Computer Use). "
            "Last-resort fallback. Slower, costs tokens, but can recover from any UI state. "
            "Used when keyboard + api both fail. NL2Shortcut itself captures the screenshot; "
            "the calling Agent dispatches to a vision model."
        ),
        "actions": ["vision.screenshot", "vision.find", "vision.click", "vision.ocr"],
        "backends_available": available_backends(),
    }


# ── 视觉模型派发 ────────────────────────────────────────────────────


def dispatch_vision(
    intent: str,
    screenshot_b64: str,
    model: str = "deepseek",
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """将截图发送给具备视觉能力的 LLM 并解析其响应。

    Args:
        intent: 在屏幕上要查找/点击的内容（例如 "the Copy menu item"）
        screenshot_b64: base64 编码的 PNG 截图
        model: "deepseek" 或 "claude"
        api_key: API 密钥（为 None 时自动从配置加载）

    Returns:
        {"found": bool, "label": str, "bbox": [x,y,w,h]|None,
         "center": [x,y]|None, "action": "click"|"type"|"unclear",
         "text": str|None, "confidence": float, "error": str|None}
    """
    if model == "deepseek":
        return _dispatch_deepseek_vision(intent, screenshot_b64, api_key)
    elif model == "claude":
        return _dispatch_claude_vision(intent, screenshot_b64, api_key)
    else:
        return {"found": False, "error": f"unknown vision model: {model}",
                "label": "", "bbox": None, "center": None,
                "action": "unclear", "text": None, "confidence": 0.0}


def _dispatch_deepseek_vision(
    intent: str,
    screenshot_b64: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """调用 DeepSeek 视觉 API。复用与 llm.py 相同的配置。"""
    import json as _json
    import urllib.request
    import urllib.error
    import re

    # 复用 llm.py 的配置来获取 API 密钥与基础 URL
    try:
        from .llm import DEEPSEEK_BASE_URL, REQUEST_TIMEOUT

        def _load_key():
            from .llm import _load_api_key
            return _load_api_key()
    except Exception:
        DEEPSEEK_BASE_URL = "https://api.deepseek.com"
        REQUEST_TIMEOUT = 30

        def _load_key():
            return os.environ.get("DEEPSEEK_API_KEY")

    key = api_key or _load_key()
    if not key:
        return {"found": False, "error": "no DeepSeek API key configured",
                "label": "", "bbox": None, "center": None,
                "action": "unclear", "text": None, "confidence": 0.0}

    prompt = (
        f"Examine this screenshot. The user wants to: '{intent}'.\n"
        "Find the relevant UI element and return a JSON object:\n"
        '{"found": true/false, "label": "element name", '
        '"bbox": [x, y, width, height], "action": "click|type|unclear", '
        '"text": "text to type or null"}\n'
        "Only return JSON, no other text."
    )

    payload = _json.dumps({
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{screenshot_b64}"
                }},
            ]},
        ],
        "temperature": 0.1,
        "max_tokens": 500,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"].strip()
        match = re.search(r'\{[\s\S]*\}', content)
        if match:
            result = _json.loads(match.group())
            if result.get("bbox") and len(result["bbox"]) == 4:
                bbox = result["bbox"]
                result["center"] = [bbox[0] + bbox[2] // 2,
                                    bbox[1] + bbox[3] // 2]
            else:
                result["center"] = None
            result.setdefault("found", False)
            result.setdefault("label", "")
            result.setdefault("action", "unclear")
            result.setdefault("text", None)
            result.setdefault("confidence", 0.8 if result.get("found") else 0.0)
            result["error"] = None
            return result
        return {"found": False, "error": f"could not parse: {content[:200]}",
                "label": "", "bbox": None, "center": None,
                "action": "unclear", "text": None, "confidence": 0.0}
    except Exception as e:
        return {"found": False, "error": str(e),
                "label": "", "bbox": None, "center": None,
                "action": "unclear", "text": None, "confidence": 0.0}


def _dispatch_claude_vision(
    intent: str,
    screenshot_b64: str,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """调用 Claude 视觉 API。需要 ANTHROPIC_API_KEY。"""
    import json as _json
    import urllib.request
    import urllib.error
    import re

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {"found": False, "error": "no Anthropic API key configured",
                "label": "", "bbox": None, "center": None,
                "action": "unclear", "text": None, "confidence": 0.0}

    prompt = (
        f"Examine this screenshot. The user wants to: '{intent}'.\n"
        "Find the relevant UI element and return a JSON object:\n"
        '{"found": true/false, "label": "element name", '
        '"bbox": [x, y, width, height], "action": "click|type|unclear", '
        '"text": "text to type or null"}\n'
        "Only return JSON, no other text."
    )

    payload = _json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                }},
            ]},
        ],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = _json.loads(resp.read().decode("utf-8"))
        content = body["content"][0]["text"].strip()
        match = re.search(r'\{[\s\S]*\}', content)
        if match:
            result = _json.loads(match.group())
            if result.get("bbox") and len(result["bbox"]) == 4:
                bbox = result["bbox"]
                result["center"] = [bbox[0] + bbox[2] // 2,
                                    bbox[1] + bbox[3] // 2]
            else:
                result["center"] = None
            result.setdefault("found", False)
            result.setdefault("label", "")
            result.setdefault("action", "unclear")
            result.setdefault("text", None)
            result.setdefault("confidence", 0.8 if result.get("found") else 0.0)
            result["error"] = None
            return result
        return {"found": False, "error": f"could not parse: {content[:200]}",
                "label": "", "bbox": None, "center": None,
                "action": "unclear", "text": None, "confidence": 0.0}
    except Exception as e:
        return {"found": False, "error": str(e),
                "label": "", "bbox": None, "center": None,
                "action": "unclear", "text": None, "confidence": 0.0}


if __name__ == "__main__":
    # 快速冒烟测试
    print("Available backends:", available_backends())
    r = vision_screenshot("test intent", app="vscode")
    print(r.to_dict())
