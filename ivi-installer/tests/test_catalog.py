"""Tests for ivi_installer.catalog.

The catalog has two halves: a bundled ``extras.json`` (curated RU/CIS
entries with verified AppGallery C-ids + a Telegram direct download)
and ``from_appgallery_index()`` which converts the AppGallery API
index into our entry shape. Both are exercised here without touching
the network — AppGallery test fixtures are synthetic.
"""
from __future__ import annotations

import pytest

from ivi_installer import catalog


# =============================================================================
# Bundled extras (extras.json)
# =============================================================================


def test_load_extras_returns_appgallery_or_direct_entries():
    cat = catalog.load_extras()
    assert cat.apps, "extras.json should not be empty"
    for entry in cat.apps:
        assert entry.primary_source_kind in ("appgallery", "direct")


def test_load_alias_still_works():
    """``catalog.load`` is kept as a back-compat alias for tests / older
    main-window code that haven't moved to ``load_extras`` yet."""
    assert catalog.load() == catalog.load_extras()


def test_load_drops_entry_without_sources(tmp_path, monkeypatch):
    """A malformed entry doesn't crash the loader."""
    fixture = tmp_path / "extras.json"
    fixture.write_text("""{
        "schema_version": 2,
        "apps": [
            {"id": "good", "name": "Good", "category": "audio",
             "sources": [{"kind": "appgallery", "package": "x"}]},
            {"id": "bad", "name": "Bad", "category": "audio",
             "sources": []}
        ]
    }""", encoding="utf-8")

    class _FakeResourceFile:
        def __init__(self, path):
            self.path = path

        def open(self, mode):
            return open(self.path, mode)

    class _FakeResources:
        def joinpath(self, name):
            assert name == "extras.json"
            return _FakeResourceFile(fixture)

    monkeypatch.setattr(
        catalog._resources, "files", lambda _pkg: _FakeResources())
    cat = catalog.load_extras()
    ids = [a.id for a in cat.apps]
    assert ids == ["good"]


# =============================================================================
# search() — uses the standard fixture (synthetic catalog).
# =============================================================================


@pytest.fixture
def synth_apps():
    return (
        catalog.CatalogEntry(
            id="a.player", name="Aplayer", category="entertainment",
            sources=({"kind": "appgallery", "id": "C100"},),
            tested=True, min_api=21,
            description_en="audio app", description_ru="аудио приложение",
        ),
        catalog.CatalogEntry(
            id="b.compass", name="Bcompass", category="navigation",
            sources=({"kind": "appgallery", "id": "C101"},),
            tested=False, min_api=24,
            description_en="navigation app", description_ru="навигация",
        ),
        catalog.CatalogEntry(
            id="c.video.direct", name="Cvideo", category="entertainment",
            sources=({"kind": "direct", "url": "https://e.org/c.apk"},),
            tested=True, min_api=23,
            description_en="video app", description_ru="видео",
        ),
    )


def test_search_substring_case_insensitive(synth_apps):
    results = catalog.search(synth_apps, query="APLAYER")
    assert {a.id for a in results} == {"a.player"}


def test_search_filters_by_category(synth_apps):
    results = catalog.search(synth_apps, category="entertainment")
    assert all(a.category == "entertainment" for a in results)
    assert {a.id for a in results} == {"a.player", "c.video.direct"}


def test_search_filters_by_tested_only(synth_apps):
    tested = catalog.search(synth_apps, tested_only=True)
    assert all(a.tested for a in tested)
    assert {a.id for a in tested} == {"a.player", "c.video.direct"}


def test_search_source_filter_accepts_both_kind_and_display(synth_apps):
    by_kind = catalog.search(synth_apps, source="appgallery")
    by_label = catalog.search(synth_apps, source="AppGallery")
    assert {a.id for a in by_kind} == {a.id for a in by_label}


def test_search_min_api_at_most(synth_apps):
    cheap = catalog.search(synth_apps, min_api_at_most=21)
    for a in cheap:
        assert a.min_api is None or a.min_api <= 21


def test_search_combines_filters(synth_apps):
    results = catalog.search(
        synth_apps, query="video", category="entertainment",
        source="Direct", tested_only=False,
    )
    assert {a.id for a in results} == {"c.video.direct"}


def test_derive_initials():
    assert catalog.derive_initials("Organic Maps") == "OM"
    assert catalog.derive_initials("VLC") == "VL"
    assert catalog.derive_initials("") == "··"


# =============================================================================
# Sections + sort
# =============================================================================


def _ts_ago(days: int, *, now_ms: int) -> int:
    return now_ms - days * 24 * 60 * 60 * 1000


def test_search_section_new_uses_added_at():
    now = 1_700_000_000_000
    apps = (
        catalog.CatalogEntry(
            id="fresh", name="Fresh", category="tools",
            sources=({"kind": "appgallery", "package": "fresh"},),
            added_at=_ts_ago(2, now_ms=now),
        ),
        catalog.CatalogEntry(
            id="stale", name="Stale", category="tools",
            sources=({"kind": "appgallery", "package": "stale"},),
            added_at=_ts_ago(60, now_ms=now),
        ),
    )
    fresh = catalog.search(apps, section="new", now_ms=now)
    assert {a.id for a in fresh} == {"fresh"}


def test_search_section_updated_uses_last_updated_at():
    now = 1_700_000_000_000
    apps = (
        catalog.CatalogEntry(
            id="updated", name="Up", category="tools",
            sources=({"kind": "appgallery", "package": "updated"},),
            last_updated_at=_ts_ago(3, now_ms=now),
        ),
        catalog.CatalogEntry(
            id="dormant", name="Dorm", category="tools",
            sources=({"kind": "appgallery", "package": "dormant"},),
            last_updated_at=_ts_ago(180, now_ms=now),
        ),
    )
    recent = catalog.search(apps, section="updated", now_ms=now)
    assert {a.id for a in recent} == {"updated"}


def test_search_section_tested_filters_by_flag():
    apps = (
        catalog.CatalogEntry(
            id="t", name="T", category="tools", tested=True,
            sources=({"kind": "appgallery", "package": "t"},),
        ),
        catalog.CatalogEntry(
            id="u", name="U", category="tools", tested=False,
            sources=({"kind": "appgallery", "package": "u"},),
        ),
    )
    assert [a.id for a in catalog.search(apps, section="tested")] == ["t"]


def test_search_section_all_is_noop():
    apps = (
        catalog.CatalogEntry(
            id="a", name="A", category="tools",
            sources=({"kind": "appgallery", "package": "a"},),
        ),
    )
    assert catalog.search(apps, section="all") == [apps[0]]
    assert catalog.search(apps, section=None) == [apps[0]]


# =============================================================================
# AppGallery index conversion
# =============================================================================


def _ag_item(pkg: str, *, appid: str = "C123", name: str | None = None,
             memo: str = "Test app", kind_name: str = "Развлечения",
             size: int = 1024 * 1024 * 5,
             version: str = "1.0", icon: str = "https://example.org/i.png",
             ) -> dict:
    return {
        "appid":          appid,
        "package":        pkg,
        "name":           name or pkg,
        "memo":           memo,
        "kindName":       kind_name,
        "tagName":        kind_name,
        "size":           size,
        "appVersionName": version,
        "icon":           icon,
    }


def _ag_index(category_label: str, *items: dict) -> dict:
    """Build an index that puts ``items`` under one URI section."""
    return {
        "fetched_at": 1_777_000_000,
        "shard": "europe",
        "locale": "ru_RU",
        "categories": {
            category_label: {"uri": f"<uri-{category_label}>",
                              "items": list(items)},
        },
    }


def test_from_appgallery_index_round_trip():
    idx = _ag_index(
        "entertainment",
        _ag_item("ru.kinopoisk", appid="C101036423",
                 name="Кинопоиск", memo="Кино и сериалы",
                 size=70 * 1024 * 1024,
                 version="7.83.0"),
    )
    apps = catalog.from_appgallery_index(idx)
    assert len(apps) == 1
    a = apps[0]
    assert a.id == "ru.kinopoisk"
    assert a.name == "Кинопоиск"
    # Tagged with the URI-section it appeared under, NOT with the
    # per-item kindName. This is the only signal AppGallery gives us
    # that's actually used in our category dropdown.
    assert a.category == "entertainment"
    assert a.primary_source_kind == "appgallery"
    assert a.sources[0]["id"] == "C101036423"
    assert a.size_mb == 70.0
    assert a.version == "7.83.0"
    assert a.description_ru == "Кино и сериалы"
    # AppGallery doesn't expose minSdk in tab listings; we keep min_api
    # unset rather than guessing from targetSDK.
    assert a.min_api is None


def test_from_appgallery_index_dedupes_within_a_section():
    """Even pulling a single URI, the same package can surface twice
    inside paginated results. First-seen wins on dedup so we don't
    emit duplicate rows."""
    idx = {
        "categories": {
            "most_ranked": {"items": [
                _ag_item("a.b", appid="C1", name="First"),
                _ag_item("a.b", appid="C2", name="Dup"),
            ]},
        },
    }
    apps = catalog.from_appgallery_index(idx)
    assert [a.id for a in apps] == ["a.b"]
    assert apps[0].name == "First"
    assert apps[0].category == "most_ranked"


def test_from_appgallery_index_only_packages_filter():
    idx = _ag_index(
        "tools",
        _ag_item("a.want"), _ag_item("b.skip"), _ag_item("c.want"))
    apps = catalog.from_appgallery_index(
        idx, only_packages={"a.want", "c.want"})
    assert {a.id for a in apps} == {"a.want", "c.want"}


def test_from_appgallery_index_keeps_section_label_verbatim():
    """v0.22 stopped enforcing a fixed taxonomy — whatever URI label
    the fetcher used surfaces verbatim on the entry. Default fetches
    only walk the Most Ranked URI, so all entries get tagged
    ``most_ranked``; the one-shot extras-builder script is free to
    run with a different label."""
    idx = _ag_index("not-a-known-section", _ag_item("p.q"))
    apps = catalog.from_appgallery_index(idx)
    assert apps[0].category == "not-a-known-section"


def test_from_appgallery_index_default_label_is_most_ranked():
    """The shipping fetcher only walks the Most Ranked URI, so every
    entry comes out tagged ``most_ranked`` after the rename."""
    from ivi_installer.sources import appgallery_index
    assert appgallery_index.MOST_RANKED_LABEL == "most_ranked"
    idx = _ag_index(appgallery_index.MOST_RANKED_LABEL, _ag_item("p.q"))
    apps = catalog.from_appgallery_index(idx)
    assert apps[0].category == "most_ranked"


