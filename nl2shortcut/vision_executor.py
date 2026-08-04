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

# ── 合规闸门 ──────────────────────────────────────────────────────────
# compliance_mode = False → 默认禁用视觉层（合规要求）
# compliance_mode = True  → 非合规场景启用（端侧纯视觉模型）
#
# 融合策略④"端侧纯视觉 → 合规兜底层"的落点。
# 所有 vision_*() 函数在 compliance_mode=False 时返回禁用结果。
# ───────────────────────────────────────────────────────────────────────

_compliance_mode: bool = False


def set_compliance_mode(enabled: bool) -> None:
    """设置合规模式。True 启用视觉层，False 禁用（默认）。"""
    global _compliance_mode
    _compliance_mode = enabled


def get_compliance_mode() -> bool:
    """查询当前合规模式。"""
    return _compliance_mode


def _check_compliance() -> Optional[VisionResult]:
    """合规闸门检查。禁用时返回拒绝结果，启用时返回 None（通过）。"""
    if not _compliance_mode:
        return VisionResult(
            ok=False,
            action="vision.blocked",
            tier="vision",
            platform=_detect_platform(),
            message=(
                "Vision tier is disabled by compliance policy. "
                "Set compliance_mode=True to enable vision fallback. "
                "This is a safety gate: vision-based automation (screenshot + visual model) "
                "poses compliance risks in regulated environments."
            ),
            duration_ms=0.0,
            error_code="compliance_disabled",
        )
    return None


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


def _get_active_window_bbox() -> Optional[Tuple[int, int, int, int]]:
    """获取前台窗口的边界框 (left, top, right, bottom)。

    Returns:
        (left, top, right, bottom) 或 None。
    """
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return (rect.left, rect.top, rect.right, rect.bottom)
        elif sys.platform == "darwin":
            # macOS: 通过 Quartz 获取
            try:
                import Quartz  # type: ignore
                win_info = Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionOnScreenOnly
                    | Quartz.kCGWindowListExcludeDesktopElements,
                    Quartz.kCGNullWindowID,
                )
                for win in win_info:
                    if win.get("kCGWindowLayer", 0) == 0:
                        bounds = win.get("kCGWindowBounds", {})
                        x = int(bounds.get("X", 0))
                        y = int(bounds.get("Y", 0))
                        w = int(bounds.get("Width", 0))
                        h = int(bounds.get("Height", 0))
                        return (x, y, x + w, y + h)
            except Exception:
                pass
        elif sys.platform.startswith("linux"):
            # Linux: xdotool
            import subprocess
            try:
                result = subprocess.run(
                    ["xdotool", "getactivewindow", "getwindowgeometry",
                     "--shell"],
                    capture_output=True, text=True, timeout=3,
                )
                lines = result.stdout.strip().split("\n")
                vals = {}
                for line in lines:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        vals[k] = int(v)
                x = vals.get("X", 0)
                y = vals.get("Y", 0)
                w = vals.get("WIDTH", 0)
                h = vals.get("HEIGHT", 0)
                return (x, y, x + w, y + h)
            except Exception:
                pass
    except Exception:
        pass
    return None


def _crop_image(png_bytes: bytes, region: Tuple[int, int, int, int]) -> Optional[bytes]:
    """裁剪 PNG 图像到指定区域 region=(left, top, right, bottom)。"""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes))
        cropped = img.crop(region)
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


def _detect_sensitive_regions(
    png_bytes: bytes, width: int, height: int,
) -> List[Tuple[int, int, int, int]]:
    """在截图中检测敏感区域（密码框、API Key 等），返回黑框区域列表。

    当前采用启发式规则（未来可接入 OCR/视觉模型做更高精度识别）：
      1. 底部 15% 区域标记为"可能存在敏感信息"（状态栏/输入区）
      2. 顶部 40 像素标记为"标题栏"（可能有个人姓名/账号）

    返回: [(x, y, w, h), ...] 需要擦除的矩形区域列表。
    """
    regions = []
    # 标题栏区域（top 40px）—— 窗口标题可能包含用户名/个人文件路径
    regions.append((0, 0, width, min(40, height)))
    # 底部输入区（bottom 15%）—— 可能有输入框中的密码/私密文字
    bottom_h = max(int(height * 0.15), 30)
    regions.append((0, height - bottom_h, width, bottom_h))
    return regions


def _redact_image(
    png_bytes: bytes,
    redaction_regions: List[Tuple[int, int, int, int]],
) -> bytes:
    """对截图指定区域进行黑色覆盖（安全擦除）。

    Args:
        png_bytes: 原始 PNG 字节。
        redaction_regions: [(x, y, w, h), ...] 需要黑色覆盖的区域。

    Returns:
        擦除后的 PNG 字节。若 PIL 不可用则返回原始字节。
    """
    if not redaction_regions:
        return png_bytes
    try:
        from PIL import Image, ImageDraw
        img = Image.open(io.BytesIO(png_bytes))
        draw = ImageDraw.Draw(img)
        for rx, ry, rw, rh in redaction_regions:
            draw.rectangle([rx, ry, rx + rw, ry + rh], fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return png_bytes


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
    """按顺序尝试多个截图后端。支持 sub-region 裁剪。返回 (png_bytes, method_used)。"""
    # 1) PIL.ImageGrab（Windows 上的首选）
    png = _try_pil_screenshot()
    if png:
        if region:
            png = _crop_image(png, region)
        return png, "PIL.ImageGrab"
    # 2) mss（跨平台、速度快）
    png = _try_mss_screenshot()
    if png:
        if region:
            png = _crop_image(png, region)
        return png, "mss"
    # 3) pyautogui（较慢的兜底方案）
    png = _try_pyautogui_screenshot()
    if png:
        if region:
            png = _crop_image(png, region)
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

    compliance_mode=False 时返回合规禁用结果（融合策略④）。
    """
    # ── 合规闸门 ──
    blocked = _check_compliance()
    if blocked is not None:
        return blocked

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

    compliance_mode=False 时返回禁用结果。
    """
    # ── 合规闸门 ──
    if not _compliance_mode:
        return {
            "found": False,
            "error": "Vision dispatch is disabled by compliance policy.",
            "label": "", "bbox": None, "center": None,
            "action": "unclear", "text": None, "confidence": 0.0,
        }

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


# ── 增强视觉兜底：主动窗口 + 安全擦除 ──────────────────────────────────


def capture_active_window(
    intent: str = "",
    app: str = "",
    encode_b64: bool = True,
    redact: bool = True,
) -> VisionResult:
    """截图仅包含活动窗口区域，并可选择性地擦除敏感区域。

    融合策略④的实现：局部截图 + 安全擦除。
    相比全屏截图，活动窗口截图体积更小且避免泄露其他窗口内容。
    """
    # ── 合规闸门 ──
    blocked = _check_compliance()
    if blocked is not None:
        return blocked

    t0 = time.time()
    plat = _detect_platform()
    if plat not in ("windows", "macos", "linux"):
        return VisionResult(
            ok=False, action="vision.active_window", platform=plat,
            message=f"platform '{plat}' is not supported",
            duration_ms=(time.time() - t0) * 1000,
            error_code="platform_unsupported",
        )

    # 获取活动窗口边界框 → 截图 + 裁剪
    bbox = _get_active_window_bbox()
    if bbox is None:
        # 降级：全屏截图
        png_bytes, method = _capture_screenshot()
        capture_region = "full_screen"
    else:
        png_bytes, method = _capture_screenshot(region=bbox)
        capture_region = f"active_window:{bbox}"

    if not png_bytes:
        return VisionResult(
            ok=False, action="vision.active_window", platform=plat,
            message="no screenshot backend available",
            duration_ms=(time.time() - t0) * 1000,
            error_code="no_image_lib",
        )

    # 安全擦除
    width, height = 0, 0
    try:
        if png_bytes[:8] == b"\x89PNG\r\n\x1a\n" and len(png_bytes) > 24:
            width = int.from_bytes(png_bytes[16:20], "big")
            height = int.from_bytes(png_bytes[20:24], "big")
    except Exception:
        pass

    if redact and width > 0 and height > 0:
        sensitive = _detect_sensitive_regions(png_bytes, width, height)
        if sensitive:
            png_bytes = _redact_image(png_bytes, sensitive)
            method += "+redacted"

    data: Dict[str, Any] = {
        "format": "png",
        "width": width,
        "height": height,
        "capture_region": capture_region,
        "capture_method": method,
        "size_bytes": len(png_bytes),
        "redacted": redact,
        "hint": _build_vision_hint(intent, app),
        "fallback_suggested": "vision.click OR escalate to human",
    }

    if encode_b64:
        data["image_b64"] = base64.b64encode(png_bytes).decode("ascii")
    else:
        data["image_path"] = ""

    return VisionResult(
        ok=True,
        action="vision.active_window",
        platform=plat,
        message=(
            f"captured active window {width}x{height} ({len(png_bytes)} bytes) via {method}. "
            + ("Sensitive regions redacted." if redact else "")
        ),
        duration_ms=(time.time() - t0) * 1000,
        data=data,
        error_code="ok",
    )


def capture_region(
    x: int, y: int, w: int, h: int,
    intent: str = "",
    app: str = "",
    encode_b64: bool = True,
    redact: bool = False,
) -> VisionResult:
    """截图指定坐标区域 (x, y, w, h)。

    用于精确 UI 元素定位场景，例如找到某个按钮的 bounding box 后截取。
    """
    blocked = _check_compliance()
    if blocked is not None:
        return blocked

    t0 = time.time()
    plat = _detect_platform()

    bbox = (x, y, x + w, y + h)
    png_bytes, method = _capture_screenshot(region=bbox)

    if not png_bytes:
        return VisionResult(
            ok=False, action="vision.region", platform=plat,
            message="no screenshot backend available",
            duration_ms=(time.time() - t0) * 1000,
            error_code="no_image_lib",
        )

    if redact and w > 0 and h > 0:
        sensitive = _detect_sensitive_regions(png_bytes, w, h)
        if sensitive:
            png_bytes = _redact_image(png_bytes, sensitive)
            method += "+redacted"

    data: Dict[str, Any] = {
        "format": "png",
        "width": w,
        "height": h,
        "capture_region": f"{x},{y},{w},{h}",
        "capture_method": method,
        "size_bytes": len(png_bytes),
        "redacted": redact,
        "hint": _build_vision_hint(intent, app),
        "fallback_suggested": "vision.click OR escalate to human",
    }

    if encode_b64:
        data["image_b64"] = base64.b64encode(png_bytes).decode("ascii")

    return VisionResult(
        ok=True,
        action="vision.region",
        platform=plat,
        message=f"captured region {w}x{h} at ({x},{y}) via {method}",
        duration_ms=(time.time() - t0) * 1000,
        data=data,
        error_code="ok",
    )


def safe_screenshot(
    intent: str = "",
    app: str = "",
    redact: bool = True,
    encode_b64: bool = True,
) -> VisionResult:
    """安全截图 —— 全屏 + 自动敏感区域擦除。

    这是对合规场景下 vision_screenshot() 的安全增强版。
    确保敏感信息（密码框、API Key、个人信息）在 base64 编码前已被黑色覆盖。
    """
    result = vision_screenshot(intent=intent, app=app, encode_b64=False)
    if not result.ok:
        return result

    if redact:
        w = result.data.get("width", 0)
        h = result.data.get("height", 0)
        if w > 0 and h > 0:
            # 解码 base64 → 擦除 → 重新编码
            raw_b64 = result.data.get("image_b64", "")
            if raw_b64:
                try:
                    raw_bytes = base64.b64decode(raw_b64)
                    sensitive = _detect_sensitive_regions(raw_bytes, w, h)
                    if sensitive:
                        raw_bytes = _redact_image(raw_bytes, sensitive)
                        result.data["image_b64"] = (
                            base64.b64encode(raw_bytes).decode("ascii") if encode_b64 else ""
                        )
                        result.data["redacted"] = True
                        result.data["capture_method"] += "+redacted"
                        result.message += " Sensitive regions redacted."
                except Exception:
                    pass

    return result


def redact_text_from_screenshot(
    png_bytes: bytes,
    text_regions: List[Tuple[int, int, int, int]],
) -> bytes:
    """对截图中的指定文字区域做黑色覆盖擦除。

    Args:
        png_bytes: 原始 PNG 字节
        text_regions: [(x, y, w, h), ...] 需要擦除的矩形列表

    Returns:
        擦除后的 PNG 字节
    """
    return _redact_image(png_bytes, text_regions)


if __name__ == "__main__":
    # 快速冒烟测试
    print("Available backends:", available_backends())
    r = vision_screenshot("test intent", app="vscode")
    print("Full screen:", r.ok, f"{r.data.get('width', 0)}x{r.data.get('height', 0)}")
    r2 = capture_active_window("test intent", app="vscode")
    print("Active window:", r2.ok, f"{r2.data.get('width', 0)}x{r2.data.get('height', 0)}")
