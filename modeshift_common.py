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

    # loose per-key colors (set directly, not via a zone). key name -> hex.
    keys = mode.get("keys", {}) or {}
    mode["keys"] = {k: v for k, v in keys.items() if isinstance(v, str)}
    return mode


def mode_has_effects(mode: dict) -> bool:
    """True if any zone in the mode carries an animated effect."""
    for z in mode.get("zones", []):
        e = z.get("effect")
        if isinstance(e, dict) and e.get("type") not in (None, "none"):
            return True
    return False


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


def write_watcher_command(profile_name: str, path: Path = None):
    path = path or WATCHER_CMD_PATH
    try:
        path.write_text(json.dumps({"profile": profile_name}))
    except OSError:
        pass


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

    name = device.name
    cfg.setdefault("devices", {})
    if name not in cfg["devices"]:
        cfg["devices"][name] = {"profiles": _profiles_for_new_device(cfg, device)}
    cfg["active_device"] = name
    return device, name


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

def get_active_window() -> tuple[str, int] | None:
    try:
        win_id = subprocess.run(
            ["kdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if not win_id:
            return None
        win_class = subprocess.run(
            ["kdotool", "getwindowclassname", win_id],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        pid_str = subprocess.run(
            ["kdotool", "getwindowpid", win_id],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        pid = int(pid_str) if pid_str.isdigit() else -1
        return (win_class, pid)
    except FileNotFoundError:
        raise RuntimeError(
            "kdotool not found on PATH. Install it (AUR: 'yay -S kdotool') for "
            "active-window detection on KDE Wayland."
        )
    except subprocess.TimeoutExpired:
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
