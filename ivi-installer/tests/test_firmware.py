"""Tests for ivi_installer.firmware — runtime profile detection."""
from __future__ import annotations

from unittest.mock import patch

from ivi_installer import adb, firmware


def _ok(stdout: str = "", stderr: str = "") -> adb.AdbResult:
    return adb.AdbResult(args=(), exit_code=0, stdout=stdout, stderr=stderr)


def _fail() -> adb.AdbResult:
    return adb.AdbResult(args=(), exit_code=1, stdout="", stderr="boom")


# ---- installer pkg ---------------------------------------------------------

def test_detect_picks_deepal_when_appinstaller_car_present():
    pkgs = "\n".join([
        "package:android",
        "package:com.huawei.appinstaller.car",
        "package:com.example.x",
    ])
    users = (
        "Users:\n"
        "    UserInfo{0:Driver:c13} running\n"
        "    UserInfo{10:NoLoginUser:410} running\n"
        "    UserInfo{13:active:1010} running\n"
    )
    def fake_run(*args, **kwargs):
        if args[1] == "pm" and args[2] == "list" and args[3] == "packages":
            return _ok(stdout=pkgs)
        if args[1] == "pm" and args[2] == "list" and args[3] == "users":
            return _ok(stdout=users)
        if args[1] == "getprop":
            return _ok(stdout="31\n")
        return _ok()
    with patch.object(adb, "run", side_effect=fake_run):
        profile = firmware.detect("S1")
    assert profile.installer_pkg == "com.huawei.appinstaller.car"
    assert profile.is_deepal
    assert not profile.is_avatr
    assert profile.api_level == 31
    assert profile.screen_users == (10, 13)


def test_detect_picks_avatr_when_only_appmarket_vehicle_present():
    pkgs = "package:com.huawei.appmarket.vehicle\n"
    def fake_run(*args, **kwargs):
        if args[1] == "pm" and args[2] == "list" and args[3] == "packages":
            return _ok(stdout=pkgs)
        if args[1] == "pm" and args[2] == "list" and args[3] == "users":
            return _ok(stdout="UserInfo{10:foo:0} running\n")
        if args[1] == "getprop":
            return _ok(stdout="29\n")
        return _ok()
    with patch.object(adb, "run", side_effect=fake_run):
        profile = firmware.detect("S1")
    assert profile.installer_pkg == "com.huawei.appmarket.vehicle"
    assert profile.is_avatr
    assert profile.api_level == 29


def test_detect_falls_back_to_default_when_probe_fails():
    with patch.object(adb, "run", return_value=_fail()):
        profile = firmware.detect("S1")
    assert profile.installer_pkg == firmware.DEFAULT_PROFILE.installer_pkg
    assert profile.screen_users == firmware.DEFAULT_PROFILE.screen_users


def test_detect_excludes_user_zero_from_screens():
    pkgs = "package:com.huawei.appinstaller.car\n"
    users = (
        "Users:\n"
        "    UserInfo{0:Driver:c13} running\n"
        "    UserInfo{11:Sec:1010} running\n"
    )
    def fake_run(*args, **kwargs):
        if args[1] == "pm" and args[2] == "list" and args[3] == "packages":
            return _ok(stdout=pkgs)
        if args[1] == "pm" and args[2] == "list" and args[3] == "users":
            return _ok(stdout=users)
        if args[1] == "getprop":
            return _ok(stdout="31\n")
        return _ok()
    with patch.object(adb, "run", side_effect=fake_run):
        profile = firmware.detect("S1")
    assert 0 not in profile.screen_users
    assert profile.screen_users == (11,)


def test_detect_label_says_deepal_for_appinstaller_car():
    pkgs = "package:com.huawei.appinstaller.car\n"
    def fake_run(*args, **kwargs):
        if args[1] == "pm" and args[2] == "list" and args[3] == "packages":
            return _ok(stdout=pkgs)
        if args[1] == "pm" and args[2] == "list" and args[3] == "users":
            return _ok(stdout="UserInfo{10:x:0} running\n")
        if args[1] == "getprop":
            return _ok(stdout="31\n")
        return _ok()
    with patch.object(adb, "run", side_effect=fake_run):
        profile = firmware.detect("S1")
    assert "Deepal" in profile.label


def test_detect_label_says_avatr_for_appmarket_vehicle():
    pkgs = "package:com.huawei.appmarket.vehicle\n"
    def fake_run(*args, **kwargs):
        if args[1] == "pm" and args[2] == "list" and args[3] == "packages":
            return _ok(stdout=pkgs)
        if args[1] == "pm" and args[2] == "list" and args[3] == "users":
            return _ok(stdout="UserInfo{10:x:0} running\n")
        if args[1] == "getprop":
            return _ok(stdout="29\n")
        return _ok()
    with patch.object(adb, "run", side_effect=fake_run):
        profile = firmware.detect("S1")
    assert "Avatr" in profile.label
