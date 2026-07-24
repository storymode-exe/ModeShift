#!/usr/bin/env python3
"""
diagnose_zones.py

One-off diagnostic: dumps whatever physical-grid (matrix) data OpenRGB
reports for the configured device's zones. This tells us whether the
upcoming profile-editor GUI can auto-draw your keyboard's layout from real
OpenRGB data (portable to other boards too) or needs a hand-mapped picture
specific to the V6 Ultra 8K.

Usage:
    python3 diagnose_zones.py
"""
import json
import sys
from pathlib import Path

from openrgb import OpenRGBClient
from openrgb.utils import ZoneType

CONFIG_PATH = Path(__file__).parent / "games.json"


def main():
    import rgb_common as rc
    try:
        cfg = rc.load_config()
    except FileNotFoundError:
        cfg = {"openrgb": {"host": "127.0.0.1", "port": 6742},
               "poll_interval_seconds": 1.5, "devices": {}, "active_device": None}
    client = rc.open_client(cfg, client_name="zone-diagnostic")
    try:
        device, _name = rc.select_device(cfg, client)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    print(f"Device: {device.name}")
    print(f"Total LEDs: {len(device.leds)}")
    print(f"Zones: {len(device.zones)}\n")

    for zi, zone in enumerate(device.zones):
        zt = zone.type
        zt_name = ZoneType(zt).name if not isinstance(zt, ZoneType) else zt.name
        print(f"--- Zone {zi}: {zone.name!r}  type={zt_name} ---")

        mat_h = getattr(zone, "mat_height", None)
        mat_w = getattr(zone, "mat_width", None)
        matrix_map = getattr(zone, "matrix_map", None)

        print(f"  mat_height={mat_h}  mat_width={mat_w}")

        if matrix_map:
            print("  matrix_map (row -> [led_index or . for empty]):")
            for row_i, row in enumerate(matrix_map):
                cells = []
                for v in row:
                    if v is None:
                        cells.append("   .")
                    else:
                        cells.append(f"{v:>4}")
                print(f"    row {row_i:>2}: " + " ".join(cells))

            print("\n  Resolved key names per grid cell (row, col -> LED name):")
            for row_i, row in enumerate(matrix_map):
                for col_i, led_idx in enumerate(row):
                    if led_idx is None:
                        continue
                    try:
                        led_name = device.leds[led_idx].name
                    except IndexError:
                        led_name = f"<no LED at global index {led_idx}>"
                    print(f"    ({row_i:>2},{col_i:>2}) -> idx {led_idx:>4}: {led_name}")
        else:
            print("  No matrix_map reported for this zone.")

        print()


if __name__ == "__main__":
    main()
