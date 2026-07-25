# Changelog

All notable changes to ModeShift are documented here.

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
