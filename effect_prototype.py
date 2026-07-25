#!/usr/bin/env python3
"""
effect_prototype.py  --  reactive "type lighting" spike for ModeShift.

Purpose: find out whether your keyboard (through OpenRGB + naaraxi's plugin)
can take rapid per-frame color updates smoothly, before we build effects into
ModeShift for real.

What it does: every key you press lights up at CFG['color'] and PEAK brightness,
then fades to 0% (off) over CFG['fade_seconds']. Set several colors to have it
cycle a new color on each keypress. It prints the achieved framerate and the
average time each push to the board takes, so we can read the performance.

Run it from your repo folder (next to modeshift_common.py), with OpenRGB's SDK
server running and your user in the `input` group:

    python3 effect_prototype.py

Ctrl+C to quit (it turns the board off on exit). Tweak CFG below and re-run.
"""
import sys
import time
import threading
import selectors

import modeshift_common as rc  # your renamed shared module
from openrgb.utils import RGBColor

# ------------------------------------------------------------------ CONFIG ---
CFG = {
    "colors": ["FF0000"],   # one hex color, or several to alternate on each press
    "peak_brightness": 100, # 0-100: how bright a key starts when pressed
    "fade_seconds": 0.6,    # time to fade from peak down to off
    "fps": 60,              # target frames per second (try 30 and 60 to compare)
    "base_color": "000000", # resting color under the effect (000000 = off)
    "trigger_on_repeat": False,  # also re-trigger while a key is held down
}
# -----------------------------------------------------------------------------


def blend(base: RGBColor, top: RGBColor, a: float) -> RGBColor:
    """Alpha-blend `top` over `base` with opacity a (0..1)."""
    return RGBColor(
        int(base.red   + (top.red   - base.red)   * a),
        int(base.green + (top.green - base.green) * a),
        int(base.blue  + (top.blue  - base.blue)  * a),
    )


def main():
    try:
        import evdev
        from evdev import InputDevice, ecodes
    except ImportError:
        sys.exit("python-evdev not installed. Install it (and be in the `input` group).")

    cfg = rc.load_config()
    client = rc.open_client(cfg, client_name="modeshift-effect-proto")
    device, name = rc.select_device(cfg, client)
    num = len(device.leds)
    print(f"Device: {name}  ({num} LEDs)")

    # try to put the board in Direct mode so per-frame updates apply live
    try:
        device.set_mode("Direct")
    except Exception:
        try:
            device.set_mode("direct")
        except Exception:
            pass

    # evdev key code -> LED index, built from your shared key-name mapping
    code_to_led = {}
    for idx, led in enumerate(device.leds):
        ecode_name = rc.key_name_to_ecode_name(rc.led_shorthand(led))
        if ecode_name:
            code = getattr(ecodes, ecode_name, None)
            if code is not None:
                code_to_led[code] = idx
    print(f"Mapped {len(code_to_led)} keys to LEDs.")

    base = rc.hex_to_rgbcolor(CFG["base_color"])
    colors = [CFG["colors"][i % len(CFG["colors"])] for i in range(len(CFG["colors"]))]
    color_objs = [rc.hex_to_rgbcolor(c) for c in colors]
    peak = max(0, min(100, CFG["peak_brightness"])) / 100.0
    fade = max(0.01, float(CFG["fade_seconds"]))
    frame_dt = 1.0 / max(1, CFG["fps"])

    # per-key state: when it was last pressed, and in which color
    press_time = {}     # led_idx -> monotonic timestamp
    press_color = {}    # led_idx -> RGBColor
    counter = [0]
    stop = threading.Event()

    # ---- keypress listener thread ----
    def listen():
        devs = []
        for path in evdev.list_devices():
            try:
                d = InputDevice(path)
            except Exception:
                continue
            caps = d.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_A in caps:   # looks like a keyboard
                devs.append(d)
        if not devs:
            print("No readable keyboards found (are you in the `input` group?).",
                  file=sys.stderr)
            return
        sel = selectors.DefaultSelector()
        for d in devs:
            sel.register(d, selectors.EVENT_READ)
        wanted = {1, 2} if CFG["trigger_on_repeat"] else {1}
        while not stop.is_set():
            for key, _ in sel.select(timeout=0.2):
                for ev in key.fileobj.read():
                    if ev.type == ecodes.EV_KEY and ev.value in wanted:
                        idx = code_to_led.get(ev.code)
                        if idx is not None:
                            press_time[idx] = time.monotonic()
                            press_color[idx] = color_objs[counter[0] % len(color_objs)]
                            counter[0] += 1

    t = threading.Thread(target=listen, daemon=True)
    t.start()

    # ---- render loop ----
    print(f"Running at {CFG['fps']} fps. Press keys. Ctrl+C to stop.\n")
    push_times = []
    frames = 0
    last_report = time.monotonic()
    try:
        while True:
            now = time.monotonic()
            frame = [base] * num
            for idx, t0 in list(press_time.items()):
                a = 1.0 - (now - t0) / fade
                if a <= 0:
                    press_time.pop(idx, None)
                    continue
                frame[idx] = blend(base, press_color.get(idx, color_objs[0]), a * peak)

            p0 = time.monotonic()
            device.set_colors(frame, fast=True)
            push_times.append(time.monotonic() - p0)
            frames += 1

            # once a second, report real framerate + avg push time
            if now - last_report >= 1.0:
                avg_ms = 1000 * sum(push_times) / len(push_times)
                print(f"  ~{frames} fps   avg board update: {avg_ms:5.1f} ms"
                      f"   (max {1000*max(push_times):5.1f} ms)")
                push_times.clear()
                frames = 0
                last_report = now

            sleep = frame_dt - (time.monotonic() - now)
            if sleep > 0:
                time.sleep(sleep)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        try:
            device.set_colors([rc.hex_to_rgbcolor("000000")] * num, fast=True)
        except Exception:
            pass
        print("\nStopped, board cleared.")


if __name__ == "__main__":
    main()
