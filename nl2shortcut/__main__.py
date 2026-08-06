"""Allow running as python -m nl2shortcut.

无参数 → 启动 GUI（双击 .exe 的默认行为）
有参数 → 走 CLI（如 `python -m nl2shortcut exec 复制`）
"""

import sys


def _looks_like_cli(args):
    """判断是否是 CLI 调用（有任意参数）。"""
    return bool(args)


def main():
    args = sys.argv[1:]
    if not _looks_like_cli(args):
        # 无参数 → 启动 GUI
        from .gui import main as gui_main
        gui_main()
    else:
        # 有参数 → 走 CLI
        from .cli import main as cli_main
        cli_main()


if __name__ == "__main__":
    main()
