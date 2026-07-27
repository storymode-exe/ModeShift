# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the portable Windows build of ModeShift.

Produces dist/ModeShift/ containing:
    ModeShift.exe          the editor
    ModeShiftWatcher.exe   the tray daemon
    _internal/             the shared runtime and bundled assets

Both executables are windowed (console=False), so neither flashes a terminal.
Build with packaging\\build.bat, or by hand:

    pyinstaller --clean --noconfirm packaging\\modeshift.spec
"""

import os

ROOT = os.path.abspath(os.path.join(os.getcwd()))
ASSETS = [(os.path.join(ROOT, "assets"), "assets")]
ICON = os.path.join(ROOT, "assets", "modeshift.ico")

# openrgb-python and pynput both pull modules in dynamically, so name them
HIDDEN = [
    "openrgb", "openrgb.utils", "openrgb.orgb", "openrgb.network",
    "pynput", "pynput.keyboard", "pynput.keyboard._win32",
    "pynput._util", "pynput._util.win32",
    "psutil", "PIL", "PIL.Image", "PIL.ImageDraw", "pystray",
    "pystray._win32",
]

editor_a = Analysis(
    [os.path.join(ROOT, "modeshift_editor.py")],
    pathex=[ROOT],
    datas=ASSETS,
    hiddenimports=HIDDEN,
    excludes=["evdev", "tkinter", "matplotlib", "numpy.testing"],
    noarchive=False,
)

watcher_a = Analysis(
    [os.path.join(ROOT, "modeshift_watcher.py")],
    pathex=[ROOT],
    datas=ASSETS,
    hiddenimports=HIDDEN,
    # the tray daemon needs no Qt at all: leaving it out halves the folder
    excludes=["evdev", "tkinter", "PySide6", "shiboken6", "matplotlib"],
    noarchive=False,
)

# No MERGE(): the two programs share one COLLECT folder, so identical runtime
# files simply land on top of each other. MERGE saves nothing here and makes
# missing-module failures much harder to read.

editor_pyz = PYZ(editor_a.pure)
editor_exe = EXE(
    editor_pyz, editor_a.scripts, [],
    exclude_binaries=True,
    name="ModeShift",
    icon=ICON,
    console=False,
    disable_windowed_traceback=False,
)

watcher_pyz = PYZ(watcher_a.pure)
watcher_exe = EXE(
    watcher_pyz, watcher_a.scripts, [],
    exclude_binaries=True,
    name="ModeShiftWatcher",
    icon=ICON,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    editor_exe, editor_a.binaries, editor_a.datas,
    watcher_exe, watcher_a.binaries, watcher_a.datas,
    strip=False, upx=False,
    name="ModeShift",
)
