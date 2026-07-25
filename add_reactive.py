#!/usr/bin/env python3
"""
add_reactive.py  --  add a reactive "type lighting" effect to a mode in games.json.

Run from the folder that has games.json:
    python3 add_reactive.py

Edit the settings just below, then run. It backs up games.json to
games.json.bak first, then writes the reactive block into the target mode
using proper JSON parsing (no hand-editing). Re-run any time to change it.
"""
import json
import shutil
import sys
import pathlib

# ----------------------------------------------------------------- SETTINGS ---
PROFILE = "Default"     # which profile to add it to (a name from your games.json)
MODE = None             # a mode name, or None to use that profile's active mode

REACTIVE = {
    "enabled": True,
    "scope": "all",         # "all" = every key reacts; "zones" = only zones flagged reactive
    "colors": ["FF3300"],   # one hex, or several to cycle when "alternate" is true
    "peak_brightness": 100, # 0-100: how bright a key starts when pressed
    "fade_seconds": 0.6,    # time to fade from peak down to the layer underneath
    "alternate": False,     # cycle through "colors" on each keypress
    "keys": [],             # explicit key list; overrides scope. [] = use scope
}

SET_BASE_DARK = True    # also set the mode's base_color to 000000 (the "lava" look)
# -----------------------------------------------------------------------------

CONFIG = pathlib.Path("games.json")


def main():
    if not CONFIG.exists():
        sys.exit("games.json not found. Run this from the folder that has it.")

    cfg = json.loads(CONFIG.read_text())
    device = cfg.get("active_device")
    devices = cfg.get("devices", {})
    if device not in devices:
        sys.exit(f"Active device {device!r} not found. Available: {list(devices)}")

    profiles = devices[device]["profiles"]
    if PROFILE not in profiles:
        sys.exit(f"Profile {PROFILE!r} not found. Available: {list(profiles)}")
    prof = profiles[PROFILE]

    mode_name = MODE or prof.get("active_mode")
    modes = prof.get("modes", {})
    if mode_name not in modes:
        sys.exit(f"Mode {mode_name!r} not found in {PROFILE!r}. Available: {list(modes)}")
    mode = modes[mode_name]

    shutil.copy(CONFIG, "games.json.bak")
    mode["reactive"] = dict(REACTIVE)
    if SET_BASE_DARK:
        mode["base_color"] = "000000"
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")

    print(f"Added reactive effect to '{PROFILE}' / '{mode_name}' on device '{device}'.")
    if SET_BASE_DARK:
        print("Set that mode's base_color to 000000 for the dark 'lava' look.")
    print("Backup written to games.json.bak")


if __name__ == "__main__":
    main()
