"""Tests for ivi_installer.devices.

The fixtures below come from real `adb devices -l` outputs we have
seen in the wild, plus a few constructed edge cases.
"""
from __future__ import annotations

import pytest

from ivi_installer.devices import (
    MODEL_MAP,
    AndroidUser,
    Device,
    DisplayInfo,
    ScreenCategory,
    _FALLBACK_CATEGORIES,
    build_displays,
    categorize_screens,
    describe_model,
    parse_devices_output,
    parse_display_names,
    parse_focused_users,
    parse_pm_users,
)


# ---- happy path fixtures ----

AVATR_12 = (
    "List of devices attached\n"
    "1234ABCD               device product:ICHU3200F2-ADV "
    "model:AVATR_12 device:ICHU3200F2-ADV transport_id:1\n"
)

DEEPAL_X2 = (
    "List of devices attached\n"
    "DPL5XYZ7  device product:ICHU3200X2-ADV "
    "model:DEEPAL_S07 device:ICHU3200X2-ADV transport_id:3\n"
)

# Two devices simultaneously (multi-device edge case from supplement §9.1).
TWO_DEVICES = (
    "List of devices attached\n"
    "1234ABCD               device product:ICHU3200F2-ADV "
    "model:AVATR_12 device:ICHU3200F2-ADV transport_id:1\n"
    "DPL5XYZ7               device product:ICHU3200X2-ADV "
    "model:DEEPAL_S07 device:ICHU3200X2-ADV transport_id:2\n"
)

# Common during initial USB plug — device is "unauthorized" until confirmed.
UNAUTHORIZED = (
    "List of devices attached\n"
    "PLUGGEDX  unauthorized\n"
)

OFFLINE = (
    "List of devices attached\n"
    "OFFLINE1  offline\n"
)

EMPTY = "List of devices attached\n\n"


# ---- parse_devices_output ----

def test_parses_avatr_12_line():
    [d] = parse_devices_output(AVATR_12)
    assert d == Device(
        serial="1234ABCD",
        state="device",
        product="ICHU3200F2-ADV",
        model="Avatr 12",
        transport_id="1",
    )
    assert d.is_ready


def test_parses_deepal_line():
    [d] = parse_devices_output(DEEPAL_X2)
    assert d.product == "ICHU3200X2-ADV"
    assert d.model == "Deepal S07 / X2"
    assert d.is_ready


def test_parses_multiple_devices():
    devices = parse_devices_output(TWO_DEVICES)
    assert len(devices) == 2
    serials = {d.serial for d in devices}
    assert serials == {"1234ABCD", "DPL5XYZ7"}


def test_parses_unauthorized_state():
    [d] = parse_devices_output(UNAUTHORIZED)
    assert d.state == "unauthorized"
    assert d.is_ready is False
    assert d.product is None
    assert d.model is None


def test_parses_offline_state():
    [d] = parse_devices_output(OFFLINE)
    assert d.state == "offline"
    assert d.is_ready is False


def test_handles_empty_output():
    assert parse_devices_output(EMPTY) == []
    assert parse_devices_output("") == []


def test_skips_unparseable_lines():
    """Defensive: future adb versions add extra header lines."""
    weird = (
        "* daemon not running; starting now at tcp:5037 *\n"
        "* daemon started successfully *\n"
        "List of devices attached\n"
        "1234ABCD               device product:ICHU3200F2-ADV transport_id:1\n"
    )
    [d] = parse_devices_output(weird)
    assert d.serial == "1234ABCD"


def test_unknown_product_keeps_raw_id():
    """A device we have not catalogued yet stays usable, just unlabelled."""
    text = (
        "List of devices attached\n"
        "NEWHW1   device product:ICHU3300NEW-ADV transport_id:9\n"
    )
    [d] = parse_devices_output(text)
    assert d.product == "ICHU3300NEW-ADV"
    assert d.model is None    # not in MODEL_MAP yet
    assert d.is_ready


def test_supports_extra_whitespace():
    text = (
        "List of devices attached\n"
        "  1234ABCD    device   product:ICHU3200F2-ADV   transport_id:1  \n"
    )
    [d] = parse_devices_output(text)
    assert d.serial == "1234ABCD"
    assert d.product == "ICHU3200F2-ADV"


# ---- describe_model ----

def test_describe_known_model():
    assert describe_model("ICHU3200F2-ADV") == "Avatr 12"


def test_describe_unknown_model_falls_back_to_raw():
    assert describe_model("ICHU9999-ADV") == "ICHU9999-ADV"


def test_describe_none_is_unknown():
    assert describe_model(None) == "Unknown device"


# ---- MODEL_MAP integrity (regression guard) ----

def test_model_map_covers_pyqttool_known_ids():
    """If carSlot.py knows it, we must label it."""
    pyqttool_ids = {
        "SGLA-X6S", "PRVC-F3", "ICHU3200F2-ADV", "ICHU3200X2-ADV",
    }
    assert pyqttool_ids.issubset(MODEL_MAP.keys())


# ---- DeviceInfo / DeviceCapabilities batch parser ----

from ivi_installer.devices import (
    DeviceCapabilities,
    DeviceInfo,
    detect_full_info,
    parse_capabilities_batch,
)


# Realistic Pura 70 Ultra (ABR-AL60) batch output. Test scenario from DoD.
PURA_BATCH = (
    "ABR-AL60\n"        # ro.product.model
    "HWABR\n"           # ro.product.device
    "12\n"              # ro.build.version.release
    "32\n"              # ro.build.version.sdk
    "\n"                # ro.build.version.harmonyos (empty)
    "4.2.0\n"           # hw_sc.build.platform.version
    "arm64-v8a\n"       # ro.product.cpu.abi
    "ru-RU\n"           # persist.sys.locale
    "shell\n"           # whoami
    "0\n"               # ps -A | grep -c hdbd  (0 = no hdbd)
)

# Avatr 12 IVI batch output (made up but plausible).
AVATR_BATCH = (
    "AVATR_12\n"
    "ICHU3200F2-ADV\n"
    "12\n"
    "31\n"
    "3.0.0\n"
    "3.0.0.300\n"
    "arm64-v8a\n"
    "zh-CN\n"
    "root\n"
    "2\n"
)


def test_parse_pura_phone_batch_is_not_avatr_not_root():
    info = parse_capabilities_batch("ABCDEF", PURA_BATCH)
    assert info.serial == "ABCDEF"
    assert info.model_name == "ABR-AL60"
    assert info.product_code == "HWABR"
    assert info.android_release == "12"
    assert info.android_api == 32
    assert info.harmonyos_version == "4.2.0"
    assert info.cpu_abi == "arm64-v8a"
    assert info.locale == "ru-RU"
    assert info.adbd_user == "shell"
    assert info.hdbd_count == 0
    caps = info.capabilities
    assert caps.is_root is False
    assert caps.has_hdc is False
    assert caps.is_avatr_ivi is False
    assert caps.is_harmony is True   # hw_sc.build.platform.version present
    assert caps.android_api == 32


def test_parse_avatr_ivi_batch_flags_avatr_and_root():
    info = parse_capabilities_batch("S1", AVATR_BATCH)
    assert info.product_code == "ICHU3200F2-ADV"
    assert info.label == "Avatr 12"
    caps = info.capabilities
    assert caps.is_root is True
    assert caps.has_hdc is True
    assert caps.is_avatr_ivi is True
    assert caps.is_harmony is True


def test_parse_handles_truncated_output():
    """Some busybox shells swallow newlines. We should not raise."""
    info = parse_capabilities_batch("S1", "ABR-AL60\n")
    assert info.model_name == "ABR-AL60"
    # Everything else should be None / defaults.
    assert info.android_release is None
    assert info.android_api is None
    assert info.adbd_user == ""
    assert info.capabilities.is_root is False


def test_parse_empty_output_is_safe():
    info = parse_capabilities_batch("S1", "")
    assert info.serial == "S1"
    assert info.model_name is None
    assert info.product_code is None
    assert info.capabilities.is_root is False


def test_parse_test_device_suffix():
    raw = (
        "ABR-test\nHWABR-QL\n12\n31\n\n4.0\narm64-v8a\nru-RU\nshell\n0\n"
    )
    info = parse_capabilities_batch("X", raw)
    assert info.product_code == "HWABR-QL"
    assert info.is_test_device is True


def test_parse_uses_fallback_product_when_getprop_empty():
    raw = (
        "Model X\n\n12\n31\n\n4.0\narm64-v8a\nen-US\nshell\n0\n"
    )
    info = parse_capabilities_batch("X", raw, fallback_product="ICHU3200F2-ADV")
    assert info.product_code == "ICHU3200F2-ADV"
    assert info.label == "Avatr 12"
    assert info.capabilities.is_avatr_ivi is True


def test_parse_invalid_sdk_falls_back_to_minus_one():
    raw = (
        "X\nHWABR\n12\nNOT_A_NUMBER\n\n4.0\narm64-v8a\n\n\n0\n"
    )
    info = parse_capabilities_batch("X", raw)
    assert info.android_api is None
    assert info.capabilities.android_api == -1


def test_detect_full_info_runs_one_shell_command(monkeypatch):
    """A single subprocess for the whole property batch — no per-prop calls."""
    from unittest.mock import patch

    from ivi_installer import adb as adb_module
    from ivi_installer.adb import AdbResult

    captured = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["serial"] = kwargs.get("serial")
        return AdbResult(args=args, exit_code=0, stdout=AVATR_BATCH, stderr="")

    with patch.object(adb_module, "run", side_effect=fake_run):
        info = detect_full_info("S1")

    assert captured["args"][0] == "shell"
    # Single concatenated script, one call only.
    assert "getprop ro.product.model" in captured["args"][1]
    assert info.capabilities.is_avatr_ivi is True


def test_detect_full_info_handles_failed_shell(monkeypatch):
    from unittest.mock import patch

    from ivi_installer import adb as adb_module
    from ivi_installer.adb import AdbResult

    with patch.object(adb_module, "run", return_value=AdbResult(
            args=("shell",), exit_code=1, stdout="", stderr="error")):
        info = detect_full_info("S1", fallback_product="ICHU3200F2-ADV")
    assert info.serial == "S1"
    assert info.product_code == "ICHU3200F2-ADV"
    assert info.label == "Avatr 12"
    assert info.capabilities.is_root is False


# ---- pm list users parsing ----

def test_parse_pm_users_typical():
    text = (
        "Users:\n"
        "\tUserInfo{0:Owner:13} running\n"
        "\tUserInfo{10:Driver:0}\n"
    )
    users = parse_pm_users(text)
    assert users == [
        AndroidUser(user_id=0, name="Owner", running=True),
        AndroidUser(user_id=10, name="Driver", running=False),
    ]


def test_parse_pm_users_no_flags():
    text = "Users:\n\tUserInfo{0:Owner} running\n"
    users = parse_pm_users(text)
    assert users == [AndroidUser(user_id=0, name="Owner", running=True)]


def test_parse_pm_users_empty_or_unparseable():
    assert parse_pm_users("") == []
    assert parse_pm_users("garbage line\nanother garbage\n") == []


def test_parse_pm_users_blank_name():
    text = "Users:\n\tUserInfo{42::0}\n"
    users = parse_pm_users(text)
    assert users == [AndroidUser(user_id=42, name="", running=False)]


# ---- categorize_screens ----

def test_categorize_screens_uses_named_buckets():
    """When `pm list users` returns clear role names, we honour them."""
    users = [
        AndroidUser(user_id=10, name="Driver", running=True),
        AndroidUser(user_id=11, name="Passenger", running=True),
        AndroidUser(user_id=12, name="Rear", running=True),
    ]
    cats = {c.key: c for c in categorize_screens(users)}
    assert cats["driver"].user_ids == (10,)
    assert cats["passenger"].user_ids == (11,)
    assert cats["rear"].user_ids == (12,)


def test_categorize_screens_fallback_when_names_are_opaque():
    """Real Deepal S09 case: names like 'NoLoginUser', 'NoLoginUser_4',
    '-1661017452' don't match any heuristic. Without the fallback all
    non-13 ids would land in 'rear', leaving 'passenger' empty and
    breaking the per-screen install UI. Fall back to the known DEEPAL
    layout instead."""
    users = [
        AndroidUser(user_id=0, name="Owner", running=True),
        AndroidUser(user_id=10, name="NoLoginUser", running=False),
        AndroidUser(user_id=11, name="NoLoginUser_6", running=True),
        AndroidUser(user_id=12, name="NoLoginUser_4", running=True),
        AndroidUser(user_id=13, name="-1661017452", running=True),
    ]
    cats = categorize_screens(users)
    assert cats == _FALLBACK_CATEGORIES
    cat_by_key = {c.key: c for c in cats}
    assert cat_by_key["driver"].user_ids == (13,)
    assert cat_by_key["passenger"].user_ids == (10, 12)
    assert cat_by_key["rear"].user_ids == (11,)


def test_categorize_screens_fallback_when_no_multimedia_users():
    """Probe returned only user 0 — no multimedia screens at all.
    Use fallback so the UI grid still renders."""
    users = [AndroidUser(user_id=0, name="Owner", running=True)]
    assert categorize_screens(users) == _FALLBACK_CATEGORIES


def test_categorize_screens_partial_naming_keeps_heuristics():
    """If at least one user has a recognisable role name, we trust the
    heuristics for the whole list — the fallback is reserved for the
    'no signal at all' case."""
    users = [
        AndroidUser(user_id=10, name="passenger", running=True),
        AndroidUser(user_id=11, name="opaque-name", running=True),
        AndroidUser(user_id=13, name="???", running=True),
    ]
    cats = {c.key: c for c in categorize_screens(users)}
    # Recognised by heuristic.
    assert 10 in cats["passenger"].user_ids
    # Not recognised + not the top id → defaults to rear.
    assert 11 in cats["rear"].user_ids
    # Not recognised, but is the top id → driver.
    assert 13 in cats["driver"].user_ids


def test_categorize_screens_dynamic_fallback_when_no_user_13():
    """Real non-Deepal case: car has main user 12 with no user 13. The
    old `_FALLBACK_CATEGORIES` would put driver=(13,) — a non-existent
    id — leaving the install grid pointing at nothing. Now the fallback
    treats ``max(ids)`` as driver and lumps everything else into rear
    (we have no name signal to split passenger from rear)."""
    users = [
        AndroidUser(user_id=10, name="opaque", running=True),
        AndroidUser(user_id=11, name="opaque", running=True),
        AndroidUser(user_id=12, name="opaque", running=True),
    ]
    cats = {c.key: c for c in categorize_screens(users)}
    assert cats["driver"].user_ids == (12,)
    assert cats["passenger"].user_ids == ()
    assert cats["rear"].user_ids == (10, 11)


def test_categorize_screens_dynamic_fallback_with_high_user_id():
    """If the device exposes a user above 13 (seen on some firmwares),
    treat it as the driver — not silently dropped by an upper-bound
    filter."""
    users = [
        AndroidUser(user_id=10, name="opaque", running=True),
        AndroidUser(user_id=14, name="opaque", running=True),
    ]
    cats = {c.key: c for c in categorize_screens(users)}
    assert cats["driver"].user_ids == (14,)
    assert cats["rear"].user_ids == (10,)


def test_categorize_screens_heuristic_top_id_is_driver_when_unnamed():
    """Even when one user has a clear name (kicks the heuristic path
    instead of the dynamic fallback), unrecognised non-top ids land in
    rear and the highest unrecognised id still claims driver."""
    users = [
        AndroidUser(user_id=10, name="passenger", running=True),
        AndroidUser(user_id=12, name="opaque", running=True),  # top id
        AndroidUser(user_id=11, name="opaque", running=True),
    ]
    cats = {c.key: c for c in categorize_screens(users)}
    assert cats["passenger"].user_ids == (10,)
    assert cats["driver"].user_ids == (12,)   # top id, not the magic 13
    assert cats["rear"].user_ids == (11,)


# ---- display probing (dumpsys-based, primary source) ----

# Trimmed real `dumpsys display` output from a Deepal S09 (D587).
# The format embeds the display id and panel name in a single token,
# e.g. "displayId 0control_panel" — that's what we're parsing.
_DEEPAL_S09_DUMPSYS_DISPLAY = (
    'DisplayDeviceInfo{", displayId 0control_panel": uniqueId="****"}\n'
    '    mBaseDisplayInfo=DisplayInfo{"control_panel", displayId 0", ...}\n'
    'DisplayDeviceInfo{", displayId 3hud_panel": uniqueId="****"}\n'
    '    mBaseDisplayInfo=DisplayInfo{"hud_panel", displayId 3", ...}\n'
    'DisplayDeviceInfo{", displayId 4central_rear_panel": uniqueId="****"}\n'
    '    mBaseDisplayInfo=DisplayInfo{"central_rear_panel", displayId 4", ...}\n'
    'DisplayDeviceInfo{", displayId 6co-driver_panel": uniqueId="****"}\n'
    '    mBaseDisplayInfo=DisplayInfo{"co-driver_panel", displayId 6", ...}\n'
)


# Realistic `dumpsys window displays` excerpt from the same machine —
# four blocks, each with the focus user we recorded live.
_DEEPAL_S09_DUMPSYS_WINDOW_DISPLAYS = """\
  Display: mDisplayId=0 rootTasks=1
  mCurrentFocus=Window{55627f7 u13 com.huawei.android.launcher/x.CockpitHomeLauncher}
  Display: mDisplayId=3 rootTasks=1
  mCurrentFocus=Window{2f7fbba u13 com.zejing.d587/com.zejing.d587.D587}
  Display: mDisplayId=4 rootTasks=1
  mCurrentFocus=Window{6bb7af1 u12 com.huawei.android.launcher/x.CockpitHomeLauncher}
    * ActivityRecord{6bb7af1 u12 com.huawei.android.launcher/x.CockpitHomeLauncher t1200001}
  Display: mDisplayId=6 rootTasks=1
  mCurrentFocus=Window{ab423fd u11 com.huawei.android.launcher/x.CockpitHomeLauncher}
    * ActivityRecord{ab423fd u11 com.huawei.android.launcher/x.CockpitHomeLauncher t1100002}
"""


def test_parse_display_names_deepal_s09():
    names = parse_display_names(_DEEPAL_S09_DUMPSYS_DISPLAY)
    assert names == {
        0: "control_panel",
        3: "hud_panel",
        4: "central_rear_panel",
        6: "co-driver_panel",
    }


def test_parse_focused_users_deepal_s09():
    focused = parse_focused_users(_DEEPAL_S09_DUMPSYS_WINDOW_DISPLAYS)
    assert focused == {0: 13, 3: 13, 4: 12, 6: 11}


def test_parse_focused_users_falls_back_to_activity_record():
    """When `mCurrentFocus` is missing / `null` (display OFF but with
    a rooted task), the parser falls back to the first ActivityRecord
    user in that block."""
    text = (
        "  Display: mDisplayId=4 rootTasks=1\n"
        "  mCurrentFocus=null\n"
        "    * ActivityRecord{xxx u12 com.huawei.android.launcher/x.Y t1}\n"
    )
    assert parse_focused_users(text) == {4: 12}


def test_build_displays_combines_name_and_focus():
    names = {0: "control_panel", 6: "co-driver_panel"}
    focused = {0: 13, 6: 11}
    out = build_displays(names, focused)
    assert out == [
        DisplayInfo(display_id=0, display_name="control_panel",
                    user_id=13, role="driver"),
        DisplayInfo(display_id=6, display_name="co-driver_panel",
                    user_id=11, role="passenger"),
    ]


def test_build_displays_unknown_panel_role_is_none():
    """A panel whose name doesn't match any known fragment ends up
    with role=None — the caller drops it from category resolution."""
    names = {99: "weird_panel_xyz"}
    out = build_displays(names, {99: 14})
    assert len(out) == 1
    assert out[0].role is None


def test_build_displays_off_display_keeps_user_id_none():
    """Display present in `dumpsys display` but with no focused window
    surfaces user_id=None. Categorizer will skip it."""
    names = {4: "central_rear_panel"}
    out = build_displays(names, {})  # nothing focused
    assert len(out) == 1
    assert out[0].user_id is None
    assert out[0].role == "rear"


# ---- categorize_screens with display-based input ----

def test_categorize_screens_uses_displays_over_user_names():
    """Display-based mapping is the primary source: opaque user names
    that would have driven the fallback layout (which has rear=11,
    passenger=10/12) are overridden by the actual display→user wiring
    on a Deepal S09 (passenger=11, rear=12)."""
    users = [
        AndroidUser(user_id=11, name="NoLoginUser_6", running=True),
        AndroidUser(user_id=12, name="NoLoginUser_4", running=True),
        AndroidUser(user_id=13, name="-1661017452", running=True),
    ]
    displays = [
        DisplayInfo(display_id=0, display_name="control_panel",
                    user_id=13, role="driver"),
        DisplayInfo(display_id=3, display_name="hud_panel",
                    user_id=13, role="hud"),
        DisplayInfo(display_id=4, display_name="central_rear_panel",
                    user_id=12, role="rear"),
        DisplayInfo(display_id=6, display_name="co-driver_panel",
                    user_id=11, role="passenger"),
    ]
    cats = {c.key: c for c in categorize_screens(users, displays=displays)}
    # Real S09 mapping — opposite of the hardcoded fallback for 11/12.
    assert cats["driver"].user_ids == (13,)
    assert cats["passenger"].user_ids == (11,)
    assert cats["rear"].user_ids == (12,)
    # HUD is collapsed into driver (same display group), no separate bucket.
    assert "hud" not in cats


def test_categorize_screens_falls_back_when_displays_lack_driver():
    """If the displays probe didn't surface a driver display (e.g. the
    parsing missed it), we go to user-name heuristics so the UI still
    renders something sensible."""
    users = [
        AndroidUser(user_id=11, name="Passenger", running=True),
        AndroidUser(user_id=12, name="Rear", running=True),
        AndroidUser(user_id=13, name="Driver", running=True),
    ]
    displays = [
        DisplayInfo(display_id=4, display_name="central_rear_panel",
                    user_id=12, role="rear"),
    ]  # no driver display in the list
    cats = {c.key: c for c in categorize_screens(users, displays=displays)}
    # Heuristics from user names take over.
    assert cats["driver"].user_ids == (13,)
    assert cats["passenger"].user_ids == (11,)
    assert cats["rear"].user_ids == (12,)


def test_categorize_screens_displays_empty_falls_back_cleanly():
    """When `displays=[]` (probe returned nothing), behaviour matches
    the no-displays path — heuristics, then fallback."""
    users = [AndroidUser(user_id=13, name="Driver", running=True)]
    cats = {c.key: c for c in categorize_screens(users, displays=[])}
    assert cats["driver"].user_ids == (13,)
