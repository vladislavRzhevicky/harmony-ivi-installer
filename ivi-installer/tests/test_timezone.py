"""Unit tests for ivi_installer/timezone.py."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from ivi_installer import adb, timezone
from ivi_installer.adb import AdbResult


def _ok(stdout: str = "", stderr: str = "", exit_code: int = 0) -> AdbResult:
    return AdbResult(args=(), exit_code=exit_code, stdout=stdout, stderr=stderr)


# ---- get_current ----

def test_get_current_uses_alarm_primary():
    with patch.object(adb, "run", return_value=_ok(stdout="Asia/Shanghai\n")) as m:
        assert timezone.get_current("S1") == "Asia/Shanghai"
    assert m.call_count == 1
    assert m.call_args.args[:4] == ("shell", "cmd", "alarm", "get-time-zone")


def test_get_current_falls_back_to_getprop():
    side = [
        _ok(stdout="\n"),                       # cmd alarm empty
        _ok(stdout="Europe/Moscow\n"),          # getprop fallback
    ]
    with patch.object(adb, "run", side_effect=side):
        assert timezone.get_current("S1") == "Europe/Moscow"


def test_get_current_rejects_non_iana_from_alarm():
    """`cmd alarm` may print 'CST' or 'GMT' on weird devices — we ignore."""
    side = [
        _ok(stdout="CST\n"),                    # not IANA (no '/')
        _ok(stdout="Asia/Shanghai\n"),
    ]
    with patch.object(adb, "run", side_effect=side):
        assert timezone.get_current("S1") == "Asia/Shanghai"


def test_get_current_returns_utc_when_both_empty():
    side = [_ok(stdout="\n"), _ok(stdout="\n")]
    with patch.object(adb, "run", side_effect=side):
        assert timezone.get_current("S1") == "UTC"


# ---- set_timezone ----

def test_set_timezone_runs_three_commands_in_order():
    side = [_ok(), _ok(), _ok()]
    with patch.object(adb, "run", side_effect=side) as m:
        lines = timezone.set_timezone("S1", "Europe/Moscow")
    cmds = [c.args for c in m.call_args_list]
    assert cmds[0][:4] == ("shell", "cmd", "alarm", "set-time-zone")
    assert cmds[0][4] == "Europe/Moscow"
    assert cmds[1][:3] == ("shell", "setprop", "persist.sys.timezone")
    assert cmds[1][3] == "Europe/Moscow"
    assert cmds[2][:2] == ("shell", "am")
    assert "android.intent.action.TIMEZONE_CHANGED" in cmds[2]
    assert any("ok" in line for line in lines)


def test_set_timezone_tolerates_alarm_failure():
    """`cmd alarm` may not exist on older builds; we proceed anyway."""
    side = [
        _ok(stderr="cmd: not found\n", exit_code=1),  # alarm fails
        _ok(),                                          # setprop OK
        _ok(),                                          # broadcast OK
    ]
    with patch.object(adb, "run", side_effect=side):
        lines = timezone.set_timezone("S1", "Europe/Moscow")
    assert any("warn" in line for line in lines)
    assert any("setprop" in line and "ok" in line for line in lines)


def test_set_timezone_tolerates_setprop_denied():
    """On HarmonyOS / production Android, SELinux blocks setprop for the
    shell user — `cmd alarm` does the real work. Don't raise; the caller
    will verify via re-read."""
    side = [
        _ok(),                                          # alarm OK
        _ok(stderr="Failed to set property 'persist.sys.timezone'\n",
             exit_code=1),                              # setprop denied
        _ok(),                                          # broadcast OK
    ]
    with patch.object(adb, "run", side_effect=side):
        lines = timezone.set_timezone("S1", "Europe/Moscow")
    # All three commands ran, no raise.
    assert any("cmd alarm" in line and "ok" in line for line in lines)
    assert any("setprop" in line and "warn" in line for line in lines)


def test_set_timezone_uses_binder_fallback_when_alarm_unknown():
    """HarmonyOS NEXT: `cmd alarm` exists but has no set-time-zone
    subcommand. We then try `service call alarm 3 s16 <tz>` — the
    historical AlarmManagerService.setTimeZone transaction code."""
    side = [
        _ok(stdout="Unknown command: set-time-zone\n", exit_code=255),
        _ok(stdout="Result: Parcel(00000000)\n"),       # service call OK
        _ok(stderr="SELinux denied\n", exit_code=1),    # setprop denied
        _ok(),                                           # broadcast OK
    ]
    with patch.object(adb, "run", side_effect=side) as m:
        lines = timezone.set_timezone("S1", "Europe/Moscow")
    assert m.call_count == 4
    cmds = [c.args for c in m.call_args_list]
    assert cmds[1][:5] == ("shell", "service", "call", "alarm", "3")
    assert cmds[1][5] == "s16"
    assert cmds[1][6] == "Europe/Moscow"
    assert any("service call alarm" in line and "ok" in line for line in lines)


def test_set_timezone_skips_binder_fallback_when_cmd_alarm_works():
    """On healthy Android 12 — `cmd alarm` succeeds, so we don't
    double-write via the legacy binder path."""
    side = [_ok(), _ok(), _ok()]
    with patch.object(adb, "run", side_effect=side) as m:
        timezone.set_timezone("S1", "Europe/Moscow")
    assert m.call_count == 3
    cmds = [c.args for c in m.call_args_list]
    assert all(c[:3] != ("shell", "service", "call") for c in cmds)


def test_set_timezone_runs_all_three_even_when_each_fails():
    """Worst case: every command errors; we still return lines and let
    the caller verify by re-reading. Used to surface 'commands sent but
    device unchanged' to the UI without blowing up."""
    side = [
        _ok(stderr="x\n", exit_code=1),
        _ok(stderr="y\n", exit_code=1),
        _ok(stderr="z\n", exit_code=1),
    ]
    with patch.object(adb, "run", side_effect=side) as m:
        lines = timezone.set_timezone("S1", "Europe/Moscow")
    assert m.call_count == 3
    assert sum(1 for line in lines if "warn" in line) == 3


def test_set_timezone_rejects_invalid_iana():
    with patch.object(adb, "run") as m:
        with pytest.raises(ValueError, match="Unknown IANA"):
            timezone.set_timezone("S1", "Mars/Olympus_Mons")
    assert m.call_count == 0  # no adb commands fired


def test_validate_iana_accepts_known_zones():
    timezone.validate_iana_timezone("Europe/Moscow")
    timezone.validate_iana_timezone("Asia/Shanghai")
    timezone.validate_iana_timezone("UTC")


def test_validate_iana_rejects_typos():
    with pytest.raises(ValueError):
        timezone.validate_iana_timezone("Europe/moscow")  # case-sensitive
    with pytest.raises(ValueError):
        timezone.validate_iana_timezone("Russia/Moscow")
    with pytest.raises(ValueError):
        timezone.validate_iana_timezone("")
