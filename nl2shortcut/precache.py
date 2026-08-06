"""PrecachedShortcutMap — 预编译的「意图 → 键位」内存表（方向 D 的核心模块）。

背景
────
`_try_shortcut_lookup` 在每次「单快捷键」操作时都要走三步：
  1. IntentEngine.recognize()  —— 遍历 COMMAND_KEYWORDS (200+ 条) + 模糊匹配
  2. DatabaseManager.get_by_command() —— 每次开 SQLite 连接查盘
  3. Platform.detect() —— 系统调用

这三类「查找开销」对 *同一台机器* 是**完全确定的**：
  - 意图 "复制" 永远映射到 command "copy"
  - command "copy" 在当前平台永远映射到 "Ctrl+C"

因此可以在进程启动时把这三步「预编译」成一张纯内存表：
  - `_intent_index` : 规范化意图词 → command   (O(1) 命中)
  - `_key_index`    : command       → 平台键位   (O(1) 命中)

权威数据来源
────────────
预编译表**直接以整个快捷键库为准**（微软官方 MS_SHORTCUTS，200+ 操作），
覆盖快捷键库所对应的所有操作；并额外支持「键位组合字符串 → 操作」的直接映射
（输入 "ctrl+c" 即可映射执行 Ctrl+C）。

来源优先级（后者覆盖前者）：
  1. MS_SHORTCUTS 全量（权威完整库，含 mac/linux 键位）
  2. 运行时 DB（若有更精确数据）
  3. _BUILTIN_KEYS（最终兜底）

_BUILTIN_KEYS 由 `_CORE_KEYS`（人工维护的语义命令，如 copy / task_view，
带 mac/linux 键位）+ **快捷键库全量命令**在模块导入时合并而成，
因此「快捷键库里的所有命令都是内置标准命令」：即便 MS_SHORTCUTS 导入失败、
DB 为空，全部 200 条库命令仍能通过纯常量表 O(1) 命中。
同名冲突时核心语义命令优先；`_KEY_TO_CANON` 另记录「键位 → 代表命令」，
使同一键位的多条重复条目（ms_win_tab / ms_win_tab_2）在反查时结果唯一。

之后任何一次简单操作都走 O(1) 内存查表，永不碰 IntentEngine / SQLite / Platform.detect()，
把「第一次」和「第 N 次」的单快捷键操作都压到 ~1ms（与 cache_hit 持平）。

线程安全
──────
build() 在构造时调用一次；运行时查询均为只读、无锁（GIL 保护下的 dict 读取是原子的）。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .models import Platform


def _norm_key(k: str) -> str:
    """规范化键位字符串：小写、去空格。'Ctrl+C' -> 'ctrl+c'。"""
    return (k or "").strip().lower().replace(" ", "")


def _canon_key(k: str) -> str:
    """键位显示规范化：每个片段首字母大写，保持 '+' 连接。

    'ctrl+C' -> 'Ctrl+C'，'win+r' -> 'Win+R'，'shift+alt+f' -> 'Shift+Alt+F'。
    用于统一来自 MS_SHORTCUTS（全小写）与 SEED_SQL（已规范）的键位显示风格。
    """
    if not k:
        return k
    parts = k.split("+")
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # 单字符或已有大写首字母则保持，否则首字母大写
        if len(p) == 1:
            out.append(p.upper())
        else:
            out.append(p[0].upper() + p[1:].lower())
    return "+".join(out)


class PrecachedShortcutMap:
    """预编译的快捷键内存表。

    用法
    ----
        db = DatabaseManager(...)
        precache = PrecachedShortcutMap.build(db)

        # 自然语言意图 → 键位
        key = precache.lookup("复制")          # -> "Ctrl+C"
        # 键位组合字符串 → 键位（直接映射操作）
        key = precache.lookup("ctrl+c")        # -> "Ctrl+C"
        key = precache.lookup_command("copy")  # -> "Ctrl+C"
    """

    # 永远不该走「单快捷键直发」通道的 command（复合/视觉/鼠标）
    _COMPOSITE_COMMANDS: Set[str] = frozenset({
        "__composite__", "__plan__", "__composite",
        "left_click", "select",  # 走鼠标/空间选择通道，不走键位
    })

    # ── 核心语义命令 → 各平台键位（人工维护，语义名 + 跨平台）──
    # 这一层的价值在于「语义命令名」（copy / task_view …），中文意图词绑定于此，
    # 且提供 mac/linux 键位（MS 库只有 Windows 键位）。
    # 格式: command -> (windows_key, mac_key, linux_key)
    _CORE_KEYS: Dict[str, tuple] = {
        "copy":         ("Ctrl+C", "Cmd+C", "Ctrl+C"),
        "paste":        ("Ctrl+V", "Cmd+V", "Ctrl+V"),
        "cut":          ("Ctrl+X", "Cmd+X", "Ctrl+X"),
        "undo":         ("Ctrl+Z", "Cmd+Z", "Ctrl+Z"),
        "redo":         ("Ctrl+Y", "Cmd+Shift+Z", "Ctrl+Shift+Z"),
        "delete":       ("Delete", "Delete", "Delete"),
        "select_all":   ("Ctrl+A", "Cmd+A", "Ctrl+A"),
        "find":         ("Ctrl+F", "Cmd+F", "Ctrl+F"),
        "replace":      ("Ctrl+H", "Cmd+Option+F", "Ctrl+H"),
        "bold":         ("Ctrl+B", "Cmd+B", "Ctrl+B"),
        "italic":       ("Ctrl+I", "Cmd+I", "Ctrl+I"),
        "underline":    ("Ctrl+U", "Cmd+U", "Ctrl+U"),
        "save":         ("Ctrl+S", "Cmd+S", "Ctrl+S"),
        "open":         ("Ctrl+O", "Cmd+O", "Ctrl+O"),
        "close":        ("Ctrl+W", "Cmd+W", "Ctrl+W"),
        "new_file":     ("Ctrl+N", "Cmd+N", "Ctrl+N"),
        "print":        ("Ctrl+P", "Cmd+P", "Ctrl+P"),
        "save_as":      ("Ctrl+Shift+S", "Cmd+Shift+S", "Ctrl+Shift+S"),
        "align_center": ("Ctrl+E", "Cmd+E", "Ctrl+E"),
        "close_all":    ("Ctrl+Shift+W", "Cmd+Option+W", "Ctrl+Shift+W"),
        "fullscreen":   ("F11", "Cmd+Ctrl+F", "F11"),
        "minimize":     ("Win+D", "Cmd+M", "Ctrl+Super+D"),
        "zoom_in":      ("Ctrl+Plus", "Cmd+Plus", "Ctrl+Plus"),
        "zoom_out":     ("Ctrl+Minus", "Cmd+Minus", "Ctrl+Minus"),
        "zoom_reset":   ("Ctrl+0", "Cmd+0", "Ctrl+0"),
        "refresh":      ("F5", "Cmd+R", "F5"),
        "switch_app":   ("Alt+Tab", "Cmd+Tab", "Alt+Tab"),
        "switch_tab":   ("Ctrl+Tab", "Cmd+Option+Right", "Ctrl+Tab"),
        "comment":      ("Ctrl+/", "Cmd+/", "Ctrl+/"),
        "format_code":  ("Shift+Alt+F", "Shift+Option+F", "Ctrl+Shift+I"),
        "rename":       ("F2", "F2", "F2"),
        "go_to_line":   ("Ctrl+G", "Cmd+G", "Ctrl+G"),
        "command_palette": ("Ctrl+Shift+P", "Cmd+Shift+P", "Ctrl+Shift+P"),
        "duplicate_line":  ("Ctrl+Shift+D", "Cmd+Shift+D", "Ctrl+Shift+D"),
        "lock_screen":  ("Win+L", "Cmd+Ctrl+Q", "Ctrl+Alt+L"),
        "task_manager": ("Ctrl+Shift+Esc", "Cmd+Option+Esc", "Ctrl+Alt+Delete"),
        "screenshot":   ("Win+Shift+S", "Cmd+Shift+4", "Shift+PrintScreen"),
        "run_dialog":   ("Win+R", "", "Alt+F2"),
        "task_view":    ("Win+Tab", "Ctrl+Up", "Super+S"),
        "ms_win_e":     ("Win+E", "Cmd+Shift+O", "Super+O"),
    }

    # ── 内置标准命令全表 = 核心语义命令 + 快捷键库全量 ──────────────────
    # 由 _build_builtin_keys() 在类定义时一次性生成，使「快捷键库里的所有命令」
    # 都成为内置标准命令：即便 MS_SHORTCUTS 导入失败、DB 为空，
    # 200 条库命令仍可通过 _BUILTIN_KEYS 常量兜底 O(1) 命中。
    #
    # 命名规则：_CORE_KEYS 的语义命令名优先，库命令不覆盖同名核心命令。
    # 覆盖范围：库里**每个** command 都会登记（含 ms_win_tab_2 这类重复条目），
    #           保证「按命令名查键位」永不落空。
    # 高风险键（ctrl+alt+delete、win+L 等）按你的选择照常保留。
    _BUILTIN_KEYS: Dict[str, tuple] = {}

    # 规范化键位 → 该键位的**代表命令**（同键位多条时取首个）。
    # 用于反向「键位 → 命令」查询时给出唯一确定的结果，实现键位维度去重。
    _KEY_TO_CANON: Dict[str, str] = {}

    @classmethod
    def _build_builtin_keys(cls, core: Dict[str, tuple]):
        """把快捷键库全量并入内置标准命令表。

        返回 (merged, key_to_canon)：
          - merged:       command → (win, mac, linux)，库中每个命令名都在表内
          - key_to_canon: 规范化键位 → 代表命令（键位维度去重）
        """
        merged: Dict[str, tuple] = dict(core)
        key_to_canon: Dict[str, str] = {}
        # 核心语义命令先占位为各自键位的代表命令
        for c, v in core.items():
            if v and v[0]:
                key_to_canon.setdefault(_norm_key(v[0]), c)

        try:
            from .windows10_shortcuts import MS_SHORTCUTS
        except Exception:
            return merged, key_to_canon

        for row in MS_SHORTCUTS:
            # (command, desc, desc_cn, desc_cn2, win, mac, linux, cat, app)
            if len(row) < 7:
                continue
            cmd = (row[0] or "").strip().lower()
            win_key = (row[4] or "").strip()
            if not cmd or not win_key:
                continue
            # 键位维度去重：同一键位只认首个 command 为代表
            key_to_canon.setdefault(_norm_key(win_key), cmd)
            if cmd in merged:
                continue  # 核心语义命令优先，不被库覆盖
            merged[cmd] = (
                _canon_key(win_key),
                _canon_key((row[5] or "").strip()),
                _canon_key((row[6] or "").strip()),
            )
        return merged, key_to_canon

    def __init__(self) -> None:
        # 规范化意图词（小写、去空格） → command
        self._intent_index: Dict[str, str] = {}
        # command → 平台键位（构建时按当前平台定稿）
        self._key_index: Dict[str, str] = {}
        # 反向：command → 是否可用（有键位且非 composite）
        self._ok_commands: Set[str] = set()
        # 按长度降序排列的关键词列表（用于子串快速匹配）
        self._keywords_sorted: List[str] = []
        self._platform: Platform = Platform.detect()
        self._size_intents: int = 0
        self._size_commands: int = 0
        self._built: bool = False

    # ── 构建 ─────────────────────────────────────────────────────────────

    @classmethod
    def build(cls, db, intent_engine=None) -> "PrecachedShortcutMap":
        """从快捷键库（MS_SHORTCUTS 全量）+ DB + 意图引擎预编译整张表。

        Args:
            db: DatabaseManager 实例（已 seed，可选；为 None 时仅用 MS_SHORTCUTS + 内置）。
            intent_engine: 可选 IntentEngine，用于复用其 COMMAND_KEYWORDS。

        返回值：构建完成的 PrecachedShortcutMap（即使 DB 为空也返回可用实例）。
        """
        self = cls()
        plat = self._platform = Platform.detect()

        key_of = {Platform.WINDOWS: 0, Platform.MACOS: 1, Platform.LINUX: 2}

        # ── 1) command → 键位：加载整张快捷键库（MS_SHORTCUTS 全量）──
        # 这是「快捷键库所对应的所有操作」的权威来源，覆盖 200+ 操作。
        try:
            from .windows10_shortcuts import MS_SHORTCUTS
            for row in MS_SHORTCUTS:
                # 元组格式: (command, desc, desc_cn, desc_cn2, win, mac, linux, cat, app)
                if len(row) < 7:
                    continue
                cmd = (row[0] or "").strip()
                if not cmd or cmd in self._COMPOSITE_COMMANDS:
                    continue
                wk, mk, lk = row[4], row[5], row[6]
                key = {Platform.WINDOWS: wk, Platform.MACOS: mk, Platform.LINUX: lk}.get(plat, "")
                if not key:
                    continue
                self._key_index[cmd] = key
                self._ok_commands.add(cmd)
        except Exception:
            pass

        # 2) DB 覆盖（运行时数据优先）
        try:
            if db is not None:
                for sc in db.get_all():
                    cmd = (sc.command or "").strip()
                    if not cmd or cmd in self._COMPOSITE_COMMANDS:
                        continue
                    key = sc.get_key(plat)
                    if not key:
                        continue
                    self._key_index[cmd] = key
                    self._ok_commands.add(cmd)
        except Exception:
            pass

        # 3) 内置保底层（最终兜底，保证零数据环境核心命令可用）
        for cmd, (wk, mk, lk) in cls._BUILTIN_KEYS.items():
            key = {Platform.WINDOWS: wk, Platform.MACOS: mk, Platform.LINUX: lk}.get(plat, "")
            if key and cmd not in self._key_index:
                self._key_index[cmd] = key
                self._ok_commands.add(cmd)

        # ── 4) 意图词 → command ──
        # 4a) 每个 MS/DB command 的「键位组合字符串」直接作为意图词
        #     （实现「输入 ctrl+c 即可映射执行」）
        #     同一键位可能对应多个 command（ms_win_tab / ms_win_tab_2 / task_view），
        #     优先绑定 _KEY_TO_CANON 里的代表命令，保证结果稳定且语义最佳。
        for nk, canon in cls._KEY_TO_CANON.items():
            if nk and canon in self._key_index and nk not in self._intent_index:
                self._intent_index[nk] = canon
        for cmd, key in self._key_index.items():
            nk = _norm_key(key)
            if nk and nk not in self._intent_index:
                self._intent_index[nk] = cmd

        # 4b) 复用 IntentEngine.COMMAND_KEYWORDS（中/英/拼音/按键组合 → 标准命令名）
        #     标准命令名（copy/paste…）可能不在 MS_SHORTCUTS（MS 用 ms_ctrl_c 形式），
        #     用「键位桥接」自动对齐到 MS 中相同键位的 command。
        try:
            if intent_engine is not None and hasattr(intent_engine, "COMMAND_KEYWORDS"):
                # 先建 规范化键位 → command 反查表（仅 MS/DB 层，用于桥接）
                key_to_cmd: Dict[str, str] = {}
                for c, k in self._key_index.items():
                    nk = _norm_key(k)
                    if nk and nk not in key_to_cmd:
                        key_to_cmd[nk] = c
                for kw, std_cmd in intent_engine.COMMAND_KEYWORDS.items():
                    if std_cmd in self._COMPOSITE_COMMANDS:
                        continue
                    norm = kw.strip().lower()
                    if not norm:
                        continue
                    # 该标准命令已有键位 → 直接注册
                    if std_cmd in self._ok_commands:
                        self._intent_index.setdefault(norm, std_cmd)
                        continue
                    # 否则：用该标准命令在内置表里的键位，桥接到 MS 中同键位的 command
                    builtin = cls._BUILTIN_KEYS.get(std_cmd)
                    if builtin:
                        nk = _norm_key(builtin[key_of[plat]])
                        bridged = key_to_cmd.get(nk)
                        if bridged and bridged in self._ok_commands:
                            self._intent_index.setdefault(norm, bridged)
                            continue
                    # 仍找不到 → 若内置表有该命令（兜底），直接注册
                    if std_cmd in cls._BUILTIN_KEYS:
                        self._intent_index.setdefault(norm, std_cmd)
        except Exception:
            pass

        # 4c) 反向补充：command 自身作为意图词（"copy" → "copy" → 键位）
        for cmd in self._ok_commands:
            norm = cmd.strip().lower()
            if norm and norm not in self._intent_index:
                self._intent_index[norm] = cmd

        # 4d) 内置意图词兜底（即便 IntentEngine 不可用，"复制"/"Ctrl+C" 也能命中）
        _BUILTIN_INTENT = {
            "复制": "copy", "粘贴": "paste", "剪切": "cut", "撤销": "undo",
            "重做": "redo", "删除": "delete", "全选": "select_all", "查找": "find",
            "搜索": "find", "替换": "replace", "加粗": "bold", "斜体": "italic",
            "下划线": "underline", "保存": "save", "关闭": "close",
            "新建": "new_file", "打印": "print", "另存为": "save_as", "居中": "align_center",
            "全部关闭": "close_all", "全屏": "fullscreen", "最小化": "minimize",
            "放大": "zoom_in", "缩小": "zoom_out", "刷新": "refresh",
            "切换应用": "switch_app", "下一标签": "switch_tab", "注释": "comment",
            "格式化": "format_code", "重命名": "rename", "命令面板": "command_palette",
            "复制行": "duplicate_line", "锁屏": "lock_screen", "任务管理器": "task_manager",
            "截图": "screenshot", "运行": "run_dialog",
            "任务视图": "task_view", "多任务视图": "task_view", "时间线": "task_view",
            "打开任务视图": "task_view", "虚拟桌面": "task_view",
            "资源管理器": "ms_win_e", "打开资源管理器": "ms_win_e",
            "文件资源管理器": "ms_win_e", "此电脑": "ms_win_e",
        }
        for kw, std_cmd in _BUILTIN_INTENT.items():
            # 通过键位桥接找实际 command（优先 MS），找不到则用内置兜底命令
            target = None
            if std_cmd in self._ok_commands:
                target = std_cmd
            else:
                builtin = cls._BUILTIN_KEYS.get(std_cmd)
                if builtin:
                    nk = _norm_key(builtin[key_of[plat]])
                    target = next((c for c, k in self._key_index.items()
                                   if _norm_key(k) == nk), std_cmd)
            if target:
                self._intent_index.setdefault(kw.strip().lower(), target)

        # 按长度降序建立子串匹配索引（长词优先，避免短词被长词包含时误截断）
        # 过滤：只保留中文词（>=2 字）、长度>=3 的英文/拼音、键位组合字符串
        filtered: List[str] = []
        import re as _re_kw
        for kw in self._intent_index:
            if len(kw) >= 2 and _re_kw.search(r'[\u4e00-\u9fff]', kw):
                filtered.append(kw)
            elif "+" in kw:
                filtered.append(kw)  # 键位组合字符串（ctrl+c 等）
            elif len(kw) >= 3:
                filtered.append(kw)
        self._keywords_sorted = sorted(set(filtered), key=len, reverse=True)

        # 统一键位显示风格（'ctrl+C' -> 'Ctrl+C'），仅影响展示，不影响匹配
        for cmd in list(self._key_index.keys()):
            self._key_index[cmd] = _canon_key(self._key_index[cmd])

        self._size_intents = len(self._intent_index)
        self._size_commands = len(self._key_index)
        self._built = True
        return self

    # ── 查询 ─────────────────────────────────────────────────────────────

    def lookup(self, intent: str) -> Optional[str]:
        """规范化意图词 → 平台键位。未命中返回 None。

        支持自然语言（"复制"）与键位组合字符串（"ctrl+c"）两种输入。
        intent 会被 lower + strip 后查表，命中即返回键位。
        """
        if not intent:
            return None
        norm = intent.strip().lower()
        cmd = self._intent_index.get(norm)
        if cmd is None:
            # 容错：用户可能带空格输入 "Ctrl + C"
            cmd = self._intent_index.get(_norm_key(intent))
        if cmd is None:
            return None
        return self._key_index.get(cmd)

    def lookup_full(self, intent: str) -> Optional[Tuple[str, str]]:
        """规范化意图词 → (command, 平台键位)。未命中返回 None。

        比 lookup() 多返回 command，便于调用方记录/写缓存。
        """
        if not intent:
            return None
        norm = intent.strip().lower()
        cmd = self._intent_index.get(norm)
        if cmd is None:
            cmd = self._intent_index.get(_norm_key(intent))
        if cmd is None:
            return None
        key = self._key_index.get(cmd)
        if key is None:
            return None
        return (cmd, key)

    def lookup_contains(self, intent: str) -> Optional[Tuple[str, str, str]]:
        """子串查找：返回 intent 中包含的最长意图词 → (command, 键位, 匹配词)。

        用于处理 "打开任务管理器" 这种带前缀的自然语言：
        精确匹配不到 "打开任务管理器"，但包含 "任务管理器" → task_manager。
        只匹配长度>=2 的中文词、键位组合、或长度>=3 的英文/拼音，降低误命中。
        """
        if not intent or not self._keywords_sorted:
            return None
        text = intent.strip().lower()
        for kw in self._keywords_sorted:
            if kw in text:
                cmd = self._intent_index.get(kw)
                if cmd is not None:
                    key = self._key_index.get(cmd)
                    if key:
                        return (cmd, key, kw)
        return None

    def lookup_command(self, command: str) -> Optional[str]:
        """command → 平台键位。未命中返回 None。"""
        if not command:
            return None
        return self._key_index.get(command.strip().lower())

    def has_command(self, command: str) -> bool:
        """该 command 是否在预编译表中且可用（有键位、非 composite）。"""
        if not command:
            return False
        return command.strip().lower() in self._ok_commands

    @property
    def is_built(self) -> bool:
        return self._built

    @property
    def stats(self) -> Dict[str, object]:
        return {
            "intents": self._size_intents,
            "commands": self._size_commands,
            "platform": self._platform.value,
        }


# 模块导入时一次性把「快捷键库里的所有命令」并入内置标准命令表。
# 放在类体外是因为类定义期间无法调用自身的 classmethod。
(
    PrecachedShortcutMap._BUILTIN_KEYS,
    PrecachedShortcutMap._KEY_TO_CANON,
) = PrecachedShortcutMap._build_builtin_keys(PrecachedShortcutMap._CORE_KEYS)
