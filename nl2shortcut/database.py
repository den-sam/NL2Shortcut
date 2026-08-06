"""SQLite database manager for shortcut mappings."""

import os
import sqlite3
import threading
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from .models import Shortcut, Platform
from .windows10_shortcuts import MS_SHORTCUTS

# 调试日志：设置环境变量 NL2SHORTCUT_DEBUG=1 启用
_log = logging.getLogger(__name__)
if os.environ.get("NL2SHORTCUT_DEBUG"):
    _log.setLevel(logging.DEBUG)
else:
    _log.setLevel(logging.WARNING)  # 显式禁用 DEBUG，防止 root logger 配置泄漏

SEED_SQL = r"""
INSERT OR IGNORE INTO shortcuts (command, description, command_cn, description_cn, windows_key, mac_key, linux_key, category, application) VALUES
('copy', 'Copy', '复制', '复制选中内容', 'Ctrl+C', 'Cmd+C', 'Ctrl+C', '编辑', 'common'),
('paste', 'Paste', '粘贴', '粘贴剪贴板内容', 'Ctrl+V', 'Cmd+V', 'Ctrl+V', '编辑', 'common'),
('cut', 'Cut', '剪切', '剪切选中内容', 'Ctrl+X', 'Cmd+X', 'Ctrl+X', '编辑', 'common'),
('undo', 'Undo', '撤销', '撤销上一步操作', 'Ctrl+Z', 'Cmd+Z', 'Ctrl+Z', '编辑', 'common'),
('redo', 'Redo', '重做', '重做已撤销操作', 'Ctrl+Y', 'Cmd+Shift+Z', 'Ctrl+Shift+Z', '编辑', 'common'),
('delete', 'Delete', '删除', '删除选中内容', 'Delete', 'Delete', 'Delete', '编辑', 'common'),
('select_all', 'Select All', '全选', '全选当前内容', 'Ctrl+A', 'Cmd+A', 'Ctrl+A', '编辑', 'common'),
('find', 'Find', '查找', '查找文本', 'Ctrl+F', 'Cmd+F', 'Ctrl+F', '编辑', 'common'),
('replace', 'Find and Replace', '查找替换', '查找并替换文本', 'Ctrl+H', 'Cmd+Option+F', 'Ctrl+H', '编辑', 'common'),
('bold', 'Bold', '加粗', '加粗选中文字', 'Ctrl+B', 'Cmd+B', 'Ctrl+B', '编辑', 'common'),
('italic', 'Italic', '斜体', '斜体选中文字', 'Ctrl+I', 'Cmd+I', 'Ctrl+I', '编辑', 'common'),
('underline', 'Underline', '下划线', '下划线选中文字', 'Ctrl+U', 'Cmd+U', 'Ctrl+U', '编辑', 'common'),
('save', 'Save File', '保存', '保存当前文件', 'Ctrl+S', 'Cmd+S', 'Ctrl+S', '文件', 'common'),
('open', 'Open File', '打开', '打开文件', 'Ctrl+O', 'Cmd+O', 'Ctrl+O', '文件', 'common'),
('close', 'Close Window/Tab', '关闭', '关闭当前窗口/标签', 'Ctrl+W', 'Cmd+W', 'Ctrl+W', '文件', 'common'),
('new_file', 'New File', '新建', '新建文件', 'Ctrl+N', 'Cmd+N', 'Ctrl+N', '文件', 'common'),
('print', 'Print', '打印', '打印当前文档', 'Ctrl+P', 'Cmd+P', 'Ctrl+P', '文件', 'common'),
('save_as', 'Save As', '另存为', '另存为新的文件', 'Ctrl+Shift+S', 'Cmd+Shift+S', 'Ctrl+Shift+S', '文件', 'common'),
('align_center', 'Align Center', '居中', '文本居中对齐', 'Ctrl+E', 'Cmd+E', 'Ctrl+E', '编辑', 'common'),
('close_all', 'Close All', '全部关闭', '关闭所有窗口', 'Ctrl+Shift+W', 'Cmd+Option+W', 'Ctrl+Shift+W', '文件', 'common'),
('fullscreen', 'Toggle Fullscreen', '全屏', '切换全屏模式', 'F11', 'Cmd+Ctrl+F', 'F11', '视图', 'common'),
('minimize', 'Minimize All Windows', '最小化', '最小化所有窗口', 'Win+D', 'Cmd+M', 'Ctrl+Super+D', '视图', 'common'),
('zoom_in', 'Zoom In', '放大', '放大显示', 'Ctrl+Plus', 'Cmd+Plus', 'Ctrl+Plus', '视图', 'common'),
('zoom_out', 'Zoom Out', '缩小', '缩小显示', 'Ctrl+Minus', 'Cmd+Minus', 'Ctrl+Minus', '视图', 'common'),
('zoom_reset', 'Reset Zoom', '重置缩放', '重置为默认缩放', 'Ctrl+0', 'Cmd+0', 'Ctrl+0', '视图', 'common'),
('refresh', 'Refresh', '刷新', '刷新当前页面', 'F5', 'Cmd+R', 'F5', '视图', 'common'),
('hard_refresh', 'Hard Refresh', '强制刷新', '强制刷新(清除缓存)', 'Ctrl+F5', 'Cmd+Shift+R', 'Ctrl+Shift+R', '视图', 'browser'),
('switch_app', 'Switch Application', '切换应用', '切换应用程序窗口', 'Alt+Tab', 'Cmd+Tab', 'Alt+Tab', '导航', 'common'),
('switch_tab', 'Next Tab', '下一标签', '切换到下一个标签', 'Ctrl+Tab', 'Cmd+Option+Right', 'Ctrl+Tab', '导航', 'common'),
('switch_tab_prev', 'Previous Tab', '上一标签', '切换到上一个标签', 'Ctrl+Shift+Tab', 'Cmd+Option+Left', 'Ctrl+Shift+Tab', '导航', 'common'),
('go_to_start', 'Beginning of Line', '行首', '跳转到行首', 'Home', 'Cmd+Left', 'Home', '导航', 'common'),
('go_to_end', 'End of Line', '行尾', '跳转到行尾', 'End', 'Cmd+Right', 'End', '导航', 'common'),
('go_to_top', 'Top of Document', '文档开头', '跳转到文档开头', 'Ctrl+Home', 'Cmd+Up', 'Ctrl+Home', '导航', 'common'),
('go_to_bottom', 'Bottom of Document', '文档末尾', '跳转到文档末尾', 'Ctrl+End', 'Cmd+Down', 'Ctrl+End', '导航', 'common'),
('page_up', 'Page Up', '上翻页', '向上翻页', 'PageUp', 'Fn+Up', 'PageUp', '导航', 'common'),
('page_down', 'Page Down', '下翻页', '向下翻页', 'PageDown', 'Fn+Down', 'PageDown', '导航', 'common'),
('comment', 'Toggle Comment', '注释', '切换行注释', 'Ctrl+/', 'Cmd+/', 'Ctrl+/', '代码', 'vscode'),
('format_code', 'Format Code', '格式化', '格式化代码', 'Shift+Alt+F', 'Shift+Option+F', 'Ctrl+Shift+I', '代码', 'vscode'),
('rename', 'Rename Symbol', '重命名', '重命名符号', 'F2', 'F2', 'F2', '代码', 'vscode'),
('go_to_line', 'Go to Line', '跳转行', '跳转到指定行号', 'Ctrl+G', 'Cmd+G', 'Ctrl+G', '代码', 'vscode'),
('go_to_definition', 'Go to Definition', '转到定义', '跳转到符号定义', 'F12', 'F12', 'F12', '代码', 'vscode'),
('search_file', 'Search File by Name', '搜索文件', '按文件名搜索文件', 'Ctrl+P', 'Cmd+P', 'Ctrl+P', '代码', 'vscode'),
('command_palette', 'Command Palette', '命令面板', '打开命令面板', 'Ctrl+Shift+P', 'Cmd+Shift+P', 'Ctrl+Shift+P', '代码', 'vscode'),
('duplicate_line', 'Duplicate Line', '复制行', '复制当前行', 'Ctrl+Shift+D', 'Cmd+Shift+D', 'Ctrl+Shift+D', '代码', 'vscode'),
('move_line_up', 'Move Line Up', '上移行', '将当前行上移', 'Alt+Up', 'Option+Up', 'Alt+Up', '代码', 'vscode'),
('move_line_down', 'Move Line Down', '下移行', '将当前行下移', 'Alt+Down', 'Option+Down', 'Alt+Down', '代码', 'vscode'),
('lock_screen', 'Lock Screen', '锁屏', '锁定屏幕', 'Win+L', 'Cmd+Ctrl+Q', 'Ctrl+Alt+L', '系统', 'common'),
('task_manager', 'Task Manager', '任务管理器', '打开任务管理器', 'Ctrl+Shift+Esc', 'Cmd+Option+Esc', 'Ctrl+Alt+Delete', '系统', 'common'),
('screenshot', 'Screenshot (Region)', '截图', '区域截图', 'Win+Shift+S', 'Cmd+Shift+4', 'Shift+PrintScreen', '系统', 'common'),
('screenshot_full', 'Full Screenshot', '全屏截图', '全屏截图', 'PrintScreen', 'Cmd+Shift+3', 'PrintScreen', '系统', 'common'),
('run_dialog', 'Run Dialog', '运行', '打开运行对话框', 'Win+R', '', 'Alt+F2', '系统', 'common'),
('select', 'Select (Space)', '选择', '用空格勾选/选择项目', 'Space', 'Space', 'Space', '编辑', 'common');
INSERT OR IGNORE INTO synonyms (id, command_id, synonym) VALUES
(1,1,'duplicate'),
(2,2,'insert'),
(3,3,'move'),
(4,4,'revert'),
(5,4,'go back'),
(6,5,'forward'),
(7,6,'remove'),
(8,6,'erase'),
(9,7,'highlight all'),
(10,7,'choose all'),
(11,8,'search'),
(12,8,'look for'),
(13,9,'find and replace'),
(14,9,'substitute'),
(15,10,'thick'),
(16,10,'make bold'),
(17,11,'slant'),
(18,11,'cursive'),
(19,12,'underscore'),
(20,13,'store'),
(21,13,'save document'),
(22,14,'load'),
(23,14,'open document'),
(24,15,'shut'),
(25,15,'close tab'),
(26,16,'new'),
(27,16,'create'),
(28,17,'print document'),
(29,18,'save copy'),
(30,19,'close everything'),
(31,20,'full screen'),
(32,20,'maximize'),
(33,21,'hide all'),
(34,21,'show desktop'),
(35,22,'enlarge'),
(36,22,'bigger'),
(37,23,'shrink'),
(38,23,'smaller'),
(39,24,'normal size'),
(40,25,'reload'),
(41,25,'reload page'),
(42,26,'force refresh'),
(43,26,'clear cache'),
(44,27,'switch window'),
(45,27,'alt tab'),
(46,28,'next tab'),
(47,29,'previous tab'),
(48,30,'home'),
(49,30,'line start'),
(50,31,'end'),
(51,31,'line end'),
(52,32,'top'),
(53,32,'document start'),
(54,33,'bottom'),
(55,33,'document end'),
(56,34,'scroll up'),
(57,35,'scroll down'),
(58,36,'comment out'),
(59,36,'comment code'),
(60,37,'beautify'),
(61,37,'format'),
(62,38,'refactor'),
(63,38,'rename symbol'),
(64,39,'jump to line'),
(65,39,'goto line'),
(66,40,'jump to definition'),
(67,40,'definition'),
(68,41,'quick open'),
(69,41,'file search'),
(70,42,'show all commands'),
(71,43,'open terminal'),
(72,43,'toggle terminal'),
(73,44,'copy line'),
(74,44,'copy line down'),
(75,45,'shift up'),
(76,46,'shift down'),
(77,47,'lock'),
(78,47,'lock computer'),
(79,48,'force quit'),
(80,49,'snip'),
(81,49,'screen capture'),
(82,50,'full capture'),
(83,50,'screen shot full'),
(84,51,'run'),
(85,51,'launch'),
(86,51,'run command'),
(87,62,'centered text'),
(88,62,'centered paragraph'),
(89,62,'text center'),
(90,62,'align center'),
(91,62,'居中对齐'),
(92,62,'文字居中'),
(93,62,'居中文本'),
(94,62,'文本居中'),
(95,52,'空格'),
(96,52,'勾选'),
(97,52,'space'),
(98,52,'select'),
(99,52,'选中');
"""


class DatabaseManager:
    """Manages shortcut mappings stored in SQLite."""

    # ═══ 字段迁移 (v0.4.2: 中英双语支持) ═══
    MIGRATIONS = [
        "ALTER TABLE shortcuts ADD COLUMN command_cn TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE shortcuts ADD COLUMN description_cn TEXT NOT NULL DEFAULT ''",
    ]

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            config_dir = Path.home() / ".nl2shortcut"
            config_dir.mkdir(parents=True, exist_ok=True)
            db_path = config_dir / "shortcuts.db"
        self.db_path = db_path
        # 模块级单例连接 + Lock（遵循工程约束：避免每次操作新建连接）
        self._conn_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        # get_all 内存缓存 + 失效标志
        self._all_cache: Optional[List[Shortcut]] = None
        self._all_cache_category: Optional[str] = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """返回单例 SQLite 连接（线程安全）。

        首次调用时创建连接并设置 PRAGMA，后续复用同一连接。
        写操作通过 _conn_lock 串行化，避免 WAL 模式下的写冲突。
        """
        if self._conn is None:
            with self._conn_lock:
                if self._conn is None:
                    _log.debug("[DB] 首次创建连接 db=%s", self.db_path)
                    conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA foreign_keys=ON")
                    self._conn = conn
        return self._conn

    def _invalidate_cache(self) -> None:
        """置脏 get_all 缓存（写操作后调用）。"""
        _log.debug("[DB] 缓存失效（写操作触发）")
        self._all_cache = None
        self._all_cache_category = None

    def _init_db(self) -> None:
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS shortcuts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    windows_key TEXT NOT NULL DEFAULT '',
                    mac_key TEXT NOT NULL DEFAULT '',
                    linux_key TEXT NOT NULL DEFAULT '',
                    command_cn TEXT NOT NULL DEFAULT '',
                    description_cn TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '通用',
                    application TEXT NOT NULL DEFAULT 'common',
                    frequency INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS synonyms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command_id INTEGER NOT NULL,
                    synonym TEXT NOT NULL,
                    FOREIGN KEY (command_id) REFERENCES shortcuts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_synonyms_synonym
                    ON synonyms(synonym);
                CREATE INDEX IF NOT EXISTS idx_shortcuts_command
                    ON shortcuts(command);
            """)
            conn.commit()
            # 执行字段迁移 (v0.4.2: 中英双语支持)
            cursor = conn.cursor()
            for sql in self.MIGRATIONS:
                try:
                    cursor.execute(sql)
                except sqlite3.OperationalError:
                    pass  # 列已存在
            conn.commit()
            # 为已有数据填充中文翻译
            self._migrate_cn_data(conn)
            # ═══ 接入微软官方 Windows 10 键盘快捷方式 ═══
            # 幂等：基于 command 唯一约束，已存在则跳过（INSERT OR IGNORE）
            self._seed_ms_shortcuts(conn)
            self._seed_ms_synonyms(conn)
        finally:
            pass  # 单例连接不关闭

    def reimport_ms_shortcuts(self) -> int:
        """一次性重导入：清理旧 ms_% 数据后重新接入（用于数据更新）。

        幂等安全：先删除所有 ms_% 记录，再重新插入当前 MS_SHORTCUTS。
        """
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            # 删除旧 ms 记录的同义词与本体
            cur.execute(
                "DELETE FROM synonyms WHERE command_id IN "
                "(SELECT id FROM shortcuts WHERE command LIKE 'ms_%')")
            cur.execute("DELETE FROM shortcuts WHERE command LIKE 'ms_%'")
            conn.commit()
            n = self._seed_ms_shortcuts(conn)
            self._seed_ms_synonyms(conn)
            return n
        finally:
            pass  # 单例连接不关闭
        self._invalidate_cache()

    def _seed_ms_shortcuts(self, conn) -> int:
        """将微软官方 Windows 10 键盘快捷方式接入库（幂等，可重复调用）。

        MS_SHORTCUTS 元组格式与 SEED_SQL 的 shortcuts 表一致:
        (command, description, command_cn, description_cn,
         windows_key, mac_key, linux_key, category, application)
        """
        cursor = conn.cursor()
        n = 0
        for row in MS_SHORTCUTS:
            try:
                cursor.execute(
                    "INSERT OR IGNORE INTO shortcuts "
                    "(command, description, command_cn, description_cn, "
                    " windows_key, mac_key, linux_key, category, application) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(row),
                )
                if cursor.rowcount > 0:
                    n += 1
            except sqlite3.Error:
                continue
        conn.commit()
        return n

    # 高价值 MS 快捷键同义词，用于消除与通用命令（如"打开"→Ctrl+O）的歧义
    MS_SYNONYMS = {
        "ms_win_e":      ["文件资源管理器", "资源管理器", "此电脑", "我的电脑", "explorer",
                          "打开文件资源管理器", "打开资源管理器"],
        "ms_win_i":      ["设置", "windows设置", "系统设置", "settings", "打开设置"],
        "ms_win_r":      ["运行", "运行命令", "运行对话框", "run", "打开运行", "打开运行对话框"],
        "ms_win_d":      ["显示桌面", "隐藏桌面", "显示和隐藏桌面", "桌面"],
        "ms_win_dot":    ["表情符号", "表情", "emoji", "表情面板", "打开表情符号面板"],
        "ms_win_ctrl_d": ["虚拟桌面", "新建桌面", "新建虚拟桌面", "打开虚拟桌面"],
        "ms_win_t":      ["任务栏", "聚焦任务栏", "taskbar", "将焦点移动到任务栏"],
        "ms_ctrl_m":     ["标记模式", "进入标记模式", "mark mode"],
        "ms_win_x":      ["快捷菜单", "高级用户菜单", "winx"],
    }

    def _seed_ms_synonyms(self, conn) -> int:
        """为 MS 快捷键补充同义词（幂等）。"""
        cursor = conn.cursor()
        n = 0
        for command, syns in self.MS_SYNONYMS.items():
            cur = cursor.execute(
                "SELECT id FROM shortcuts WHERE command=?", (command,))
            row = cur.fetchone()
            if row is None:
                continue
            cid = row[0]
            for syn in syns:
                try:
                    cursor.execute(
                        "INSERT OR IGNORE INTO synonyms (command_id, synonym) "
                        "VALUES (?, ?)", (cid, syn))
                    if cursor.rowcount > 0:
                        n += 1
                except sqlite3.Error:
                    continue
        conn.commit()
        return n

    def _migrate_cn_data(self, conn):
        """Migrate existing rows with Chinese translations (v0.4.2)."""
        cn_map = {
            'copy': ('复制', '复制选中内容'),
            'paste': ('粘贴', '粘贴剪贴板内容'),
            'cut': ('剪切', '剪切选中内容'),
            'undo': ('撤销', '撤销上一步操作'),
            'redo': ('重做', '重做已撤销操作'),
            'delete': ('删除', '删除选中内容'),
            'select_all': ('全选', '全选当前内容'),
            'find': ('查找', '查找文本'),
            'replace': ('查找替换', '查找并替换文本'),
            'bold': ('加粗', '加粗选中文字'),
            'italic': ('斜体', '斜体选中文字'),
            'underline': ('下划线', '下划线选中文字'),
            'save': ('保存', '保存当前文件'),
            'open': ('打开', '打开文件'),
            'close': ('关闭', '关闭当前窗口/标签'),
            'new_file': ('新建', '新建文件'),
            'print': ('打印', '打印当前文档'),
            'save_as': ('另存为', '另存为新的文件'),
            'close_all': ('全部关闭', '关闭所有窗口'),
            'fullscreen': ('全屏', '切换全屏模式'),
            'minimize': ('最小化', '最小化所有窗口'),
            'zoom_in': ('放大', '放大显示'),
            'zoom_out': ('缩小', '缩小显示'),
            'zoom_reset': ('重置缩放', '重置为默认缩放'),
            'refresh': ('刷新', '刷新当前页面'),
            'hard_refresh': ('强制刷新', '强制刷新(清除缓存)'),
            'switch_app': ('切换应用', '切换应用程序窗口'),
            'switch_tab': ('下一标签', '切换到下一个标签'),
            'switch_tab_prev': ('上一标签', '切换到上一个标签'),
            'go_to_start': ('行首', '跳转到行首'),
            'go_to_end': ('行尾', '跳转到行尾'),
            'go_to_top': ('文档开头', '跳转到文档开头'),
            'go_to_bottom': ('文档末尾', '跳转到文档末尾'),
            'page_up': ('上翻页', '向上翻页'),
            'page_down': ('下翻页', '向下翻页'),
            'comment': ('注释', '切换行注释'),
            'format_code': ('格式化', '格式化代码'),
            'rename': ('重命名', '重命名符号'),
            'go_to_line': ('跳转行', '跳转到指定行号'),
            'go_to_definition': ('转到定义', '跳转到符号定义'),
            'search_file': ('搜索文件', '按文件名搜索文件'),
            'command_palette': ('命令面板', '打开命令面板'),
            'terminal': ('终端', '切换终端面板'),
            'duplicate_line': ('复制行', '复制当前行'),
            'move_line_up': ('上移行', '将当前行上移'),
            'move_line_down': ('下移行', '将当前行下移'),
            'lock_screen': ('锁屏', '锁定屏幕'),
            'task_manager': ('任务管理器', '打开任务管理器'),
            'screenshot': ('截图', '区域截图'),
            'screenshot_full': ('全屏截图', '全屏截图'),
            'run_dialog': ('运行', '打开运行对话框'),
        }
        cursor = conn.cursor()
        for cmd, (cmd_cn, desc_cn) in cn_map.items():
            cursor.execute(
                "UPDATE shortcuts SET command_cn=?, description_cn=? "
                "WHERE command=? AND (command_cn='' OR command_cn IS NULL)",
                (cmd_cn, desc_cn, cmd)
            )
        conn.commit()

    def seed_database(self) -> int:
        """Populate with initial data. Returns count of shortcuts."""
        conn = self._get_conn()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM shortcuts")
            cnt = cursor.fetchone()["cnt"]
            if cnt > 0:
                _log.debug("[DB] seed_database 跳过（已有 %d 条）", cnt)
                return 0
            conn.executescript(SEED_SQL)
            conn.commit()
            cursor.execute("SELECT COUNT(*) as cnt FROM shortcuts")
            total = cursor.fetchone()["cnt"]
            _log.debug("[DB] seed_database 完成，共插入 %d 条", total)
            return total
        finally:
            pass  # 单例连接不关闭

    def get_all(self, category: Optional[str] = None) -> List[Shortcut]:
        # 内存缓存：首次查询后缓存结果，写操作时通过 _invalidate_cache() 置脏
        if self._all_cache is not None and self._all_cache_category == category:
            _log.debug("[DB] get_all 缓存命中 cat=%s rows=%d", category, len(self._all_cache))
            return self._all_cache

        conn = self._get_conn()
        try:
            if category:
                rows = conn.execute(
                    "SELECT * FROM shortcuts WHERE category=? ORDER BY command",
                    (category,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM shortcuts ORDER BY category, command"
                ).fetchall()
            result = [self._row_to_shortcut(r) for r in rows]
            self._all_cache = result
            self._all_cache_category = category
            _log.debug("[DB] get_all 缓存未命中，重新查询 cat=%s rows=%d", category, len(result))
            return result
        finally:
            pass  # 单例连接不关闭

    def get_by_command(self, command: str) -> Optional[Shortcut]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM shortcuts WHERE command=?",
                (command,)
            ).fetchone()
            found = self._row_to_shortcut(row) if row else None
            _log.debug("[DB] get_by_command cmd=%s hit=%s", command, bool(found))
            return found
        finally:
            pass  # 单例连接不关闭

    def search(self, keyword: str) -> List[Shortcut]:
        conn = self._get_conn()
        try:
            pattern = f"%{keyword}%"
            rows = conn.execute("""
                SELECT DISTINCT s.* FROM shortcuts s
                LEFT JOIN synonyms sy ON s.id = sy.command_id
                WHERE s.command LIKE ?
                   OR s.description LIKE ?
                   OR sy.synonym LIKE ?
                ORDER BY s.frequency DESC, s.command
            """, (pattern, pattern, pattern)).fetchall()
            result = [self._row_to_shortcut(r) for r in rows]
            _log.debug("[DB] search kw=%r rows=%d", keyword, len(result))
            return result
        finally:
            pass  # 单例连接不关闭

    def find_by_synonym(self, text: str) -> Optional[Shortcut]:
        conn = self._get_conn()
        try:
            row = conn.execute("""
                SELECT s.* FROM shortcuts s
                JOIN synonyms sy ON s.id = sy.command_id
                WHERE sy.synonym = ?
                LIMIT 1
            """, (text,)).fetchone()
            found = self._row_to_shortcut(row) if row else None
            _log.debug("[DB] find_by_synonym text=%r hit=%s", text, bool(found))
            return found
        finally:
            pass  # 单例连接不关闭

    def increment_frequency(self, command: str) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE shortcuts SET frequency = frequency + 1 WHERE command=?",
                (command,)
            )
            conn.commit()
        finally:
            pass  # 单例连接不关闭
        _log.debug("[DB] increment_frequency cmd=%s", command)
        # frequency 变化不影响 get_all 结果（频率在结果排序中非关键字段），不置脏

    def add_shortcut(self, shortcut: Shortcut) -> bool:
        conn = self._get_conn()
        try:
            conn.execute("""
                INSERT INTO shortcuts
                    (command, description, command_cn, description_cn,
                     windows_key, mac_key, linux_key,
                     category, application)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                shortcut.command,
                shortcut.description,
                shortcut.command_cn,
                shortcut.description_cn,
                shortcut.windows_key,
                shortcut.mac_key,
                shortcut.linux_key,
                shortcut.category,
                shortcut.application,
            ))
            conn.commit()
            _log.debug("[DB] add_shortcut 成功 cmd=%s win=%s", shortcut.command, shortcut.windows_key)
            return True
        except sqlite3.IntegrityError:
            _log.debug("[DB] add_shortcut 冲突（命令已存在）cmd=%s", shortcut.command)
            return False
        finally:
            pass  # 单例连接不关闭
        self._invalidate_cache()

    def update_shortcut(self, shortcut: Shortcut) -> bool:
        if shortcut.id is None:
            _log.debug("[DB] update_shortcut 拒绝（id=None）cmd=%s", shortcut.command)
            return False
        conn = self._get_conn()
        try:
            conn.execute("""
                UPDATE shortcuts SET
                    command=?, description=?, windows_key=?, mac_key=?,
                    linux_key=?, category=?, application=?
                WHERE id=?
            """, (
                shortcut.command, shortcut.description,
                shortcut.windows_key, shortcut.mac_key,
                shortcut.linux_key, shortcut.category,
                shortcut.application, shortcut.id,
            ))
            conn.commit()
            changed = conn.total_changes > 0
            _log.debug("[DB] update_shortcut id=%s cmd=%s changed=%s", shortcut.id, shortcut.command, changed)
            return changed
        finally:
            pass  # 单例连接不关闭
        self._invalidate_cache()

    def get_stats(self) -> Tuple[int, List[Tuple[str, str, int]]]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(frequency), 0) as total FROM shortcuts"
            ).fetchone()
            total = row["total"]
            top = conn.execute(
                "SELECT command, description, frequency FROM shortcuts "
                "WHERE frequency > 0 ORDER BY frequency DESC LIMIT 10"
            ).fetchall()
            return total, [
                (r["command"], r["description"], r["frequency"]) for r in top
            ]
        finally:
            pass  # 单例连接不关闭

    def reset_frequency(self) -> None:
        conn = self._get_conn()
        try:
            conn.execute("UPDATE shortcuts SET frequency = 0")
            conn.commit()
        finally:
            pass  # 单例连接不关闭

    @staticmethod
    def _row_to_shortcut(row: sqlite3.Row) -> Shortcut:
        return Shortcut(
            id=row["id"],
            command=row["command"],
            description=row["description"],
            command_cn=row["command_cn"] if "command_cn" in row.keys() else "",
            description_cn=row["description_cn"] if "description_cn" in row.keys() else "",
            windows_key=row["windows_key"],
            mac_key=row["mac_key"],
            linux_key=row["linux_key"],
            category=row["category"],
            application=row["application"],
            frequency=row["frequency"],
        )
