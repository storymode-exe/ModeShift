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
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from PySide6.QtCore import Qt, QRect, QRectF, QSize, QPointF, QUrl, QTimer
    from PySide6.QtGui import (
        QColor, QPainter, QConicalGradient, QLinearGradient, QBrush, QPen,
        QDesktopServices,
    )
    from PySide6.QtWidgets import (
        QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout,
        QHBoxLayout, QPushButton, QComboBox, QLineEdit, QLabel,
        QMessageBox, QInputDialog, QScrollArea, QCheckBox,
        QFrame, QListWidget, QListWidgetItem, QSlider, QSpinBox, QGroupBox,
        QRubberBand, QTabWidget,
    )
except ImportError:
    print("Missing dependency: PySide6. Install with: pip install PySide6", file=sys.stderr)
    sys.exit(1)

import modeshift_common as rc

APP_NAME = "ModeShift"
APP_VERSION = "1.0.0"
APP_AUTHOR = "StoryMode"
APP_LICENSE = "GPLv3"
KOFI_URL = "https://ko-fi.com/storymode"

WATCHER_SCRIPT = Path(__file__).parent / "modeshift_watcher.py"

CUSTOM_COLORS_PATH = rc.CONFIG_PATH.parent / "custom_colors.json"
SETTINGS_PATH = rc.CONFIG_PATH.parent / "settings.json"

DEFAULT_SETTINGS = {
    "detect_ding": True,
    "ding_volume": 35,           # percent
    "detect_countdown_lights": True,
    "detect_seconds": 10,
    "finale_flash": True,
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
    "selected": "3px solid #FFD700",   # yellow: currently selected
    "mode": "3px solid #2ECC40",       # green: has a Change-Mode function
    "profile": "3px solid #FF4136",    # red: has a Change-Profile function
}


class KeyTile(QPushButton):
    def __init__(self, key_name: str):
        super().__init__(_short_label(key_name))
        self.key_name = key_name
        self.setToolTip(key_name)
        self.setFixedSize(54, 46)
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
            f"border: {border}; border-radius: 3px; font-size: 10px; }}"
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


class MainWindow(QMainWindow):
    def __init__(self, cfg: dict, device, device_name, config_path):
        super().__init__()
        self.cfg = cfg
        self.device = device
        self.device_name = device_name
        self.config_path = config_path
        self.led_lookup = rc.build_led_lookup(device)
        self.zone = rc.find_matrix_zone(device)
        if self.zone is None:
            raise RuntimeError(f"Device '{device.name}' has no matrix zone to draw.")

        self.settings = self._load_settings()
        self.current_profile_name = rc.DEFAULT_PROFILE_NAME
        self.current_mode_name = self._profile()["active_mode"]
        self.selected_keys: set[str] = set()
        self.active_zone_idx: int | None = None
        self.tiles: dict[str, KeyTile] = {}
        self.edit_mode = "zones"          # 'zones' or 'functions'
        self.func_selected_key: str | None = None  # single-key selection in Functions tab
        self._loading = False  # guard against signal recursion

        self.setWindowTitle(f"ModeShift Profile Editor: {device_name}")
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

        # ===== top: left panel | keyboard =====
        top = QHBoxLayout()
        top.addWidget(self._build_left_panel())
        top.addWidget(self._build_keyboard(), stretch=1)
        root.addLayout(top, stretch=1)

        # ===== bottom: tabbed (Color Zones / Functions) | color picker =====
        bottom = QHBoxLayout()
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.addTab(self._build_zones_panel(), "Color Zones")
        self.bottom_tabs.addTab(self._build_functions_panel(), "Functions")
        self.bottom_tabs.addTab(self._build_settings_panel(), "Settings")
        self.bottom_tabs.addTab(self._build_howto_panel(), "How-To")
        self.bottom_tabs.addTab(self._build_about_panel(), "About")
        self.bottom_tabs.currentChanged.connect(self._on_tab_changed)
        bottom.addWidget(self.bottom_tabs, stretch=1)
        bottom.addWidget(self._build_color_panel())
        root.addLayout(bottom)

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

        v.addWidget(QLabel("<b>Modes</b>  (active = applied by watcher)"))
        self.mode_list = QListWidget()
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

        self.set_active_btn = QPushButton("Set selected mode active")
        self.set_active_btn.clicked.connect(self._on_set_active_mode)
        v.addWidget(self.set_active_btn)

        v.addWidget(self._hline())
        self.live_check = QCheckBox("Live preview on keyboard")
        self.live_check.setChecked(True)
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
        return scroll

    def sized_for_keyboard(self):
        """Return a (width, height) that shows the whole keyboard without
        clipping the numpad, clamped to the available screen."""
        try:
            kb_w = self.keyboard_container.sizeHint().width()
        except Exception:
            kb_w = 1300
        # 240 left panel + inter-panel spacing + scrollbar + window chrome
        want_w = kb_w + 300
        want_h = 820
        try:
            screen = QApplication.primaryScreen().availableGeometry()
            want_w = min(want_w, screen.width() - 40)
            want_h = min(want_h, screen.height() - 60)
        except Exception:
            pass
        return max(want_w, 1000), max(want_h, 600)

    def _build_zones_panel(self) -> QWidget:
        box = QGroupBox("Zones (in selected mode)")
        v = QVBoxLayout(box)

        v.addWidget(QLabel(
            "<i>Select key(s) and pick a color to set them directly. "
            "Make a zone if you want to name a group and recolor it all at once.</i>"))

        self.zone_list = QListWidget()
        self.zone_list.currentRowChanged.connect(self._on_zone_selected)
        self.zone_list.itemDoubleClicked.connect(lambda _i: self._on_rename_zone())
        v.addWidget(self.zone_list)

        row1 = QHBoxLayout()
        new_zone_btn = QPushButton("New zone from selection")
        new_zone_btn.clicked.connect(self._on_new_zone)
        row1.addWidget(new_zone_btn)
        clear_btn = QPushButton("Clear selection")
        clear_btn.clicked.connect(self._on_clear_selection)
        row1.addWidget(clear_btn)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        rename_btn = QPushButton("Rename zone")
        rename_btn.clicked.connect(self._on_rename_zone)
        row2.addWidget(rename_btn)
        del_btn = QPushButton("Delete zone")
        del_btn.clicked.connect(self._on_delete_zone)
        row2.addWidget(del_btn)
        v.addLayout(row2)

        reset_btn = QPushButton("Reset selected keys to base color")
        reset_btn.setToolTip("Remove direct colors from the selected keys "
                             "(they fall back to their zone or the base color).")
        reset_btn.clicked.connect(self._on_reset_keys)
        v.addWidget(reset_btn)

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

        v.addStretch(1)
        return box

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
        inner = QWidget()
        v = QVBoxLayout(inner)
        text = QLabel(
            "<h3>Quick start</h3>"
            "<b>1. Color your keys.</b> Click a key (or drag a box over several) and pick "
            "a color, it applies right away. No zone needed. Use <i>Set as mode BASE "
            "color</i> for the whole board.<br><br>"
            "<b>2. Zones (optional).</b> Select keys and hit <i>New zone from selection</i> "
            "to name a group you can recolor or dim all at once.<br><br>"
            "<b>3. Profiles &amp; games.</b> Make a profile per game with <i>New</i>, then set "
            "its <b>Match string</b>, or just click <i>Detect from focused window</i>, "
            "alt-tab into the game, and it fills it for you.<br><br>"
            "<b>4. Modes.</b> A profile can hold several modes (e.g. Star Citizen: Flight, "
            "On Foot). The <b>active</b> mode is what the watcher shows.<br><br>"
            "<b>5. Functions.</b> On the Functions tab, click one key and bind it to change "
            "MODE (green) or PROFILE (red) on press and/or release, for momentary, "
            "hold-to-light effects like Star Citizen's flight vs on-foot lighting.<br><br>"
            "<b>6. Go live.</b> Hit <i>Apply Now</i> to push a profile and sync the running "
            "watcher, or <i>Start / Restart Watcher</i> to (re)launch it. The watcher then "
            "switches profiles automatically based on your focused game.<br><br>"

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

            "<h3>Tips</h3>"
            "• Direct key colors beat zone colors beat the base color.<br>"
            "• The watcher must have OpenRGB's SDK Server running.<br>"
            "• Key functions need your user in the <tt>input</tt> group.<br>"
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
            s.clicked.connect(lambda _c=False, h=hexc: self._set_picker_hex(h, apply=True))
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

        # saved custom colors
        saved_header = QHBoxLayout()
        saved_header.addWidget(QLabel("Saved colors"))
        saved_header.addStretch(1)
        add_row_btn = QPushButton("+")
        add_row_btn.setFixedSize(24, 22)
        add_row_btn.setToolTip("Add another row of 8 slots")
        add_row_btn.clicked.connect(self._on_add_custom_row)
        saved_header.addWidget(add_row_btn)
        v.addLayout(saved_header)

        self.custom_grid = QGridLayout()
        self.custom_grid.setSpacing(3)
        self.custom_buttons: list[SwatchButton] = []
        v.addLayout(self.custom_grid)
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
        base_btn = QPushButton("Set as mode BASE color")
        base_btn.clicked.connect(self._on_set_base_color)
        v.addWidget(base_btn)

        v.addStretch(1)
        self._set_picker_hex("FFFFFF", apply=False)
        self._update_brightness_enabled()
        return box

    # -------------------------------------------------- saved colors ---

    def _load_custom_colors(self):
        self.custom_colors: list = [None] * CUSTOM_ROW
        try:
            if CUSTOM_COLORS_PATH.exists():
                data = json.loads(CUSTOM_COLORS_PATH.read_text())
                colors = data.get("colors", [])
                # pad up to a whole number of rows, min one row, max cap
                n = max(CUSTOM_ROW, min(CUSTOM_MAX, ((len(colors) + CUSTOM_ROW - 1) // CUSTOM_ROW) * CUSTOM_ROW))
                self.custom_colors = [None] * n
                for i, c in enumerate(colors[:n]):
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
        for i, hexc in enumerate(self.custom_colors):
            btn = SwatchButton(i, self._on_custom_left, self._on_custom_right)
            btn.set_hex(hexc)
            self.custom_grid.addWidget(btn, i // CUSTOM_ROW, i % CUSTOM_ROW)
            self.custom_buttons.append(btn)

    def _on_custom_left(self, index):
        hexc = self.custom_colors[index]
        if hexc:  # filled -> use it
            self._set_picker_hex(hexc, apply=True)
        else:     # empty -> save current picker color here
            self.custom_colors[index] = self._picker_hex()
            self.custom_buttons[index].set_hex(self.custom_colors[index])
            self._save_custom_colors()

    def _on_custom_right(self, index):
        self.custom_colors[index] = None
        self.custom_buttons[index].set_hex(None)
        self._save_custom_colors()

    def _on_add_custom_row(self):
        if len(self.custom_colors) >= CUSTOM_MAX:
            self._status(f"Custom colors capped at {CUSTOM_MAX}.")
            return
        self.custom_colors.extend([None] * CUSTOM_ROW)
        self._rebuild_custom_grid()
        self._save_custom_colors()

    def _update_brightness_enabled(self):
        on = self.active_zone_idx is not None
        self.bright_slider.setEnabled(on)
        self.bright_title.setText("Brightness (selected zone)" if on
                                  else "Brightness (select a zone)")

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
            self.zone_list.addItem(f"{z['name']}  ({len(z['keys'])} keys, {z['brightness']}%)")
        self.active_zone_idx = None
        self._loading = False
        self._render_keyboard()
        self._update_selection_label()
        self._update_brightness_enabled()

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
            else:
                tile.set_border("selected" if key_name in self.selected_keys else None)

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
        match = (win_class or proc or "").strip().lower()
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
        self._status(f"'{self.current_mode_name}' is now the active mode (unsaved).")

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
        # Color Zones tab: toggle multi-selection.
        if tile.key_name in self.selected_keys:
            self.selected_keys.discard(tile.key_name)
        else:
            self.selected_keys.add(tile.key_name)
        # manual selection detaches from any active zone
        self.active_zone_idx = None
        self.zone_list.setCurrentRow(-1)
        tile.set_border("selected" if tile.key_name in self.selected_keys else None)
        self._update_selection_label()
        self._update_brightness_enabled()

    def _on_box_select(self, names):
        """Rubber-band drag: add every key in the box to the selection.
        Only meaningful in the Color Zones tab (Functions is single-key)."""
        if self.edit_mode in ("functions", "about") or not names:
            return
        self.selected_keys.update(names)
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

    def _on_reset_keys(self):
        """Remove direct (loose) colors from the selected keys so they revert
        to their zone color or the mode's base color."""
        if not self.selected_keys:
            self._status("Select the keys you want to reset first.")
            return
        km = self._mode().get("keys", {})
        removed = 0
        for k in list(self.selected_keys):
            if km.pop(k, None) is not None:
                removed += 1
        self._render_keyboard()
        self._apply_live()
        self._status(f"Reset {removed} key(s) to base/zone color.")

    # ---------------------------------------------------- zones ---

    def _on_zone_selected(self, row):
        if self._loading or row < 0 or row >= len(self._zones()):
            self.active_zone_idx = None
            return
        self.active_zone_idx = row
        zone = self._zones()[row]
        self.selected_keys = set(zone["keys"])
        self._loading = True
        self._set_picker_hex(zone["color"], apply=False)
        self.bright_slider.setValue(int(zone.get("brightness", 100)))
        self.bright_label.setText(f"{int(zone.get('brightness', 100))}%")
        self._loading = False
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
        self.edit_mode = {0: "zones", 1: "functions"}.get(index, "about")
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
        self._set_picker_hex(self._picker_hex(), apply=True)

    def _on_hex_changed(self):
        if self._loading:
            return
        text = self.hex_field.text().strip().lstrip("#")
        if len(text) == 6:
            self._set_picker_hex(text, apply=True)

    def _on_wheel_changed(self, hex_color):
        if self._loading:
            return
        self._set_picker_hex(hex_color, apply=True)

    def _on_brightness_changed(self, val):
        self.bright_label.setText(f"{val}%")
        if self._loading:
            return
        if self.active_zone_idx is not None:
            self._zones()[self.active_zone_idx]["brightness"] = val
            self._refresh_zone_row(self.active_zone_idx)
            self._apply_live()

    def _apply_picker_to_zone(self):
        """Apply the picker color. If a zone is selected, edit that zone. If
        instead loose keys are selected (no zone), color those keys directly,
        no zone required. Otherwise just hold the color for later."""
        if self.active_zone_idx is not None:
            self._zones()[self.active_zone_idx]["color"] = self._picker_hex()
            self._refresh_zone_row(self.active_zone_idx)
            self._apply_live()
        elif self.selected_keys:
            km = self._mode().setdefault("keys", {})
            hexc = self._picker_hex()
            for k in self.selected_keys:
                km[k] = hexc
            self._render_keyboard()
            self._apply_live()

    def _refresh_zone_row(self, idx):
        z = self._zones()[idx]
        item = self.zone_list.item(idx)
        if item:
            item.setText(f"{z['name']}  ({len(z['keys'])} keys, {z['brightness']}%)")
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
            layout = rc.resolve_mode_layout(self._mode())
            colors = rc.build_color_array(layout, self.led_lookup, len(self.device.leds))
            self.device.set_colors(colors)
            self._status("Applied to keyboard.")
        except Exception as e:
            self._status(f"Failed to apply: {e}")

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
            # stop any existing watcher instances (ignore "no process" result)
            subprocess.run(["pkill", "-f", WATCHER_SCRIPT.name],
                           capture_output=True)
            import time
            time.sleep(0.3)
            log = open("/tmp/rgb_watcher.log", "a")
            subprocess.Popen(
                [sys.executable, str(WATCHER_SCRIPT)],
                stdout=log, stderr=log, start_new_session=True,
            )
            self._status("Watcher (re)started. Check your tray for its icon "
                         "(log: /tmp/rgb_watcher.log).")
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
    win.resize(w, h)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
