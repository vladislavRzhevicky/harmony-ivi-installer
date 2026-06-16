"""SQLite-backed catalog store.

Replaces the in-memory ``tuple[CatalogEntry, ...]`` that the Store tab
held in v0.15. With ~4400 entries, an in-memory list spends ~8 MB on
dataclass instances we mostly never look at — every UI render only
needs the ~80 entries currently on screen plus the (filtered, sorted)
id sequence.

Layout
------
* On-disk file at ``~/.ivi-installer/cache/catalog.sqlite3``. Built on
  first launch from the F-Droid index (the JSON cache stays on disk
  too — it's the wire-format layer; SQLite is the query layer).
* Schema: a single ``apps`` table with one column per
  ``CatalogEntry`` field plus pre-computed ``name_lower`` and
  ``search_blob`` columns (lower-cased concat of all searchable
  text), so query-time work is just `LIKE '%foo%'` against an index.
* Indexes on category / source_kind / tested / added_at /
  last_updated_at / size_mb / name_lower for the chips + sort
  combinations. The full list is on the order of kilobytes — cheap.
* WAL journal mode so the writer (background worker) and reader (UI
  thread) don't block each other.

Concurrency
-----------
SQLite connections aren't thread-safe by default. We keep that
constraint: each ``CatalogStore`` instance owns one connection,
created in the thread that opened it. The worker that REBUILDS the
database (CatalogFetchWorker) opens its own writer, writes inside a
transaction, commits, closes. The UI opens a read-only handle. WAL
makes that combination safe.

In-memory mode
--------------
``CatalogStore.from_apps(...)`` opens a ``:memory:`` SQLite and writes
the supplied entries — used by the extras-only seed catalog and by
all the unit tests, so the codepath is identical to the production
on-disk path.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, Sequence

from .catalog import (
    CatalogEntry,
    SECTION_RECENCY_DAYS,
    SOURCE_DISPLAY,
)

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_DAY_MS = 24 * 60 * 60 * 1000

# DDL kept inline — the schema is small enough that an external .sql
# file would just hide it. Versioned so a future schema bump can wipe
# the on-disk cache and re-build.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS apps (
    id              TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    name_lower      TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    source_kind     TEXT    NOT NULL,
    sources_json    TEXT    NOT NULL,     -- json-encoded list of source dicts
    description_ru  TEXT,
    description_en  TEXT,
    search_blob     TEXT    NOT NULL,     -- lowered concat for LIKE search
    icon_url        TEXT,
    tested          INTEGER NOT NULL DEFAULT 0,
    min_api         INTEGER,
    size_mb         REAL,
    version         TEXT,
    homepage        TEXT,
    notes_ru        TEXT,
    notes_en        TEXT,
    initials        TEXT,
    added_at        INTEGER,
    last_updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS apps_source_kind_idx  ON apps(source_kind);
CREATE INDEX IF NOT EXISTS apps_tested_idx       ON apps(tested);
CREATE INDEX IF NOT EXISTS apps_name_lower_idx   ON apps(name_lower);
CREATE INDEX IF NOT EXISTS apps_added_idx        ON apps(added_at);
CREATE INDEX IF NOT EXISTS apps_last_updated_idx ON apps(last_updated_at);
CREATE INDEX IF NOT EXISTS apps_size_idx         ON apps(size_mb);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def default_db_path() -> Path:
    return Path.home() / ".ivi-installer" / "cache" / "catalog.sqlite3"


# ---------------------------------------------------------------------------
# row <-> CatalogEntry
# ---------------------------------------------------------------------------


def _entry_to_row(entry: CatalogEntry) -> tuple:
    sources = list(entry.sources or ())
    return (
        entry.id,
        entry.name,
        entry.name.casefold(),
        entry.category.lower(),
        entry.primary_source_kind.lower(),
        json.dumps(sources, ensure_ascii=False),
        entry.description_ru,
        entry.description_en,
        _search_blob(entry),
        entry.icon_url,
        1 if entry.tested else 0,
        entry.min_api,
        entry.size_mb,
        entry.version,
        entry.homepage,
        entry.notes_ru,
        entry.notes_en,
        entry.initials,
        entry.added_at,
        entry.last_updated_at,
    )


def _search_blob(entry: CatalogEntry) -> str:
    parts: list[str] = [
        entry.name or "",
        entry.category or "",
        entry.description_ru or "",
        entry.description_en or "",
        entry.id or "",
    ]
    return " ".join(p for p in parts if p).casefold()


_INSERT_SQL = """
INSERT OR REPLACE INTO apps (
    id, name, name_lower, category, source_kind, sources_json,
    description_ru, description_en, search_blob, icon_url, tested,
    min_api, size_mb, version, homepage, notes_ru, notes_en,
    initials, added_at, last_updated_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _row_to_entry(row: sqlite3.Row) -> CatalogEntry:
    raw_sources = json.loads(row["sources_json"]) if row["sources_json"] else []
    sources = tuple(s for s in raw_sources if isinstance(s, dict))
    return CatalogEntry(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        sources=sources,
        description_ru=row["description_ru"],
        description_en=row["description_en"],
        icon_url=row["icon_url"],
        tested=bool(row["tested"]),
        min_api=row["min_api"],
        size_mb=row["size_mb"],
        version=row["version"],
        homepage=row["homepage"],
        notes_ru=row["notes_ru"],
        notes_en=row["notes_en"],
        initials=row["initials"],
        added_at=row["added_at"],
        last_updated_at=row["last_updated_at"],
    )


# ---------------------------------------------------------------------------
# CatalogStore
# ---------------------------------------------------------------------------


class CatalogStore:
    """SQLite-backed catalog query layer.

    A store holds exactly one connection. Build on a worker thread,
    pass the *path* to the UI thread, and have the UI open its own
    read-only handle via :meth:`open_readonly`.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        path: str | None = None,
        owns_connection: bool = True,
    ):
        self._conn = conn
        self._path = path
        self._owns = owns_connection
        # Row-as-mapping helps when we hand rows out to ``_row_to_entry``.
        conn.row_factory = sqlite3.Row

    # ---- construction ----

    @classmethod
    def open_readonly(cls, path: Path) -> "CatalogStore":
        """Open ``path`` for read-only access. Used by the UI thread."""
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        return cls(conn, path=str(path))

    @classmethod
    def from_apps(
        cls,
        apps: Iterable[CatalogEntry],
        *,
        generated_at: str = "",
    ) -> "CatalogStore":
        """Build an in-memory store from an iterable of CatalogEntry.

        Used by tests + the extras-only fallback when the live F-Droid
        index hasn't loaded yet. Same code path as the on-disk build,
        only the connection target differs.
        """
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        store = cls(conn, path=None)
        store._apply_schema()
        store._write_entries(apps)
        store._set_meta("generated_at", generated_at)
        store._set_meta("schema_version", str(SCHEMA_VERSION))
        return store

    @classmethod
    def build_to_path(
        cls,
        path: Path,
        *,
        apps: Iterable[CatalogEntry],
        generated_at: str = "",
    ) -> Path:
        """Atomically (re-)build the SQLite at ``path`` from ``apps``.

        Writes inside a single ``IMMEDIATE`` transaction — readers on
        WAL keep seeing the previous catalog until the COMMIT lands.
        Returns the path so the caller can chain into ``open_readonly``.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), check_same_thread=False)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            store = cls(conn, path=str(path))
            store._apply_schema()
            with conn:
                conn.execute("DELETE FROM apps")
                cur = conn.cursor()
                cur.executemany(
                    _INSERT_SQL,
                    [_entry_to_row(a) for a in apps],
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("generated_at", generated_at or ""),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    ("built_at", str(int(time.time()))),
                )
        finally:
            conn.close()
        return path

    # ---- internal helpers ----

    def _apply_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA_SQL)

    def _write_entries(self, apps: Iterable[CatalogEntry]) -> None:
        rows = [_entry_to_row(a) for a in apps]
        with self._conn:
            self._conn.executemany(_INSERT_SQL, rows)

    def _set_meta(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        cur = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row is not None else default

    # ---- queries ----

    def total(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM apps").fetchone()[0]

    def generated_at(self) -> str:
        return self.get_meta("generated_at", "")

    def query_ids(
        self,
        *,
        query: str = "",
        source: str | None = None,
        section: str | None = None,
        now_ms: int | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[str]:
        """Return the ids that match all filters, in **AppGallery's
        own order** — preserved via ``ORDER BY rowid`` since the
        SQLite ``rowid`` is monotonically assigned at INSERT time and
        we INSERT in the order ``from_appgallery_index`` produces.

        That order is the AppGallery API's rank order for the
        Most-Ranked list, with curated extras overlaid first via
        ``merge_apps`` so they take the lowest rowids.

        ``limit=None`` returns the entire match set; for paged
        rendering pass ``limit=BATCH_SIZE`` and bump ``offset`` per
        page. The implementation runs a single SQL — no Python-side
        scanning.
        """
        where, params = self._build_where(
            query=query, source=source,
            section=section, now_ms=now_ms,
        )
        sql = f"SELECT id FROM apps {where} ORDER BY rowid"
        if limit is not None:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        cur = self._conn.execute(sql, params)
        return [row["id"] for row in cur]

    def count(
        self,
        *,
        query: str = "",
        source: str | None = None,
        section: str | None = None,
        now_ms: int | None = None,
    ) -> int:
        where, params = self._build_where(
            query=query, source=source,
            section=section, now_ms=now_ms,
        )
        sql = f"SELECT COUNT(*) FROM apps {where}"
        return self._conn.execute(sql, params).fetchone()[0]

    def fetch(self, app_id: str) -> CatalogEntry | None:
        cur = self._conn.execute(
            "SELECT * FROM apps WHERE id = ? LIMIT 1", (app_id,))
        row = cur.fetchone()
        return _row_to_entry(row) if row is not None else None

    def fetch_many(self, ids: Sequence[str]) -> list[CatalogEntry]:
        if not ids:
            return []
        # Preserve the requested order — SQLite's IN doesn't guarantee
        # that. Build a temp lookup, then re-order.
        placeholders = ",".join("?" * len(ids))
        cur = self._conn.execute(
            f"SELECT * FROM apps WHERE id IN ({placeholders})",
            tuple(ids),
        )
        by_id = {row["id"]: _row_to_entry(row) for row in cur}
        return [by_id[i] for i in ids if i in by_id]

    # ---- where / order builders ----

    @staticmethod
    def _normalise_source(source: str) -> str:
        s = (source or "").strip().lower()
        for kind, label in SOURCE_DISPLAY.items():
            if label.lower() == s:
                return kind
        return s

    def _build_where(
        self,
        *,
        query: str,
        source: str | None,
        section: str | None,
        now_ms: int | None,
    ) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []

        q = (query or "").strip().casefold()
        if q:
            clauses.append("search_blob LIKE ?")
            params.append(f"%{q}%")

        src = self._normalise_source(source or "")
        if src and src != "all":
            clauses.append("source_kind = ?")
            params.append(src)

        sec = (section or "").strip().lower() or "all"
        if sec == "tested":
            clauses.append("tested = 1")
        elif sec in ("new", "updated"):
            if now_ms is None:
                now_ms = int(time.time() * 1000)
            cutoff = now_ms - SECTION_RECENCY_DAYS * _DAY_MS
            field = "added_at" if sec == "new" else "last_updated_at"
            clauses.append(f"{field} IS NOT NULL AND {field} >= ?")
            params.append(cutoff)

        return ("WHERE " + " AND ".join(clauses)) if clauses else "", params

    # ---- lifecycle ----

    def close(self) -> None:
        if self._owns and self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None  # type: ignore[assignment]

    def __enter__(self) -> "CatalogStore":
        return self

    def __exit__(self, *_a) -> None:
        self.close()


def merge_apps(
    primary: Iterable[CatalogEntry],
    extras: Iterable[CatalogEntry],
) -> tuple[CatalogEntry, ...]:
    """Combine two iterables of entries; ``extras`` win on id collision
    AND are emitted FIRST in the resulting tuple.

    Used at build time to overlay the ~40 curated entries (Telegram
    via direct download + AppGallery curated picks) on top of the
    live AppGallery index. Curated entries surface at the top of the
    catalog because the SQLite store keeps them in insertion order
    and the UI shows that order verbatim.
    """
    by_id: dict[str, CatalogEntry] = {}
    # Extras first so they take the lowest rowids in SQLite — the UI
    # then shows them at the top of the All view. Their order in
    # extras.json is preserved.
    for e in extras:
        by_id[e.id] = e
    for e in primary:
        if e.id in by_id:
            continue
        by_id[e.id] = e
    return tuple(by_id.values())


__all__ = [
    "CatalogStore",
    "default_db_path",
    "merge_apps",
    "SCHEMA_VERSION",
]
