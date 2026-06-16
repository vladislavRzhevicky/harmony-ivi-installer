"""Read and set the device timezone via standard Android commands.

All operations are non-destructive and reversible (just call set with
the previous IANA name) — see docs/11 «Разрешённые операции». No /system
writes, no reboots, no Huawei pkg disabling.

Source for the exact commands: AvatrInstaller.Core decompile §1260/1272.
"""
from __future__ import annotations

import logging

from . import adb

log = logging.getLogger(__name__)


def get_current(serial: str) -> str:
    """Return the device's current IANA timezone name.

    Primary: `cmd alarm get-time-zone` (the source of truth, reflects
    runtime alarm-service state).
    Fallback: `getprop persist.sys.timezone` (kept across reboots).
    Returns "UTC" if both come back empty.
    """
    primary = adb.run(
        "shell", "cmd", "alarm", "get-time-zone",
        serial=serial, check=False, timeout=10,
    ).stdout.strip()
    if primary and "/" in primary:
        return primary
    fallback = adb.run(
        "shell", "getprop", "persist.sys.timezone",
        serial=serial, check=False, timeout=10,
    ).stdout.strip()
    return fallback or "UTC"


def set_timezone(serial: str, tz: str) -> list[str]:
    """Apply an IANA timezone to the device.

    Runs three best-effort commands in sequence — each is allowed to
    fail individually. The actual outcome must be confirmed by the
    caller via `get_current(serial) == tz`.

        1. `cmd alarm set-time-zone <tz>` — alarm service via Binder IPC.
           This is the working path on modern Android / HarmonyOS where
           SELinux blocks step 2 for shell-user adbd.
        2. `setprop persist.sys.timezone <tz>` — works on AOSP / older
           builds and on rooted adbd. Denied by SELinux on production
           devices for shell user, which is fine — step 1 already did
           the real work.
        3. `am broadcast TIMEZONE_CHANGED` — wake any in-process
           listeners. Cosmetic.

    Returns a list of human-readable log lines describing each step.
    Raises AdbError only if all three fail with the adb transport itself
    (device disconnected); never on a per-command application error.
    """
    validate_iana_timezone(tz)
    lines: list[str] = []

    def _record(label: str, r) -> bool:
        if r.exit_code == 0:
            lines.append(f"{label} → ok")
            return True
        msg = (r.stderr or r.stdout).strip() or f"exit {r.exit_code}"
        msg = msg.splitlines()[0] if msg else msg
        lines.append(f"{label} → warn: {msg}")
        log.warning("%s failed: %s", label, msg)
        return False

    alarm = adb.run(
        "shell", "cmd", "alarm", "set-time-zone", tz,
        serial=serial, check=False, timeout=15,
    )
    _record(f"cmd alarm set-time-zone {tz}", alarm)

    # Legacy binder fallback: AlarmManagerService.setTimeZone is at
    # transaction code 3 historically. Some HarmonyOS / Huawei builds
    # ship without the `cmd alarm` subcommand but still expose the
    # service. Only try this when `cmd alarm` looks unrecognised, to
    # avoid firing two writes on healthy devices.
    haystack = (alarm.stderr + alarm.stdout).lower()
    if alarm.exit_code != 0 and (
        "unknown command" in haystack or "no such" in haystack
    ):
        legacy = adb.run(
            "shell", "service", "call", "alarm", "3", "s16", tz,
            serial=serial, check=False, timeout=15,
        )
        _record(f"service call alarm 3 s16 {tz}", legacy)

    prop = adb.run(
        "shell", "setprop", "persist.sys.timezone", tz,
        serial=serial, check=False, timeout=15,
    )
    _record(f"setprop persist.sys.timezone {tz}", prop)

    bcast = adb.run(
        "shell", "am", "broadcast",
        "-a", "android.intent.action.TIMEZONE_CHANGED",
        serial=serial, check=False, timeout=15,
    )
    _record("broadcast TIMEZONE_CHANGED", bcast)

    return lines


def validate_iana_timezone(tz: str) -> None:
    """Raise ValueError if `tz` is not a known IANA name.

    Uses the host's tzdata via stdlib `zoneinfo`. The IVI's own tzdata
    is a strict subset of what Python ships, so anything we accept here
    is also valid on the device.
    """
    import zoneinfo
    if tz not in zoneinfo.available_timezones():
        raise ValueError(f"Unknown IANA timezone: {tz!r}")
