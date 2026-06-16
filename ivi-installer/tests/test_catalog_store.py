"""Tests for ivi_installer.catalog_store.

The store is a thin SQL layer; we exercise it via the in-memory
``from_apps`` constructor so each test runs isolated and fast. The
on-disk ``build_to_path`` path also gets a smoke test.
"""
from __future__ import annotations

import sqlite3

import pytest

from ivi_installer.catalog import CatalogEntry
from ivi_installer.catalog_store import (
    CatalogStore,
    SCHEMA_VERSION,
    default_db_path,
    merge_apps,
)


def _e(
    app_id: str,
    *,
    name: str | None = None,
    category: str = "tools",
    source_kind: str = "appgallery",
    tested: bool = False,
    size_mb: float | None = 10.0,
    added_at: int | None = None,
    last_updated_at: int | None = None,
    description_en: str | None = None,
    description_ru: str | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        id=app_id,
        name=name or app_id.rsplit(".", 1)[-1].title(),
        category=category,
        sources=({"kind": source_kind, "package": app_id},),
        description_en=description_en or f"Description of {app_id}",
        description_ru=description_ru,
        size_mb=size_mb,
        tested=tested,
        added_at=added_at,
        last_updated_at=last_updated_at,
    )


# =============================================================================
# Round-trip
# =============================================================================


def test_from_apps_round_trips_an_entry():
    e = _e("a.b", name="A B", category="entertainment", tested=True,
           size_mb=12.5, added_at=1_000, last_updated_at=2_000,
           description_ru="русское", description_en="english")
    store = CatalogStore.from_apps([e])
    out = store.fetch("a.b")
    assert out is not None
    assert out.id == "a.b"
    assert out.name == "A B"
    assert out.category == "entertainment"
    assert out.tested is True
    assert out.size_mb == 12.5
    assert out.added_at == 1_000
    assert out.last_updated_at == 2_000
    assert out.description_ru == "русское"
    assert out.description_en == "english"
    assert out.primary_source_kind == "appgallery"


def test_total_and_get_meta():
    store = CatalogStore.from_apps([_e("a"), _e("b"), _e("c")],
                                    generated_at="2026-05-07T00:00:00Z")
    assert store.total() == 3
    assert store.generated_at() == "2026-05-07T00:00:00Z"
    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)


def test_fetch_missing_id_returns_none():
    store = CatalogStore.from_apps([_e("a")])
    assert store.fetch("nope") is None


def test_fetch_many_preserves_request_order():
    store = CatalogStore.from_apps([_e("a"), _e("b"), _e("c"), _e("d")])
    out = store.fetch_many(["c", "a", "d"])
    assert [e.id for e in out] == ["c", "a", "d"]


# =============================================================================
# query_ids — filters
# =============================================================================


def test_query_ids_preserves_insertion_order():
    """Default order is the order entries were inserted (mirrors
    AppGallery's API ranking) — ``ORDER BY rowid``."""
    store = CatalogStore.from_apps([
        _e("z.app", name="Zebra"),
        _e("a.app", name="Aardvark"),
        _e("m.app", name="Middle"),
    ])
    assert store.query_ids() == ["z.app", "a.app", "m.app"]


def test_query_ids_text_search_is_case_insensitive():
    store = CatalogStore.from_apps([
        _e("a.audio", name="AudioPlayer", category="entertainment"),
        _e("b.video", name="VideoMaker", category="entertainment"),
    ])
    assert store.query_ids(query="AUDIO") == ["a.audio"]
    assert store.query_ids(query="video") == ["b.video"]


def test_query_ids_filters_by_source():
    apps = [
        _e("a.ent", name="AppA", category="entertainment",
           source_kind="appgallery"),
        _e("b.tools", name="AppB", category="tools", source_kind="appgallery"),
        _e("c.ent", name="AppC", category="entertainment",
           source_kind="direct"),
    ]
    store = CatalogStore.from_apps(apps)
    assert store.query_ids(source="appgallery") == ["a.ent", "b.tools"]
    # source filter accepts the display label too.
    assert store.query_ids(source="Direct") == ["c.ent"]


def test_query_ids_section_new_uses_added_at():
    now = 1_700_000_000_000
    one_day = 24 * 60 * 60 * 1000
    apps = [
        _e("fresh", added_at=now - 5 * one_day),
        _e("stale", added_at=now - 365 * one_day),
        _e("missing", added_at=None),
    ]
    store = CatalogStore.from_apps(apps)
    assert store.query_ids(section="new", now_ms=now) == ["fresh"]


def test_query_ids_section_updated_uses_last_updated_at():
    now = 1_700_000_000_000
    one_day = 24 * 60 * 60 * 1000
    apps = [
        _e("fresh", last_updated_at=now - 5 * one_day),
        _e("stale", last_updated_at=now - 365 * one_day),
    ]
    store = CatalogStore.from_apps(apps)
    assert store.query_ids(section="updated", now_ms=now) == ["fresh"]


def test_query_ids_section_tested_filters_by_flag():
    apps = [_e("t", tested=True), _e("u", tested=False)]
    store = CatalogStore.from_apps(apps)
    assert store.query_ids(section="tested") == ["t"]


# =============================================================================
# query_ids — pagination
# =============================================================================


def test_query_ids_pagination():
    apps = [_e(f"app{i:03d}", name=f"App{i:03d}") for i in range(10)]
    store = CatalogStore.from_apps(apps)
    page1 = store.query_ids(limit=4, offset=0)
    page2 = store.query_ids(limit=4, offset=4)
    page3 = store.query_ids(limit=4, offset=8)
    assert page1 == ["app000", "app001", "app002", "app003"]
    assert page2 == ["app004", "app005", "app006", "app007"]
    assert page3 == ["app008", "app009"]


def test_count_matches_query_ids_length():
    apps = [_e("a", source_kind="appgallery"),
            _e("b", source_kind="appgallery"),
            _e("c", source_kind="direct")]
    store = CatalogStore.from_apps(apps)
    assert store.count(source="appgallery") == 2
    assert store.count(source="appgallery") == len(
        store.query_ids(source="appgallery"))


# =============================================================================
# build_to_path — disk smoke test
# =============================================================================


def test_build_to_path_writes_file_and_reopens(tmp_path):
    db_path = tmp_path / "catalog.sqlite3"
    apps = [_e("a"), _e("b"), _e("c")]
    CatalogStore.build_to_path(
        db_path, apps=apps, generated_at="2026-05-07T00:00:00Z")
    assert db_path.is_file()

    store = CatalogStore.open_readonly(db_path)
    try:
        assert store.total() == 3
        assert store.generated_at() == "2026-05-07T00:00:00Z"
        assert store.query_ids() == ["a", "b", "c"]
    finally:
        store.close()


def test_build_to_path_is_idempotent_drops_old_rows(tmp_path):
    db_path = tmp_path / "catalog.sqlite3"
    CatalogStore.build_to_path(db_path, apps=[_e("a"), _e("b")])
    CatalogStore.build_to_path(db_path, apps=[_e("c")])  # full rebuild
    store = CatalogStore.open_readonly(db_path)
    try:
        assert store.query_ids() == ["c"]
    finally:
        store.close()


def test_open_readonly_rejects_writes(tmp_path):
    db_path = tmp_path / "catalog.sqlite3"
    CatalogStore.build_to_path(db_path, apps=[_e("a")])
    store = CatalogStore.open_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            store._conn.execute("DELETE FROM apps")
    finally:
        store.close()


# =============================================================================
# merge_apps — extras-overlay precedence
# =============================================================================


def test_merge_apps_lets_extras_win_on_collision():
    primary = _e("ru.kinopoisk", name="kinopoisk-from-feed",
                 source_kind="appgallery")
    extras_e = _e("ru.kinopoisk", name="Кинопоиск (curated)",
                  source_kind="appgallery", tested=True)
    merged = merge_apps([primary, _e("other")], [extras_e])
    by_id = {e.id: e for e in merged}
    # Extras win for clashing ids — curated name + tested flag survive.
    assert by_id["ru.kinopoisk"].name == "Кинопоиск (curated)"
    assert by_id["ru.kinopoisk"].tested is True
    # Non-clashing primary entry survives.
    assert "other" in by_id


def test_default_db_path_lives_under_home():
    p = default_db_path()
    assert p.name == "catalog.sqlite3"
    assert ".ivi-installer" in p.parts
