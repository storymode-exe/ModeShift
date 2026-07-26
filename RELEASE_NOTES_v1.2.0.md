# ModeShift v1.2.0

Key States, plus profile import and export.

## Key States

A new tab that turns individual keys into status lights for the game you are playing.

- **Cooldown**: press the key and it switches to your "on cooldown" color, then
  counts down. Tick *Fade toward the ready color* and the key blends back to ready
  as the timer runs, so the key itself is a rough progress bar. When it is up, the
  key signals ready (solid, blink, or breathe) either for a few seconds or until
  you press it again. A key that is off cooldown always rests at its ready color.
- **Toggle**: press to advance to the next color and it stays there. Two colors
  gives a simple on/off, useful for shields, engines, or landing gear.
- **Re-sync shortcut**: bind a key or a combo (for example Ctrl + Shift + R) to
  snap every indicator back to its default. There is also a "Reset key states"
  item in the tray menu.

Key states are stored **per mode**, so a Flight mode and an On Foot mode can each
have their own set.

Honest limitation, and it is stated right in the tab: indicators follow **your
keypresses**, not the game. ModeShift cannot tell whether an ability actually
fired or whether a shield was knocked offline, so a light can drift out of sync.
Pressing the key again re-syncs a toggle, and the re-sync shortcut resets everything.

## Import and export profiles

Settings now has an import and export section. Export one profile or all of them
to a portable `.modeshift` file (plain JSON) to back up or share. Importing keeps
your existing profiles and asks Replace, Keep both, or Skip on a name conflict.
Profiles coming from a different keyboard are filtered to the keys your board
actually has.

## Also new

- **Adjustable keyboard size** in the editor: 100, 125, 150, or 200%. The window
  grows to fit, up to your screen size.
- The How-To tab gained a **"What covers what"** section explaining the full layer
  stack, from the base color up to key states.

## Fixes and polish

- Toggle state colors could not be edited at all (the swatches were editing the
  selected zone's effect instead).
- A cooldown key's ready color was being covered by the zone color underneath.
- Multi-color breathing popped when it changed color; each breath now dips to dark
  exactly at the change.
- Effect color and slider edits now update the live preview immediately.
- The color wheel's Apply button no longer acts on other tabs.
- Layout: the keyboard spans the window and is centered, the window opens at its
  true minimum and cannot be shrunk enough to clip the board, and panels scroll
  vertically instead of forcing the window taller or scrolling sideways.

Existing setups load and migrate automatically. See CHANGELOG.md for the full list.
