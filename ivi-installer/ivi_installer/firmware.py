"""Runtime detection of HwSAPT firmware profile.

The installer-package name and the set of multimedia-screen users differ
between Huawei IVI sub-brands and firmware generations:

  * Deepal (HarmonyOS 4.x / 5.x, API 31)  → ``com.huawei.appinstaller.car``
  * Avatr  (HarmonyOS 2.x, API 29)        → ``com.huawei.appmarket.vehicle``

Both of these are installed as system packages by the OEM. We probe the
device, pick the one that's actually present, and fall back to the
Deepal default if both are present (the original target of this tool).

Screen-user fan-out: Deepal S09 has 5 Android users (0/10/11/12/13)
with three multimedia displays + HUD; Avatr 11 typically has fewer.
We enumerate ``pm list users`` and pick every non-system user that
isn't headless/guest.

Adopted from the partner installer's ``_detect_firmware`` flow, but
extended with real probing instead of a static profile selector.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import adb

log = logging.getLogger(__name__)


# Order matters: Deepal first because that's the project's primary target.
# When both packages are reported as installed (rare; happens on re-flashed
# heads), we prefer the Deepal one. Apps installed via the wrong installer
# pkg fail with INSTALL_FAILED_INTERNAL_ERROR.
_INSTALLER_CANDIDATES: tuple[str, ...] = (
    "com.huawei.appinstaller.car",       # Deepal / HarmonyOS 4.x+
    "com.huawei.appmarket.vehicle",      # Avatr / HarmonyOS 2.x
)

# System users that never bind a multimedia screen — drop them from
# fan-out targets. User 0 is HEADLESS on Huawei automotive heads (it
# runs system services only), and "Guest" / "Restricted" are obvious.
_SYSTEM_USER_FLAGS: tuple[str, ...] = (
    "FLAG_GUEST",
    "FLAG_RESTRICTED",
    "FLAG_DEMO",
    "FLAG_EPHEMERAL",
)
_USER_LINE_RE = re.compile(r"UserInfo\{(\d+):([^:}]+):([0-9a-fA-FxX]+)\}")


@dataclass(frozen=True)
class FirmwareProfile:
    """Snapshot of car-specific install parameters.

    All fields are populated best-effort; defaults are baked in for the
    Deepal S09 / HwSAPT case so install paths still work when probing
    fails (e.g. on a transient adb drop).
    """
    installer_pkg: str
    screen_users: tuple[int, ...]
    api_level: int | None
    label: str

    @property
    def is_avatr(self) -> bool:
        return self.installer_pkg == "com.huawei.appmarket.vehicle"

    @property
    def is_deepal(self) -> bool:
        return self.installer_pkg == "com.huawei.appinstaller.car"


# Sane defaults that match the project's primary target. We use these
# when probing fails — the install pipeline still tries to do something
# useful instead of giving up.
DEFAULT_PROFILE = FirmwareProfile(
    installer_pkg="com.huawei.appinstaller.car",
    screen_users=(10, 11, 12, 13),
    api_level=31,
    label="Deepal / HarmonyOS 5.x (default)",
)


def detect(serial: str, *, timeout: int = 15) -> FirmwareProfile:
    """Probe the device and return its FirmwareProfile.

    Falls back to ``DEFAULT_PROFILE`` on probe failures. Never raises.
    """
    installer = _detect_installer_pkg(serial, timeout=timeout) or DEFAULT_PROFILE.installer_pkg
    users = _detect_screen_users(serial, timeout=timeout) or DEFAULT_PROFILE.screen_users
    api = _detect_api_level(serial, timeout=timeout)

    if installer == "com.huawei.appmarket.vehicle":
        label = f"Avatr / HarmonyOS 2.x (API {api or '?'})"
    elif installer == "com.huawei.appinstaller.car":
        if api and api >= 31:
            label = f"Deepal / HarmonyOS 4.x+ (API {api})"
        else:
            label = f"Deepal / HarmonyOS (API {api or '?'})"
    else:
        label = f"unknown installer={installer} (API {api or '?'})"

    return FirmwareProfile(
        installer_pkg=installer,
        screen_users=tuple(users),
        api_level=api,
        label=label,
    )


def _detect_installer_pkg(serial: str, *, timeout: int) -> str | None:
    """Pick the first known Huawei installer package that's installed."""
    r = adb.run("shell", "pm", "list", "packages",
                serial=serial, check=False, timeout=timeout)
    if r.exit_code != 0:
        return None
    present: set[str] = set()
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("package:"):
            present.add(line[len("package:"):])
    for cand in _INSTALLER_CANDIDATES:
        if cand in present:
            return cand
    return None


def _detect_screen_users(serial: str, *, timeout: int) -> tuple[int, ...] | None:
    """Return non-system user IDs from ``pm list users``.

    The output looks like::

        Users:
            UserInfo{0:Driver:c13} running
            UserInfo{10:NoLoginUser:410} running
            UserInfo{11:SECONDARY:1010} running
            UserInfo{12:com.huawei.usercreate:1010} running
            UserInfo{13:com.huawei.usercreate:1010} running

    User 0 is HEADLESS on Huawei automotive — it never binds a screen.
    Flag bits: ``0x400`` = FLAG_PROFILE, ``0x10`` = FLAG_RESTRICTED, etc.
    For our purposes "any non-zero user that's running" is sufficient.
    """
    r = adb.run("shell", "pm", "list", "users",
                serial=serial, check=False, timeout=timeout)
    if r.exit_code != 0:
        return None
    users: list[int] = []
    for line in (r.stdout or "").splitlines():
        m = _USER_LINE_RE.search(line)
        if not m:
            continue
        uid = int(m.group(1))
        if uid == 0:
            continue
        users.append(uid)
    return tuple(sorted(set(users))) if users else None


def _detect_api_level(serial: str, *, timeout: int) -> int | None:
    r = adb.run("shell", "getprop", "ro.build.version.sdk",
                serial=serial, check=False, timeout=timeout)
    if r.exit_code != 0:
        return None
    s = (r.stdout or "").strip()
    try:
        return int(s)
    except ValueError:
        return None
