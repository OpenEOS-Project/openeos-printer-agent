# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build of the desktop window + tray (separate from the service
binary so headless installs never carry Qt).

    pyinstaller openeos-printer-agent-ui.spec

Windows: one-folder build (dist/openeos-printer-agent-ui/) that the Inno
Setup installer ships - starts fast, no unpacking on every login.
Linux:   single file dist/openeos-printer-agent-ui.
"""

import sys

from PyInstaller.utils.hooks import copy_metadata

WINDOWS = sys.platform.startswith("win")

datas = []
try:
    datas += copy_metadata("openeos-printer-agent")
except Exception:
    pass

hiddenimports = []
if WINDOWS:
    hiddenimports += ["win32service", "win32timezone"]

# Only QtCore/QtGui/QtWidgets/QtNetwork(/QtDBus for the Linux tray) are used.
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "PIL",
    "escpos",
    "zeroconf",
    "jinja2",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtPdf",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtSvg",
    "PySide6.QtXml",
    "PySide6.QtConcurrent",
    "PySide6.QtPrintSupport",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
]

a = Analysis(
    ["pyinstaller_ui_entry.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data)

icon = "packaging/icons/openeos-printer-agent.ico" if WINDOWS else None

if WINDOWS:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="openeos-printer-agent-ui",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="openeos-printer-agent-ui")
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="openeos-printer-agent-ui",
        debug=False,
        strip=False,
        upx=False,
        console=False,
    )
