# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile build for the release binaries.

    pyinstaller openeos-printer-agent.spec

Produces dist/openeos-printer-agent(.exe). Templates and python-escpos'
capabilities data are bundled as data files; the Windows-only modules are
listed as hidden imports so the service wrapper works in the frozen exe.
"""

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
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "pandas"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
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
)
