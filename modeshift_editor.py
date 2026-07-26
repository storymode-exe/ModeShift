#!/usr/bin/env python3
"""
modeshift_editor.py

OpenRGB-style GUI for editing games.json (Profiles -> Modes -> Zones).

Layout:
    +-------------------+-------------------------------+
    | Profiles (combo)  |                               |
    | Modes    (list)   |   Keyboard grid (click keys   |
    |                   |   to select; drawn from your  |
    |                   |   real OpenRGB layout)        |
    +-------------------+---------------+---------------+
    | Zones (from selection, rename,    | Color picker  |
    | brightness, delete)               | (swatches,    |
    |                                   |  RGB, hex,    |
    |                                   |  brightness)  |
    +-----------------------------------+---------------+

A Zone is a named group of keys with one color + brightness. A Mode is a set
of zones over a base color. A Profile is a set of modes plus a window-match
string. The watcher applies a profile's ACTIVE mode.

Requirements: pip install PySide6
Run:          python3 modeshift_editor.py
"""

import colorsys
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QRect, QRectF, QSize, QPointF, QUrl, QTimer
    from PySide6.QtGui import (
        QColor, QPainter, QConicalGradient, QLinearGradient, QBrush, QPen,
        QDesktopServices, QPixmap, QIcon,
    )
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout,
        QHBoxLayout, QPushButton, QComboBox, QLineEdit, QLabel,
        QMessageBox, QInputDialog, QScrollArea, QCheckBox,
        QFrame, QListWidget, QListWidgetItem, QSlider, QSpinBox, QGroupBox,
        QRubberBand, QTabWidget, QDialog, QFileDialog, QMenu,
    )
except ImportError:
    print("Missing dependency: PySide6. Install with: pip install PySide6", file=sys.stderr)
    sys.exit(1)

import modeshift_common as rc
import modeshift_effects as fx

APP_NAME = "ModeShift"
APP_VERSION = "1.4.0"
APP_AUTHOR = "StoryMode"
APP_LICENSE = "GPLv3"
KOFI_URL = "https://ko-fi.com/storymode"

WATCHER_SCRIPT = Path(__file__).parent / "modeshift_watcher.py"
IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    # the per-user Startup folder: anything here runs at login
    AUTOSTART_FILE = (Path(os.environ.get("APPDATA", Path.home())) / "Microsoft" /
                      "Windows" / "Start Menu" / "Programs" / "Startup" /
                      "ModeShift Watcher.bat")
else:
    AUTOSTART_FILE = (Path.home() / ".config" / "autostart" /
                      "modeshift-watcher.desktop")

CUSTOM_COLORS_PATH = rc.CONFIG_PATH.parent / "custom_colors.json"
SETTINGS_PATH = rc.CONFIG_PATH.parent / "settings.json"

EXPORT_SUFFIX = ".modeshift"
EXPORT_MARKER = "modeshift_export"

# Key tile size at 100%; scaled by the 'keyboard_scale' setting at startup.
BASE_TILE_W, BASE_TILE_H = 54, 46
TILE_SCALE = 1.0

DEFAULT_SETTINGS = {
    "detect_ding": True,
    "ding_volume": 35,           # percent
    "detect_countdown_lights": True,
    "detect_seconds": 10,
    "finale_flash": True,
    "keyboard_scale": 100,
    "saved_color_slots": 24,
}
CUSTOM_ROW = 8            # swatches per row
CUSTOM_MAX = 64           # hard cap on saved custom colors


def hsv_to_hex(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return f"{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}"


def hex_to_hsv(hex_color: str):
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hsv(r, g, b)


# --- short labels so the numpad / long keys stay readable on small tiles ---
_SHORT_LABELS = {
    "Escape": "Esc", "Print Screen": "PrSc", "Scroll Lock": "ScrLk",
    "Pause/Break": "Pause", "Media Mute": "Mute", "Media Play/Pause": "Play",
    "Media Previous": "Prev", "Media Next": "Next", "Backspace": "Bksp",
    "Insert": "Ins", "Page Up": "PgUp", "Page Down": "PgDn", "Delete": "Del",
    "Num Lock": "NumLk", "Caps Lock": "Caps", "Left Shift": "LShift",
    "Right Shift": "RShift", "Left Control": "LCtrl", "Right Control": "RCtrl",
    "Left Alt": "LAlt", "Right Alt": "RAlt", "Left Windows": "LWin",
    "Right Windows": "RWin", "Up Arrow": "↑", "Down Arrow": "↓",
    "Left Arrow": "←", "Right Arrow": "→", "\\ (ANSI)": "\\",
    "Number Pad /": "N /", "Number Pad *": "N *", "Number Pad -": "N -",
    "Number Pad +": "N +", "Number Pad Enter": "N ⏎", "Number Pad .": "N .",
    "Number Pad 0": "N0", "Number Pad 1": "N1", "Number Pad 2": "N2",
    "Number Pad 3": "N3", "Number Pad 4": "N4", "Number Pad 5": "N5",
    "Number Pad 6": "N6", "Number Pad 7": "N7", "Number Pad 8": "N8",
    "Number Pad 9": "N9",
}

# OpenRGB-style preset swatches
_PRESETS = ["000000", "FF0000", "FF8000", "FFFF00", "00FF00",
            "00FFFF", "0000FF", "FF00FF", "FFFFFF"]


def _short_label(key_name: str) -> str:
    return _SHORT_LABELS.get(key_name, key_name)


def _contrast_text(hex_color: str) -> str:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.55 else "#FFFFFF"


# border kind -> CSS border for KeyTile. Keeps the RGB colour as the fill and
# uses the border to indicate state/assignment.
_BORDER_STYLES = {
    None: "1px solid #333",
    "selected": "3px solid #FFD700",   # yellow: currently selected (free)
    "zone": "3px solid #2ECC40",       # green: key belongs to the selected zone
    "mode": "3px solid #2ECC40",       # green: has a Change-Mode function
    "profile": "3px solid #FF4136",    # red: has a Change-Profile function
    "keystate": "3px solid #00BFFF",   # blue: has a cooldown/toggle indicator
}


class KeyTile(QPushButton):
    def __init__(self, key_name: str):
        super().__init__(_short_label(key_name))
        self.key_name = key_name
        self.setToolTip(key_name)
        self.setFixedSize(int(BASE_TILE_W * TILE_SCALE), int(BASE_TILE_H * TILE_SCALE))
        self.setFlat(True)
        # Let mouse events pass through to the KeyboardCanvas so it can handle
        # both single-click toggle and rubber-band box selection uniformly.
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._hex = "000000"
        self._border = None
        self._restyle()

    def set_color(self, hex_color: str):
        self._hex = hex_color
        self._restyle()

    def set_border(self, kind):
        self._border = kind
        self._restyle()

    def _restyle(self):
        border = _BORDER_STYLES.get(self._border, _BORDER_STYLES[None])
        self.setStyleSheet(
            f"QPushButton {{ background-color: #{self._hex}; color: {_contrast_text(self._hex)}; "
            f"border: {border}; border-radius: 3px; "
            f"font-size: {max(9, int(10 * TILE_SCALE))}px; }}"
        )


class KeyboardCanvas(QWidget):
    """Holds the key tiles and handles selection: a plain click toggles one
    key; click-and-drag draws a rubber-band box and adds every intersecting
    key to the selection."""

    DRAG_THRESHOLD = 6  # px before a press becomes a drag

    def __init__(self, on_toggle, on_box):
        super().__init__()
        self.tiles: dict[str, KeyTile] = {}
        self._on_toggle = on_toggle   # callback(KeyTile)
        self._on_box = on_box         # callback(list[str])
        self._rubber = QRubberBand(QRubberBand.Rectangle, self)
        self._origin = None
        self._dragging = False

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self._origin = e.position().toPoint()
        self._dragging = False
        self._rubber.setGeometry(QRect(self._origin, QSize()))

    def mouseMoveEvent(self, e):
        if self._origin is None:
            return
        pos = e.position().toPoint()
        if not self._dragging and (pos - self._origin).manhattanLength() > self.DRAG_THRESHOLD:
            self._dragging = True
            self._rubber.show()
        if self._dragging:
            self._rubber.setGeometry(QRect(self._origin, pos).normalized())

    def mouseReleaseEvent(self, e):
        if self._origin is None:
            return
        if self._dragging:
            rect = self._rubber.geometry()
            self._rubber.hide()
            names = [n for n, t in self.tiles.items() if t.geometry().intersects(rect)]
            self._on_box(names)
        else:
            pt = e.position().toPoint()
            for t in self.tiles.values():
                if t.geometry().contains(pt):
                    self._on_toggle(t)
                    break
        self._origin = None
        self._dragging = False


class ColorWheel(QWidget):
    """An OpenRGB-style HSV picker: an outer hue ring around an inner
    saturation/value square. Drag in either area to pick. Calls on_changed(hex)
    on user interaction (not on programmatic setColor)."""

    def __init__(self, on_changed, size=220):
        super().__init__()
        self.setFixedSize(size, size)
        self.on_changed = on_changed
        self._h = 0.0
        self._s = 0.0
        self._v = 1.0
        self._ring_frac = 0.16  # ring thickness as fraction of radius
        self._drag_target = None  # 'ring' | 'square' | None

    # -- geometry helpers --
    def _radii(self):
        R = min(self.width(), self.height()) / 2 - 2
        return R, R * (1 - self._ring_frac)

    def _square_rect(self):
        _R, r_in = self._radii()
        side = r_in * 1.41421356 * 0.99  # inscribed square
        cx, cy = self.width() / 2, self.height() / 2
        return QRectF(cx - side / 2, cy - side / 2, side, side)

    # -- public --
    def setColor(self, hex_color: str):
        self._h, self._s, self._v = hex_to_hsv(hex_color)
        self.update()

    def color(self) -> str:
        return hsv_to_hex(self._h, self._s, self._v)

    # -- painting --
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        cx, cy = self.width() / 2, self.height() / 2
        R, r_in = self._radii()

        # hue ring
        grad = QConicalGradient(cx, cy, 0)
        for i in range(0, 361, 30):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1, 1))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(QRectF(cx - R, cy - R, 2 * R, 2 * R))
        # punch the hole
        p.setBrush(self.palette().window())
        p.drawEllipse(QRectF(cx - r_in, cy - r_in, 2 * r_in, 2 * r_in))

        # SV square for current hue
        sq = self._square_rect()
        base = QColor.fromHsvF(self._h, 1, 1)
        p.setBrush(base)
        p.drawRect(sq)
        gh = QLinearGradient(sq.left(), 0, sq.right(), 0)
        gh.setColorAt(0.0, QColor(255, 255, 255, 255))
        gh.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setBrush(QBrush(gh))
        p.drawRect(sq)
        gv = QLinearGradient(0, sq.top(), 0, sq.bottom())
        gv.setColorAt(0.0, QColor(0, 0, 0, 0))
        gv.setColorAt(1.0, QColor(0, 0, 0, 255))
        p.setBrush(QBrush(gv))
        p.drawRect(sq)

        # markers
        p.setBrush(Qt.NoBrush)
        # hue marker
        import math
        ang = self._h * 2 * math.pi
        mr = (R + r_in) / 2
        hx = cx + mr * math.cos(ang)
        hy = cy - mr * math.sin(ang)
        p.setPen(QPen(QColor("#FFFFFF"), 2))
        p.drawEllipse(QPointF(hx, hy), 5, 5)
        p.setPen(QPen(QColor("#000000"), 1))
        p.drawEllipse(QPointF(hx, hy), 6, 6)
        # sv marker
        mx = sq.left() + self._s * sq.width()
        my = sq.top() + (1 - self._v) * sq.height()
        p.setPen(QPen(QColor("#FFFFFF"), 2))
        p.drawEllipse(QPointF(mx, my), 5, 5)
        p.setPen(QPen(QColor("#000000"), 1))
        p.drawEllipse(QPointF(mx, my), 6, 6)
        p.end()

    # -- mouse --
    def mousePressEvent(self, e):
        self._drag_target = None
        self._handle(e, press=True)

    def mouseMoveEvent(self, e):
        if self._drag_target:
            self._handle(e, press=False)

    def mouseReleaseEvent(self, _e):
        self._drag_target = None

    def _handle(self, e, press):
        import math
        pos = e.position()
        cx, cy = self.width() / 2, self.height() / 2
        dx, dy = pos.x() - cx, cy - pos.y()
        dist = math.hypot(dx, dy)
        R, r_in = self._radii()
        sq = self._square_rect()

        target = self._drag_target
        if press:
            if r_in <= dist <= R + 1:
                target = "ring"
            elif sq.contains(pos):
                target = "square"
            self._drag_target = target

        if target == "ring":
            self._h = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
        elif target == "square":
            self._s = min(1.0, max(0.0, (pos.x() - sq.left()) / sq.width()))
            self._v = min(1.0, max(0.0, 1 - (pos.y() - sq.top()) / sq.height()))
        else:
            return
        self.update()
        if self.on_changed:
            self.on_changed(self.color())


class SwatchButton(QPushButton):
    """A saved-color slot. Left-click selects (or, if empty, saves the current
    color); right-click clears. Empty = None."""

    def __init__(self, index, on_left, on_right):
        super().__init__()
        self.index = index
        self._on_left = on_left
        self._on_right = on_right
        self.setFixedSize(26, 26)
        self.hex_color = None
        self.set_hex(None)

    def set_hex(self, hex_color):
        self.hex_color = hex_color
        if hex_color:
            self.setStyleSheet(f"background-color: #{hex_color}; border: 1px solid #777;")
            self.setToolTip(f"#{hex_color}  (left-click: use · right-click: clear)")
        else:
            self.setStyleSheet("background-color: #FFFFFF; border: 1px dashed #999;")
            self.setToolTip("Empty slot (left-click to save the current color)")

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._on_right(self.index)
        else:
            self._on_left(self.index)


class KeyCaptureDialog(QDialog):
    """Press a physical key; it is shown, then Accept confirms it. Reads raw
    input via evdev (same path the watcher uses), polled on the GUI thread."""

    def __init__(self, parent, valid_names):
        super().__init__(parent)
        self.setWindowTitle("Capture a key")
        self.captured = None
        self._valid = {n.lower(): n for n in valid_names}
        self.setMinimumWidth(320)
        self.setMaximumSize(520, 260)      # keep it a small popup, not a panel
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Press a key, or hold modifiers and press a key "
                           "(for example Ctrl + Shift + R)."))
        self.label = QLabel("Waiting...")
        self.label.setStyleSheet("font-weight:bold; font-size:15px; padding:8px;")
        self.label.setWordWrap(True)
        v.addWidget(self.label)
        v.addStretch(1)
        row = QHBoxLayout()
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.setEnabled(False)
        self.accept_btn.clicked.connect(self.accept)
        row.addWidget(self.accept_btn)
        again = QPushButton("Clear")
        again.clicked.connect(self._reset_capture)
        row.addWidget(again)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        v.addLayout(row)
        self._held = []          # keys currently down, in press order

        self._devs, self._sel, self._codes = [], None, {}
        self._listener = None
        self._pending = []          # (key name, pressed) captured off-thread

        if sys.platform.startswith("win"):
            self._start_windows_capture(valid_names, v)
            self.resize(360, 170)
            return

        try:
            import evdev
            from evdev import ecodes
            import selectors
            for name in valid_names:
                en = rc.key_name_to_ecode_name(name)
                if en and hasattr(ecodes, en):
                    self._codes[getattr(ecodes, en)] = name
            self._sel = selectors.DefaultSelector()
            for path in evdev.list_devices():
                try:
                    d = evdev.InputDevice(path)
                except (PermissionError, OSError):
                    continue
                keys = d.capabilities().get(ecodes.EV_KEY, [])
                if ecodes.KEY_A in keys and ecodes.KEY_Z in keys:
                    self._devs.append(d)
                    self._sel.register(d, selectors.EVENT_READ)
            if not self._devs:
                raise RuntimeError("no readable keyboards")
            self._ecodes = ecodes
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._poll)
            self._timer.start(40)
        except Exception as e:
            self.label.setText("Key capture unavailable")
            note = QLabel("<i>Raw key capture needs python-evdev and your user in "
                          f"the <tt>input</tt> group.<br>({e})</i>")
            note.setWordWrap(True)
            v.addWidget(note)
        self.resize(360, 170)

    def _start_windows_capture(self, valid_names, layout):
        """Windows: capture via a pynput hook. The hook runs on its own thread,
        so events are queued and drained by a timer on the GUI thread."""
        try:
            from pynput import keyboard
        except ImportError as e:
            self.label.setText("Key capture unavailable")
            note = QLabel("<i>Install pynput for key capture on Windows:<br>"
                          f"<tt>pip install pynput</tt><br>({e})</i>")
            note.setWordWrap(True)
            layout.addWidget(note)
            return

        valid = {n.lower(): n for n in valid_names}

        def name_for(key):
            vk = getattr(key, "vk", None)
            if vk is None:
                vk = getattr(getattr(key, "value", None), "vk", None)
            if vk is None:
                return None
            name = rc.win_vk_to_key_name(int(vk))
            return valid.get(name.lower()) if name else None

        def on_press(key):
            name = name_for(key)
            if name:
                self._pending.append((name, True))

        def on_release(key):
            name = name_for(key)
            if name:
                self._pending.append((name, False))

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.start()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._drain_pending)
        self._timer.start(40)

    def _drain_pending(self):
        """Apply queued Windows key events on the GUI thread."""
        while self._pending:
            name, pressed = self._pending.pop(0)
            if pressed:
                if name not in self._held:
                    self._held.append(name)
                combo = list(self._held)
                if self.captured is None or len(combo) > len(self.captured):
                    self.captured = combo
                    self.label.setText(" + ".join(combo))
                    self.accept_btn.setEnabled(True)
            elif name in self._held:
                self._held.remove(name)

    def _reset_capture(self):
        self.captured = None
        self._held = []
        self.label.setText("Waiting...")
        self.accept_btn.setEnabled(False)

    def _poll(self):
        if self._sel is None:
            return
        for key, _ in self._sel.select(timeout=0):
            try:
                for ev in key.fileobj.read():
                    if ev.type != self._ecodes.EV_KEY:
                        continue
                    name = self._codes.get(ev.code)
                    if not name:
                        continue
                    if ev.value == 1:                       # key down
                        if name not in self._held:
                            self._held.append(name)
                        # the longest simultaneous press wins, so holding
                        # Ctrl+Shift then R captures all three
                        combo = list(self._held)
                        if self.captured is None or len(combo) > len(self.captured):
                            self.captured = combo
                            self.label.setText(" + ".join(combo))
                            self.accept_btn.setEnabled(True)
                    elif ev.value == 0 and name in self._held:   # key up
                        self._held.remove(name)
            except OSError:
                continue

    def closeEvent(self, e):
        try:
            self._timer.stop()
        except Exception:
            pass
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        for d in self._devs:
            try:
                d.close()
            except Exception:
                pass
        super().closeEvent(e)


class MainWindow(QMainWindow):
    def __init__(self, cfg: dict, device, device_name, config_path):
        super().__init__()
        self.cfg = cfg
        self.device = device
        self.device_name = device_name
        self.config_path = config_path
        self.led_lookup = rc.build_led_lookup(device)
        self._preview = fx.EffectEngine(device, self.led_lookup)  # animates effects in live preview
        self.zone = rc.find_matrix_zone(device)
        if self.zone is None:
            raise RuntimeError(f"Device '{device.name}' has no matrix zone to draw.")

        self.settings = self._load_settings()
        # apply the keyboard zoom before any key tiles are built
        global TILE_SCALE
        TILE_SCALE = max(1.0, min(2.0, int(self.settings.get("keyboard_scale", 100)) / 100.0))
        self.current_profile_name = rc.DEFAULT_PROFILE_NAME
        self.current_mode_name = self._profile()["active_mode"]
        self.selected_keys: set[str] = set()
        self.active_zone_idx: int | None = None
        self.tiles: dict[str, KeyTile] = {}
        self.edit_mode = "zones"          # 'zones' or 'functions'
        self.func_selected_key: str | None = None  # single-key selection in Functions tab
        self._loading = False  # guard against signal recursion

        self.setWindowTitle(f"{APP_NAME} Profile Editor: {device_name}")
        self._build_ui()
        self._reload_all()

    # -------------------------------------------------- convenience refs ---

    def _all_profiles(self) -> dict:
        """Profiles for the currently-selected keyboard."""
        return self.cfg["devices"][self.device_name]["profiles"]

    def _profile(self) -> dict:
        return self._all_profiles()[self.current_profile_name]

    def _mode(self) -> dict:
        return self._profile()["modes"][self.current_mode_name]

    def _zones(self) -> list:
        return self._mode()["zones"]

    # -------------------------------------------------------------- UI ---

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # ===== top: the keyboard, full width =====
        # The profile/mode panel lives in the bottom row instead of beside the
        # board: it is taller than the keyboard, and having it up here forced
        # the whole top row to its height and left a big gap under the keys.
        root.addWidget(self._build_keyboard(), stretch=0)

        # ===== bottom: profile panel | tabs | color picker =====
        bottom = QHBoxLayout()
        left_panel = self._scrollable(self._build_left_panel())
        left_panel.setFixedWidth(258)
        bottom.addWidget(left_panel)
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(self._scrollable(self._build_zones_panel()), "Color Zones")
        self.bottom_tabs.addTab(self._scrollable(self._build_functions_panel()), "Functions")
        self.bottom_tabs.addTab(self._scrollable(self._build_keystates_panel()), "Key States")
        self.bottom_tabs.addTab(self._scrollable(self._build_settings_panel()), "Settings")
        self.bottom_tabs.addTab(self._build_howto_panel(), "How-To")
        self.bottom_tabs.addTab(self._scrollable(self._build_about_panel()), "About")
        self.bottom_tabs.currentChanged.connect(self._on_tab_changed)
        bottom.addWidget(self.bottom_tabs, stretch=1)
        color_panel = self._build_color_panel()
        # The colour panel keeps its natural height and sits at the top of the
        # row; the profile panel and the tabs take any extra vertical space, so
        # their lists grow when the window is made taller.
        color_panel.setFixedHeight(color_panel.sizeHint().height())
        bottom.addWidget(color_panel, alignment=Qt.AlignTop)
        root.addLayout(bottom, stretch=1)
        # No explicit minimum here: the non-scrolling tabs already report their
        # own minimum, and asking the tab widget for a sizeHint would include
        # the long scrollable pages and blow the window up.

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(240)
        v = QVBoxLayout(panel)

        v.addWidget(QLabel("<b>Profile</b>"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_selected)
        v.addWidget(self.profile_combo)

        pbtns = QHBoxLayout()
        for label, slot in [("New", self._on_new_profile), ("Rename", self._on_rename_profile),
                            ("Delete", self._on_delete_profile)]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            pbtns.addWidget(b)
        v.addLayout(pbtns)

        v.addWidget(QLabel("Match string (window / process):"))
        self.match_field = QLineEdit()
        self.match_field.setPlaceholderText("e.g. starcitizen  (blank = idle default)")
        self.match_field.editingFinished.connect(self._on_match_changed)
        v.addWidget(self.match_field)

        self.detect_btn = QPushButton("Detect from focused window")
        self.detect_btn.setToolTip("Click, then focus your game within 10 seconds. "
                                   "It fills the match string from the game's window.")
        self.detect_btn.clicked.connect(self._on_detect_window)
        v.addWidget(self.detect_btn)

        v.addWidget(self._hline())

        v.addWidget(QLabel("<b>Modes</b>"))
        self.mode_list = QListWidget()
        self.mode_list.setMinimumHeight(64)
        self.mode_list.setSizeAdjustPolicy(QListWidget.AdjustIgnored)
        self.mode_list.currentRowChanged.connect(self._on_mode_selected)
        self.mode_list.itemDoubleClicked.connect(lambda _i: self._on_rename_mode())
        v.addWidget(self.mode_list, stretch=1)

        mbtns = QHBoxLayout()
        for label, slot in [("Add", self._on_add_mode), ("Rename", self._on_rename_mode),
                            ("Delete", self._on_delete_mode)]:
            b = QPushButton(label)
            b.clicked.connect(slot)
            mbtns.addWidget(b)
        v.addLayout(mbtns)

        self.set_active_btn = QPushButton("● Start the profile on this mode")
        self.set_active_btn.setToolTip(
            "When this profile is applied, the watcher starts on this mode. "
            "Key functions can switch modes afterwards without changing it.")
        self.set_active_btn.clicked.connect(self._on_set_active_mode)
        v.addWidget(self.set_active_btn)

        v.addWidget(self._hline())
        self.live_check = QCheckBox("Live preview on keyboard")
        self.live_check.setChecked(True)
        self.live_check.toggled.connect(self._on_live_toggled)
        v.addWidget(self.live_check)

        apply_btn = QPushButton("Apply Now")
        apply_btn.setToolTip("Push this profile to the keyboard and tell a running "
                             "watcher to switch to it (so key functions test correctly).")
        apply_btn.clicked.connect(self._on_apply_now)
        v.addWidget(apply_btn)

        save_btn = QPushButton("Save to games.json")
        save_btn.clicked.connect(self._on_save)
        v.addWidget(save_btn)

        v.addWidget(self._hline())
        watcher_btn = QPushButton("Start / Restart Watcher")
        watcher_btn.setToolTip("Applies your saved profiles live and enables game "
                               "detection + key functions in the background.")
        watcher_btn.clicked.connect(self._on_start_restart_watcher)
        v.addWidget(watcher_btn)

        return panel

    def _build_keyboard(self) -> QWidget:
        container = KeyboardCanvas(self._on_tile_clicked, self._on_box_select)
        grid = QGridLayout(container)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(4)
        grid.setAlignment(Qt.AlignLeft | Qt.AlignTop)

        grid.setAlignment(Qt.AlignHCenter | Qt.AlignTop)   # centre the board

        num_cols = self.zone.mat_width
        gap_cols = self._find_gap_columns(self.zone.matrix_map, num_cols)

        for row_i, row in enumerate(self.zone.matrix_map):
            for col_i, led_idx in enumerate(row):
                if led_idx is None:
                    continue
                try:
                    led = self.device.leds[led_idx]
                except IndexError:
                    continue
                key_name = rc.led_shorthand(led)
                tile = KeyTile(key_name)
                grid.addWidget(tile, row_i, col_i)
                self.tiles[key_name] = tile

        for col in range(num_cols):
            grid.setColumnMinimumWidth(col, 24 if col in gap_cols else 0)

        container.tiles = self.tiles
        self.keyboard_container = container  # used to size the window to the full board
        scroll = QScrollArea()
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)

        # Pin the board to its natural height: it never stretches, and never
        # squashes into a vertical scrollbar either.
        kb_h = container.sizeHint().height()
        scroll.setFixedHeight(kb_h)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(scroll)
        legend = QLabel(
            "Left-click: select key  ·  Ctrl-click: add / remove  ·  Drag: box-select  "
            "·  Ctrl-drag: add box     "
            "<span style='color:#FFD700'>■</span> selected   "
            "<span style='color:#2ECC40'>■</span> editing zone")
        legend.setStyleSheet("color:#999; font-size:11px; padding:3px 6px;")
        wl.addWidget(legend)
        return wrap

    def sized_for_keyboard(self):
        """Return a (width, height) that shows the whole keyboard without
        clipping the numpad, clamped to the available screen."""
        try:
            kb_w = self.keyboard_container.sizeHint().width()
        except Exception:
            kb_w = 1300
        # the keyboard spans the full width, so only chrome/margins to add;
        # this also becomes the hard minimum so the board never needs to
        # scroll sideways
        want_w = kb_w + 40
        # Qt's own minimum for everything assembled: the keyboard is a fixed
        # height, so this is exactly "as small as it can go" and the window
        # opens there instead of taller.
        try:
            want_h = self.minimumSizeHint().height()
        except Exception:
            want_h = 900
        try:
            screen = QApplication.primaryScreen().availableGeometry()
            want_w = min(want_w, screen.width() - 40)
            want_h = min(want_h, screen.height() - 60)
        except Exception:
            pass
        return want_w, want_h

    def _build_zones_panel(self) -> QWidget:
        box = QGroupBox("Zones (in selected mode)")
        v = QVBoxLayout(box)

        v.addWidget(QLabel(
            "<i>Select key(s) and pick a color to set them directly. "
            "Make a zone if you want to name a group and recolor it all at once.</i>"))

        self.zone_list = QListWidget()
        # keep the window's natural height compact; the list still grows when
        # the window is made taller
        self.zone_list.setMinimumHeight(90)
        self.zone_list.setSizeAdjustPolicy(QListWidget.AdjustIgnored)
        self.zone_list.currentRowChanged.connect(self._on_zone_selected)
        self.zone_list.itemDoubleClicked.connect(lambda _i: self._on_rename_zone())
        # drag a zone up or down the layer stack, or use the arrow buttons
        self.zone_list.setDragDropMode(QListWidget.InternalMove)
        self.zone_list.setDefaultDropAction(Qt.MoveAction)
        self.zone_list.model().rowsMoved.connect(self._on_zone_rows_moved)
        # right-click for the same actions as the buttons
        self.zone_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.zone_list.customContextMenuRequested.connect(self._on_zone_menu)
        v.addWidget(self.zone_list, stretch=1)   # grows when the window does

        # reorder arrows stacked on the left, the four zone actions in a 2x2
        self.zone_up_btn = QPushButton("▲")
        self.zone_up_btn.setToolTip("Move this zone up (higher layer, wins on overlap). "
                                    "You can also drag zones in the list.")
        self.zone_up_btn.setFixedWidth(34)
        self.zone_up_btn.clicked.connect(lambda: self._on_zone_move(-1))
        self.zone_down_btn = QPushButton("▼")
        self.zone_down_btn.setToolTip("Move this zone down (lower layer).")
        self.zone_down_btn.setFixedWidth(34)
        self.zone_down_btn.clicked.connect(lambda: self._on_zone_move(1))

        new_zone_btn = QPushButton("New zone from selection")
        new_zone_btn.clicked.connect(self._on_new_zone)
        clear_btn = QPushButton("Clear selection")
        clear_btn.clicked.connect(self._on_clear_selection)
        rename_btn = QPushButton("Rename")
        rename_btn.clicked.connect(self._on_rename_zone)
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._on_delete_zone)

        btns = QGridLayout()
        btns.addWidget(self.zone_up_btn,   0, 0)
        btns.addWidget(new_zone_btn,       0, 1)
        btns.addWidget(clear_btn,          0, 2)
        btns.addWidget(self.zone_down_btn, 1, 0)
        btns.addWidget(rename_btn,         1, 1)
        btns.addWidget(del_btn,            1, 2)
        btns.setColumnStretch(1, 1)
        btns.setColumnStretch(2, 1)
        v.addLayout(btns)

        # --- per-zone effect: pick a type, then its options appear ---
        eff = QGroupBox("Effect (selected zone)")
        ev = QVBoxLayout(eff)

        trow = QHBoxLayout()
        trow.addWidget(QLabel("Effect:"))
        self.effect_combo = QComboBox()
        for label, val in [("None", None), ("Type lighting (reactive)", "reactive"),
                           ("Breathing", "breathing"), ("Blinking", "blinking"),
                           ("Color cycle", "colorcycle"), ("Twinkle", "twinkle")]:
            self.effect_combo.addItem(label, val)
        self.effect_combo.currentIndexChanged.connect(self._on_effect_type_changed)
        trow.addWidget(self.effect_combo, 1)
        ev.addLayout(trow)

        self._eff_panels = {}
        hint = ("<i>Click a swatch to change it, right-click to remove. "
                "Add up to 8 colors; multiples cycle automatically.</i>")

        # reactive: colors cycled per keypress, fade, peak
        p, pv = self._eff_panel()
        self.rx_colors_row = self._mk_colors_control(pv)
        r = QHBoxLayout()
        r.addWidget(QLabel("Fade:")); self.rx_fade = self._mk_spin(50, 5000, " ms", 50); r.addWidget(self.rx_fade)
        r.addWidget(QLabel("Peak:")); self.rx_peak = self._mk_spin(0, 100, " %", 5); r.addWidget(self.rx_peak)
        pv.addLayout(r)
        self._eff_panels["reactive"] = p; ev.addWidget(p)

        # breathing: colors (cycle per breath), speed, min/max brightness
        p, pv = self._eff_panel()
        self.br_colors_row = self._mk_colors_control(pv)
        r = QHBoxLayout(); r.addWidget(QLabel("Speed:")); self.br_period = self._mk_spin(300, 10000, " ms", 100); r.addWidget(self.br_period); pv.addLayout(r)
        r = QHBoxLayout(); r.addWidget(QLabel("Min:")); self.br_min = self._mk_spin(0, 100, " %", 5); r.addWidget(self.br_min)
        r.addWidget(QLabel("Max:")); self.br_max = self._mk_spin(0, 100, " %", 5); r.addWidget(self.br_max); pv.addLayout(r)
        self._eff_panels["breathing"] = p; ev.addWidget(p)

        # blinking: colors (cycle per blink), on/off timing
        p, pv = self._eff_panel()
        self.bl_colors_row = self._mk_colors_control(pv)
        r = QHBoxLayout(); r.addWidget(QLabel("On:")); self.bl_on = self._mk_spin(50, 5000, " ms", 50); r.addWidget(self.bl_on)
        r.addWidget(QLabel("Off:")); self.bl_off = self._mk_spin(50, 5000, " ms", 50); r.addWidget(self.bl_off); pv.addLayout(r)
        self._eff_panels["blinking"] = p; ev.addWidget(p)

        # colorcycle: rainbow or custom stops, speed
        p, pv = self._eff_panel()
        self.cc_rainbow = QCheckBox("Rainbow (full spectrum)")
        self.cc_rainbow.stateChanged.connect(self._on_eff_param); pv.addWidget(self.cc_rainbow)
        r = QHBoxLayout(); r.addWidget(QLabel("Speed:")); self.cc_period = self._mk_spin(500, 20000, " ms", 100); r.addWidget(self.cc_period); pv.addLayout(r)
        self.cc_stops_row = self._mk_colors_control(pv)
        pv.addWidget(QLabel("<i>Colors used only when Rainbow is off.</i>"))
        self._eff_panels["colorcycle"] = p; ev.addWidget(p)

        # twinkle: colors (or random), density, fade
        p, pv = self._eff_panel()
        self.tw_colors_row = self._mk_colors_control(pv)
        self.tw_rainbow = QCheckBox("Random rainbow colors")
        self.tw_rainbow.stateChanged.connect(self._on_eff_param); pv.addWidget(self.tw_rainbow)
        r = QHBoxLayout(); r.addWidget(QLabel("Density:")); self.tw_density = self._mk_spin(0, 100, " %", 5); r.addWidget(self.tw_density)
        r.addWidget(QLabel("Fade:")); self.tw_fade = self._mk_spin(100, 5000, " ms", 50); r.addWidget(self.tw_fade); pv.addLayout(r)
        self._eff_panels["twinkle"] = p; ev.addWidget(p)

        ev.addWidget(QLabel(
            "<i>Add up to 8 colors; click a swatch to change it, right-click to "
            "remove. Multiple colors cycle automatically.</i>"))
        # Only one effect panel is visible at a time, so reserve room for the
        # tallest of them: switching effects then never resizes the tab or
        # pushes controls out of view.
        tallest = max((p.minimumSizeHint().height() for p in self._eff_panels.values()),
                      default=0)
        if tallest:
            for p in self._eff_panels.values():
                p.setMinimumHeight(tallest)
        v.addWidget(eff)

        self.selection_label = QLabel("0 keys selected")
        v.addWidget(self.selection_label)
        return box

    def _build_functions_panel(self) -> QWidget:
        box = QGroupBox("Key functions (this profile)")
        v = QVBoxLayout(box)

        v.addWidget(QLabel(
            "Click one key above to bind it. "
            "<span style='color:#2ECC40'>■</span> mode key  "
            "<span style='color:#FF4136'>■</span> profile key  "
            "<span style='color:#FFD700'>■</span> selected"
        ))

        self.func_key_label = QLabel("No key selected")
        self.func_key_label.setStyleSheet("font-weight: bold;")
        v.addWidget(self.func_key_label)

        # On Press row
        press_row = QHBoxLayout()
        press_row.addWidget(QLabel("On Press:"))
        self.press_action = QComboBox()
        self.press_action.addItem("Do nothing", None)
        self.press_action.addItem("Change Mode", "mode")
        self.press_action.addItem("Change Profile", "profile")
        self.press_action.currentIndexChanged.connect(lambda _i: self._on_func_changed("on_press"))
        press_row.addWidget(self.press_action)
        self.press_target = QComboBox()
        self.press_target.currentIndexChanged.connect(lambda _i: self._on_func_changed("on_press"))
        press_row.addWidget(self.press_target, stretch=1)
        v.addLayout(press_row)

        # On Release row
        release_row = QHBoxLayout()
        release_row.addWidget(QLabel("On Release:"))
        self.release_action = QComboBox()
        self.release_action.addItem("Do nothing", None)
        self.release_action.addItem("Change Mode", "mode")
        self.release_action.addItem("Change Profile", "profile")
        self.release_action.currentIndexChanged.connect(lambda _i: self._on_func_changed("on_release"))
        release_row.addWidget(self.release_action)
        self.release_target = QComboBox()
        self.release_target.currentIndexChanged.connect(lambda _i: self._on_func_changed("on_release"))
        release_row.addWidget(self.release_target, stretch=1)
        v.addLayout(release_row)

        clear_btn = QPushButton("Clear this key's functions")
        clear_btn.clicked.connect(self._on_clear_function)
        v.addWidget(clear_btn)

        v.addWidget(QLabel(
            "<i>Tip: for a momentary hold (Star Citizen), set On Press → Change "
            "Mode → Mode 2, and On Release → Change Mode → Mode 1. Rename the modes "
            "to taste (Flight, On Foot, and so on).</i>"
        ))
        v.addStretch(1)
        self._set_func_editor_enabled(False)
        return box

    def _build_about_panel(self) -> QWidget:
        box = QGroupBox("About")
        v = QVBoxLayout(box)

        v.addWidget(QLabel(f"<h2>{APP_NAME}</h2>"))
        v.addWidget(QLabel(
            f"Version {APP_VERSION} &nbsp;·&nbsp; {APP_LICENSE}<br>"
            f"Created by <b>{APP_AUTHOR}</b><br><br>"
            "Per-key RGB profiles, modes, zones, and key-triggered mode/profile "
            "switching for any OpenRGB keyboard on Linux."
        ))

        kofi_btn = QPushButton("☕  Support on Ko-fi")
        kofi_btn.setStyleSheet(
            "QPushButton { background-color: #29ABE0; color: white; font-weight: bold; "
            "padding: 8px; border-radius: 4px; } QPushButton:hover { background-color: #1E90C0; }"
        )
        kofi_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(KOFI_URL)))
        v.addWidget(kofi_btn)

        v.addWidget(QLabel(
            f"<a style='color:#8ab4f8' href='{KOFI_URL}'>{KOFI_URL}</a>"
        ))
        note = QLabel("If this tool saved you from wrestling with configs, a coffee is appreciated but never required. 💜")
        note.setWordWrap(True)
        v.addWidget(note)

        v.addWidget(self._hline())
        credit = QLabel(
            "Huge thanks to <b>naaraxi</b> for the Keychron OpenRGB plugin and the "
            "firmware work behind it. Without him my keyboard wouldn't communicate "
            "with OpenRGB at all, and this app would never have crossed my mind to build.<br>"
            "<a style='color:#8ab4f8' href='https://github.com/naaraxi/keychron_ultra_openrgb'>"
            "github.com/naaraxi/keychron_ultra_openrgb</a>"
        )
        credit.setWordWrap(True)
        credit.setOpenExternalLinks(True)
        v.addWidget(credit)

        v.addStretch(1)
        return box

    # ------------------------------------------------------ settings ---

    def _load_settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        try:
            if SETTINGS_PATH.exists():
                data = json.loads(SETTINGS_PATH.read_text())
                for k in DEFAULT_SETTINGS:
                    if k in data:
                        s[k] = data[k]
        except Exception:
            pass
        return s

    def _save_settings(self):
        try:
            SETTINGS_PATH.write_text(json.dumps(self.settings, indent=2))
        except Exception as e:
            self._status(f"Couldn't save settings: {e}")

    @staticmethod
    def _wrap_labels(inner: QWidget) -> QWidget:
        """Let a panel's text reflow so it never needs horizontal scrolling."""
        for lbl in inner.findChildren(QLabel):
            lbl.setWordWrap(True)
        return inner

    @staticmethod
    def _scrollable(inner: QWidget) -> QWidget:
        """Wrap a panel in a scroll area so its content can't force the whole
        window taller; it scrolls vertically instead. Never scrolls sideways:
        labels wrap and the panel reflows to whatever width it is given."""
        for lbl in inner.findChildren(QLabel):
            lbl.setWordWrap(True)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        return scroll

    def _build_keystates_panel(self) -> QWidget:
        box = QGroupBox("Key states (this mode)")
        v = QVBoxLayout(box)

        warn = QLabel(
            "<b>Heads up:</b> these indicators follow <i>your keypresses</i>, not the "
            "game. ModeShift cannot see whether an ability actually fired, whether a "
            "shield was knocked offline, or whether you toggled something with the "
            "mouse, so a light can drift out of sync with the game. Press the key "
            "again to re-sync a toggle (the next press re-syncs a cooldown), or use "
            "<i>Reset key states</i> in the tray menu to snap everything back."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("background:#3a2f1a; border:1px solid #7a5c1e; "
                           "border-radius:4px; padding:6px; color:#e8d9b0;")
        v.addWidget(warn)

        v.addWidget(QLabel("Click one key above to set it up. "
                           "<span style='color:#00BFFF'>■</span> has a key state"))
        self.ks_key_label = QLabel("No key selected")
        self.ks_key_label.setStyleSheet("font-weight: bold;")
        v.addWidget(self.ks_key_label)

        trow = QHBoxLayout()
        trow.addWidget(QLabel("Type:"))
        self.ks_combo = QComboBox()
        for label, val in [("None", None), ("Cooldown (timer)", "cooldown"),
                           ("Toggle (on / off)", "toggle")]:
            self.ks_combo.addItem(label, val)
        self.ks_combo.currentIndexChanged.connect(self._on_ks_type_changed)
        trow.addWidget(self.ks_combo, 1)
        v.addLayout(trow)

        self._ks_panels = {}

        # cooldown: duration, active/stage colors, ready signal
        p, pv = self._eff_panel()
        r = QHBoxLayout(); r.addWidget(QLabel("Duration:"))
        self.ks_duration = QSpinBox(); self.ks_duration.setRange(1, 3600)
        self.ks_duration.setSuffix(" s"); self.ks_duration.valueChanged.connect(self._on_ks_param)
        r.addWidget(self.ks_duration); r.addStretch(1); pv.addLayout(r)

        r = QHBoxLayout()
        b = QPushButton("Set 'ready' color"); b.clicked.connect(lambda: self._ks_set_color("ready_color"))
        r.addWidget(b); self.ks_ready_sw = QLabel(); self.ks_ready_sw.setFixedSize(22, 22)
        r.addWidget(self.ks_ready_sw); r.addStretch(1); pv.addLayout(r)
        pv.addWidget(QLabel("<i>The key rests at this color whenever the ability is "
                            "available, on top of any zone color.</i>"))

        r = QHBoxLayout()
        b = QPushButton("Set 'on cooldown' color"); b.clicked.connect(lambda: self._ks_set_color("active_color"))
        r.addWidget(b); self.ks_active_sw = QLabel(); self.ks_active_sw.setFixedSize(22, 22)
        r.addWidget(self.ks_active_sw); r.addStretch(1); pv.addLayout(r)

        self.ks_fade = QCheckBox("Fade toward the ready color as it counts down")
        self.ks_fade.setToolTip("The key blends from the cooldown color to the ready "
                                "color, so at a glance you can tell roughly how much "
                                "time is left.")
        self.ks_fade.stateChanged.connect(self._on_ks_param)
        pv.addWidget(self.ks_fade)

        r = QHBoxLayout(); r.addWidget(QLabel("When ready:"))
        self.ks_signal = QComboBox()
        for label, val in [("Just the ready color", "solid"), ("Blink", "blink"),
                           ("Breathe", "breathe")]:
            self.ks_signal.addItem(label, val)
        self.ks_signal.currentIndexChanged.connect(self._on_ks_param)
        r.addWidget(self.ks_signal, 1); pv.addLayout(r)

        r = QHBoxLayout()
        self.ks_keep_ready = QCheckBox("Keep it up until I press the key again")
        self.ks_keep_ready.stateChanged.connect(self._on_ks_keep_ready)
        r.addWidget(self.ks_keep_ready); pv.addLayout(r)
        r = QHBoxLayout(); r.addWidget(QLabel("or stop after"))
        self.ks_ready_secs = QSpinBox(); self.ks_ready_secs.setRange(1, 60)
        self.ks_ready_secs.setSuffix(" s")
        self.ks_ready_secs.valueChanged.connect(self._on_ks_param)
        r.addWidget(self.ks_ready_secs); r.addStretch(1); pv.addLayout(r)

        self.ks_stages_row = QHBoxLayout(); pv.addLayout(self.ks_stages_row)
        self._ks_panels["cooldown"] = p; v.addWidget(p)

        # toggle: 2+ state colors
        p, pv = self._eff_panel()
        r = QHBoxLayout()
        b = QPushButton("Add state color"); b.clicked.connect(self._on_ks_add_toggle_color)
        r.addWidget(b); r.addStretch(1); pv.addLayout(r)
        self.ks_toggle_row = QHBoxLayout(); pv.addLayout(self.ks_toggle_row)
        pv.addWidget(QLabel("<i>Each press advances to the next color and stays "
                            "there. Two colors give a simple on/off. Click a swatch "
                            "to change it, right-click to remove.</i>"))
        self._ks_panels["toggle"] = p; v.addWidget(p)

        # reserve room for the taller of the cooldown / toggle panels
        tallest = max((p.minimumSizeHint().height() for p in self._ks_panels.values()),
                      default=0)
        if tallest:
            for p in self._ks_panels.values():
                p.setMinimumHeight(tallest)

        clear_btn = QPushButton("Remove key state from this key")
        clear_btn.clicked.connect(self._on_ks_clear)
        v.addWidget(clear_btn)

        v.addWidget(self._hline())
        v.addWidget(QLabel("<b>Re-sync shortcut</b>"))
        rr = QHBoxLayout()
        self.ks_reset_label = QLabel("(none set)")
        rr.addWidget(self.ks_reset_label, 1)
        b = QPushButton("Set key"); b.clicked.connect(self._on_ks_set_reset_key)
        rr.addWidget(b)
        b2 = QPushButton("Clear"); b2.clicked.connect(self._on_ks_clear_reset_key)
        rr.addWidget(b2)
        v.addLayout(rr)
        v.addWidget(QLabel("<i>Pressing this key resets every cooldown and toggle "
                           "back to its default, handy when the lights drift out of "
                           "sync with the game. Applies to all profiles.</i>"))
        self._sync_reset_key_label()
        v.addStretch(1)

        self._sync_ks_controls()
        return box

    def _build_settings_panel(self) -> QWidget:
        box = QGroupBox("Settings")
        v = QVBoxLayout(box)

        # --- Detect-from-window feedback ---
        v.addWidget(QLabel("<b>Detect-from-window cues</b>"))

        self.set_countdown_chk = QCheckBox("Show the number-key countdown on the keyboard")
        self.set_countdown_chk.setChecked(self.settings["detect_countdown_lights"])
        self.set_countdown_chk.toggled.connect(
            lambda on: self._set_setting("detect_countdown_lights", on))
        v.addWidget(self.set_countdown_chk)

        self.set_flash_chk = QCheckBox("Flash the whole keyboard when capture finishes")
        self.set_flash_chk.setChecked(self.settings["finale_flash"])
        self.set_flash_chk.toggled.connect(lambda on: self._set_setting("finale_flash", on))
        v.addWidget(self.set_flash_chk)

        self.set_ding_chk = QCheckBox("Play a sound when capture finishes")
        self.set_ding_chk.setChecked(self.settings["detect_ding"])
        self.set_ding_chk.toggled.connect(lambda on: self._set_setting("detect_ding", on))
        v.addWidget(self.set_ding_chk)

        vol_row = QHBoxLayout()
        vol_row.addWidget(QLabel("Ding volume"))
        self.set_vol_slider = QSlider(Qt.Horizontal)
        self.set_vol_slider.setRange(0, 100)
        self.set_vol_slider.setValue(int(self.settings["ding_volume"]))
        self.set_vol_slider.valueChanged.connect(self._on_vol_changed)
        vol_row.addWidget(self.set_vol_slider)
        self.set_vol_label = QLabel(f"{int(self.settings['ding_volume'])}%")
        vol_row.addWidget(self.set_vol_label)
        test_btn = QPushButton("Test")
        test_btn.clicked.connect(self._play_done_sound)
        vol_row.addWidget(test_btn)
        v.addLayout(vol_row)

        dur_row = QHBoxLayout()
        dur_row.addWidget(QLabel("Countdown length (seconds)"))
        self.set_dur_spin = QSpinBox()
        self.set_dur_spin.setRange(3, 30)
        self.set_dur_spin.setValue(int(self.settings["detect_seconds"]))
        self.set_dur_spin.valueChanged.connect(lambda val: self._set_setting("detect_seconds", val))
        dur_row.addWidget(self.set_dur_spin)
        dur_row.addStretch(1)
        v.addLayout(dur_row)

        v.addWidget(self._hline())

        # --- Watcher ---
        v.addWidget(QLabel("<b>Watcher</b>"))

        self.set_autostart_chk = QCheckBox("Start the watcher when I log in")
        self.set_autostart_chk.setToolTip(
            "Writes a desktop autostart entry so the tray watcher launches "
            "automatically. The editor does not start on login, only the watcher.")
        self.set_autostart_chk.setChecked(AUTOSTART_FILE.exists())
        self.set_autostart_chk.toggled.connect(self._on_autostart_toggled)
        v.addWidget(self.set_autostart_chk)
        self.autostart_note = QLabel("")
        v.addWidget(self.autostart_note)
        self._sync_autostart_note()
        poll_row = QHBoxLayout()
        poll_row.addWidget(QLabel("Poll interval (seconds)"))
        self.set_poll_spin = QSpinBox()
        self.set_poll_spin.setRange(1, 10)
        self.set_poll_spin.setValue(int(round(self.cfg.get("poll_interval_seconds", 1.5))))
        self.set_poll_spin.valueChanged.connect(self._on_poll_changed)
        poll_row.addWidget(self.set_poll_spin)
        poll_row.addStretch(1)
        v.addLayout(poll_row)
        v.addWidget(QLabel(
            "<i>How often the watcher checks the focused window. Lower = snappier "
            "profile switching, slightly more CPU. Save + Restart Watcher to apply.</i>"))

        v.addWidget(self._hline())

        # --- Appearance ---
        v.addWidget(QLabel("<b>Appearance</b>"))
        sc_row = QHBoxLayout()
        sc_row.addWidget(QLabel("Keyboard size"))
        self.set_scale_combo = QComboBox()
        for label, val in [("100%", 100), ("125%", 125), ("150%", 150), ("200%", 200)]:
            self.set_scale_combo.addItem(label, val)
        cur = int(self.settings.get("keyboard_scale", 100))
        for i in range(self.set_scale_combo.count()):
            if self.set_scale_combo.itemData(i) == cur:
                self.set_scale_combo.setCurrentIndex(i)
                break
        self.set_scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        sc_row.addWidget(self.set_scale_combo)
        sc_row.addStretch(1)
        v.addLayout(sc_row)
        v.addWidget(QLabel(
            "<i>Draws the keyboard bigger. The window grows to fit, up to your screen "
            "size. Reopen the editor to apply.</i>"))

        slot_row = QHBoxLayout()
        slot_row.addWidget(QLabel("Saved color slots"))
        self.set_slots_combo = QComboBox()
        for n in range(CUSTOM_ROW, CUSTOM_MAX + 1, CUSTOM_ROW):
            self.set_slots_combo.addItem(f"{n}", n)
        cur_slots = self._slot_count()
        for i in range(self.set_slots_combo.count()):
            if self.set_slots_combo.itemData(i) == cur_slots:
                self.set_slots_combo.setCurrentIndex(i)
                break
        self.set_slots_combo.currentIndexChanged.connect(self._on_slots_changed)
        slot_row.addWidget(self.set_slots_combo)
        slot_row.addStretch(1)
        v.addLayout(slot_row)
        v.addWidget(QLabel(
            "<i>How many saved color swatches to show, in rows of 8. Colors in "
            "hidden slots are kept, they just are not displayed. Reopen the editor "
            "to apply.</i>"))

        v.addWidget(self._hline())

        # --- Import / export profiles ---
        v.addWidget(QLabel("<b>Import / export profiles</b>"))
        ex_row = QHBoxLayout()
        self.export_combo = QComboBox()
        ex_row.addWidget(self.export_combo, 1)
        ex_btn = QPushButton("Export")
        ex_btn.clicked.connect(self._on_export_profiles)
        ex_row.addWidget(ex_btn)
        im_btn = QPushButton("Import")
        im_btn.clicked.connect(self._on_import_profiles)
        ex_row.addWidget(im_btn)
        v.addLayout(ex_row)
        v.addWidget(QLabel(
            f"<i>Exports to a <tt>{EXPORT_SUFFIX}</tt> file (plain JSON) you can back "
            "up or share. Importing keeps your existing profiles and asks what to do "
            "if a name already exists. Keys your board does not have are dropped.</i>"))

        v.addStretch(1)
        return box

    def _sync_autostart_note(self):
        if AUTOSTART_FILE.exists():
            self.autostart_note.setText(
                f"<i>On. Launching from:<br><tt>{WATCHER_SCRIPT}</tt></i>")
        else:
            self.autostart_note.setText(
                "<i>Off. The watcher only runs when you start it yourself.</i>")

    def _on_autostart_toggled(self, on):
        """Create or remove the desktop autostart entry for the watcher."""
        try:
            if on:
                AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
                if IS_WINDOWS:
                    # pythonw + start = no console window on login
                    AUTOSTART_FILE.write_text(
                        "@echo off\r\n"
                        f'start "" pythonw "{WATCHER_SCRIPT}" --wait\r\n')
                    self._status("The watcher will start automatically on login.")
                    self._sync_autostart_note()
                    return
                AUTOSTART_FILE.write_text(
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    f"Name={APP_NAME} Watcher\n"
                    "GenericName=Keyboard Lighting Daemon\n"
                    "Comment=Switches keyboard lighting per focused game\n"
                    # --wait retries the OpenRGB connection: on login the SDK
                    # server is often a moment behind us
                    f"Exec=python3 {WATCHER_SCRIPT} --wait\n"
                    "Icon=input-keyboard\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    "X-GNOME-Autostart-enabled=true\n"
                    "X-KDE-autostart-after=panel\n"
                    "StartupNotify=false\n"
                )
                self._status("The watcher will start automatically on login.")
            else:
                AUTOSTART_FILE.unlink(missing_ok=True)
                self._status("Removed the watcher from login startup.")
        except OSError as e:
            QMessageBox.warning(self, "Autostart", f"Could not update autostart:\n{e}")
        self._sync_autostart_note()

    def _on_slots_changed(self, _idx):
        val = self.set_slots_combo.currentData()
        if val:
            self._set_setting("saved_color_slots", int(val))
            self._status(f"Saved color slots set to {val}. "
                         f"Reopen the editor to apply.")

    def _on_scale_changed(self, _idx):
        val = self.set_scale_combo.currentData()
        if val:
            self._set_setting("keyboard_scale", int(val))
            self._status(f"Keyboard size set to {val}%. Reopen the editor to apply.")

    def _reload_export_combo(self):
        if not hasattr(self, "export_combo"):
            return
        self.export_combo.blockSignals(True)
        self.export_combo.clear()
        self.export_combo.addItem("All profiles", None)
        for name in self._all_profiles():
            self.export_combo.addItem(name, name)
        self.export_combo.blockSignals(False)

    def _on_export_profiles(self):
        which = self.export_combo.currentData()
        profiles = self._all_profiles()
        payload = dict(profiles) if which is None else {which: profiles.get(which)}
        if not payload or any(p is None for p in payload.values()):
            QMessageBox.warning(self, "Nothing to export", "That profile no longer exists.")
            return
        default = ("modeshift-profiles" if which is None
                   else which.lower().replace(" ", "-")) + EXPORT_SUFFIX
        path, _ = QFileDialog.getSaveFileName(
            self, "Export profiles", default,
            f"ModeShift profiles (*{EXPORT_SUFFIX});;JSON (*.json);;All files (*)")
        if not path:
            return
        if not path.endswith((EXPORT_SUFFIX, ".json")):
            path += EXPORT_SUFFIX
        doc = {
            EXPORT_MARKER: 1,
            "app_version": APP_VERSION,
            "device": self.device_name,
            "profiles": copy.deepcopy(payload),
        }
        try:
            Path(path).write_text(json.dumps(doc, indent=2) + "\n")
            self._status(f"Exported {len(payload)} profile(s) to {Path(path).name}.")
        except OSError as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def _on_import_profiles(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import profiles", "",
            f"ModeShift profiles (*{EXPORT_SUFFIX});;JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            doc = json.loads(Path(path).read_text())
        except (OSError, ValueError) as e:
            QMessageBox.critical(self, "Import failed", f"Could not read that file.\n{e}")
            return
        if not isinstance(doc, dict) or EXPORT_MARKER not in doc:
            QMessageBox.critical(self, "Import failed",
                                 "That does not look like a ModeShift export.")
            return
        incoming = doc.get("profiles")
        if not isinstance(incoming, dict) or not incoming:
            QMessageBox.warning(self, "Nothing to import", "That file has no profiles.")
            return

        # run through the normalizer (migrates older exports) and drop keys this
        # keyboard does not have, reusing the same carry-over used for new boards
        clean = rc._normalize_profiles(copy.deepcopy(incoming))
        clean = rc.carry_over_profiles(clean, rc.device_key_names(self.device))

        profiles = self._all_profiles()
        added = replaced = skipped = 0
        for name, prof in clean.items():
            if name == rc.DEFAULT_PROFILE_NAME and name in profiles:
                target = self._unique_profile_name(f"{name} (imported)")
            elif name in profiles:
                box = QMessageBox(self)
                box.setWindowTitle("Profile exists")
                box.setText(f"'{name}' already exists. What would you like to do?")
                rep = box.addButton("Replace", QMessageBox.AcceptRole)
                keep = box.addButton("Keep both", QMessageBox.ActionRole)
                box.addButton("Skip", QMessageBox.RejectRole)
                box.exec()
                if box.clickedButton() is rep:
                    target = name
                    replaced += 1
                elif box.clickedButton() is keep:
                    target = self._unique_profile_name(name)
                else:
                    skipped += 1
                    continue
            else:
                target = name
            if target != name or target not in profiles:
                added += 1 if target not in profiles else 0
            profiles[target] = prof
        self._reload_all()
        self._reload_export_combo()
        self._status(f"Imported {added + replaced} profile(s) "
                     f"({replaced} replaced, {skipped} skipped). Save to keep them.")

    def _unique_profile_name(self, base):
        profiles = self._all_profiles()
        if base not in profiles:
            return base
        n = 2
        while f"{base} {n}" in profiles:
            n += 1
        return f"{base} {n}"

    def _set_setting(self, key, value):
        self.settings[key] = value
        self._save_settings()

    def _on_vol_changed(self, val):
        self.set_vol_label.setText(f"{val}%")
        self._set_setting("ding_volume", val)

    def _on_poll_changed(self, val):
        self.cfg["poll_interval_seconds"] = float(val)
        self._status(f"Poll interval set to {val}s (Save + Restart Watcher to apply).")

    def _build_howto_panel(self) -> QWidget:
        box = QGroupBox("How-To")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        v = QVBoxLayout(inner)
        text = QLabel(
            "<h3>Quick start</h3>"
            "<b>1. Color your keys.</b> Click a key (or drag a box over several), pick a "
            "color on the wheel, then hit <i>Apply color to zone</i>. Those keys become "
            "a zone, whether that is one key or thirty. Use <i>Set as mode BASE color</i> "
            "for the whole board.<br><br>"
            "<b>2. Zones.</b> Everything you color is a zone: a named group you can "
            "recolor, dim, reorder, or give an effect. Select a zone to edit it, and "
            "Ctrl-click keys to add or remove them. Rename it so the list stays "
            "readable. Zones are layers, so drag them (or use the arrows) to control "
            "which one wins where they overlap.<br><br>"
            "<b>2b. Zone effects.</b> With a zone selected, pick an <i>Effect</i>: type "
            "lighting (lights up as you type), breathing, blinking, color cycle, or "
            "twinkle. Add up to 8 colors and they cycle automatically. A zone can be "
            "<i>No color (transparent)</i> so only its effect shows, which is how you "
            "get whole-keyboard type lighting over your normal colors.<br><br>"
            "<b>3. Profiles &amp; games.</b> Make a profile per game with <i>New</i>, then set "
            "its <b>Match string</b>, or just click <i>Detect from focused window</i>, "
            "alt-tab into the game, and it fills it for you.<br><br>"
            "<b>4. Modes.</b> A profile can hold several modes (e.g. Star Citizen: Flight, "
            "On Foot). The one marked ● is where the profile <b>starts</b>; key "
            "functions switch between them while you play, without changing that.<br><br>"
            "<b>5. Functions.</b> On the Functions tab, click one key and bind it to change "
            "MODE (green) or PROFILE (red) on press and/or release, for momentary, "
            "hold-to-light effects like Star Citizen's flight vs on-foot lighting.<br><br>"
            "<b>6. Go live.</b> Hit <i>Apply Now</i> to push a profile and sync the running "
            "watcher, or <i>Start / Restart Watcher</i> to (re)launch it. The watcher then "
            "switches profiles automatically based on your focused game.<br><br>"

            "<b>7. Key States.</b> On the Key States tab, click a key and give it a "
            "<i>cooldown</i> or a <i>toggle</i>. A <b>cooldown</b> rests at its ready "
            "color, turns the 'on cooldown' color when you press it, then counts down "
            "and signals ready again (solid, blink, or breathe). Tick <i>Fade toward "
            "the ready color</i> and the key itself becomes a rough progress bar. A "
            "<b>toggle</b> flips to the next color on each press and stays there, good "
            "for shields or engines on/off. You can also set a <i>Re-sync shortcut</i> "
            "(a key or a combo like Ctrl + Shift + R) that resets every indicator.<br>"
            "Remember these follow your keypresses, not the game, so they can drift out "
            "of sync.<br><br>"

            "<h3>Modes vs Profiles (important for functions)</h3>"
            "These behave differently when you hold a key:<br><br>"
            "<b>Mode functions stay in the same profile</b>, so a momentary hold just works: "
            "set On&nbsp;Press → Mode&nbsp;2 and On&nbsp;Release → Mode&nbsp;1 on one key, "
            "and holding it lights Mode&nbsp;2, releasing returns to Mode&nbsp;1. Both the "
            "press and the release are read from the same profile, so both fire.<br><br>"
            "<b>Profile functions switch which profile is active</b>, and that changes which "
            "key bindings are in effect. The moment a press switches you to Profile&nbsp;B, "
            "the release is read from <i>Profile B's</i> bindings, not the profile you pressed "
            "in. So a single key in one profile <b>cannot</b> do 'hold to switch profile, "
            "release to switch back.'<br>"
            "To get a momentary profile hold, split it across both profiles: put "
            "On&nbsp;Press → Profile&nbsp;B in Profile&nbsp;A, and On&nbsp;Release → "
            "Profile&nbsp;A in Profile&nbsp;B. Or keep it simple with press-only toggles "
            "(press in A → B, press again in B → A).<br><br>"

            "<h3>What covers what</h3>"
            "Everything is drawn in layers, from the bottom up:<br>"
            "1. the mode's <b>base color</b><br>"
            "2. <b>zones</b> (the top zone in the list wins where they overlap, drag "
            "them or use the arrows to reorder)<br>"
            "3. <b>zone effects</b><br>"
            "4. <b>Key State colors</b> (cooldown and toggle)<br><br>"
            "So a <b>Key State color always wins</b>: if you give a key a cooldown or "
            "toggle, its indicator color covers whatever that key was set to in Color "
            "Zones. That is deliberate, an ability's status should always be readable. "
            "A cooldown key sits at its <i>ready</i> color whenever it is available, so "
            "pick a ready color you are happy seeing all the time (or use a toggle if "
            "you would rather it match the zone).<br><br>"

            "<h3>Tips</h3>"
            "• The color wheel just holds a color, nothing changes until you hit "
            "<i>Apply color</i>, a swatch, or one of the 'set color' buttons.<br>"
            "• Effects only animate in the editor while <i>Live preview</i> is on, and "
            "type lighting needs the watcher running (it reads your keypresses).<br>"
            "• The watcher must have OpenRGB's SDK Server running.<br>"
            "• Key functions, key states, and the re-sync shortcut all need your user "
            "in the <tt>input</tt> group.<br>"
            "• Match strings match the window <i>class/process</i>, not the title, use "
            "Detect if unsure."
        )
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(text)
        v.addStretch(1)
        scroll.setWidget(inner)
        outer = QVBoxLayout(box)
        outer.addWidget(scroll)
        return box

    def _build_color_panel(self) -> QWidget:
        box = QGroupBox("Color")
        box.setFixedWidth(300)
        v = QVBoxLayout(box)

        # embedded HSV wheel (always visible, like OpenRGB)
        self.wheel = ColorWheel(self._on_wheel_changed, size=220)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(1)
        wheel_row.addWidget(self.wheel)
        wheel_row.addStretch(1)
        v.addLayout(wheel_row)

        self.color_preview = QLabel()
        self.color_preview.setFixedHeight(28)
        v.addWidget(self.color_preview)

        # preset swatches
        swatch_row = QHBoxLayout()
        swatch_row.setSpacing(3)
        for hexc in _PRESETS:
            s = QPushButton()
            s.setFixedSize(24, 24)
            s.setStyleSheet(f"background-color: #{hexc}; border: 1px solid #555;")
            s.clicked.connect(lambda _c=False, h=hexc: self._set_picker_hex(h, apply=False))
            swatch_row.addWidget(s)
        v.addLayout(swatch_row)

        # RGB + hex
        rgb_row = QHBoxLayout()
        self.r_spin, self.g_spin, self.b_spin = QSpinBox(), QSpinBox(), QSpinBox()
        for label, spin in [("R", self.r_spin), ("G", self.g_spin), ("B", self.b_spin)]:
            spin.setRange(0, 255)
            spin.valueChanged.connect(self._on_rgb_spin_changed)
            rgb_row.addWidget(QLabel(label))
            rgb_row.addWidget(spin)
        v.addLayout(rgb_row)

        hex_row = QHBoxLayout()
        hex_row.addWidget(QLabel("Hex"))
        self.hex_field = QLineEdit()
        self.hex_field.setMaxLength(7)
        self.hex_field.editingFinished.connect(self._on_hex_changed)
        hex_row.addWidget(self.hex_field)
        v.addLayout(hex_row)

        # no-color / transparent for the selected zone (clears its static color)
        self.zone_transp_btn = QPushButton("⊘  No color (transparent, effect only)")
        self.zone_transp_btn.setToolTip("Clear the selected zone's static color so "
                                        "nothing shows except its type-lighting. Ideal "
                                        "for a whole-keyboard type-lighting layer.")
        self.zone_transp_btn.clicked.connect(self._on_zone_transparent)
        v.addWidget(self.zone_transp_btn)

        # saved custom colors
        v.addWidget(QLabel("Saved colors"))

        # The saved colours live in their own little scroll area: filling all 64
        # slots would otherwise push the panel (and the window) taller. Three
        # rows are visible and the rest scrolls.
        swatch_host = QWidget()
        self.custom_grid = QGridLayout(swatch_host)
        self.custom_grid.setSpacing(3)
        self.custom_grid.setContentsMargins(0, 0, 0, 0)
        self.custom_buttons: list[SwatchButton] = []
        self.custom_scroll = QScrollArea()
        self.custom_scroll.setWidget(swatch_host)
        self.custom_scroll.setWidgetResizable(True)
        self.custom_scroll.setFrameShape(QFrame.NoFrame)
        self.custom_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rows = max(1, self._slot_count() // CUSTOM_ROW)
        self.custom_scroll.setFixedHeight(rows * 29 + 4)
        v.addWidget(self.custom_scroll)
        self._load_custom_colors()
        self._rebuild_custom_grid()

        # brightness (per selected zone)
        v.addWidget(self._hline())
        self.bright_title = QLabel("Brightness (select a zone)")
        v.addWidget(self.bright_title)
        bright_row = QHBoxLayout()
        self.bright_slider = QSlider(Qt.Horizontal)
        self.bright_slider.setRange(0, 100)
        self.bright_slider.setValue(100)
        self.bright_slider.valueChanged.connect(self._on_brightness_changed)
        bright_row.addWidget(self.bright_slider)
        self.bright_label = QLabel("100%")
        bright_row.addWidget(self.bright_label)
        v.addLayout(bright_row)

        v.addWidget(self._hline())
        self.apply_btn = QPushButton("Apply color to zone")
        self.apply_btn.setToolTip("Recolor the selected zone. With keys selected and "
                                  "no zone active, makes a new zone from them.")
        self.apply_btn.clicked.connect(self._apply_picker_to_zone)
        self.apply_btn.setMinimumHeight(34)
        v.addWidget(self.apply_btn)
        base_btn = QPushButton("Set as mode BASE color")
        base_btn.clicked.connect(self._on_set_base_color)
        v.addWidget(base_btn)

        v.addStretch(1)
        self._set_picker_hex("FFFFFF", apply=False)
        self._update_brightness_enabled()
        return box

    # -------------------------------------------------- saved colors ---

    def _load_custom_colors(self):
        """Colours are stored to the cap; how many are *shown* is a setting, so
        shrinking the visible slots never throws saved colours away."""
        self.custom_colors: list = [None] * CUSTOM_MAX
        try:
            if CUSTOM_COLORS_PATH.exists():
                data = json.loads(CUSTOM_COLORS_PATH.read_text())
                for i, c in enumerate((data.get("colors") or [])[:CUSTOM_MAX]):
                    self.custom_colors[i] = c
        except Exception:
            pass

    def _save_custom_colors(self):
        try:
            CUSTOM_COLORS_PATH.write_text(json.dumps({"colors": self.custom_colors}, indent=2))
        except Exception as e:
            self._status(f"Couldn't save custom colors: {e}")

    def _rebuild_custom_grid(self):
        for b in self.custom_buttons:
            b.setParent(None)
        self.custom_buttons = []
        for i, hexc in enumerate(self.custom_colors[:self._slot_count()]):
            btn = SwatchButton(i, self._on_custom_left, self._on_custom_right)
            btn.set_hex(hexc)
            self.custom_grid.addWidget(btn, i // CUSTOM_ROW, i % CUSTOM_ROW)
            self.custom_buttons.append(btn)

    def _on_custom_left(self, index):
        hexc = self.custom_colors[index]
        if hexc:  # filled -> use it
            self._set_picker_hex(hexc, apply=False)
        else:     # empty -> save current picker color here
            self.custom_colors[index] = self._picker_hex()
            self.custom_buttons[index].set_hex(self.custom_colors[index])
            self._save_custom_colors()

    def _on_custom_right(self, index):
        self.custom_colors[index] = None
        self.custom_buttons[index].set_hex(None)
        self._save_custom_colors()

    def _slot_count(self) -> int:
        """How many saved-color slots to show (a multiple of 8, from Settings)."""
        n = int(self.settings.get("saved_color_slots", 24))
        n = max(CUSTOM_ROW, min(CUSTOM_MAX, n))
        return (n // CUSTOM_ROW) * CUSTOM_ROW

    def _update_brightness_enabled(self):
        on = self.active_zone_idx is not None
        self.bright_slider.setEnabled(on)
        self.bright_title.setText("Brightness (selected zone)" if on
                                  else "Brightness (select a zone)")
        for attr in ("effect_combo", "zone_up_btn", "zone_down_btn", "zone_transp_btn"):
            w = getattr(self, attr, None)
            if w is not None:
                w.setEnabled(on)

    def _hline(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    @staticmethod
    def _find_gap_columns(matrix_map, num_cols) -> set:
        gap_cols = set(range(num_cols))
        for row in matrix_map:
            for col_i, led_idx in enumerate(row):
                if led_idx is not None:
                    gap_cols.discard(col_i)
        return gap_cols

    # ---------------------------------------------------- full reload ---

    def _reload_all(self):
        self._loading = True
        # profiles combo
        self.profile_combo.clear()
        for name in self._all_profiles():
            self.profile_combo.addItem(name, name)
        idx = self.profile_combo.findData(self.current_profile_name)
        self.profile_combo.setCurrentIndex(max(0, idx))
        self.match_field.setText(self._profile().get("match", ""))
        self.match_field.setEnabled(self.current_profile_name != rc.DEFAULT_PROFILE_NAME)
        self._loading = False
        self._reload_export_combo()
        self._reload_modes()

    def _reload_modes(self):
        self._loading = True
        self.mode_list.clear()
        active = self._profile()["active_mode"]
        names = list(self._profile()["modes"].keys())
        if self.current_mode_name not in names:
            self.current_mode_name = active if active in names else names[0]
        for name in names:
            label = f"● {name}" if name == active else f"   {name}"
            self.mode_list.addItem(QListWidgetItem(label))
        self.mode_list.setCurrentRow(names.index(self.current_mode_name))
        self._loading = False
        self._reload_zones()
        # keep the Functions tab's mode target dropdowns in sync with the
        # current mode list (fixes new modes not appearing until restart)
        if hasattr(self, "press_action"):
            self._load_func_editor()

    def _reload_zones(self):
        self._loading = True
        self.zone_list.clear()
        for z in self._zones():
            item = QListWidgetItem(self._zone_item_text(z))
            item.setIcon(self._swatch_icon(z))
            self.zone_list.addItem(item)
        self.active_zone_idx = None
        self._loading = False
        self._render_keyboard()
        self._update_selection_label()
        self._update_brightness_enabled()
        self._sync_effect_controls()

    def _render_keyboard(self):
        layout = rc.resolve_mode_layout(self._mode())
        for key_name, tile in self.tiles.items():
            tile.set_color(layout.get(key_name, layout["_default"]))
            if self.edit_mode == "functions":
                # keep the RGB fill; border shows function assignment / selection
                if key_name == self.func_selected_key:
                    tile.set_border("selected")
                else:
                    tile.set_border(self._key_function_kind(key_name))
            elif self.edit_mode == "keystates":
                if key_name == getattr(self, "ks_selected_key", None):
                    tile.set_border("selected")
                elif key_name in self._mode().get("key_states", {}):
                    tile.set_border("keystate")
                else:
                    tile.set_border(None)
            else:
                if key_name in self.selected_keys:
                    # green while editing a zone's members, yellow for a free selection
                    tile.set_border("zone" if self.active_zone_idx is not None else "selected")
                else:
                    tile.set_border(None)

    def _update_selection_label(self):
        self.selection_label.setText(f"{len(self.selected_keys)} keys selected")

    # ---------------------------------------------------- profiles ---

    def _on_profile_selected(self, _idx):
        if self._loading:
            return
        data = self.profile_combo.currentData()
        if data is None:
            return
        self.current_profile_name = data
        self.current_mode_name = self._profile()["active_mode"]
        self.selected_keys.clear()
        self.func_selected_key = None
        self._load_func_editor()
        self.match_field.setText(self._profile().get("match", ""))
        self.match_field.setEnabled(self.current_profile_name != rc.DEFAULT_PROFILE_NAME)
        self._reload_modes()
        self._apply_live()  # push the newly-selected profile to the keyboard

    def _on_new_profile(self):
        name, ok = QInputDialog.getText(self, "New Profile", "Profile name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self._all_profiles():
            QMessageBox.warning(self, "Exists", f"Profile '{name}' already exists.")
            return
        self._all_profiles()[name] = {
            "match": name.lower(), "active_mode": rc.DEFAULT_MODE_NAME,
            "modes": {rc.DEFAULT_MODE_NAME: {"base_color": "000000", "zones": []}},
        }
        self.current_profile_name = name
        self.current_mode_name = rc.DEFAULT_MODE_NAME
        self._reload_all()
        self._status(f"Created profile '{name}' (unsaved).")

    def _on_rename_profile(self):
        if self.current_profile_name == rc.DEFAULT_PROFILE_NAME:
            QMessageBox.warning(self, "Can't rename", "The Default profile can't be renamed.")
            return
        old = self.current_profile_name
        new, ok = QInputDialog.getText(self, "Rename Profile", "New name:", text=old)
        if not ok or not new.strip() or new.strip() == old:
            return
        new = new.strip()
        if new in self._all_profiles():
            QMessageBox.warning(self, "Exists", f"Profile '{new}' already exists.")
            return
        # preserve dict order
        dev = self.cfg["devices"][self.device_name]
        dev["profiles"] = {(new if k == old else k): v for k, v in dev["profiles"].items()}
        self.current_profile_name = new
        self._reload_all()
        self._status(f"Renamed profile to '{new}' (unsaved).")

    def _on_delete_profile(self):
        if self.current_profile_name == rc.DEFAULT_PROFILE_NAME:
            QMessageBox.warning(self, "Can't delete", "The Default profile can't be deleted.")
            return
        name = self.current_profile_name
        if QMessageBox.question(self, "Delete", f"Delete profile '{name}'?") != QMessageBox.Yes:
            return
        del self._all_profiles()[name]
        self.current_profile_name = rc.DEFAULT_PROFILE_NAME
        self.current_mode_name = self._profile()["active_mode"]
        self._reload_all()
        self._status(f"Deleted profile '{name}' (unsaved).")

    def _on_match_changed(self):
        if self._loading:
            return
        self._profile()["match"] = self.match_field.text().strip()

    # -- auto-detect the match string from the focused game window --

    def _on_detect_window(self):
        if self.current_profile_name == rc.DEFAULT_PROFILE_NAME:
            QMessageBox.information(
                self, "Default profile",
                "The Default profile has no match string (it's the idle fallback). "
                "Pick or create a game profile first.")
            return
        self._detect_seconds = int(self.settings.get("detect_seconds", 10))
        self.detect_btn.setEnabled(False)
        self._detect_tick()

    _COUNTDOWN_KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"]

    def _detect_tick(self):
        if self._detect_seconds > 0:
            self.detect_btn.setText(f"Focus your game… {self._detect_seconds}")
            self._status(f"Focus your game window, capturing in {self._detect_seconds}s")
            self._show_countdown_frame(self._detect_seconds)
            self._detect_seconds -= 1
            QTimer.singleShot(1000, self._detect_tick)
        else:
            self._do_detect()

    def _show_countdown_frame(self, remaining):
        """Light the number-row keys 1..0 as a live countdown on the physical
        board (fewer lit as time runs out), green early, red in the final 3s."""
        if not self.settings.get("detect_countdown_lights", True):
            return
        try:
            n = len(self.device.leds)
            colors = [rc.hex_to_rgbcolor("000000")] * n
            hexc = "FF2A2A" if remaining <= 3 else "00DD66"
            for k in self._COUNTDOWN_KEYS[:remaining]:
                idx = self.led_lookup.get(k.lower())
                if idx is not None:
                    colors[idx] = rc.hex_to_rgbcolor(hexc)
            self.device.set_colors(colors)
        except Exception:
            pass

    def _play_done_sound(self):
        """Play a short system 'ding' (if enabled) at the configured volume so
        the user knows the capture fired even when their game is focused."""
        if not self.settings.get("detect_ding", True):
            return
        vol = max(0, min(100, int(self.settings.get("ding_volume", 35)))) / 100.0
        # Prefer players that support a volume argument. canberra has no volume
        # control, so it's only a fallback.
        snd = "/usr/share/sounds/freedesktop/stereo/complete.oga"
        players = [
            ["pw-play", f"--volume={vol:.2f}", snd],
            ["paplay", f"--volume={int(vol * 65536)}", snd],
            ["canberra-gtk-play", "-i", "complete"],
            ["aplay", "-q", "/usr/share/sounds/alsa/Front_Center.wav"],
        ]
        for cmd in players:
            exe = shutil.which(cmd[0])
            if not exe:
                continue
            arg = cmd[-1]
            if arg.startswith("/") and not Path(arg).exists():
                continue
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception:
                continue
        try:
            QApplication.beep()  # last-resort system bell
        except Exception:
            pass

    def _flash_keyboard(self, hex_color="FFFFFF", blinks=2):
        """Blink the whole keyboard as a 'done' cue that's visible even when a
        fullscreen game is up (where the system 'ding' can get suppressed).
        Restores the current mode afterward."""
        try:
            n = len(self.device.leds)
            on = [rc.hex_to_rgbcolor(hex_color)] * n
            off = [rc.hex_to_rgbcolor("000000")] * n
            for _ in range(blinks):
                self.device.set_colors(on)
                QApplication.processEvents()
                time.sleep(0.11)
                self.device.set_colors(off)
                QApplication.processEvents()
                time.sleep(0.11)
            self._push_to_keyboard()  # restore the mode's colors
        except Exception:
            pass

    def _restore_after_detect(self):
        """Flash the board (if enabled) then restore the mode's colors, or just
        restore if the flash is off. Covers the case where the countdown lights
        took over the board."""
        if self.settings.get("finale_flash", True):
            self._flash_keyboard()  # flashes, then restores via _push_to_keyboard
        else:
            self._push_to_keyboard()

    def _do_detect(self):
        self.detect_btn.setText("Detect from focused window")
        self.detect_btn.setEnabled(True)
        # capture the focused window FIRST (before the flash, so focus is still
        # on the game), then signal completion.
        try:
            result = rc.get_active_window()
        except Exception as e:
            self._play_done_sound()
            self._restore_after_detect()
            self._status(f"Detect failed: {e}")
            return
        self._play_done_sound()
        self._restore_after_detect()
        if not result:
            self._status("Couldn't read the focused window (nothing focused?).")
            return
        win_class, pid = result
        proc = rc.process_name_for_pid(pid)
        match = rc.best_match_string(win_class, proc)
        if not match:
            self._status("No window class or process name found for that window.")
            return
        self._loading = True
        self.match_field.setText(match)
        self._loading = False
        self._profile()["match"] = match
        self._status(f"Detected class '{win_class}', process '{proc}'. "
                     f"Match set to '{match}'. Save + Restart Watcher to use it.")

    # ---------------------------------------------------- modes ---

    def _mode_names(self):
        return list(self._profile()["modes"].keys())

    def _on_mode_selected(self, row):
        if self._loading or row < 0:
            return
        names = self._mode_names()
        if row < len(names):
            self.current_mode_name = names[row]
            self.selected_keys.clear()
            self._reload_zones()
            self._apply_live()  # push the newly-selected mode to the keyboard

    def _next_mode_name(self):
        """Suggest the next free 'Mode N' name for the current profile."""
        modes = self._profile()["modes"]
        n = 1
        while f"Mode {n}" in modes:
            n += 1
        return f"Mode {n}"

    def _retarget_mode_functions(self, old, new):
        """Keep mode-change function bindings pointing at a mode that got
        renamed (new is a name) or deleted (new is None)."""
        funcs = self._profile().get("functions", {})
        for key in list(funcs.keys()):
            binding = funcs[key]
            for slot in ("on_press", "on_release"):
                act = binding.get(slot)
                if act and act.get("action") == "mode" and act.get("target") == old:
                    if new is None:
                        binding.pop(slot, None)
                    else:
                        act["target"] = new
            if not binding.get("on_press") and not binding.get("on_release"):
                funcs.pop(key, None)

    def _on_add_mode(self):
        name, ok = QInputDialog.getText(
            self, "Add Mode", "Mode name (rename if you like):",
            text=self._next_mode_name())
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self._profile()["modes"]:
            QMessageBox.warning(self, "Exists", f"Mode '{name}' already exists.")
            return
        self._profile()["modes"][name] = {"base_color": "000000", "zones": [], "keys": {}}
        self.current_mode_name = name
        self._reload_modes()
        self._status(f"Added mode '{name}' (unsaved).")

    def _on_rename_mode(self):
        old = self.current_mode_name
        new, ok = QInputDialog.getText(self, "Rename Mode", "New name:", text=old)
        if not ok or not new.strip() or new.strip() == old:
            return
        new = new.strip()
        modes = self._profile()["modes"]
        if new in modes:
            QMessageBox.warning(self, "Exists", f"Mode '{new}' already exists.")
            return
        self._profile()["modes"] = {(new if k == old else k): v for k, v in modes.items()}
        if self._profile()["active_mode"] == old:
            self._profile()["active_mode"] = new
        self._retarget_mode_functions(old, new)
        self.current_mode_name = new
        self._reload_modes()
        self._status(f"Renamed mode to '{new}' (unsaved).")

    def _on_delete_mode(self):
        modes = self._profile()["modes"]
        if len(modes) <= 1:
            QMessageBox.warning(self, "Can't delete", "A profile needs at least one mode.")
            return
        name = self.current_mode_name
        if QMessageBox.question(self, "Delete", f"Delete mode '{name}'?") != QMessageBox.Yes:
            return
        del modes[name]
        if self._profile()["active_mode"] == name:
            self._profile()["active_mode"] = next(iter(modes))
        self._retarget_mode_functions(name, None)
        self.current_mode_name = next(iter(modes))
        self._reload_modes()
        self._status(f"Deleted mode '{name}' (unsaved).")

    def _on_set_active_mode(self):
        self._profile()["active_mode"] = self.current_mode_name
        self._reload_modes()
        self._status(f"This profile now starts on '{self.current_mode_name}' (unsaved).")

    # ---------------------------------------------------- selection / tiles ---

    def _on_tile_clicked(self, tile: KeyTile):
        if self.edit_mode == "about":
            return
        # Functions tab: single-key selection, load its binding editor.
        if self.edit_mode == "functions":
            self.func_selected_key = tile.key_name
            self._load_func_editor()
            self._render_keyboard()
            return
        # Key States tab: single-key selection, load its indicator editor.
        if self.edit_mode == "keystates":
            self.ks_selected_key = tile.key_name
            self._sync_ks_controls()
            self._render_keyboard()
            return
        # Color Zones tab. Ctrl-click toggles a key (add/remove); a plain click
        # starts a fresh single-key selection and drops any active zone.
        ctrl = bool(QApplication.keyboardModifiers() & Qt.ControlModifier)
        if ctrl:
            if tile.key_name in self.selected_keys:
                self.selected_keys.discard(tile.key_name)
            else:
                self.selected_keys.add(tile.key_name)
            # if a zone is selected, Ctrl-click edits that zone's keys live
            self._sync_zone_keys_if_editing()
        else:
            self.selected_keys = {tile.key_name}
            self.active_zone_idx = None
            self.zone_list.setCurrentRow(-1)
        self._render_keyboard()
        self._update_selection_label()
        self._update_brightness_enabled()

    def _sync_zone_keys_if_editing(self):
        """When a zone is selected, keep its key list equal to the current
        selection (so Ctrl-click add/remove edits the zone in place)."""
        if self.active_zone_idx is not None:
            self._zones()[self.active_zone_idx]["keys"] = sorted(self.selected_keys)
            self._refresh_zone_row(self.active_zone_idx)
            self._apply_live()

    def _on_box_select(self, names):
        """Rubber-band drag: add every key in the box to the selection.
        Only meaningful in the Color Zones tab (Functions is single-key)."""
        if self.edit_mode in ("functions", "about") or not names:
            return
        ctrl = bool(QApplication.keyboardModifiers() & Qt.ControlModifier)
        if ctrl:
            self.selected_keys.update(names)
            self._sync_zone_keys_if_editing()
        else:
            self.selected_keys = set(names)
            self.active_zone_idx = None
            self.zone_list.setCurrentRow(-1)
        self._render_keyboard()
        self._update_selection_label()
        self._update_brightness_enabled()

    def _on_clear_selection(self):
        self.selected_keys.clear()
        self.active_zone_idx = None
        self.zone_list.setCurrentRow(-1)
        self._render_keyboard()
        self._update_selection_label()
        self._update_brightness_enabled()

    # ---------------------------------------------------- zones ---

    def _on_zone_selected(self, row):
        if self._loading or row < 0 or row >= len(self._zones()):
            self.active_zone_idx = None
            return
        self.active_zone_idx = row
        zone = self._zones()[row]
        self.selected_keys = set(zone["keys"])
        self._loading = True
        self._set_picker_hex(zone.get("color") or "FFFFFF", apply=False)
        self.bright_slider.setValue(int(zone.get("brightness", 100)))
        self.bright_label.setText(f"{int(zone.get('brightness', 100))}%")
        self._loading = False
        self._sync_effect_controls()
        self._render_keyboard()
        self._update_selection_label()
        self._update_brightness_enabled()

    def _on_new_zone(self):
        if not self.selected_keys:
            QMessageBox.information(self, "No selection", "Click some keys first, then create a zone.")
            return
        n = len(self._zones()) + 1
        zone = {
            "name": f"Zone {n}",
            "keys": sorted(self.selected_keys),
            "color": self._picker_hex(),
            "brightness": self.bright_slider.value(),
        }
        # these keys are now zone-controlled; drop any loose per-key overrides
        # so the zone actually governs them (loose colors otherwise win)
        km = self._mode().get("keys", {})
        for k in list(self.selected_keys):
            km.pop(k, None)
        self._zones().append(zone)
        self._reload_zones()
        self.zone_list.setCurrentRow(len(self._zones()) - 1)
        self._apply_live()
        self._status(f"Created '{zone['name']}' with {len(zone['keys'])} keys (unsaved).")

    def _on_rename_zone(self):
        if self.active_zone_idx is None:
            return
        zone = self._zones()[self.active_zone_idx]
        new, ok = QInputDialog.getText(self, "Rename Zone", "Zone name:", text=zone["name"])
        if not ok or not new.strip():
            return
        zone["name"] = new.strip()
        row = self.active_zone_idx
        self._reload_zones()
        self.zone_list.setCurrentRow(row)

    def _on_delete_zone(self):
        if self.active_zone_idx is None:
            return
        zone = self._zones().pop(self.active_zone_idx)
        self.selected_keys.clear()
        self._reload_zones()
        self._apply_live()
        self._status(f"Deleted '{zone['name']}' (unsaved).")

    @staticmethod
    def move_in_list(items, src, dst):
        """Apply Qt's rowsMoved(src -> dst) to the backing list. Qt's dst is the
        index the row is inserted *before*, in the pre-move numbering."""
        if src < 0 or src >= len(items) or dst < 0 or dst > len(items) or src == dst:
            return items
        item = items.pop(src)
        items.insert(dst - 1 if dst > src else dst, item)
        return items

    def _on_zone_rows_moved(self, _p, start, _end, _dp, row):
        """The user dragged a zone: reorder the real zone list to match."""
        if self._loading:
            return
        self.move_in_list(self._zones(), start, row)
        new_idx = row - 1 if row > start else row
        self._reload_zones()
        self.zone_list.setCurrentRow(new_idx)
        self._apply_live()
        self._status("Reordered zones (unsaved).")

    def _on_zone_menu(self, pos):
        """Right-click menu on the zone list."""
        item = self.zone_list.itemAt(pos)
        if item is not None:
            self.zone_list.setCurrentRow(self.zone_list.row(item))
        menu = QMenu(self)
        act_up = menu.addAction("Move up")
        act_down = menu.addAction("Move down")
        menu.addSeparator()
        act_ren = menu.addAction("Rename zone")
        act_del = menu.addAction("Delete zone")
        menu.addSeparator()
        act_transp = menu.addAction("No color (transparent, effect only)")
        on_zone = self.active_zone_idx is not None
        for a in (act_up, act_down, act_ren, act_del, act_transp):
            a.setEnabled(on_zone)
        chosen = menu.exec(self.zone_list.mapToGlobal(pos))
        if chosen is act_up:
            self._on_zone_move(-1)
        elif chosen is act_down:
            self._on_zone_move(1)
        elif chosen is act_ren:
            self._on_rename_zone()
        elif chosen is act_del:
            self._on_delete_zone()
        elif chosen is act_transp:
            self._on_zone_transparent()

    def _on_zone_move(self, delta):
        """Move the selected zone up/down the layer stack (top of list wins)."""
        i = self.active_zone_idx
        if i is None:
            return
        zones = self._zones()
        j = i + delta
        if j < 0 or j >= len(zones):
            return
        zones[i], zones[j] = zones[j], zones[i]
        self._reload_zones()
        self.zone_list.setCurrentRow(j)
        self._apply_live()

    # ---- per-zone effect controls --------------------------------------

    def _eff_panel(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        return w, lay

    def _mk_spin(self, lo, hi, suffix, step):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setSuffix(suffix)
        s.setSingleStep(step)
        s.valueChanged.connect(self._on_eff_param)
        return s

    def _mk_colors_control(self, pv):
        """An 'Add color' button plus a row of clickable color swatches, shared
        by every effect type so they all look and behave the same."""
        row = QHBoxLayout()
        addb = QPushButton("Add color")
        addb.clicked.connect(self._on_eff_add_color)
        row.addWidget(addb)
        row.addStretch(1)
        pv.addLayout(row)
        srow = QHBoxLayout()
        pv.addLayout(srow)
        return srow

    def _on_eff_add_color(self):
        e = self._eff()
        if e is None:
            return
        cols = e.setdefault("colors", [])
        if len(cols) >= 8:
            self._status("Capped at 8 colors.")
            return
        cols.append(self._picker_hex())
        if e.get("type") == "colorcycle":
            e["rainbow"] = False
            prev = self._loading
            self._loading = True
            self.cc_rainbow.setChecked(False)
            self._loading = prev
        self._rebuild_effect_swatches(e)
        self._apply_live()
        self._status("Added color (unsaved).")

    def _style_swatch(self, label, hexc):
        label.setStyleSheet(
            f"background:#{hexc or '000000'}; border:1px solid #888; border-radius:3px;")

    def _eff(self):
        """The selected zone's effect dict (any type), or None."""
        if self.active_zone_idx is None:
            return None
        e = self._zones()[self.active_zone_idx].get("effect")
        return e if isinstance(e, dict) else None

    def _default_effect(self, t):
        c = self._picker_hex() or "FF3300"
        return {
            "reactive": {"type": "reactive", "colors": [c], "peak_brightness": 100,
                         "fade_seconds": 0.6},
            "breathing": {"type": "breathing", "colors": [c], "period_seconds": 3.0,
                          "min_brightness": 0, "max_brightness": 100},
            "blinking": {"type": "blinking", "colors": [c], "on_seconds": 0.4, "off_seconds": 0.4},
            "colorcycle": {"type": "colorcycle", "rainbow": True, "colors": [], "period_seconds": 5.0},
            "twinkle": {"type": "twinkle", "colors": [c], "rainbow": False,
                        "density": 0.3, "fade_seconds": 1.0},
        }.get(t)

    @staticmethod
    def _copy_effect(e):
        c = dict(e)
        if isinstance(c.get("colors"), list):
            c["colors"] = list(c["colors"])
        return c

    def _on_effect_type_changed(self, _idx):
        if self._loading or self.active_zone_idx is None:
            return
        if not hasattr(self, "_effect_stash"):
            self._effect_stash = {}          # id(zone) -> {type: effect dict}, in-memory
        t = self.effect_combo.currentData()
        z = self._zones()[self.active_zone_idx]
        cur = z.get("effect")
        # remember the effect we're leaving so switching back restores its settings
        if isinstance(cur, dict) and cur.get("type"):
            self._effect_stash.setdefault(id(z), {})[cur["type"]] = self._copy_effect(cur)
        if t is None:
            z.pop("effect", None)
        elif not (isinstance(cur, dict) and cur.get("type") == t):
            stashed = self._effect_stash.get(id(z), {}).get(t)
            z["effect"] = self._copy_effect(stashed) if stashed else self._default_effect(t)
        self._sync_effect_controls()
        self._refresh_zone_row(self.active_zone_idx)
        self._apply_live()
        self._status("Effect updated (unsaved). Restart the watcher to see it live.")

    def _on_eff_param(self, *_):
        if self._loading:
            return
        e = self._eff()
        if e is None:
            return
        t = e["type"]
        if t == "reactive":
            e["fade_seconds"] = round(self.rx_fade.value() / 1000.0, 3)
            e["peak_brightness"] = self.rx_peak.value()
        elif t == "breathing":
            e["period_seconds"] = round(self.br_period.value() / 1000.0, 3)
            e["min_brightness"] = self.br_min.value()
            e["max_brightness"] = self.br_max.value()
        elif t == "blinking":
            e["on_seconds"] = round(self.bl_on.value() / 1000.0, 3)
            e["off_seconds"] = round(self.bl_off.value() / 1000.0, 3)
        elif t == "colorcycle":
            e["rainbow"] = self.cc_rainbow.isChecked()
            e["period_seconds"] = round(self.cc_period.value() / 1000.0, 3)
        elif t == "twinkle":
            e["rainbow"] = self.tw_rainbow.isChecked()
            e["density"] = self.tw_density.value() / 100.0
            e["fade_seconds"] = round(self.tw_fade.value() / 1000.0, 3)
        self._apply_live()

    def _rebuild_swatch_row(self, layout, colors, target="effect"):
        """target: which color list these swatches edit.
        'effect'    -> the selected zone's effect colors
        'ks_stages' -> the selected key's cooldown stage colors
        'ks_toggle' -> the selected key's toggle state colors"""
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        for i, hexc in enumerate(colors or []):
            btn = SwatchButton(
                i,
                lambda idx, t=target: self._on_swatch_left(idx, t),
                lambda idx, t=target: self._on_swatch_right(idx, t),
            )
            btn.set_hex(hexc)
            layout.addWidget(btn)
        layout.addStretch(1)

    def _swatch_target_colors(self, target):
        """(list, owner) for a swatch row, or (None, None)."""
        if target == "effect":
            e = self._eff()
            return (e.get("colors") if e else None), e
        s = self._ks()
        if s is None:
            return None, None
        if target == "ks_stages" and s.get("type") == "cooldown":
            return s.setdefault("stages", []), s
        if target == "ks_toggle" and s.get("type") == "toggle":
            return s.setdefault("colors", []), s
        return None, None

    def _refresh_swatch_target(self, target):
        cols, _ = self._swatch_target_colors(target)
        if target == "effect":
            e = self._eff()
            if e:
                self._rebuild_effect_swatches(e)
        elif target == "ks_stages":
            self._rebuild_swatch_row(self.ks_stages_row, cols, "ks_stages")
        elif target == "ks_toggle":
            self._rebuild_swatch_row(self.ks_toggle_row, cols, "ks_toggle")

    def _on_swatch_left(self, index, target):
        """Left-click a swatch: set it to the current picker color."""
        cols, _ = self._swatch_target_colors(target)
        if not cols or index >= len(cols):
            return
        cols[index] = self._picker_hex()
        self._refresh_swatch_target(target)
        self._apply_live()
        self._status(f"Color {index + 1} set to #{cols[index]} (unsaved).")

    def _on_swatch_right(self, index, target):
        """Right-click a swatch: remove it."""
        cols, owner = self._swatch_target_colors(target)
        if not cols or index >= len(cols):
            return
        del cols[index]
        # these lists must keep at least one entry to stay meaningful
        if not cols and target in ("effect", "ks_toggle"):
            if not (target == "effect" and owner and owner.get("type") == "colorcycle"):
                cols.append(self._picker_hex())
        if target == "ks_toggle" and len(cols) < 2:
            cols.append("202020")
        self._refresh_swatch_target(target)
        self._apply_live()
        self._status("Removed color (unsaved).")

    def _rebuild_effect_swatches(self, e):
        rows = {"reactive": self.rx_colors_row, "breathing": self.br_colors_row,
                "blinking": self.bl_colors_row, "colorcycle": self.cc_stops_row,
                "twinkle": self.tw_colors_row}
        row = rows.get(e.get("type"))
        if row is not None:
            self._rebuild_swatch_row(row, e.get("colors"))

    def _sync_effect_controls(self):
        """Reflect the SELECTED zone's effect into the dropdown + panels."""
        if not hasattr(self, "effect_combo"):
            return
        e = self._eff() or {}
        t = e.get("type")
        prev = self._loading
        self._loading = True
        idx = 0
        for i in range(self.effect_combo.count()):
            if self.effect_combo.itemData(i) == t:
                idx = i
                break
        self.effect_combo.setCurrentIndex(idx)
        for k, panel in self._eff_panels.items():
            panel.setVisible(k == t)
        if t == "reactive":
            self.rx_fade.setValue(int(round(float(e.get("fade_seconds", 0.6)) * 1000)))
            self.rx_peak.setValue(int(e.get("peak_brightness", 100)))
            self._rebuild_swatch_row(self.rx_colors_row, e.get("colors"))
        elif t == "breathing":
            self.br_period.setValue(int(round(float(e.get("period_seconds", 3.0)) * 1000)))
            self.br_min.setValue(int(e.get("min_brightness", 0)))
            self.br_max.setValue(int(e.get("max_brightness", 100)))
            self._rebuild_swatch_row(self.br_colors_row, e.get("colors"))
        elif t == "blinking":
            self.bl_on.setValue(int(round(float(e.get("on_seconds", 0.4)) * 1000)))
            self.bl_off.setValue(int(round(float(e.get("off_seconds", 0.4)) * 1000)))
            self._rebuild_swatch_row(self.bl_colors_row, e.get("colors"))
        elif t == "colorcycle":
            self.cc_rainbow.setChecked(bool(e.get("rainbow", True)))
            self.cc_period.setValue(int(round(float(e.get("period_seconds", 5.0)) * 1000)))
            self._rebuild_swatch_row(self.cc_stops_row, e.get("colors"))
        elif t == "twinkle":
            self.tw_rainbow.setChecked(bool(e.get("rainbow", False)))
            self.tw_density.setValue(int(round(float(e.get("density", 0.3)) * 100)))
            self.tw_fade.setValue(int(round(float(e.get("fade_seconds", 1.0)) * 1000)))
            self._rebuild_swatch_row(self.tw_colors_row, e.get("colors"))
        self._loading = prev

    def _zone_item_text(self, z) -> str:
        e = z.get("effect")
        tag = f"  · {e['type']}" if isinstance(e, dict) and e.get("type") else ""
        if z.get("color", ""):
            meta = f"{len(z['keys'])} keys, {z.get('brightness', 100)}%"
        else:
            meta = f"{len(z['keys'])} keys, no color"
        return f"{z['name']}  ({meta}){tag}"

    def _swatch_icon(self, z):
        pix = QPixmap(16, 16)
        raw = z.get("color", "")
        if not raw:
            # transparent / no color: dark tile with a red diagonal slash
            pix.fill(QColor("#2b2b2b"))
            p = QPainter(pix)
            p.setPen(QPen(QColor("#e04040"), 2))
            p.drawLine(3, 13, 13, 3)
            p.end()
        else:
            hexc = rc.scale_hex(raw, z.get("brightness", 100))
            pix.fill(QColor(f"#{hexc}"))
        return QIcon(pix)

    def _on_zone_transparent(self):
        if self.active_zone_idx is None:
            QMessageBox.information(self, "No zone",
                                    "Select a zone first, then make it transparent.")
            return
        self._zones()[self.active_zone_idx]["color"] = ""
        self._refresh_zone_row(self.active_zone_idx)
        self._apply_live()
        self._status("Zone is now transparent (effect-only, unsaved).")

    # ---------------------------------------------------- key states ---

    def _key_states(self) -> dict:
        return self._mode().setdefault("key_states", {})

    def _ks(self):
        """The selected key's state indicator, or None."""
        k = getattr(self, "ks_selected_key", None)
        if not k:
            return None
        s = self._key_states().get(k)
        return s if isinstance(s, dict) else None

    def _default_key_state(self, t):
        c = self._picker_hex() or "FF2A00"
        if t == "cooldown":
            return {"type": "cooldown", "duration_seconds": 30.0, "active_color": c,
                    "stages": [], "ready_color": "00FF66", "ready_signal": "blink",
                    "ready_seconds": 2.0, "idle_color": ""}
        return {"type": "toggle", "colors": [c, "202020"], "start_index": 0}

    def _on_ks_type_changed(self, _idx):
        if self._loading or not getattr(self, "ks_selected_key", None):
            return
        t = self.ks_combo.currentData()
        states = self._key_states()
        if t is None:
            states.pop(self.ks_selected_key, None)
        else:
            cur = states.get(self.ks_selected_key)
            if not (isinstance(cur, dict) and cur.get("type") == t):
                states[self.ks_selected_key] = self._default_key_state(t)
        self._sync_ks_controls()
        self._render_keyboard()
        self._status("Key state updated (unsaved). Restart the watcher to use it.")

    def _on_ks_param(self, *_):
        if self._loading:
            return
        s = self._ks()
        if s is None or s["type"] != "cooldown":
            return
        s["duration_seconds"] = float(self.ks_duration.value())
        s["ready_signal"] = self.ks_signal.currentData() or "solid"
        s["countdown_fade"] = self.ks_fade.isChecked()
        if not self.ks_keep_ready.isChecked():
            s["ready_seconds"] = float(self.ks_ready_secs.value())

    def _on_ks_keep_ready(self, state):
        """'Keep it up until I press again' is stored as ready_seconds = 0."""
        if self._loading:
            return
        s = self._ks()
        if s is None or s["type"] != "cooldown":
            return
        keep = bool(state)
        s["ready_seconds"] = 0.0 if keep else float(self.ks_ready_secs.value())
        self.ks_ready_secs.setEnabled(not keep)

    def _ks_set_color(self, field):
        s = self._ks()
        if s is None:
            return
        s[field] = self._picker_hex()
        self._sync_ks_controls()
        self._status(f"Set {field.replace('_', ' ')} to #{s[field]} (unsaved).")

    def _on_ks_add_toggle_color(self):
        s = self._ks()
        if s is None or s["type"] != "toggle":
            return
        cols = s.setdefault("colors", [])
        if len(cols) >= 8:
            self._status("Up to 8 toggle states.")
            return
        cols.append(self._picker_hex())
        self._rebuild_swatch_row(self.ks_toggle_row, cols, "ks_toggle")
        self._status("Added toggle state (unsaved).")

    def _sync_reset_key_label(self):
        k = self.settings.get("reset_key") or ""
        self.ks_reset_label.setText(k if k else "(none set)")

    def _on_ks_set_reset_key(self):
        dlg = KeyCaptureDialog(self, list(self.tiles.keys()))
        if dlg.exec() and dlg.captured:
            combo = " + ".join(dlg.captured)
            self._set_setting("reset_key", combo)
            self._sync_reset_key_label()
            self._status(f"'{combo}' will reset key states "
                         f"(restart the watcher to apply).")

    def _on_ks_clear_reset_key(self):
        self._set_setting("reset_key", "")
        self._sync_reset_key_label()
        self._status("Re-sync shortcut cleared (restart the watcher to apply).")

    def _on_ks_clear(self):
        k = getattr(self, "ks_selected_key", None)
        if not k:
            return
        self._key_states().pop(k, None)
        self._sync_ks_controls()
        self._render_keyboard()
        self._status(f"Removed key state from {k} (unsaved).")

    def _sync_ks_controls(self):
        if not hasattr(self, "ks_combo"):
            return
        k = getattr(self, "ks_selected_key", None)
        s = self._ks() or {}
        t = s.get("type")
        prev = self._loading
        self._loading = True
        self.ks_key_label.setText(k or "No key selected")
        idx = 0
        for i in range(self.ks_combo.count()):
            if self.ks_combo.itemData(i) == t:
                idx = i
                break
        self.ks_combo.setCurrentIndex(idx)
        self.ks_combo.setEnabled(bool(k))
        for name, panel in self._ks_panels.items():
            panel.setVisible(name == t)
        if t == "cooldown":
            self.ks_duration.setValue(int(round(float(s.get("duration_seconds", 30)))))
            secs = float(s.get("ready_seconds", 2))
            keep = secs <= 0
            self.ks_keep_ready.setChecked(keep)
            self.ks_ready_secs.setEnabled(not keep)
            self.ks_ready_secs.setValue(int(round(secs)) if secs >= 1 else 2)
            self.ks_fade.setChecked(bool(s.get("countdown_fade", True)))
            sig = s.get("ready_signal", "solid")
            for i in range(self.ks_signal.count()):
                if self.ks_signal.itemData(i) == sig:
                    self.ks_signal.setCurrentIndex(i)
                    break
            self._style_swatch(self.ks_active_sw, s.get("active_color", "FF2A00"))
            self._style_swatch(self.ks_ready_sw, s.get("ready_color", "00FF66"))
            self._rebuild_swatch_row(self.ks_stages_row, s.get("stages"), "ks_stages")
        elif t == "toggle":
            self._rebuild_swatch_row(self.ks_toggle_row, s.get("colors"), "ks_toggle")
        self._loading = prev

    # ---------------------------------------------------- functions ---

    def _functions(self) -> dict:
        return self._profile().setdefault("functions", {})

    def _key_function_kind(self, key_name):
        """Returns 'mode', 'profile', or None for the border indicator."""
        b = self._functions().get(key_name)
        if not b:
            return None
        actions = [e["action"] for e in (b.get("on_press"), b.get("on_release")) if e]
        if "mode" in actions:
            return "mode"
        if "profile" in actions:
            return "profile"
        return None

    def _on_tab_changed(self, index):
        self.edit_mode = {0: "zones", 1: "functions", 2: "keystates"}.get(index, "about")
        # the Apply button only makes sense on the Color Zones tab; elsewhere
        # the color wheel feeds that tab's own "set colour" controls instead
        if hasattr(self, "apply_btn"):
            on_zones = self.edit_mode == "zones"
            self.apply_btn.setEnabled(on_zones)
            self.apply_btn.setText("Apply color to zone" if on_zones
                                   else "Apply color (Color Zones tab only)")
            self.apply_btn.setToolTip(
                "Recolor the selected zone. With keys selected and no zone "
                "active, makes a new zone from them."
                if on_zones else
                "Switch to the Color Zones tab to paint keys. On this tab, use "
                "the tab's own colour buttons and swatches.")
        # selections are independent between the tabs
        self._render_keyboard()

    def _set_func_editor_enabled(self, on):
        for w in (self.press_action, self.press_target, self.release_action, self.release_target):
            w.setEnabled(on)

    def _populate_target(self, target_combo, action):
        target_combo.blockSignals(True)
        target_combo.clear()
        if action == "mode":
            for name in self._profile()["modes"]:
                target_combo.addItem(name, name)
        elif action == "profile":
            for name in self._all_profiles():
                target_combo.addItem(name, name)
        target_combo.blockSignals(False)

    def _load_func_editor(self):
        """Fill the press/release action+target combos from the selected key's
        binding."""
        self._loading = True
        key = self.func_selected_key
        if key is None:
            self.func_key_label.setText("No key selected")
            self._set_func_editor_enabled(False)
            self._loading = False
            return
        self.func_key_label.setText(f"Key: {key}")
        self._set_func_editor_enabled(True)
        binding = self._functions().get(key, {})
        for action_combo, target_combo, event in (
            (self.press_action, self.press_target, "on_press"),
            (self.release_action, self.release_target, "on_release"),
        ):
            ev = binding.get(event)
            action = ev["action"] if ev else None
            ai = action_combo.findData(action)
            action_combo.setCurrentIndex(ai if ai >= 0 else 0)
            self._populate_target(target_combo, action)
            if ev:
                ti = target_combo.findData(ev["target"])
                target_combo.setCurrentIndex(ti if ti >= 0 else 0)
        self._loading = False

    def _on_func_changed(self, event):
        if self._loading or self.func_selected_key is None:
            return
        action_combo = self.press_action if event == "on_press" else self.release_action
        target_combo = self.press_target if event == "on_press" else self.release_target
        action = action_combo.currentData()

        # if the action just changed, repopulate the target list to match
        expected = {"mode": list(self._profile()["modes"]),
                    "profile": list(self._all_profiles())}.get(action, [])
        current_items = [target_combo.itemData(i) for i in range(target_combo.count())]
        if current_items != expected:
            self._loading = True
            self._populate_target(target_combo, action)
            self._loading = False

        key = self.func_selected_key
        funcs = self._functions()
        binding = funcs.get(key, {"on_press": None, "on_release": None})
        if action is None:
            binding[event] = None
        else:
            target = target_combo.currentData()
            binding[event] = {"action": action, "target": target} if target else None
        # drop the key entirely if nothing is bound
        if not binding.get("on_press") and not binding.get("on_release"):
            funcs.pop(key, None)
        else:
            funcs[key] = binding
        self._render_keyboard()
        self._status(f"Updated functions for '{key}' (unsaved).")

    def _on_clear_function(self):
        if self.func_selected_key is None:
            return
        self._functions().pop(self.func_selected_key, None)
        self._load_func_editor()
        self._render_keyboard()
        self._status(f"Cleared functions for '{self.func_selected_key}' (unsaved).")

    # ---------------------------------------------------- color picker ---

    def _picker_hex(self) -> str:
        return f"{self.r_spin.value():02X}{self.g_spin.value():02X}{self.b_spin.value():02X}"

    def _set_picker_hex(self, hex_color: str, apply: bool):
        hex_color = hex_color.lstrip("#")
        try:
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        except (ValueError, IndexError):
            return
        was_loading = self._loading
        self._loading = True
        self.r_spin.setValue(r)
        self.g_spin.setValue(g)
        self.b_spin.setValue(b)
        self.hex_field.setText(f"#{hex_color.upper()}")
        self.color_preview.setStyleSheet(
            f"background-color: #{hex_color}; border: 1px solid #555;")
        self.wheel.setColor(hex_color)  # keep the wheel marker in sync
        self._loading = was_loading
        if apply:
            self._apply_picker_to_zone()

    def _on_rgb_spin_changed(self, _v):
        if self._loading:
            return
        self._set_picker_hex(self._picker_hex(), apply=False)

    def _on_hex_changed(self):
        if self._loading:
            return
        text = self.hex_field.text().strip().lstrip("#")
        if len(text) == 6:
            self._set_picker_hex(text, apply=False)

    def _on_wheel_changed(self, hex_color):
        if self._loading:
            return
        self._set_picker_hex(hex_color, apply=False)

    def _on_brightness_changed(self, val):
        self.bright_label.setText(f"{val}%")
        if self._loading:
            return
        if self.active_zone_idx is not None:
            self._zones()[self.active_zone_idx]["brightness"] = val
            self._refresh_zone_row(self.active_zone_idx)
            self._apply_live()

    def _apply_picker_to_zone(self):
        """Apply the picker color. With a zone selected, recolor that zone.
        With loose keys selected, turn them into a new zone and color it, so
        everything on the board is always a zone."""
        if self.active_zone_idx is not None:
            self._zones()[self.active_zone_idx]["color"] = self._picker_hex()
            self._refresh_zone_row(self.active_zone_idx)
            self._apply_live()
        elif self.selected_keys:
            self._on_new_zone()          # names it, colors it, selects it

    def _refresh_zone_row(self, idx):
        z = self._zones()[idx]
        item = self.zone_list.item(idx)
        if item:
            item.setText(self._zone_item_text(z))
            item.setIcon(self._swatch_icon(z))
        self._render_keyboard()

    def _on_set_base_color(self):
        self._mode()["base_color"] = self._picker_hex()
        self._render_keyboard()
        self._apply_live()
        self._status(f"Set base color to #{self._picker_hex()} (unsaved).")

    # ---------------------------------------------------- apply / save ---

    def _apply_live(self):
        if self.live_check.isChecked():
            self._push_to_keyboard()

    def _push_to_keyboard(self):
        try:
            mode = self._mode()
            layout = rc.resolve_mode_layout(mode)
            colors = rc.build_color_array(layout, self.led_lookup, len(self.device.leds))
            if rc.mode_has_effects(mode):
                # animate effects right here in the preview (reactive shows its
                # base only, since keypresses are captured by the watcher)
                self._preview.configure(colors, mode)
                self._preview.start()
                self._status("Live preview running (effects animate here).")
            else:
                self._preview.stop()
                self.device.set_colors(colors)
                self._status("Applied to keyboard.")
        except Exception as e:
            self._status(f"Failed to apply: {e}")

    def _on_live_toggled(self, on):
        # a running watcher and this preview would otherwise both drive the
        # same LEDs, which looks like two effects fighting
        try:
            rc.write_watcher_pause(bool(on))
        except Exception:
            pass
        if on:
            self._apply_live()
        else:
            self._preview.stop()
            self._status("Live preview off. The watcher has the keyboard again.")

    def closeEvent(self, e):
        try:
            self._preview.stop()
        except Exception:
            pass
        try:
            rc.write_watcher_pause(False)   # hand the keyboard back
        except Exception:
            pass
        super().closeEvent(e)

    def _on_apply_now(self):
        """Push to the keyboard now AND sync a running watcher: save the config
        so the watcher reads current state, then drop a command telling it to
        switch to this profile. Without this, the watcher keeps re-applying the
        profile it thinks is active and key functions test against the wrong one."""
        self._push_to_keyboard()
        try:
            rc.save_config(self.cfg, self.config_path)
            rc.write_watcher_command(self.current_profile_name)
            self._status(f"Applied '{self.current_profile_name}' and synced the watcher "
                         f"(if it's running).")
        except Exception as e:
            self._status(f"Applied to keyboard, but couldn't sync the watcher: {e}")

    def _on_save(self):
        try:
            rc.save_config(self.cfg, self.config_path)
            self._status(f"Saved to {self.config_path.name}.")
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))

    def _on_start_restart_watcher(self):
        """Kill any running watcher, then launch a fresh detached one so it
        picks up the latest saved games.json."""
        try:
            # save first so the watcher starts from current edits
            rc.save_config(self.cfg, self.config_path)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", f"Couldn't save before restart:\n{e}")
            return
        try:
            rc.stop_running_watcher()      # by PID, so it works on any platform
            if not IS_WINDOWS:
                # also catch watchers started before PID files existed
                subprocess.run(["pkill", "-f", WATCHER_SCRIPT.name],
                               capture_output=True)
            time.sleep(0.4)

            log_path = Path(tempfile.gettempdir()) / "modeshift_watcher.log"
            log = open(log_path, "a")
            kwargs = {"stdout": log, "stderr": log}
            if IS_WINDOWS:
                # pythonw so no console window pops up, and detach from us
                exe = Path(sys.executable)
                pythonw = exe.with_name("pythonw.exe")
                python = str(pythonw if pythonw.exists() else exe)
                kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW |
                                           subprocess.DETACHED_PROCESS)
            else:
                python = sys.executable
                kwargs["start_new_session"] = True
            subprocess.Popen([python, str(WATCHER_SCRIPT)], **kwargs)
            self._status(f"Watcher (re)started. Check your tray for its icon "
                         f"(log: {log_path}).")
        except Exception as e:
            QMessageBox.critical(self, "Watcher failed to start", str(e))

    def _status(self, text):
        self.status_label.setText(text)


def main():
    app = QApplication(sys.argv)
    try:
        cfg = rc.load_config()
    except FileNotFoundError:
        # fresh install / first run: start empty, the device is auto-detected below
        cfg = {"openrgb": {"host": "127.0.0.1", "port": 6742},
               "poll_interval_seconds": 1.5, "devices": {}, "active_device": None}
    except Exception as e:
        QMessageBox.critical(None, "Config error", f"Couldn't load games.json:\n{e}")
        sys.exit(1)
    try:
        client = rc.open_client(cfg, client_name="profile-editor")
        device, device_name = rc.select_device(cfg, client)  # auto-detects the keyboard
    except Exception as e:
        QMessageBox.critical(
            None, "OpenRGB connection failed",
            f"{e}\n\nMake sure OpenRGB is running with the SDK Server started "
            f"(Server tab -> Start Server), and a per-key keyboard is connected.",
        )
        sys.exit(1)
    try:
        win = MainWindow(cfg, device, device_name, rc.CONFIG_PATH)
    except Exception as e:
        QMessageBox.critical(None, "Startup error", str(e))
        sys.exit(1)
    w, h = win.sized_for_keyboard()
    # hard stop: never let the window shrink past the keyboard, and open at
    # exactly that minimum rather than something taller
    win.setMinimumSize(w, h)
    win.resize(w, h)
    # live preview starts on, so tell any running watcher to stand down
    try:
        rc.write_watcher_pause(True)
    except Exception:
        pass
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
