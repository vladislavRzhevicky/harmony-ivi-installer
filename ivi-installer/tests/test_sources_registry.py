"""Tests for ivi_installer.sources registry dispatch."""
from __future__ import annotations

import pytest

from ivi_installer import sources


def test_registry_lists_supported_kinds():
    assert set(sources.RESOLVERS) == {"appgallery", "direct"}


def test_dispatch_unknown_kind_raises():
    with pytest.raises(KeyError):
        sources.resolve({"kind": "rustore"}, out_dir="/tmp")


def test_dispatch_missing_kind_raises():
    with pytest.raises(ValueError):
        sources.resolve({"package": "x"}, out_dir="/tmp")


def test_dispatch_routes_to_named_resolver(tmp_path, monkeypatch):
    seen = {}

    def _spy(entry, *, out_dir, progress, log_callback):
        seen["entry"] = entry
        return tmp_path / "ok.apk"

    monkeypatch.setitem(sources.RESOLVERS, "appgallery", _spy)
    sources.resolve(
        {"kind": "appgallery", "id": "C12345"},
        out_dir=tmp_path,
    )
    assert seen["entry"]["id"] == "C12345"
