#!/usr/bin/env python3
"""
Adds a Baldur's Gate 3 profile to games.json safely.

Run it from the folder that contains games.json:
    python3 add_bg3_profile.py

It backs up games.json to games.json.bak first, then inserts the profile
under your active device using proper JSON parsing (no hand-editing).
Re-running it just overwrites the BG3 profile (and refreshes the backup).
"""
import json
import shutil
import sys
import pathlib

CONFIG = pathlib.Path("games.json")
PROFILE_NAME = "Baldur's Gate 3"
# Set to a device name to force it; None uses the config's active_device.
TARGET_DEVICE = None

BG3_PROFILE = {
    "match": "bg3",
    "active_mode": "Explore",
    "modes": {
        "Explore": {
            "base_color": "140A05",
            "zones": [
                {"name": "Camera",    "keys": ["W", "A", "S", "D", "Q", "E"],                     "color": "FFB000", "brightness": 100},
                {"name": "Hotbar",    "keys": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"], "color": "00E5FF", "brightness": 100},
                {"name": "Party",     "keys": ["F1", "F2", "F3", "F4"],                           "color": "00FF66", "brightness": 100},
                {"name": "Save/Load", "keys": ["F5", "F8"],                                       "color": "FF2A2A", "brightness": 100},
                {"name": "End Turn",  "keys": ["Space"],                                          "color": "FF6A00", "brightness": 100},
                {"name": "Highlight", "keys": ["Left Alt"],                                       "color": "FF00CC", "brightness": 100},
                {"name": "Menus",     "keys": ["I", "M", "J"],                                    "color": "2A6BFF", "brightness": 100},
                {"name": "Sneak",     "keys": ["C"],                                              "color": "9D00FF", "brightness": 100},
                {"name": "Menu",      "keys": ["Escape"],                                         "color": "FFFFFF", "brightness": 100},
            ],
            "keys": {},
        },
        "Combat": {
            "base_color": "1A0000",
            "zones": [
                {"name": "Actions",  "keys": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"], "color": "FF3300", "brightness": 100},
                {"name": "End Turn", "keys": ["Space"],                                          "color": "FFAA00", "brightness": 100},
                {"name": "Party",    "keys": ["F1", "F2", "F3", "F4"],                           "color": "00FF66", "brightness": 100},
            ],
            "keys": {},
        },
    },
    "functions": {
        "Right Alt": {
            "on_press":   {"action": "mode", "target": "Combat"},
            "on_release": {"action": "mode", "target": "Explore"},
        }
    },
}


def main():
    if not CONFIG.exists():
        sys.exit("games.json not found. Run this from the folder that has it.")

    cfg = json.loads(CONFIG.read_text())

    device = TARGET_DEVICE or cfg.get("active_device")
    devices = cfg.get("devices", {})
    if device not in devices:
        sys.exit(f"Device {device!r} not found. Available: {list(devices)}")

    profiles = devices[device].setdefault("profiles", {})
    action = "Replaced" if PROFILE_NAME in profiles else "Added"

    shutil.copy(CONFIG, "games.json.bak")
    profiles[PROFILE_NAME] = BG3_PROFILE
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")

    print(f"{action} '{PROFILE_NAME}' on device '{device}'.")
    print("Backup written to games.json.bak")


if __name__ == "__main__":
    main()
