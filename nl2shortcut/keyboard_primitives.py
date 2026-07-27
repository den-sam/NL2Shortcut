"""keyboard_primitives.py —— 通用键盘操作原语模块。

架构
────────────
本模块提供可组合的键盘原语，作为数据库中无硬编码快捷键时的兜底层。

执行优先级（由轻到重）：
    1. Tab / 方向键导航          （无需上下文）
    2. Alt+字母 菜单访问         （Windows/macOS 标准约定）
    3. PyAutoGUI / 适配器热键     （经由 adapter.py）
    4. Shell 运行（Win+R → 命令） （最后手段）

每个原语都是一个具名函数，满足：
  - 返回类似 ExecutionResult 的字典，含 'success'、'description'、'error'
  - 遵循可配置的等待时间，确保下一步前焦点已稳定
  - 可观测：调用方始终能知道发生了什么

与 adapter.py 的共存关系
──────────────────────────────
adapter.py 负责底层热键派发（send_keys / type_text）。本模块将这些
原语封装为更高层级、可重新组合的动作。当 adapter.py 已覆盖某行为
（如 Ctrl+C）时，对应原语直接调用适配器 —— 不做重复实现。
"""

from __future__ import annotations

import time
import subprocess
import sys
import re
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field

from .models import Platform

# ── 延迟导入适配器（避免导入时即依赖 pyautogui） ──

_adapters: Dict[str, Any] = {}

def _get_adapter():
    """延迟创建并缓存当前最优可用的 KeyboardAdapter。"""
    if not _adapters:
        try:
            from .adapter import create_adapter
            _adapters["instance"] = create_adapter()
        except Exception as exc:
            raise RuntimeError(
                "keyboard_primitives 需要一个可用的 KeyboardAdapter。"
                "请确认已安装 pyautogui：pip install pyautogui"
            ) from exc
    return _adapters["instance"]


# ── 默认计时常量（毫秒） ───────────────────────────────────────────────

DEFAULT_WAIT_MS: int   = 150   # 连续按键之间的稳定等待时间
DEFAULT_STABLE_MS: int = 300   # 发送下一段序列前的额外等待
DEFAULT_TYPE_INTERVAL: float = 0.01  # 每个字符之间的间隔（秒）
DEFAULT_SEARCH_WAIT_MS: int = 800   # 搜索触发（Enter）后默认的等待毫秒数

# ── 结果数据类 ───────────────────────────────────────────────────────────

@dataclass
class PrimitiveResult:
    """单次原语操作的结果。

    Attributes
    ----------
    success : bool
        为 True 表示动作执行过程中未抛出异常。
    description : str
        对本次尝试所执行操作的可读描述。
    action_name : str
        被调用的原语函数名称。
    error : Optional[str]
        当 success 为 False 时的错误信息。
    elapsed_ms : float
        动作消耗的墙钟时间（不含稳定等待时间）。
    """
    success: bool
    description: str
    action_name: str
    error: Optional[str] = None
    elapsed_ms: float = 0.0

    def __repr__(self) -> str:
        icon = "\u2705" if self.success else "\u274c"
        base = f"{icon} {self.action_name}: {self.description}"
        if self.error:
            base += f"  [ERROR: {self.error}]"
        return base


# ─────────────────────────────────────────────────────────────────────────────
# 内部辅助函数
# ─────────────────────────────────────────────────────────────────────────────

def _press(key: str, wait_ms: int = DEFAULT_WAIT_MS) -> PrimitiveResult:
    """经由适配器发送单次按键。"""
    start = time.perf_counter()
    try:
        adapter = _get_adapter()
        adapter.send_keys(key)
        elapsed = (time.perf_counter() - start) * 1000
        return PrimitiveResult(
            success=True,
            description=f"Press {key!r}",
            action_name="_press",
            elapsed_ms=elapsed,
        )
    except Exception as exc:  # pragma: no cover — real errors logged
        return PrimitiveResult(
            success=False,
            description=f"Press {key!r}",
            action_name="_press",
            error=str(exc),
            elapsed_ms=(time.perf_counter() - start) * 1000,
        )


def _settle(wait_ms: int) -> None:
    """等待焦点稳定后再执行下一个动作。"""
    time.sleep(wait_ms / 1000)


# ─────────────────────────────────────────────────────────────────────────────
# 键盘链执行器（含确定性等待步）
# ─────────────────────────────────────────────────────────────────────────────

def _coerce_ms(value: Any) -> int:
    """将任意值安全地转为非负毫秒整数。"""
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def _substitute(text: str, variables: Optional[Dict[str, Any]]) -> str:
    """将字符串中的 ``{name}`` 占位符替换为变量表中的值。"""
    if not variables:
        return text
    for name, val in variables.items():
        text = text.replace("{" + name + "}", str(val))
    return text


def _send_keys_step(key: str, dry_run: bool, idx: int) -> Dict[str, Any]:
    """经由适配器发送一次按键组合，返回统一格式的步骤结果。"""
    if dry_run:
        return {"index": idx, "kind": "key", "success": True,
                "description": f"send_keys {key!r} (dry_run)",
                "error": None, "elapsed_ms": 0.0}
    start = time.perf_counter()
    try:
        adapter = _get_adapter()
        adapter.send_keys(key)
        return {"index": idx, "kind": "key", "success": True,
                "description": f"send_keys {key!r}", "error": None,
                "elapsed_ms": (time.perf_counter() - start) * 1000}
    except Exception as exc:
        return {"index": idx, "kind": "key", "success": False,
                "description": f"send_keys {key!r}", "error": str(exc),
                "elapsed_ms": (time.perf_counter() - start) * 1000}


def _send_text_step(text: str, dry_run: bool, type_interval: float, idx: int) -> Dict[str, Any]:
    """经由适配器逐字符输入文本，返回统一格式的步骤结果。"""
    if dry_run:
        return {"index": idx, "kind": "type", "success": True,
                "description": f"type_text {text!r} (dry_run)",
                "error": None, "elapsed_ms": 0.0}
    start = time.perf_counter()
    try:
        adapter = _get_adapter()
        adapter.type_text(text, interval=type_interval)
        return {"index": idx, "kind": "type", "success": True,
                "description": f"type_text {text!r}", "error": None,
                "elapsed_ms": (time.perf_counter() - start) * 1000}
    except Exception as exc:
        return {"index": idx, "kind": "type", "success": False,
                "description": f"type_text {text!r}", "error": str(exc),
                "elapsed_ms": (time.perf_counter() - start) * 1000}


def _search_wait_value() -> int:
    """返回搜索触发后的默认等待毫秒数。

    优先复用 composites 中 ``get_load_wait("search_index")`` 的运行时值
    （含 ``set_load_waits`` / 环境变量覆盖），拿不到时回落到本地
    ``DEFAULT_SEARCH_WAIT_MS``（800ms）。
    """
    try:
        from .composites import get_load_wait
        return get_load_wait("search_index")
    except Exception:
        return DEFAULT_SEARCH_WAIT_MS


def _looks_like_sleep(raw: Any) -> bool:
    """判断某步是否已是显式 ``["sleep", ms]``，用于避免自动等待与之叠加。"""
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return str(raw[0]).lower() == "sleep"
    return False


def run_keyboard_chain(
    steps: List[Any],
    variables: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    type_interval: Optional[float] = None,
    auto_search_wait: bool = True,
    search_wait_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """按序执行一组键盘链步骤，步骤之间可插入确定性等待。

    用于解决「目标应用（如资源管理器）仍在加载、UI 尚未就绪时，
    后续按键因执行过快而作用于错误状态」的竞态问题。典型场景：
    选中文件、切换目录、触发搜索后，UI 需要时间刷新，若不加等待
    就紧接着发 ``Ctrl+A`` 或调出右键菜单，往往会落空（未真正选中
    文件）。在关键节点插入 ``["sleep", ms]`` 步即可消除该竞态。

    步类型（列表中每个元素）
    ------------------------------
    - 字符串（或单元素列表，如 ``["Alt+D"]``）
          视作按键组合或要输入的文本，先经 ``{变量}`` 模板替换，
          再经适配器发送。
    - ``["sleep", ms]``
          暂停 ``ms`` 毫秒，等待 UI 稳定（本执行器新增的核心能力）。
          例如 ``["sleep", 300]`` 让目录刷新完成后再继续。
    - ``["type", text]``
          逐字符输入 ``text``（同样支持 ``{变量}`` 替换）。

    自动搜索等待（auto_search_wait）
    --------------------------------
    设 ``auto_search_wait=True`` 时，执行器会跟踪「搜索上下文」：一旦出现
    ``Ctrl+E`` / ``Ctrl+F`` 聚焦搜索框，随后在搜索框上下文内的 ``Enter``
    即被识别为**搜索触发**。该 ``Enter`` 执行后，若下一步并非显式
    ``["sleep", ms]``，则自动补一段等待（默认 800ms，见 ``search_index``
    加载延迟），从而**无论调用方是否记得写 sleep，搜索后都保证有间隔**，
    且不会与已有的显式 sleep 叠加成双倍。

    参数
    ----
    steps : list
        步骤列表，详见上方步类型说明。
    variables : dict, optional
        用于 ``{name}`` 替换的变量表，例如
        ``{"source_folder": "D:\\\\Downloads"}``。
    dry_run : bool
        为 True 时只记录将要执行的步骤，不真正发送按键、也不真正休眠。
    type_interval : float, optional
        输入文本时字符之间的间隔秒数，默认沿用模块常量 ``DEFAULT_TYPE_INTERVAL``。
    auto_search_wait : bool, optional
        为 True（默认）时启用「搜索触发后自动补间隔」逻辑（详见上文）。
        设为 False 可关闭该行为（完全依赖调用方显式写的 ``["sleep", ...]``）。
    search_wait_ms : int, optional
        自动搜索等待的毫秒数；省略时复用 composites 的 ``search_index``
        加载延迟（含运行时覆盖），再不行回落到 800ms。

    返回
    ----
    list[dict]
        每个步骤一条结果记录，字段含 ``index`` / ``kind`` / ``success`` /
        ``description`` / ``error`` / ``elapsed_ms``。

    示例
    ----
    >>> run_keyboard_chain([
    ...     ["Alt+D"], ["{source_folder}"], ["Enter"],
    ...     ["Ctrl+E"], ["{pattern}"], ["Enter"],   # 自动补 800ms
    ...     ["Ctrl+A"],
    ... ], variables={"source_folder": "D:\\Downloads", "pattern": "report"},
    ...    auto_search_wait=True)
    """
    iv = type_interval if type_interval is not None else DEFAULT_TYPE_INTERVAL
    # 自动搜索等待的生效间隔：显式覆盖 > composites 的 search_index > 本地 800ms
    effective_search_wait = (
        max(0, int(search_wait_ms)) if search_wait_ms is not None
        else _search_wait_value()
    )
    results: List[Dict[str, Any]] = []
    _search_focus_seen = False     # 当前是否处于搜索框上下文（已聚焦 Ctrl+E/F）
    _pending_search_wait = False   # 上一步 Enter 是否为搜索触发
    for idx, raw in enumerate(steps or []):
        # 归一化：单元素列表视为其首个字符串
        if isinstance(raw, (list, tuple)):
            if len(raw) == 2 and str(raw[0]).lower() == "sleep":
                ms = _coerce_ms(raw[1])
                if dry_run:
                    results.append({"index": idx, "kind": "sleep", "success": True,
                                    "description": f"sleep {ms}ms (dry_run)",
                                    "error": None, "elapsed_ms": 0.0})
                else:
                    start = time.perf_counter()
                    time.sleep(ms / 1000.0)
                    results.append({"index": idx, "kind": "sleep", "success": True,
                                    "description": f"sleep {ms}ms", "error": None,
                                    "elapsed_ms": (time.perf_counter() - start) * 1000})
                # 显式 sleep 会消费掉待定的搜索等待，避免叠加成双倍
                _pending_search_wait = False
                continue
            if len(raw) == 2 and str(raw[0]).lower() == "type":
                text = _substitute(str(raw[1]), variables)
                results.append(_send_text_step(text, dry_run, iv, idx))
                continue
            # 其它列表：取首个元素作为按键串
            item = raw[0] if raw else ""
        else:
            item = str(raw)

        key = _substitute(item, variables)
        norm = key.strip().lower()

        # 搜索上下文跟踪
        if auto_search_wait:
            if norm in ("ctrl+e", "ctrl+f"):
                _search_focus_seen = True
            elif norm in ("alt+d", "ctrl+l", "f4", "escape"):
                # 地址栏聚焦 / 取消等导航操作会脱离搜索上下文
                _search_focus_seen = False
            elif norm == "enter" and _search_focus_seen:
                # 搜索框上下文内的 Enter 即「搜索触发」
                _pending_search_wait = True
                _search_focus_seen = False

        results.append(_send_keys_step(key, dry_run, idx))

        # 搜索触发后自动补间隔（仅当下一步不是显式 sleep 时）
        if auto_search_wait and _pending_search_wait:
            _pending_search_wait = False
            nxt = steps[idx + 1] if idx + 1 < len(steps) else None
            if not _looks_like_sleep(nxt):
                if dry_run:
                    results.append({"index": idx, "kind": "sleep", "success": True,
                                    "description": f"auto search wait {effective_search_wait}ms (dry_run)",
                                    "error": None, "elapsed_ms": 0.0})
                else:
                    start = time.perf_counter()
                    time.sleep(effective_search_wait / 1000.0)
                    results.append({"index": idx, "kind": "sleep", "success": True,
                                    "description": f"auto search wait {effective_search_wait}ms",
                                    "error": None,
                                    "elapsed_ms": (time.perf_counter() - start) * 1000})
    return results


# ─────────────────────────────────────────────────────────────────────────────
# KeyboardPrimitives —— 主要对外公开的类
# ─────────────────────────────────────────────────────────────────────────────

class KeyboardPrimitives:
    """NL2Shortcut 的可组合键盘动作原语。

    当没有可用的硬编码快捷键时，可将本类作为兜底规划器使用。每个
    方法都返回 ``PrimitiveResult``，便于调用方决定是否继续，或回退到
    更重的机制（Shell）。

    示例
    -------
    >>> kb = KeyboardPrimitives()
    >>> kb.tab(3)         # 按 Tab 3 次以遍历焦点
    >>> kb.alt_letter("F")  # Alt+F → 打开「文件」菜单
    >>> kb.navigate_to_menu("文件→另存为")
    """

    def __init__(
        self,
        wait_ms: int = DEFAULT_WAIT_MS,
        stable_ms: int = DEFAULT_STABLE_MS,
        type_interval: float = DEFAULT_TYPE_INTERVAL,
        platform: Optional[Platform] = None,
    ) -> None:
        """
        Parameters
        ----------
        wait_ms : int
            多按序列（如 ``tab(3)``）中连续按键之间的等待毫秒数。
        stable_ms : int
            序列完成后额外等待的毫秒数，以便下一段序列开始前 UI 焦点
            能够稳定下来。
        type_interval : float
            ``type_text`` 中每个字符之间暂停的秒数。
        platform : Platform, optional
            覆盖自动检测到的平台。
        """
        self.wait_ms = wait_ms
        self.stable_ms = stable_ms
        self.type_interval = type_interval
        self._platform = platform or Platform.detect()
        self._adapter_initialised = False

    # ── 平台辅助 ────────────────────────────────────────────────────

    @property
    def platform(self) -> Platform:
        return self._platform

    def _key(self, key: str) -> str:
        """将归一化后的按键名称映射为适配器的热键字符串。"""
        return key.lower().strip()

    # ── 导航原语 ────────────────────────────────────────────────

    def tab(self, n: int = 1) -> PrimitiveResult:
        """按下 **Tab** ``n`` 次以向前遍历焦点。

        Parameters
        ----------
        n : int
            Tab 键按下的次数，必须 >= 1。

        Returns
        -------
        PrimitiveResult
        """
        n = max(1, int(n))
        start = time.perf_counter()
        try:
            adapter = _get_adapter()
            for i in range(n):
                adapter.send_keys("tab")
                if i < n - 1:
                    _settle(self.wait_ms)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Tab × {n}",
                action_name="tab",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"Tab × {n}",
                action_name="tab",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def shift_tab(self, n: int = 1) -> PrimitiveResult:
        """按下 **Shift+Tab** ``n`` 次以向后遍历焦点。

        Parameters
        ----------
        n : int
            Shift+Tab 键按下的次数，必须 >= 1。

        Returns
        -------
        PrimitiveResult
        """
        n = max(1, int(n))
        start = time.perf_counter()
        try:
            adapter = _get_adapter()
            for i in range(n):
                adapter.send_keys("shift+tab")
                if i < n - 1:
                    _settle(self.wait_ms)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Shift+Tab × {n}",
                action_name="shift_tab",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"Shift+Tab × {n}",
                action_name="shift_tab",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def arrow(self, direction: str, n: int = 1) -> PrimitiveResult:
        """按下 **方向键** ``n`` 次。

        Parameters
        ----------
        direction : str
            取值为 ``"up"``、``"down"``、``"left"``、``"right"`` 之一。
        n : int
            按键次数，必须 >= 1。

        Returns
        -------
        PrimitiveResult
        """
        valid = {"up", "down", "left", "right"}
        direction = direction.lower().strip()
        if direction not in valid:
            return PrimitiveResult(
                success=False,
                description=f"arrow({direction!r}, {n})",
                action_name="arrow",
                error=f"Invalid direction {direction!r}; must be one of {valid}",
            )
        n = max(1, int(n))
        start = time.perf_counter()
        try:
            adapter = _get_adapter()
            for i in range(n):
                adapter.send_keys(direction)
                if i < n - 1:
                    _settle(self.wait_ms)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Arrow {direction} × {n}",
                action_name="arrow",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"arrow({direction!r}, {n})",
                action_name="arrow",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def enter(self) -> PrimitiveResult:
        """按下 **Enter** 以确认 / 激活当前获得焦点的元素。"""
        start = time.perf_counter()
        try:
            _get_adapter().send_keys("enter")
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description="Enter",
                action_name="enter",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="Enter",
                action_name="enter",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def escape(self) -> PrimitiveResult:
        """按下 **Escape** 以取消 / 关闭当前活动对话框。"""
        start = time.perf_counter()
        try:
            _get_adapter().send_keys("esc")
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description="Escape",
                action_name="escape",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="Escape",
                action_name="escape",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def home(self) -> PrimitiveResult:
        """按下 **Home** —— 移动到行首 / 列表开头。"""
        start = time.perf_counter()
        try:
            _get_adapter().send_keys("home")
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description="Home",
                action_name="home",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="Home",
                action_name="home",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def end(self) -> PrimitiveResult:
        """按下 **End** —— 移动到行尾 / 列表末尾。"""
        start = time.perf_counter()
        try:
            _get_adapter().send_keys("end")
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description="End",
                action_name="end",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="End",
                action_name="end",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def page_up(self) -> PrimitiveResult:
        """按下 **Page Up**（向上翻页）。"""
        start = time.perf_counter()
        try:
            _get_adapter().send_keys("pageup")
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description="PageUp",
                action_name="page_up",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="PageUp",
                action_name="page_up",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def page_down(self) -> PrimitiveResult:
        """按下 **Page Down**（向下翻页）。"""
        start = time.perf_counter()
        try:
            _get_adapter().send_keys("pagedown")
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description="PageDown",
                action_name="page_down",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="PageDown",
                action_name="page_down",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    # ── 菜单访问原语 ──────────────────────────────────────────────

    def alt_letter(self, letter: str) -> PrimitiveResult:
        """按下 **Alt+``letter``** 以打开顶层菜单。

        常见的 Windows / KDE / GTK 约定::

            F  → 文件菜单       E  → 编辑菜单
            V  → 视图菜单        H  → 帮助菜单
            S  → 保存（已处于文件菜单时）
            N  → 新建（已处于文件菜单时）

        在 macOS 上，该方式通常会激活等价的功能菜单栏。

        Parameters
        ----------
        letter : str
            单个字符；不区分大小写。

        Returns
        -------
        PrimitiveResult
        """
        letter = letter.strip()
        if not letter:
            return PrimitiveResult(
                success=False,
                description=f"alt_letter({letter!r})",
                action_name="alt_letter",
                error="letter must be a non-empty single character",
            )
        key = f"alt+{letter}"
        start = time.perf_counter()
        try:
            _get_adapter().send_keys(key)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Alt+{letter.upper()}",
                action_name="alt_letter",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"alt_letter({letter!r})",
                action_name="alt_letter",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def alt_arrow(self, direction: str) -> PrimitiveResult:
        """按下 **Alt+方向键** —— 在菜单栏的各个子菜单之间导航。

        Parameters
        ----------
        direction : str
            取值为 ``"up"``、``"down"``、``"left"``、``"right"`` 之一。

        Returns
        -------
        PrimitiveResult
        """
        valid = {"up", "down", "left", "right"}
        direction = direction.lower().strip()
        if direction not in valid:
            return PrimitiveResult(
                success=False,
                description=f"alt_arrow({direction!r})",
                action_name="alt_arrow",
                error=f"Invalid direction {direction!r}",
            )
        key = f"alt+{direction}"
        start = time.perf_counter()
        try:
            _get_adapter().send_keys(key)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Alt+{direction.title()}",
                action_name="alt_arrow",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"alt_arrow({direction!r})",
                action_name="alt_arrow",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def menu_sequence(self, seq: str) -> PrimitiveResult:
        """执行以字符串形式给出的 **菜单栏序列**。

        字母会被依次发送；调用方负责插入分隔符（``→``）以提升可读性。

        Parameters
        ----------
        seq : str
            字符序列。分隔符 ``→``（U+2192）会被忽略，以便诸如
            ``"文件→另存为"`` 这样的自然语言描述可以直接传入。
            空格会被剔除。

            示例：``"F→S"`` 或 ``"FS"`` 或 ``"Alt+F, S"``

        Returns
        -------
        PrimitiveResult
            将整个序列作为单个步骤上报。
        """
        # 剔除分隔符与空白字符，仅保留字母数字字符
        clean = re.sub(r"[^a-zA-Z0-9]", "", seq)
        if not clean:
            return PrimitiveResult(
                success=False,
                description=f"menu_sequence({seq!r})",
                action_name="menu_sequence",
                error="Sequence contains no valid characters",
            )
        start = time.perf_counter()
        errors: List[str] = []
        try:
            adapter = _get_adapter()
            for i, ch in enumerate(clean):
                adapter.send_keys("alt+" + ch.lower())
                if i < len(clean) - 1:
                    _settle(self.wait_ms * 2)   # menu sub-items need a bit more time
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Menu sequence: {seq!r} → Alt+{'+Alt+'.join(clean.upper())}",
                action_name="menu_sequence",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            errors.append(str(exc))
            return PrimitiveResult(
                success=False,
                description=f"menu_sequence({seq!r})",
                action_name="menu_sequence",
                error="; ".join(errors),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    # ── 文本 / 热键原语 ───────────────────────────────────────────

    def type_text(self, text: str) -> PrimitiveResult:
        """逐字符地经由适配器输入一段字符串。

        Parameters
        ----------
        text : str
            要输入的字符串。特殊字符（如 ``{enter}``）会原样发送；
            对于 PyAutoGUI 风格的关键字面值，请用 ``{}`` 包裹。

        Returns
        -------
        PrimitiveResult
        """
        if not text:
            return PrimitiveResult(
                success=True,
                description="type_text('') — no-op",
                action_name="type_text",
            )
        start = time.perf_counter()
        try:
            adapter = _get_adapter()
            adapter.type_text(text, interval=self.type_interval)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"type_text({len(text)} chars)",
                action_name="type_text",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"type_text({text[:20]!r}...)",
                action_name="type_text",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def hotkey_combo(self, *keys: str) -> PrimitiveResult:
        """发送一组 **热键组合**（如 ``"ctrl"``、``"c"``）。

        这是对适配器 ``send_keys`` 的一层薄封装，但会返回
        ``PrimitiveResult``，以保证可观测性的一致性。

        Parameters
        ----------
        *keys : str
            以 ``+`` 连接的按键名，或包含 ``+`` 的单个字符串。
            示例::

                kb.hotkey_combo("ctrl", "c")
                kb.hotkey_combo("ctrl+shift+esc")

        Returns
        -------
        PrimitiveResult
        """
        if not keys:
            return PrimitiveResult(
                success=False,
                description="hotkey_combo() called with no keys",
                action_name="hotkey_combo",
                error="At least one key must be provided",
            )
        # 接受 ("ctrl","c") 或 ("ctrl+c",) 两种写法 —— 统一归一化为后者
        combo = "+".join(k.lower().strip() for k in keys)
        start = time.perf_counter()
        try:
            _get_adapter().send_keys(combo)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Hotkey: {combo.upper()}",
                action_name="hotkey_combo",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"hotkey_combo({combo!r})",
                action_name="hotkey_combo",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    # ── Shell 原语 ────────────────────────────────────────────────────

    def shell_run(self, command: str) -> PrimitiveResult:
        """通过 **Win+R**（Windows）/ Spotlight（macOS）/ dmenu（Linux）运行命令。

        在 Windows 上，该序列为::

            Win+R  →  输入命令  →  Enter

        Parameters
        ----------
        command : str
            要执行的命令或程序名称。

        Returns
        -------
        PrimitiveResult
        """
        if not command.strip():
            return PrimitiveResult(
                success=False,
                description="shell_run('')",
                action_name="shell_run",
                error="command must be non-empty",
            )
        start = time.perf_counter()
        errors: List[str] = []
        try:
            if self._platform == Platform.WINDOWS:
                # 1. 打开「运行」对话框
                _get_adapter().send_keys("win+r")
                _settle(400)
                # 2. 输入命令
                _get_adapter().type_text(command, interval=0.01)
                _settle(100)
                # 3. 执行
                _get_adapter().send_keys("enter")
                _settle(self.stable_ms)
                desc = f"Shell run: Win+R → {command!r} → Enter"
            elif self._platform == Platform.MACOS:
                _get_adapter().send_keys("command+space")  # Spotlight 聚焦搜索
                _settle(400)
                _get_adapter().type_text(command, interval=0.01)
                _settle(100)
                _get_adapter().send_keys("enter")
                _settle(self.stable_ms)
                desc = f"Shell run: Cmd+Space → {command!r} → Enter"
            else:
                # Linux：尝试使用 dmenu_run
                proc = subprocess.run(
                    ["dmenu_run", "-p", "> "],
                    input=command.encode(),
                    timeout=5,
                )
                desc = f"Shell run: dmenu_run → {command!r}"
                return PrimitiveResult(
                    success=(proc.returncode == 0),
                    description=desc,
                    action_name="shell_run",
                    error=f"dmenu_run exit code {proc.returncode}" if proc.returncode else None,
                    elapsed_ms=(time.perf_counter() - start) * 1000,
                )
            return PrimitiveResult(
                success=True,
                description=desc,
                action_name="shell_run",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            errors.append(str(exc))
            return PrimitiveResult(
                success=False,
                description=f"shell_run({command[:20]!r}...)",
                action_name="shell_run",
                error="; ".join(errors),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def shell_powershell(self, script: str) -> PrimitiveResult:
        """直接执行 PowerShell 脚本（仅限 Windows）。

        使用 ``subprocess.run``，因此不会弹出交互式 Shell 窗口。

        Parameters
        ----------
        script : str
            PowerShell 命令或多行脚本块。

        Returns
        -------
        PrimitiveResult
        """
        if self._platform != Platform.WINDOWS:
            return PrimitiveResult(
                success=False,
                description="shell_powershell (non-Windows)",
                action_name="shell_powershell",
                error="shell_powershell is only supported on Windows",
            )
        if not script.strip():
            return PrimitiveResult(
                success=False,
                description="shell_powershell('')",
                action_name="shell_powershell",
                error="script must be non-empty",
            )
        start = time.perf_counter()
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy", "Bypass",
                    "-Command", script,
                ],
                capture_output=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            success = result.returncode == 0
            error_msg = None if success else (
                result.stderr.strip() or f"exit code {result.returncode}"
            )
            return PrimitiveResult(
                success=success,
                description=f"PowerShell: {script[:40]!r}...",
                action_name="shell_powershell",
                error=error_msg,
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except subprocess.TimeoutExpired:
            return PrimitiveResult(
                success=False,
                description=f"shell_powershell timeout: {script[:20]!r}...",
                action_name="shell_powershell",
                error="Execution timed out after 30 seconds",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"shell_powershell({script[:20]!r}...)",
                action_name="shell_powershell",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    # ── 组合原语 ───────────────────────────────────────────────

    def navigate_to_menu(self, menu_path: str) -> PrimitiveResult:
        """使用 Alt+字母 序列，导航到层级化的 **菜单路径**。

        Parameters
        ----------
        menu_path : str
            人类可读的菜单路径。分隔符 ``→``（U+2192）会被忽略。
            路径中的空白与非字母数字字符会被剔除，随后每个字母
            都以 ``Alt+letter`` 的形式依次发送。

            示例::

                "文件→另存为"  →  Alt+F, S
                "Edit → Find"  →  Alt+E, F
                "F→S"          →  Alt+F, S
                "工具→选项"    →  Alt+T, O

        Returns
        -------
        PrimitiveResult
        """
        if not menu_path.strip():
            return PrimitiveResult(
                success=False,
                description="navigate_to_menu('')",
                action_name="navigate_to_menu",
                error="menu_path must be non-empty",
            )
        return self.menu_sequence(menu_path)

    def close_dialog(self) -> PrimitiveResult:
        """使用 **Esc** 关闭当前活动模态对话框，必要时回退到 **Alt+F4**。

        Returns
        -------
        PrimitiveResult
        """
        # 先尝试 Esc（最轻量，最常见）
        esc_result = self.escape()
        if esc_result.success:
            return PrimitiveResult(
                success=True,
                description="close_dialog → Escape",
                action_name="close_dialog",
                elapsed_ms=esc_result.elapsed_ms,
            )
        # 回退方案：Alt+F4（Windows）/ Cmd+W（macOS）/ Alt+F4（Linux）
        start = time.perf_counter()
        try:
            if self._platform == Platform.MACOS:
                combo = "command+w"
            else:
                combo = "alt+f4"
            _get_adapter().send_keys(combo)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"close_dialog → {combo.upper().replace('+', ' + ')}",
                action_name="close_dialog",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="close_dialog (all methods failed)",
                action_name="close_dialog",
                error=f"Esc failed; Alt+F4/Cmd+W error: {exc}",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def select_all(self) -> PrimitiveResult:
        """在当前上下文中使用 **Ctrl+A**（macOS 上为 Cmd+A）全选项目 / 文本。"""
        start = time.perf_counter()
        try:
            combo = "command+a" if self._platform == Platform.MACOS else "ctrl+a"
            _get_adapter().send_keys(combo)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Select all: {combo.upper()}",
                action_name="select_all",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="select_all",
                action_name="select_all",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def left_click(self, clicks: int = 1) -> PrimitiveResult:
        """执行鼠标左键点击（在鼠标光标的当前位置）。

        对应自然语言中的「空格 / 点击 / 单击」。

        Parameters
        ----------
        clicks : int
            连击次数，默认为 1（单击）。设为 2 即为双击。

        Returns
        -------
        PrimitiveResult
        """
        start = time.perf_counter()
        try:
            _get_adapter().click(button="left", clicks=clicks)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Left click × {clicks}",
                action_name="left_click",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description=f"left_click × {clicks}",
                action_name="left_click",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def unselect_all(self) -> PrimitiveResult:
        """使用 **Escape** 或 **Ctrl+Click** 清除当前选择。"""
        # Escape 是最通用的做法
        return self.escape()

    def delete_char(self, n: int = 1) -> PrimitiveResult:
        """删除 ``n`` 个字符：向后（**Delete**）或向前（**Backspace**）。

        默认使用 **Delete**（向后删除）；传入 ``n < 0`` 时使用 Backspace。

        Parameters
        ----------
        n : int
            要删除的字符数。正数 → Delete，负数 → Backspace。
            默认为 1（Delete）。

        Returns
        -------
        PrimitiveResult
        """
        n = int(n)
        start = time.perf_counter()
        errors: List[str] = []
        try:
            adapter = _get_adapter()
            key = "delete" if n > 0 else "backspace"
            count = abs(n)
            for i in range(count):
                adapter.send_keys(key)
                if i < count - 1:
                    _settle(self.wait_ms)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"delete_char × {count} ({key})",
                action_name="delete_char",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            errors.append(str(exc))
            return PrimitiveResult(
                success=False,
                description=f"delete_char(n={n})",
                action_name="delete_char",
                error="; ".join(errors),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def undo(self) -> PrimitiveResult:
        """使用 **Ctrl+Z**（macOS 上为 Cmd+Z）撤销上一次操作。"""
        start = time.perf_counter()
        try:
            combo = "command+z" if self._platform == Platform.MACOS else "ctrl+z"
            _get_adapter().send_keys(combo)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Undo: {combo.upper()}",
                action_name="undo",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="undo",
                action_name="undo",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )

    def redo(self) -> PrimitiveResult:
        """重做上一次被撤销的操作。

        - **Ctrl+Y** 或 **Ctrl+Shift+Z**（Windows/Linux）
        - **Cmd+Shift+Z**（macOS）
        """
        start = time.perf_counter()
        try:
            if self._platform == Platform.MACOS:
                combo = "command+shift+z"
            else:
                # 先尝试 Ctrl+Y（在 Windows 上更常见），回退到 Ctrl+Shift+Z
                try:
                    _get_adapter().send_keys("ctrl+y")
                    _settle(self.stable_ms)
                    return PrimitiveResult(
                        success=True,
                        description="Redo: CTRL+Y",
                        action_name="redo",
                        elapsed_ms=(time.perf_counter() - start) * 1000,
                    )
                except Exception:  # pragma: no cover
                    combo = "ctrl+shift+z"
            _get_adapter().send_keys(combo)
            _settle(self.stable_ms)
            return PrimitiveResult(
                success=True,
                description=f"Redo: {combo.upper()}",
                action_name="redo",
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
        except Exception as exc:
            return PrimitiveResult(
                success=False,
                description="redo",
                action_name="redo",
                error=str(exc),
                elapsed_ms=(time.perf_counter() - start) * 1000,
            )
