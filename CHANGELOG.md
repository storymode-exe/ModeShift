# Changelog

All notable changes to ModeShift are documented here.

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
