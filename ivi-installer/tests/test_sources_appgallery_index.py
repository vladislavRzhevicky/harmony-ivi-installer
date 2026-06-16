"""Tests for ivi_installer.sources.appgallery_index — the discovery
side of the AppGallery integration (catalog dump + search-suggestion
endpoints).

These tests stub out ``urllib.request.urlopen`` so nothing hits the
network. We don't try to round-trip the JWT bootstrap; the
search-suggestion endpoints don't need it, and ``fetch_index`` is
covered separately by the in-memory ``CatalogStore`` tests via
``catalog.from_appgallery_index`` round-trips.
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from ivi_installer.sources import appgallery_index


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")
        self._pos = 0

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


# ---------------------------------------------------------------------------
# Most Ranked is now the only URI fetch_index walks
# ---------------------------------------------------------------------------


def test_default_category_uris_now_only_most_ranked():
    """v0.22 collapsed twelve URIs to one — Most Ranked is the curated
    cross-category Top list. The shorter pull cuts cold-fetch from
    ~33 s to ~3-5 s."""
    assert len(appgallery_index.DEFAULT_CATEGORY_URIS) == 1
    label, uri = appgallery_index.DEFAULT_CATEGORY_URIS[0]
    assert label == appgallery_index.MOST_RANKED_LABEL == "most_ranked"
    assert uri == appgallery_index.MOST_RANKED_URI
    assert "A02000" in uri


# ---------------------------------------------------------------------------
# Hot keyword endpoint — unauthenticated, JSON in/out
# ---------------------------------------------------------------------------


def test_get_hot_search_list_flattens_buckets_and_dedupes():
    payload = {
        "rtnCode": 0,
        "list": [
            {"name": "All", "dataList": []},
            {"name": "Apps", "dataList": [
                {"name": "Telegram"},
                {"name": "kinopoisk"},
            ]},
            {"name": "Games", "dataList": [
                # Case-different duplicate of "Telegram" — drops out.
                {"name": "telegram"},
                {"name": "World of Tanks"},
                {"name": ""},          # empty drops out
            ]},
        ],
    }
    with patch.object(appgallery_index.urllib.request, "urlopen",
                       return_value=_FakeResponse(payload)) as urlopen:
        out = appgallery_index.get_hot_search_list()
    # Order preserved across buckets, dupes dropped, empty stripped.
    assert out == ["Telegram", "kinopoisk", "World of Tanks"]
    # Verify it hit the /edge/index/getnewhotsearchlist URL with a
    # JSON body — not the authed /edge/uowap/index path.
    req = urlopen.call_args.args[0]
    assert req.full_url.endswith("/edge/index/getnewhotsearchlist")
    body = json.loads(req.data.decode("utf-8"))
    assert body == {"serviceType": 20, "zone": "", "locale": "en"}
    assert req.headers["Content-type"] == "application/json"


def test_get_hot_search_list_handles_empty_response():
    payload = {"rtnCode": 0, "list": []}
    with patch.object(appgallery_index.urllib.request, "urlopen",
                       return_value=_FakeResponse(payload)):
        assert appgallery_index.get_hot_search_list() == []


# ---------------------------------------------------------------------------
# completeSearchWord — type-ahead
# ---------------------------------------------------------------------------


def test_complete_search_word_returns_normalised_shape():
    payload = {
        "rtnCode": 0,
        "keyword": "tik",
        "list": ["tik", "TikTak", "Tikado", "TIKI"],
        "app": {
            "appid": "C100315379",
            "package": "com.zhiliaoapp.musically",
            "name": "TikTok",
            "kindName": "Entertainment",
            "icon": "https://appimg-dre.dbankcdn.com/.../icon.png",
        },
        "appList": [],          # redundant in the wire format; we ignore it
    }
    with patch.object(appgallery_index.urllib.request, "urlopen",
                       return_value=_FakeResponse(payload)) as urlopen:
        out = appgallery_index.complete_search_word("tik")
    assert out["keyword"] == "tik"
    assert out["suggestions"] == ["tik", "TikTak", "Tikado", "TIKI"]
    assert out["top_app"]["appid"] == "C100315379"
    assert out["top_app"]["name"] == "TikTok"
    # Verify the URL + JSON body shape.
    req = urlopen.call_args.args[0]
    assert req.full_url.endswith("/edge/index/completeSearchWord")
    body = json.loads(req.data.decode("utf-8"))
    assert body["keyword"] == "tik"
    assert body["serviceType"] == 20


def test_complete_search_word_omits_request_when_keyword_empty():
    """An empty keyword would just trip AppGallery's rtnCode != 0
    path. Skip the round-trip and return an empty shape."""
    with patch.object(appgallery_index.urllib.request, "urlopen") as urlopen:
        out = appgallery_index.complete_search_word("   ")
    assert urlopen.call_count == 0
    assert out == {"keyword": "", "suggestions": [], "top_app": None}


def test_complete_search_word_handles_no_app_match():
    payload = {
        "rtnCode": 0,
        "keyword": "qq",
        "list": ["qq"],
        # AppGallery omits ``app`` entirely when there's no confident
        # match. We should return None for top_app without crashing.
    }
    with patch.object(appgallery_index.urllib.request, "urlopen",
                       return_value=_FakeResponse(payload)):
        out = appgallery_index.complete_search_word("qq")
    assert out["suggestions"] == ["qq"]
    assert out["top_app"] is None
