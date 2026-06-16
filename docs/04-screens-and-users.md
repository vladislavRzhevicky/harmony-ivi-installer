# Screens and Android users

On Huawei-automotive head units the cabin has several **physical displays**,
and the system maps **one Android user per screen**. An installed app only
appears on a screen if it's installed *for that screen's user*. So to put an app
on every display you must know the screen ↔ user mapping - and it is **not**
derivable from user names; it has to be probed.

Reference firmware: a Deepal S09 (board `ALSK-D587`, HwSAPT, HarmonySpace 5.0).
Implementation:
[`../ivi-installer/ivi_installer/devices.py`](../ivi-installer/ivi_installer/devices.py)
(`list_displays`, `categorize_screens`).

## The layout (reference car)

Four physical displays, five Android users:

| displayId | display name         | size       | role      | active user |
|----------:|----------------------|-----------:|-----------|:-----------:|
| 0 | `control_panel`        | 2560×1440 | driver    | u13 |
| 3 | `hud_panel`            | 800×480   | HUD       | u13 |
| 4 | `central_rear_panel`   | 3036×1708 | rear      | u12 |
| 6 | `co-driver_panel`      | 2560×1440 | passenger | u11 |

```
Android users (pm list users):
  0   system / HEADLESS   (no UI)
  10  legacy / unused     (dead on this firmware)
  11  → co-driver_panel   (passenger)
  12  → central_rear_panel (rear)
  13  → control_panel + hud_panel  (driver + HUD, same display group)
```

Driver and HUD share a display group, so they share a user (13). Rear and
passenger each carry `FLAG_DISPLAY_CREATE_USER` → their own display group →
their own user.

**This layout is specific to one car.** Other cars put the driver on user 10,
12, 14, or higher, sometimes with no user 13 at all. Never hardcode
`(10, 11, 12, 13)`.

## Why names don't work

On this firmware `pm list users` returns opaque names like `NoLoginUser`,
`NoLoginUser_4`, `NoLoginUser_6`, and numeric junk - none of which match any
"driver/passenger/rear" heuristic. **Display names, however, are firmware-fixed
and identical across Huawei IVIs** (`control_panel`, `co-driver_panel`,
`central_rear_panel`, `hud_panel`). That's the reliable signal.

## How to resolve it

Two `dumpsys` reads, joined:

```bash
# 1. display id → display name
adb shell dumpsys display

# 2. display id → focused Android user
adb shell dumpsys window displays
```

- From `dumpsys display`, parse the `displayId <N><name>"` tokens to get
  `{display_id: name}`. (The `-` in `co-driver_panel` matters - a regex that
  stops at `-` mislabels the passenger screen.)
- From `dumpsys window displays`, for each `Display: mDisplayId=N` block, read
  `mCurrentFocus=Window{u<N> …}` to get the user. If a display is OFF and has
  no focused window, fall back to the first `ActivityRecord{u<N>}` in the block
  (the rooted task usually stays on the right user even when the screen is off).
- Map display name → role with an **ordered** pattern table (specific fragments
  first, so `co-driver` is matched before `driver`):

  ```
  co-driver / co_driver / codriver / front_passenger / passenger  → passenger
  central_rear / rear_seat / rear / backseat                      → rear
  hud                                                             → hud (folded into driver)
  control_panel / driver / main_panel                            → driver
  ```

## Resolution chain (most reliable first)

`categorize_screens(users, displays)` tries, in order:

1. **Display-based mapping** (preferred). If the displays resolved at least the
   driver bucket, take `display.role → display.user_id` directly. HUD folds into
   driver (same display group), so it never shows as a separate install target.
2. **User-name heuristics** - only if displays yielded nothing; classify
   `pm list users` names (`driver/main/front` → driver, etc.). Weak on opaque
   firmwares.
3. **Dynamic fallback** - `driver = (max id,)`, every other multimedia id →
   `rear`, `passenger = ()` (don't invent a split with no signal).
4. **Static fallback** - only when there are no multimedia users at all, or the
   ids exactly match the original Deepal S09 shape.

For the **install seed/fan-out probe**, use `devices.live_screen_users(serial)`,
which returns the running cabin-screen user ids for *this* car. Feed those to
`pm install-existing --user N`.

## Reproduce it yourself

```bash
adb shell dumpsys display                  # displays, sizes, states
adb shell dumpsys window displays          # which user renders on each display
adb shell pm list users                    # the Android user list
adb shell am get-current-user              # the active driver user
adb shell dumpsys SurfaceFlinger | grep -E "layerStack=|uid="
```

## Edge cases

- **Rear screen OFF.** `central_rear_panel` is often powered down when nobody's
  in the back; `mCurrentFocus` is null, but the `ActivityRecord{uN}` fallback
  still recovers the user.
- **3-screen cars (no rear).** One `FLAG_DISPLAY_CREATE_USER` display is simply
  absent; the resolver returns an empty bucket for that role and the UI renders
  a disabled checkbox.
- **HUD.** Physically a separate 800×480 display, but logically part of the
  driver (same user). You can't install "onto the HUD" as a separate target -
  the native AR-HUD stack owns it, and its user is the driver's.
