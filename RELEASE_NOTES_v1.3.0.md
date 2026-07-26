# ModeShift v1.3.0

A simpler color model and a tidier editor.

## Everything you color is a zone

Select any keys, pick a color, and hit **Apply color to zone**. That selection
becomes a zone, whether it is a single key or thirty of them. Coloring keys
outside a zone is gone, so there is one concept instead of two and the layer
stack is uniform: base color, then zones, then zone effects, then key states.

If your config had keys colored directly, they are **migrated into zones
automatically** (grouped by color and placed on top, which is the precedence they
had before), so your board looks exactly the same. They appear named "Keys" and
you can rename them.

## Zone controls

- The zone buttons are now a compact 2x2 grid with the reorder arrows stacked
  beside them, which keeps the effect controls in view instead of pushing them
  off the panel.
- **Drag zones** in the list to reorder layers, as well as using the arrows.
- **Right-click a zone** for move up, move down, rename, delete, and transparent.
- The *Reset keys* button is gone; deleting the zone is the reset.

## Saved colors

A new **Saved color slots** setting chooses how many swatches to show, from 8 to
64 in rows of 8, replacing the old "+" button. Colors in hidden slots are kept,
they are simply not displayed, so turning the number back up brings them back.

## Fixed

- Saved colors past the first few rows could be clipped out of view, because the
  color panel's height was fixed when the window was built.

Existing setups load and migrate automatically. See CHANGELOG.md for the full list.
