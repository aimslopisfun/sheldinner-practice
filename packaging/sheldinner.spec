# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Sheldinner Practice (one-file binary).

Build with:
    python packaging/build.py
or directly:
    pyinstaller --noconfirm packaging/sheldinner.spec

Produces a single self-contained executable in dist/.
"""

from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_data_files, collect_submodules, collect_dynamic_libs,
)

# Resolve the repo root robustly whether this spec is run from the repo root
# or from the packaging/ directory.
_SPEC_DIR = Path(SPECPATH).resolve()
REPO = _SPEC_DIR.parent if _SPEC_DIR.name == "packaging" else _SPEC_DIR
ICON_ICO = REPO / "resources" / "icon.ico"
# On Windows the EXE needs a real .ico; elsewhere the binary icon is unused
# (the in-app window icon comes from the bundled icon.png at runtime).
import sys as _sys
ICON = str(ICON_ICO) if _sys.platform == "win32" and ICON_ICO.exists() else None

datas = [(str(REPO / "resources"), "resources")]
# rosu_pp_py ships a native extension (.pyd/.so) plus a .pyi; bundle them all.
datas += collect_data_files("rosu_pp_py")
binaries = collect_dynamic_libs("rosu_pp_py")

hiddenimports = ["rosu_pp_py", "requests"]
hiddenimports += collect_submodules("rosu_pp_py")

a = Analysis(
    [str(REPO / "main.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SheldinnerPractice",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # windowed app
    disable_windowed_traceback=False,
    icon=ICON,
)
