"""意图识别引擎。

识别层级（由快到慢）：
1. 直接关键字匹配（内置字典）
2. 数据库同义词查找（SQLite）
3. 包含 / 子串匹配
4. 基于 difflib 的模糊匹配
5. spaCy 语义匹配（可选，需安装 spacy 模型）
"""

import os
import re
import difflib
import logging
from typing import Optional

from .models import IntentResult
from .database import DatabaseManager

# 调试日志：设置环境变量 NL2SHORTCUT_DEBUG=1 启用
_log = logging.getLogger(__name__)
if os.environ.get("NL2SHORTCUT_DEBUG"):
    _log.setLevel(logging.DEBUG)
else:
    _log.setLevel(logging.WARNING)  # 显式禁用 DEBUG，防止 root logger 配置泄漏

# 优先用 rapidfuzz（C++ 实现，10-100x 更快），未安装则回退到 difflib
try:
    from rapidfuzz.distance import Indel
    _HAS_RAPIDFUZZ = True

    def _fuzzy_ratio(a: str, b: str) -> float:
        """rapidfuzz 归一化相似度（基于 Indel 距离，与 difflib.SequenceMatcher.ratio 等价）。"""
        if not a or not b:
            return 0.0
        # rapidfuzz 的 normalized_similarity 返回 0.0-1.0
        return Indel.normalized_similarity(a, b)
except ImportError:
    _HAS_RAPIDFUZZ = False

    def _fuzzy_ratio(a: str, b: str) -> float:
        """difflib 回退路径（Python 纯实现，较慢）。"""
        if not a or not b:
            return 0.0
        return difflib.SequenceMatcher(None, a, b).ratio()



# Window-context search: "在文件资源管理器里搜索 X" / "资源管理器里找 X" / "文件夹里查找 X"
_WINDOW_SEARCH_RE = re.compile(
    r'^\s*在?\s*(?:文件资源管理器|资源管理器|文件管理器|文件夹(?:窗口)?|此电脑|explorer)\s*'
    r'(?:里|中|窗口)?\s*(?:搜索|查找|找)\s*(.+?)\s*$'
)


def _extract_dialog_search(text: str):
    """从对话框相关短语中提取搜索关键字。

    处理所有变体形式，包括：
      - 在对话框里找文件        → 关键字："文件"
      - 在对话框里找 report    → 关键字："report"
      - 在文件对话框里找图片    → 关键字："图片"
      - 在打开文件对话框里搜索  → 关键字：""（默认取 "文件"）
      - 打开文件对话框找图片    → 关键字："图片"
      - 文件对话框里找 report   → 关键字："report"（无 "在" 前缀）
      - 对话框里找 report       → 关键字："report"（裸 "对话框里"）
      - 对话框里搜索            → 关键字：""（裸形式，无关键字）
      - 对话框找 report         → 关键字："report"（无 "里"）
      - 保存对话框搜索 docx     → 关键字："docx"

    返回搜索关键字（str）；若不是对话框搜索短语则返回 None。
    """
    text = text.strip()
    verbs = ["搜索", "查找", "找"]

    # ── Type D: window-context search (File Explorer / 资源管理器 / 文件夹) ──
    # "在文件资源管理器里搜索 X" → keyword: X ; routes to Explorer search composite
    mw = _WINDOW_SEARCH_RE.match(text)
    if mw:
        kw = mw.group(1).strip()
        return kw if kw else "文件"

    # All patterns — longest first (贪婪优先，防止短模式先匹配)
    # Type A: "在...对话框里" — 完整形式
    patterns_a = [
        "在打开文件对话框里",
        "在保存文件对话框里",
        "在文件对话框里",
        "在打开对话框里",
        "在保存对话框里",
        "在对话框里",
    ]
    # Type B: "对话框里" — 无 "在" 前缀，直接以 "对话框里" 开头
    patterns_b = [
        "对话框里",
    ]
    # Type C: "文件对话框" — 无 "在"，动词紧跟对话框名
    patterns_c = [
        "打开文件对话框",
        "保存文件对话框",
        "文件对话框",
        "打开对话框",
        "保存对话框",
        "对话框",
    ]

    all_patterns = patterns_a + patterns_b + patterns_c

    for prefix in all_patterns:
        if not text.startswith(prefix):
            continue

        rest = text[len(prefix):]

        # Try: [verb][keyword]  — verb directly after prefix
        for v in verbs:
            if rest.startswith(v):
                keyword = rest[len(v):].strip()
                return keyword if keyword else "文件"

        # Try: [里][verb][keyword] — Type B pattern leaves "里" in rest
        # e.g. "文件对话框里找 report" → prefix="文件对话框", rest="里找 report"
        if rest.startswith("里"):
            rest2 = rest[len("里"):]
            for v in verbs:
                if rest2.startswith(v):
                    keyword = rest2[len(v):].strip()
                    return keyword if keyword else "文件"
            if rest2:
                return rest2.strip()

        # Type C fallback: only accept rest that starts with a search verb
        # ✅ "对话框找 report"  → prefix="对话框", rest="找 report"
        #    starts with "找" (search verb) → keyword="report"
        # ❌ "对话框打开文件"  → prefix="对话框", rest="打开文件"
        #    starts with "打开" (not a search verb) → NOT a search phrase
        if rest:
            for v in verbs:
                if rest.startswith(v):
                    keyword = rest[len(v):].strip()
                    return keyword if keyword else "文件"
            return None  # rest doesn't start with a search verb → not a search phrase

    return None


# ── 路径请求提取 ────────────────────────────────────────
# 针对 "打开此文件夹" / "跳转到路径" / "在对话框中输入" 的模式
import re as _re_path
_FOLDER_PATH = _re_path.compile(r'[A-Za-z]:[\\/](?:[^\\/:*?"<>|\r\n ]+[\\/]?)*|\\\\[^\\/:*?"<>|\r\n ]+[\\/][^\\/:*?"<>|\r\n ]+')
_PATH_TRIGGERS_OPEN = [
    "打开", "跳转到", "前往", "进入", "转到", "去到", "进到", "访问",
    "导航到", "进入", "到", "去看看", "open", "navigate to", "go to",
    "cd to", "进入文件夹", "打开文件夹",
]
_PATH_TRIGGERS_DIALOG = [
    "输入路径", "输入文件夹", "输入这个路径", "粘贴路径", "输入地址",
    "在对话框输入", "对话框里输入", "路径", "type path", "paste path",
    "input path",
]
_PATH_TRIGGERS_TYPE = [
    "输入", "填入", "粘贴", "写入", "type", "input", "fill in", "enter",
]


def _extract_folder_path_request(text: str):
    """检测 "打开文件夹 X" / "在对话框中输入路径 X" 类的请求。

    返回 (folder_path, op) 元组，其中 op 取值为：
      - 'open'   ：打开文件资源管理器并导航到该路径
      - 'dialog' ：将路径输入已打开的通用文件对话框
      - 'type'   ：仅将路径输入当前获得焦点的任意文本框

    若未检测到文件夹路径或无触发动词，则返回 None。

    示例：
      '打开 C:\\Users\\Deng2'         → ('C:\\Users\\Deng2', 'open')
      '跳转到 D:/projects'           → ('D:/projects', 'open')
      '去到 \\\\server\\share'        → ('\\\\server\\share', 'open')
      '在对话框输入 D:\\docs'          → ('D:\\docs', 'dialog')
      '输入路径 C:\\Users'            → ('C:\\Users', 'type')
    """
    text = text.strip()
    m = _FOLDER_PATH.search(text)
    if not m:
        return None

    folder_path = m.group(0).rstrip("/\\")
    rest = text[:m.start()] + text[m.end():]
    rest = rest.strip()

    # 根据 `rest`（路径以外的文本）中的触发动词判断操作类型
    # 顺序很重要：'dialog' 触发词最为具体，应优先匹配
    for trig in _PATH_TRIGGERS_DIALOG:
        if trig in rest or trig in text:
            return (folder_path, "dialog")
    for trig in _PATH_TRIGGERS_OPEN:
        if trig in rest or trig in text:
            return (folder_path, "open")
    for trig in _PATH_TRIGGERS_TYPE:
        if trig in rest:
            return (folder_path, "type")

    # 默认：若路径存在但无特定触发词，则假定为 "open"（打开）
    return (folder_path, "open")



def _strip_quotes(s: str) -> str:
    """去掉字符串两側包围的引号（中英文单双引号、书名号、反引号）。

    用于组合模式捕获文件名/路径时，剔除用户随手加的引号，
    例如 'C:\\\\a.docx' / “报告” / 「英语单词表」 → C:\\\\a.docx / 报告 / 英语单词表。
    """
    QUOTE_CHARS = "'\"「」‘’“”`"
    s = s.strip()
    # 前后引号独立剥除：正则捕获时常只吞掉一侧引号，
    # 必须允许「只有前导/只有结尾引号」的非对称情况。
    if len(s) >= 1 and s[0] in QUOTE_CHARS:
        s = s[1:].strip()
    if len(s) >= 1 and s[-1] in QUOTE_CHARS:
        s = s[:-1].strip()
    return s

class IntentEngine:
    """从自然语言输入中识别用户意图。"""

    # 置信度阈值
    EXACT_THRESHOLD = 0.95
    CONTAINS_THRESHOLD = 0.80
    SYNONYM_THRESHOLD = 0.85
    FUZZY_THRESHOLD = 0.60
    MIN_CONFIDENCE = 0.50

    COMMAND_KEYWORDS = {
        # 英文
        "copy": "copy", "paste": "paste", "cut": "cut",
        "undo": "undo", "redo": "redo",
        "save": "save", "open": "open", "close": "close",
        "find": "find", "replace": "replace",
        "bold": "bold", "italic": "italic", "underline": "underline",
        "delete": "delete", "print": "print",
        "refresh": "refresh", "fullscreen": "fullscreen",
        "minimize": "minimize",
        "zoom": "zoom_in",
        "screenshot": "screenshot",
        "lock": "lock_screen",
        "comment": "comment", "format": "format_code",
        "rename": "rename", "terminal": "terminal",
        # 中文
        "\u590d\u5236": "copy", "\u7c98\u8d34": "paste", "\u526a\u5207": "cut",
        "\u64a4\u9500": "undo", "\u91cd\u505a": "redo",
        "\u4fdd\u5b58": "save", "\u5173\u95ed": "close",
        "\u67e5\u627e": "find", "\u641c\u7d22": "find", "\u66ff\u6362": "replace",
        "\u52a0\u7c97": "bold", "\u659c\u4f53": "italic", "\u4e0b\u5212\u7ebf": "underline",
        "\u5220\u9664": "delete", "\u6253\u5370": "print",
        "center": "align_center", "centered": "align_center", "\u5c45\u4e2d": "align_center",
        # 直接输入按键组合（精确匹配优先于模糊匹配）
        # 居中对齐
        "ctrl+e": "align_center", "ctrl-e": "align_center", "ctrl e": "align_center",
        "Ctrl+E": "align_center", "Ctrl-E": "align_center", "Ctrl E": "align_center",
        # 加粗 / 斜体 / 下划线
        "ctrl+b": "bold", "ctrl-b": "bold", "ctrl b": "bold",
        "Ctrl+B": "bold", "Ctrl-B": "bold", "Ctrl B": "bold",
        "ctrl+i": "italic", "ctrl-i": "italic", "ctrl i": "italic",
        "Ctrl+I": "italic", "Ctrl-I": "italic", "Ctrl I": "italic",
        "ctrl+u": "underline", "ctrl-u": "underline", "ctrl u": "underline",
        "Ctrl+U": "underline", "Ctrl-U": "underline", "Ctrl U": "underline",
        # 所有 Ctrl+字母 快捷键（使用精确关键字，避免与 ctrl+e 发生模糊冲突）
        "ctrl+a": "select_all", "ctrl-a": "select_all", "ctrl+c": "copy",
        "ctrl+d": "duplicate_line", "ctrl+f": "find", "ctrl+k": "search_file",
        "ctrl+n": "new_file", "ctrl+o": "open", "ctrl+p": "print",
        "ctrl+r": "refresh", "ctrl+s": "save", "ctrl+v": "paste",
        "ctrl+w": "close", "ctrl+x": "cut", "ctrl+y": "redo", "ctrl+z": "undo",
        "Ctrl+A": "select_all", "Ctrl+D": "duplicate_line", "Ctrl+F": "find",
        "Ctrl+K": "search_file", "Ctrl+N": "new_file", "Ctrl+O": "open",
        "Ctrl+P": "print", "Ctrl+R": "refresh", "Ctrl+S": "save",
        "Ctrl+V": "paste", "Ctrl+W": "close", "Ctrl+X": "cut",
        "Ctrl+Y": "redo", "Ctrl+Z": "undo",
        "Ctrl+Shift+P": "command_palette", "Ctrl+Shift+S": "save_as",
        "Ctrl+Shift+D": "duplicate_line", "Ctrl+Shift+W": "close_all",
                # 按键组合的小写变体
        "ctrl+shift+p": "command_palette", "ctrl+shift+s": "save_as",
        "ctrl+shift+d": "duplicate_line", "ctrl+shift+w": "close_all",
"\u5237\u65b0": "refresh", "\u5168\u5c4f": "fullscreen",
        "\u6700\u5c0f\u5316": "minimize",
        "\u622a\u56fe": "screenshot", "\u622a\u5c4f": "screenshot",
        "\u9501\u5c4f": "lock_screen",
        "\u6ce8\u91ca": "comment", "\u683c\u5f0f\u5316": "format_code",
        "\u91cd\u547d\u540d": "rename", "\u7ec8\u7aef": "terminal",
        "\u5168\u9009": "select_all",
        "\u65b0\u5efa": "new_file",
        "\u5207\u6362": "switch_app", "\u5207\u6362\u5e94\u7528": "switch_app",
        "\u5173\u95ed\u5168\u90e8": "close_all",
        "\u653e\u5927": "zoom_in",
        "\u7f29\u5c0f": "zoom_out",
        "\u4efb\u52a1\u7ba1\u7406\u5668": "task_manager",
        "\u8fd0\u884c": "run_dialog",
        "\u547d\u4ee4\u9762\u677f": "command_palette",
        "\u641c\u7d22\u6587\u4ef6": "search_file",
        "\u8df3\u8f6c\u5230\u5b9a\u4e49": "go_to_definition", "\u8f6c\u5230\u5b9a\u4e49": "go_to_definition",
        "\u590d\u5236\u884c": "duplicate_line",
        "\u8d44\u6e90\u7ba1\u7406\u5668": "ms_win_e",
        "\u6253\u5f00\u8d44\u6e90\u7ba1\u7406\u5668": "ms_win_e",
        "\u6587\u4ef6\u8d44\u6e90\u7ba1\u7406\u5668": "ms_win_e",
        # 拼音兜底（拉丁化的中文）
        "fuzhi": "copy", "zhantie": "paste", "jianqie": "cut",
        "chexiao": "undo", "chongzuo": "redo",
        "baocun": "save", "guanbi": "close",
        "chazhao": "find", "sousuo": "find", "tihuan": "replace",
        "jiacu": "bold", "xieti": "italic", "xiahuaxian": "underline",
        "shanchu": "delete", "dayin": "print",
        "shuaxin": "refresh", "quanping": "fullscreen",
        "zuixiaohua": "minimize",
        "jietu": "screenshot",
        "suoping": "lock_screen",
        "zhushi": "comment", "geshihua": "format_code",
        "zhongmingming": "rename", "zhongduan": "terminal",
        "quanxuan": "select_all",
        "xinjian": "new_file",
        "qiehuan": "switch_app",
        "guanbiquanbu": "close_all",
        "fangda": "zoom_in",
        "suoxiao": "zoom_out",
        "renwuguanliqi": "task_manager",
        "yunxing": "run_dialog",
        "minglingmianban": "command_palette",
        "sousuowenjian": "search_file",
        "tiaozhuandaodingyi": "go_to_definition",
        "fuzhihang": "duplicate_line",
        # 鼠标操作
        "空格": "select", "space": "select",
        "选择": "select", "勾选": "select", "select": "select",
        "点击": "left_click", "click": "left_click",
        "鼠标左键": "left_click", "left click": "left_click",
        "单击": "left_click", "左键": "left_click",
    }

    # ── 终端打开模式（在关键字匹配之前） ──
    TERMINAL_OPEN_PATTERNS = [
        re.compile(r'^\s*(?:打开终端|打开cmd|打开命令行|打开命令提示符|打开powershell|打开power shell)\s*$'),
    ]

    # ── 打开应用程序的统一流程 ──
    # "打开X"（X 为应用名）统一走 Win → 搜索 → 等待 300ms → Enter 流程，
    # 不再绑定 Ctrl+O（那是「打开文件对话框」的快捷键）。
    # 例外：已注册的特定目标由各自的精确通道处理，不走此通用流程。
    _OPEN_APP_EXCLUDES = frozenset({
        # 资源管理器系列 → precache 精确命中 Win+E
        "资源管理器", "文件资源管理器", "此电脑", "explorer",
        # 终端系列 → _try_terminal_open 命中
        "终端", "cmd", "命令行", "命令提示符", "powershell", "power shell",
        # 任务视图 → precache 精确命中
        "任务视图", "多任务视图", "时间线",
    })
    _OPEN_APP_RE = re.compile(r'^\s*打\s*开\s*(\S.+?)\s*$')

    # ── 组合模式（文件复制 / 移动 / 查找） ──
    # 在关键字层之前运行，以避免 "复制X到Y" 被误读为单个键盘快捷键
    # "copy"（Ctrl+C）。
    # 元组：(正则表达式, 操作类型, 是否剔除中文方位词后缀)
    #
    # 注意：所有组合动作现在都使用纯键盘操作（不再使用 shell 脚本）。
    # - 复制 / 移动：上下文菜单导航（Shift+F10）
    # - 查找：Win+E → Ctrl+L → 输入 → Enter → Tab
    COMPOSITE_PATTERNS = [
        # ── 查找模式（在资源管理器中的键盘搜索） ──
        # 顺序很重要：更具体的模式（find_open）必须排在通用 find 之前
        # "找到X并打开" / "搜索X并打开"（必须排在第一位）
        (re.compile(r"^\s*(?:找到|查找|搜索|找)\s*(.+?)\s*'?\s*并\s*打开\s*$"), "find_open", False),
        # "找到X并复制" / "搜索X并复制" → 键盘搜索并复制首项
        # （必须排在通用 find 之前，否则会被 "找到X" 抢先匹配）
        (re.compile(r"^\s*(?:找到|查找|搜索|找)\s*(.+?)\s*'?\s*并\s*复制\s*$"), "find_copy", False),
        # "复制找到的X" / "复制搜索到的X" → 键盘搜索并复制首项
        (re.compile(r'^\s*复制\s*(?:找到|查找|搜索|搜到|找)\s*(?:的)?\s*(.+?)\s*$'), "find_copy", False),
        # 英文 "find X and copy" / "search X and copy"
        (re.compile(r'^\s*(?:find|search)\s+(.+?)\s+and\s+copy\s*$', re.IGNORECASE), "find_copy", False),
        # "找到X" / "查找X" / "搜索X" / "找X" → 触发键盘搜索
        (re.compile(r'^\s*(?:找到|查找|搜索|找)\s*(.+?)\s*$'), "find", False),
        # ── 英文查找模式 ──
        # "find X" / "search for X" / "search X" / "locate X" → 键盘搜索
        (re.compile(r'^\s*(?:find|search\s+for|search|locate)\s+(.+?)\s*$', re.IGNORECASE), "find", False),
        # ── 对话框专用查找模式 ──
        # "在对话框里找X" / "在打开文件对话框里找X" / "在保存对话框里找X" → 键盘搜索
        (re.compile(r'^\s*在(?:(?:打开|保存)?文件)?对话框里(?:找|搜索|查找)\s+(.+?)\s*$'), "find", False),
        # ── 带目标的复制 / 移动（上下文菜单导航） ──
        # 中文：把X复制到Y / 把X拷贝到Y
        (re.compile(r'^\s*把\s*(.+?)\s*(?:复制到|拷贝到)\s*(.+?)\s*[内中里]?\s*$'), "copy", True),
        # 中文：将X复制到Y / 将X拷贝到Y
        (re.compile(r'^\s*将\s*(.+?)\s*(?:复制到|拷贝到)\s*(.+?)\s*[内中里]?\s*$'), "copy", True),
        # 中文：X复制到Y
        (re.compile(r'^\s*(.+?)\s*复制到\s*(.+?)\s*[内中里]?\s*$'), "copy", True),
        # 中文：复制X到Y
        (re.compile(r'^\s*复制\s*(.+?)\s*到\s*(.+?)\s*[内中里]?\s*$'), "copy", True),
        # 中文：把X移动到Y / 把X剪切到Y
        (re.compile(r'^\s*把\s*(.+?)\s*(?:移动到|剪切到)\s*(.+?)\s*[内中里]?\s*$'), "move", True),
        # 中文：将X移动到Y / 将X剪切到Y
        (re.compile(r'^\s*将\s*(.+?)\s*(?:移动到|剪切到)\s*(.+?)\s*[内中里]?\s*$'), "move", True),
        # 中文：X移动到Y
        (re.compile(r'^\s*(.+?)\s*移动到\s*(.+?)\s*[内中里]?\s*$'), "move", True),
        # 英文：copy X to Y / move X to Y
        (re.compile(r'^\s*copy\s+(.+?)\s+to\s+(.+?)\s*$', re.IGNORECASE), "copy", False),
        (re.compile(r'^\s*move\s+(.+?)\s+to\s+(.+?)\s*$', re.IGNORECASE), "move", False),
        # ── 无源（复制到桌面 / 移动到下载）：源用通配 *（空捕获组让循环补 *）──
        (re.compile(r'^\s*()(?:复制|拷贝)\s*到\s*(.+?)\s*[内中里]?\s*$'), "copy", True),
        (re.compile(r'^\s*()(?:移动|剪切)\s*到\s*(.+?)\s*[内中里]?\s*$'), "move", True),
        # ── 把/将 + 源 + 复制[文件/内容/资料…] + 到 + 目标（对象词夹在动词与到之间）──
        (re.compile(r'^\s*(?:把|将)\s*(.+?)\s*(?:复制|拷贝)\s*(?:文件|内容|资料|它|这个|这份|选中的|选中|此文件|该文件)?\s*到\s*(.+?)\s*[内中里]?\s*$'), "copy", True),
        (re.compile(r'^\s*(?:把|将)\s*(.+?)\s*(?:移动|剪切)\s*(?:文件|内容|资料|它|这个|这份|选中的|选中|此文件|该文件)?\s*到\s*(.+?)\s*[内中里]?\s*$'), "move", True),
        # ── 从 X 复制/移动 Y 到 Z（Y 为文件，夹在动词与到之间；3 组）──
        (re.compile(r'^\s*(?:从|自)\s*(.+?)\s*(?:复制|拷贝)\s*(?:的)?\s*(.+?)\s*(?:到|去)\s*(.+?)\s*[内中里]?\s*$'), "copy", False),
        (re.compile(r'^\s*(?:从|自)\s*(.+?)\s*(?:移动|剪切)\s*(?:的)?\s*(.+?)\s*(?:到|去)\s*(.+?)\s*[内中里]?\s*$'), "move", False),
    ]

    def __init__(self, db: DatabaseManager):
        self._db = db
        self._nlp = None
        self._nlp_available = False

    def enable_spacy(self) -> bool:
        """尝试加载 spaCy 模型。成功返回 True。"""
        try:
            import spacy
            self._nlp = spacy.load("en_core_web_sm")
            self._nlp_available = True
            return True
        except Exception:
            try:
                import spacy
                self._nlp = spacy.load("zh_core_web_sm")
                self._nlp_available = True
                return True
            except Exception:
                return False

    def _clean_text(self, text: str) -> str:
        text = text.lower().strip()
        text = re.sub(r'[^\w\s+\-]', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()



    def recognize(self, text: str) -> IntentResult:
        """主入口：从自然语言识别意图。"""
        original = text.strip()
        if not original:
            _log.debug("[Intent] 空输入，返回 unknown")
            return IntentResult(intent="unknown", command="", confidence=0.0)

        # ── 终端打开检测 —— 在关键字匹配之前 ──
        iso = self._try_terminal_open(original)
        if iso is not None:
            _log.debug("[Intent] 终端打开命中 text=%r cmd=%s", original, iso.command)
            return iso

        # ── 组合（文件复制 / 移动）检测 —— 在关键字层之前 ──
        comp = self._try_composite(original)
        if comp is not None:
            _log.debug("[Intent] 组合操作命中 text=%r cmd=%s", original, comp.command)
            return comp

        # ── 打开应用程序检测 —— 在组合检测之后 ──
        # 路径形式（"打开 C:\..."）已由上方 _try_composite 捕获；
        # 此处仅处理通用"打开X"（X 为应用名）→ 统一开始菜单搜索流程。
        oap = self._try_open_app(original)
        if oap is not None:
            _log.debug("[Intent] 打开应用命中 text=%r cmd=%s", original, oap.command)
            return oap

        cleaned = self._clean_text(original)

        # 第 1 层：直接关键字匹配
        result = self._keyword_match(cleaned)
        if result and result.confidence >= self.EXACT_THRESHOLD:
            _log.debug("[Intent] L1 关键字命中 text=%r cmd=%s conf=%.3f", original, result.command, result.confidence)
            return result

        # 第 2 层：数据库同义词
        result = self._synonym_match(cleaned)
        if result and result.confidence >= self.SYNONYM_THRESHOLD:
            _log.debug("[Intent] L2 同义词命中 text=%r cmd=%s conf=%.3f", original, result.command, result.confidence)
            return result

        # 第 3 层：包含匹配
        result = self._contains_match(cleaned)
        if result and result.confidence >= self.CONTAINS_THRESHOLD:
            _log.debug("[Intent] L3 子串命中 text=%r cmd=%s conf=%.3f", original, result.command, result.confidence)
            return result

        # 第 4 层：模糊匹配
        result = self._fuzzy_match(cleaned)
        if result and result.confidence >= self.FUZZY_THRESHOLD:
            _log.debug("[Intent] L4 模糊命中 text=%r cmd=%s conf=%.3f", original, result.command, result.confidence)
            return result

        # 第 5 层：spaCy 语义匹配（若可用）
        if self._nlp_available:
            result = self._spacy_match(original)
            if result and result.confidence >= self.MIN_CONFIDENCE:
                _log.debug("[Intent] L5 spaCy命中 text=%r cmd=%s conf=%.3f", original, result.command, result.confidence)
                return result

        _log.debug("[Intent] 5层瀑布全部未命中 text=%r（返回 unknown）", original)
        return (
            result if result
            else IntentResult(intent="unknown", command="", confidence=0.0)
        )

    def _try_terminal_open(self, text: str) -> Optional[IntentResult]:
        """检测 "open terminal" / "打开终端"，并生成一个真实的按键序列方案。"""
        for pat in self.TERMINAL_OPEN_PATTERNS:
            if pat.match(text.strip()):
                from .composites import CompositePlan, CompositeStep
                plan = CompositePlan(
                    name="open_terminal",
                    description="Open Windows Terminal/CMD",
                    confidence=0.95,
                    reasoning="Win+R → cmd → Enter",
                    steps=[
                        CompositeStep(kind="key", keys="Win+R",
                                      description="Win+R 打开运行"),
                        CompositeStep(kind="wait", wait_ms=200,
                                      description="等待运行对话框"),
                        CompositeStep(kind="type", text="cmd",
                                      description="输入 cmd"),
                        CompositeStep(kind="key", keys="Enter",
                                      description="Enter 打开命令行"),
                    ],
                )
                return IntentResult(
                    intent="open_terminal",
                    command="__composite__",
                    confidence=0.95,
                    matched_keyword="terminal_open",
                    composite_plan=plan,
                )
        return None

    def _try_open_app(self, text: str) -> Optional[IntentResult]:
        """检测 "打开X" 并生成统一的应用打开流程：Win → 搜索 → 等待 → Enter。

        所有「打开X」（X 为应用名）均走此统一流程，不绑定 Ctrl+O。
        例外：已注册的特定目标（资源管理器=Win+E、终端=Win+R→cmd）由各自的
        精确通道处理，不走到这里。
        路径形式（"打开 C:\\..."）由 _try_composite 的路径检测先捕获。
        """
        m = self._OPEN_APP_RE.match(text.strip())
        if not m:
            return None
        app_name = m.group(1).strip()
        if not app_name or app_name in self._OPEN_APP_EXCLUDES:
            _log.debug("[Intent] 打开应用被排除 app=%r（在 _OPEN_APP_EXCLUDES 中）", app_name)
            return None
        _log.debug("[Intent] 打开应用生成计划 app=%r", app_name)
        from .composites import make_open_app
        plan = make_open_app(app_name=app_name)
        return IntentResult(
            intent="open_app",
            command="__composite__",
            confidence=0.90,
            matched_keyword=f"open_app: {app_name}",
            composite_plan=plan,
        )

    def _try_composite(self, text: str) -> Optional[IntentResult]:
        """检测文件复制 / 移动 / 查找的组合意图，如 '复制X到Y' / 'copy X to Y' / '找到X'。

        返回一个 IntentResult，其 command='__composite__' 并带有一个填充好的
        composite_plan（使用纯键盘，无 shell 脚本）；若无任何模式匹配则返回 None。
        """
        from .composites import (
            make_file_copy_context_menu,
            make_file_move_context_menu,
            make_file_search_keyboard,
            make_file_search_copy,
            make_file_search_terminal,
            make_terminal_copy_to_folder,
            make_terminal_move_to_folder,
            make_copy_to_folder,
            make_move_to_folder,
            make_find_and_copy,
            make_type_folder_path,
            make_open_folder,
            make_dialog_open_path,
        )

        # ── 路径输入（打开文件夹 / 在对话框中输入）—— 优先于其他所有逻辑 ──
        # 捕获明确的文件夹路径，如 "打开 C:\Users\..." 或 "跳转到 ~/projects"
        # 但「查找/搜索…并复制/并打开」属于组合查找模式，必须交给下方
        # COMPOSITE_PATTERNS 循环（find_copy → make_find_and_copy），不能被
        # 「有路径即默认打开文件夹」的兜底抢先匹配。
        if re.search(
            r'^\s*(?:找到|查找|搜索|找)\s*.+\s*\'?\s*并\s*(?:复制|打开)',
            text,
        ):
            path_match = None
        else:
            path_match = _extract_folder_path_request(text)
        if path_match is not None:
            folder_path, op = path_match
            if op == "open":
                plan = make_open_folder(folder_path=folder_path)
                return IntentResult(
                    intent="composite_open_folder",
                    command="__composite__",
                    confidence=0.88,
                    matched_keyword=f"open: {folder_path}",
                    composite_plan=plan,
                )
            elif op == "dialog":
                plan = make_dialog_open_path(folder_path=folder_path)
                return IntentResult(
                    intent="composite_dialog_open_path",
                    command="__composite__",
                    confidence=0.85,
                    matched_keyword=f"dialog: {folder_path}",
                    composite_plan=plan,
                )
            elif op == "type":
                plan = make_type_folder_path(folder_path=folder_path)
                return IntentResult(
                    intent="composite_type_path",
                    command="__composite__",
                    confidence=0.85,
                    matched_keyword=f"type: {folder_path}",
                    composite_plan=plan,
                )

        # ── 对话框专用搜索（在正则循环之前）──────────────────────
        # 处理："在对话框里找文件" / "在打开文件对话框里搜索" / "打开文件对话框找图片"
        dialog_kw = _extract_dialog_search(text)
        if dialog_kw is not None:
            plan = make_file_search_keyboard(pattern=dialog_kw)
            return IntentResult(
                intent="composite_find",
                command="__composite__",
                confidence=0.80,
                matched_keyword=f"find: {dialog_kw}",
                composite_plan=plan,
            )

        # 填充词（文件/内容/资料/它/这个…）应视为「当前选中项」，归一为通配 *
        _FILLER_SOURCES = {
            "文件", "内容", "资料", "它", "它们", "这个", "这份", "这些",
            "选中的", "选中", "当前", "此文件", "该文件", "此", "这",
        }

        for pattern, op, strip_cn in self.COMPOSITE_PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            groups = m.groups()
            # 3 组模式（从 X 复制 Y 到 Z）：源 = Y(文件)，目标 = Z
            if len(groups) >= 3 and groups[2] is not None and op in ("copy", "move"):
                source = (groups[1] or "").strip()
                dest = (groups[2] or "").strip()
            else:
                source = _strip_quotes((groups[0] or "").strip())
                dest = _strip_quotes(
                    (groups[1].strip() if len(groups) > 1 and groups[1] else ""))
            if strip_cn and dest:
                dest = re.sub(r'[内中里]$', '', dest).strip()
            if not source:
                # 复制到桌面 / 移动到下载 这类无源省略 → 用通配 *
                if op in ("copy", "move"):
                    source = "*"
                else:
                    continue
            if source in _FILLER_SOURCES:
                source = "*"

            if op == "copy":
                plan = make_terminal_copy_to_folder(
                    source_pattern=source,
                    dest_pattern=dest or "Desktop",
                )
            elif op == "move":
                plan = make_terminal_move_to_folder(
                    source_pattern=source,
                    dest_pattern=dest or "Desktop",
                )
            elif op == "find":
                plan = make_file_search_terminal(pattern=source)
            elif op == "find_copy":
                # 在资源管理器搜索框输入（完整路径或关键词），Space 勾选后 Ctrl+C 复制
                plan = make_find_and_copy(file_path=source)
            elif op == "find_open":
                plan = make_file_search_terminal(pattern=source)
                plan.name = "find_and_open_terminal"
                plan.description = f'终端搜索并手动打开：「{source}」'
                plan.description = f'资源管理器键盘搜索并选中：「{source}」'
            else:
                continue
            return IntentResult(
                intent=f"composite_{op}",
                command="__composite__",
                confidence=0.80 if op.startswith("find") else 0.85,
                matched_keyword=f"{op}: {source}" + (f" → {dest}" if dest else ""),
                composite_plan=plan,
            )
        return None

    def _keyword_match(self, text: str) -> Optional[IntentResult]:
        words = text.split()
        # 优先匹配多词短语
        for i in range(min(len(words), 3), 0, -1):
            phrase = ' '.join(words[:i])
            if phrase in self.COMMAND_KEYWORDS:
                cmd = self.COMMAND_KEYWORDS[phrase]
                return IntentResult(
                    intent=cmd, command=cmd,
                    confidence=0.95, matched_keyword=phrase
                )
        # 单个词
        for w in words:
            if w in self.COMMAND_KEYWORDS:
                cmd = self.COMMAND_KEYWORDS[w]
                conf = 0.90 if w == text else 0.85
                return IntentResult(
                    intent=cmd, command=cmd,
                    confidence=conf, matched_keyword=w
                )
        return None

    def _synonym_match(self, text: str) -> Optional[IntentResult]:
        shortcut = self._db.find_by_synonym(text)
        if shortcut:
            return IntentResult(
                intent=shortcut.command, command=shortcut.command,
                confidence=0.85, matched_keyword=text
            )
        return None

    def _contains_match(self, text: str) -> Optional[IntentResult]:
        for kw, cmd in self.COMMAND_KEYWORDS.items():
            if len(kw) >= 2 and kw in text:
                return IntentResult(
                    intent=cmd, command=cmd,
                    confidence=0.80, matched_keyword=kw
                )
        for s in self._db.get_all():
            if s.description.lower() and s.description.lower() in text:
                return IntentResult(
                    intent=s.command, command=s.command,
                    confidence=0.78, matched_keyword=s.description
                )
        return None

    def _fuzzy_match(self, text: str) -> Optional[IntentResult]:
        words = text.split()
        best_score = 0.0
        best_cmd = ""
        best_desc = ""

        for kw, cmd in self.COMMAND_KEYWORDS.items():
            for w in words:
                if len(w) < 2:
                    continue
                ratio = _fuzzy_ratio(w, kw)
                if ratio >= 0.7 and ratio * 0.85 > best_score:
                    best_score = ratio * 0.85
                    best_cmd = cmd
                    best_desc = kw

        for s in self._db.get_all():
            for w in words:
                if len(w) < 2:
                    continue
                ratio = _fuzzy_ratio(w, s.command)
                if ratio >= 0.7 and ratio * 0.80 > best_score:
                    best_score = ratio * 0.80
                    best_cmd = s.command
                    best_desc = s.description

        if best_cmd and best_score >= self.FUZZY_THRESHOLD:
            _log.debug("[Intent] fuzzy 匹配成功 text=%r cmd=%s score=%.3f kw=%s", text, best_cmd, best_score, best_desc)
            return IntentResult(
                intent=best_cmd, command=best_cmd,
                confidence=best_score, matched_keyword=best_desc
            )
        _log.debug("[Intent] fuzzy 未达阈值 text=%r best_cmd=%s score=%.3f", text, best_cmd, best_score)
        return None

    def _spacy_match(self, text: str) -> Optional[IntentResult]:
        try:
            doc = self._nlp(text)
            best_score = 0.0
            best_cmd = None
            # 将每条数据库记录的命令与描述组合成文档，计算语义相似度
            for s in self._db.get_all():
                desc_doc = self._nlp(f"{s.command} {s.description}")
                score = doc.similarity(desc_doc)
                if score > best_score:
                    best_score = score
                    best_cmd = s.command
            if best_cmd and best_score >= 0.5:
                return IntentResult(
                    intent=best_cmd, command=best_cmd,
                    confidence=min(best_score, 0.75),
                    matched_keyword="spaCy 语义相似度"
                )
        except Exception:
            pass
        return None
