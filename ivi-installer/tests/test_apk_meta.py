"""Tests for ivi_installer.apk_meta — APK manifest reader + helpers."""
from __future__ import annotations

from pathlib import Path

import pytest

from ivi_installer import apk_meta
from ivi_installer.apk_meta import (
    ApkMeta,
    derive_display_name,
    derive_initials,
    read_meta,
)

# Path to the keyboard APK that ships in our resources dir — gives us
# a real-world AXML to parse end-to-end (rather than mocking out the
# binary format, which would just re-test our own code).
_CELIA_APK = (
    Path(__file__).parent.parent
    / "ivi_installer" / "resources" / "celia-keyboard-11-0-5-352.apk"
)


# ---- AXML parsing on a real APK -------------------------------------------

@pytest.mark.skipif(
    not _CELIA_APK.exists(),
    reason="bundled Celia APK not available",
)
def test_read_meta_celia_keyboard():
    meta = read_meta(_CELIA_APK)
    assert meta.package == "com.huawei.ohos.inputmethod"
    assert meta.version_name == "11.0.5.352"
    assert meta.version_code == 110005352


def test_read_meta_returns_blanks_for_non_apk(tmp_path):
    not_an_apk = tmp_path / "garbage.apk"
    not_an_apk.write_bytes(b"this is not a zip file")
    meta = read_meta(not_an_apk)
    assert meta == ApkMeta(package="", version_name="",
                            version_code=None, label=None)


def test_read_meta_returns_blanks_for_zip_without_manifest(tmp_path):
    import zipfile
    z_path = tmp_path / "no-manifest.apk"
    with zipfile.ZipFile(z_path, "w") as z:
        z.writestr("classes.dex", b"\x00")
    meta = read_meta(z_path)
    assert meta.package == ""
    assert meta.version_name == ""


# ---- display-name heuristic -----------------------------------------------

@pytest.mark.parametrize("package, expected", [
    ("com.spotify.music", "Spotify"),
    ("org.telegram.messenger", "Telegram"),
    ("com.facebook.katana", "Facebook"),
    ("com.whatsapp", "Whatsapp"),
    ("com.google.android.youtube", "Youtube"),
    ("com.huawei.appmarket", "Appmarket"),
    ("com.huawei.ohos.inputmethod", "Inputmethod"),
    ("com.bosch.vdi", "Bosch"),
    ("com.android.car.media", "Media"),
])
def test_display_name_picks_first_non_noise_segment(package, expected):
    """For packages without a literal `<application android:label="…">`
    the display name comes from the package id. We pick the first
    segment that isn't a generic prefix (com / org / vendor names),
    so brands like "Spotify" and "Telegram" surface instead of the
    less-recognizable category suffix ("Music", "Messenger")."""
    meta = ApkMeta(package=package, version_name="1.0",
                    version_code=1, label=None)
    assert derive_display_name(meta, fallback="<stem>") == expected


def test_display_name_prefers_literal_label_over_package():
    meta = ApkMeta(package="com.example.app", version_name="1",
                    version_code=1, label="Cool App")
    assert derive_display_name(meta, fallback="other") == "Cool App"


def test_display_name_falls_back_when_package_is_all_noise():
    meta = ApkMeta(package="com.app", version_name="", version_code=None,
                    label=None)
    # All segments are in the noise list → we still produce *something*
    # rather than blank ("app" is the last raw segment).
    assert derive_display_name(meta, fallback="").lower() == "app"


def test_display_name_uses_caller_fallback_when_no_package():
    meta = ApkMeta(package="", version_name="", version_code=None,
                    label=None)
    assert derive_display_name(meta, fallback="myfile") == "myfile"


# ---- initials -------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("Spotify", "SP"),
    ("My Cool App", "MC"),
    ("Telegram", "TE"),
    ("YouTube", "YT"),       # camelCase splits → "You Tube" → "YT"
    ("input_method", "IM"),  # snake_case splits the same way
])
def test_initials_two_letters_from_name(name, expected):
    assert derive_initials(name, package="") == expected


def test_initials_fall_back_to_package_last_segment():
    assert derive_initials("", package="com.spotify.music") == "MU"


def test_initials_ultimate_fallback_is_AP():
    assert derive_initials("", package="") == "AP"
