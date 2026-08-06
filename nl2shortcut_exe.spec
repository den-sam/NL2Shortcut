# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — 把 NL2Shortcut 打包成单 .exe。

用法：
    pyinstaller nl2shortcut_exe.spec

产物：
    dist/NL2Shortcut/NL2Shortcut.exe     （含依赖，可直接双击运行）

关键点：
  - bundle PyQt5（GUI 必需）
  - bundle native_core/output/nl2shortcut_native.dll（C++ 加速模块）
  - hiddenimports：uiautomation / pyautogui 的子模块常被漏掉
  - 不要 bundle 源码 .py（用 .pyc 即可，减小体积）
"""

import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# ── 隐式导入：PyInstaller 静态分析漏掉的动态导入 ──────────────────────
hiddenimports = []
hiddenimports += collect_submodules('PyQt5')
hiddenimports += collect_submodules('pyautogui')
hiddenimports += [
    'yaml',
    'sqlite3',
    'ctypes',
    'ctypes.wintypes',
    'json',
    'pathlib',
    'dataclasses',
    'nl2shortcut',
    'nl2shortcut.cli',
    'nl2shortcut.gui',
    'nl2shortcut.master',
    'nl2shortcut.agent',
    'nl2shortcut.planner',
    'nl2shortcut.workflow',
    'nl2shortcut.workflow_matcher',
    'nl2shortcut.operation_memory',
    'nl2shortcut.execution_controller',
    'nl2shortcut.native_loader',
    'nl2shortcut.precache',
    'nl2shortcut.selfcheck',
    'nl2shortcut.perception',
    'nl2shortcut.intent',
    'nl2shortcut.database',
    'nl2shortcut.tiers',
    'nl2shortcut.context_store',
    'nl2shortcut.llm',
    'nl2shortcut.logger',
    'nl2shortcut.models',
    'nl2shortcut.adapter',
    'nl2shortcut.keyboard_primitives',
    'nl2shortcut.composites',
]

# ── 数据文件：native dll + 数据库 ──────────────────────────────────────
datas = []
# C++ 加速模块 DLL
native_dll = 'nl2shortcut/native_core/output/nl2shortcut_native.dll'
if os.path.exists(native_dll):
    datas.append((native_dll, 'nl2shortcut/native_core/output'))
# 备用位置（根目录也有一份）
root_dll = 'nl2shortcut/nl2shortcut_native.dll'
if os.path.exists(root_dll):
    datas.append((root_dll, 'nl2shortcut'))

# PyQt5 数据文件（图标、翻译等）
datas += collect_data_files('PyQt5')


a = Analysis(
    ['nl2shortcut/__main__.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 排除不需要的大模块，减小体积
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
        'spacy',  # 可选依赖，不打包
        'pynput',  # 可选依赖
        'IPython',
        'notebook',
        'jupyter',
        'pytest',
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(
    a.pure,
    a.zipped_data,
    cipher=block_cipher,
)

# ── 单文件夹模式（比 onefile 启动快，且避免 onefile 的解压延迟） ────────
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NL2Shortcut',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # GUI 程序，不显示控制台
    icon=None,  # TODO: 可添加 icon='assets/icon.ico'
    # 修复 Windows 上高 DPI 模糊
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NL2Shortcut',
)
