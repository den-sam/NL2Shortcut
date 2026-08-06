"""nl2shortcut GUI - VS Code Light+ 风格界面

自然语言 → 快捷键 · 中英文双引擎 · 51 条内置快捷键
左侧导航栏 (20%) + 右侧内容区 (80%)
"""

# 类别英文键 → 中文名称（数据库 category 列已统一存储为中文）
CAT_EN_TO_CN = {
    'edit': '编辑', 'file': '文件', 'view': '视图',
    'navigate': '导航', 'code': '代码', 'system': '系统',
    'general': '通用', 'windows_logo': 'Windows徽标键',
    'file_explorer': '文件资源管理器', 'command_prompt': '命令提示符',
    'virtual_desktop': '虚拟桌面', 'taskbar': '任务栏',
    'settings': '设置', 'dialog': '对话框'
}

import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    from PyQt5.QtWidgets import (
        QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
        QLabel, QLineEdit, QPushButton, QCheckBox,
        QTableWidget, QTableWidgetItem, QTextEdit, QHeaderView,
        QMessageBox, QSplitter, QGroupBox, QGridLayout, QStatusBar,
        QMenuBar, QMenu, QAction, QProgressBar, QStyleFactory, QStyle,
        QStyledItemDelegate,
        QComboBox, QShortcut as QtShortcut, QShortcut, QFrame, QStackedWidget,
        QScrollArea, QSizePolicy, QSpacerItem, QToolButton,
        QDialog, QDialogButtonBox,
    )
    from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QSize
    from PyQt5.QtGui import QFont, QIcon, QKeySequence, QColor, QPalette
except ImportError:
    print("PyQt5 not found. Install: pip install PyQt5")
    sys.exit(1)

from nl2shortcut import ShortcutAgent
from nl2shortcut.models import Platform, ExecutionResult


# ═══════════════════════════════════════════════════════════════════════
# 输入文字意图识别 —— "输入：XXX" / "输入『XXX』" 等
# ═══════════════════════════════════════════════════════════════════════
# 支持的包围符号对（开/闭）
_TYPE_QUOTE_PAIRS = [
    ('「', '」'), ('『', '』'), ('"', '"'), ("'", "'"),
    ('（', '）'), ('(', ')'), ('【', '】'), ('[', ']'),
    ('“', '”'), ('‘', '’'),
]

# "输入：XXX" / "输入:XXX" —— 冒号分隔，内容到行尾（支持多行）
_TYPE_COLON_RE = re.compile(r'^输入\s*[:：]\s*(.+)$', re.DOTALL)


def _parse_type_intent(text: str) -> Optional[str]:
    """从用户输入中识别"输入：XXX" / "输入『XXX』"格式，返回要输入的文字。

    支持的格式：
        输入：你好          → 你好
        输入:你好           → 你好
        输入：你好世界      → 你好世界
        输入『你好』        → 你好
        输入「你好」        → 你好
        输入"你好"          → 你好
        输入【你好】        → 你好
        输入（你好）        → 你好

    不触发的例子（避免误识别）：
        输入框 / 输入栏 / 输入法  —— 无冒号也无包围符号
    """
    text = text.strip()
    if not text:
        return None

    # 模式1: 冒号分隔（支持多行内容）
    m = _TYPE_COLON_RE.match(text)
    if m:
        content = m.group(1).strip()
        if content:
            return content

    # 模式2: 包围符号（必须配对闭合）
    for open_ch, close_ch in _TYPE_QUOTE_PAIRS:
        prefix = f'输入{open_ch}'
        if text.startswith(prefix) and text.endswith(close_ch) and len(text) > len(prefix):
            content = text[len(prefix):-len(close_ch)].strip()
            if content:
                return content

    return None


# ═══════════════════════════════════════════════════════════════════════
# VS Code Light+ 主题色彩系统
# ═══════════════════════════════════════════════════════════════════════

# 背景层级 (由深到浅)
CL_ACTIVITY   = "#333333"   # Activity Bar / 左侧图标栏
CL_SIDEBAR    = "#F3F3F3"   # 侧边导航栏背景
CL_BG_MAIN    = "#FFFFFF"   # 主内容区背景
CL_BG_PANEL   = "#F8F8F8"   # 面板/卡片背景
CL_BG_INPUT   = "#FFFFFF"   # 输入框背景
CL_BG_HOVER   = "#E8E8E8"   # 悬停态

# 品牌色 (VS Code 蓝)
CL_ACCENT     = "#0078D4"   # 主色
CL_ACCENT_H   = "#1A8CDC"   # 悬停
CL_ACCENT_P   = "#0066B4"   # 按下
CL_ACCENT_BG  = "#E4F0FD"   # 浅蓝背景

# 语义色
CL_SUCCESS    = "#388A34"
CL_WARNING    = "#DBA11A"
CL_DANGER     = "#D73A49"
CL_INFO       = "#0078D4"

# 文字
CL_TEXT       = "#1E1E1E"   # 主文字
CL_TEXT_DIM   = "#616161"   # 次要文字
CL_TEXT_MUTED = "#999999"   # 弱化文字

# 边框
CL_BORDER      = "#E0E0E0"
CL_BORDER_FOC  = "#0078D4"


# ═══════════════════════════════════════════════════════════════════════
# 全局 QSS 样式表
# ═══════════════════════════════════════════════════════════════════════
VSCODE_QSS = f"""
/* ═════════ 全局基础 ═════════ */
QMainWindow {{
    background-color: {CL_BG_MAIN};
}}
QWidget {{
    background-color: {CL_BG_MAIN};
    color: {CL_TEXT};
    font-family: "微软雅黑", "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 13px;
}}

/* ═════════ 侧边栏导航 ═════════ */
#sidebar {{
    background-color: {CL_SIDEBAR};
    border-right: 1px solid {CL_BORDER};
    min-width: 200px;
    max-width: 240px;
}}
#sidebarTitle {{
    font-size: 11px;
    font-weight: 600;
    color: {CL_TEXT_DIM};
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 16px 20px 4px 20px;
}}
#sidebarSubtitle {{
    font-size: 12px;
    color: {CL_TEXT};
    font-weight: 600;
    padding: 2px 20px 16px 20px;
}}

/* 导航按钮 */
#navButton {{
    background: transparent;
    color: {CL_TEXT_DIM};
    border: none;
    border-radius: 0px;
    padding: 8px 20px;
    text-align: left;
    font-size: 13px;
    min-height: 32px;
}}
#navButton:hover {{
    background: {CL_BG_HOVER};
    color: {CL_TEXT};
}}
#navButton#active {{
    background: {CL_ACCENT_BG};
    color: {CL_TEXT};
    font-weight: 600;
}}

/* 分隔线 */
#navSeparator {{
    border: none;
    border-top: 1px solid {CL_BORDER};
    margin: 8px 20px;
}}

/* 侧边栏底部 */
#sidebarFooter {{
    padding: 12px 20px;
    color: {CL_TEXT_MUTED};
    font-size: 11px;
    line-height: 1.6;
}}
#sidebarFooterStatus {{
    color: {CL_ACCENT};
    font-size: 11px;
    font-weight: 600;
    padding: 0 20px 4px 20px;
}}

/* ═════════ 内容区域 ═════════ */
#contentArea {{
    background-color: {CL_BG_MAIN};
}}
#pageTitle {{
    font-size: 18px;
    font-weight: 600;
    color: {CL_TEXT};
    padding: 0;
}}
#pageSubtitle {{
    font-size: 12px;
    color: {CL_TEXT_DIM};
    padding: 0;
}}

/* ═════════ 卡片容器 ═════════ */
#card {{
    background-color: {CL_BG_PANEL};
    border: none;
    border-radius: 8px;
    padding: 24px;
}}
#cardFlat {{
    background-color: {CL_BG_MAIN};
    border: 1px solid {CL_BORDER};
    border-radius: 6px;
}}

/* ═════════ 输入框 ═════════ */
QLineEdit {{
    background: {CL_BG_INPUT};
    border: 1px solid {CL_BORDER};
    border-radius: 4px;
    padding: 8px 12px;
    font-size: 14px;
    color: {CL_TEXT};
    selection-background-color: {CL_ACCENT};
    selection-color: white;
}}
QLineEdit:focus {{
    border-color: {CL_BORDER_FOC};
    border-width: 2px;
    padding: 7px 11px;
}}
QLineEdit::placeholder {{
    color: {CL_TEXT_MUTED};
}}

/* 主输入区（多行 QTextEdit，完全融入卡片，无任何边框） */
#mainInput {{
    background: transparent;
    border: none;
    border-radius: 0;
    padding: 4px 0;
    font-size: 14px;
    color: {CL_TEXT};
    selection-background-color: {CL_ACCENT};
    selection-color: white;
}}
#mainInput:focus {{
    border: none;
}}

/* ═════════ 按钮 ═════════ */
QPushButton {{
    background: {CL_ACCENT};
    color: white;
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 600;
}}
QPushButton:hover {{
    background: {CL_ACCENT_H};
}}
QPushButton:pressed {{
    background: {CL_ACCENT_P};
}}
QPushButton:disabled {{
    background: {CL_BORDER};
    color: {CL_TEXT_MUTED};
}}

/* 次要按钮 */
#btnSecondary {{
    background: transparent;
    color: {CL_TEXT};
    border: 1px solid {CL_BORDER};
    font-weight: normal;
}}
#btnSecondary:hover {{
    background: {CL_BG_HOVER};
    color: {CL_TEXT};
}}
#btnSecondary:pressed {{
    background: {CL_BORDER};
}}

/* 危险按钮 */
#btnDanger {{
    background: transparent;
    color: {CL_DANGER};
    border: 1px solid {CL_BORDER};
    font-weight: normal;
}}
#btnDanger:hover {{
    background: rgba(215, 58, 73, 0.08);
    border-color: {CL_DANGER};
}}

/* ═════════ 复选框 ═════════ */
QCheckBox {{
    color: {CL_TEXT_DIM};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {CL_BORDER};
    border-radius: 3px;
    background: {CL_BG_INPUT};
}}
QCheckBox::indicator:checked {{
    background: {CL_ACCENT};
    border-color: {CL_ACCENT};
}}

/* ═════════ 组合框 ═════════ */
QComboBox {{
    background: {CL_BG_INPUT};
    border: 1px solid {CL_BORDER};
    border-radius: 4px;
    padding: 6px 10px;
    color: {CL_TEXT};
}}
QComboBox:hover {{ border-color: {CL_TEXT_MUTED}; }}
QComboBox:focus {{ border-color: {CL_ACCENT}; }}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
QComboBox QAbstractItemView {{
    background: {CL_BG_MAIN};
    border: 1px solid {CL_BORDER};
    border-radius: 4px;
    selection-background-color: {CL_ACCENT_BG};
    selection-color: {CL_TEXT};
    color: {CL_TEXT};
    outline: none;
}}

/* ═════════ 表格 ═════════ */
QTableWidget {{
    background: {CL_BG_MAIN};
    border: 1px solid {CL_BORDER};
    border-radius: 4px;
    gridline-color: {CL_BG_PANEL};
    color: {CL_TEXT};
    alternate-background-color: {CL_BG_PANEL};
}}
QTableWidget::item {{
    padding: 6px 10px;
    border-bottom: 1px solid transparent;
}}
QTableWidget::item:selected {{
    background: {CL_ACCENT_BG};
    color: {CL_TEXT};
}}
QHeaderView::section {{
    background: {CL_SIDEBAR};
    color: {CL_TEXT_DIM};
    border: none;
    border-bottom: 1px solid {CL_BORDER};
    padding: 8px 10px;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
}}

/* ═════════ 文本编辑 ═════════ */
QTextEdit {{
    background: {CL_BG_MAIN};
    border: 1px solid {CL_BORDER};
    border-radius: 4px;
    color: {CL_TEXT};
    font-family: "Cascadia Code", "Consolas", "微软雅黑", monospace;
    font-size: 12px;
    padding: 10px;
    selection-background-color: {CL_ACCENT_BG};
}}

/* ═════════ 进度条 ═════════ */
QProgressBar {{
    border: none;
    border-radius: 3px;
    background: {CL_BG_PANEL};
    text-align: center;
    max-height: 4px;
}}
QProgressBar::chunk {{
    background: {CL_ACCENT};
    border-radius: 3px;
}}

/* ═════════ 菜单栏 ═════════ */
QMenuBar {{
    background: {CL_SIDEBAR};
    color: {CL_TEXT_DIM};
    border-bottom: 1px solid {CL_BORDER};
    padding: 2px 0;
}}
QMenuBar::item {{
    padding: 4px 10px;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background: {CL_BG_HOVER};
    color: {CL_TEXT};
}}
QMenu {{
    background: {CL_BG_MAIN};
    color: {CL_TEXT};
    border: 1px solid {CL_BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 28px 6px 12px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background: {CL_ACCENT_BG};
    color: {CL_TEXT};
}}
QMenu::separator {{
    height: 1px;
    background: {CL_BORDER};
    margin: 4px 8px;
}}

/* ═════════ 状态栏 ═════════ */
QStatusBar {{
    background: {CL_ACCENT};
    color: white;
    border: none;
    font-size: 12px;
    padding: 2px 0;
}}
QStatusBar QLabel {{
    color: rgba(255,255,255,0.9);
    font-size: 12px;
}}

/* ═════════ 滚动条 ═════════ */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: rgba(0,0,0,0.15);
    border-radius: 5px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{
    background: rgba(0,0,0,0.25);
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: rgba(0,0,0,0.15);
    border-radius: 5px;
    min-width: 20px;
}}
QScrollBar::handle:horizontal:hover {{
    background: rgba(0,0,0,0.25);
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

/* ═════════ 结果展示区 ═════════ */
#resultKey {{
    font-size: 24px;
    font-weight: 600;
    color: {CL_TEXT};
    padding: 8px 0;
}}
#resultDetail {{
    font-size: 13px;
    color: {CL_TEXT_DIM};
}}
#resultTime {{
    font-size: 11px;
    color: {CL_TEXT_MUTED};
}}

/* ═════════ 统计卡片 ═════════ */
#statCard {{
    background-color: {CL_BG_MAIN};
    border: 1px solid {CL_BORDER};
    border-radius: 6px;
}}
#statValue {{
    font-size: 24px;
    font-weight: 600;
    color: {CL_TEXT};
}}
#statLabel {{
    font-size: 11px;
    color: {CL_TEXT_MUTED};
    text-transform: uppercase;
    letter-spacing: 0.5px;
}}

/* ═════════ 标签/徽章 ═════════ */
#badge {{
    background: {CL_BG_PANEL};
    color: {CL_ACCENT};
    border: 1px solid {CL_BORDER};
    border-radius: 12px;
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 600;
}}
#badge:hover {{
    background: {CL_ACCENT_BG};
    border-color: {CL_ACCENT};
}}

/* ═════════ 分割线 ═════════ */
#sectionDivider {{
    background: {CL_BORDER};
    max-height: 1px;
    min-height: 1px;
    border: none;
    margin: 4px 0;
}}
"""


# ═══════════════════════════════════════════════════════════════════════
# 侧边栏导航按钮
# ═══════════════════════════════════════════════════════════════════════
class SidebarButton(QPushButton):
    """侧边栏导航按钮,VS Code 风格 - 左侧蓝底激活态"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setObjectName("navButton")
        self.setCursor(Qt.PointingHandCursor)
        self.setCheckable(True)

    def set_active(self, active: bool):
        if active:
            self.setProperty("active", True)
        else:
            self.setProperty("active", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


# ═══════════════════════════════════════════════════════════════════════
# 执行面板
# ═══════════════════════════════════════════════════════════════════════
class ExecutePanel(QWidget):
    def __init__(self, agent: ShortcutAgent, history_callback, parent=None):
        super().__init__(parent)
        self._agent = agent
        self._history_cb = history_callback
        self._exact_map: dict = {}  # command -> Shortcut, built on init
        self._pending_action = None
        self._setup_ui()
        self._install_shortcuts()
        self._build_exact_map()

    def _setup_ui(self):
        self.setObjectName("contentArea")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(20)

        # 页头
        page_title = QLabel("你想做什么？")
        page_title.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {CL_TEXT};")
        page_subtitle = QLabel(
            "输入你想做的事，程序自动帮你完成"
        )
        page_subtitle.setStyleSheet(f"font-size: 13px; color: {CL_TEXT_DIM};")
        layout.addWidget(page_title)
        layout.addWidget(page_subtitle)
        layout.addSpacing(8)

        # 输入卡片
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(16)

        # 输入区域（QTextEdit 多行，融入卡片）
        self._input = QTextEdit()
        self._input.setObjectName("mainInput")
        self._input.setPlaceholderText(
            '输入你想做的事，比如：复制、保存、打开浏览器…\n'
            '要输入文字，用「输入：XXX」或「输入『XXX』」格式  (Ctrl+Enter 完成)'
        )
        self._input.setMinimumHeight(80)
        self._input.setMaximumHeight(120)
        self._input.setFont(QFont("微软雅黑", 14))
        card_layout.addWidget(self._input)

        # 执行按钮（卡片底部）
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_exec = QPushButton("去做 (Ctrl+Enter)")
        self._btn_exec.setMinimumHeight(36)
        self._btn_exec.setMinimumWidth(130)
        self._btn_exec.setCursor(Qt.PointingHandCursor)
        self._btn_exec.setStyleSheet(f"""
            QPushButton {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 10px 24px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            QPushButton:pressed {{ background: {CL_ACCENT_P}; }}
        """)
        btn_row.addWidget(self._btn_exec)
        card_layout.addLayout(btn_row)

        # ── 实时键位预览 ──
        self._preview_label = QLabel("")
        self._preview_label.setAlignment(Qt.AlignLeft)
        self._preview_label.setStyleSheet(
            f"font-size: 12px; color: {CL_ACCENT}; padding: 2px 0; min-height: 20px;"
        )
        self._preview_label.setVisible(False)
        card_layout.addWidget(self._preview_label)
        self._input.textChanged.connect(lambda: self._on_input_changed(self._input.toPlainText()))

        # 选项行
        opt_row = QHBoxLayout()
        self._dry_run = QCheckBox("先看看，不实际操作")
        self._dry_run.setChecked(True)
        self._auto_clear = QCheckBox("执行后自动清空")
        opt_row.addWidget(self._dry_run)
        opt_row.addWidget(self._auto_clear)
        opt_row.addStretch()
        card_layout.addLayout(opt_row)

        # 置信度条
        self._confidence_bar = QProgressBar()
        self._confidence_bar.setMaximum(100)
        self._confidence_bar.setVisible(False)
        card_layout.addWidget(self._confidence_bar)

        # 结果展示区
        self._result_widget = QFrame()
        self._result_widget.setVisible(False)
        self._result_widget.setStyleSheet(f"""
            background: {CL_BG_PANEL};
            border: 1px solid {CL_BORDER};
            border-radius: 6px;
            padding: 16px;
        """)
        result_layout = QVBoxLayout(self._result_widget)
        result_layout.setAlignment(Qt.AlignCenter)
        result_layout.setSpacing(4)

        self._result_key = QLabel("")
        self._result_key.setObjectName("resultKey")
        self._result_key.setAlignment(Qt.AlignCenter)
        self._result_key.setStyleSheet(f"""
            font-size: 22px; font-weight: 600; color: {CL_TEXT}; padding: 4px 0;
        """)
        result_layout.addWidget(self._result_key)

        self._result_detail = QLabel("")
        self._result_detail.setObjectName("resultDetail")
        self._result_detail.setAlignment(Qt.AlignCenter)
        result_layout.addWidget(self._result_detail)

        self._result_time = QLabel("")
        self._result_time.setObjectName("resultTime")
        self._result_time.setAlignment(Qt.AlignCenter)
        result_layout.addWidget(self._result_time)

        # ── 确认 / 取消按钮 ──
        self._confirm_row = QHBoxLayout()
        self._confirm_row.addStretch()
        self._btn_cancel = QPushButton("取消")
        self._btn_cancel.setObjectName("btnSecondary")
        self._btn_cancel.setCursor(Qt.PointingHandCursor)
        self._btn_cancel.setFixedWidth(80)
        self._btn_cancel.clicked.connect(self._dismiss_result)
        self._confirm_row.addWidget(self._btn_cancel)

        self._btn_confirm = QPushButton("确定")
        self._btn_confirm.setCursor(Qt.PointingHandCursor)
        self._btn_confirm.setFixedWidth(80)
        self._btn_confirm.setStyleSheet(f"""
            QPushButton {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 20px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            QPushButton:pressed {{ background: {CL_ACCENT_P}; }}
        """)
        self._btn_confirm.clicked.connect(self._do_confirm)
        self._confirm_row.addWidget(self._btn_confirm)
        self._confirm_widget = QWidget()
        self._confirm_widget.setLayout(self._confirm_row)
        self._confirm_widget.setVisible(False)
        card_layout.addWidget(self._confirm_widget)

        card_layout.addWidget(self._result_widget)
        layout.addWidget(card, stretch=1)

        # 快捷提示
        tips_row = QHBoxLayout()
        tips_label = QLabel("试试:")
        tips_label.setStyleSheet(f"color: {CL_TEXT_MUTED}; font-size: 12px;")
        tips_row.addWidget(tips_label)
        for example in ["复制", "保存", "全屏", "撤销", "截图", "输入：你好世界", "输入『张三』"]:
            tag = QLabel(example)
            tag.setObjectName("badge")
            tag.setCursor(Qt.PointingHandCursor)
            def make_handler(text):
                return lambda: self._fill_example(text)
            tag.mousePressEvent = lambda e, t=example: self._fill_example(t)
            tips_row.addWidget(tag)
        tips_row.addStretch()
        layout.addLayout(tips_row)
        layout.addStretch()

        self._btn_exec.clicked.connect(self._do_execute)
        # QTextEdit 无 returnPressed，Enter 换行，Ctrl+Enter 执行（全局快捷键）

    def _fill_example(self, text: str):
        self._input.setText(text)
        self._input.setFocus()

    def _build_exact_map(self):
        """Build {command: Shortcut} and {command_cn: Shortcut} for O(1) exact matching."""
        for s in self._agent.list_shortcuts():
            self._exact_map[s.command] = s
            if s.command_cn:
                self._exact_map[s.command_cn] = s

    def _on_input_changed(self, text: str):
        """Real-time preview: show mapped key when input matches a known command exactly."""
        text = text.strip()
        if not text:
            self._preview_label.setVisible(False)
            return
        s = self._exact_map.get(text)
        if s is None:
            self._preview_label.setVisible(False)
            return
        platform = Platform.detect()
        key = s.get_key(platform)
        label_text = f"→ {key}  ({s.description_cn or s.description})"
        self._preview_label.setText(label_text)
        self._preview_label.setVisible(True)

    def _do_execute(self):
        text = self._input.toPlainText().strip()
        if not text:
            return

        start = time.perf_counter()

        # ── 输入文字快速通道：识别 "输入：XXX" / "输入『XXX』" 等格式 ──
        # 命中后直接调用 agent.type_text()，绕过快捷键意图识别。
        type_content = _parse_type_intent(text)
        if type_content is not None:
            # 预览要输入的内容（截断显示，避免过长撑爆界面）
            preview_text = type_content if len(type_content) <= 40 else type_content[:37] + '…'
            # 保存待执行动作
            self._pending_action = ("type_text", type_content, text)
            # 构造预览结果
            platform = Platform.detect().value
            self._show_result(ExecutionResult(
                success=True,
                intent=f"输入文字（{len(type_content)} 字）",
                command="type",
                key_combination=preview_text,
                processing_time=time.perf_counter() - start,
                platform=platform,
                matched_keyword=text,
                dry_run=self._dry_run.isChecked(),
            ), text)
            self._confirm_widget.setVisible(True)
            return

        # ── 快速通道:精确匹配 ──
        shortcut = self._exact_map.get(text)
        if shortcut is not None:
            platform = Platform.detect()
            key = shortcut.get_key(platform)
            if not key:
                self._show_result(ExecutionResult(
                    success=False, intent=shortcut.command,
                    command=shortcut.command,
                    processing_time=time.perf_counter() - start,
                    platform=platform.value, matched_keyword=text,
                    error=f"No {platform.value} key mapping",
                ), text)
                return
            # Store pending action, show confirm buttons
            self._pending_action = ("shortcut", key, shortcut.command)
            self._show_result(ExecutionResult(
                success=True, intent=shortcut.command,
                command=shortcut.command, key_combination=key,
                processing_time=time.perf_counter() - start,
                platform=platform.value, matched_keyword=text,
                dry_run=self._dry_run.isChecked(),
            ), text)
            self._confirm_widget.setVisible(True)
            return

        # ── 常规通道:全链路意图识别 ──
        self._btn_exec.setEnabled(False)
        self._btn_exec.setText("识别中...")
        self._confidence_bar.setVisible(True)
        self._confidence_bar.setValue(30)

        result = self._agent.execute(text, dry_run=True)  # Always dry-run first
        self._show_result(result, text)
        self._btn_exec.setText("执行 (Enter)")
        self._btn_exec.setEnabled(True)

        if result.success and result.command != "unknown":
            # Store pending action, show confirm buttons
            self._pending_action = ("full", result, text)
            self._confirm_widget.setVisible(True)

    def _dismiss_result(self):
        """Cancel pending action, hide result and confirm buttons."""
        self._pending_action = None
        self._confirm_widget.setVisible(False)
        self._result_widget.setVisible(False)
        self._confidence_bar.setVisible(False)

    def _do_confirm(self):
        """Execute the stored pending action."""
        action = self._pending_action
        self._confirm_widget.setVisible(False)
        self._pending_action = None

        if action is None:
            return

        kind = action[0]
        if kind == "shortcut":
            _, key, command = action
            if not self._dry_run.isChecked():
                try:
                    self._agent.adapter.send_keys(key)
                    self._agent._db.increment_frequency(command)
                except Exception as e:
                    self._result_detail.setText(f"执行失败: {e}")
            if self._auto_clear.isChecked():
                self._input.clear()
        elif kind == "type_text":
            # 输入文字快速通道：直接调用 type_text，不经过快捷键意图识别
            _, content, _ = action
            if not self._dry_run.isChecked():
                try:
                    self._agent.type_text(content)
                    # 更新结果展示为"已输入"
                    self._result_key.setText(content if len(content) <= 40 else content[:37] + '…')
                    self._result_key.setStyleSheet(
                        f"font-size: 18px; font-weight: 600; color: {CL_SUCCESS}; padding: 4px 0;"
                    )
                    self._result_detail.setText(f"已输入 {len(content)} 个字符")
                except Exception as e:
                    self._result_detail.setText(f"输入失败: {e}")
            if self._auto_clear.isChecked():
                self._input.clear()
        elif kind == "full":
            _, result, text = action
            if not self._dry_run.isChecked():
                actual = self._agent.execute(text, dry_run=False)
                self._show_result(actual, text)
            if self._auto_clear.isChecked():
                self._input.clear()

    def _show_result(self, result: ExecutionResult, input_text: str):
        """Shared result display logic for both fast & slow paths."""
        is_fast = result.processing_time < 0.01  # sub-10ms = fast path
        if is_fast:
            self._confidence_bar.setVisible(True)
            self._confidence_bar.setValue(100)
            self._confidence_bar.setStyleSheet(
                f"QProgressBar::chunk {{ background: {CL_SUCCESS}; border-radius: 3px; }}"
            )
        elif result.success:
            self._confidence_bar.setVisible(True)
            self._confidence_bar.setValue(100)
            self._confidence_bar.setStyleSheet(
                f"QProgressBar::chunk {{ background: {CL_SUCCESS}; border-radius: 3px; }}"
            )
        else:
            self._confidence_bar.setVisible(True)
            self._confidence_bar.setValue(15)
            self._confidence_bar.setStyleSheet(
                f"QProgressBar::chunk {{ background: {CL_DANGER}; border-radius: 3px; }}"
            )

        self._result_widget.setVisible(True)
        if result.success:
            prefix = "预览 " if self._dry_run.isChecked() else ""
            fast_tag = " ⚡" if is_fast else ""
            self._result_key.setText(f"{prefix}{result.key_combination}{fast_tag}")
            self._result_key.setStyleSheet(
                f"font-size: 22px; font-weight: 600; color: {CL_TEXT}; padding: 4px 0;"
            )
            self._result_detail.setText(
                f"{result.intent or result.command}"
            )
        else:
            self._result_key.setText(result.error or "没听懂，换个说法试试")
            self._result_key.setStyleSheet(
                f"font-size: 16px; color: {CL_DANGER};"
            )
            self._result_detail.setText(
                f"{result.intent or ''}"
            )

        ms = f"{result.processing_time * 1000:.0f}ms"
        self._result_time.setText(f"用时 {ms}")

        status = "成功" if result.success else "失败"
        key = result.key_combination or result.error or ""
        self._history_cb(
            f"[{datetime.now().strftime('%H:%M:%S')}] [{status}] "
            f"「{input_text}」 → {key}  ({ms})"
        )

        if result.success and self._auto_clear.isChecked():
            self._input.clear()


# ═══════════════════════════════════════════════════════════════════════
# 快捷键库面板
# ═══════════════════════════════════════════════════════════════════════
    def _install_shortcuts(self):
        """Local panel shortcuts (active only on this page)."""
        S = QShortcut
        K = QKeySequence
        # Tab/Shift+Tab between input and checkboxes
        S(K("Tab"), self._input, context=Qt.WidgetWithChildrenShortcut).activated.connect(
            lambda: self._dry_run.setFocus())
        # Ctrl+.  focus dry-run checkbox
        S(K("Ctrl+."), self).activated.connect(
            lambda: self._dry_run.setFocus())
        # Ctrl+0  clear input
        S(K("Ctrl+0"), self).activated.connect(
            lambda: self._input.clear())
        # Escape  clear input
        S(K("Escape"), self, context=Qt.WidgetWithChildrenShortcut).activated.connect(
            lambda: self._input.clear() if self._input.hasFocus() else None)

class ShortcutsPanel(QWidget):
    """快捷键库面板。"""

    class _NoFocusDelegate(QStyledItemDelegate):
        """绘制单元格时去掉焦点虚线/实线边框，保持选中背景色。"""

        def paint(self, painter, option, index):
            option.state &= ~QStyle.State_HasFocus
            super().paint(painter, option, index)

    def __init__(self, agent: ShortcutAgent, parent=None):
        super().__init__(parent)
        self._agent = agent
        self._all_shortcuts: list = []
        self._lang_en = False  # False=中文, True=English
        self._setup_ui()
        self._install_shortcuts()
        self._load_all()

    def _setup_ui(self):
        self.setObjectName("contentArea")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(16)

        page_title = QLabel("快捷键库")
        page_title.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {CL_TEXT};")
        page_subtitle = QLabel("程序能听懂的所有操作")
        page_subtitle.setStyleSheet(f"font-size: 13px; color: {CL_TEXT_DIM};")
        layout.addWidget(page_title)
        layout.addWidget(page_subtitle)

        # 工具栏
        toolbar = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索快捷键...(命令 / 描述 / 按键)")
        self._search.setMinimumHeight(36)
        toolbar.addWidget(self._search, stretch=2)

        self._lang_btn = QPushButton("EN")
        self._lang_btn.setObjectName("btnSecondary")
        self._lang_btn.setToolTip("切换中英文显示")
        self._lang_btn.setCursor(Qt.PointingHandCursor)
        self._lang_btn.setFixedWidth(52)
        self._lang_btn.setFixedHeight(36)
        self._lang_btn.setStyleSheet("#btnSecondary { padding: 0 6px; font-size: 13px; }")
        self._lang_btn.clicked.connect(self._toggle_language)
        toolbar.addWidget(self._lang_btn)

        self._category_filter = QComboBox()
        self._category_filter.addItems([
            "全部类别", "edit--编辑", "file--文件", "view--视图",
            "navigate--导航", "code--代码", "system--系统",
            "general--通用", "windows_logo--Windows徽标键",
            "file_explorer--文件资源管理器", "command_prompt--命令提示符",
            "virtual_desktop--虚拟桌面", "taskbar--任务栏",
            "settings--设置", "dialog--对话框"
        ])
        self._category_filter.setMinimumHeight(36)
        toolbar.addWidget(self._category_filter)

        self._count_label = QLabel("")
        self._count_label.setObjectName("badge")
        toolbar.addWidget(self._count_label)
        layout.addLayout(toolbar)

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels([
            "命令", "描述", "Windows", "macOS", "类别"
        ])
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setSortingEnabled(True)
        self._table.setItemDelegate(self._NoFocusDelegate(self._table))

        h = self._table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)

        layout.addWidget(self._table)

        self._search.textChanged.connect(self._filter)
        self._category_filter.currentTextChanged.connect(self._filter)

    def _toggle_language(self):
        self._lang_en = not self._lang_en
        self._lang_btn.setText("中" if self._lang_en else "EN")
        # 搜索提示中英切换
        self._search.setPlaceholderText(
            "Search shortcuts... (command / description / key)" if self._lang_en
            else "搜索快捷键...(命令 / 描述 / 按键)"
        )
        # 分类筛选中英切换
        self._category_filter.blockSignals(True)
        current = self._category_filter.currentIndex()
        self._category_filter.clear()
        if self._lang_en:
            self._category_filter.addItems([
                "All", "edit", "file", "view",
                "navigate", "code", "system", "general",
                "windows_logo", "file_explorer", "command_prompt",
                "virtual_desktop", "taskbar", "settings", "dialog"
            ])
        else:
            self._category_filter.addItems([
                "全部类别", "edit--编辑", "file--文件", "view--视图",
                "navigate--导航", "code--代码", "system--系统"
            ])
        self._category_filter.setCurrentIndex(min(current, self._category_filter.count() - 1))
        self._category_filter.blockSignals(False)
        self._populate_table(self._all_shortcuts)

    def _load_all(self):
        self._all_shortcuts = self._agent.list_shortcuts()
        self._populate_table(self._all_shortcuts)

    def _filter(self):
        text = self._search.text().lower().strip()
        cat_text = self._category_filter.currentText()
        filtered = self._all_shortcuts
        # 中英分类词组: "全部类别" / "All"
        if cat_text not in ("全部类别", "All"):
            cat_key = cat_text.split("--")[0] if "--" in cat_text else cat_text
            cat = CAT_EN_TO_CN.get(cat_key, cat_key)
            filtered = [s for s in filtered if s.category == cat]
        if text:
            filtered = [
                s for s in filtered
                if text in s.command.lower()
                or text in s.description.lower()
                or text in s.command_cn.lower()
                or text in s.description_cn.lower()
                or text in s.windows_key.lower()
                or text in s.mac_key.lower()
            ]
        self._populate_table(filtered)

    def _populate_table(self, shortcuts):
        self._table.setRowCount(0)
        en = self._lang_en
        for s in shortcuts:
            row = self._table.rowCount()
            self._table.insertRow(row)
            # 命令: 中文优先(command_cn), 英文 fallback(command)
            cmd_text = s.command if en else (s.command_cn or s.command)
            desc_text = s.description if en else (s.description_cn or s.description)
            cmd_item = QTableWidgetItem(cmd_text)
            cmd_item.setFont(QFont("微软雅黑", -1, QFont.Bold))
            self._table.setItem(row, 0, cmd_item)
            self._table.setItem(row, 1, QTableWidgetItem(desc_text))
            self._table.setItem(row, 2, QTableWidgetItem(s.windows_key))
            self._table.setItem(row, 3, QTableWidgetItem(s.mac_key))
            # 类别显示：英文模式回退为英文键名，中文模式直接显示中文
            cat_display = CAT_EN_TO_CN.get(s.category, s.category) if en else s.category
            cat_item = QTableWidgetItem(cat_display)
            cat_item.setTextAlignment(Qt.AlignCenter)
            self._table.setItem(row, 4, cat_item)
        self._count_label.setText(f"{len(shortcuts)} 条")


# ═══════════════════════════════════════════════════════════════════════
# 执行历史面板
# ═══════════════════════════════════════════════════════════════════════
    def _install_shortcuts(self):
        """Local panel shortcuts."""
        S = QShortcut
        K = QKeySequence
        # Ctrl+N  focus the search box (placeholder for future "add new" dialog)
        S(K("Ctrl+N"), self).activated.connect(self._search.setFocus)
        # Ctrl+F  focus search
        S(K("Ctrl+F"), self).activated.connect(self._search.setFocus)
        # Ctrl+Shift+L  switch language
        S(K("Ctrl+Shift+L"), self).activated.connect(self._toggle_language)
        # Up/Down  navigate the table
        S(K("Up"), self, context=Qt.WidgetWithChildrenShortcut).activated.connect(
            lambda: self._move_row(-1))
        S(K("Down"), self, context=Qt.WidgetWithChildrenShortcut).activated.connect(
            lambda: self._move_row(1))

    def _move_row(self, delta: int):
        if not hasattr(self, "_table"):
            return
        cur = self._table.currentRow()
        new = max(0, min(self._table.rowCount() - 1, cur + delta))
        if new >= 0:
            self._table.selectRow(new)

class HistoryPanel(QWidget):
    MAX_LINES = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()
        self._install_shortcuts()

    def _setup_ui(self):
        self.setObjectName("contentArea")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(16)

        header_row = QHBoxLayout()
        page_title = QLabel("执行历史")
        page_title.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {CL_TEXT};")
        header_row.addWidget(page_title)
        header_row.addStretch()

        self._export_btn = QPushButton("导出日志")
        self._export_btn.setObjectName("btnSecondary")
        self._export_btn.setMaximumWidth(100)
        self._export_btn.setCursor(Qt.PointingHandCursor)
        header_row.addWidget(self._export_btn)

        self._clear_btn = QPushButton("清空记录")
        self._clear_btn.setObjectName("btnDanger")
        self._clear_btn.setMaximumWidth(100)
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        header_row.addWidget(self._clear_btn)

        layout.addLayout(header_row)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        layout.addWidget(self._log)

        self._clear_btn.clicked.connect(self._confirm_clear)
        self._export_btn.clicked.connect(self._export_log)

    def append(self, text: str):
        self._log.append(text)
        doc = self._log.document()
        if doc.blockCount() > self.MAX_LINES:
            cursor = self._log.textCursor()
            cursor.movePosition(cursor.Start)
            cursor.select(cursor.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()
            self._log.setTextCursor(cursor)

    def _export_log(self):
        path = Path.home() / "Desktop" / f"scut_日志_{datetime.now():%Y%m%d_%H%M%S}.txt"
        try:
            path.write_text(self._log.toPlainText(), encoding="utf-8")
            QMessageBox.information(self, "导出成功", f"日志已保存至:\n{path}")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))

    def _confirm_clear(self):
        """确认清空：弹出取消/确定对话框。"""
        if not self._log.toPlainText().strip():
            self._log.clear()
            return
        reply = QMessageBox.question(
            self,
            "确认清空",
            "确定清空所有执行历史记录？\n此操作不可撤销。",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply == QMessageBox.Ok:
            self._log.clear()


# ═══════════════════════════════════════════════════════════════════════
# 数据统计面板
# ═══════════════════════════════════════════════════════════════════════
    def _install_shortcuts(self):
        """Local panel shortcuts."""
        S = QShortcut
        K = QKeySequence
        # Delete  clear selected entry (or all if none selected)
        S(K("Delete"), self).activated.connect(self._delete_selected)
        # Ctrl+Shift+Delete  clear all
        S(K("Ctrl+Shift+Delete"), self).activated.connect(self._clear_all)
        # Ctrl+E  export log
        S(K("Ctrl+E"), self).activated.connect(self._export_log)
        # Ctrl+C  copy selected row
        S(K("Ctrl+C"), self).activated.connect(self._copy_selected)

    def _delete_selected(self):
        if hasattr(self, "_table") and self._table.rowCount():
            row = self._table.currentRow()
            if row >= 0:
                self._table.removeRow(row)

    def _clear_all(self):
        if hasattr(self, "_table"):
            self._table.setRowCount(0)

    def _copy_selected(self):
        from PyQt5.QtWidgets import QApplication
        if hasattr(self, "_table"):
            row = self._table.currentRow()
            if row >= 0:
                items = [self._table.item(row, c).text() if self._table.item(row, c) else "" for c in range(self._table.columnCount())]
                QApplication.clipboard().setText("\t".join(items))

class StatsPanel(QWidget):
    STAT_CARDS = [
        ("总执行次数", "total"),
        ("成功次数", "success"),
        ("失败次数", "failed"),
        ("成功率", "rate"),
        ("平均响应", "avg"),
        ("累计耗时", "total_time"),
    ]

    def __init__(self, agent: ShortcutAgent, parent=None):
        super().__init__(parent)
        self._agent = agent
        self._setup_ui()
        self._install_shortcuts()
        self.refresh()

    def _setup_ui(self):
        self.setObjectName("contentArea")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(20)

        header_row = QHBoxLayout()
        page_title = QLabel("数据统计")
        page_title.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {CL_TEXT};")
        header_row.addWidget(page_title)
        header_row.addStretch()

        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setObjectName("btnSecondary")
        self._refresh_btn.setMaximumWidth(80)
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        header_row.addWidget(self._refresh_btn)

        self._reset_btn = QPushButton("重置")
        self._reset_btn.setObjectName("btnDanger")
        self._reset_btn.setMaximumWidth(60)
        self._reset_btn.setCursor(Qt.PointingHandCursor)
        header_row.addWidget(self._reset_btn)

        layout.addLayout(header_row)

        # 指标卡片 (3x2 网格)
        grid = QGridLayout()
        grid.setSpacing(12)
        self._cards = {}

        for i, (label, key) in enumerate(self.STAT_CARDS):
            row, col = divmod(i, 3)
            card = QFrame()
            card.setObjectName("statCard")
            card.setFixedHeight(100)
            cl = QVBoxLayout(card)
            cl.setAlignment(Qt.AlignCenter)
            cl.setSpacing(4)

            val = QLabel("-")
            val.setStyleSheet(f"font-size: 24px; font-weight: 600; color: {CL_TEXT};")
            val.setAlignment(Qt.AlignCenter)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"font-size: 11px; color: {CL_TEXT_MUTED}; text-transform: uppercase;")
            lbl.setAlignment(Qt.AlignCenter)

            cl.addWidget(val)
            cl.addWidget(lbl)
            grid.addWidget(card, row, col)
            self._cards[key] = val

        layout.addLayout(grid)

        # 热榜
        top_group = QFrame()
        top_group.setObjectName("cardFlat")
        top_layout = QVBoxLayout(top_group)

        top_header = QLabel("常用命令排行")
        top_header.setStyleSheet(f"font-weight: 600; font-size: 14px; color: {CL_TEXT}; padding: 4px 0;")
        top_layout.addWidget(top_header)

        self._top_table = QTableWidget()
        self._top_table.setColumnCount(3)
        self._top_table.setHorizontalHeaderLabels(["命令", "描述", "使用次数"])
        self._top_table.setAlternatingRowColors(True)
        self._top_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._top_table.verticalHeader().setVisible(False)
        h = self._top_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        top_layout.addWidget(self._top_table)
        layout.addWidget(top_group)

        self._refresh_btn.clicked.connect(self.refresh)
        self._reset_btn.clicked.connect(self._do_reset)

    def refresh(self):
        stats = self._agent.get_stats()
        self._cards["total"].setText(str(stats.total_executions))
        self._cards["success"].setText(str(stats.successful))
        self._cards["failed"].setText(str(stats.failed))
        self._cards["rate"].setText(f"{stats.success_rate:.1f} %")
        self._cards["avg"].setText(f"{stats.avg_processing_time * 1000:.1f} ms")
        self._cards["total_time"].setText(f"{stats.total_processing_time:.2f} s")

        if stats.success_rate >= 95:
            self._cards["rate"].setStyleSheet(f"font-size: 24px; font-weight: 600; color: {CL_SUCCESS};")
        elif stats.success_rate >= 70:
            self._cards["rate"].setStyleSheet(f"font-size: 24px; font-weight: 600; color: {CL_WARNING};")
        else:
            self._cards["rate"].setStyleSheet(f"font-size: 24px; font-weight: 600; color: {CL_DANGER};")

        self._top_table.setRowCount(0)
        for cmd, desc, freq in stats.top_commands:
            row = self._top_table.rowCount()
            self._top_table.insertRow(row)
            cmd_item = QTableWidgetItem(cmd)
            cmd_item.setFont(QFont("微软雅黑", -1, QFont.Bold))
            self._top_table.setItem(row, 0, cmd_item)
            self._top_table.setItem(row, 1, QTableWidgetItem(desc))
            freq_item = QTableWidgetItem(str(freq))
            freq_item.setTextAlignment(Qt.AlignCenter)
            self._top_table.setItem(row, 2, freq_item)

    def _do_reset(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("重置统计")
        dlg.setMinimumWidth(360)
        dlg.setStyleSheet(f"""
            QDialog {{
                background: {CL_BG_MAIN};
            }}
            QLabel {{
                color: {CL_TEXT};
                font-size: 13px;
                padding: 20px 24px 12px 24px;
            }}
        """)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        msg = QLabel("确认清除所有执行日志和统计数据？此操作不可撤销。")
        msg.setWordWrap(True)
        layout.addWidget(msg)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(16, 12, 16, 16)
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("btnSecondary")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setFixedWidth(80)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            QPushButton:pressed {{ background: {CL_ACCENT_P}; }}
        """)
        ok_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

        if dlg.exec_() == QDialog.Accepted:
            self._agent.reset_stats()
            self.refresh()


# ═══════════════════════════════════════════════════════════════════════
# 工作流面板
# ═══════════════════════════════════════════════════════════════════════
    def _install_shortcuts(self):
        """Local panel shortcuts."""
        S = QShortcut
        K = QKeySequence
        S(K("Ctrl+R"), self).activated.connect(self.refresh if hasattr(self, "refresh") else (lambda: None))
        S(K("F5"), self).activated.connect(self.refresh if hasattr(self, "refresh") else (lambda: None))
        # Ctrl+Shift+R  reset stats (with confirmation)
        S(K("Ctrl+Shift+R"), self).activated.connect(self._confirm_reset)

    def _confirm_reset(self):
        from PyQt5.QtWidgets import QMessageBox
        r = QMessageBox.question(self, "重置统计", "确定清空所有执行统计？", QMessageBox.Yes | QMessageBox.No)
        if r == QMessageBox.Yes and hasattr(self, "_do_reset"):
            self._do_reset()

class WorkflowPanel(QWidget):
    def __init__(self, agent):
        super().__init__()
        self._agent = agent
        self._selected_wf: str = ""
        self._wf_cards: dict = {}   # name -> QFrame
        self._wf_checks: dict = {}  # name -> QCheckBox
        self._edit_mode: bool = False
        self._setup_ui()
        self._install_shortcuts()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(14)

        # ── 页头 ──
        hdr = QLabel("自动流程")
        hdr.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {CL_TEXT}; padding-bottom: 4px;")
        layout.addWidget(hdr)

        desc_row = QHBoxLayout()
        desc = QLabel("把多个步骤串起来自动执行")
        desc.setStyleSheet(f"color: {CL_TEXT_DIM}; padding-bottom: 4px;")
        desc_row.addWidget(desc)
        desc_row.addStretch()

        self._dry_check = QCheckBox("先看看，不实际操作")
        self._dry_check.setChecked(True)
        self._dry_check.setToolTip("勾选 → 只预览不实际执行；取消 → 真正发送按键/运行命令")
        desc_row.addWidget(self._dry_check)

        self._refresh_btn = QPushButton("🔄 刷新")
        self._refresh_btn.setObjectName("btnSecondary")
        self._refresh_btn.setCursor(Qt.PointingHandCursor)
        desc_row.addWidget(self._refresh_btn)
        layout.addLayout(desc_row)

        # ── 工作流卡片列表 ──
        list_header_row = QHBoxLayout()
        list_header = QLabel("📋 已保存的自动流程")
        list_header.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CL_TEXT}; padding-top: 4px;")
        list_header_row.addWidget(list_header)
        list_header_row.addStretch()

        # 全选（编辑模式时可见）
        self._select_all_cb = QCheckBox("全选")
        self._select_all_cb.setToolTip("勾选 / 取消全部工作流")
        self._select_all_cb.setCursor(Qt.PointingHandCursor)
        self._select_all_cb.setStyleSheet(f"color: {CL_TEXT_DIM}; font-size: 12px;")
        self._select_all_cb.stateChanged.connect(self._on_select_all)
        self._select_all_cb.setVisible(False)
        list_header_row.addWidget(self._select_all_cb)

        # 编辑模式切换按钮
        self._edit_toggle_btn = QPushButton("编辑")
        self._edit_toggle_btn.setObjectName("btnSecondary")
        self._edit_toggle_btn.setCursor(Qt.PointingHandCursor)
        self._edit_toggle_btn.setToolTip("进入编辑模式后可勾选并删除工作流")
        self._edit_toggle_btn.setFixedWidth(56)
        self._edit_toggle_btn.clicked.connect(self._toggle_edit_mode)
        list_header_row.addWidget(self._edit_toggle_btn)

        # 删除按钮（编辑模式时可见）
        self._delete_btn = QPushButton("删除")
        self._delete_btn.setObjectName("btnDanger")
        self._delete_btn.setCursor(Qt.PointingHandCursor)
        self._delete_btn.setToolTip("删除勾选的工作流")
        self._delete_btn.setFixedWidth(56)
        self._delete_btn.clicked.connect(self._confirm_delete)
        self._delete_btn.setVisible(False)
        list_header_row.addWidget(self._delete_btn)

        layout.addLayout(list_header_row)

        self._wf_scroll = QScrollArea()
        self._wf_scroll.setWidgetResizable(True)
        self._wf_scroll.setMaximumHeight(280)
        self._wf_scroll.setStyleSheet(f"""
            QScrollArea {{
                border: 1px solid {CL_BORDER};
                border-radius: 6px;
                background: {CL_BG_MAIN};
            }}
        """)
        self._wf_container = QWidget()
        self._wf_container.setStyleSheet(f"background: {CL_BG_MAIN};")
        self._wf_list_layout = QVBoxLayout(self._wf_container)
        self._wf_list_layout.setContentsMargins(8, 8, 8, 8)
        self._wf_list_layout.setSpacing(6)
        self._wf_list_layout.addStretch()
        self._wf_scroll.setWidget(self._wf_container)
        layout.addWidget(self._wf_scroll)

        # ── YAML 预览 + 执行步骤（左右分栏） ──
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background: {CL_BORDER};
                width: 2px;
            }}
        """)

        # ── 左栏：YAML 预览 ──
        left_panel = QWidget()
        left_panel.setMinimumWidth(200)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 8, 0)
        left_layout.setSpacing(6)

        yaml_header = QLabel("流程内容预览")
        yaml_header.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CL_TEXT}; padding-top: 4px;")
        left_layout.addWidget(yaml_header)

        self._yaml_preview = QTextEdit()
        self._yaml_preview.setReadOnly(True)
        self._yaml_preview.setPlaceholderText("点击流程卡片查看具体步骤...")
        left_layout.addWidget(self._yaml_preview, 1)  # stretch = 1, fill available space

        splitter.addWidget(left_panel)

        # ── 右栏：执行步骤表格 ──
        right_panel = QWidget()
        right_panel.setMinimumWidth(300)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(6)

        steps_header = QHBoxLayout()
        steps_label = QLabel("执行步骤")
        steps_label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CL_TEXT}; padding-top: 4px;")
        steps_header.addWidget(steps_label)
        steps_header.addStretch()
        self._clear_steps_btn = QPushButton("清空")
        self._clear_steps_btn.setObjectName("btnSecondary")
        self._clear_steps_btn.setMaximumWidth(60)
        self._clear_steps_btn.clicked.connect(lambda: self._steps_table.setRowCount(0))
        steps_header.addWidget(self._clear_steps_btn)
        right_layout.addLayout(steps_header)

        self._steps_table = QTableWidget()
        self._steps_table.setColumnCount(5)
        self._steps_table.setHorizontalHeaderLabels(["步骤", "动作", "状态", "输出", "耗时"])
        self._steps_table.setAlternatingRowColors(True)
        self._steps_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._steps_table.verticalHeader().setVisible(False)
        h = self._steps_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        h.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        right_layout.addWidget(self._steps_table, 1)  # stretch = 1

        splitter.addWidget(right_panel)

        # 默认左右各占 50%
        splitter.setSizes([400, 400])

        layout.addWidget(splitter, 1)  # stretch = 1, fill remaining vertical space

        self._refresh_btn.clicked.connect(self._load_workflows)
        self._load_workflows()

    # ── 工作流卡片 ─────────────────────────────────────────────────
    def _load_workflows(self):
        """刷新工作流卡片列表。"""
        while self._wf_list_layout.count() > 1:
            item = self._wf_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._wf_cards.clear()
        self._wf_checks.clear()
        self._selected_wf = ""

        names = self._agent.list_workflows()
        if not names:
            empty = QLabel("还没有自动流程。\n在「做什么」里输入多步操作（如「保存后关闭」），\n程序会自动记下来变成可复用的流程。")
            empty.setStyleSheet(f"color: {CL_TEXT_MUTED}; font-size: 12px; padding: 16px;")
            empty.setAlignment(Qt.AlignCenter)
            self._wf_list_layout.insertWidget(0, empty)
            self._select_all_cb.setVisible(False)
            self._delete_btn.setVisible(False)
            self._edit_toggle_btn.setVisible(False)
            return

        self._edit_toggle_btn.setVisible(True)

        for wf_name in sorted(names):
            wf = self._agent.workflow.load(wf_name)
            desc_text = wf.description if wf and wf.description else "无描述"
            step_count = len(wf.steps) if wf else 0
            source_path = wf.source_path if wf else ""

            card = QFrame()
            card.setStyleSheet(f"""
                QFrame {{
                    background: {CL_BG_PANEL};
                    border: 1px solid {CL_BORDER};
                    border-radius: 6px;
                    padding: 0px;
                }}
                QFrame:hover {{
                    border-color: {CL_ACCENT};
                    background: {CL_ACCENT_BG};
                }}
            """)
            card.setCursor(Qt.PointingHandCursor)
            card.mousePressEvent = lambda e, n=wf_name: self._select_workflow(n)

            cr = QHBoxLayout(card)
            cr.setContentsMargins(10, 10, 14, 10)
            cr.setSpacing(10)

            # ── 单个 checkbox（编辑模式可见）──
            cb = QCheckBox()
            cb.setToolTip(f"选中「{wf_name}」")
            cb.setCursor(Qt.PointingHandCursor)
            cb.setStyleSheet("QCheckBox { spacing: 0px; }")
            cb.stateChanged.connect(lambda state, n=wf_name: self._on_card_check_changed(n, state))
            cb.setVisible(self._edit_mode)
            cr.addWidget(cb)
            self._wf_checks[wf_name] = cb

            # 左侧信息
            info_layout = QVBoxLayout()
            info_layout.setSpacing(2)
            name_label = QLabel(f"<b>{wf_name}</b>")
            name_label.setStyleSheet(f"font-size: 13px; color: {CL_TEXT}; background: transparent; border: none;")
            name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            info_layout.addWidget(name_label)

            detail_label = QLabel(f"{desc_text}  ·  {step_count} 步骤")
            detail_label.setStyleSheet(f"font-size: 11px; color: {CL_TEXT_DIM}; background: transparent; border: none;")
            detail_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            info_layout.addWidget(detail_label)

            cr.addLayout(info_layout, 1)
            self._wf_cards[wf_name] = card

            # ── 编辑按钮（打开流程文件）──
            btn_edit = QPushButton("编辑")
            btn_edit.setFixedWidth(56)
            btn_edit.setFixedHeight(30)
            btn_edit.setCursor(Qt.PointingHandCursor)
            btn_edit.setToolTip(f"在外部编辑器中打开 {wf_name} 流程文件")
            btn_edit.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {CL_TEXT};
                    border: 1px solid {CL_BORDER};
                    border-radius: 4px;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background: {CL_BG_HOVER};
                    border-color: {CL_ACCENT};
                }}
            """)
            btn_edit.clicked.connect(lambda checked, p=source_path: self._edit_workflow(p))
            cr.addWidget(btn_edit)

            # 预览按钮
            btn_preview = QPushButton("预览")
            btn_preview.setFixedWidth(56)
            btn_preview.setFixedHeight(30)
            btn_preview.setCursor(Qt.PointingHandCursor)
            btn_preview.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {CL_TEXT};
                    border: 1px solid {CL_BORDER};
                    border-radius: 4px;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background: {CL_BG_HOVER};
                    border-color: {CL_ACCENT};
                }}
            """)
            btn_preview.clicked.connect(lambda checked, n=wf_name: self._run_card_workflow(n, dry=True))
            cr.addWidget(btn_preview)

            # 执行按钮
            btn_run = QPushButton("执行")
            btn_run.setFixedWidth(56)
            btn_run.setFixedHeight(30)
            btn_run.setCursor(Qt.PointingHandCursor)
            btn_run.setStyleSheet(f"""
                QPushButton {{
                    background: {CL_ACCENT};
                    color: white;
                    border: none;
                    border-radius: 4px;
                    font-size: 12px;
                    font-weight: 600;
                }}
                QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            """)
            btn_run.clicked.connect(lambda checked, n=wf_name: self._run_card_workflow(n, dry=False))
            cr.addWidget(btn_run)

            self._wf_list_layout.insertWidget(self._wf_list_layout.count() - 1, card)

        # 同步编辑模式 UI
        self._select_all_cb.setVisible(self._edit_mode)
        self._delete_btn.setVisible(self._edit_mode)

    # ── 编辑模式 ───────────────────────────────────────────────────
    def _toggle_edit_mode(self):
        """切换编辑模式：显示 / 隐藏 checkbox 和删除按钮。"""
        self._edit_mode = not self._edit_mode
        self._select_all_cb.setVisible(self._edit_mode)
        self._delete_btn.setVisible(self._edit_mode)
        if self._edit_mode:
            self._edit_toggle_btn.setText("完成")
            self._edit_toggle_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {CL_ACCENT};
                    color: white;
                    border: none;
                    border-radius: 4px;
                    font-size: 12px;
                    font-weight: 600;
                }}
                QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            """)
        else:
            self._edit_toggle_btn.setText("编辑")
            self._edit_toggle_btn.setObjectName("btnSecondary")
            self._edit_toggle_btn.setStyleSheet("")
            # 退出编辑模式时清空勾选
            self._select_all_cb.blockSignals(True)
            self._select_all_cb.setChecked(False)
            self._select_all_cb.blockSignals(False)
            for cb in self._wf_checks.values():
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)
        # 显示/隐藏各卡片的 checkbox
        for cb in self._wf_checks.values():
            cb.setVisible(self._edit_mode)

    # ── 全选 / 单选 / 编辑 / 删除 ─────────────────────────────────
    def _on_select_all(self, state):
        """全选 / 取消全选。"""
        check_all = state == Qt.Checked
        for cb in self._wf_checks.values():
            cb.blockSignals(True)
            cb.setChecked(check_all)
            cb.blockSignals(False)

    def _on_card_check_changed(self, name: str, state):
        """单个 checkbox 变化时同步全选框。"""
        if not self._wf_checks:
            return
        all_checked = all(cb.isChecked() for cb in self._wf_checks.values())
        self._select_all_cb.blockSignals(True)
        self._select_all_cb.setChecked(all_checked)
        self._select_all_cb.blockSignals(False)

    def _edit_workflow(self, source_path: str):
        """在外部编辑器中打开流程文件。"""
        import os as _os
        import subprocess as _sp
        if not source_path or not _os.path.exists(source_path):
            self._show_info_dialog("文件不存在", f"找不到工作流文件:\n{source_path}")
            return
        try:
            if sys.platform == "win32":
                _os.startfile(source_path)
            elif sys.platform == "darwin":
                _sp.Popen(["open", source_path])
            else:
                _sp.Popen(["xdg-open", source_path])
        except Exception as e:
            self._show_info_dialog("打开失败", f"无法打开编辑器:\n{e}")

    # ── 通用对话框 ─────────────────────────────────────────────────
    def _show_confirm_dialog(self, title: str, message: str) -> bool:
        """显示带「取消」「确定」按钮的确认对话框，返回 True 表示用户点了确定。"""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(380)
        dlg.setStyleSheet(f"""
            QDialog {{
                background: {CL_BG_MAIN};
            }}
            QLabel {{
                color: {CL_TEXT};
                font-size: 13px;
                padding: 20px 24px 12px 24px;
            }}
        """)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        msg = QLabel(message)
        msg.setWordWrap(True)
        layout.addWidget(msg)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(16, 12, 16, 16)
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("btnSecondary")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setFixedWidth(80)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            QPushButton:pressed {{ background: {CL_ACCENT_P}; }}
        """)
        ok_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

        return dlg.exec_() == QDialog.Accepted

    def _show_info_dialog(self, title: str, message: str):
        """显示带「确定」按钮的提示对话框。"""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumWidth(360)
        dlg.setStyleSheet(f"""
            QDialog {{
                background: {CL_BG_MAIN};
            }}
            QLabel {{
                color: {CL_TEXT};
                font-size: 13px;
                padding: 20px 24px 12px 24px;
            }}
        """)
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        msg = QLabel(message)
        msg.setWordWrap(True)
        layout.addWidget(msg)

        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(16, 12, 16, 16)
        btn_layout.addStretch()

        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setFixedWidth(80)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            QPushButton:pressed {{ background: {CL_ACCENT_P}; }}
        """)
        ok_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

        dlg.exec_()

    def _confirm_delete(self):
        """弹出确认对话框，确认后删除勾选的工作流。"""
        checked_names = [n for n, cb in self._wf_checks.items() if cb.isChecked()]
        if not checked_names:
            self._show_info_dialog("提示", "请先勾选需要删除的工作流。")
            return

        # 构建确认对话框
        name_list = "\n".join(f"  • {n}.yaml" for n in checked_names)
        ok = self._show_confirm_dialog(
            "确认删除",
            f"您确定要删除以下 {len(checked_names)} 个工作流吗？\n\n{name_list}\n\n此操作不可撤销。",
        )
        if ok:
            self._do_delete(checked_names)

    def _do_delete(self, names: list):
        """执行删除：移除对应的 .yaml 文件。"""
        import os as _os
        wf_dir = self._agent.workflow._workflows_dir
        deleted = []
        failed = []
        for name in names:
            for ext in (".yaml", ".yml"):
                path = wf_dir / f"{name}{ext}"
                if path.exists():
                    try:
                        _os.remove(str(path))
                        deleted.append(name)
                        break
                    except OSError as e:
                        failed.append(f"{name}: {e}")
                        break
            else:
                failed.append(f"{name}: 文件不存在")
        if deleted:
            self._steps_table.setRowCount(0)
            self._yaml_preview.clear()
        self._load_workflows()
        # 退出编辑模式
        if self._edit_mode:
            self._toggle_edit_mode()
        # 提示结果
        msg_parts = []
        if deleted:
            msg_parts.append(f"已删除 {len(deleted)} 个工作流。")
        if failed:
            msg_parts.append(f"{len(failed)} 个失败:\n" + "\n".join(failed))
        self._show_info_dialog("删除完成", "\n".join(msg_parts))

    # ── 选中 / 预览 / 执行 ─────────────────────────────────────────
    def _select_workflow(self, name: str):
        """点击卡片选中并预览 YAML。"""
        self._selected_wf = name
        for n, card in self._wf_cards.items():
            if n == name:
                card.setStyleSheet(f"""
                    QFrame {{
                        background: {CL_ACCENT_BG};
                        border: 2px solid {CL_ACCENT};
                        border-radius: 6px;
                    }}
                """)
            else:
                card.setStyleSheet(f"""
                    QFrame {{
                        background: {CL_BG_PANEL};
                        border: 1px solid {CL_BORDER};
                        border-radius: 6px;
                    }}
                    QFrame:hover {{
                        border-color: {CL_ACCENT};
                        background: {CL_ACCENT_BG};
                    }}
                """)
        self._preview_yaml(name)

    def _preview_yaml(self, name: str = None):
        """预览选中工作流的 YAML 内容。"""
        if name is None:
            name = self._selected_wf
        if not name:
            self._yaml_preview.clear()
            return
        wf = self._agent.workflow.load(name)
        if wf:
            try:
                with open(wf.source_path, "r", encoding="utf-8") as f:
                    self._yaml_preview.setText(f.read())
            except Exception:
                self._yaml_preview.setText(f"name: {wf.name}\nsteps: {len(wf.steps)}")

    def _run_card_workflow(self, name: str, dry: bool = False):
        """从卡片按钮执行工作流。非 dry-run 时弹出确认对话框。"""
        self._select_workflow(name)
        if not dry:
            wf = self._agent.workflow.load(name)
            step_count = len(wf.steps) if wf else 0
            ok = self._show_confirm_dialog(
                "确认执行",
                f"确定要执行工作流「{name}」吗？\n\n"
                f"描述：{wf.description if wf else '(无)'}\n"
                f"步骤数：{step_count}\n\n"
                f"执行期间会实际发送按键/运行命令。",
            )
            if not ok:
                return
        self._run_workflow(name, dry)

    def _run_workflow(self, name: str = None, dry: bool = None):
        """执行指定工作流并在步骤表格中展示结果。"""
        if name is None:
            name = self._selected_wf
        if not name:
            return
        if dry is None:
            dry = self._dry_check.isChecked()

        QApplication.processEvents()

        try:
            result = self._agent.run_workflow(name, dry_run=dry)
            self._steps_table.setRowCount(0)
            for step in result.steps:
                row = self._steps_table.rowCount()
                self._steps_table.insertRow(row)
                self._steps_table.setItem(row, 0, QTableWidgetItem(step.step_name))
                wf = self._agent.workflow.load(name)
                action = ""
                if wf and row < len(wf.steps):
                    action = wf.steps[row].action
                self._steps_table.setItem(row, 1, QTableWidgetItem(action))
                status = "✅ OK" if step.success else "❌ ERR"
                status_item = QTableWidgetItem(status)
                status_item.setTextAlignment(Qt.AlignCenter)
                if not step.success:
                    status_item.setForeground(QColor(CL_DANGER))
                else:
                    status_item.setForeground(QColor(CL_SUCCESS))
                self._steps_table.setItem(row, 2, status_item)
                output = step.error or step.output
                self._steps_table.setItem(row, 3, QTableWidgetItem(str(output)[:200]))
                self._steps_table.setItem(row, 4, QTableWidgetItem(f"{step.duration_ms:.0f}ms"))
            if result.error:
                self._show_info_dialog("工作流错误", result.error)
        except Exception as e:
            self._show_info_dialog("执行异常", str(e))


# ═══════════════════════════════════════════════════════════════════════
# 应用上下文面板
# ═══════════════════════════════════════════════════════════════════════
    def _install_shortcuts(self):
        """Local panel shortcuts."""
        S = QShortcut
        K = QKeySequence
        # F5 / Ctrl+R  refresh workflow list
        S(K("F5"), self).activated.connect(self._load_workflows)
        # Ctrl+Enter  run selected workflow (dry-run)
        S(K("Ctrl+Return"), self, context=Qt.WidgetWithChildrenShortcut).activated.connect(
            lambda: self._run_workflow(dry=True) if self._selected_wf else None)
        # Ctrl+Shift+Enter  run selected workflow (real)
        S(K("Ctrl+Shift+Return"), self, context=Qt.WidgetWithChildrenShortcut).activated.connect(
            lambda: self._run_workflow(dry=False) if self._selected_wf else None)

class AppContextPanel(QWidget):
    def __init__(self, agent):
        super().__init__()
        self._agent = agent
        self._setup_ui()
        self._install_shortcuts()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(14)

        hdr = QLabel("应用感知")
        hdr.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {CL_TEXT}; padding-bottom: 4px;")
        layout.addWidget(hdr)

        desc = QLabel("自动检测当前活跃窗口,适配不同应用的快捷键映射")
        desc.setStyleSheet(f"color: {CL_TEXT_DIM}; padding-bottom: 4px;")
        layout.addWidget(desc)

        card = QFrame()
        card.setObjectName("cardFlat")
        card_layout = QVBoxLayout(card)

        ctx_header = QHBoxLayout()
        ctx_header.addWidget(QLabel("当前运行环境"))
        ctx_header.addStretch()
        self._refresh_ctx_btn = QPushButton("刷新")
        self._refresh_ctx_btn.setObjectName("btnSecondary")
        ctx_header.addWidget(self._refresh_ctx_btn)
        card_layout.addLayout(ctx_header)

        ctx_grid = QGridLayout()
        ctx_grid.setSpacing(12)
        fields = [
            ("应用名称", "app_name"),
            ("进程名", "process_name"),
            ("窗口标题", "window_title"),
            ("平台", "platform"),
        ]
        self._ctx_labels: dict[str, QLabel] = {}
        for i, (label, key) in enumerate(fields):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {CL_TEXT_MUTED}; font-size: 12px;")
            val = QLabel("-")
            val.setStyleSheet(f"color: {CL_TEXT}; font-size: 14px; font-weight: 600;")
            val.setWordWrap(True)
            self._ctx_labels[key] = val
            ctx_grid.addWidget(lbl, i, 0)
            ctx_grid.addWidget(val, i, 1)

        card_layout.addLayout(ctx_grid)
        layout.addWidget(card)

        map_header = QLabel("应用快捷键映射")
        map_header.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CL_TEXT}; padding-top: 8px;")
        layout.addWidget(map_header)

        self._app_table = QTableWidget()
        self._app_table.setColumnCount(4)
        self._app_table.setHorizontalHeaderLabels(["应用", "命令", "默认键", "适配键"])
        self._app_table.setAlternatingRowColors(True)
        self._app_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._app_table.verticalHeader().setVisible(False)
        h = self._app_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self._app_table)

        layout.addStretch()
        self._refresh_ctx_btn.clicked.connect(self._refresh_context)
        self._refresh_context()

    def _refresh_context(self):
        try:
            ctx = self._agent.get_context()
            self._ctx_labels["app_name"].setText(ctx.app_name or "未知")
            self._ctx_labels["process_name"].setText(ctx.process_name or "未知")
            self._ctx_labels["window_title"].setText(ctx.window_title or "-")
            self._ctx_labels["platform"].setText(ctx.platform or "-")

            shortcuts = self._agent.search_shortcuts(ctx.app_name) if ctx.app_name else []
            if not shortcuts:
                shortcuts = self._agent.list_shortcuts()[:10]
                for s in shortcuts:
                    s.application = "common"

            self._app_table.setRowCount(0)
            for s in shortcuts[:20]:
                row = self._app_table.rowCount()
                self._app_table.insertRow(row)
                self._app_table.setItem(row, 0, QTableWidgetItem(getattr(s, "application", "common")))
                cmd_item = QTableWidgetItem(s.command)
                cmd_item.setFont(QFont("微软雅黑", -1, QFont.Bold))
                self._app_table.setItem(row, 1, cmd_item)
                self._app_table.setItem(row, 2, QTableWidgetItem(s.windows_key))
                platform = Platform.detect()
                key = s.get_key(platform)
                self._app_table.setItem(row, 3, QTableWidgetItem(key))
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════════════
    def _install_shortcuts(self):
        """Local panel shortcuts."""
        S = QShortcut
        K = QKeySequence
        S(K("F5"), self).activated.connect(self._refresh_context)
        S(K("Ctrl+R"), self).activated.connect(self._refresh_context)

class MainWindow(QMainWindow):
    NAV_ITEMS = [
        ("做什么", 0),
        #("快捷键库", 5),  # 仅通过 帮助→快捷键参考(&K) 进入
        ("自动流程", 1),
        ("应用感知", 2),
        ("执行历史", 3),
        ("数据统计", 4),
    ]

    def __init__(self):
        super().__init__()
        self._agent = ShortcutAgent()
        self._nav_buttons: list[SidebarButton] = []
        self._current_page = 0
        self._setup_ui()
        self._setup_menus()
        self._setup_shortcuts()
        self.setWindowTitle("NL2Shortcut — 用说话操作电脑")
        self.resize(1100, 720)
        self.setMinimumSize(900, 550)

    def _setup_ui(self):
        self.setStyleSheet(VSCODE_QSS)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ═══ 侧边栏导航 ═══
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        # 侧边栏标题
        section_label = QLabel("导航")
        section_label.setObjectName("sidebarTitle")
        sidebar_layout.addWidget(section_label)

        app_title = QLabel("智能助手")
        app_title.setObjectName("sidebarSubtitle")
        sidebar_layout.addWidget(app_title)

        sep = QFrame()
        sep.setObjectName("navSeparator")
        sidebar_layout.addWidget(sep)

        # 导航按钮
        for text, _ in self.NAV_ITEMS:
            btn = SidebarButton(text)
            self._nav_buttons.append(btn)
            sidebar_layout.addWidget(btn)

        sidebar_layout.addStretch()

        # ── Mini Dialog 启动按钮 ──
        mini_btn = QPushButton("💬 迷你对话窗")
        mini_btn.setObjectName("miniLaunchBtn")
        mini_btn.setCursor(Qt.PointingHandCursor)
        mini_btn.setToolTip("启动迷你浮动对话窗（全局热键 Ctrl+Alt+M 唤出）")
        mini_btn.setMinimumHeight(36)
        mini_btn.setStyleSheet(f"""
            QPushButton {{
                background: #10B981;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                margin: 4px 12px 4px 12px;
                font-size: 13px;
                font-weight: 600;
                font-family: 'Microsoft YaHei';
            }}
            QPushButton:hover {{
                background: #059669;
            }}
            QPushButton:pressed {{
                background: #047857;
            }}
        """)
        mini_btn.clicked.connect(self._launch_mini_dialog)
        sidebar_layout.addWidget(mini_btn)

        # ── Overlay 启动按钮 ──
        overlay_btn = QPushButton("⚡ 启动 Overlay")
        overlay_btn.setObjectName("overlayLaunchBtn")
        overlay_btn.setCursor(Qt.PointingHandCursor)
        overlay_btn.setToolTip("启动系统托盘 + 全局热键 + 浮窗输入栏\n(快捷键: Ctrl+Alt+O)")
        overlay_btn.setMinimumHeight(36)
        overlay_btn.setStyleSheet(f"""
            QPushButton {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 16px;
                margin: 8px 12px 4px 12px;
                font-size: 13px;
                font-weight: 600;
                font-family: 'Microsoft YaHei';
            }}
            QPushButton:hover {{
                background: {CL_ACCENT_H};
            }}
            QPushButton:pressed {{
                background: {CL_ACCENT_P};
            }}
        """)
        overlay_btn.clicked.connect(self._launch_overlay)
        sidebar_layout.addWidget(overlay_btn)

        # 底部状态
        llm_status = "智能模式" if self._agent.llm_available else "基础模式"
        llm_color = CL_SUCCESS if self._agent.llm_available else CL_TEXT_MUTED
        self._llm_label = QLabel(llm_status)
        self._llm_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {llm_color}; padding: 4px 20px;"
        )
        # 设置按钮
        settings_btn = QPushButton("⚙ 设置")
        settings_btn.setObjectName("navButton")
        settings_btn.setCursor(Qt.PointingHandCursor)
        settings_btn.setToolTip("LLM 设置")
        settings_btn.setMinimumHeight(32)
        settings_btn.clicked.connect(self._show_llm_settings)
        sidebar_layout.addWidget(settings_btn)

        sidebar_layout.addWidget(self._llm_label)

        ver_label = QLabel(f"v1.0.0  ·  {Platform.detect().name}")
        ver_label.setStyleSheet(f"font-size: 11px; color: {CL_TEXT_MUTED}; padding: 2px 20px 12px 20px;")
        sidebar_layout.addWidget(ver_label)

        root.addWidget(sidebar)

        # ═══ 右侧内容区 (~80%) ═══
        content = QFrame()
        content.setStyleSheet(f"background-color: {CL_BG_MAIN};")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget()

        self._history = HistoryPanel()
        self._shortcuts_panel = ShortcutsPanel(self._agent)
        self._workflow_panel = WorkflowPanel(self._agent)
        self._context_panel = AppContextPanel(self._agent)
        self._stats_panel = StatsPanel(self._agent)
        from .agent_panel import AgentPanel
        self._agent_panel = AgentPanel(self._agent)

        self._stack.addWidget(self._agent_panel)        # 0 — Agent 首位
        self._stack.addWidget(self._workflow_panel)     # 1
        self._stack.addWidget(self._context_panel)      # 2
        self._stack.addWidget(self._history)            # 3
        self._stack.addWidget(self._stats_panel)        # 4
        self._stack.addWidget(self._shortcuts_panel)    # 5 — 快捷键库（仅帮助→快捷键参考 进入）

        content_layout.addWidget(self._stack)
        root.addWidget(content)

        # 导航切换 - Sidebar
        for i, btn in enumerate(self._nav_buttons):
            btn.clicked.connect(lambda checked, idx=i: self._switch_page(idx))

        self._set_active_all(0)

        # VS Code 风格蓝色状态栏
        self._status = QStatusBar()
        self._status.setStyleSheet(f"""
            QStatusBar {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                font-size: 12px;
                padding: 2px 10px;
                min-height: 22px;
            }}
            QStatusBar QLabel {{
                color: rgba(255,255,255,0.9);
                font-size: 12px;
                padding: 0 12px;
            }}
        """)
        adapter_name = type(self._agent.adapter).__name__
        self._status_label = QLabel(
            f"{Platform.detect().name}  ·  {adapter_name}  ·  已就绪"
        )
        self._status.addWidget(self._status_label)
        # Agent API status (right side of status bar)
        from PyQt5.QtWidgets import QLabel as _QL
        self._agent_status_action = _QL("服务启动中...")
        self._agent_status_action.setStyleSheet("color: #616161; padding: 0 12px;")
        self._status.addPermanentWidget(self._agent_status_action)
        self.setStatusBar(self._status)

    def _switch_page(self, index: int):
        if index < 0 or index >= self._stack.count():
            return
        self._set_active_all(index)
        self._stack.setCurrentIndex(index)
        self._current_page = index
        if index == 5:
            self._shortcuts_panel._load_all()
        elif index == 2:
            self._context_panel._refresh_context()
        elif index == 4:
            self._stats_panel.refresh()

    def _set_active_all(self, stack_index: int):
        """根据栈索引高亮侧边栏对应按钮；若不在 NAV_ITEMS 中则全部取消高亮"""
        target = -1
        for i, (_, si) in enumerate(self.NAV_ITEMS):
            if si == stack_index:
                target = i
                break
        for i, btn in enumerate(self._nav_buttons):
            btn.set_active(i == target)

    def _setup_menus(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件(&F)")
        refresh_action = QAction("刷新快捷键库(&R)", self)
        refresh_action.setShortcut(QKeySequence.Refresh)
        refresh_action.triggered.connect(lambda: self._shortcuts_panel._load_all())
        file_menu.addAction(refresh_action)
        file_menu.addSeparator()
        exit_action = QAction("退出(&X)", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self._confirm_exit)
        file_menu.addAction(exit_action)

        view_menu = menubar.addMenu("视图(&V)")
        for idx, (text, stack_idx) in enumerate(self.NAV_ITEMS):
            act = QAction(text, self)
            act.setShortcut(QKeySequence(f"Ctrl+{idx+1}"))
            act.triggered.connect(lambda _, si=stack_idx: self._switch_page(si))
            view_menu.addAction(act)

        tool_menu = menubar.addMenu("工具(&T)")
        mini_action = QAction("启动迷你对话窗(&M)...", self)
        mini_action.setShortcut(QKeySequence("Ctrl+Alt+M"))
        mini_action.triggered.connect(self._launch_mini_dialog)
        tool_menu.addAction(mini_action)
        overlay_action = QAction("启动全局热键 Overlay(&O)...", self)
        overlay_action.setShortcut(QKeySequence("Ctrl+Alt+O"))
        overlay_action.triggered.connect(self._launch_overlay)
        tool_menu.addAction(overlay_action)

        help_menu = menubar.addMenu("帮助(&H)")
        llm_action = QAction("智能识别设置(&D)", self)
        llm_action.triggered.connect(self._show_llm_settings)
        help_menu.addAction(llm_action)
        help_menu.addSeparator()
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
        shortcut_ref = QAction("快捷键参考(&K)", self)
        shortcut_ref.setShortcut(QKeySequence("Ctrl+K"))
        shortcut_ref.triggered.connect(lambda: self._switch_page(5))
        help_menu.addAction(shortcut_ref)

    def _setup_shortcuts(self):
        """全局键盘快捷键。这本身就是产品的核心承诺：
        一个快捷键智能体，其自身操作也理应主要靠键盘完成。

        模式参照 VS Code / Sublime：
          - Ctrl+1..7   切换页面
          - Ctrl+L      聚焦指令输入框
          - Ctrl+F      聚焦搜索框
          - Ctrl+Enter  执行 / 发送
          - Ctrl+,      设置
          - F1          快捷键速查表
          - Ctrl+Shift+Enter  切换预览模式
          - Ctrl+Shift+A  跳转到 Agent 面板
        """
        Q = QtShortcut
        SEQ = QKeySequence

        # ── Page switching (Ctrl+1..N，对应侧边栏导航项) ──
        for i, (_, stack_idx) in enumerate(self.NAV_ITEMS):
            sc = Q(SEQ(f"Ctrl+{i+1}"), self)
            sc.activated.connect(lambda idx=stack_idx: self._switch_page(idx))

        # ── Cross-panel navigation ──
        Q(SEQ("Ctrl+L"), self).activated.connect(self._focus_command)
        Q(SEQ("Ctrl+K"), self).activated.connect(lambda: self._switch_page(5))
        Q(SEQ("Ctrl+H"), self).activated.connect(lambda: self._switch_page(3))
        Q(SEQ("Ctrl+Shift+A"), self).activated.connect(lambda: self._switch_page(6))
        Q(SEQ("Ctrl+Shift+S"), self).activated.connect(lambda: self._switch_page(1))
        Q(SEQ("Ctrl+Shift+D"), self).activated.connect(lambda: self._switch_page(4))
        Q(SEQ("Ctrl+Shift+W"), self).activated.connect(lambda: self._switch_page(2))

        # ── Common actions ──
        Q(SEQ("Ctrl+Enter"), self).activated.connect(self._trigger_execute)
        Q(SEQ("Ctrl+Shift+Enter"), self).activated.connect(self._toggle_dry_run)
        Q(SEQ("Ctrl+R"), self).activated.connect(self._refresh_current)
        Q(SEQ("Ctrl+F"), self).activated.connect(self._focus_search)
        Q(SEQ("F1"), self).activated.connect(self._show_cheatsheet)
        Q(SEQ("Ctrl+/"), self).activated.connect(self._show_cheatsheet)
        Q(SEQ("Ctrl+,"), self).activated.connect(self._show_settings)
        Q(SEQ("F5"), self).activated.connect(self._refresh_current)
        Q(SEQ("Escape"), self).activated.connect(self._clear_focus)

    def _focus_command(self):
        """Focus the input box on Execute or Agent panel."""
        idx = self._stack.currentIndex()
        if idx == 0 and hasattr(self._exec_panel, "_input"):
            self._exec_panel._input.setFocus()
            self._exec_panel._input.selectAll()
        elif idx == 6 and hasattr(self._agent_panel, "_chat_input"):
            self._agent_panel._chat_input.setFocus()

    def _focus_search(self):
        """Focus the search field on the active panel, if it has one."""
        idx = self._stack.currentIndex()
        panel = self._stack.widget(idx)
        for attr in ("_search", "_filter", "searchBar", "_search_input"):
            w = getattr(panel, attr, None)
            if w and hasattr(w, "setFocus"):
                w.setFocus()
                if hasattr(w, "selectAll"):
                    w.selectAll()
                return

    def _trigger_execute(self):
        """Trigger execute on Execute panel, send on Agent panel."""
        idx = self._stack.currentIndex()
        if idx == 0 and hasattr(self._exec_panel, "_btn_exec"):
            self._exec_panel._btn_exec.click()
        elif idx == 6 and hasattr(self._agent_panel, "_btn_send"):
            self._agent_panel._btn_send.click()

    def _toggle_dry_run(self):
        """Toggle dry-run on Execute panel."""
        if hasattr(self._exec_panel, "_dry_run"):
            self._exec_panel._dry_run.toggle()
            state = "ON" if self._exec_panel._dry_run.isChecked() else "OFF"
            self.statusBar().showMessage(f"  预览模式:{state}", 2000)

    def _refresh_current(self):
        idx = self._stack.currentIndex()
        if idx == 1 and hasattr(self._shortcuts_panel, "_populate_table"):
            self._shortcuts_panel._populate_table()
        elif idx == 2 and hasattr(self._workflow_panel, "_load_workflows"):
            self._workflow_panel._load_workflows()
        elif idx == 4 and hasattr(self._history, "refresh"):
            self._history.refresh()
        elif idx == 5 and hasattr(self._stats_panel, "refresh"):
            self._stats_panel.refresh()
        elif idx == 6 and hasattr(self._agent_panel, "_load_capabilities"):
            self._agent_panel._load_capabilities()
        self.statusBar().showMessage("  已刷新", 1500)

    def _clear_focus(self):
        self._stack.currentWidget().setFocus()

    def _show_cheatsheet(self):
        """Popup listing all keyboard shortcuts."""
        from PyQt5.QtWidgets import QDialog, QTextEdit, QVBoxLayout
        dlg = QDialog(self)
        dlg.setWindowTitle("NL2Shortcut 快捷键速查")
        dlg.resize(620, 540)
        dlg.setStyleSheet("background: #FFFFFF;")
        v = QVBoxLayout(dlg)
        v.setSpacing(12)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setStyleSheet(
            "QTextEdit { background: #FFFFFF; color: #1E1E1E; border: none; "
            "font-family: 'Consolas', 'Microsoft YaHei'; font-size: 12px; }"
        )
        text.setHtml(self._cheatsheet_html())
        v.addWidget(text)

        # 确定按钮
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setFixedWidth(80)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            QPushButton:pressed {{ background: {CL_ACCENT_P}; }}
        """)
        ok_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(ok_btn)
        v.addLayout(btn_row)

        dlg.exec_()

    def _cheatsheet_html(self) -> str:
        rows = [
            ("导航", [
                ("Ctrl+1",        "快捷执行"),
                ("Ctrl+2",        "工作流"),
                ("Ctrl+3",        "应用感知"),
                ("Ctrl+4",        "执行历史"),
                ("Ctrl+5",        "数据统计"),
                ("Ctrl+K",        "→ 快捷键库"),
                ("Ctrl+H",        "→ 历史"),
                ("Ctrl+Shift+A",  "→ Agent 控制台"),
                ("Ctrl+Shift+S",  "→ 工作流"),
                ("Ctrl+Shift+D",  "→ 数据统计"),
                ("Ctrl+Shift+W",  "→ 应用感知"),
            ]),
            ("执行面板 (Ctrl+1)", [
                ("Ctrl+Enter",          "执行当前指令"),
                ("Ctrl+.",              "聚焦 dry-run 复选框"),
                ("Ctrl+0 / Esc",        "清空输入"),
            ]),
            ("快捷键库 (Ctrl+K · 帮助→快捷键参考)", [
                ("Ctrl+F",              "聚焦搜索"),
                ("Ctrl+Shift+L",        "切换中英文"),
                ("↑ ↓",                 "上下移动选中行"),
            ]),
            ("工作流 (Ctrl+2)", [
                ("Ctrl+Enter",          "运行选中（预览）"),
                ("Ctrl+Shift+Enter",    "运行选中（实际）"),
                ("F5",                  "刷新列表"),
            ]),
            ("应用感知 (Ctrl+3)", [
                ("F5 / Ctrl+R",         "重新检测"),
            ]),
            ("执行历史 (Ctrl+4)", [
                ("Delete",              "删除选中行"),
                ("Ctrl+Shift+Delete",   "清空全部"),
                ("Ctrl+E",              "导出日志"),
                ("Ctrl+C",              "复制选中行"),
            ]),
            ("数据统计 (Ctrl+5)", [
                ("F5 / Ctrl+R",         "刷新"),
                ("Ctrl+Shift+R",        "重置统计（确认）"),
            ]),
            ("Agent 控制台 (Ctrl+Shift+A)", [
                ("Ctrl+Enter",          "发送"),
                ("Ctrl+B",              "启动/停止 API server"),
                ("F5",                  "刷新能力清单"),
            ]),
            ("全局", [
                ("F1 / Ctrl+/",         "本速查表"),
                ("Ctrl+,",              "设置"),
                ("Ctrl+Q",              "退出"),
                ("Esc",                 "取消焦点"),
                ("Ctrl+Alt+O",          "唤出 Overlay 输入栏"),
            ]),
        ]
        out = [
            '<h2 style="color:#0078D4; margin: 4px 0 4px 0;">⚡  NL2Shortcut — Keyboard Master Agent</h2>'
            '<p style="color:#616161; font-size: 12px; margin: 0 0 14px 0;">'
            '把 Agent 意图转为键鼠操作 · p50 &lt; 5ms · 51 条内置动作 · 30+ 应用适配</p>'
        ]
        for section, items in rows:
            out.append(f'<h3 style="color:#388A34; margin: 12px 0 6px 0;">{section}</h3>')
            out.append('<table cellpadding="4" cellspacing="0" style="border-collapse: collapse;">')
            for key, desc in items:
                out.append(
                    f'<tr><td style="padding: 4px 12px 4px 0;">'
                    f'<code style="background:#F0F0F0; color:#0078D4; padding: 2px 8px; '
                    f'border-radius: 3px; font-weight: 600;">{key}</code></td>'
                    f'<td style="color:#1E1E1E;">{desc}</td></tr>'
                )
            out.append('</table>')
        out.append(
            '<hr style="margin: 14px 0; border: none; border-top: 1px solid #E0E0E0;">'
            '<p style="color:#616161; font-size: 11px;">'
            'NL2Shortcut 是"会听人话的快捷键助手"--本软件的主要操作都配了快捷键。'
            '本软件也提供 <b>外部调用接口</b>(Ctrl+7 进入控制台启动),'
            '可供其他 AI 助手通过 HTTP 调用本程序执行快捷键。</p>'
        )
        return ''.join(out)

    def _show_settings(self):
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.information(
            self, "设置",
            "设置说明：\n\n"
            "• 在「帮助 → 智能识别设置」里开启智能识别\n"
            "• 在「快捷键库」里查看程序能听懂的所有操作\n"
            "• 在「自动流程」里查看和管理自动执行的多步操作\n\n"
            "程序会自动记住你的使用习惯，越用越顺手。"
        )

    def _launch_mini_dialog(self):
        """Launch the mini floating dialog in background (tray + global hotkey)."""
        import subprocess
        import shutil
        import os
        python = shutil.which("python") or shutil.which("python3") or "python"
        try:
            subprocess.Popen(
                [python, "-m", "nl2shortcut", "mini"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=0x00000008,
            )
            self.statusBar().showMessage("  迷你对话窗已启动 (Ctrl+Alt+M 唤出)", 4000)
        except Exception as e:
            QMessageBox.warning(self, "启动失败", f"迷你对话窗启动失败:\n{e}")

    def _launch_overlay(self):
        """Launch NL2Shortcut overlay in background (system tray + global hotkey)."""
        import subprocess
        import shutil
        import os
        from PyQt5.QtCore import QProcess

        # Check if overlay already running
        existing = self._find_overlay_process()
        if existing:
            QMessageBox.information(
                self,
                "Overlay 已在运行",
                f"系统托盘中的 NL2Shortcut overlay 正在运行 (PID {existing})。\n\n"
                f"使用 Ctrl+Alt+S 唤出浮窗输入栏。",
            )
            return

        # Spawn detached python process
        try:
            python_exe = sys.executable
            proj_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NO_WINDOW

            proc = subprocess.Popen(
                [python_exe, "-m", "nl2shortcut", "overlay"],
                cwd=proj_root,
                creationflags=creationflags,
                close_fds=True,
            )
            self._status_label.setText(
                f"⚡ Overlay 已启动 (PID {proc.pid})  ·  按 Ctrl+Alt+S 唤出浮窗"
            )
            QMessageBox.information(
                self,
                "Overlay 已启动",
                f"NL2Shortcut overlay 已在后台运行 (PID {proc.pid})。\n\n"
                f"全局热键: Ctrl+Alt+S\n"
                f"系统托盘: 紫色 ⚡ NL2Shortcut 图标\n"
                f"右键托盘图标可退出",
            )
        except Exception as e:
            QMessageBox.critical(self, "启动失败", f"无法启动 overlay:\n{e}")

    def _find_overlay_process(self) -> int:
        """Find existing nl2shortcut overlay process by checking window title."""
        import subprocess
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            ).decode("utf-8", errors="ignore")
            for line in out.strip().splitlines():
                parts = line.replace('"', "").split(",")
                if len(parts) >= 2 and parts[0].lower() == "python.exe":
                    pid = int(parts[1])
                    cmdline = self._get_cmdline(pid)
                    if "nl2shortcut" in cmdline and "overlay" in cmdline:
                        return pid
        except Exception:
            pass
        return 0

    def _get_cmdline(self, pid: int) -> str:
        try:
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            psapi.GetModuleFileNameExW.argtypes = (
                wintypes.HANDLE, wintypes.HMODULE, wintypes.LPCWSTR, wintypes.DWORD
            )
            psapi.GetModuleFileNameExW.restype = wintypes.DWORD
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                buf = ctypes.create_unicode_buffer(512)
                psapi.GetModuleFileNameExW(h, None, buf, 512)
                kernel32.CloseHandle(h)
                return buf.value
        except Exception:
            pass
        return ""

    def _show_llm_settings(self):
        dialog = LlmSettingsDialog(self._agent, self)
        if dialog.exec_() == QDialog.Accepted:
            self._refresh_llm_status()

    def _refresh_llm_status(self):
        llm_available = self._agent.llm_available
        llm_status = "智能模式" if llm_available else "基础模式"
        llm_color = CL_SUCCESS if llm_available else CL_TEXT_MUTED
        self._llm_label.setText(llm_status)
        self._llm_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {llm_color}; padding: 4px 20px;"
        )

    def _show_about(self):
        QMessageBox.about(
            self, "关于 NL2Shortcut",
            f"<h2>NL2Shortcut v1.0.0</h2>"
            "<p><b>用说话的方式操作电脑</b></p>"
            "<p>输入你想做的事，程序自动帮你完成对应的操作。</p>"
            "<hr>"
            "<p><b>能做什么</b></p>"
            "<p>· 复制、粘贴、保存等常用操作<br>"
            "· 打开软件、切换窗口<br>"
            "· 把多步操作串起来自动执行<br>"
            "· 记住你的习惯，越用越快</p>"
            "<hr>"
            f"<p>简单、快速、本地运行</p>",
        )


    def _auto_start_agent_api(self):
        """NL2Shortcut 的主要对外入口是 Agent API —— 在启动时即启动它。

        委托给 AgentPanel._start_server()，以便状态栏与
        AgentPanel._poll_status() 共用同一个 _server_thread 引用。
        这样可保证 UI 同步：按钮的启用 / 禁用状态与状态指示灯
        始终与服务器实际状态保持一致。
        """
        try:
            self._agent_panel._start_server()
            self._agent_status_action.setText("Agent API: listening on :7770")
            self._agent_status_action.setStyleSheet(
                "color: #0078D4; padding: 0 12px; font-weight: 600;"
            )
        except OSError:
            self._agent_status_action.setText("Agent API: already running")
            self._agent_status_action.setStyleSheet(
                "color: #388A34; padding: 0 12px; font-weight: 600;"
            )
        except Exception as e:
            self._agent_status_action.setText(f"Agent API: error ({e})")
            self._agent_status_action.setStyleSheet(
                "color: #D32F2F; padding: 0 12px;"
            )

    def _confirm_exit(self):
        """退出确认：弹出取消/确定对话框。"""
        reply = QMessageBox.question(
            self,
            "退出 NL2Shortcut",
            "确定要退出 NL2Shortcut 吗？\n\n"
            "退出后全局热键 Overlay（如果正在运行）不受影响，\n"
            "但本 GUI 窗口将关闭。",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply == QMessageBox.Ok:
            self.close()

    def closeEvent(self, event):
        """窗口关闭事件：弹出确认对话框。"""
        reply = QMessageBox.question(
            self,
            "退出 NL2Shortcut",
            "确定要退出 NL2Shortcut 吗？\n\n"
            "退出后全局热键 Overlay（如果正在运行）不受影响，\n"
            "但本 GUI 窗口将关闭。",
            QMessageBox.Ok | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply == QMessageBox.Ok:
            event.accept()
        else:
            event.ignore()


# ═══════════════════════════════════════════════════════════════════════
# DeepSeek LLM 设置对话框
# ═══════════════════════════════════════════════════════════════════════
class LlmSettingsDialog(QDialog):
    def __init__(self, agent, parent=None):
        super().__init__(parent)
        from .llm import DeepSeekEngine, _load_api_key
        self._agent = agent
        self._DeepSeekEngine = DeepSeekEngine
        self._load_api_key = _load_api_key
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("智能识别设置")
        self.setMinimumWidth(520)
        self.setModal(True)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {CL_BG_MAIN};
                border: 1px solid {CL_BORDER};
            }}
            QLabel {{
                color: {CL_TEXT};
                font-size: 13px;
            }}
            QLineEdit {{
                padding: 8px 12px;
                border: 1px solid {CL_BORDER};
                border-radius: 4px;
                background: {CL_BG_INPUT};
                color: {CL_TEXT};
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border-color: {CL_BORDER_FOC};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel("智能识别")
        title.setStyleSheet(f"font-size: 18px; font-weight: 600; color: {CL_TEXT};")
        layout.addWidget(title)

        desc = QLabel(
            "开启后，程序能听懂更复杂的说法。\n"
            "比如输入「把这行复制一下」→ 自动帮你按 Ctrl+C\n"
            "不开启也能用，只是理解能力会弱一些。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {CL_TEXT_DIM}; font-size: 12px;")
        layout.addWidget(desc)

        status_layout = QHBoxLayout()
        llm_available = self._agent.llm_available
        status_text = "已开启" if llm_available else "未开启"
        status_label = QLabel(status_text)
        status_label.setStyleSheet(f"font-weight: 600; font-size: 14px;")
        status_layout.addWidget(status_label)

        if llm_available and self._agent._llm:
            llm = self._agent._llm
            info = QLabel(f"平均延迟 {llm.avg_latency*1000:.0f}ms  ·  调用 {llm._total_calls} 次")
            info.setStyleSheet(f"color: {CL_TEXT_MUTED}; font-size: 11px;")
            status_layout.addStretch()
            status_layout.addWidget(info)

        layout.addLayout(status_layout)

        key_label = QLabel("识别密钥：")
        layout.addWidget(key_label)

        self._key_input = QLineEdit()
        self._key_input.setPlaceholderText("sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        self._key_input.setEchoMode(QLineEdit.Password)

        existing_key = self._load_api_key()
        if existing_key:
            self._key_input.setText(existing_key)

        layout.addWidget(self._key_input)

        hint = QLabel(
            "获取 Key:<a href='https://platform.deepseek.com/api_keys' "
            f"style='color:{CL_ACCENT}'>platform.deepseek.com/api_keys</a>"
        )
        hint.setOpenExternalLinks(True)
        hint.setStyleSheet(f"font-size: 11px; color: {CL_TEXT_MUTED};")
        layout.addWidget(hint)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setObjectName("btnSecondary")
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        test_btn = QPushButton("测试连接")
        test_btn.setCursor(Qt.PointingHandCursor)
        test_btn.setObjectName("btnSecondary")
        test_btn.clicked.connect(self._test_connection)
        btn_layout.addWidget(test_btn)

        save_btn = QPushButton("确定")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setFixedWidth(80)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {CL_ACCENT}; color: white; border: none;
                border-radius: 4px; padding: 8px 20px; font-size: 13px; font-weight: 600;
            }}
            QPushButton:hover {{ background: {CL_ACCENT_H}; }}
        """)
        save_btn.clicked.connect(self._save_and_close)
        btn_layout.addWidget(save_btn)

        layout.addLayout(btn_layout)

        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        self._result_label.setStyleSheet(f"font-size: 12px; padding: 8px;")
        layout.addWidget(self._result_label)

    def _test_connection(self):
        key = self._key_input.text().strip()
        if not key:
            self._show_result(False, "请输入识别密钥")
            return

        self._show_result(False, "正在测试连接...")
        QApplication.processEvents()

        from .llm import _save_api_key
        _save_api_key(key)

        ok, msg = self._DeepSeekEngine.check_connectivity()
        self._show_result(ok, msg)

    def _save_and_close(self):
        key = self._key_input.text().strip()
        if not key:
            self._show_result(False, "请输入识别密钥")
            return

        ok = self._agent.configure_llm(key)
        if ok:
            self.accept()
        else:
            self._show_result(False, self._agent._llm.last_error or "配置失败")

    def _show_result(self, ok: bool, msg: str):
        color = CL_SUCCESS if ok else CL_DANGER
        self._result_label.setText(msg)
        self._result_label.setStyleSheet(
            f"font-size: 12px; padding: 8px; color: {color};"
        )


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("nl2shortcut")
    app.setApplicationVersion("1.0.0")

    # Force light palette (overrides Windows dark mode / Qt Fusion dark)
    light_palette = QPalette()
    light_palette.setColor(QPalette.Window, QColor(255, 255, 255))
    light_palette.setColor(QPalette.Base, QColor(255, 255, 255))
    light_palette.setColor(QPalette.AlternateBase, QColor(248, 248, 248))
    light_palette.setColor(QPalette.Text, QColor(30, 30, 30))
    light_palette.setColor(QPalette.WindowText, QColor(30, 30, 30))
    light_palette.setColor(QPalette.Button, QColor(240, 240, 240))
    light_palette.setColor(QPalette.ButtonText, QColor(30, 30, 30))
    light_palette.setColor(QPalette.ToolTipBase, QColor(255, 255, 220))
    light_palette.setColor(QPalette.ToolTipText, QColor(30, 30, 30))
    app.setPalette(light_palette)

    app.setStyle(QStyleFactory.create("Fusion"))
    # Apply QSS at app level for consistent background
    app.setStyleSheet(VSCODE_QSS)

    font = QFont("微软雅黑", 10)
    app.setFont(font)

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("nl2shortcut.gui")
        except Exception:
            pass

    window = MainWindow()
    window.show()
    # NL2Shortcut is an Agent execution endpoint — start the API server on launch.
    # This way `nl2shortcut` (no args) means: "give me the GUI + the Agent endpoint".
    window._auto_start_agent_api()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
