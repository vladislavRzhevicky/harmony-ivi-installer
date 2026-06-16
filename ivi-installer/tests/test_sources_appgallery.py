"""Tests for ivi_installer.sources.appgallery."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ivi_installer.sources import appgallery


# ---- parse_app_id ----------------------------------------------------------

def test_parse_app_id_bare():
    assert appgallery.parse_app_id("C101898721") == "C101898721"
    assert appgallery.parse_app_id("c101898721") == "C101898721"
    assert appgallery.parse_app_id("  C101898721  ") == "C101898721"


def test_parse_app_id_url_full():
    assert appgallery.parse_app_id(
        "https://appgallery.huawei.com/app/C101898721"
    ) == "C101898721"


def test_parse_app_id_url_appdl():
    assert appgallery.parse_app_id(
        "https://appgallery.cloud.huawei.com/appdl/C101898721"
    ) == "C101898721"


def test_parse_app_id_returns_none_for_garbage():
    assert appgallery.parse_app_id("") is None
    assert appgallery.parse_app_id("https://example.com/app/123") is None
    assert appgallery.parse_app_id("foo bar baz") is None


def test_parse_app_id_picks_first_token():
    # If the user pastes a long URL, we still pick the first match.
    assert appgallery.parse_app_id(
        "C123 then later C999"
    ) == "C123"


# ---- _safe_filename --------------------------------------------------------

def test_safe_filename_strips_path_traversal():
    assert appgallery._safe_filename("../../etc/passwd") == "passwd"
    assert appgallery._safe_filename("foo bar.apk") == "foo_bar.apk"


def test_safe_filename_falls_back_when_empty():
    assert appgallery._safe_filename("") == "download.apk"
    # All chars stripped → fallback.
    assert appgallery._safe_filename("...") == "download.apk"


# ---- _filename_from_disposition --------------------------------------------

def test_filename_from_disposition_quoted():
    assert appgallery._filename_from_disposition(
        'attachment; filename="MyApp.apk"'
    ) == "MyApp.apk"


def test_filename_from_disposition_utf8():
    assert appgallery._filename_from_disposition(
        "attachment; filename*=UTF-8''Hello%20World.apk"
    ) == "Hello World.apk"


def test_filename_from_disposition_none():
    assert appgallery._filename_from_disposition("") is None
    assert appgallery._filename_from_disposition("inline") is None


# ---- download (integration-style with a fake urlopen) ----------------------

class _FakeResponse:
    def __init__(self, body: bytes, *, headers: dict | None = None):
        self._body = body
        self._pos = 0
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _ok_apk_body(extra_bytes: int = 20_000) -> bytes:
    """Build a fake APK body big enough to clear the tiny-file guard
    (the competitor-aligned threshold rejects payloads <10 KB)."""
    return b"PK\x03\x04" + b"A" * extra_bytes


def test_download_writes_apk(tmp_path):
    apk_body = _ok_apk_body()
    fake = _FakeResponse(apk_body, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Length": str(len(apk_body)),
        "Content-Disposition": 'attachment; filename="myapp.apk"',
    })
    with patch.object(appgallery.urllib.request, "urlopen",
                       return_value=fake):
        path = appgallery.download("C12345", out_dir=tmp_path)
    assert path.is_file()
    assert path.name == "myapp.apk"
    assert path.read_bytes() == apk_body


def test_download_rejects_html(tmp_path):
    fake = _FakeResponse(b"<html>not found</html>", headers={
        "Content-Type": "text/html; charset=utf-8",
    })
    with patch.object(appgallery.urllib.request, "urlopen",
                       return_value=fake):
        with pytest.raises(RuntimeError, match="HTML"):
            appgallery.download("C12345", out_dir=tmp_path)


def test_download_rejects_non_zip_payload(tmp_path):
    # Server returns 200 OK with octet-stream but the body isn't a ZIP.
    # Pad past the 10 KB tiny-file guard so we hit the magic check.
    fake = _FakeResponse(b"GIF89a..." + b"\x00" * 20_000, headers={
        "Content-Type": "application/octet-stream",
    })
    with patch.object(appgallery.urllib.request, "urlopen",
                       return_value=fake):
        with pytest.raises(RuntimeError, match="not a ZIP/APK"):
            appgallery.download("C12345", out_dir=tmp_path)


def test_download_rejects_tiny_payload(tmp_path):
    """Competitor parity: even a valid-looking PK header is rejected
    if the response is suspiciously small (CDN error blob)."""
    fake = _FakeResponse(b"PK\x03\x04tinyerror", headers={
        "Content-Type": "application/octet-stream",
        "Content-Length": "13",
    })
    with patch.object(appgallery.urllib.request, "urlopen",
                       return_value=fake):
        with pytest.raises(RuntimeError, match="too small"):
            appgallery.download("C12345", out_dir=tmp_path)


def test_download_falls_back_to_id_when_no_disposition(tmp_path):
    apk_body = _ok_apk_body()
    fake = _FakeResponse(apk_body, headers={
        "Content-Type": "application/octet-stream",
        "Content-Length": str(len(apk_body)),
    })
    with patch.object(appgallery.urllib.request, "urlopen",
                       return_value=fake):
        path = appgallery.download("C99999", out_dir=tmp_path)
    assert path.name == "C99999.apk"


def test_download_rejects_invalid_id(tmp_path):
    with pytest.raises(ValueError):
        appgallery.download("not-an-id", out_dir=tmp_path)


def test_download_progress_callback_fires(tmp_path):
    apk_body = b"PK\x03\x04" + b"X" * 200_000
    fake = _FakeResponse(apk_body, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Length": str(len(apk_body)),
    })
    seen: list[tuple[int, int]] = []
    with patch.object(appgallery.urllib.request, "urlopen",
                       return_value=fake):
        appgallery.download(
            "C12345", out_dir=tmp_path,
            progress=lambda b, t: seen.append((b, t)),
        )
    # Final tick should report the full size.
    assert seen[-1][0] == len(apk_body)
    assert seen[-1][1] == len(apk_body)
    # First tick is the (0, total) prelude.
    assert seen[0] == (0, len(apk_body))


def test_download_disables_ssl_verification(tmp_path):
    """Competitor parity: SSL hostname check + cert verification are
    disabled because the Huawei CDN cert chain is flaky from desktop
    trust stores. We pass an unverified context to urlopen."""
    apk_body = _ok_apk_body()
    fake = _FakeResponse(apk_body, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Length": str(len(apk_body)),
    })
    with patch.object(appgallery.urllib.request, "urlopen",
                       return_value=fake) as urlopen:
        appgallery.download("C12345", out_dir=tmp_path)
    ctx = urlopen.call_args.kwargs.get("context")
    assert ctx is not None
    import ssl as _ssl
    assert ctx.check_hostname is False
    assert ctx.verify_mode == _ssl.CERT_NONE
    # And the timeout should match the competitor's 120s default.
    assert urlopen.call_args.kwargs.get("timeout") == 120.0
