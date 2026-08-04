"""AgentPanel — NL2Shortcut is the Agent's high-speed execution endpoint.

In this panel users can:
  1. Start/stop the Agent API server (the endpoint itself)
  2. Try the chat demo: type an intent, watch the endpoint resolve it
  3. Browse the 51 actions with Agent metadata:
     - stability (how reliably it works across app versions)
     - api_equivalent (a native API Agent can call instead)
     - gui_fallback (how a GUI Agent would do it)

The chat side is intentionally simple — the real Agent loop
(OpenClaw / Claude / AutoGLM) is what NL2Shortcut serves. Here you
just see the contract in action.
"""

import json
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from PyQt5.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
        QTextEdit, QFrame,
        QCheckBox, QGroupBox, QSizePolicy, QApplication, QStatusBar,
    )
    from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
    from PyQt5.QtGui import QColor
except ImportError:
    pass


# Color palette — must match gui.py top
CL_BG_MAIN    = "#FFFFFF"
CL_BG_PANEL   = "#F8F8F8"
CL_BG_INPUT   = "#FFFFFF"
CL_BORDER     = "#E0E0E0"
CL_TEXT       = "#1E1E1E"
CL_TEXT_DIM   = "#616161"
CL_TEXT_MUTED = "#999999"
CL_ACCENT     = "#0078D4"
CL_ACCENT_H   = "#1A8CDC"
CL_ACCENT_P   = "#0066B4"
CL_ACCENT_BG  = "#E4F0FD"
CL_SUCCESS    = "#388A34"
CL_WARNING    = "#DBA11A"
CL_DANGER     = "#D73A49"
CL_INFO       = "#0078D4"


def is_server_online(endpoint: str, timeout: float = 3.0) -> bool:
    """Check if an NL2Shortcut server is reachable at the given endpoint."""
    try:
        req = urllib.request.Request(f"{endpoint.rstrip('/')}/v1/health", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


class _ApiServerThread(QThread):
    """Background thread that runs the Agent API server."""
    log = pyqtSignal(str)
    def __init__(self, host: str = "127.0.0.1", port: int = 7770):
        super().__init__()
        self._host = host
        self._port = port
        self._server = None
    def run(self):
        try:
            from .agent_api import _ThreadingHTTPServer, _Handler
            from .agent import ShortcutAgent
            agent = ShortcutAgent()
            handler_cls = type("_BoundHandler", (_Handler,), {"agent": agent})
            self._server = _ThreadingHTTPServer((self._host, self._port), handler_cls)
            self.log.emit(f"Agent API listening on http://{self._host}:{self._port}")
            self._server.serve_forever()
        except OSError as e:
            self.log.emit(f"Bind failed on {self._host}:{self._port} — {e}")
        except Exception as e:
            self.log.emit(f"Server crashed: {e}")


class _ApiCaller(QThread):
    """Background thread for HTTP calls so the UI never freezes."""
    finished = pyqtSignal(dict)
    def __init__(self, endpoint: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 15.0):
        super().__init__()
        self._endpoint = endpoint.rstrip("/")
        self._path = path
        self._payload = payload
        self._timeout = timeout
    def run(self):
        url = f"{self._endpoint}{self._path}"
        try:
            if self._payload is None:
                req = urllib.request.Request(url, method="GET")
            else:
                body = json.dumps(self._payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={"Content-Type": "application/json"},
                )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                txt = resp.read().decode("utf-8")
                self.finished.emit({"ok": True, "body": json.loads(txt)})
        except urllib.error.HTTPError as e:
            self.finished.emit({"ok": False, "error": f"HTTP {e.code}", "body": e.read().decode("utf-8", errors="replace")})
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            self.finished.emit({"ok": False, "error": str(e)})
        except Exception as e:
            self.finished.emit({"ok": False, "error": repr(e)})


class _SmartExecuteThread(QThread):
    """后台线程：工作流优先智能执行管道。

    Pipeline:
      WorkflowMatcher.match() → 命中 → 加载工作流 → 执行
                            → 未命中 → GoalPlanner.plan() → 执行 → plan_to_workflow() 保存
    """
    finished = pyqtSignal(dict)

    def __init__(self, intent: str, dry_run: bool, timeout: float = 30.0):
        super().__init__()
        self._intent = intent
        self._dry_run = dry_run
        self._timeout = timeout

    def run(self):
        try:
            from .master import KeyboardMasterAgent
            master = KeyboardMasterAgent()
            result = master.smart_execute(
                self._intent,
                dry_run=self._dry_run,
                timeout=self._timeout,
                learn=True,
                auto_save=True,
            )
            self.finished.emit(result)
        except Exception as e:
            self.finished.emit({
                "ok": False,
                "pipeline": "error",
                "error": str(e),
                "intent": self._intent,
                "elapsed_ms": 0,
                "matched_workflow": None,
                "match_confidence": 0.0,
                "auto_saved": False,
                "auto_saved_path": None,
                "plan": None,
                "steps_executed": 0,
                "results": [],
            })


class AgentPanel(QWidget):
    """Agent control center: server control + chat demo + metadata browser."""

    DEFAULT_ENDPOINT = "http://127.0.0.1:7770"

    def __init__(self, agent, parent=None):
        super().__init__(parent)
        self._agent = agent
        self._server_thread: Optional[_ApiServerThread] = None
        self._caller: Optional[_ApiCaller] = None
        self._endpoint = self.DEFAULT_ENDPOINT
        self._setup_ui()
        self._install_shortcuts()
        # Poll server status every 2s
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_status)
        self._timer.start(2000)
        self._poll_status()

    def _install_shortcuts(self):
        """Agent panel local shortcuts."""
        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence
        S = QShortcut
        K = QKeySequence
        # Ctrl+B  toggle API server
        S(K("Ctrl+B"), self).activated.connect(self._toggle_server)
        # Ctrl+L  focus chat input
        S(K("Ctrl+L"), self).activated.connect(self._chat_input.setFocus)
        # Enter  send (already wired in _setup_ui, but redundant registration
        # ensures it works from any widget in the panel)

    def _toggle_server(self):
        if self._server_thread and self._server_thread.isRunning():
            self._stop_server()
        else:
            self._start_server()

    # ── 通用对话框 ─────────────────────────────────────────────────
    def _show_confirm_dialog(self, parent, title: str, message: str) -> bool:
        """显示带「取消」「确定」按钮的确认对话框，返回 True 表示用户点了确定。"""
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        dlg = QDialog(parent)
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
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setFixedWidth(80)
        cancel_btn.setStyleSheet(self._secondary_btn_style())
        cancel_btn.clicked.connect(dlg.reject)
        btn_layout.addWidget(cancel_btn)

        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setFixedWidth(80)
        ok_btn.setStyleSheet(self._primary_btn_style())
        ok_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

        return dlg.exec_() == QDialog.Accepted

    def _show_info_dialog(self, parent, title: str, message: str):
        """显示带「确定」按钮的提示对话框。"""
        from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        dlg = QDialog(parent)
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
        ok_btn.setStyleSheet(self._primary_btn_style())
        ok_btn.clicked.connect(dlg.accept)
        btn_layout.addWidget(ok_btn)

        layout.addLayout(btn_layout)

        dlg.exec_()

    def _show_workflows(self):
        """Open a dialog listing all workflows with run/dry-run actions."""
        from PyQt5.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                     QLabel, QPushButton, QScrollArea, QFrame)
        from .workflow import WorkflowEngine
        from .agent import ShortcutAgent

        dlg = QDialog(self)
        dlg.setWindowTitle("工作流管理 — NL2Shortcut")
        dlg.resize(600, 450)
        dlg.setStyleSheet(self.styleSheet())
        layout = QVBoxLayout(dlg)

        title = QLabel("📋 可用工作流")
        title.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {CL_TEXT};")
        layout.addWidget(title)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: 1px solid {CL_BORDER}; border-radius: 4px; background: {CL_BG_MAIN}; }}")
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setSpacing(6)

        try:
            engine = WorkflowEngine(ShortcutAgent())
            names = engine.list_workflows()
            if not names:
                cl.addWidget(QLabel("暂无工作流。通过 nl2shortcut workflow create 创建。"))
            else:
                for wf_name in sorted(names):
                    wf = engine.load(wf_name)
                    row = QFrame()
                    row.setStyleSheet(f"QFrame {{ background: {CL_BG_PANEL}; border-radius: 4px; padding: 8px; }}")
                    rl = QHBoxLayout(row)
                    rl.setContentsMargins(12, 8, 12, 8)

                    info = QLabel(f"<b>{wf_name}</b><br><span style='color:{CL_TEXT_DIM};font-size:11px;'>{wf.description}</span>")
                    info.setTextFormat(Qt.RichText)
                    rl.addWidget(info, 1)

                    btn_dry = QPushButton("预览")
                    btn_dry.setFixedWidth(50)
                    btn_dry.setStyleSheet(self._secondary_btn_style())
                    btn_dry.clicked.connect(lambda checked, n=wf_name: _run_wf(n, dry=True))
                    rl.addWidget(btn_dry)

                    btn_run = QPushButton("执行")
                    btn_run.setFixedWidth(50)
                    btn_run.setStyleSheet(self._primary_btn_style())
                    btn_run.clicked.connect(lambda checked, n=wf_name: _run_wf(n, dry=False))
                    rl.addWidget(btn_run)

                    cl.addWidget(row)
        except Exception as e:
            cl.addWidget(QLabel(f"加载失败: {e}"))

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        def _run_wf(name, dry=False):
            try:
                if not dry:
                    ok = self._show_confirm_dialog(
                        dlg,
                        "确认执行",
                        f"确定要执行工作流「{name}」吗？\n\n"
                        f"执行期间会实际发送按键/运行命令。",
                    )
                    if not ok:
                        return
                eng = WorkflowEngine(ShortcutAgent())
                result = eng.run(name, dry_run=dry)
                if result.success:
                    steps_info = "\n".join(
                        f"  {'✅' if s.success else '❌'} {s.step_name}: {s.output[:40]}"
                        for s in result.steps
                    )
                    mode = "预览" if dry else "执行"
                    self._show_info_dialog(dlg, f"{mode}完成",
                        f"工作流 '{name}' {mode}完成\n{steps_info}")
                else:
                    self._show_info_dialog(dlg, "失败", f"工作流 '{name}' 失败:\n{result.error}")
            except Exception as e:
                self._show_info_dialog(dlg, "错误", str(e))

        # 底部按钮行：取消 + 关闭
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setStyleSheet(self._secondary_btn_style())
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setFixedWidth(80)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        close_btn = QPushButton("确定")
        close_btn.setStyleSheet(self._primary_btn_style())
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        dlg.exec_()

    # ── UI construction ──────────────────────────────────────────────
    def _setup_ui(self):
        self.setObjectName("contentArea")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 24)
        root.setSpacing(16)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("🤖 Agent 控制台")
        title.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {CL_TEXT};")
        hdr.addWidget(title)
        hdr.addStretch()
        root.addLayout(hdr)

        # ── Server control bar ──
        bar = QFrame()
        bar.setObjectName("card")
        bar.setStyleSheet(f"""
            QFrame#card {{
                background: {CL_BG_PANEL};
                border: 1px solid {CL_BORDER};
                border-radius: 6px;
            }}
        """)
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(12)

        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet(f"color: {CL_TEXT_MUTED}; font-size: 14px;")
        bl.addWidget(self._status_dot)

        self._status_text = QLabel("状态：未检测")
        self._status_text.setStyleSheet(f"color: {CL_TEXT}; font-size: 13px; font-weight: 600;")
        bl.addWidget(self._status_text)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {CL_BORDER};")
        bl.addWidget(sep)

        self._endpoint_label = QLabel(f"端点：{self.DEFAULT_ENDPOINT}")
        self._endpoint_label.setStyleSheet(f"color: {CL_TEXT_DIM}; font-size: 12px; font-family: 'Consolas', 'Microsoft YaHei';")
        bl.addWidget(self._endpoint_label)
        bl.addStretch()

        self._btn_toggle = QPushButton("▶ 启动 Agent API")
        self._btn_toggle.setCursor(Qt.PointingHandCursor)
        self._btn_toggle.setStyleSheet(self._primary_btn_style())
        self._btn_toggle.clicked.connect(self._toggle_server)
        bl.addWidget(self._btn_toggle)

        self._btn_refresh = QPushButton("刷新")
        self._btn_refresh.setCursor(Qt.PointingHandCursor)
        self._btn_refresh.setStyleSheet(self._secondary_btn_style())
        self._btn_refresh.clicked.connect(self._poll_status)
        bl.addWidget(self._btn_refresh)

        self._btn_workflows = QPushButton("📋 工作流")
        self._btn_workflows.setCursor(Qt.PointingHandCursor)
        self._btn_workflows.setStyleSheet(self._secondary_btn_style())
        self._btn_workflows.clicked.connect(self._show_workflows)
        bl.addWidget(self._btn_workflows)

        root.addWidget(bar)

        # ── Two-column body ──
        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        # ── LEFT: Chat / demo loop ──
        chat_card = QFrame()
        chat_card.setObjectName("chatCard")
        chat_card.setStyleSheet(f"""
            QFrame#chatCard {{
                background: {CL_BG_PANEL};
                border: none;
                border-radius: 6px;
            }}
        """)
        cl = QVBoxLayout(chat_card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        ch_title = QLabel("💬  Agent 演示对话")
        ch_title.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {CL_TEXT}; padding: 14px 16px 8px 16px; background: transparent;")
        cl.addWidget(ch_title)

        # Chat history
        self._chat = QTextEdit()
        self._chat.setReadOnly(True)
        self._chat.setStyleSheet(f"""
            QTextEdit {{
                background: transparent;
                color: {CL_TEXT};
                border: none;
                padding: 8px 16px;
                font-family: 'Microsoft YaHei', 'Segoe UI';
                font-size: 13px;
            }}
        """)
        cl.addWidget(self._chat, 1)

        # Input row — 带边框，融入卡片风格
        in_row = QHBoxLayout()
        in_row.setContentsMargins(16, 8, 16, 12)
        in_row.setSpacing(8)
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText('试试："保存文件"、"帮我撤销"、"format code"...')
        self._chat_input.setMinimumHeight(32)
        self._chat_input.setStyleSheet(f"""
            QLineEdit {{
                background: {CL_BG_INPUT};
                color: {CL_TEXT};
                border: 1px solid {CL_BORDER};
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border: 1px solid {CL_ACCENT};
            }}
        """)
        self._chat_input.returnPressed.connect(self._on_chat_send)
        in_row.addWidget(self._chat_input, 1)

        self._dry_run_chk = QCheckBox("仅预览")
        self._dry_run_chk.setToolTip(
            "勾选 → 只显示要按的键，不真发键（预览 / Dry-run）\n"
            "取消 → 实际向系统发送按键（会触发复制/粘贴/撤销等效果）"
        )
        self._dry_run_chk.setCursor(Qt.PointingHandCursor)
        self._dry_run_chk.setStyleSheet(
            f"QCheckBox {{ color: {CL_TEXT_MUTED}; font-size: 12px; spacing: 4px; }}"
            f"QCheckBox::indicator {{ width: 14px; height: 14px; }}"
        )
        in_row.addWidget(self._dry_run_chk)

        self._btn_send = QPushButton("发送")
        self._btn_send.setCursor(Qt.PointingHandCursor)
        self._btn_send.setMinimumHeight(38)
        self._btn_send.setMinimumWidth(80)
        self._btn_send.setStyleSheet(self._primary_btn_style())
        self._btn_send.clicked.connect(self._on_chat_send)
        in_row.addWidget(self._btn_send)

        cl.addLayout(in_row)
        body.addWidget(chat_card, 1)

        # Initial chat
        self._append_chat("system", "👋  我是 NL2Shortcut — Keyboard Master Agent。\n\n"
            "执行逻辑：\n"
            "  ① 匹配已有工作流 → 直接执行\n"
            "  ② 无匹配 → LLM 自动拆解 → 执行 → 自动保存为新工作流\n\n"
            "试试：\n"
            "  • \"保存这个文件\"\n"
            "  • \"帮我撤销刚才的操作\"\n"
            "  • \"复制这一段\"\n\n"
            "启动上方 Agent API 后，外部 AI Agent 可通过 HTTP 调用我。")

    # ── Chat logic ───────────────────────────────────────────────────
    def _on_chat_send(self):
        text = self._chat_input.text().strip()
        if not text:
            return
        self._chat_input.clear()
        self._append_chat("user", text)

        # 工作流优先智能执行
        dry_run = self._dry_run_chk.isChecked()
        self._append_chat("thinking", "🔍  正在匹配已有工作流...")
        if self._caller and self._caller.isRunning():
            return
        self._caller = _SmartExecuteThread(text, dry_run=dry_run)
        self._caller.finished.connect(self._on_chat_response)
        self._caller.start()

    def _on_chat_response(self, result: Dict[str, Any]):
        # Remove the "thinking" line
        cursor = self._chat.textCursor()
        cursor.movePosition(cursor.End)
        cursor.select(cursor.LineUnderCursor)
        cursor.removeSelectedText()
        cursor.deletePreviousChar()

        pipeline = result.get("pipeline", "error")
        elapsed = result.get("elapsed_ms", 0)
        steps_executed = result.get("steps_executed", 0)
        matched_wf = result.get("matched_workflow")
        auto_saved = result.get("auto_saved", False)
        auto_saved_path = result.get("auto_saved_path")
        step_results = result.get("results", [])

        # ── error ──
        if pipeline == "error" or (not result.get("ok") and result.get("error")):
            self._append_chat("error",
                f"❌  执行失败：{result.get('error', '未知错误')}\n\n"
                f"请检查输入是否正确，或尝试其他写法。")
            return

        # ── workflow_match ──
        if pipeline == "workflow_match":
            status = "✅" if result["ok"] else "⚠️"
            # Header
            wf_header = (
                f'<div style="margin: 10px 20% 10px 0; background: #FFFFFF; '
                f'border: 1px solid {CL_BORDER}; '
                f'border-radius: 4px 14px 14px 14px; padding: 12px 16px;">'
                f'<div style="color: {CL_TEXT_MUTED}; font-size: 10px; font-weight: 600; '
                f'margin-bottom: 4px;">Agent</div>'
                f'<div style="color: {CL_TEXT}; font-size: 14px; line-height: 1.6;">'
                f'{status}  工作流匹配执行 &nbsp;·&nbsp; '
                f'<span style="color:{CL_ACCENT};font-weight:600;">{matched_wf}</span> &nbsp;·&nbsp; '
                f'{steps_executed} 步 &nbsp;·&nbsp; {elapsed:.0f}ms'
                f'</div>'
                f'</div>'
            )
            self._chat.append(wf_header)

            # Step results as individual cards
            for i, sr in enumerate(step_results):
                icon = "✅" if sr.get("success") else "❌"
                step_name = sr.get("step", f"步骤{i+1}")
                output = sr.get("output", "")
                err = sr.get("error", "")
                result_text = f"{icon} {step_name}"
                if output:
                    result_text += f' → <span style="color:{CL_ACCENT};">{output[:40]}</span>'
                if err:
                    result_text += f' <span style="color:{CL_DANGER};">({err})</span>'
                step_html = (
                    f'<div style="margin: 4px 20% 4px 0; background: {CL_BG_PANEL}; '
                    f'border: 1px solid {CL_BORDER}; border-left: 3px solid {CL_SUCCESS}; '
                    f'border-radius: 6px; padding: 8px 14px; font-size: 13px;">'
                    f'<span style="background: {CL_SUCCESS}; color: white; border-radius: 10px; '
                    f'padding: 1px 8px; font-size: 11px; font-weight: 600; margin-right: 8px;">{i+1}</span>'
                    f'<span style="color: {CL_TEXT};">{result_text}</span>'
                    f'</div>'
                )
                self._chat.append(step_html)

        # ── planner_generated ──
        elif pipeline == "planner_generated":
            status = "✅" if result["ok"] else "⚠️"
            plan = result.get("plan")
            plan_steps = plan.get("steps", []) if plan else []

            # Header
            header_html = (
                f'<div style="margin: 10px 20% 10px 0; background: #FFFFFF; '
                f'border: 1px solid {CL_BORDER}; '
                f'border-radius: 4px 14px 14px 14px; padding: 12px 16px;">'
                f'<div style="color: {CL_TEXT_MUTED}; font-size: 10px; font-weight: 600; '
                f'margin-bottom: 4px;">Agent</div>'
                f'<div style="color: {CL_TEXT}; font-size: 14px; line-height: 1.6;">'
                f'{status}  LLM 拆解执行 &nbsp;·&nbsp; {len(plan_steps)} 步 &nbsp;·&nbsp; {elapsed:.0f}ms'
                f'</div>'
                f'</div>'
            )
            self._chat.append(header_html)

            # Step-by-step cards
            for i, ps in enumerate(plan_steps):
                action = ps.get("action", "?")
                desc = ps.get("description", "")
                key = ps.get("key_combination", "")
                text = ps.get("text", "")
                cmd = ps.get("command", "")
                wait_ms = ps.get("wait_ms", 0)
                confidence = ps.get("confidence", 1.0)
                reasoning = ps.get("reasoning", "")

                # Determine step detail
                if action == "shortcut" and key:
                    detail = f'<span style="color:{CL_ACCENT};font-weight:600;">{key}</span>'
                elif action == "type" and text:
                    detail = f'输入 "<span style="color:{CL_ACCENT};">{text[:30]}</span>"'
                elif action == "shell" and cmd:
                    detail = f'<span style="color:{CL_WARNING};">$</span> {cmd[:50]}'
                elif action == "wait":
                    detail = f'等待 {wait_ms}ms'
                elif action == "composite":
                    detail = f'<span style="color:{CL_INFO};">🔍</span> {ps.get("composite_hint", "")[:50]}'
                elif action == "tab":
                    direction_map = {"tab": "Tab", "shift_tab": "Shift+Tab",
                                     "left": "←", "right": "→", "up": "↑", "down": "↓"}
                    d = direction_map.get(ps.get("direction", "tab"), "Tab")
                    n = ps.get("n", 1)
                    detail = f'{d} × {n}'
                else:
                    detail = action

                step_html = (
                    f'<div style="margin: 4px 20% 4px 0; background: {CL_BG_PANEL}; '
                    f'border: 1px solid {CL_BORDER}; border-left: 3px solid {CL_ACCENT}; '
                    f'border-radius: 6px; padding: 8px 14px; font-size: 13px;">'
                    f'<div style="display: flex; align-items: center; gap: 8px;">'
                    f'<span style="background: {CL_ACCENT}; color: white; border-radius: 10px; '
                    f'padding: 1px 8px; font-size: 11px; font-weight: 600;">{i+1}</span>'
                    f'<span style="color: {CL_TEXT}; font-weight: 600;">{desc}</span>'
                    f'<span style="color: {CL_TEXT_DIM}; font-size: 12px; margin-left: auto;">{detail}</span>'
                    f'</div>'
                )
                if confidence < 1.0:
                    conf_color = CL_WARNING if confidence >= 0.5 else CL_DANGER
                    step_html += (
                        f'<div style="color: {conf_color}; font-size: 11px; '
                        f'padding-left: 28px; margin-top: 2px;">'
                        f'置信度 {confidence:.0%}</div>'
                    )
                if reasoning:
                    step_html += (
                        f'<div style="color: {CL_TEXT_MUTED}; font-size: 11px; '
                        f'padding-left: 28px; margin-top: 2px; font-style: italic;">'
                        f'💡 {reasoning}</div>'
                    )
                step_html += '</div>'
                self._chat.append(step_html)

            # Execution results
            if step_results:
                result_header = (
                    f'<div style="margin: 6px 20% 4px 0; font-size: 12px; color: {CL_TEXT_DIM}; '
                    f'padding-left: 4px; font-weight: 600;">执行结果：</div>'
                )
                self._chat.append(result_header)
                for i, sr in enumerate(step_results):
                    icon = "✅" if sr.get("success") else "❌"
                    step_name = sr.get("step", f"步骤{i+1}")
                    output = sr.get("output", "")
                    err = sr.get("error", "")
                    result_text = f"{icon} {step_name}"
                    if output:
                        result_text += f' → <span style="color:{CL_ACCENT};">{output[:40]}</span>'
                    if err:
                        result_text += f' <span style="color:{CL_DANGER};">({err})</span>'
                    result_html = (
                        f'<div style="margin: 2px 20% 2px 0; font-size: 12px; '
                        f'padding: 4px 12px; color: {CL_TEXT};">{result_text}</div>'
                    )
                    self._chat.append(result_html)

            # Workflow save notification
            if auto_saved:
                saved_html = (
                    f'<div style="margin: 10px 20% 10px 0; background: {CL_ACCENT_BG}; '
                    f'border: 1px solid {CL_ACCENT}; '
                    f'border-radius: 6px; padding: 10px 14px;">'
                    f'<div style="color: {CL_ACCENT_P}; font-size: 12px; font-weight: 600;">'
                    f'📁  已自动保存为工作流</div>'
                    f'<div style="color: {CL_TEXT}; font-size: 12px; margin-top: 4px;">'
                    f'<b>文件名：</b>{Path(auto_saved_path).name if auto_saved_path else "?"}<br>'
                    f'<b>路径：</b>{auto_saved_path or "?"}'
                    f'</div>'
                    f'<div style="color: {CL_TEXT_DIM}; font-size: 11px; margin-top: 4px;">'
                    f'下次说相同意图时将直接匹配此工作流执行'
                    f'</div>'
                    f'</div>'
                )
                self._chat.append(saved_html)
        elif pipeline == "single_shortcut":
            sr = step_results[0] if step_results else {}
            status = "✅" if result["ok"] else "❌"
            output = sr.get("output", "?")
            err = sr.get("error", "")
            shortcut_html = (
                f'<div style="margin: 10px 20% 10px 0; background: #FFFFFF; '
                f'border: 1px solid {CL_BORDER}; '
                f'border-radius: 4px 14px 14px 14px; padding: 12px 16px;">'
                f'<div style="color: {CL_TEXT_MUTED}; font-size: 10px; font-weight: 600; '
                f'margin-bottom: 4px;">Agent</div>'
                f'<div style="color: {CL_TEXT}; font-size: 14px; line-height: 1.6;">'
                f'{status}  快捷执行 &nbsp;·&nbsp; '
                f'<span style="color:{CL_ACCENT};font-weight:600;">{output}</span> &nbsp;·&nbsp; '
                f'{elapsed:.0f}ms'
                f'</div>'
            )
            if err:
                shortcut_html += f'<div style="color: {CL_DANGER}; font-size: 12px; margin-top: 4px;">⚠ {err}</div>'
            shortcut_html += '</div>'
            self._chat.append(shortcut_html)

        else:
            unknown_html = (
                f'<div style="margin: 10px 20% 10px 0; background: #FFFFFF; '
                f'border: 1px solid {CL_DANGER}; '
                f'border-radius: 4px 14px 14px 14px; padding: 12px 16px;">'
                f'<div style="color: {CL_TEXT_MUTED}; font-size: 10px; font-weight: 600; '
                f'margin-bottom: 4px;">Agent</div>'
                f'<div style="color: {CL_DANGER}; font-size: 14px;">❌ 未知管道：{pipeline}</div>'
                f'</div>'
            )
            self._chat.append(unknown_html)

    def _append_chat(self, who: str, text: str):
        safe = text.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

        if who == "user":
            # 用户气泡：右侧浅色（与 Agent 气泡对称，黑字清晰可读）
            html = (
                f'<div style="margin: 10px 0 10px 20%; background: {CL_ACCENT_BG}; '
                f'border: 1px solid {CL_ACCENT}; '
                f'border-radius: 14px 14px 4px 14px; padding: 12px 16px;">'
                f'<div style="color: {CL_ACCENT_P}; font-size: 10px; font-weight: 600; '
                f'margin-bottom: 4px; text-align: right;">你</div>'
                f'<div style="color: {CL_TEXT}; font-size: 14px; line-height: 1.6; text-align: right;">{safe}</div>'
                f'</div>'
            )
        elif who == "agent":
            # Agent 气泡：左侧白底
            html = (
                f'<div style="margin: 10px 20% 10px 0; background: #FFFFFF; '
                f'border: 1px solid #E8E8E8; '
                f'border-radius: 4px 14px 14px 14px; padding: 12px 16px;">'
                f'<div style="color: {CL_TEXT_MUTED}; font-size: 10px; font-weight: 600; '
                f'margin-bottom: 4px;">Agent</div>'
                f'<div style="color: {CL_TEXT}; font-size: 14px; line-height: 1.6;">{safe}</div>'
                f'</div>'
            )
        elif who == "error":
            # 错误气泡：左侧浅红
            html = (
                f'<div style="margin: 10px 20% 10px 0; background: #FFEBEE; '
                f'border-radius: 4px 14px 14px 14px; padding: 12px 16px;">'
                f'<div style="color: {CL_DANGER}; font-size: 10px; font-weight: 600; '
                f'margin-bottom: 4px;">⚠ 错误</div>'
                f'<div style="color: {CL_TEXT}; font-size: 14px; line-height: 1.6;">{safe}</div>'
                f'</div>'
            )
        elif who == "system":
            # 系统消息：居中轻量
            html = (
                f'<div style="margin: 6px 25%; text-align: center; color: {CL_TEXT_MUTED}; '
                f'font-size: 12px; padding: 4px 8px; line-height: 1.5;">{safe}</div>'
            )
        elif who == "thinking":
            # 思考中：居中斜体
            html = (
                f'<div style="margin: 6px 25%; text-align: center; color: {CL_TEXT_MUTED}; '
                f'font-size: 12px; padding: 4px 8px; font-style: italic;">{safe}</div>'
            )
        else:
            html = safe

        self._chat.append(html)

    # ── Server control ───────────────────────────────────────────────
    def _start_server(self):
        # Check if a server is already running externally
        if is_server_online(self._endpoint):
            self._status_text.setText(f"状态：在线（已连接）")
            self._status_dot.setStyleSheet(f"color: {CL_SUCCESS}; font-size: 14px;")
            self._btn_toggle.setText("■ 停止 Agent API")
            self._btn_toggle.setStyleSheet(self._danger_btn_style())
            self._btn_toggle.setEnabled(True)
            self._btn_refresh.setEnabled(True)
            self._append_chat("system", f"✅  已连接到 Agent API on {self.DEFAULT_ENDPOINT}")
            return

        if self._server_thread and self._server_thread.isRunning():
            return
        # Start server via subprocess (avoids binding conflicts with standalone server)
        import subprocess, sys
        self._btn_toggle.setEnabled(False)
        self._btn_toggle.setText("启动中...")
        self._status_text.setText("状态：启动中...")
        self._status_dot.setStyleSheet(f"color: {CL_WARNING}; font-size: 14px;")
        self._append_chat("system", f"🚀  正在启动 Agent API on {self.DEFAULT_ENDPOINT} ...")
        try:
            subprocess.Popen(
                [sys.executable, "-m", "nl2shortcut", "start-server", "--port", "7770"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self._append_chat("error", f"启动失败: {e}")
            self._btn_toggle.setEnabled(True)

    def _stop_server(self):
        if self._server_thread:
            # Thread is serving forever; we just mark intent — cleanest way is to
            # shut down the underlying server. Since we hold a ref, we can call shutdown.
            try:
                if self._server_thread._server:
                    self._server_thread._server.shutdown()
                    self._server_thread._server.server_close()
            except Exception:
                pass
            self._server_thread = None
        self._btn_toggle.setText("▶ 启动 Agent API")
        self._btn_toggle.setStyleSheet(self._primary_btn_style())
        self._btn_refresh.setEnabled(False)
        self._status_text.setText("状态：已停止")
        self._status_dot.setStyleSheet(f"color: {CL_DANGER}; font-size: 14px;")
        self._append_chat("system", "⏹  Agent API 已停止。")

    def _on_server_log(self, msg: str):
        self._append_chat("system", msg)

    def _poll_status(self):
        # Background quick check
        # NOTE: Server cold-start can take ~2s (import + init), so use 3s timeout
        try:
            req = urllib.request.Request(f"{self._endpoint}/v1/health", method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as r:
                body = json.loads(r.read().decode("utf-8"))
                if body.get("ok"):
                    self._status_text.setText(f"状态：在线  ·  v{body.get('version', '?')}")
                    self._status_dot.setStyleSheet(f"color: {CL_SUCCESS}; font-size: 14px;")
                    self._btn_toggle.setText("■ 停止 Agent API")
                    self._btn_toggle.setStyleSheet(self._danger_btn_style())
                    self._btn_refresh.setEnabled(True)
                    return
        except Exception:
            pass
        # offline
        if not (self._server_thread and self._server_thread.isRunning()):
            self._status_text.setText("状态：离线")
            self._status_dot.setStyleSheet(f"color: {CL_DANGER}; font-size: 14px;")
            self._btn_toggle.setText("▶ 启动 Agent API")
            self._btn_toggle.setStyleSheet(self._primary_btn_style())
            self._btn_refresh.setEnabled(False)

    # ── Metadata browser ─────────────────────────────────────────────
    # ── Styles ───────────────────────────────────────────────────────
    def _primary_btn_style(self):
        return f"""
            QPushButton {{
                background: {CL_ACCENT};
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {CL_ACCENT_H}; }}
            QPushButton:pressed {{ background: {CL_ACCENT_P}; }}
            QPushButton:disabled {{ background: {CL_BORDER}; color: {CL_TEXT_MUTED}; }}
        """
    def _secondary_btn_style(self):
        return f"""
            QPushButton {{
                background: {CL_BG_MAIN};
                color: {CL_TEXT};
                border: 1px solid {CL_BORDER};
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 12px;
            }}
            QPushButton:hover {{ background: {CL_BG_PANEL}; border-color: {CL_ACCENT}; }}
            QPushButton:disabled {{ color: {CL_TEXT_MUTED}; background: {CL_BG_PANEL}; }}
        """

    def _danger_btn_style(self):
        return f"""
            QPushButton {{
                background: #D32F2F;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 6px 14px;
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton:hover {{ background: #B71C1C; }}
            QPushButton:pressed {{ background: #7F0000; }}
        """

    def closeEvent(self, ev):
        if self._server_thread and self._server_thread.isRunning():
            try:
                if self._server_thread._server:
                    self._server_thread._server.shutdown()
            except Exception:
                pass
        super().closeEvent(ev)
