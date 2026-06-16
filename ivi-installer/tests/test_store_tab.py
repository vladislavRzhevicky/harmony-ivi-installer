"""Tests for ivi_installer.ui.store_tab.

The store tab now renders lazily (one batch of ``BATCH_SIZE`` rows on
load, more on scroll), so most tests build a synthetic 5-entry catalog
that's well below the batch size — every entry materialises a row in
the first paint.
"""
from __future__ import annotations

from PySide6.QtWidgets import QApplication
import pytest

from ivi_installer import catalog
from ivi_installer.catalog import Catalog, CatalogEntry
from ivi_installer.catalog_store import CatalogStore
from ivi_installer.ui.store_tab import (
    PHASE_DOWNLOADING,
    PHASE_FAILED,
    PHASE_SUCCESS,
    StoreTab,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def _entry(
    app_id: str,
    *,
    name: str | None = None,
    category: str = "tools",
    source_kind: str = "appgallery",
    tested: bool = False,
) -> CatalogEntry:
    return CatalogEntry(
        id=app_id,
        name=name or app_id.rsplit(".", 1)[-1],
        category=category,
        sources=({"kind": source_kind, "package": app_id},),
        description_ru=f"описание {app_id}",
        description_en=f"description {app_id}",
        tested=tested,
        min_api=21,
        size_mb=12.0,
        version="1.0",
        initials=catalog.derive_initials(name or app_id),
    )


@pytest.fixture
def cat():
    """Tiny synthetic catalog that stays below BATCH_SIZE so every row
    is rendered immediately."""
    apps = (
        _entry("org.schabi.newpipe", name="NewPipe", category="entertainment", tested=True),
        _entry("de.danoeh.antennapod", name="AntennaPod", category="entertainment", tested=True),
        _entry("org.fdroid.example.audio", name="ExampleAudio", category="entertainment"),
        _entry("ru.kinopoisk", name="Кинопоиск", category="entertainment",
               source_kind="appgallery"),
        _entry("org.telegram.messenger", name="Telegram", category="social",
               source_kind="direct", tested=True),
    )
    return Catalog(schema_version=2, generated_at="2026-05-07T00:00:00Z", apps=apps)


def _visible_ids(tab: StoreTab) -> list[str]:
    """The ids that the filter currently keeps visible.

    With lazy rendering, an entry can pass the filter even before its
    row has been materialised — ``_filtered_ids`` is the source of
    truth. ``isHidden()`` on materialised rows still flips correctly.
    """
    return list(tab._filtered_ids)


def test_store_tab_renders_first_batch(qapp, cat):
    """Catalogs smaller than BATCH_SIZE materialise every row at once."""
    tab = StoreTab(cat)
    assert len(tab._filtered_ids) == len(cat.apps)
    # All rows materialised because the catalog fits in one batch.
    assert len(tab._rendered_ids) == len(cat.apps)


def test_search_narrows_visible_rows(qapp, cat):
    tab = StoreTab(cat)
    tab.search_input.setText("newpipe")
    QApplication.processEvents()
    visible = _visible_ids(tab)
    assert "org.schabi.newpipe" in visible
    assert "ru.kinopoisk" not in visible


def test_no_category_dropdown_in_toolbar(qapp, cat):
    """v0.22 dropped the Category filter — the toolbar only carries
    a search input, source dropdown, the AppGallery toggle, and the
    refresh button."""
    tab = StoreTab(cat)
    assert not hasattr(tab, "category_combo")


def test_source_filter_uses_display_or_kind(qapp, cat):
    tab = StoreTab(cat)
    idx = next(i for i in range(tab.source_combo.count())
               if tab.source_combo.itemData(i) == "appgallery")
    tab.source_combo.setCurrentIndex(idx)
    QApplication.processEvents()
    visible = _visible_ids(tab)
    for entry in cat.apps:
        if entry.id in visible:
            assert entry.primary_source_kind == "appgallery"




def test_install_button_emits_signal(qapp, cat):
    tab = StoreTab(cat)
    captured: list = []
    tab.install_requested.connect(captured.append)
    first = cat.apps[0]
    tab._rows[first.id]._on_clicked()
    assert captured == [first]


def test_set_install_phase_progresses_button(qapp, cat):
    tab = StoreTab(cat)
    first = cat.apps[0]
    tab.set_install_phase(first.id, PHASE_DOWNLOADING, progress=42)
    btn = tab._rows[first.id]._button
    assert "%" in btn.text()
    tab.set_install_phase(first.id, PHASE_SUCCESS)
    assert "Installed" in tab._rows[first.id]._button.text()


def test_set_install_phase_failed_shows_retry(qapp, cat):
    tab = StoreTab(cat)
    first = cat.apps[0]
    tab.set_install_phase(first.id, PHASE_FAILED)
    assert "Retry" in tab._rows[first.id]._button.text()


def test_global_busy_disables_other_install_buttons(qapp, cat):
    tab = StoreTab(cat)
    tab.set_global_busy(True)
    # Only materialised rows respond to set_global_busy. With the tiny
    # synthetic catalog every row is rendered, so this still verifies
    # the lock holds across the whole list.
    assert tab._rows  # we have some materialised rows
    for row in tab._rows.values():
        assert not row._button.isEnabled()
    tab.set_global_busy(False)
    for row in tab._rows.values():
        assert row._button.isEnabled()


def test_show_error_view(qapp, cat):
    tab = StoreTab(cat)
    tab.show_error("ECONNRESET")
    assert tab._body.currentIndex() == 2


def test_search_with_no_matches_shows_empty(qapp, cat):
    tab = StoreTab(cat)
    tab.search_input.setText("zzznevermatchanything")
    QApplication.processEvents()
    assert tab._body.currentIndex() == 1   # empty page


def test_reset_filters_button_clears_state(qapp, cat):
    tab = StoreTab(cat)
    tab.search_input.setText("foo")
    idx = next(i for i in range(tab.source_combo.count())
               if tab.source_combo.itemData(i) == "appgallery")
    tab.source_combo.setCurrentIndex(idx)
    tab._reset_filters()
    assert tab.search_input.text() == ""
    assert tab.source_combo.currentData() == "all"


def test_filter_preserves_appgallery_insertion_order(qapp):
    """Without a Sort combo, the catalog list comes back in the
    order entries were INSERTed into the SQLite store. That order is
    AppGallery's own (categories iterate in DEFAULT_CATEGORY_URIS
    order, items in API rank order)."""
    apps = (
        CatalogEntry(
            id="z.app", name="Zebra", category="tools",
            sources=({"kind": "appgallery", "package": "z"},),
        ),
        CatalogEntry(
            id="a.app", name="Aardvark", category="tools",
            sources=({"kind": "appgallery", "package": "a"},),
        ),
        CatalogEntry(
            id="m.app", name="Middle", category="tools",
            sources=({"kind": "appgallery", "package": "m"},),
        ),
    )
    tab = StoreTab(Catalog(schema_version=2, generated_at="", apps=apps))
    # NOT sorted alphabetically — original tuple order survives.
    assert tab._filtered_ids == ["z.app", "a.app", "m.app"]




def test_lazy_render_only_first_batch_for_large_catalog(qapp):
    """A catalog with more than BATCH_SIZE entries renders only the
    first batch upfront. The remaining entries are still in
    ``_filtered_ids`` so the filter / footer counts work, but no
    AppRow widgets exist for them yet."""
    big = tuple(
        _entry(f"app.example{i:04d}", name=f"App {i:04d}")
        for i in range(StoreTab.BATCH_SIZE * 3 + 5)
    )
    big_cat = Catalog(schema_version=2, generated_at="", apps=big)
    tab = StoreTab(big_cat)
    assert len(tab._filtered_ids) == len(big)
    # Rendered exactly the first batch — not all entries.
    assert len(tab._rendered_ids) == StoreTab.BATCH_SIZE
    assert len(tab._rows) == StoreTab.BATCH_SIZE


def test_set_catalog_swap_replaces_entries(qapp, cat):
    """``set_catalog`` clears cached rows so a new live-fetched catalog
    fully replaces a placeholder one."""
    tab = StoreTab(None)
    assert tab._filtered_ids == []
    tab.set_catalog(cat)
    assert len(tab._filtered_ids) == len(cat.apps)
    assert "org.schabi.newpipe" in tab._rows


def test_appgallery_toggle_emits_signal_with_state(qapp, cat):
    tab = StoreTab(cat)
    captured: list[bool] = []
    tab.appgallery_toggle_requested.connect(captured.append)
    tab.appgallery_toggle.click()
    assert captured == [True]
    tab.appgallery_toggle.click()
    assert captured == [True, False]


def test_set_appgallery_toggle_does_not_re_emit(qapp, cat):
    """Programmatic state sync (e.g. when main_window restores the
    saved preference at launch) must NOT bounce the signal back —
    that would loop with the main_window toggle handler."""
    tab = StoreTab(cat)
    captured: list[bool] = []
    tab.appgallery_toggle_requested.connect(captured.append)
    tab.set_appgallery_toggle(True)
    tab.set_appgallery_toggle(False)
    assert captured == []


# =============================================================================
# Search dropdown — hot keywords + completeSearchWord
# =============================================================================


def test_search_text_change_starts_typeahead_debounce(qapp, cat):
    """Typing a non-empty query should arm the debounce timer so the
    network call only fires on pause, not per keystroke."""
    tab = StoreTab(cat)
    tab.search_input.setText("tik")
    assert tab._typeahead_timer.isActive()


def test_search_text_change_cancels_typeahead_when_input_cleared(qapp, cat):
    tab = StoreTab(cat)
    tab.search_input.setText("tik")
    assert tab._typeahead_timer.isActive()
    tab.search_input.setText("")
    assert not tab._typeahead_timer.isActive()


def test_search_typeahead_emits_with_trimmed_keyword(qapp, cat):
    tab = StoreTab(cat)
    captured: list[str] = []
    tab.search_suggestions_requested.connect(captured.append)
    tab.search_input.setText("  tik  ")
    # Fire the timer synchronously instead of waiting on the real
    # SEARCH_TYPEAHEAD_DEBOUNCE_MS window.
    tab._typeahead_timer.stop()
    tab._on_typeahead_timer_fired()
    assert captured == ["tik"]


def test_set_search_suggestions_drops_stale_results(qapp, cat):
    """If the user has moved on (cleared the input or typed something
    else), arriving suggestions must not show up — that would flash a
    panel for an old query."""
    tab = StoreTab(cat)
    tab.search_input.setText("tik")
    # Network thread is "still running"; user clears the input.
    tab.search_input.setText("")
    tab.set_search_suggestions(
        "tik", ["tik", "TikTok"],
        {"appid": "C1", "name": "TikTok"})
    # Dropdown shouldn't have a typeahead view — either hidden or
    # showing the (empty) hot panel.
    if tab._suggest_dropdown is not None:
        assert not tab._suggest_dropdown.isVisible()


def test_set_hot_keywords_caches_for_next_focus(qapp, cat):
    """Calling set_hot_keywords without focus should still cache the
    list so the next focus shows them without another network call."""
    tab = StoreTab(cat)
    tab.set_hot_keywords(["foo", "bar"])
    assert tab._hot_keywords == ["foo", "bar"]


def test_suggested_app_install_emits_full_item_dict(qapp, cat):
    """Clicking the top-match app card must emit the raw AppGallery
    item dict so main_window can build a CatalogEntry from it (name,
    icon, package, C-id)."""
    tab = StoreTab(cat)
    captured: list[dict] = []
    tab.suggested_app_install_requested.connect(captured.append)
    item = {
        "appid": "C100315379",
        "package": "com.zhiliaoapp.musically",
        "name": "TikTok",
        "kindName": "Entertainment",
        "icon": "https://example/icon.png",
    }
    # Drive the popup-private handler directly — exercising the QPushButton
    # click would require the popup to be parented and visible.
    tab._on_suggestion_app_clicked(item)
    assert captured == [item]


def test_suggestion_keyword_click_fills_input_and_filters(qapp, cat):
    tab = StoreTab(cat)
    # No suggestion-flood signal: clicking a keyword fills the input
    # locally and runs the local filter.
    captured: list[str] = []
    tab.search_suggestions_requested.connect(captured.append)
    tab._on_suggestion_keyword_clicked("Telegram")
    assert tab.search_input.text() == "Telegram"
    # No typeahead emit — clicking a suggestion is the END state.
    assert captured == []


def test_focus_in_when_input_empty_requests_hot_keywords(qapp, cat):
    """First focus on an empty input asks the main window to fetch
    the hot-keyword list (one-shot — the cached list is reused on
    later focuses)."""
    tab = StoreTab(cat)
    captured: list[None] = []
    tab.hot_keywords_requested.connect(lambda: captured.append(None))
    # Drive the focus path manually; avoids needing a window manager.
    tab._show_hot_keywords_dropdown()
    assert captured == [None]
    # Second invocation while still empty doesn't re-fire.
    tab._show_hot_keywords_dropdown()
    assert captured == [None]
