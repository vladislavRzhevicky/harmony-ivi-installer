"""Unit tests for ivi_installer/device_info.py."""
from __future__ import annotations

from unittest.mock import patch

from ivi_installer import adb, device_info
from ivi_installer.adb import AdbResult


# ---- helpers ----

def _r(stdout: str = "", stderr: str = "", exit_code: int = 0) -> AdbResult:
    return AdbResult(args=(), exit_code=exit_code, stdout=stdout, stderr=stderr)


_GETPROP_SAMPLE = """\
[ro.product.brand]: [Avatr]
[ro.product.manufacturer]: [Huawei]
[ro.product.model]: [AVATR_12]
[ro.product.device]: [ICHU3200F2-ADV]
[ro.product.name]: [ICHU3200F2-ADV]
[ro.hardware]: [kirin990]
[ro.product.board]: [kirin990]
[ro.product.cpu.abi]: [arm64-v8a]
[ro.product.cpu.abilist]: [arm64-v8a,armeabi-v7a,armeabi]
[ro.build.fingerprint]: [Avatr/AVATR_12/ICHU3200F2-ADV:12/HUAWEIICHU3200F2-ADV/100:user/release-keys]
[ro.build.version.release]: [12]
[ro.build.version.sdk]: [31]
[ro.build.version.security_patch]: [2024-12-01]
[ro.build.id]: [HUAWEIICHU3200F2-ADV]
[ro.build.type]: [user]
[ro.build.tags]: [release-keys]
[ro.build.user]: [jenkins]
[ro.build.host]: [build-host-1]
[ro.build.date]: [Wed Dec 4 12:00:00 UTC 2024]
[ro.bootloader]: [unknown]
[ro.bootmode]: [normal]
[hw_sc.build.platform.version]: [3.0.0]
[ro.build.version.emui]: [EmotionUI_13.0.0]
[ro.config.carrier]: [HwAvatr]
[persist.sys.locale]: [zh-CN]
[persist.sys.country]: [CN]
[persist.sys.timezone]: [Asia/Shanghai]
[wifi.interface]: [wlan0]
[ro.debuggable]: [0]
[ro.secure]: [1]
[sys.boot_completed]: [1]
"""


# ---- collect() smoke ----

def test_collect_returns_all_expected_sections():
    """One call should produce every advertised section."""
    expected = {
        "Identity", "Build & OS", "HarmonyOS", "Hardware",
        "Display", "Memory & Storage", "Battery",
        "Network", "Locale & Time", "Users & Packages", "ADB / Shell",
    }
    with patch.object(adb, "run", return_value=_r(stdout=_GETPROP_SAMPLE)):
        sections = device_info.collect("S1")
    titles = {title for title, _ in sections}
    assert expected <= titles


def test_collect_identity_pulls_from_getprop():
    with patch.object(adb, "run", return_value=_r(stdout=_GETPROP_SAMPLE)):
        sections = device_info.collect("MYSERIAL")
    identity = dict(next(rows for title, rows in sections if title == "Identity"))
    assert identity["Serial"] == "MYSERIAL"
    assert identity["Brand"] == "Avatr"
    assert identity["Manufacturer"] == "Huawei"
    assert identity["Model"] == "AVATR_12"
    assert identity["Primary ABI" if False else "Device"] == "ICHU3200F2-ADV"


def test_collect_build_section():
    with patch.object(adb, "run", return_value=_r(stdout=_GETPROP_SAMPLE)):
        sections = device_info.collect("S1")
    build = dict(next(rows for title, rows in sections if title == "Build & OS"))
    assert build["Android version"] == "12"
    assert build["API level"] == "31"
    assert build["Security patch"] == "2024-12-01"
    assert build["Build type"] == "user"


def test_collect_locale_section():
    with patch.object(adb, "run", return_value=_r(stdout=_GETPROP_SAMPLE)):
        sections = device_info.collect("S1")
    locale = dict(next(rows for title, rows in sections
                        if title == "Locale & Time"))
    assert locale["Locale"] == "zh-CN"
    assert locale["Country"] == "CN"
    assert locale["Timezone"] == "Asia/Shanghai"


def test_collect_handles_empty_getprop_gracefully():
    """With no props at all, every field should be the placeholder."""
    with patch.object(adb, "run", return_value=_r(stdout="")):
        sections = device_info.collect("S1")
    # Sanity: the call did not raise and we still got our sections.
    assert len(sections) >= 5
    identity = dict(next(rows for title, rows in sections if title == "Identity"))
    assert identity["Brand"] == device_info.PLACEHOLDER
    assert identity["Serial"] == "S1"


def test_collect_does_not_raise_when_a_probe_throws():
    """A failing read should be swallowed, the rest of the snapshot
    should still come back."""
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if "cat" in args and "/proc/cpuinfo" in args:
            raise RuntimeError("boom")
        if calls["n"] == 1:
            return _r(stdout=_GETPROP_SAMPLE)
        return _r(stdout="")

    with patch.object(adb, "run", side_effect=flaky):
        sections = device_info.collect("S1")
    hardware = dict(next(rows for title, rows in sections if title == "Hardware"))
    assert hardware["CPU cores"] == device_info.PLACEHOLDER


# ---- format_sections ----

def test_format_sections_renders_human_readable():
    sections = [
        ("Identity", [("Serial", "S1"), ("Brand", "Avatr")]),
        ("Empty", []),
    ]
    text = device_info.format_sections(sections)
    assert "━━ Identity ━━" in text
    assert "Serial" in text and "S1" in text
    assert "Brand" in text and "Avatr" in text
    assert "━━ Empty ━━" in text
    assert "(no data)" in text
