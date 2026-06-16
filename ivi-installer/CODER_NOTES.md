# IVI Installer - redesign handoff to the human dev

This file documents what's done, what's stubbed, what was deliberately
deviated from the design, and the precise hooks where the next dev
should wire real backend state into the new visual surfaces.

The redesign lives on the `redesign` branch. The pre-redesign UI is
preserved on `dev` / tags `0.5.0` / `0.6.0` / `0.7.0`.

---

## Files added / changed

| Path | Change |
|---|---|
| `ivi_installer/ui/theme.py` | **new** - design tokens + QSS generator + `apply_app_theme()` |
| `ivi_installer/ui/widgets.py` | **new** - custom widgets (DropZone, ApkCard, Pipeline, GrantMatrix, SuccessBanner, XapkWarningBanner, LogPane, VehicleGlyph, PulsingDot, Spinner, ScreensDiagram) |
| `ivi_installer/ui/device_status.py` | **rewritten** - same public API (signals, `dot_label`/`main_label`/`sub_label`/`badge_label`, COLOR_* exports), redesigned visuals (vehicle glyph, pulsing dot, mono meta line, state-tinted background) |
| `ivi_installer/ui/main_window.py` | **rewritten** - new two-column layout for Install tab, restyled Tools / Timezone / Device-info tabs, always-on LogPane at the bottom. **All pre-redesign widget names and handler methods preserved** so backend logic and tests are unchanged |
| `ivi_installer/__main__.py` | added `apply_app_theme(app, theme="dark")` before window construction |

---

## What's done (matches the design tightly)

* **Window shell** - 1100×800 (matches `tokens.json#app.windowSize`).
* **Device strip** with vehicle glyph (custom-painted SVG-style sedan
  with accent windshield "screen"), pulsing connection dot, and
  `Connected via USB` caption. Reacts to `connected` / `searching` /
  `unauthorized` / `multi` / `none` states with the right typography
  and tinted backgrounds.
* **Tabs** - underline accent on the active tab (Windows-flavoured
  variant from the design - see "deviations" below for why we kept this
  on macOS too).
* **Install tab** two-column grid:
  * Drop zone with dashed border, diagonal stripe pattern, upload
    glyph, mono `.apk` and accent-underlined `Choose file…`.
    Drag-drop is fully wired; `.xapk` triggers the warn banner.
  * APK summary card with gradient initials icon, package title,
    mono meta, dismiss `✕`. Hides drop zone when populated.
  * `.xapk` warn banner - appears when a `.xapk` is dropped or picked.
  * Install-on radio cards with embedded HUD-chip + 3 user-chip
    diagram (chips fill accent based on `all` vs `driver`).
  * Force-reinstall checkbox + helper text.
  * Pill-shaped primary CTA with "Connect a device to enable." /
    "Drop an APK to enable." hint.
  * Pipeline with 5 stages (idle ring, running spinner, done check,
    failed X) - left accent rule + tinted bg on the running stage.
  * Grant matrix (8 perms × 5 users) with tinted alternating rows
    and `-` placeholders.
  * Success banner - green-tinted with check glyph, mono package name
    inline, "View detailed log" accent link.
* **Tools tab** - exactly the design's three cards: full-width
  "Grant runtime permissions" with combobox + Refresh + primary CTA
  + last-run summary block, then half-width "Diagnose" + "Bypass
  health" side by side.
* **Timezone tab** - current-tz block + Apply button on the same row,
  filter input underneath, scrollable IANA list with mono offsets,
  selected row uses accent-tinted left border.
* **Device info tab** - uppercase header label + Refresh + Copy on
  the right, mono `<pre>`-styled body.
* **Keyboards tab** (5th tab - see deviation below) - restyled to use
  the same card pattern.
* **Log pane** at the bottom of every tab - header bar with `LOG`
  uppercase label, accent-underlined mono path link, ghost
  Copy/Save/Clear buttons on the right; mono body with timestamps.
* **Theme** - single QSS source (`theme.build_qss(theme)`), tokens
  match the sRGB-hex fallbacks of `design/handoff/tokens.json`.

---

## Deliberate deviations from the design

These were chosen to keep the existing test suite (184 tests) passing
and to avoid destabilising production behaviour.

### 1. Native window chrome

The design mocks up custom title bars (macOS traffic lights / Windows
min-max-close). We use the **native OS chrome** because:

* Frameless windows on macOS lose proper full-screen, focus, and
  expose-app-window behaviours.
* Windows custom chrome breaks DWM shadows + snap-to-edge.
* The app title and version still appear in the OS title bar via
  `setWindowTitle("IVI Installer")` and the bundle metadata.

If product wants the mocked chrome, the right path is a custom
`QWidget` window with `Qt.FramelessWindowHint` + manual hit-testing.
**Don't** ship that without testing on Win10/11 + macOS 12/13/14.

### 2. Five tabs instead of four

Design shows four tabs (`Install APK · Tools · Timezone · Device
info`). We keep **Keyboards** as the 5th tab because:

* `tests/test_main_window.py::test_keyboards_tab_has_celia_install_button`
  asserts it exists with the exact button label.
* The Celia keyboard install + IME enrollment is a real shipped
  feature (v0.7.0) with its own backend workers.

If product wants Keyboards folded into Tools, the cleanest option is
to add a 4th card to the Tools tab and delete the 5th tab - see
`_build_keyboards_tab` for everything that needs to move.

### 3. AppGallery URL download is hidden, not removed

The design has no surface for downloading APKs from a Huawei
AppGallery link. The user requested *"keep the URL-download logic;
just hide it in the new design"*.

Implementation:

* Widgets `appgallery_input`, `appgallery_button`, `appgallery_progress`
  exist on the Install tab but are **`setVisible(False)`**.
* All handlers (`_on_appgallery_download`, `_on_appgallery_progress`,
  `_on_appgallery_result`, `_on_appgallery_error`) are wired and
  functional.
* A hidden hotkey **`Ctrl/Cmd+Shift+D`** opens a `QInputDialog` that
  asks for the AppGallery link / id and triggers the download.
* The downloaded APK is auto-loaded into the new APK card via
  `_set_apk_file(...)`.

If product wants this surfaced again, the simplest path: add a small
"From URL…" link near the drop zone that calls `_prompt_appgallery()`.

### 4. Strategy radios (`pm-disable` vs `HDB broker`) are hidden

The design only shows one install path. We keep both `strat_pmdisable_radio`
and `strat_hdb_radio` (default checked: HDB broker) but `setVisible(False)`.
Setting still persists via `settings.set(SETTING_PRIMARY_STRATEGY, ...)`,
the install pipeline still cascades primary → fallback automatically.

`tests/test_main_window.py::test_strategy_radio_defaults_to_hdb_broker`
relies on these widgets existing.

If product wants user-selectable strategy back, expose them in a small
"Advanced" disclosure under the Force-reinstall checkbox.

### 5. Window chrome version tag

Design shows `IVI Installer  v0.4.2` next to the title in the chrome.
We get this for free in the OS title bar via `setWindowTitle`; the
in-window version tag is therefore omitted. Bump version in
`pyproject.toml` (`project.version` and `tool.briefcase.version`).

---

## Visual stubs that need backend wiring

The design shows two surfaces that the **current backend doesn't drive
directly**. They're rendered in the redesign but only get *demo
visuals* on success - see below.

### `Pipeline` - 5-stage rail

File: `ivi_installer/ui/widgets.py::Pipeline`.

The widget has a clean public API:

```python
pipeline.set_step(running_index)            # idle / running / future
pipeline.mark_done_with_timing(idx, "180ms")
pipeline.mark_failed(idx)
pipeline.reset()
```

**What's missing:** `workers.InstallWorker` currently emits the
`attempt` signal once per *strategy* (pm-disable / hdb-broker), not
once per pipeline stage. So we can't bind 1:1 today.

**What we do today** (in `MainWindow._on_install`):
* `pipeline.set_step(0)` when install starts (broker check goes
  active).
* On `_on_install_result` success → mark all 5 stages done + show
  success banner.
* On failure → `mark_failed(0)`.

**What to do for proper wiring** (recommended):
1. Add per-stage signals to `installer.HdbBrokerInstall`:
   - `stage_started(int idx, str label, str hint)`
   - `stage_finished(int idx, str timing_str)`
   - `stage_failed(int idx, str message)`
2. Re-emit them through `workers.InstallWorker`.
3. Connect them in `MainWindow._on_install` to the `pipeline.*` calls.

The stage IDs and copy already match `design/handoff/copy.json#install.stages`.

### `GrantMatrix` - 8 perms × 5 users grid

File: `ivi_installer/ui/widgets.py::GrantMatrix`.

API:

```python
grant_matrix.reset()                      # all em-dashes
grant_matrix.fill_demo_success()          # 39 ✓ + CAMERA×u0 ✗
# planned: grant_matrix.set_cell(perm_idx, user_idx, "ok"|"fail"|"idle")
```

**What's missing:** the install pipeline runs runtime-permission grants
via `strategies.grant_runtime_perms_per_user(...)` but only logs
them - there's no per-cell signal.

**Recommended wiring:**
1. Have the perm-grant strategy emit `perm_granted(user, perm, ok)` for
   each `pm grant` call.
2. Bubble through `workers.InstallWorker`.
3. Add a `set_cell(...)` method to `GrantMatrix` (the `_cells` dict
   keyed by `(row, col)` is already there) and call it from
   `MainWindow._on_install`.

Today the matrix shows `-` everywhere until a successful install, at
which point we paint the design's demo state (all green except the
synthetic `CAMERA × u0` red - kept on purpose because the actual
installer does skip CAMERA on user 0 on these vehicles).

### Success banner content

`SuccessBanner.show_success(package, scope, tail)` renders the design's
copy. Today we use `self._selected_path.stem` as the package name -
real package extraction from the APK is **not** done. To get the real
package name + version + size, parse the AndroidManifest.xml; see
`appgallery.py` for an existing aapt-free path that uses `zipfile`
directly.

---

## Pixel-perfect items deliberately approximated

* **oklch colors** - Qt has no oklch; we use the sRGB hex fallbacks
  from `tokens.json`. Visually 99% identical for dark theme; light
  theme may render slightly cooler on wide-gamut displays.
* **Window shadow** - `0 30px 80px -20px rgba(0,0,0,0.45)` on the
  whole window. The OS draws its own shadow; emulating the design's
  custom shadow needs frameless mode (see deviation #1).
* **Animations** - `iviSpin` (0.9s) and `iviPulse` (1.6s) are
  approximated by Python `QTimer`s in `Spinner` and `PulsingDot`.
  Visually indistinguishable; CPU cost is negligible.
* **Blinking caret** - not yet rendered in the log pane (the design
  shows a 7×13 accent block at the very end). Trivial to add: subclass
  `QPlainTextEdit` and paint a rect in `paintEvent`. Skipped because
  it has no functional value.
* **Light theme** - tokens are defined in `theme.TOKENS["light"]` and
  `apply_app_theme(app, theme="light")` works, but the app boots in
  dark. Auto-switch with the OS isn't wired; we'd need to listen to
  `QGuiApplication.styleHints().colorSchemeChanged` and re-apply the
  QSS. Easy to add when product wants it.

---

## How to run / verify

```bash
cd ivi-installer
source .venv/bin/activate
pip install -e .
python -m ivi_installer            # GUI
python -m ivi_installer --cli      # device list, no Qt needed
pytest                              # 184 tests, all green
```

To screenshot a state without a real device, see the snippet that
generated `/tmp/redesign-*.png` during the redesign session - feed any
`DeviceStatus` into `MainWindow._on_status` directly.

---

## Open questions for product

1. **Custom chrome y/n?** - see deviation #1.
2. **Keyboards tab placement** - keep as 5th tab or fold into Tools?
   See deviation #2.
3. **AppGallery URL download - surface or remove entirely?** - today
   it's hidden behind a hotkey (deviation #3).
4. **Light-mode auto-switch** - should the app follow the OS theme?
5. **Stage-level pipeline progress** - worth adding per-stage signals
   to the install worker so the Pipeline animates live? It's a real
   investment in `installer.py`; product should decide whether the
   visual fidelity is worth the refactor.
