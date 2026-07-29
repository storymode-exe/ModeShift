# Changelog

All notable changes to ModeShift are documented here.

## v1.5.1

### Fixed
- **The keyboard could stay on its own firmware lighting even when the watcher
  had connected to the right device.** Direct mode is what makes per-LED colours
  apply at all, and it was only set once, on connect. At login a board is often
  not ready to accept it yet: the call succeeds, the keyboard keeps its own
  lighting, and every frame ModeShift sends afterwards is ignored for the rest
  of the session. Direct mode is now re-asserted for the first minute after
  connecting, and after that whenever the device reports it has drifted.
- **The keyboard could sit on its firmware lighting for a whole session**, most
  often after login, with the watcher running and apparently healthy. An OpenRGB
  device handle is only an index into its device list, and OpenRGB rebuilds that
  list whenever the device count changes, which it does while hardware is still
  being detected at boot. A watcher that connected during that window spent the
  rest of the session writing colours to whatever later occupied that slot. The
  watcher now re-checks its device every few seconds, reconnects when the list
  moves, and moves back onto the keyboard named in your config if that keyboard
  only turns up after startup.
- Profiles could appear to vanish: profiles are stored per keyboard name, and
  landing on a different name gave you that name's (often nearly empty) set with
  nothing said about it. The watcher now logs when the keyboard it connected to
  is not the one the config asks for.

## v1.5.0

### Added
- **Windows support.** ModeShift now runs on Windows as well as Linux. Focused
  window detection uses `user32`, key capture uses `pynput`, and neither needs
  any special permissions. Because Windows window classes are often generic
  (`UnrealWindow`, `Chrome_WidgetWin_1`), matching also considers the process
  name and prefers it when the class is one of the known generic ones.
- **Portable Windows build.** `ModeShift-windows-portable.zip` on the releases
  page contains `ModeShift.exe` (the editor) and `ModeShiftWatcher.exe` (the tray
  daemon). Unzip anywhere writable and run it; there is no installer and nothing
  in the registry except the autostart entry. Build it yourself from a checkout
  with `packaging\build.bat`.
- **Windows autostart**, driven by the same *Start the watcher when I log in*
  checkbox in Settings. It writes a single registry Run value under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` and unticking removes it.
  No console window appears at login.
- **A logo.** ModeShift has a mark now: a yin-yang where one half is the brand
  purple and the other takes the color of the profile currently applied, so the
  tray tells you which profile is live at a glance. Shipped as SVG, PNGs, and a
  multi-size `.ico`, and used for the app icon, the window icon, and the tray.
- **Duplicate** buttons for profiles and modes. A duplicated profile copies its
  modes, zones, effects, functions, and key states, but starts with an empty
  match string, since two profiles matching the same window would be ambiguous.
- **The editor remembers its window size and position** between launches. The
  keyboard-derived size is still a hard floor so the board can never be clipped.
  A saved geometry is clamped to the current screen, so unplugging a monitor
  cannot strand the window off screen.

### Changed
- The **Default profile can now be deleted**. Delete every profile and a blank
  Default takes its place, since the editor and the watcher both need somewhere
  to fall back to. If the deleted profile was the starred default, the star
  clears with it.
- Configuration is resolved next to the executable in a packaged build, and next
  to the sources when run from a checkout, so the portable build keeps its
  settings with it instead of in a temporary folder.
- The modes list stops at nine rows and scrolls, rather than growing until the
  whole profile panel needs a scrollbar.

### Fixed
- **The keyboard stayed on its firmware lighting after login on Linux** until you
  touched the tray. The poll loop did nothing at all when the focused window
  could not be read, which is exactly the state right after login, before
  anything has focus. It now falls back to the default profile, so the board
  lights immediately. Same when no detection tool is installed.
- **Start/Restart Watcher could leave more than one watcher running**, stacking
  up tray icons. The PID file only ever names the most recent watcher, so an
  earlier one (from autostart, for example) survived every restart. ModeShift now
  finds watchers by process, and the watcher stops any others when it starts, so
  there can only ever be one.

## v1.4.0

### Added
- **X11 support for automatic game detection.** ModeShift picks its window
  backend at runtime: `kdotool` on KDE Wayland, `xprop` on X11, and `xprop` as a
  fallback on Wayland sessions without kdotool (which covers XWayland games).
  No new dependency: `xprop` ships with the standard X11 utilities. Profiles and
  match strings work exactly as before.
- **Start the watcher when I log in**, a checkbox in Settings that writes or
  removes the desktop autostart entry, so autostart no longer needs the
  `install_desktop.sh` script.
- `modeshift_watcher.py --detect-test` prints your session type, the chosen
  backend, and the focused window once a second, for checking detection on a new
  desktop without running the tray or OpenRGB.

### Changed
- If no window-detection tool is installed, ModeShift now says which package to
  install instead of failing with a bare error, and manual profile switching from
  the tray keeps working.

## v1.3.0

### Changed
- **Everything you color is now a zone.** Selecting keys and hitting *Apply color
  to zone* turns that selection into a zone, whether it is one key or thirty.
  Coloring keys outside a zone is gone, which means one concept instead of two
  and a uniform layer stack. Any loose key colors in an existing config are
  migrated into zones automatically (grouped by color, placed on top so they keep
  the precedence they had).
- Compact zone controls: the five button rows are now a 2x2 grid with the reorder
  arrows stacked beside them, which keeps the effect controls in view.
- The *Reset keys* button is gone; deleting the zone is the reset.

### Added
- Zones can be reordered by dragging them in the list, as well as with the arrows.
- Right-click a zone for move up/down, rename, delete, and transparent.
- **Saved color slots** setting: choose how many swatches to show, from 8 to 64 in
  rows of 8. Colors in hidden slots are kept, just not displayed. This replaces
  the old "+" button.

### Fixed
- Saved colors past the first few rows could be clipped out of view, because the
  color panel's height was fixed when the window was built.

## v1.2.0

### Added
- **Key States tab**: per-key indicators, stored per mode.
  - *Cooldown*: a press starts a timer. The key sits at its ready color when the
    ability is available, switches to the cooldown color on press, optionally
    fades back toward ready as it counts down, then signals ready (solid, blink,
    or breathe) either for a set time or until pressed again.
  - *Toggle*: a press advances to the next color and stays there, for things like
    shields or engines on and off.
  - A re-sync shortcut (a single key or a combo such as Ctrl + Shift + R) resets
    every indicator, and there is a matching "Reset key states" item in the tray.
  - The tab carries a plain warning that indicators follow your keypresses, not
    the game, so they can drift.
- **Import and export profiles** (Settings tab): export one profile or all of them
  to a portable `.modeshift` file, and import with Replace / Keep both / Skip on
  name conflicts. Imported profiles are migrated and filtered to the keys your
  board actually has.
- **Adjustable keyboard size** in the editor (100, 125, 150, 200%). The window
  grows to fit, up to your screen size.
- How-To gained a "What covers what" section explaining the full layer stack.

### Changed
- Key State colors deliberately draw above zones, direct key colors, and effects,
  so an ability's status is always readable.
- The keyboard now spans the full width of the window and is centered; the profile
  panel sits beside the tabs. The window opens at its true minimum size and cannot
  be shrunk small enough to clip the board.
- Every tab scrolls vertically and reflows horizontally, so panels no longer force
  the window taller or show horizontal scrollbars.
- The color wheel only applies where you send it: the Apply button is limited to
  the Color Zones tab, and other tabs use their own color controls.

### Fixed
- Toggle state colors could not be edited: the Key States swatches were wired to
  the selected zone's effect instead of the key's own colors.
- A cooldown key's ready color was covered by the zone color underneath it.
- Multi-color breathing switched color at mid-brightness, causing a visible pop.
  Each breath now dips to dark exactly where the color changes.
- Flipping a toggle did not always repaint immediately.
- Effect color and parameter edits did not update the live preview.

## v1.1.0

### Added
- Per-zone lighting effects: type lighting (reactive), breathing, blinking,
  color cycle (rainbow or custom stops), and twinkle. Every zone can carry its
  own effect.
- Multiple colors per effect (up to 8), edited with a shared color control
  (click a swatch to change it, right-click to remove). Multiples cycle
  automatically, per keypress, per breath, per blink, and so on.
- Zone layering: reorder zones with up/down arrows; the top of the list wins
  where zones overlap.
- Transparent (no-color) zones, so a zone can carry only an effect and let the
  base color or lower layers show through.
- Effect-type dropdown in the editor: pick an effect and only its options
  appear.
- Live preview now animates effects directly in the editor.
- Zone key editing: Ctrl-click keys to add or remove them from a selected zone
  (green highlight = editing a zone, yellow = a free selection), with an
  on-screen legend under the keyboard.

### Changed
- Effects moved from a mode-level setting to per-zone. Existing setups migrate
  automatically on load.

### Fixed
- Multi-color breathing now dips to dark at each color change, so the
  transition is seamless.

## v1.0.0

- First public release: per-game RGB lighting profiles for OpenRGB keyboards on
  Linux, with profiles, modes, zones, per-key colors, hold-to-switch key
  functions, focused-window detection, and a tray watcher.
