"""Catalog of apps shown in the Store tab.

The catalog has two pieces:

* a bundled ``resources/extras.json`` with ~40 hand-curated RU/CIS
  apps verified at build time via the AppGallery API (one entry,
  Telegram, ships with a direct-download URL);
* a live AppGallery feed pulled on demand via
  ``ivi_installer.sources.appgallery_index.fetch_index`` and
  flattened into ``CatalogEntry`` instances by
  ``from_appgallery_index``.

Earlier versions also speculatively pulled the F-Droid v2 index; that
path was removed in v0.19 — F-Droid's content didn't match the
in-car target audience and the 50 MB index ate startup time.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from importlib import resources as _resources
from typing import Iterable, Sequence

log = logging.getLogger(__name__)

# Category labels are kept on the entry for display purposes only —
# the row prints them after the version (e.g. "v1.2 · most_ranked"),
# but the UI no longer offers a Category dropdown to filter on. v0.22
# fetches a single Most-Ranked list from AppGallery and tags every
# entry from it with that label; bundled extras carry whatever label
# the curator wrote into ``extras.json``.
SOURCE_DISPLAY: dict[str, str] = {
    "appgallery": "AppGallery",
    "direct":     "Direct",
}

@dataclass(frozen=True)
class CatalogEntry:
    id: str
    name: str
    category: str
    sources: tuple[dict, ...]
    description_ru: str | None = None
    description_en: str | None = None
    icon_url: str | None = None
    tested: bool = False
    min_api: int | None = None
    size_mb: float | None = None
    version: str | None = None
    homepage: str | None = None
    notes_ru: str | None = None
    notes_en: str | None = None
    initials: str | None = None
    # F-Droid stamps each package with two timestamps in milliseconds
    # since epoch: ``added`` (first published on F-Droid) and
    # ``lastUpdated`` (a new version reached the index). Both feed the
    # store's "New" / "Updated" sections and the sort dropdown. None
    # for sources that don't supply them (extras.json entries).
    added_at: int | None = None
    last_updated_at: int | None = None

    @property
    def primary_source_kind(self) -> str:
        if not self.sources:
            return ""
        return self.sources[0].get("kind", "")

    @property
    def primary_source_label(self) -> str:
        return SOURCE_DISPLAY.get(self.primary_source_kind,
                                  self.primary_source_kind or "?")

    def description(self, lang: str = "ru") -> str:
        if lang == "ru" and self.description_ru:
            return self.description_ru
        if lang == "en" and self.description_en:
            return self.description_en
        return self.description_ru or self.description_en or ""

    def notes(self, lang: str = "ru") -> str | None:
        if lang == "ru" and self.notes_ru:
            return self.notes_ru
        if lang == "en" and self.notes_en:
            return self.notes_en
        return self.notes_ru or self.notes_en


def _coerce_sources(raw: Iterable[dict]) -> tuple[dict, ...]:
    out: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        kind = s.get("kind")
        if not kind:
            continue
        out.append(dict(s))
    return tuple(out)


def _entry_from_dict(d: dict) -> CatalogEntry | None:
    try:
        app_id = d["id"]
        name = d["name"]
        category = d["category"]
        raw_sources = d["sources"]
    except KeyError as e:
        log.warning("catalog: dropping entry missing %s: %r", e, d.get("id"))
        return None
    sources = _coerce_sources(raw_sources)
    if not sources:
        log.warning("catalog: dropping %r — no usable sources", app_id)
        return None
    size_mb = d.get("size_mb")
    if size_mb is not None:
        try:
            size_mb = float(size_mb)
        except (TypeError, ValueError):
            size_mb = None
    min_api = d.get("min_api")
    if min_api is not None:
        try:
            min_api = int(min_api)
        except (TypeError, ValueError):
            min_api = None
    return CatalogEntry(
        id=str(app_id),
        name=str(name),
        category=str(category),
        sources=sources,
        description_ru=d.get("description_ru"),
        description_en=d.get("description_en"),
        icon_url=d.get("icon_url"),
        tested=bool(d.get("tested", False)),
        min_api=min_api,
        size_mb=size_mb,
        version=d.get("version"),
        homepage=d.get("homepage"),
        notes_ru=d.get("notes_ru"),
        notes_en=d.get("notes_en"),
        initials=d.get("initials"),
    )


@dataclass(frozen=True)
class Catalog:
    """A snapshot of the catalog rendered in the Store tab."""
    schema_version: int
    generated_at: str
    apps: tuple[CatalogEntry, ...]


def load_extras() -> Catalog:
    """Load the bundled non-F-Droid extras (``resources/extras.json``).

    These are the entries the F-Droid index can't possibly cover —
    AppGallery-only apps and direct-URL APKs we still want to surface
    in the store. Returned as a Catalog so callers can merge into a
    full catalog with the same shape.
    """
    with _resources.files("ivi_installer.resources").joinpath(
            "extras.json").open("rb") as fh:
        raw = json.loads(fh.read().decode("utf-8"))
    apps_raw = raw.get("apps") or []
    apps: list[CatalogEntry] = []
    for d in apps_raw:
        entry = _entry_from_dict(d)
        if entry is not None:
            apps.append(entry)
    return Catalog(
        schema_version=int(raw.get("schema_version", 2)),
        generated_at=str(raw.get("generated_at", "")),
        apps=tuple(apps),
    )


# Back-compat shim — older code (and tests) called ``load()``.
load = load_extras


# ---------------------------------------------------------------------------
# AppGallery → CatalogEntry conversion
# ---------------------------------------------------------------------------


def _appgallery_size_mb(item: dict) -> float | None:
    raw = item.get("size") or item.get("fullSize")
    try:
        if raw:
            return round(int(raw) / (1024 * 1024), 1)
    except (TypeError, ValueError):
        pass
    return None


def from_appgallery_index(
    index: dict,
    *,
    only_packages: Sequence[str] | None = None,
) -> tuple[CatalogEntry, ...]:
    """Convert an AppGallery index dict (as built by
    ``ivi_installer.sources.appgallery_index.fetch_index``) into a
    deduped tuple of CatalogEntry.

    Every entry's ``category`` is the URI-section label it appeared
    under (in v0.22+ that's always ``most_ranked`` — we no longer
    walk multiple URIs). Items keep AppGallery's API-returned rank
    order via Python dict insertion order.

    ``only_packages`` filters the conversion down to the listed
    packages; useful when seeding the bundled extras.json with
    specific apps and ignoring the rest of the category dump.
    """
    only = set(only_packages) if only_packages else None
    seen: dict[str, CatalogEntry] = {}
    cats = index.get("categories") or {}
    for label, payload in cats.items():
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            pkg = item.get("package") or ""
            appid = item.get("appid") or ""
            if not pkg or not appid:
                continue
            if only is not None and pkg not in only:
                continue
            if pkg in seen:
                continue
            entry = _appgallery_item_to_entry(item, category=label)
            if entry is not None:
                seen[pkg] = entry
    return tuple(seen.values())


def _appgallery_item_to_entry(
    item: dict,
    *,
    category: str,
) -> CatalogEntry | None:
    pkg = item.get("package") or ""
    appid = item.get("appid") or ""
    name = item.get("name") or pkg
    if not pkg or not appid or not name:
        return None
    desc = item.get("memo") or ""
    return CatalogEntry(
        id=str(pkg),
        name=str(name),
        category=str(category or ""),
        sources=(
            {"kind": "appgallery", "id": str(appid)},
        ),
        description_ru=desc,
        description_en=desc,
        icon_url=item.get("icon") or None,
        tested=False,
        # AppGallery doesn't surface minSdkVersion in tab listings —
        # only `targetSDK` (the build's target). Leave min_api unset
        # so the "min Android" line in the row simply doesn't render
        # rather than showing a misleading number.
        min_api=None,
        size_mb=_appgallery_size_mb(item),
        version=item.get("appVersionName") or item.get("versionName") or None,
        homepage=None,
        initials=derive_initials(str(name)),
        added_at=None,
        last_updated_at=None,
    )


# ---------------------------------------------------------------------------
# Catalog merge
# ---------------------------------------------------------------------------


def merge(*catalogs: Catalog | Sequence[CatalogEntry] | None) -> Catalog:
    """Merge several catalogs into one, with later entries overriding
    earlier ones by ``id``.

    Used to overlay the bundled extras (Telegram, AppGallery) on top of
    the F-Droid live list — extras win when an id collides (so a
    hand-curated description / tested flag isn't clobbered).
    """
    by_id: dict[str, CatalogEntry] = {}
    generated_at = ""
    for source in catalogs:
        if source is None:
            continue
        if isinstance(source, Catalog):
            entries = source.apps
            if source.generated_at:
                generated_at = source.generated_at
        else:
            entries = tuple(source)
        for entry in entries:
            by_id[entry.id] = entry
    apps = tuple(sorted(by_id.values(),
                        key=lambda e: (e.name.casefold(), e.id)))
    return Catalog(
        schema_version=2,
        generated_at=generated_at,
        apps=apps,
    )


# ---------------------------------------------------------------------------
# Sections + sorting
# ---------------------------------------------------------------------------

# How recent a timestamp must be (in days) to count toward the "New" /
# "Updated" sections. F-Droid pushes weekly-ish, so a 30-day window
# catches a comfortable handful of packages without going stale.
SECTION_RECENCY_DAYS = 30
_DAY_MS = 24 * 60 * 60 * 1000

# Section ids the UI knows about. The chip row above the toolbar is
# fixed at this exact set — any addition needs both a chip and a
# branch in ``search``.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("all", "All"),
    ("new", "New"),
    ("updated", "Recently updated"),
    ("tested", "Tested"),
)

def _section_passes(
    entry: CatalogEntry, section: str, now_ms: int,
) -> bool:
    if section in (None, "", "all"):
        return True
    cutoff = now_ms - SECTION_RECENCY_DAYS * _DAY_MS
    if section == "new":
        return entry.added_at is not None and entry.added_at >= cutoff
    if section == "updated":
        return (
            entry.last_updated_at is not None
            and entry.last_updated_at >= cutoff
        )
    if section == "tested":
        return entry.tested
    # Unknown section — fail-open so a UI/state mismatch doesn't blank
    # the catalog entirely.
    return True


def search(
    apps: Sequence[CatalogEntry],
    *,
    query: str = "",
    category: str | None = None,
    source: str | None = None,
    tested_only: bool = False,
    min_api_at_most: int | None = None,
    section: str | None = None,
    now_ms: int | None = None,
) -> list[CatalogEntry]:
    """Pure filter — never touches the network.

    Layered filters, all AND-ed together:

    * ``query`` — case-insensitive substring across name / category /
      descriptions / id.
    * ``category`` — exact match against our nine-category taxonomy
      (``"all"`` / None is a no-op).
    * ``source`` — primary source ``kind`` match (accepts both raw
      kinds like ``fdroid`` and display labels like ``F-Droid``).
    * ``tested_only`` — boolean filter on the ``tested`` flag.
    * ``section`` — preset filter for the chip row: ``"new"`` /
      ``"updated"`` (timestamps within ``SECTION_RECENCY_DAYS`` of
      ``now_ms``) / ``"tested"`` / ``"all"``.
    * ``min_api_at_most`` — drop entries needing a higher SDK.

    ``now_ms`` is injectable so tests can pin "now" deterministically;
    when omitted, ``time.time()`` is consulted lazily (only if a
    time-aware section is active).
    """
    q = (query or "").strip().lower()
    cat = (category or "").strip().lower()
    src = (source or "").strip().lower()
    sec = (section or "").strip().lower() or "all"

    # Map display names → kind for filter convenience.
    src_norm = src
    for k, v in SOURCE_DISPLAY.items():
        if v.lower() == src:
            src_norm = k
            break

    if sec in ("new", "updated") and now_ms is None:
        import time as _time
        now_ms = int(_time.time() * 1000)

    out: list[CatalogEntry] = []
    for app in apps:
        if q:
            haystack = " ".join((
                app.name.lower(),
                app.category.lower(),
                (app.description_ru or "").lower(),
                (app.description_en or "").lower(),
                app.id.lower(),
            ))
            if q not in haystack:
                continue
        if cat and cat != "all" and app.category.lower() != cat:
            continue
        if src_norm and src_norm != "all" \
                and app.primary_source_kind.lower() != src_norm:
            continue
        if tested_only and not app.tested:
            continue
        if min_api_at_most is not None and app.min_api is not None \
                and app.min_api > min_api_at_most:
            continue
        if not _section_passes(app, sec, now_ms or 0):
            continue
        out.append(app)
    return out


def derive_initials(name: str) -> str:
    """Two-character display fallback used by the row icon plate."""
    if not name:
        return "··"
    parts = [p for p in name.split() if p]
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return parts[0][:2].upper() if parts else "··"


__all__ = [
    "SECTIONS",
    "SECTION_RECENCY_DAYS",
    "SOURCE_DISPLAY",
    "Catalog",
    "CatalogEntry",
    "derive_initials",
    "from_appgallery_index",
    "load",
    "load_extras",
    "merge",
    "search",
]
