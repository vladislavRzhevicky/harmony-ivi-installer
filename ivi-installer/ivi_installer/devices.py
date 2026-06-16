"""Detect connected IVI head units via `adb devices -l` plus a getprop
batch for capability detection.

Reference: docs/11 §5.2 + docs/12 §2 (carSlot.py:36-47).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import adb

log = logging.getLogger(__name__)


# ---- regex for product → vehicle classification ----

_AVATR_IVI_RE = re.compile(r"^ICHU\d+")
_TEST_BUILD_RE = re.compile(r"-(QL|EVT|DVT|PVT|TEST|ENG)$")


# Hardware-ID → human-readable model. Cross-checked against
# /tmp/pyqtTool/carSlot.py:25-46. Add new entries as new vehicles surface.
MODEL_MAP: dict[str, str] = {
    "ICHU3200F2-ADV":   "Avatr 12",
    "ICHU3200X2-ADV":   "Deepal S07 / X2",
    "ICHU3200E12-ADV":  "Avatr 12 (rev E)",
    "ICHU3100F123-ADV": "Avatr 11",
    "PRVC-F3":          "Avatr (F3 platform)",
    "SGLA-X6S":         "Avatr (X6 platform)",
    # Deepal IVI hardware codes (HwSAPT family, ALSK prefix).
    "ALSK-D587":        "Deepal S09",
    "ALSK-D507":        "Deepal S07",
    "ALSK-L507":        "Deepal L07",
    "ALSK-SL03":        "Deepal SL03",
}


# NOTE — deviates from docs/12 §2 on purpose.
# docs/12 suggests: r'([A-Z0-9]+)\s+device\s+product:(\S+)'  (ready-only).
# We keep an extended regex that also captures `offline / unauthorized /
# authorizing / connecting / ...` states because the UI status bar needs
# to display them (see main_window._update_status_bar). Should anything
# in device detection misbehave at runtime, this is the first place to
# revisit and fall back to the supplement's narrower form.
_DEVICE_LINE_RE = re.compile(
    r"^(?P<serial>\S+)\s+(?P<state>device|offline|unauthorized|"
    r"connecting|authorizing|recovery|sideload|bootloader|host|no\s+permissions)"
    r"(?:\s+(?P<rest>.*))?$"
)
_PRODUCT_RE = re.compile(r"product:(\S+)")
_TRANSPORT_RE = re.compile(r"transport_id:(\d+)")


@dataclass(frozen=True)
class Device:
    serial: str
    state: str                 # "device", "offline", "unauthorized", ...
    product: str | None        # raw e.g. "ICHU3200F2-ADV" — None for offline/unauth
    model: str | None          # human label from MODEL_MAP, or None when unknown
    transport_id: str | None

    @property
    def is_ready(self) -> bool:
        return self.state == "device"


def parse_devices_output(text: str) -> list[Device]:
    """Parse the output of `adb devices -l`.

    Skips the "List of devices attached" header and any blank lines.
    Tolerates extra whitespace and unknown trailing fields.
    """
    devices: list[Device] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        m = _DEVICE_LINE_RE.match(line)
        if not m:
            log.debug("skipping unrecognised devices line: %r", line)
            continue
        serial = m.group("serial")
        state = m.group("state").replace("  ", " ")
        rest = m.group("rest") or ""
        product_match = _PRODUCT_RE.search(rest)
        product = product_match.group(1) if product_match else None
        transport_match = _TRANSPORT_RE.search(rest)
        transport = transport_match.group(1) if transport_match else None
        devices.append(Device(
            serial=serial,
            state=state,
            product=product,
            model=MODEL_MAP.get(product) if product else None,
            transport_id=transport,
        ))
    return devices


def list_devices() -> list[Device]:
    """Run `adb devices -l` and return parsed Device objects (all states).

    Use `[d for d in list_devices() if d.is_ready]` to filter to only
    those usable for installation.
    """
    result = adb.run("devices", "-l", check=False, timeout=10)
    if result.exit_code != 0:
        log.warning("adb devices -l exit=%d stderr=%r",
                    result.exit_code, result.stderr.strip())
        return []
    return parse_devices_output(result.stdout)


def detect_first() -> Device | None:
    """Return the first ready device, or None."""
    for d in list_devices():
        if d.is_ready:
            return d
    return None


def describe_model(product: str | None) -> str:
    """Human-friendly label for a hardware product id.

    Unknown products fall through to the raw id; missing product reports
    "Unknown device".
    """
    if not product:
        return "Unknown device"
    return MODEL_MAP.get(product, product)


# ---- DeviceInfo / DeviceCapabilities (rich detail for the status panel) ----

@dataclass(frozen=True)
class DeviceCapabilities:
    """Boolean / numeric flags the rest of the UI keys off of."""
    is_root: bool          # whoami == 'root'
    has_hdc: bool          # hdbd process visible (Huawei IVI)
    is_harmony: bool       # any HarmonyOS-version property is non-empty
    is_avatr_ivi: bool     # product matches r'^ICHU\d+'
    android_api: int       # ro.build.version.sdk (-1 if unknown)


@dataclass(frozen=True)
class DeviceInfo:
    """Everything the status panel knows about a single connected device.

    Filled by `detect_full_info(serial)`. Fields default to None / empty
    when the corresponding property is unset on the device.
    """
    serial: str
    state: str = "device"
    product_code: str | None = None       # ro.product.device or `adb devices -l`
    model_name: str | None = None         # ro.product.model
    label: str = ""                        # MODEL_MAP[product_code] or fallback
    android_release: str | None = None    # e.g. "12"
    android_api: int | None = None        # int from ro.build.version.sdk
    harmonyos_version: str | None = None  # combined: ro.build.version.harmonyos OR hw_sc.build.platform.version
    cpu_abi: str | None = None
    locale: str | None = None
    adbd_user: str = ""                    # whoami
    hdbd_count: int = 0                    # ps -A | grep -c hdbd
    is_test_device: bool = False           # product code matches test-build suffix
    capabilities: DeviceCapabilities = field(
        default_factory=lambda: DeviceCapabilities(
            is_root=False, has_hdc=False, is_harmony=False,
            is_avatr_ivi=False, android_api=-1,
        )
    )


# Single shell-script run on the device. Each line corresponds to one
# property. The order MUST match the parsing in _parse_capabilities_batch.
_BATCH_SCRIPT = (
    "getprop ro.product.model;"
    "getprop ro.product.device;"
    "getprop ro.build.version.release;"
    "getprop ro.build.version.sdk;"
    "getprop ro.build.version.harmonyos;"
    "getprop hw_sc.build.platform.version;"
    "getprop ro.product.cpu.abi;"
    "getprop persist.sys.locale;"
    "whoami;"
    "ps -A | grep -c hdbd"
)


def _parse_int_safe(value: str, default: int = -1) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _none_if_empty(value: str) -> str | None:
    v = (value or "").strip()
    return v or None


def parse_capabilities_batch(serial: str, raw_output: str,
                              fallback_product: str | None = None) -> DeviceInfo:
    """Parse the 10-line output of `_BATCH_SCRIPT` into a DeviceInfo.

    The number of lines on the device may vary (e.g. when a getprop
    silently swallows newlines on busybox). We pad with empty strings
    so out-of-bounds is impossible.

    `fallback_product` is the product string from `adb devices -l`,
    used when the device's own `getprop ro.product.device` is empty.
    """
    lines = [line.rstrip("\r") for line in raw_output.splitlines()]
    while len(lines) < 10:
        lines.append("")

    (
        model_name, product_device, android_release, android_sdk,
        harmonyos_v1, harmonyos_v2, cpu_abi, locale, user, hdbd_str,
    ) = lines[:10]

    product_code = _none_if_empty(product_device) or fallback_product
    api = _parse_int_safe(android_sdk, -1)
    api_or_none = api if api >= 0 else None
    harmonyos = _none_if_empty(harmonyos_v1) or _none_if_empty(harmonyos_v2)
    user_clean = (user or "").strip()
    hdbd_count = max(0, _parse_int_safe(hdbd_str, 0))

    is_test = bool(product_code and _TEST_BUILD_RE.search(product_code))
    is_avatr = bool(product_code and _AVATR_IVI_RE.match(product_code))
    is_harmony = harmonyos is not None
    is_root = user_clean == "root"
    has_hdc = hdbd_count > 0

    label = describe_model(product_code) if product_code else (model_name or "")

    capabilities = DeviceCapabilities(
        is_root=is_root,
        has_hdc=has_hdc,
        is_harmony=is_harmony,
        is_avatr_ivi=is_avatr,
        android_api=api if api >= 0 else -1,
    )

    return DeviceInfo(
        serial=serial,
        state="device",
        product_code=product_code,
        model_name=_none_if_empty(model_name),
        label=label,
        android_release=_none_if_empty(android_release),
        android_api=api_or_none,
        harmonyos_version=harmonyos,
        cpu_abi=_none_if_empty(cpu_abi),
        locale=_none_if_empty(locale),
        adbd_user=user_clean,
        hdbd_count=hdbd_count,
        is_test_device=is_test,
        capabilities=capabilities,
    )


# ---- Android users on the device ------------------------------------------

@dataclass(frozen=True)
class AndroidUser:
    user_id: int
    name: str
    running: bool


_USER_LINE_RE = re.compile(
    r"UserInfo\{(?P<id>\d+):(?P<name>[^:}]*)(?::(?P<flags>[0-9a-fA-Fx]+))?\}"
    r"(?P<rest>.*)$"
)


def parse_pm_users(text: str) -> list[AndroidUser]:
    """Parse `pm list users` output into AndroidUser records.

    Sample input::

        Users:
            UserInfo{0:Owner:13} running
            UserInfo{10:Driver:0}
    """
    users: list[AndroidUser] = []
    for line in text.splitlines():
        m = _USER_LINE_RE.search(line)
        if not m:
            continue
        try:
            uid = int(m.group("id"))
        except ValueError:
            continue
        running = "running" in (m.group("rest") or "").lower()
        users.append(AndroidUser(
            user_id=uid,
            name=(m.group("name") or "").strip(),
            running=running,
        ))
    return users


# ---- Physical displays + role mapping -------------------------------------

@dataclass(frozen=True)
class DisplayInfo:
    """One physical display in the IVI cabin and the user that paints it.

    `display_name` comes from `dumpsys display` (e.g. "control_panel",
    "co-driver_panel", "central_rear_panel", "hud_panel"). `user_id`
    is the Android user id of the activity currently rooted on that
    display (driver=13 on a 4-screen Deepal), pulled from
    `dumpsys window displays`. `role` is our normalised tag.
    """
    display_id: int
    display_name: str
    user_id: int | None       # None when no activity is bound (display OFF)
    role: str | None          # "driver" | "passenger" | "rear" | "hud" | None


# Map known display-name fragments to roles. Order matters: more
# specific patterns first ("co-driver" must be matched before plain
# "driver"). Names are matched case-insensitive.
_DISPLAY_ROLE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("co-driver", "passenger"),
    ("co_driver", "passenger"),
    ("codriver", "passenger"),
    ("front_passenger", "passenger"),
    ("passenger", "passenger"),
    ("central_rear", "rear"),
    ("rear_seat", "rear"),
    ("rear", "rear"),
    ("backseat", "rear"),
    ("hud", "hud"),
    ("control_panel", "driver"),
    ("driver", "driver"),
    ("main_panel", "driver"),
)


def _classify_display_name(name: str) -> str | None:
    """Return "driver" / "passenger" / "rear" / "hud" or None for `name`.

    Only returns a role when the name matches a known fragment.
    Unknown names → None so the caller can drop them or log.
    """
    n = (name or "").lower()
    for fragment, role in _DISPLAY_ROLE_PATTERNS:
        if fragment in n:
            return role
    return None


# `dumpsys display` paints DisplayDeviceInfo blocks with a header that
# concatenates the displayId and the display name without a separator,
# e.g. `displayId 3hud_panel": ...`. We capture both halves.
_DISPLAY_NAME_RE = re.compile(
    r'displayId (?P<id>\d+)(?P<name>[a-zA-Z][a-zA-Z0-9_\-]*)"',
)


def parse_display_names(dumpsys_display: str) -> dict[int, str]:
    """Parse display id → display name map from `dumpsys display` output."""
    out: dict[int, str] = {}
    for m in _DISPLAY_NAME_RE.finditer(dumpsys_display):
        try:
            did = int(m.group("id"))
        except ValueError:
            continue
        name = m.group("name").strip()
        # Don't overwrite once captured — first hit per display id is
        # the DisplayDeviceInfo header; later mentions in
        # mBaseDisplayInfo / mOverrideDisplayInfo repeat the same name.
        out.setdefault(did, name)
    return out


# `dumpsys window displays` lays out per-display blocks; we walk it
# imperatively and remember the active display id as we hit each
# `Display: mDisplayId=N` header. Within a block we look for the first
# `mCurrentFocus=Window{... uN ...}` (preferred) or the first
# `ActivityRecord{... uN ...}` (fallback when the display has no focus
# but does have a rooted task — i.e. display is OFF but holds state).
_WD_DISPLAY_HEADER_RE = re.compile(r"Display: mDisplayId=(\d+)")
_WD_FOCUS_USER_RE = re.compile(r"mCurrentFocus=Window\{\S+ u(\d+) ")
_WD_ACTIVITY_USER_RE = re.compile(r"ActivityRecord\{\S+ u(\d+) ")


def parse_focused_users(dumpsys_window_displays: str) -> dict[int, int]:
    """Parse display id → user id from `dumpsys window displays` output.

    Prefers `mCurrentFocus=Window{u<N>}`; falls back to the first
    `ActivityRecord{u<N>}` in that display's block. Displays without
    any of those are omitted.
    """
    out: dict[int, int] = {}
    fallback: dict[int, int] = {}
    current: int | None = None
    for line in dumpsys_window_displays.splitlines():
        m = _WD_DISPLAY_HEADER_RE.search(line)
        if m:
            try:
                current = int(m.group(1))
            except ValueError:
                current = None
            continue
        if current is None:
            continue
        if current not in out:
            mu = _WD_FOCUS_USER_RE.search(line)
            if mu:
                try:
                    out[current] = int(mu.group(1))
                    continue
                except ValueError:
                    pass
        if current not in fallback:
            ma = _WD_ACTIVITY_USER_RE.search(line)
            if ma:
                try:
                    fallback[current] = int(ma.group(1))
                except ValueError:
                    pass
    # Merge: focus wins, activity-record fills gaps.
    for did, uid in fallback.items():
        out.setdefault(did, uid)
    return out


def build_displays(
    names: dict[int, str], focused: dict[int, int],
) -> list[DisplayInfo]:
    """Combine name and focused-user maps into a sorted DisplayInfo list."""
    out: list[DisplayInfo] = []
    for did in sorted(names):
        nm = names[did]
        out.append(DisplayInfo(
            display_id=did,
            display_name=nm,
            user_id=focused.get(did),
            role=_classify_display_name(nm),
        ))
    return out


def list_displays(serial: str) -> list[DisplayInfo]:
    """Probe the device for physical displays and the user driving each one.

    Two adb shell calls (`dumpsys display` + `dumpsys window displays`).
    Returns [] on adb errors so the caller can fall back to name-based
    heuristics. Errors are logged at WARNING.
    """
    disp = adb.run("shell", "dumpsys", "display",
                   serial=serial, check=False, timeout=15)
    if disp.exit_code != 0:
        log.warning("dumpsys display failed (serial=%s, exit=%d)",
                    serial, disp.exit_code)
        return []
    win = adb.run("shell", "dumpsys", "window", "displays",
                  serial=serial, check=False, timeout=15)
    if win.exit_code != 0:
        log.warning("dumpsys window displays failed (serial=%s, exit=%d)",
                    serial, win.exit_code)
        return []
    names = parse_display_names(disp.stdout)
    focused = parse_focused_users(win.stdout)
    return build_displays(names, focused)


@dataclass(frozen=True)
class ScreenCategory:
    """A logical screen category in the IVI cabin (driver / passenger / rear).

    The cabin has up to four multimedia user-screens behind ids 10–13;
    physical role assignment varies by car model and firmware. This
    record holds the category name we expose in the UI and the resolved
    list of Android user ids that belong to it (with sensible labels
    pulled from the device's own `pm list users` names when available).
    """
    key: str                          # "driver" | "passenger" | "rear"
    label: str                        # "Driver", "Passenger", "Rear"
    user_ids: tuple[int, ...]         # one or more Android user ids
    user_labels: tuple[str, ...] = () # human names from the device, if known


# Hardcoded fallback used when the device returned no multimedia users
# at all (probe failed entirely, or the user list only had user 0).
# Driver=13 is just the most common id on Huawei IVI; the layout below
# matches Deepal S09 and is wrong on cars with main user 10/12/14/etc.,
# so we only fall back to it when we have *no* live ids to work with.
_FALLBACK_CATEGORIES: tuple[ScreenCategory, ...] = (
    ScreenCategory(key="driver",    label="Driver",    user_ids=(13,)),
    ScreenCategory(key="passenger", label="Passenger", user_ids=(10, 12)),
    ScreenCategory(key="rear",      label="Rear",      user_ids=(11,)),
)


def _dynamic_fallback(
    multimedia: list["AndroidUser"],
) -> tuple[ScreenCategory, ...]:
    """Build categories from real ids when displays + names give nothing.

    The highest id in the multimedia set goes to ``driver`` (mirrors
    the competitor's ``car_user_id = max(running non-zero)`` heuristic
    and ``_resolve_packageinstaller_user``). Every other id falls into
    ``rear`` — without role signal we can't tell passenger from rear,
    and putting them all in one bucket beats inventing a wrong split
    (the previous hardcoded ``passenger=(10,12), rear=(11,)`` mirror
    image silently swapped seats on non-S09 cars).

    Returns the canonical hardcoded layout when ``multimedia`` contains
    user 13 *and* its ids are a subset of {10,11,12,13} — that's the
    Deepal S09 case where the historical layout is known to be correct.
    """
    if not multimedia:
        return _FALLBACK_CATEGORIES
    ids = sorted({u.user_id for u in multimedia})
    if 13 in ids and set(ids).issubset({10, 11, 12, 13}):
        return _FALLBACK_CATEGORIES
    top = max(ids)
    others = tuple(uid for uid in ids if uid != top)
    name_by_id = {u.user_id: u.name or f"user {u.user_id}" for u in multimedia}
    return (
        ScreenCategory(key="driver",    label="Driver",
                       user_ids=(top,),
                       user_labels=(name_by_id[top],)),
        ScreenCategory(key="passenger", label="Passenger", user_ids=()),
        ScreenCategory(key="rear",      label="Rear",
                       user_ids=others,
                       user_labels=tuple(name_by_id[u] for u in others)),
    )


def _categories_from_displays(
    displays: list[DisplayInfo],
    users: list[AndroidUser],
) -> tuple[ScreenCategory, ...] | None:
    """Build categories from the display→user mapping.

    Preferred over user-name heuristics because display names
    (control_panel / co-driver_panel / central_rear_panel) are
    fixed by the firmware and don't depend on whether
    `pm list users` reports human-friendly user names.

    Returns None if the displays probe didn't give us at least
    one role-resolvable display with a known user — caller will
    fall back to user-name heuristics.
    """
    if not displays:
        return None

    # role -> ordered list of (user_id, label) pairs.
    buckets: dict[str, list[tuple[int, str]]] = {
        "driver": [], "passenger": [], "rear": [],
    }
    name_by_id = {u.user_id: u.name for u in users}
    seen_per_role: dict[str, set[int]] = {
        "driver": set(), "passenger": set(), "rear": set(),
    }
    for d in displays:
        if d.role is None or d.role == "hud":
            # HUD shares its user with the driver display (same display
            # group), so don't duplicate it as its own bucket.
            continue
        if d.user_id is None:
            continue
        if d.role not in buckets:
            continue
        if d.user_id in seen_per_role[d.role]:
            continue
        seen_per_role[d.role].add(d.user_id)
        label = name_by_id.get(d.user_id) or f"user {d.user_id}"
        buckets[d.role].append((d.user_id, label))

    # Need at least the driver bucket populated to trust this path —
    # otherwise the probe didn't surface anything actionable.
    if not buckets["driver"]:
        return None

    out: list[ScreenCategory] = []
    for key, label in (("driver", "Driver"),
                        ("passenger", "Passenger"),
                        ("rear", "Rear")):
        entries = sorted(buckets[key])
        if not entries:
            out.append(ScreenCategory(key=key, label=label, user_ids=()))
        else:
            out.append(ScreenCategory(
                key=key, label=label,
                user_ids=tuple(uid for uid, _ in entries),
                user_labels=tuple(name for _, name in entries),
            ))
    return tuple(out)


def categorize_screens(
    users: list[AndroidUser],
    displays: list[DisplayInfo] | None = None,
) -> tuple[ScreenCategory, ...]:
    """Group multimedia-screen users into Driver / Passenger / Rear.

    Resolution order:

    1. **Display-based mapping** (when ``displays`` is non-empty and at
       least the driver display is identified). This is the most
       reliable source on Huawei IVI: physical display names like
       ``control_panel`` / ``co-driver_panel`` / ``central_rear_panel``
       are fixed by the firmware, and `dumpsys window displays` tells
       us which Android user owns each display.
    2. **User-name heuristics** on `pm list users`:
       * "driver" / "front" / "main"            → driver
       * "passenger" / "front passenger" / "co" → passenger
       * "rear" / "back" / "passenger 2"        → rear
    3. **Hardcoded `_FALLBACK_CATEGORIES`** when the device returned
       nothing usable from either source — guarantees that the UI
       still has a working three-bucket layout for Deepal S09.
    """
    if displays:
        from_displays = _categories_from_displays(displays, users)
        if from_displays is not None:
            return from_displays

    # Non-system users only. The upper bound used to be 13 on the
    # assumption that the cabin set is always {10..13} — different cars
    # have been seen with ids above 13 (and with main user as low as 10
    # without a 13 at all), so we drop the cap and key off the highest
    # live id below.
    multimedia = [u for u in users if u.user_id >= 10]
    if not multimedia:
        return _FALLBACK_CATEGORIES

    # The highest id is treated as the driver in unsigned-name fallbacks.
    # Mirrors the competitor's `car_user_id = max(running non-zero)` and
    # the `_resolve_packageinstaller_user` logic on the strategy side, so
    # the UI bucket and the install target stay consistent.
    top_id = max(u.user_id for u in multimedia)

    # If `pm list users` returned only opaque names (e.g. "NoLoginUser",
    # "NoLoginUser_4", "-1661017452"), heuristics can't tell driver from
    # passenger from rear. Without this branch every unclassified id
    # except `top_id` would land in `rear`, leaving Passenger empty —
    # the historical Deepal S09 case. Build a fallback that works for
    # any id set instead of pinning to (10,11,12,13).
    if all(_classify_user_name(u.name) is None for u in multimedia):
        return _dynamic_fallback(multimedia)

    buckets: dict[str, list[tuple[int, str]]] = {
        "driver": [], "passenger": [], "rear": [],
    }
    for u in multimedia:
        bucket = _classify_user_name(u.name)
        if bucket is None:
            # Unknown name shape: highest id defaults to driver, rest to rear.
            bucket = "driver" if u.user_id == top_id else "rear"
        buckets[bucket].append((u.user_id, u.name or f"user {u.user_id}"))

    # If after classification the driver bucket ended up empty but the
    # top id is present, force it into driver — without role signal that
    # is our best single-id guess across every Huawei IVI we've seen.
    if not buckets["driver"]:
        for u in multimedia:
            if u.user_id == top_id:
                buckets["driver"].append(
                    (u.user_id, u.name or f"user {u.user_id}"))
                for key in ("passenger", "rear"):
                    buckets[key] = [
                        e for e in buckets[key] if e[0] != top_id
                    ]
                break

    out: list[ScreenCategory] = []
    for key, label in (("driver", "Driver"),
                        ("passenger", "Passenger"),
                        ("rear", "Rear")):
        entries = sorted(buckets[key])
        if not entries:
            # Skip empty buckets — but still give callers a placeholder
            # so the UI stays a 1×3 grid even on degenerate device sets.
            out.append(ScreenCategory(key=key, label=label, user_ids=()))
        else:
            out.append(ScreenCategory(
                key=key, label=label,
                user_ids=tuple(uid for uid, _ in entries),
                user_labels=tuple(name for _, name in entries),
            ))
    # If every category is empty (all multimedia users had blank names
    # AND none was id 13), the heuristic produced nothing useful — fall
    # back to the known layout.
    if all(not c.user_ids for c in out):
        return _FALLBACK_CATEGORIES
    return tuple(out)


_DRIVER_HINTS = ("driver", "main", "front")
_PASSENGER_HINTS = ("passenger", "co-driver", "co_driver", "codriver")
_REAR_HINTS = ("rear", "back", "second row", "row 2", "row2")


def _classify_user_name(name: str) -> str | None:
    """Return "driver" / "passenger" / "rear" if `name` clearly matches.

    Returns None for ambiguous / blank names so the caller can pick a
    sensible default instead of forcing a random bucket.
    """
    n = (name or "").strip().lower()
    if not n:
        return None
    # Order matters: "passenger" must be checked before "rear" because a
    # name like "rear passenger" should land in passenger only when the
    # caller wants — here we treat it as rear (more specific).
    for hint in _REAR_HINTS:
        if hint in n:
            return "rear"
    for hint in _PASSENGER_HINTS:
        if hint in n:
            return "passenger"
    for hint in _DRIVER_HINTS:
        if hint in n:
            return "driver"
    return None


def list_android_users(serial: str) -> list[AndroidUser]:
    """Run `pm list users` on the device and parse the result.

    Returns an empty list on adb errors so callers can fall back to a
    static default set.
    """
    result = adb.run("shell", "pm", "list", "users", serial=serial,
                      check=False, timeout=10)
    if result.exit_code != 0:
        log.warning("pm list users failed (serial=%s, exit=%d): %s",
                    serial, result.exit_code, result.stderr.strip())
        return []
    return parse_pm_users(result.stdout)


def detect_full_info(serial: str, fallback_product: str | None = None) -> DeviceInfo:
    """Run the property batch on `serial` and return a populated DeviceInfo.

    Uses one `adb shell` invocation to keep latency low. On failure
    returns a DeviceInfo with bare fields and `state="device"` so the
    UI still has a serial to show.
    """
    result = adb.run("shell", _BATCH_SCRIPT, serial=serial,
                      check=False, timeout=10)
    if result.exit_code != 0:
        log.warning("capabilities batch failed (serial=%s, exit=%d): %s",
                    serial, result.exit_code, result.stderr.strip())
        return DeviceInfo(serial=serial, state="device",
                           product_code=fallback_product,
                           label=describe_model(fallback_product))
    return parse_capabilities_batch(serial, result.stdout,
                                    fallback_product=fallback_product)
