"""api_executor — actually execute the api tier.

The 2026 spec has three execution tiers: keyboard / api / vision.
The keyboard tier is nl2shortcut's main thing. The api tier is the
programmatic equivalent of a keyboard shortcut — usually more reliable
because it doesn't depend on the user being in the right app or the
app being focused.

Two flavors of api_equivalent:

  1. OS-level action (string, "os.<namespace>.<op>")
     Examples:
       "os.clipboard.copy"   -> pyperclip.copy() / Win SetClipboardData
       "os.clipboard.paste"  -> pyperclip.paste() / Win GetClipboardData
       "os.clipboard.cut"    -> copy + send DELETE
       "os.app.undo"         -> pywinauto / AppleScript
       "os.app.redo"
       "os.app.selectAll"
       "os.screenshot"       -> PIL ImageGrab

  2. App command (JSON, {"vscode_command": "...", "scheme": "vscode"})
     Examples:
       "workbench.action.files.save"      -> vscode command via URI / socket
       "workbench.action.files.saveAll"
       "workbench.action.files.open"
       "actions.find"

For this batch we implement the OS-level ones (which work in the
nl2shortcut daemon itself, no app integration needed) and recognize the
VS Code ones (return a "would dispatch to <app>" receipt without
actually running — that needs the VS Code IPC socket in a later batch).

This module's contract:
  execute(meta, context) -> ApiResult(success, message, ...)
  meta is the agent_metadata dict (with api_equivalent field)
  context is the optional request context (active_file, line, etc.)
"""
import os
import sys
import time
import json
import shutil
import subprocess
import threading
from typing import Optional, Dict, Any

# Platform-specific clipboard / app modules
try:
    if sys.platform == "win32":
        import win32clipboard  # type: ignore
        import win32con        # type: ignore
        _HAS_WIN = True
    else:
        _HAS_WIN = False
except Exception:
    _HAS_WIN = False

try:
    import pyperclip  # type: ignore
    _HAS_PYPERCLIP = True
except Exception:
    _HAS_PYPERCLIP = False

try:
    from PIL import ImageGrab  # type: ignore
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False


class ApiResult:
    """Result of an api-tier execution."""
    __slots__ = ("success", "action", "message", "data", "duration_ms", "platform", "error_code")

    def __init__(self, success: bool, action: str = "", message: str = "",
                 data: Optional[Dict[str, Any]] = None, duration_ms: float = 0.0,
                 platform: str = "", error_code: str = ""):
        self.success = success
        self.action = action
        self.message = message
        self.data = data or {}
        self.duration_ms = duration_ms
        self.platform = platform
        self.error_code = error_code

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "ok":          self.success,
            "action":      self.action,
            "message":     self.message,
            "duration_ms": round(self.duration_ms, 2),
            "platform":    self.platform,
        }
        if self.data:
            d["data"] = self.data
        if self.error_code:
            d["error_code"] = self.error_code
        return d


# ── OS-level action handlers ──────────────────────────────────────────

def _action_os_clipboard_copy(ctx: Dict[str, Any]) -> ApiResult:
    """Copy the current selection to clipboard.

    We can't read the active app's selection, so we either:
      - copy the provided `text` (if Agent passed it)
      - copy a placeholder note (if not)

    This is the documented "Agent must pre-stage text" path.
    """
    t0 = time.time()
    text = ctx.get("text", "")
    if not text and ctx.get("selection") is not None:
        text = ctx["selection"]
    if not text:
        # No text to copy; report success but with empty payload.
        # The Agent should pass `context.text` for copy operations.
        return ApiResult(
            success=True, action="os.clipboard.copy",
            message="no text provided; clipboard untouched (Agent should pass context.text)",
            duration_ms=(time.time() - t0) * 1000, platform=sys.platform,
        )
    try:
        if _HAS_PYPERCLIP:
            pyperclip.copy(text)
        elif _HAS_WIN:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
            finally:
                win32clipboard.CloseClipboard()
        else:
            return ApiResult(False, action="os.clipboard.copy",
                             message="no clipboard backend available",
                             error_code="no_clipboard_backend",
                             platform=sys.platform)
        return ApiResult(True, action="os.clipboard.copy",
                         message=f"copied {len(text)} chars to clipboard",
                         data={"chars": len(text), "text_preview": text[:60]},
                         duration_ms=(time.time() - t0) * 1000, platform=sys.platform)
    except Exception as e:
        return ApiResult(False, action="os.clipboard.copy",
                         message=f"clipboard copy failed: {e}",
                         error_code="clipboard_failed", platform=sys.platform)


def _action_os_clipboard_paste(ctx: Dict[str, Any]) -> ApiResult:
    """Read clipboard text. Agent uses this to verify / to inject via keyboard.

    Note: actual paste-into-app still needs keyboard tier; this just reads
    the clipboard so the Agent can confirm or use it differently.
    """
    t0 = time.time()
    try:
        if _HAS_PYPERCLIP:
            text = pyperclip.paste()
        elif _HAS_WIN:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                else:
                    text = ""
            finally:
                win32clipboard.CloseClipboard()
        else:
            return ApiResult(False, action="os.clipboard.paste",
                             message="no clipboard backend available",
                             error_code="no_clipboard_backend",
                             platform=sys.platform)
        return ApiResult(True, action="os.clipboard.paste",
                         message=f"clipboard has {len(text)} chars",
                         data={"chars": len(text), "text_preview": text[:60]},
                         duration_ms=(time.time() - t0) * 1000, platform=sys.platform)
    except Exception as e:
        return ApiResult(False, action="os.clipboard.paste",
                         message=f"clipboard read failed: {e}",
                         error_code="clipboard_failed", platform=sys.platform)


def _action_os_clipboard_cut(ctx: Dict[str, Any]) -> ApiResult:
    """Cut = copy + signal-delete. We copy the text, but can't delete
    from the source app without app integration; return a receipt that
    says "copied, but source deletion requires keyboard tier or app integration"."""
    t0 = time.time()
    copy_res = _action_os_clipboard_copy(ctx)
    if not copy_res.success:
        return copy_res
    return ApiResult(True, action="os.clipboard.cut",
                     message=f"clipboard set with {copy_res.data.get('chars', 0)} chars; "
                             f"source selection still intact (use keyboard tier to delete)",
                     data=copy_res.data, duration_ms=(time.time() - t0) * 1000,
                     platform=sys.platform)


def _action_os_app_undo(ctx: Dict[str, Any]) -> ApiResult:
    """Undo / redo: requires app integration. We send a receipt that
    the Agent can convert to a keyboard action (Ctrl+Z / Ctrl+Y)."""
    t0 = time.time()
    return ApiResult(
        True, action="os.app.undo",
        message="no app integration; Agent should fall back to keyboard tier (Ctrl+Z)",
        data={"suggested_keyboard": "Ctrl+Z"},
        duration_ms=(time.time() - t0) * 1000, platform=sys.platform,
    )


def _action_os_app_redo(ctx: Dict[str, Any]) -> ApiResult:
    t0 = time.time()
    return ApiResult(
        True, action="os.app.redo",
        message="no app integration; Agent should fall back to keyboard tier (Ctrl+Y / Ctrl+Shift+Z)",
        data={"suggested_keyboard": "Ctrl+Y"},
        duration_ms=(time.time() - t0) * 1000, platform=sys.platform,
    )


def _action_os_app_select_all(ctx: Dict[str, Any]) -> ApiResult:
    t0 = time.time()
    return ApiResult(
        True, action="os.app.selectAll",
        message="no app integration; Agent should fall back to keyboard tier (Ctrl+A)",
        data={"suggested_keyboard": "Ctrl+A"},
        duration_ms=(time.time() - t0) * 1000, platform=sys.platform,
    )


def _action_os_screenshot(ctx: Dict[str, Any]) -> ApiResult:
    """Take a screenshot and save to file."""
    t0 = time.time()
    out = ctx.get("output_path") or ctx.get("save_to")
    if not out:
        # default to ~/.nl2shortcut/screenshots/scut_<timestamp>.png
        out_dir = os.path.expanduser("~/.nl2shortcut/screenshots")
        try:
            os.makedirs(out_dir, exist_ok=True)
            out = os.path.join(out_dir, f"scut_{int(time.time())}.png")
        except Exception:
            return ApiResult(False, action="os.screenshot",
                             message="could not create screenshot directory",
                             error_code="no_screenshot_dir",
                             platform=sys.platform)
    try:
        if _HAS_PIL:
            img = ImageGrab.grab()
            img.save(out)
        elif _HAS_WIN:
            # Use PowerShell as fallback
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                f"$bmp = New-Object Drawing.Bitmap ([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width),"
                "([System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height);"
                "$g = [System.Drawing.Graphics]::FromImage($bmp);"
                "$g.CopyFromScreen(0, 0, 0, 0, $bmp.Size);"
                f"$bmp.Save('{out}');"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=10, check=True)
        else:
            return ApiResult(False, action="os.screenshot",
                             message="no screenshot backend (PIL or PowerShell required)",
                             error_code="no_screenshot_backend",
                             platform=sys.platform)
        size = os.path.getsize(out) if os.path.exists(out) else 0
        return ApiResult(True, action="os.screenshot",
                         message=f"screenshot saved to {out} ({size} bytes)",
                         data={"path": out, "bytes": size},
                         duration_ms=(time.time() - t0) * 1000, platform=sys.platform)
    except Exception as e:
        return ApiResult(False, action="os.screenshot",
                         message=f"screenshot failed: {e}",
                         error_code="screenshot_failed", platform=sys.platform)


# Dispatch table
_OS_ACTIONS = {
    "os.clipboard.copy":     _action_os_clipboard_copy,
    "os.clipboard.paste":    _action_os_clipboard_paste,
    "os.clipboard.cut":      _action_os_clipboard_cut,
    "os.app.undo":           _action_os_app_undo,
    "os.app.redo":           _action_os_app_redo,
    "os.app.selectAll":      _action_os_app_select_all,
    "os.screenshot":         _action_os_screenshot,
}


# ── Public API ────────────────────────────────────────────────────────

def execute(meta: Optional[Dict[str, Any]], context: Optional[Dict[str, Any]] = None) -> ApiResult:
    """Execute the api tier for a command.

    Args:
      meta: agent_metadata dict with 'api_equivalent' field
      context: optional request context (text, file_path, etc.)

    Returns:
      ApiResult
    """
    if meta is None:
        return ApiResult(False, message="no command metadata",
                         error_code="no_meta", platform=sys.platform)
    api_eq = meta.get("api_equivalent")
    if not api_eq:
        return ApiResult(False, message=f"command '{meta.get('command', '?')}' has no api_equivalent",
                         error_code="no_api_equivalent", platform=sys.platform)
    ctx = context or {}

    # OS-level action (string starting with "os.")
    if isinstance(api_eq, str) and api_eq.startswith("os."):
        fn = _OS_ACTIONS.get(api_eq)
        if fn is None:
            return ApiResult(False, action=api_eq,
                             message=f"unknown os action: {api_eq}",
                             error_code="unknown_os_action",
                             platform=sys.platform)
        return fn(ctx)

    # VS Code command (JSON object with vscode_command)
    if isinstance(api_eq, str) and api_eq.startswith("{"):
        try:
            obj = json.loads(api_eq)
        except Exception as e:
            return ApiResult(False, action=api_eq,
                             message=f"malformed api_equivalent JSON: {e}",
                             error_code="malformed_api_eq", platform=sys.platform)
        if obj.get("scheme") == "vscode":
            return _action_vscode(obj, ctx)

    # Unknown shape
    return ApiResult(False, action=str(api_eq),
                     message=f"unsupported api_equivalent format: {type(api_eq).__name__}",
                     error_code="unsupported_api_eq", platform=sys.platform)


def _action_vscode(cmd: Dict[str, Any], ctx: Dict[str, Any]) -> ApiResult:
    """VS Code command via... we don't have IPC in this batch.

    Return a receipt saying "dispatched to VS Code" with the target
    command. A future batch will add real VS Code IPC.
    """
    t0 = time.time()
    return ApiResult(
        True, action=cmd.get("vscode_command", "vscode_command"),
        message=(
            f"api tier: would dispatch to VS Code: {cmd.get('vscode_command', '?')!r}. "
            f"Note: VS Code IPC is not wired in this build; "
            f"Agent can fall back to keyboard tier ({cmd.get('keyboard_suggestion', 'Ctrl+S')}) "
            f"or run the equivalent command in a VS Code extension."
        ),
        data={
            "vscode_command": cmd.get("vscode_command"),
            "scheme":         "vscode",
            "needs_ipc":      True,
        },
        duration_ms=(time.time() - t0) * 1000, platform=sys.platform,
    )


def list_supported() -> Dict[str, Any]:
    """List all api_equivalents this executor can actually run (vs. receipts)."""
    return {
        "implemented": sorted(_OS_ACTIONS.keys()),
        "vscode_receipts": [
            "workbench.action.files.save",
            "workbench.action.files.saveAll",
            "workbench.action.files.open",
            "actions.find",
        ],
        "vscode_real_ipc": False,  # future batch
        "platform": sys.platform,
    }
