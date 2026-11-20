# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile build for the release binaries.

    pyinstaller openeos-printer-agent.spec

Produces dist/openeos-printer-agent(.exe). Templates and python-escpos'
capabilities data are bundled as data files; the Windows-only modules are
listed as hidden imports so the service wrapper works in the frozen exe.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

block_cipher = None

datas = [("src/templates", "src/templates")]
datas += collect_data_files("escpos")
# importlib.metadata.version("openeos-printer-agent") in src/version.py -
# present after `pip install .`; without it the constant in version.py is used.
try:
    datas += copy_metadata("openeos-printer-agent")
except Exception:
    pass

hiddenimports = collect_submodules("zeroconf") + collect_submodules("escpos")
if sys.platform.startswith("win"):
    hiddenimports += [
        "win32timezone",
        "win32print",
        "win32service",
        "win32serviceutil",
        "win32event",
        "win32security",
        "servicemanager",
        "libusb_package",
    ]
    datas += collect_data_files("libusb_package")

a = Analysis(
    ["pyinstaller_entry.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "pandas", "PySide6", "shiboken6"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
icon = "packaging/icons/openeos-printer-agent.ico" if sys.platform.startswith("win") else None

# OPENEOS_ONEDIR=1: one-folder build for the Windows installer - the service
# starts in about a second instead of unpacking ~30 MB to %TEMP% (and having
# Defender scan it) on every start. Default: the single-file release binary.
if os.environ.get("OPENEOS_ONEDIR") == "1":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="openeos-printer-agent",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="openeos-printer-agent")
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="openeos-printer-agent",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        icon=icon,
    )
