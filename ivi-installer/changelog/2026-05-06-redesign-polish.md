# 2026-05-06 - UI redesign polish

Iterative cleanup pass on the redesigned IVI Installer UI. No business
logic touched - only visuals, layout, and a couple of UX wires.

## Top device strip (`ivi_installer/ui/device_status.py`)

- Removed the car (`VehicleGlyph`) icon from the strip. The attribute
  `self._glyph` is kept and just hidden so any code/tests touching it
  keep working.
- Reduced spacing between the title row and the sub-line:
  `col.setSpacing(2)` → `0`.
- Tightened vertical padding of the strip: outer margins `8/8` → `6/6`,
  `setMinimumHeight(72)` → `60`.
- Forced background to `#1d1e21` (`bgRaised`) regardless of detection
  state by:
  - `setAttribute(Qt.WA_StyledBackground, True)` - so QSS bg actually
    paints on a custom `QWidget` subclass.
  - Inline `setStyleSheet("QWidget#deviceStrip { background: ... }")`
    fallback so the color renders even before global QSS resolves.

## Theme (`ivi_installer/ui/theme.py`)

- Removed the `#deviceStrip[state="searching"]` darker override so the
  strip stays at `bgRaised` (`#1d1e21`) regardless of "searching" /
  "none" / "connected" state. The `unauthorized` warn-tint variant is
  kept.
- Added `QSplitter#bodySplitter::handle:vertical` styling - thin border
  lines, subtly highlighted on hover.

## Tab pill / segmented control (`ivi_installer/ui/widgets.py`)

`MacSegmentedTabBar` (the macOS-style centered segmented control above
the tab content):

- Increased height once so text would stop clipping (56 → 72), then
  reduced by ~20% to a friendlier compact size: **fixed height 58**.
- Final values:
  - widget height: `58`
  - outer margins: `(0, 10, 0, 11)`
  - pill inner padding: `(3, 3, 3, 3)`
  - pill border-radius: `9`
  - button border-radius: `7`
  - button padding: `9px 22px` (was `7px 22px` - text was clipping)
  - font-size: unchanged at `13px`

## Layout / scroll behavior (`ivi_installer/ui/main_window.py`)

- Swapped stretch factors so the install tab content expands with the
  window and the log pane sizes to its content:
  `tabs stretch=1`, `log_pane stretch=0`.
- Wrapped tabs + log pane in a vertical `QSplitter`
  (`#bodySplitter`) - the user can now drag the divider to resize the
  log pane:
  - `setChildrenCollapsible(False)` so neither side disappears
  - `setHandleWidth(6)`
  - initial sizes `[600, 180]`
  - `setStretchFactor(0, 1) / (1, 0)`
- Result: the install-tab `QScrollArea` now only shows its scrollbar
  when content actually overflows.

## Log pane (`ivi_installer/ui/widgets.py`)

- Set `self.view.setMinimumHeight(140)` on the log `QPlainTextEdit` so
  the pane keeps a sensible default height now that it no longer
  receives all the layout stretch.

## Strategy switch restored (`ivi_installer/ui/main_window.py`)

The previous redesign hid the install-strategy radios entirely. They've
been wired back into the UI as a small segmented pill on the
`Force reinstall` row:

- Visible UI: `STRATEGY` section label + pill with two buttons -
  `pm-disable` and `HDB broker`. Styled inline to match the rest of the
  redesign (sunken pill, raised + bold for the active item, hover
  brightens muted text).
- Hidden radios (`strat_pmdisable_radio` / `strat_hdb_radio`) are kept
  as the **canonical state** for settings persistence and existing
  tests.
- Two-way binding: visible buttons drive the hidden radios; the hidden
  radios drive the visible buttons. So `settings.set` / programmatic
  changes from tests stay authoritative, and the UI always reflects
  reality.
- Tooltips on both buttons explaining the path.

## Build

- Rebuilt with `briefcase update macOS && briefcase build macOS`.
- Output: `build/ivi_installer/macos/app/IVI Installer.app`.

## Files touched

- `ivi_installer/ui/theme.py`
- `ivi_installer/ui/widgets.py`
- `ivi_installer/ui/main_window.py`
- `ivi_installer/ui/device_status.py`
