"""Tests for ivi_installer.sources.direct."""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from ivi_installer.sources import direct


class _FakeResponse:
    def __init__(self, body: bytes, headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        if n < 0:
            chunk = self._body
            self._body = b""
            return chunk
        chunk = self._body[:n]
        self._body = self._body[n:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _ok_apk_body(extra: int = 20_000) -> bytes:
    return b"PK\x03\x04" + b"A" * extra


def test_resolve_writes_apk(tmp_path):
    body = _ok_apk_body()
    fake = _FakeResponse(body, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Length": str(len(body)),
    })
    with patch.object(direct.urllib.request, "urlopen", return_value=fake):
        path = direct.resolve(
            {"kind": "direct", "url": "https://example.org/foo.apk"},
            out_dir=tmp_path,
        )
    assert path.is_file()
    assert path.name == "foo.apk"
    assert path.read_bytes() == body


def test_resolve_rejects_html(tmp_path):
    fake = _FakeResponse(b"<html/>", headers={
        "Content-Type": "text/html; charset=utf-8",
    })
    with patch.object(direct.urllib.request, "urlopen", return_value=fake):
        with pytest.raises(RuntimeError, match="HTML"):
            direct.resolve(
                {"kind": "direct", "url": "https://example.org/foo.apk"},
                out_dir=tmp_path,
            )


def test_resolve_rejects_non_zip(tmp_path):
    fake = _FakeResponse(b"GIF89a" + b"\x00" * 20_000, headers={
        "Content-Type": "application/octet-stream",
    })
    with patch.object(direct.urllib.request, "urlopen", return_value=fake):
        with pytest.raises(RuntimeError, match="not a ZIP/APK"):
            direct.resolve(
                {"kind": "direct", "url": "https://example.org/foo.apk"},
                out_dir=tmp_path,
            )


def test_resolve_rejects_tiny_payload(tmp_path):
    fake = _FakeResponse(b"PK\x03\x04tiny", headers={
        "Content-Type": "application/octet-stream",
    })
    with patch.object(direct.urllib.request, "urlopen", return_value=fake):
        with pytest.raises(RuntimeError, match="too small"):
            direct.resolve(
                {"kind": "direct", "url": "https://example.org/foo.apk"},
                out_dir=tmp_path,
            )


def test_resolve_verifies_sha256(tmp_path):
    body = _ok_apk_body()
    expected = hashlib.sha256(body).hexdigest()
    fake = _FakeResponse(body, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Length": str(len(body)),
    })
    with patch.object(direct.urllib.request, "urlopen", return_value=fake):
        path = direct.resolve(
            {"kind": "direct", "url": "https://example.org/foo.apk",
             "sha256": expected},
            out_dir=tmp_path,
        )
    assert path.is_file()


def test_resolve_rejects_wrong_sha256(tmp_path):
    body = _ok_apk_body()
    fake = _FakeResponse(body, headers={
        "Content-Type": "application/vnd.android.package-archive",
        "Content-Length": str(len(body)),
    })
    with patch.object(direct.urllib.request, "urlopen", return_value=fake):
        with pytest.raises(RuntimeError, match="sha256 mismatch"):
            direct.resolve(
                {"kind": "direct", "url": "https://example.org/foo.apk",
                 "sha256": "00" * 32},
                out_dir=tmp_path,
            )


def test_resolve_requires_url(tmp_path):
    with pytest.raises(ValueError):
        direct.resolve({"kind": "direct"}, out_dir=tmp_path)


def test_safe_filename_appends_apk_suffix():
    assert direct._safe_filename("foo") == "foo.apk"
    assert direct._safe_filename("foo.apk") == "foo.apk"
    assert direct._safe_filename("../etc/passwd") == "passwd.apk"
