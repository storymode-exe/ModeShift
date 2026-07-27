"""
modeshift_common.py

Shared config/OpenRGB logic used by both modeshift_watcher.py (the
background/tray automation daemon) and modeshift_editor.py (the GUI).

DATA MODEL (games.json)
-----------------------
{
  "openrgb": { "host", "port", "device_name" },
  "poll_interval_seconds": 1.5,
  "profiles": {
    "Default": {
      "match": "",                 # window-class / process substring; "" = idle fallback
      "active_mode": "Base",       # which mode the watcher applies
      "modes": {
        "Base": {
          "base_color": "2F00FF",  # fills every key not covered by a zone
          "zones": [
            { "name": "WASD", "keys": ["W","A","S","D"],
              "color": "FF4000", "brightness": 100 }
          ]
        }
      }
    },
    "cs2": { "match": "cs2", "active_mode": "Base", "modes": { ... } }
  }
}

A MODE resolves to a full per-key layout: start with base_color everywhere,
then paint each zone's (brightness-scaled) color onto its keys. That layout
is what actually gets pushed to the keyboard.

The old flat schema (default_layout + games) is auto-migrated on load.
"""

import copy
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import psutil
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

try:
    from openrgb.utils import DeviceType as _DeviceType
    _DEVICE_TYPE_KEYBOARD = _DeviceType.KEYBOARD
except Exception:  # enum missing/renamed in some versions -> fall back to matrix test
    _DEVICE_TYPE_KEYBOARD = None

CONFIG_PATH = Path(__file__).parent / "games.json"

_UNSET = object()
_BACKEND = _UNSET          # cached window-detection tool, see detect_backend()

DEFAULT_PROFILE_NAME = "Default"
DEFAULT_MODE_NAME = "Mode 1"


# ------------------------------------------------------------- colors ---

def hex_to_rgbcolor(hex_str: str) -> RGBColor:
    hex_str = hex_str.strip().lstrip("#")
    if len(hex_str) != 6:
        raise ValueError(f"Invalid hex color: {hex_str!r}")
    return RGBColor(int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))


def rgbcolor_to_hex(color: RGBColor) -> str:
    return f"{color.red:02X}{color.green:02X}{color.blue:02X}"


def scale_hex(hex_str: str, brightness: int) -> str:
    """Scales a hex color by brightness (0-100). 100 = unchanged, 0 = black."""
    hex_str = hex_str.strip().lstrip("#")
    r, g, b = int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16)
    f = max(0, min(100, int(brightness))) / 100.0
    return f"{round(r * f):02X}{round(g * f):02X}{round(b * f):02X}"


# ----------------------------------------------------- schema / migration ---

def _clean(d: dict) -> dict:
    """Drop '_comment'-style keys (but keep '_default')."""
    return {k: v for k, v in d.items() if not (k.startswith("_") and k != "_default")}


def _layout_to_zones(layout: dict):
    """Convert an old flat {_default, key: color} layout into
    (base_color, [zone,...]) grouping keys that share a color."""
    base = layout.get("_default", "000000")
    by_color: dict[str, list] = {}
    for k, v in layout.items():
        if k == "_default":
            continue
        by_color.setdefault(str(v).upper(), []).append(k)
    zones = []
    for i, (color, keys) in enumerate(by_color.items(), start=1):
        zones.append({"name": f"Zone {i}", "keys": keys, "color": color, "brightness": 100})
    return base, zones


def _migrate_flat_schema(cfg: dict) -> dict:
    """Convert the legacy default_layout/games schema into profiles/modes/zones."""
    profiles: dict[str, dict] = {}

    default_layout = _clean(cfg.get("default_layout", {"_default": "000000"}))
    base, zones = _layout_to_zones(default_layout)
    profiles[DEFAULT_PROFILE_NAME] = {
        "match": "",
        "active_mode": DEFAULT_MODE_NAME,
        "modes": {DEFAULT_MODE_NAME: {"base_color": base, "zones": zones}},
    }

    for name, layout in _clean(cfg.get("games", {})).items():
        if not isinstance(layout, dict):
            continue
        base, zones = _layout_to_zones(_clean(layout))
        profiles[name] = {
            "match": name,
            "active_mode": DEFAULT_MODE_NAME,
            "modes": {DEFAULT_MODE_NAME: {"base_color": base, "zones": zones}},
        }

    cfg.pop("default_layout", None)
    cfg.pop("games", None)
    cfg["profiles"] = profiles
    return cfg


_EFFECT_TYPES = ("reactive", "breathing", "blinking", "colorcycle", "twinkle")
_DEFAULT_EFFECT_COLOR = {"reactive": "FF3300", "breathing": "2F00FF",
                        "blinking": "FF0000", "twinkle": "FFFFFF"}


def _normalize_effect(eff):
    """Validate/fill a zone's effect dict, or None if there isn't a valid one.
    Every effect now uses a unified 'colors' list (1-8); a legacy single
    'color' is migrated into it. colorcycle may have an empty list (rainbow)."""
    if not isinstance(eff, dict):
        return None
    t = eff.get("type")
    if t not in _EFFECT_TYPES:
        return None
    cols = [c for c in eff.get("colors", []) if isinstance(c, str)]
    if not cols and isinstance(eff.get("color"), str):   # migrate old single color
        cols = [eff["color"]]
    cols = cols[:8]
    if not cols and t != "colorcycle":
        cols = [_DEFAULT_EFFECT_COLOR[t]]
    out = {"type": t, "colors": cols}
    if t == "reactive":
        out.update({"peak_brightness": int(eff.get("peak_brightness", 100)),
                    "fade_seconds": float(eff.get("fade_seconds", 0.6))})
    elif t == "breathing":
        out.update({"period_seconds": float(eff.get("period_seconds", 3.0)),
                    "min_brightness": int(eff.get("min_brightness", 0)),
                    "max_brightness": int(eff.get("max_brightness", 100))})
    elif t == "blinking":
        out.update({"on_seconds": float(eff.get("on_seconds", 0.4)),
                    "off_seconds": float(eff.get("off_seconds", 0.4))})
    elif t == "colorcycle":
        out.update({"rainbow": bool(eff.get("rainbow", True)),
                    "period_seconds": float(eff.get("period_seconds", 5.0))})
    elif t == "twinkle":
        out.update({"rainbow": bool(eff.get("rainbow", False)),
                    "density": float(eff.get("density", 0.3)),
                    "fade_seconds": float(eff.get("fade_seconds", 1.0))})
    return out


_KEY_STATE_TYPES = ("cooldown", "toggle")
_READY_SIGNALS = ("solid", "blink", "breathe")


def _normalize_key_state(ks):
    """Validate one key-state indicator (cooldown or toggle), or None.

    cooldown: press starts a timer. The key shows active_color, optionally
    stepping through `stages` colors as it runs down, then signals ready.
    toggle:   press flips through `colors` and stays there.

    Both track YOUR KEYPRESSES only, not the game's real state, so they can
    drift. Pressing the key again re-syncs a toggle; the next press re-syncs a
    cooldown."""
    if not isinstance(ks, dict):
        return None
    t = ks.get("type")
    if t not in _KEY_STATE_TYPES:
        return None
    if t == "cooldown":
        stages = [c for c in ks.get("stages", []) if isinstance(c, str)][:4]
        sig = ks.get("ready_signal", "solid")
        return {
            "type": "cooldown",
            "duration_seconds": max(0.1, float(ks.get("duration_seconds", 30.0))),
            "active_color": ks.get("active_color", "FF2A00"),
            # smoothly blend the on-cooldown colour toward the ready colour as
            # the timer runs down, so the key itself reads as a progress bar
            "countdown_fade": bool(ks.get("countdown_fade", True)),
            "stages": stages,
            "ready_color": ks.get("ready_color", "00FF66"),
            "ready_signal": sig if sig in _READY_SIGNALS else "solid",
            "ready_seconds": max(0.0, float(ks.get("ready_seconds", 2.0))),
            # when not counting down the key sits at its ready color, so an
            # ability that's available is always visibly "ready" instead of
            # falling back to the zone colour underneath
            "idle_color": ks.get("idle_color") or ks.get("ready_color", "00FF66"),
        }
    colors = [c for c in ks.get("colors", []) if isinstance(c, str)][:8]
    if len(colors) < 2:
        colors = (colors or ["00FF66"]) + ["202020"]
    return {"type": "toggle", "colors": colors,
            "start_index": max(0, min(len(colors) - 1, int(ks.get("start_index", 0))))}


def _normalize_mode(mode: dict) -> dict:
    mode = _clean(mode)
    mode.setdefault("base_color", "000000")
    clean_zones = []
    for z in mode.get("zones", []) or []:
        if not isinstance(z, dict):
            continue
        cz = {
            "name": z.get("name", "Zone"),
            "keys": list(z.get("keys", [])),
            "color": z.get("color", "FFFFFF"),
            "brightness": int(z.get("brightness", 100)),
        }
        if isinstance(z.get("effect"), dict):
            cz["effect"] = z["effect"]          # normalized below
        if z.get("reactive"):
            cz["_legacy_reactive"] = True       # carried only for migration
        clean_zones.append(cz)

    # migrate legacy mode-level reactive block -> per-zone reactive effects
    legacy = mode.get("reactive")
    if isinstance(legacy, dict) and legacy.get("enabled"):
        params = {
            "type": "reactive",
            "colors": [c for c in legacy.get("colors", []) if isinstance(c, str)] or ["FF3300"],
            "peak_brightness": int(legacy.get("peak_brightness", 100)),
            "fade_seconds": float(legacy.get("fade_seconds", 0.6)),
            "alternate": bool(legacy.get("alternate", False)),
        }
        scope = legacy.get("scope", "zones")
        for cz in clean_zones:
            if "effect" not in cz and (scope == "all" or cz.get("_legacy_reactive")):
                cz["effect"] = dict(params)
    mode.pop("reactive", None)

    for cz in clean_zones:
        cz.pop("_legacy_reactive", None)
        eff = _normalize_effect(cz.get("effect"))
        if eff:
            cz["effect"] = eff
        else:
            cz.pop("effect", None)
    mode["zones"] = clean_zones

    # Older configs could colour keys directly, outside any zone. Everything is
    # a zone now, so migrate those loose colours into zones (grouped by colour)
    # and put them on top, which is the precedence they used to have.
    loose = {k: v for k, v in (mode.get("keys", {}) or {}).items()
             if isinstance(v, str)}
    if loose:
        by_color: dict[str, list] = {}
        for key, hexc in loose.items():
            by_color.setdefault(hexc.upper(), []).append(key)
        migrated = [{"name": f"Keys {i}" if len(by_color) > 1 else "Keys",
                     "keys": sorted(keys_), "color": color, "brightness": 100}
                    for i, (color, keys_) in enumerate(by_color.items(), start=1)]
        clean_zones = migrated + clean_zones
        mode["zones"] = clean_zones
    mode["keys"] = {}

    # per-key state indicators (cooldown / toggle), per mode
    states = {}
    for key, ks in (mode.get("key_states", {}) or {}).items():
        n = _normalize_key_state(ks)
        if n is not None:
            states[key] = n
    if states:
        mode["key_states"] = states
    else:
        mode.pop("key_states", None)
    return mode


def mode_has_effects(mode: dict) -> bool:
    """True if the mode needs the animated render loop (any zone effect, or
    any key-state indicator)."""
    for z in mode.get("zones", []):
        e = z.get("effect")
        if isinstance(e, dict) and e.get("type") not in (None, "none"):
            return True
    return bool(mode.get("key_states"))


def _normalize_binding(b):
    """A single key binding: {on_press, on_release}, each None or
    {action: 'mode'|'profile', target: <name>}."""
    if not isinstance(b, dict):
        return None

    def event(e):
        if not isinstance(e, dict):
            return None
        action = e.get("action")
        target = e.get("target")
        if action not in ("mode", "profile") or not target:
            return None
        return {"action": action, "target": target}

    on_press = event(b.get("on_press"))
    on_release = event(b.get("on_release"))
    if on_press is None and on_release is None:
        return None
    return {"on_press": on_press, "on_release": on_release}


def _normalize_profile(prof: dict) -> dict:
    prof = _clean(prof)
    prof.setdefault("match", "")
    modes = _clean(prof.get("modes", {}) or {})
    if not modes:
        modes = {DEFAULT_MODE_NAME: {"base_color": "000000", "zones": []}}
    prof["modes"] = {name: _normalize_mode(m) for name, m in modes.items()}
    active = prof.get("active_mode")
    if active not in prof["modes"]:
        active = next(iter(prof["modes"]))
    prof["active_mode"] = active

    # per-key function bindings (Change Mode / Change Profile on press/release)
    functions = {}
    for key, binding in _clean(prof.get("functions", {}) or {}).items():
        nb = _normalize_binding(binding)
        if nb is not None:
            functions[key] = nb
    prof["functions"] = functions

    return prof


def _empty_profiles() -> dict:
    return {
        DEFAULT_PROFILE_NAME: {
            "match": "", "active_mode": DEFAULT_MODE_NAME,
            "modes": {DEFAULT_MODE_NAME: {"base_color": "000000", "zones": []}},
            "functions": {},
        }
    }


def _normalize_profiles(profiles: dict) -> dict:
    profiles = _clean(profiles or {})
    if DEFAULT_PROFILE_NAME not in profiles:
        profiles = {**_empty_profiles(), **profiles}
    return {name: _normalize_profile(p) for name, p in profiles.items()}


def _normalize_device(dev: dict) -> dict:
    dev = _clean(dev or {})
    dev["profiles"] = _normalize_profiles(dev.get("profiles", {}))
    return dev


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r") as f:
        cfg = json.load(f)

    cfg.setdefault("openrgb", {})
    cfg["openrgb"].setdefault("host", "127.0.0.1")
    cfg["openrgb"].setdefault("port", 6742)
    cfg.setdefault("poll_interval_seconds", 1.5)

    # ---- migrate older schemas into the auto-detect 'devices' schema ----
    if "devices" not in cfg:
        legacy_name = cfg.get("openrgb", {}).get("device_name") or cfg.get("active_device")
        if "profiles" not in cfg and ("default_layout" in cfg or "games" in cfg):
            cfg = _migrate_flat_schema(cfg)  # oldest flat schema -> cfg['profiles']
        profiles = cfg.pop("profiles", None)
        cfg["devices"] = {}
        if profiles is not None:
            name = legacy_name or "Keyboard"
            cfg["devices"][name] = {"profiles": profiles}
            cfg.setdefault("active_device", name)

    cfg["devices"] = {name: _normalize_device(d) for name, d in _clean(cfg.get("devices", {})).items()}
    if cfg.get("active_device") not in cfg["devices"]:
        cfg["active_device"] = next(iter(cfg["devices"]), None)
    cfg["openrgb"].pop("device_name", None)  # no longer used; auto-detected now

    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH):
    out = {
        "openrgb": {"host": cfg["openrgb"]["host"], "port": cfg["openrgb"]["port"]},
        "poll_interval_seconds": cfg["poll_interval_seconds"],
        "active_device": cfg.get("active_device"),
        "devices": cfg["devices"],
    }
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")


def active_profiles(cfg: dict) -> dict:
    """The profiles dict for the currently-selected device."""
    name = cfg.get("active_device")
    return cfg["devices"][name]["profiles"]


# ------------------------------------------- editor -> watcher command ---
# The editor and the watcher are separate processes. When you hit "Apply Now"
# in the editor it drops a tiny command file here; the running watcher notices
# it on its next poll, reloads games.json, and switches to that profile so its
# colors and key functions match what you're editing.

WATCHER_CMD_PATH = CONFIG_PATH.parent / "watcher_command.json"
WATCHER_CMD_AUTO = "__auto__"
WATCHER_PID_PATH = CONFIG_PATH.parent / "watcher.pid"


def write_watcher_pid(path: Path = None):
    """The running watcher records its PID so the editor can stop exactly that
    process, instead of pattern-matching process names per platform."""
    path = path or WATCHER_PID_PATH
    try:
        path.write_text(str(os.getpid()))
    except OSError:
        pass


def clear_watcher_pid(path: Path = None):
    path = path or WATCHER_PID_PATH
    try:
        (path).unlink(missing_ok=True)
    except OSError:
        pass


def read_watcher_pid(path: Path = None):
    path = path or WATCHER_PID_PATH
    try:
        pid = int(path.read_text().strip())
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def stop_running_watcher(path: Path = None) -> bool:
    """Stop the watcher recorded in the PID file. True if we asked one to stop."""
    pid = read_watcher_pid(path)
    if pid is None:
        return False
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=5)
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        clear_watcher_pid(path)
    return True


WATCHER_CMD_PAUSE = "__pause__"
WATCHER_CMD_RESUME = "__resume__"


def write_watcher_command(profile_name: str, path: Path = None):
    path = path or WATCHER_CMD_PATH
    try:
        path.write_text(json.dumps({"profile": profile_name}))
    except OSError:
        pass


def write_watcher_pause(paused: bool, path: Path = None):
    """Ask a running watcher to stop or resume driving the keyboard."""
    write_watcher_command(WATCHER_CMD_PAUSE if paused else WATCHER_CMD_RESUME, path)


def write_watcher_preview(profile_name: str, mode: dict, path: Path = None):
    """Hand the editor's in-progress mode to the watcher to render.

    The watcher owns the keyboard and is the only process reading key presses,
    so previewing through it keeps type lighting, key functions and key states
    working while you edit, instead of the two fighting over the LEDs."""
    path = path or WATCHER_CMD_PATH
    try:
        path.write_text(json.dumps(
            {"preview": {"profile": profile_name, "mode": mode}}))
    except OSError:
        pass


def write_watcher_preview_off(path: Path = None):
    """Stop previewing and go back to following the focused window."""
    write_watcher_command(WATCHER_CMD_AUTO, path)


def read_watcher_command(path: Path = None):
    path = path or WATCHER_CMD_PATH
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


# ------------------------------------------- key name -> evdev code name ---
# Maps our LED/key shorthand names (as shown by --list-leds) to Linux evdev
# ecode NAMES. The watcher resolves these to numeric codes at runtime via
# evdev.ecodes, so this module doesn't need python-evdev installed to import.

_EVDEV_SPECIAL = {
    "Escape": "KEY_ESC",
    "Print Screen": "KEY_SYSRQ", "Scroll Lock": "KEY_SCROLLLOCK",
    "Pause/Break": "KEY_PAUSE",
    "Media Mute": "KEY_MUTE", "Media Play/Pause": "KEY_PLAYPAUSE",
    "Media Previous": "KEY_PREVIOUSSONG", "Media Next": "KEY_NEXTSONG",
    "`": "KEY_GRAVE", "-": "KEY_MINUS", "=": "KEY_EQUAL",
    "Backspace": "KEY_BACKSPACE", "Insert": "KEY_INSERT", "Home": "KEY_HOME",
    "Page Up": "KEY_PAGEUP", "Page Down": "KEY_PAGEDOWN",
    "Delete": "KEY_DELETE", "End": "KEY_END",
    "Num Lock": "KEY_NUMLOCK",
    "Number Pad /": "KEY_KPSLASH", "Number Pad *": "KEY_KPASTERISK",
    "Number Pad -": "KEY_KPMINUS", "Number Pad +": "KEY_KPPLUS",
    "Number Pad Enter": "KEY_KPENTER", "Number Pad .": "KEY_KPDOT",
    "Tab": "KEY_TAB", "[": "KEY_LEFTBRACE", "]": "KEY_RIGHTBRACE",
    "\\ (ANSI)": "KEY_BACKSLASH", "Caps Lock": "KEY_CAPSLOCK",
    ";": "KEY_SEMICOLON", "'": "KEY_APOSTROPHE", "Enter": "KEY_ENTER",
    "Left Shift": "KEY_LEFTSHIFT", "Right Shift": "KEY_RIGHTSHIFT",
    ",": "KEY_COMMA", ".": "KEY_DOT", "/": "KEY_SLASH",
    "Up Arrow": "KEY_UP", "Down Arrow": "KEY_DOWN",
    "Left Arrow": "KEY_LEFT", "Right Arrow": "KEY_RIGHT",
    "Left Control": "KEY_LEFTCTRL", "Right Control": "KEY_RIGHTCTRL",
    "Left Alt": "KEY_LEFTALT", "Right Alt": "KEY_RIGHTALT",
    "Left Windows": "KEY_LEFTMETA", "Right Windows": "KEY_RIGHTMETA",
    "Menu": "KEY_COMPOSE", "Space": "KEY_SPACE",
}


# ------------------------------- Windows virtual-key codes -> our key names ---
# Only the codes our LED names use. Left/right modifiers and the numpad are
# distinguished so Left Control and Right Alt bind separately, as on Linux.
_VK_TO_KEY = {
    0x1B: "Escape", 0x08: "Backspace", 0x09: "Tab", 0x0D: "Enter",
    0x20: "Space", 0x14: "Caps Lock", 0x90: "Num Lock", 0x91: "Scroll Lock",
    0x2C: "Print Screen", 0x13: "Pause/Break", 0x2D: "Insert", 0x2E: "Delete",
    0x24: "Home", 0x23: "End", 0x21: "Page Up", 0x22: "Page Down",
    0x25: "Left Arrow", 0x26: "Up Arrow", 0x27: "Right Arrow", 0x28: "Down Arrow",
    0xA0: "Left Shift", 0xA1: "Right Shift",
    0xA2: "Left Control", 0xA3: "Right Control",
    0xA4: "Left Alt", 0xA5: "Right Alt",
    0x5B: "Left Windows", 0x5C: "Right Windows", 0x5D: "Menu",
    # punctuation (US layout)
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\ (ANSI)", 0xDD: "]", 0xDE: "'",
    # numpad
    0x6A: "Number Pad *", 0x6B: "Number Pad +", 0x6D: "Number Pad -",
    0x6E: "Number Pad .", 0x6F: "Number Pad /",
    # media
    0xAD: "Media Mute", 0xB3: "Media Play/Pause",
    0xB0: "Media Next", 0xB1: "Media Previous",
}


def win_vk_to_key_name(vk: int) -> str | None:
    """Windows virtual-key code -> the key name used in games.json, or None."""
    if vk in _VK_TO_KEY:
        return _VK_TO_KEY[vk]
    if 0x30 <= vk <= 0x39:                 # 0-9
        return chr(vk)
    if 0x41 <= vk <= 0x5A:                 # A-Z
        return chr(vk)
    if 0x60 <= vk <= 0x69:                 # numpad 0-9
        return f"Number Pad {vk - 0x60}"
    if 0x70 <= vk <= 0x7B:                 # F1-F12
        return f"F{vk - 0x6F}"
    return None


def key_name_to_ecode_name(key_name: str):
    """Returns the evdev ecode name (e.g. 'KEY_LEFTCTRL') for one of our key
    names, or None if unmapped."""
    if key_name in _EVDEV_SPECIAL:
        return _EVDEV_SPECIAL[key_name]
    if len(key_name) == 1 and key_name.isalpha():
        return f"KEY_{key_name.upper()}"
    if len(key_name) == 1 and key_name.isdigit():
        return f"KEY_{key_name}"
    if key_name.startswith("F") and key_name[1:].isdigit():
        return f"KEY_{key_name.upper()}"          # F1..F12
    if key_name.startswith("Number Pad ") and key_name[-1].isdigit():
        return f"KEY_KP{key_name[-1]}"            # Number Pad 0..9
    return None


# --------------------------------------------------- mode/profile resolution ---

def get_active_mode(profile: dict) -> dict:
    modes = profile.get("modes", {})
    name = profile.get("active_mode")
    if name and name in modes:
        return modes[name]
    if modes:
        return next(iter(modes.values()))
    return {"base_color": "000000", "zones": []}


def resolve_mode_layout(mode: dict) -> dict:
    """Flatten a mode into {_default, key_name: hex}. Precedence (later wins):
    base color -> zones -> loose per-key colors."""
    layout = {"_default": mode.get("base_color", "000000")}
    # Zones are a layer stack: the FIRST zone in the list is the TOP layer and
    # wins where zones overlap, so paint from the bottom of the stack upward.
    for zone in reversed(mode.get("zones", [])):
        raw = zone.get("color", "")
        if not raw:
            continue  # transparent zone: no static fill, only its effect shows
        color = scale_hex(raw, zone.get("brightness", 100))
        for key in zone.get("keys", []):
            layout[key] = color
    # loose per-key colors are the most specific -> applied last
    for key, hexc in mode.get("keys", {}).items():
        layout[key] = hexc
    return layout


# Window classes that many unrelated apps share, so they are useless for
# identifying a game. Electron and Chromium apps all report Chrome_WidgetWin_1,
# every Firefox window is MozillaWindowClass, and so on.
_GENERIC_CLASSES = {
    "chrome_widgetwin_0", "chrome_widgetwin_1", "mozillawindowclass",
    "qwidget", "qt5152qwindowicon", "qt5qwindowicon", "qt6qwindowicon",
    "sdl_app", "glfw30", "unrealwindow", "unitywndclass", "wine",
    "progman", "workerw", "cabinetwclass", "applicationframewindow",
    "windowsforms10.window.8.app.0.141b42a_r6_ad1", "tkwindow",
}


def best_match_string(window_class: str, process_name: str) -> str:
    """Pick the more identifying of a window class and a process name.

    On Windows the class is usually a generic toolkit name shared by many apps,
    so the process name is what actually identifies a game. On Linux the class
    is normally specific and preferred."""
    cls = (window_class or "").strip().lower()
    proc = (process_name or "").strip().lower()
    if proc.endswith(".exe"):
        proc = proc[:-4]
    if not cls or cls in _GENERIC_CLASSES:
        return proc or cls
    return cls


def resolve_profile_name(window_class: str, pid: int, profiles: dict) -> str:
    """Returns the matching profile name, falling back to DEFAULT_PROFILE_NAME."""
    wc = (window_class or "").lower()
    pn = process_name_for_pid(pid)
    for name, prof in profiles.items():
        m = (prof.get("match") or "").strip().lower()
        if not m:
            continue
        if m in wc or m in pn:
            return name
    return DEFAULT_PROFILE_NAME


# ----------------------------------------------------------- OpenRGB ---

def open_client(cfg: dict, client_name: str = "rgb-tool"):
    ocfg = cfg["openrgb"]
    return OpenRGBClient(address=ocfg["host"], port=ocfg["port"], name=client_name)


def is_keyboard(device) -> bool:
    """True if OpenRGB reports this device as a keyboard. Falls back to
    'has a per-key matrix zone' only if the type enum isn't available, since
    some non-keyboards (GPUs, boards) also expose matrix zones."""
    if _DEVICE_TYPE_KEYBOARD is not None:
        try:
            return device.type == _DEVICE_TYPE_KEYBOARD
        except Exception:
            pass
    return find_matrix_zone(device) is not None


def candidate_devices(client) -> list:
    """Every device ModeShift could drive: anything with a per-key matrix.
    Used by the editor's device picker, so a wrong auto-pick can be corrected."""
    return [d for d in client.devices if find_matrix_zone(d) is not None]


def list_keyboards(client) -> list:
    """Connected keyboards, preferring ones with a per-key matrix (what the
    editor needs to draw), and never returning non-keyboard devices like the
    GPU just because they happen to expose a matrix zone."""
    typed = [d for d in client.devices if is_keyboard(d)]
    per_key = [d for d in typed if find_matrix_zone(d) is not None]
    if per_key:
        return per_key
    if typed:
        return typed
    # last resort: nothing typed as a keyboard, fall back to matrix devices
    return [d for d in client.devices if find_matrix_zone(d) is not None]


def device_key_names(device) -> set:
    """The set of (shorthand) key names this device exposes."""
    return {led_shorthand(led) for led in device.leds}


def carry_over_profiles(source_profiles: dict, valid_keys: set) -> dict:
    """Copy profiles from one keyboard to another, keeping only the keys that
    exist on the target device (best-effort). Base colors, mode/profile names,
    match strings and active-mode all carry over; keys the new board doesn't
    have are dropped from zones and functions."""
    valid_lower = {k.lower() for k in valid_keys}
    out = {}
    for pname, prof in source_profiles.items():
        p = copy.deepcopy(prof)
        p["functions"] = {k: v for k, v in p.get("functions", {}).items()
                          if k.lower() in valid_lower}
        for mode in p.get("modes", {}).values():
            for zone in mode.get("zones", []):
                zone["keys"] = [k for k in zone.get("keys", []) if k.lower() in valid_lower]
            mode["keys"] = {k: v for k, v in mode.get("keys", {}).items()
                            if k.lower() in valid_lower}
        out[pname] = p
    return out


def _profiles_for_new_device(cfg: dict, device) -> dict:
    """Build the initial profiles for a keyboard we've never seen: carry over
    from the first existing device that has profiles, else start fresh."""
    valid = device_key_names(device)
    for existing in cfg.get("devices", {}).values():
        if existing.get("profiles"):
            return _normalize_profiles(carry_over_profiles(existing["profiles"], valid))
    return _empty_profiles()


def select_device(cfg: dict, client, preferred: str = None):
    """Auto-detect the keyboard to use. Prefers `preferred`, else the config's
    last active_device, else the first keyboard OpenRGB reports. Ensures the
    config has an entry for it (creating one via carry-over if it's new).
    Returns (device, device_name). Mutates cfg."""
    keyboards = list_keyboards(client)
    if not keyboards:
        available = ", ".join(d.name for d in client.devices) or "(none found)"
        raise RuntimeError(
            f"No per-key keyboard found in OpenRGB. Devices available: {available}"
        )

    want = (preferred or cfg.get("active_device") or "").lower()
    device = None
    if want:
        for d in keyboards:
            if want in d.name.lower():
                device = d
                break
    if device is None:
        device = keyboards[0]

    ensure_direct_mode(device)      # otherwise set_colors silently does nothing

    name = device.name
    cfg.setdefault("devices", {})
    if name not in cfg["devices"]:
        cfg["devices"][name] = {"profiles": _profiles_for_new_device(cfg, device)}
    cfg["active_device"] = name
    return device, name


def ensure_direct_mode(device) -> bool:
    """Put the device into Direct mode so live per-LED colours actually apply.

    In a hardware/firmware mode OpenRGB ignores set_colors, which looks like
    ModeShift doing nothing at all. Harmless if the device is already Direct or
    has no such mode."""
    try:
        current = getattr(getattr(device, "active_mode", None), "name", "") or ""
        if current.lower() == "direct":
            return True
    except Exception:
        pass
    for name in ("Direct", "direct"):
        try:
            device.set_mode(name)
            return True
        except Exception:
            continue
    return False


def led_shorthand(led) -> str:
    """'Key: Number Pad 0' -> 'Number Pad 0'."""
    return led.name.split(":", 1)[1].strip() if ":" in led.name else led.name


def build_led_lookup(device) -> dict:
    lookup = {}
    for i, led in enumerate(device.leds):
        lookup.setdefault(led.name.lower(), i)
        lookup.setdefault(led_shorthand(led).lower(), i)
    return lookup


def build_color_array(layout: dict, led_lookup: dict, num_leds: int) -> list:
    base_color = hex_to_rgbcolor(layout.get("_default", "000000"))
    colors = [base_color for _ in range(num_leds)]
    for key_name, hex_color in layout.items():
        if key_name == "_default":
            continue
        idx = led_lookup.get(key_name.lower())
        if idx is None:
            print(f"[modeshift_common] warning: no LED named {key_name!r} on this device",
                  file=sys.stderr)
            continue
        colors[idx] = hex_to_rgbcolor(hex_color)
    return colors


def find_matrix_zone(device):
    for zone in device.zones:
        if getattr(zone, "matrix_map", None):
            return zone
    return None


# ------------------------------------------------- active window (KDE/Wayland) ---

def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def detect_backend() -> str | None:
    """Which mechanism to ask for the focused window.

    'win32' on Windows, 'kdotool' on KDE Wayland, 'xprop' on X11 (including
    XWayland sessions that expose DISPLAY), or None if nothing is available.
    Cached after the first call."""
    global _BACKEND
    if _BACKEND is not _UNSET:
        return _BACKEND
    if sys.platform.startswith("win"):
        _BACKEND = "win32"          # built into the OS, nothing to install
        return _BACKEND
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland = session == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))
    x11 = session == "x11" or bool(os.environ.get("DISPLAY"))
    order = ["kdotool", "xprop"] if wayland else ["xprop", "kdotool"]
    if not wayland and not x11:
        order = ["xprop", "kdotool"]
    _BACKEND = next((c for c in order if _have(c)), None)
    return _BACKEND


def _run(args, timeout=2) -> str:
    return subprocess.run(args, capture_output=True, text=True,
                          timeout=timeout).stdout.strip()


def parse_xprop_active_window(text: str) -> str | None:
    """'_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3c00007, 0x0' -> '0x3c00007'."""
    m = re.search(r"#\s*(0x[0-9a-fA-F]+)", text or "")
    win = m.group(1) if m else None
    return None if win in (None, "0x0") else win


def parse_xprop_class(text: str) -> str:
    """'WM_CLASS(STRING) = "instance", "Class"' -> 'instance Class'.

    Both parts are returned because games vary in which one carries the useful
    name, and matching is a lowercase substring test either way."""
    parts = re.findall(r'"([^"]*)"', text or "")
    return " ".join(p for p in parts if p)


def parse_xprop_pid(text: str) -> int:
    m = re.search(r"=\s*(\d+)", text or "")
    return int(m.group(1)) if m else -1


def _win32_active_window() -> tuple[str, int] | None:
    """Focused window on Windows, via user32. The Win32 class name is often
    generic (for example 'UnrealWindow'), so the process name usually does the
    matching; both are checked by resolve_profile_name."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return (buf.value or "", int(pid.value) or -1)


def get_active_window() -> tuple[str, int] | None:
    """(window class, pid) for the focused window, or None if it can't be read."""
    backend = detect_backend()
    if backend == "win32":
        try:
            return _win32_active_window()
        except Exception:
            return None
    if backend is None:
        raise RuntimeError(
            "No window-detection tool found. Install kdotool for KDE Wayland "
            "(AUR: 'paru -S kdotool'), or xprop for X11 (package 'xorg-xprop' "
            "on Arch, 'x11-utils' on Debian/Ubuntu). Without one of these, "
            "automatic game detection is off; you can still pick profiles from "
            "the tray."
        )
    try:
        if backend == "kdotool":
            win_id = _run(["kdotool", "getactivewindow"])
            if not win_id:
                return None
            win_class = _run(["kdotool", "getwindowclassname", win_id])
            pid_str = _run(["kdotool", "getwindowpid", win_id])
            return (win_class, int(pid_str) if pid_str.isdigit() else -1)

        # X11 via xprop only: no extra dependency beyond the standard x11 utils
        win_id = parse_xprop_active_window(
            _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"]))
        if not win_id:
            return None
        info = _run(["xprop", "-id", win_id, "WM_CLASS", "_NET_WM_PID"])
        cls, pid = "", -1
        for line in info.splitlines():
            if line.startswith("WM_CLASS"):
                cls = parse_xprop_class(line)
            elif line.startswith("_NET_WM_PID"):
                pid = parse_xprop_pid(line)
        return (cls, pid)
    except (subprocess.TimeoutExpired, ValueError):
        return None
    except FileNotFoundError:
        global _BACKEND
        _BACKEND = _UNSET          # tool vanished; re-detect next time
        return None


def process_name_for_pid(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        proc = psutil.Process(pid)
        name = proc.name().lower()
        generic_wrappers = {"wine64-preloader", "wine-preloader", "wine64", "wine", "steam"}
        depth = 0
        while name in generic_wrappers and depth < 5:
            proc = proc.parent()
            if proc is None:
                break
            name = proc.name().lower()
            depth += 1
        return name
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""
