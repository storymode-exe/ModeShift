"""
modeshift_effects.py  --  per-zone animated lighting effects for the watcher.

A static base layout is composited each frame with one effect layer per zone
that has an "effect" set. Zones are a layer stack (the first zone in the list
is the top layer), so effects are painted bottom-up and the top zone wins.

Effect types (each carries its own colors/params, per zone):
  reactive    keys light on press and fade out (type lighting)
  breathing   smooth sine pulse of the effect color over the base
  blinking    hard on/off of the effect color (adjustable on/off timing)
  colorcycle  rotate through the rainbow, or through a list of color stops
  twinkle     random keys sparkle and fade, like starlight

The render loop runs only while a mode actually has effects; feed_key() routes
a keypress to the top-most reactive layer covering that key. A lock guards the
shared state; the board push happens outside the lock.
"""
import math
import random
import threading
import time

from openrgb.utils import RGBColor

import modeshift_common as rc


def _blend(base: RGBColor, top: RGBColor, a: float) -> RGBColor:
    a = 0.0 if a < 0 else 1.0 if a > 1 else a
    return RGBColor(
        int(base.red + (top.red - base.red) * a),
        int(base.green + (top.green - base.green) * a),
        int(base.blue + (top.blue - base.blue) * a),
    )


def _hsv(h: float, s: float = 1.0, v: float = 1.0) -> RGBColor:
    """h,s,v in 0..1 -> RGBColor."""
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = [(v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q)][i]
    return RGBColor(int(r * 255), int(g * 255), int(b * 255))


def _sample_gradient(stops, pos: float) -> RGBColor:
    """stops: list[RGBColor]; pos 0..1 cyclic. Blends around the ring."""
    n = len(stops)
    if n == 1:
        return stops[0]
    x = (pos % 1.0) * n
    i = int(x) % n
    return _blend(stops[i], stops[(i + 1) % n], x - int(x))


class EffectEngine:
    def __init__(self, device, led_lookup: dict):
        self.device = device
        self.led_lookup = led_lookup
        self.num = len(device.leds)
        self._lock = threading.Lock()
        self._thread = None
        self._running = threading.Event()
        self._base = [RGBColor(0, 0, 0)] * self.num
        self._layers = []          # bottom -> top render order
        self._reactive_of = {}     # led_idx -> top-most reactive layer for that key
        self._fps = 60
        self._base_dirty = True

    # -- configuration -----------------------------------------------------

    def _idxs_for(self, zone):
        out = []
        for k in zone.get("keys", []):
            i = self.led_lookup.get(k.lower())
            if i is not None:
                out.append(i)
        return out

    def _build_layer(self, eff, idxs):
        t = eff["type"]
        cols = [rc.hex_to_rgbcolor(c) for c in eff.get("colors", [])]
        layer = {"type": t, "idxs": idxs, "colors": cols}
        if t == "reactive":
            layer["colors"] = cols or [RGBColor(255, 51, 0)]
            layer.update(
                peak=max(0, min(100, int(eff.get("peak_brightness", 100)))) / 100.0,
                fade=max(0.01, float(eff.get("fade_seconds", 0.6))),
                presses={}, counter=0)
        elif t == "breathing":
            layer["colors"] = cols or [RGBColor(47, 0, 255)]
            layer.update(
                period=max(0.1, float(eff.get("period_seconds", 3.0))),
                lo=max(0, min(100, int(eff.get("min_brightness", 0)))) / 100.0,
                hi=max(0, min(100, int(eff.get("max_brightness", 100)))) / 100.0)
        elif t == "blinking":
            layer["colors"] = cols or [RGBColor(255, 0, 0)]
            layer.update(
                on=max(0.0, float(eff.get("on_seconds", 0.4))),
                off=max(0.0, float(eff.get("off_seconds", 0.4))))
        elif t == "colorcycle":
            layer.update(
                rainbow=bool(eff.get("rainbow", True)) or not cols,
                stops=cols or [RGBColor(255, 0, 0)],
                period=max(0.2, float(eff.get("period_seconds", 5.0))))
        elif t == "twinkle":
            layer["colors"] = cols or [RGBColor(255, 255, 255)]
            layer.update(
                rainbow=bool(eff.get("rainbow", False)),
                density=max(0.0, min(1.0, float(eff.get("density", 0.3)))),
                fade=max(0.05, float(eff.get("fade_seconds", 1.0))),
                active={})
        return layer

    def configure(self, base_colors, mode: dict):
        with self._lock:
            self._base = list(base_colors)
            layers = []
            for zone in mode.get("zones", []):
                eff = zone.get("effect")
                if not isinstance(eff, dict) or eff.get("type") in (None, "none"):
                    continue
                idxs = self._idxs_for(zone)
                if idxs:
                    layers.append(self._build_layer(eff, idxs))
            layers.reverse()          # zone list index 0 = top -> render it last
            reactive_of = {}
            for layer in layers:      # later (higher) reactive layer wins per key
                if layer["type"] == "reactive":
                    for i in layer["idxs"]:
                        reactive_of[i] = layer
            self._layers = layers
            self._reactive_of = reactive_of
            self._base_dirty = True

    # -- keypress feed -----------------------------------------------------

    def feed_key(self, led_idx):
        if led_idx is None:
            return
        with self._lock:
            if not self._running.is_set():
                return
            layer = self._reactive_of.get(led_idx)
            if layer is None:
                return
            cols = layer["colors"]
            if len(cols) > 1:       # 2+ colors -> cycle a new one per press
                col = cols[layer["counter"] % len(cols)]
                layer["counter"] += 1
            else:
                col = cols[0]
            layer["presses"][led_idx] = (time.monotonic(), col)

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if self._running.is_set():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if not self._running.is_set():
            return
        self._running.clear()
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        self._thread = None

    def is_running(self) -> bool:
        return self._running.is_set()

    # -- rendering ---------------------------------------------------------

    def _render_layer(self, layer, frame, now):
        t = layer["type"]
        if t == "reactive":
            for i, (t0, col) in list(layer["presses"].items()):
                a = 1.0 - (now - t0) / layer["fade"]
                if a <= 0:
                    del layer["presses"][i]
                    continue
                frame[i] = _blend(frame[i], col, a * layer["peak"])
        elif t == "breathing":
            cols = layer["colors"]
            col = cols[int(now / layer["period"]) % len(cols)]   # cycle color per breath
            # (1-cos)/2 dips to 0 at each period boundary, so the color swap
            # happens at the darkest point of the breath and stays seamless.
            phase = (1 - math.cos(2 * math.pi * now / layer["period"])) / 2
            a = layer["lo"] + (layer["hi"] - layer["lo"]) * phase
            for i in layer["idxs"]:
                frame[i] = _blend(frame[i], col, a)
        elif t == "blinking":
            period = layer["on"] + layer["off"]
            cols = layer["colors"]
            if period <= 0:
                on, cyc = True, 0
            else:
                on, cyc = (now % period) < layer["on"], int(now / period)
            if on:
                col = cols[cyc % len(cols)]                        # cycle color per blink
                for i in layer["idxs"]:
                    frame[i] = col
        elif t == "colorcycle":
            pos = (now / layer["period"]) % 1.0
            col = _hsv(pos) if layer["rainbow"] else _sample_gradient(layer["stops"], pos)
            for i in layer["idxs"]:
                frame[i] = col
        elif t == "twinkle":
            active = layer["active"]
            # spawn: probability scales with density
            if layer["idxs"] and random.random() < layer["density"] * 0.4:
                i = random.choice(layer["idxs"])
                col = _hsv(random.random()) if layer["rainbow"] else random.choice(layer["colors"])
                active[i] = (now, col)
            for i, (t0, col) in list(active.items()):
                a = 1.0 - (now - t0) / layer["fade"]
                if a <= 0:
                    del active[i]
                    continue
                frame[i] = _blend(frame[i], col, a)

    def _has_animation(self) -> bool:
        # reactive/twinkle are only "busy" when something is active; the
        # continuous effects always need frames.
        for layer in self._layers:
            if layer["type"] in ("breathing", "blinking", "colorcycle"):
                return True
            if layer["type"] == "reactive" and layer["presses"]:
                return True
            if layer["type"] == "twinkle" and (layer["active"] or layer["density"] > 0):
                return True
        return False

    def _compose(self, now):
        """Return the frame to push, or None if idle (nothing to animate and
        base already shown)."""
        with self._lock:
            if not self._base_dirty and not self._has_animation():
                return None
            frame = list(self._base)
            for layer in self._layers:      # bottom -> top
                self._render_layer(layer, frame, now)
            self._base_dirty = False
            return frame

    def _run(self):
        while self._running.is_set():
            frame_dt = 1.0 / self._fps
            frame = self._compose(time.monotonic())
            if frame is not None:
                try:
                    self.device.set_colors(frame, fast=True)
                except Exception:
                    pass
            time.sleep(frame_dt)
