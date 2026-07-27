#!/usr/bin/env python3
"""
modeshift_watcher.py

Background/tray daemon that watches the active window on KDE Plasma (Wayland)
and applies the matching profile's ACTIVE mode to the keyboard via OpenRGB.

Data model (Profiles -> Modes -> Zones) lives in games.json; edit it with
modeshift_editor.py or by hand. See modeshift_common.py for the schema.

Tray menu:
    Pause                     - stop applying colors
    Auto (detect game)        - follow the focused window -> profile
    <each profile by name>    - force a specific profile regardless of focus
    Reload games.json         - re-read the config after editing
    Quit

Requirements:
    pip install openrgb-python psutil pystray pillow
    kdotool on PATH (AUR: kdotool)
    OpenRGB SDK Server running (OpenRGB -> SDK Server tab -> Start Server)

Run with --list-leds to print every LED name this device reports:
    python3 modeshift_watcher.py --list-leds
"""

import atexit
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from openrgb import OpenRGBClient  # noqa: F401  (surface import errors early)
    from PIL import Image, ImageDraw
    import pystray
except ImportError as e:
    print(f"Missing dependency: {e.name}. Install with: "
          f"pip install openrgb-python psutil pystray pillow", file=sys.stderr)
    sys.exit(1)

import modeshift_common as rc
import modeshift_effects as fx

CONFIG_PATH = rc.CONFIG_PATH
AUTO = "__auto__"  # follow game detection


class Watcher:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        try:
            self.config = rc.load_config(config_path)
        except FileNotFoundError:
            # first run with no config yet: auto-detect creates a default profile
            self.config = {"openrgb": {"host": "127.0.0.1", "port": 6742},
                           "poll_interval_seconds": 1.5, "devices": {}, "active_device": None}
        self.client = None
        self.device = None
        self.led_lookup: dict = {}
        self._reactive = None              # fx.EffectEngine, created on connect
        self._held = set()                 # keys currently down (for combos)
        self._combo_fired = False          # debounce for the re-sync shortcut
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._manual = AUTO  # AUTO, or a profile name to force
        self._last_applied = "__unset__"  # last (profile, mode) signature applied
        self._apply_lock = threading.Lock()
        self._active_profile_name = None   # profile currently being displayed
        self._mode_override = None         # mode forced by a key function (momentary)
        self._icon = None                  # tray icon, set by main() for live updates
        self._icon_color = "2F00FF"        # last mode colour, for icon redraws
        self._icon_title = "ModeShift"
        self._preview = None               # (profile, mode) sent by the editor
        # Seed with the current command-file contents so a stale command from a
        # previous session doesn't fire on startup. We never start paused: the
        # editor previews through us now rather than pausing us, so a leftover
        # pause in the file must not strand the keyboard.
        self._cmd_text = self._command_text()
        self._connect()

    def refresh_menu(self):
        """Rebuild the tray menu so newly added or renamed profiles show up
        without needing 'Reload' by hand."""
        rebuild = getattr(self, "_rebuild_menu", None)
        if rebuild is None or self._icon is None:
            return
        try:
            self._icon.menu = rebuild()
            self._icon.update_menu()
        except Exception:
            pass

    def _reset_key(self):
        """The optional 'reset key states' shortcut from settings.json, read
        fresh so the editor can change it without a full restart. Returns a
        list of key names (a combo like Ctrl + Shift + R), or None."""
        try:
            import json
            path = self.config_path.parent / "settings.json"
            raw = (json.loads(path.read_text()).get("reset_key") or "").strip()
            if not raw:
                return None
            return [k.strip() for k in raw.split("+") if k.strip()]
        except Exception:
            return None

    def _command_text(self) -> str:
        try:
            return rc.WATCHER_CMD_PATH.read_text()
        except OSError:
            return ""

    def reload_config_only(self):
        """Re-read games.json without reconnecting to OpenRGB (lighter than
        reload_config; used when the editor tells us to switch profiles)."""
        try:
            self.config = rc.load_config(self.config_path)
        except Exception as e:
            print(f"[modeshift_watcher] config reload failed: {e}", file=sys.stderr)
            return
        self._last_applied = "__unset__"
        self._mode_override = None

    def _check_editor_command(self):
        """If the editor dropped a new command, act on it. Compares the file's
        contents rather than its timestamp: two commands written within the
        same filesystem timestamp tick would otherwise be missed."""
        try:
            text = rc.WATCHER_CMD_PATH.read_text()
        except OSError:
            return
        if text == self._cmd_text:
            return
        self._cmd_text = text
        cmd = rc.read_watcher_command()
        if not cmd:
            return
        # live preview: the editor hands us the mode it is editing and we
        # render it, so key input keeps working while you edit
        preview = cmd.get("preview")
        if isinstance(preview, dict) and isinstance(preview.get("mode"), dict):
            self._preview = (preview.get("profile") or rc.DEFAULT_PROFILE_NAME,
                             rc._normalize_mode(dict(preview["mode"])))
            self._last_applied = "__unset__"
            self.apply_profile(self._preview[0])
            return

        profile = cmd.get("profile")
        if self._preview is not None and profile is not None:
            self._preview = None          # any other command ends the preview
            self._last_applied = "__unset__"
        # the editor pauses us while its live preview owns the keyboard
        if profile == rc.WATCHER_CMD_PAUSE:
            if not self.is_paused():
                self.pause()
                print("[modeshift] paused for the editor's live preview",
                      file=sys.stderr)
            return
        if profile == rc.WATCHER_CMD_RESUME:
            if self.is_paused():
                self.resume()
                print("[modeshift] resumed", file=sys.stderr)
            return
        self.reload_config_only()
        self.refresh_menu()          # profiles may have been added or renamed
        if profile in (None, rc.WATCHER_CMD_AUTO):
            self.set_manual(AUTO)
        else:
            self.set_manual(profile)
        print(f"[modeshift] editor command: switch to {profile!r}", file=sys.stderr)

    def _connect(self):
        if self._reactive is not None:
            self._reactive.stop()
        self.client = rc.open_client(self.config, client_name="game-watcher")
        self.device, self.device_name = rc.select_device(self.config, self.client)
        self.led_lookup = rc.build_led_lookup(self.device)
        self._reactive = fx.EffectEngine(self.device, self.led_lookup)

    def _profiles(self) -> dict:
        """Profiles for the auto-detected active keyboard."""
        return self.config["devices"][self.device_name]["profiles"]

    def reload_config(self):
        self.config = rc.load_config(self.config_path)
        self._connect()
        self._last_applied = "__unset__"
        self._mode_override = None

    def profile_names(self) -> list:
        return list(self._profiles().keys())

    def set_manual(self, value: str):
        """value: AUTO to follow game detection, or a profile name to force."""
        self._manual = value
        self._last_applied = "__unset__"
        self._mode_override = None

    def apply_profile(self, name: str):
        profiles = self._profiles()
        prof = profiles.get(name) or profiles.get(rc.DEFAULT_PROFILE_NAME)
        if prof is None:
            return

        # a change of profile clears any momentary mode override
        if name != self._active_profile_name:
            self._active_profile_name = name
            self._mode_override = None

        modes = prof.get("modes", {})
        override = self._mode_override
        mode_name = override if override in modes else prof.get("active_mode")
        mode = modes.get(mode_name) or rc.get_active_mode(prof)

        # while the editor is previewing, render what it sent us instead
        if self._preview is not None and self._mode_override is None:
            name, mode = self._preview[0], self._preview[1]
            mode_name = "__preview__"
            self._active_profile_name = name

        signature = (name, mode_name)
        with self._apply_lock:
            if signature == self._last_applied:
                return
            layout = rc.resolve_mode_layout(mode)
            colors = rc.build_color_array(layout, self.led_lookup, len(self.device.leds))
            # Set only this device's own LED array -- never "Apply All Devices".
            # The engine always runs, even for a mode with no effects: some
            # keyboards revert to their own firmware lighting if the host stops
            # sending direct-mode frames, so it keeps a static layout alive too.
            if self._reactive is not None:
                self._reactive.configure(colors, mode)
                self._reactive.start()
            else:
                self.device.set_colors(colors)
            self._last_applied = signature
            self._update_icon(mode, name, mode_name)

    def _update_icon(self, mode, profile_name, mode_name):
        """Recolor the tray icon to the current mode so mode/profile switches
        (including key-triggered ones) are visible at a glance."""
        self._icon_color = representative_color(mode)
        self._icon_title = f"ModeShift: {profile_name} / {mode_name}"
        self._refresh_icon()

    def _refresh_icon(self):
        """Redraw the tray icon for the current colour and pause state."""
        if self._icon is None:
            return
        try:
            paused = self.is_paused()
            self._icon.icon = make_icon_image(self._icon_color, paused=paused)
            self._icon.title = (f"{self._icon_title}  (paused)" if paused
                                else self._icon_title)
        except Exception:
            pass

    # -- key functions (momentary mode / profile switching) --------------

    def handle_key_event(self, key_name: str, pressed: bool):
        """Called by the key listener thread. Looks up a binding for the key
        in the currently-active profile and applies its press/release action
        immediately (not on the next poll tick)."""
        name = self._active_profile_name
        if name is None:
            return
        prof = self._profiles().get(name)
        if not prof:
            return
        binding = prof.get("functions", {}).get(key_name)
        if not binding:
            return
        event = binding.get("on_press") if pressed else binding.get("on_release")
        if not event:
            return
        if event["action"] == "mode":
            self._mode_override = event["target"]
            self._last_applied = "__unset__"
            self.apply_profile(name)
        elif event["action"] == "profile":
            self.set_manual(event["target"])
            self.apply_profile(event["target"])

    def pause(self):
        self._paused.set()
        if self._reactive is not None:
            self._reactive.stop()   # stop driving the board while paused
        self._refresh_icon()        # show the pause bars in the tray

    def resume(self):
        self._paused.clear()
        self._last_applied = "__unset__"
        self._refresh_icon()

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def stop(self):
        self._stop.set()
        if self._reactive is not None:
            self._reactive.stop()

    def run(self):
        while not self._stop.is_set():
            try:
                # checked even while paused, otherwise a paused watcher could
                # never see the editor's resume command
                self._check_editor_command()
            except Exception as e:
                print(f"[modeshift] command check failed: {e}", file=sys.stderr)
            if not self._paused.is_set():
                try:
                    if self._manual != AUTO:
                        self.apply_profile(self._manual)
                    else:
                        result = rc.get_active_window()
                        if result is not None:
                            win_class, pid = result
                            name = rc.resolve_profile_name(
                                win_class, pid, self._profiles(),
                                self.config.get("default_profile"))
                            self.apply_profile(name)
                except Exception as e:
                    print(f"[modeshift_watcher] error in poll loop: {e}", file=sys.stderr)
            time.sleep(self.config["poll_interval_seconds"])

    def on_key(self, key_name: str, pressed: bool):
        """Single entry point for a physical key event, from whichever listener
        the platform uses. Drives the re-sync shortcut, type lighting, and the
        per-key functions."""
        if pressed:
            self._held.add(key_name)
        else:
            self._held.discard(key_name)
        combo = self._reset_key()
        if combo:
            complete = all(k in self._held for k in combo)
            if not complete:
                self._combo_fired = False        # ready to fire again
            elif (pressed and not self._combo_fired
                  and key_name in combo and self._reactive is not None):
                # fire once when the combo completes, not on every later
                # keypress while it is still held down
                self._combo_fired = True
                self._reactive.reset_key_states()
                print("[modeshift] key states reset", file=sys.stderr)
        if pressed and self._reactive is not None:
            self._reactive.feed_key(self.led_lookup.get(key_name.lower()))
        try:
            self.handle_key_event(key_name, pressed=pressed)
        except Exception as e:
            print(f"[modeshift] key handler error: {e}", file=sys.stderr)

    def run_key_listener(self):
        """Starts the platform's key listener. Fails soft: if it is unavailable,
        key-driven features just don't fire and everything else keeps working."""
        if sys.platform.startswith("win"):
            return self._run_key_listener_windows()
        return self._run_key_listener_evdev()

    def _run_key_listener_windows(self):
        """Windows: a global keyboard hook via pynput. No special privileges
        needed, unlike the 'input' group on Linux."""
        try:
            from pynput import keyboard
        except ImportError:
            print("[modeshift] pynput not installed; type lighting, key "
                  "functions and key states are disabled. Install with: "
                  "pip install pynput", file=sys.stderr)
            return

        valid = {rc.led_shorthand(l).lower() for l in self.device.leds}

        def name_for(key):
            vk = getattr(key, "vk", None)
            if vk is None:
                vk = getattr(getattr(key, "value", None), "vk", None)
            if vk is None:
                return None
            name = rc.win_vk_to_key_name(int(vk))
            # only report keys this keyboard actually has an LED for
            return name if name and name.lower() in valid else None

        def on_press(key):
            name = name_for(key)
            if name:
                self.on_key(name, True)

        def on_release(key):
            name = name_for(key)
            if name:
                self.on_key(name, False)

        print("[modeshift] key features active (Windows hook)", file=sys.stderr)
        with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
            while not self._stop.is_set():
                listener.join(0.5)
                if not listener.running:
                    break

    def _run_key_listener_evdev(self):
        """Linux: reads raw keyboard input via evdev. Requires python-evdev and
        read access to /dev/input (add your user to the 'input' group)."""
        try:
            import evdev
            from evdev import ecodes
            import selectors
        except ImportError:
            print("[modeshift_watcher] python-evdev not installed; key functions "
                  "disabled. Install with: pip install evdev", file=sys.stderr)
            return

        # our key names -> numeric evdev codes -> back, so we can translate
        # incoming events to the names used in games.json functions
        code_to_name = {}
        for key_name in (rc.led_shorthand(l) for l in self.device.leds):
            ecode_name = rc.key_name_to_ecode_name(key_name)
            if ecode_name and hasattr(ecodes, ecode_name):
                code_to_name[getattr(ecodes, ecode_name)] = key_name

        try:
            paths = evdev.list_devices()
        except Exception as e:
            print(f"[modeshift_watcher] can't list input devices: {e}", file=sys.stderr)
            return

        keyboards = []
        for path in paths:
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
                keys = caps.get(ecodes.EV_KEY, [])
                # a real keyboard reports the letter keys
                if ecodes.KEY_A in keys and ecodes.KEY_Z in keys:
                    keyboards.append(dev)
            except (PermissionError, OSError):
                continue

        if not keyboards:
            print("[modeshift_watcher] no readable keyboard devices found for key "
                  "functions. Are you in the 'input' group? (sudo usermod -aG "
                  "input $USER, then log out/in)", file=sys.stderr)
            return

        print(f"[modeshift_watcher] key functions active on: "
              f"{', '.join(d.name for d in keyboards)}", file=sys.stderr)

        selector = selectors.DefaultSelector()
        for dev in keyboards:
            selector.register(dev, selectors.EVENT_READ)

        while not self._stop.is_set():
            for sel_key, _mask in selector.select(timeout=0.5):
                dev = sel_key.fileobj
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY or event.value not in (0, 1):
                            continue  # ignore autorepeat (value 2)
                        key_name = code_to_name.get(event.code)
                        if key_name is None:
                            continue
                        self.on_key(key_name, pressed=(event.value == 1))
                except OSError:
                    continue


def _luminance(hex_color: str) -> float:
    hex_color = hex_color.lstrip("#")
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def representative_color(mode: dict) -> str:
    """Pick a color that best represents a mode for the tray icon: the base
    color, unless it's near-black and there are zones, in which case use the
    brightest zone color so the icon stays visible."""
    base = mode.get("base_color", "000000")
    zones = [z for z in mode.get("zones", []) if z.get("color")]  # skip transparent
    if _luminance(base) < 30 and zones:
        best = max(zones, key=lambda z: _luminance(z.get("color", "000000")))
        return rc.scale_hex(best.get("color", "FFFFFF"), best.get("brightness", 100))
    return base


BRAND_PURPLE = (106, 61, 255)      # the half that never changes
ICON_TILT = 45                     # extra rotation, degrees
ICON_CLOCKWISE = True              # swirl direction


def make_icon_image(color_hex="FF5A00", paused=False):
    """ModeShift's mark: a yin-yang where one half is the brand purple and the
    other takes the colour of the mode currently applied, so the tray tells you
    at a glance which profile you are on."""
    color_hex = (color_hex or "FF5A00").lstrip("#")
    try:
        live = tuple(int(color_hex[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        live = (255, 90, 0)
    if sum(live) < 40:                      # a near-black mode would vanish
        live = (255, 90, 0)

    S, ss = 64, 8                            # draw big, shrink for smooth edges
    n = S * ss
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = 2 * ss
    box = (pad, pad, n - pad, n - pad)
    r = (n - 2 * pad) / 2
    cx = cy = n / 2

    # purple on top, live colour below, with the swirl reading clockwise
    d.ellipse(box, fill=live)
    d.pieslice(box, 180, 360, fill=BRAND_PURPLE)
    side = 1 if ICON_CLOCKWISE else -1
    d.ellipse((cx + side * r / 2 - r / 2, cy - r / 2,
               cx + side * r / 2 + r / 2, cy + r / 2), fill=BRAND_PURPLE)
    d.ellipse((cx - side * r / 2 - r / 2, cy - r / 2,
               cx - side * r / 2 + r / 2, cy + r / 2), fill=live)

    # tilt the mark itself; the pause bars stay upright so they read as a
    # pause button rather than part of the swirl
    if ICON_TILT:
        img = img.rotate(ICON_TILT, resample=Image.BICUBIC)

    if paused:
        d = ImageDraw.Draw(img)
        bar_w, bar_h = r / 4, r * 0.9
        for sign in (-1, 1):
            x = cx + sign * r / 3
            d.rounded_rectangle((x - bar_w / 2, cy - bar_h / 2,
                                 x + bar_w / 2, cy + bar_h / 2),
                                radius=bar_w / 3, fill=(20, 22, 27),
                                outline=(255, 255, 255), width=ss)

    return img.resize((S, S), Image.LANCZOS)


def build_tray(watcher: Watcher):
    def on_pause(icon, item):
        watcher.resume() if watcher.is_paused() else watcher.pause()

    def on_reset_states(icon, item):
        """Snap cooldown/toggle indicators back to default when they drift out
        of sync with what's actually happening in the game."""
        if watcher._reactive is not None:
            watcher._reactive.reset_key_states()

    def on_open_editor(icon, item):
        """Launch the editor from the tray."""
        cmd = rc.program_command("editor")
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(
                    cmd,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)
            else:
                subprocess.Popen(cmd, start_new_session=True)
        except Exception as e:
            print(f"[modeshift] couldn't start the editor: {e}", file=sys.stderr)

    def on_reload(icon, item):
        try:
            watcher.reload_config()
            icon.menu = build_menu()
            icon.update_menu()
            print("[modeshift] configuration reloaded")
        except Exception as e:
            print(f"[modeshift_watcher] failed to reload config: {e}", file=sys.stderr)

    def on_quit(icon, item):
        watcher.stop()
        icon.stop()

    def on_auto(icon, item):
        watcher.set_manual(AUTO)

    def make_selector(name):
        def _select(icon, item):
            watcher.set_manual(name)
        return _select

    def build_menu():
        profile_items = [
            pystray.MenuItem(
                name, make_selector(name), radio=True,
                checked=(lambda item, n=name: watcher._manual == n),
            )
            for name in watcher.profile_names()
        ]
        return pystray.Menu(
            pystray.MenuItem("Pause", on_pause, checked=lambda item: watcher.is_paused()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Auto (detect game)", on_auto, radio=True,
                             checked=lambda item: watcher._manual == AUTO),
            *profile_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open editor", on_open_editor),
            pystray.MenuItem("Reset key states", on_reset_states),
            pystray.MenuItem("Reload configuration", on_reload),
            pystray.MenuItem("Quit", on_quit),
        )

    watcher._rebuild_menu = build_menu      # so the watcher can refresh it live
    default_prof = watcher._profiles().get(rc.DEFAULT_PROFILE_NAME, {})
    start_color = representative_color(rc.get_active_mode(default_prof)) if default_prof else "2F00FF"
    watcher._icon_color = start_color
    watcher._icon_title = f"ModeShift: {watcher.device_name}"
    paused = watcher.is_paused()          # may already be paused by the editor
    return pystray.Icon(
        "modeshift_watcher",
        icon=make_icon_image(start_color, paused=paused),
        title=watcher._icon_title + ("  (paused)" if paused else ""),
        menu=build_menu())


def list_leds(config_path: Path):
    cfg = rc.load_config(config_path)
    try:
        client = rc.open_client(cfg, client_name="game-watcher-list-leds")
        device, _name = rc.select_device(cfg, client)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    print(f"Device: {device.name}  ({len(device.leds)} LEDs)\n")
    print(f"{'idx':>4}  {'full name':<30} shorthand for games.json")
    for i, led in enumerate(device.leds):
        print(f"{i:>4}  {led.name:<30} {rc.led_shorthand(led)}")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    config_path = Path(args[0]) if args else CONFIG_PATH

    if "--list-leds" in sys.argv:
        list_leds(config_path)
        return

    # quick check that window detection works on this desktop, without needing
    # OpenRGB or the tray: prints the focused window every second
    if "--detect-test" in sys.argv:
        print(f"session : {os.environ.get('XDG_SESSION_TYPE', '(unset)')}")
        print(f"backend : {rc.detect_backend() or 'NONE FOUND'}")
        print("Focus other windows to see what ModeShift reads. Ctrl+C to stop.\n")
        try:
            while True:
                try:
                    result = rc.get_active_window()
                except RuntimeError as e:
                    print(e)
                    return
                if result is None:
                    print("  (could not read the focused window)")
                else:
                    win_class, pid = result
                    print(f"  class={win_class!r}  pid={pid}  "
                          f"process={rc.process_name_for_pid(pid)!r}")
                time.sleep(1)
        except KeyboardInterrupt:
            return

    # --wait: retry the OpenRGB connection for ~30s. Used by the autostart
    # entry so a slightly-slower OpenRGB SDK server on login doesn't cause a
    # hard failure. Interactive runs (no flag) fail fast instead.
    wait = "--wait" in sys.argv
    attempts = 15 if wait else 1
    watcher = None
    last_err = None
    for i in range(attempts):
        try:
            watcher = Watcher(config_path)
            break
        except Exception as e:
            last_err = e
            if wait and i < attempts - 1:
                print(f"[modeshift_watcher] waiting for OpenRGB... ({e})", file=sys.stderr)
                time.sleep(2)
    if watcher is None:
        print(f"[modeshift_watcher] startup failed: {last_err}", file=sys.stderr)
        sys.exit(1)

    # record our PID so the editor's Start/Restart button can stop exactly
    # this process, on any platform
    rc.write_watcher_pid()
    atexit.register(rc.clear_watcher_pid)

    worker = threading.Thread(target=watcher.run, daemon=True)
    worker.start()

    # separate thread listens for physical key presses to drive per-key
    # Change-Mode / Change-Profile functions (Star Citizen holds, etc.)
    key_thread = threading.Thread(target=watcher.run_key_listener, daemon=True)
    key_thread.start()

    icon = build_tray(watcher)
    watcher._icon = icon  # let apply_profile recolor the tray on mode/profile change
    icon.run()  # blocks; must run on the main thread


if __name__ == "__main__":
    main()
