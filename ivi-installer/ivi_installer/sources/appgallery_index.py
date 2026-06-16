"""Live discovery of apps via Huawei AppGallery's web API.

The web app (`https://appgallery.huawei.com`) speaks to a JSON edge
service at `https://web-dre.hispace.dbankcloud.com/edge`. Reverse-
engineered from the production JS bundle. Two transport styles:

A. **Authed `/edge/uowap/index`** — list/category dumps.
   1. Bootstrap a session by GETting the AppGallery homepage (sets
      `HWWAFSESID` / `HWWAFSESTIME` cookies).
   2. POST `/edge/webedge/getInterfaceCode` (empty JSON body) to get a
      short-lived JWT (~15 min). The JWT body is HMAC-signed against
      the request's User-Agent, so subsequent calls must reuse the
      same UA.
   3. POST `/edge/uowap/index` form-urlencoded with
      `Interface-Code: <jwt>_<ts_ms>` and the desired `method=...`.
      The web app uses GET URLs in DevTools but the actual transport
      is POST — GETs return 1002 "InterfaceCode Verification failed".

B. **Unauthenticated `/edge/index/*`** — search-suggestion endpoints.
   No JWT, no cookies, plain JSON body. Used by the search bar's
   "hot keywords" panel and the type-ahead suggestion dropdown.

Methods exposed here:

* :func:`category_items` / :func:`fetch_index` — walk the curated
  Most-Ranked URI and cache the result on disk (24 h TTL).
* :func:`search` — keyword search via ``getTabDetail`` with
  ``uri=searchapp|<keyword>``.
* :func:`get_hot_search_list` — the hot-keyword panel shown when the
  user focuses the search input.
* :func:`complete_search_word` — type-ahead: ``{list, app}`` for the
  keyword the user is typing.

This module is the AppGallery analogue of `sources/fdroid.py`. It's
used by the catalog tab as a discovery source (toggle in the UI) and
by the one-shot extras-builder script that seeds the bundled
``resources/extras.json`` with verified C-ids.
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Iterable

log = logging.getLogger(__name__)

# Edge shards. ``europe`` is the safe default for desktop hosts in
# Europe / RU / CIS. Other shards are listed for completeness; the
# server returns the same schema regardless.
SHARDS: dict[str, str] = {
    "europe":    "https://web-dre.hispace.dbankcloud.com/edge",
    "russia":    "https://web-drru.hispace.dbankcloud.com/edge",
    "singapore": "https://web-dra.hispace.dbankcloud.com/edge",
    "china":     "https://web-drcn.hispace.dbankcloud.com/edge",
}

DEFAULT_SHARD = "europe"

# AppGallery validates the JWT against the User-Agent. We pin a real
# desktop Chrome UA — generic curl / urllib UAs get rejected at the
# WAF before they reach the IC check.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Most Ranked is the curated cross-category Top list AppGallery's web
# app shows on its landing page. The compound URI follows the form
# ``automore|doublecolumncard|<page>|A02000`` — ``903219`` is the
# Top → Most Ranked page on the EU shard, statKey ``A02000``.
#
# Up to v0.21 we walked twelve category URIs (Cars / Navigation / …)
# and tagged each app with the section it surfaced from. v0.22 drops
# the Category dropdown and pulls only this one list so the cold-fetch
# is ~3-5 s instead of ~33 s. Every app gets the literal label
# ``most_ranked`` — the catalog row prints it after the version, the
# UI doesn't filter on it.
MOST_RANKED_LABEL = "most_ranked"
MOST_RANKED_URI = "automore|doublecolumncard|903219|A02000"
DEFAULT_CATEGORY_URIS: tuple[tuple[str, str], ...] = (
    (MOST_RANKED_LABEL, MOST_RANKED_URI),
)

INDEX_TTL_SECONDS = 24 * 3600
DEFAULT_PAGE_SIZE = 25
# Each category caps at this many pages. AppGallery typically stops
# itself with ``hasNextPage=0`` well before this cap on small
# categories (Cars, Navigation), but the bigger ones (Tools,
# Entertainment, Most Ranked) saturate it. 20 × 25 = up to 500 items
# per category, ~1 minute total on a cold cache, instantly served
# from the 24 h disk cache after.
DEFAULT_MAX_PAGES = 20


def _ssl_context() -> ssl.SSLContext:
    # Same trade-off as fdroid.py: relax verification because some
    # corporate networks ship broken trust stores. Payloads we actually
    # download are validated separately (size + ZIP magic + sha256 in
    # the install path).
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# ---------------------------------------------------------------------------
# Session — cookies + interfaceCode JWT
# ---------------------------------------------------------------------------


class AppGallerySession:
    """One opener + one JWT, scoped to one shard.

    A session lasts ~15 minutes (the JWT's lifetime). Build a new one
    at the top of every batch; don't try to refresh in place.
    """

    def __init__(self, shard: str = DEFAULT_SHARD):
        if shard not in SHARDS:
            raise ValueError(f"unknown shard {shard!r}")
        self.shard = shard
        self.base = SHARDS[shard]
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context()),
            urllib.request.HTTPCookieProcessor(self._cj),
        )
        self._opener.addheaders = [
            ("User-Agent", _USER_AGENT),
            ("Origin", "https://appgallery.huawei.com"),
            ("Referer", "https://appgallery.huawei.com/"),
        ]
        self._ic: str | None = None
        self._ic_iat: int = 0

    def _bootstrap_cookies(self) -> None:
        """GET appgallery.huawei.com to seed `HWWAFSESID` cookies.

        The cookies aren't strictly required for the IC fetch to
        succeed, but the API call after IC fetch is gated on a valid
        session cookie issued by the same edge shard, and the bootstrap
        request is what populates the cookie jar with both.
        """
        with self._opener.open(
                "https://appgallery.huawei.com/", timeout=10) as resp:
            resp.read()

    def _fetch_interface_code(self) -> str:
        req = urllib.request.Request(
            f"{self.base}/webedge/getInterfaceCode",
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self._opener.open(req, timeout=10) as resp:
            ic = json.loads(resp.read())
        if not isinstance(ic, str) or "." not in ic:
            raise RuntimeError(
                f"appgallery: getInterfaceCode returned unexpected shape: "
                f"{type(ic).__name__} {repr(ic)[:80]}")
        self._ic = ic
        # Decode iat for liveness tracking. Format is JWT body base64url.
        try:
            import base64
            mid = ic.split(".")[1]
            mid += "=" * (-len(mid) % 4)
            payload = json.loads(base64.urlsafe_b64decode(mid))
            self._ic_iat = int(payload.get("iat") or 0)
        except Exception:
            self._ic_iat = int(time.time())
        return ic

    def _ensure_ic(self) -> str:
        if self._ic is None:
            self._bootstrap_cookies()
            return self._fetch_interface_code()
        # JWT lifetime is 15 minutes. Re-fetch a few minutes before
        # expiry to give the next call a fresh window.
        if time.time() - self._ic_iat > 12 * 60:
            return self._fetch_interface_code()
        return self._ic

    def post_uowap(
        self,
        method: str,
        *,
        page: int = 1,
        max_results: int = DEFAULT_PAGE_SIZE,
        locale: str = "en",
        zone: str = "",
        ver: str = "1.1",
        extra: dict | None = None,
        timeout: float = 15.0,
    ) -> dict:
        """Authed POST to ``/edge/uowap/index`` with the given method.

        Returns the parsed JSON response. The shape varies by method
        (see callers).
        """
        ic = self._ensure_ic()
        ts = int(time.time() * 1000)
        pairs: list[tuple[str, str]] = [
            ("method",       method),
            ("serviceType",  "20"),
            ("reqPageNum",   str(page)),
            ("maxResults",   str(max_results)),
            ("zone",         zone),
            ("locale",       locale),
            ("ver",          ver),
        ]
        if extra:
            for k, v in extra.items():
                pairs.append((k, str(v)))
        body = urllib.parse.urlencode(pairs).encode()
        req = urllib.request.Request(
            f"{self.base}/uowap/index",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json, text/plain, */*",
                "Interface-Code": f"{ic}_{ts}",
            },
        )
        with self._opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read())


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def get_tab_detail(
    session: AppGallerySession,
    *,
    uri: str,
    page: int = 1,
    max_results: int = DEFAULT_PAGE_SIZE,
    locale: str = "ru_RU",
) -> dict:
    """Fetch one page of a tab list. ``uri`` selects the page."""
    return session.post_uowap(
        "internal.getTabDetail",
        page=page, max_results=max_results, locale=locale,
        extra={"uri": uri},
    )


def search(
    session: AppGallerySession,
    keyword: str,
    *,
    max_results: int = 10,
    locale: str = "ru_RU",
) -> list[dict]:
    """Keyword search. AppGallery's web app searches via getTabDetail
    with ``uri=searchapp|<keyword>``; using that here keeps us on the
    same authed path that everything else uses.

    Returns a flat list of result item dicts. Each item has
    ``appid``, ``name``, ``package``, ``versionName`` (sometimes),
    ``icon``, plus assorted scoring/metadata fields.
    """
    d = get_tab_detail(
        session,
        uri=f"searchapp|{keyword}",
        max_results=max_results,
        locale=locale,
    )
    out: list[dict] = []
    for layout in d.get("layoutData") or []:
        out.extend(layout.get("dataList") or [])
    return out


def category_items(
    session: AppGallerySession,
    uri: str,
    *,
    locale: str = "ru_RU",
    max_pages: int = DEFAULT_MAX_PAGES,
    page_size: int = DEFAULT_PAGE_SIZE,
    log_callback: Callable[[str], None] = lambda _l: None,
) -> list[dict]:
    """Walk a category URI across pages and return every item.

    Stops at ``max_pages`` to bound the request count, or earlier if
    AppGallery reports ``hasNextPage=0``.
    """
    out: list[dict] = []
    for page in range(1, max_pages + 1):
        d = get_tab_detail(
            session, uri=uri, page=page,
            max_results=page_size, locale=locale,
        )
        rtn = d.get("rtnCode")
        if rtn != 0:
            log_callback(
                f"appgallery: getTabDetail({uri}) page {page} "
                f"rtnCode={rtn}")
            break
        items = []
        for layout in d.get("layoutData") or []:
            items.extend(layout.get("dataList") or [])
        out.extend(items)
        log_callback(
            f"appgallery: {uri} p{page}: +{len(items)} (total {len(out)})")
        if not d.get("hasNextPage"):
            break
    return out


# ---------------------------------------------------------------------------
# Cached index fetch (mirrors fdroid.fetch_index shape)
# ---------------------------------------------------------------------------


def default_cache_dir() -> Path:
    return Path.home() / ".ivi-installer" / "cache"


def fetch_index(
    *,
    cache_dir: Path | None = None,
    ttl_seconds: int = INDEX_TTL_SECONDS,
    force: bool = False,
    shard: str = DEFAULT_SHARD,
    locale: str = "ru_RU",
    category_uris: Iterable[tuple[str, str]] = DEFAULT_CATEGORY_URIS,
    timeout: float = 60.0,
    log_callback: Callable[[str], None] = lambda _l: None,
) -> dict:
    """Pull every configured category and return one merged dict.

    Disk-cached at ``<cache_dir>/appgallery-index.json`` for 24 h —
    AppGallery rotates its top-charts but not constantly, and one
    refresh per day matches the F-Droid pattern.

    Shape:
        {
            "fetched_at": <int epoch>,
            "shard": "europe",
            "locale": "ru_RU",
            "categories": {
                "<label>": {"uri": "...", "items": [<raw items>]}
            }
        }

    Use ``from_appgallery_index`` to flatten and deduplicate this into
    a tuple of ``CatalogEntry``.
    """
    cache_dir = Path(cache_dir or default_cache_dir())
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "appgallery-index.json"

    if cache_path.is_file() and not force:
        age = time.time() - cache_path.stat().st_mtime
        if age < ttl_seconds:
            log_callback(f"appgallery: using cached index ({cache_path})")
            try:
                return json.loads(cache_path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.warning("appgallery cache unreadable (%s) — refetch", e)

    session = AppGallerySession(shard=shard)
    out: dict = {
        "fetched_at": int(time.time()),
        "shard": shard,
        "locale": locale,
        "categories": {},
    }
    for label, uri in category_uris:
        try:
            items = category_items(
                session, uri, locale=locale,
                log_callback=log_callback,
            )
        except Exception as e:
            log.exception("appgallery: %s (%s) failed", label, uri)
            log_callback(f"appgallery: {label} fetch failed: {e}")
            items = []
        out["categories"][label] = {"uri": uri, "items": items}

    tmp = cache_path.with_suffix(cache_path.suffix + ".part")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cache_path)
    total = sum(len(c["items"]) for c in out["categories"].values())
    log_callback(
        f"appgallery: cached index ({total} items across "
        f"{len(out['categories'])} categories)")
    return out


# ---------------------------------------------------------------------------
# Unauthenticated /edge/index/* endpoints — search suggestions
# ---------------------------------------------------------------------------
#
# The web app talks to two more endpoints when the user clicks into
# the search bar. Unlike /edge/uowap/index they don't need a JWT or
# session cookies — a plain JSON POST returns the data. We keep them
# stateless (no AppGallerySession) so the UI can call them directly
# from a debounced text-changed handler without paying the bootstrap
# round-trip.


def _index_post(
    path: str,
    payload: dict,
    *,
    shard: str = DEFAULT_SHARD,
    timeout: float = 10.0,
) -> dict:
    """POST a JSON body to ``/edge/<path>`` and return the parsed JSON.

    Used by the search-suggestion endpoints below. Doesn't share state
    with :class:`AppGallerySession` — these endpoints are public.
    """
    if shard not in SHARDS:
        raise ValueError(f"unknown shard {shard!r}")
    url = f"{SHARDS[shard]}/{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": _USER_AGENT,
            "Origin": "https://appgallery.huawei.com",
            "Referer": "https://appgallery.huawei.com/",
        },
    )
    with urllib.request.urlopen(
        req, timeout=timeout, context=_ssl_context()
    ) as resp:
        return json.loads(resp.read())


def get_hot_search_list(
    *,
    shard: str = DEFAULT_SHARD,
    locale: str = "en",
    timeout: float = 10.0,
) -> list[str]:
    """Return the flat list of "hot" search keywords.

    The wire response groups suggestions under category buckets
    ("All" / "Apps" / "Games"); we flatten and dedupe (case-insensitive)
    in iteration order, since the UI shows a single dropdown panel.
    """
    data = _index_post(
        "index/getnewhotsearchlist",
        {"serviceType": 20, "zone": "", "locale": locale},
        shard=shard, timeout=timeout,
    )
    out: list[str] = []
    seen: set[str] = set()
    for bucket in data.get("list") or []:
        for item in bucket.get("dataList") or []:
            name = (item.get("name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out


def complete_search_word(
    keyword: str,
    *,
    shard: str = DEFAULT_SHARD,
    locale: str = "en",
    timeout: float = 8.0,
) -> dict:
    """Type-ahead search.

    The wire response carries a ``list`` of keyword suggestions plus,
    when AppGallery has a confident top match, an ``app`` dict (and
    redundant ``appList``) with the C-id, package, name, icon, etc.

    Returns a normalised dict so callers don't need to re-implement
    that flattening:

        {
            "keyword":     str,                 # echoed back
            "suggestions": list[str],
            "top_app":     dict | None,         # raw AG item, if any
        }

    Empty/whitespace ``keyword`` returns the empty shape without
    making a request — AppGallery itself rejects empty queries with
    rtnCode != 0.
    """
    kw = (keyword or "").strip()
    if not kw:
        return {"keyword": "", "suggestions": [], "top_app": None}
    data = _index_post(
        "index/completeSearchWord",
        {"serviceType": 20, "keyword": kw, "zone": "", "locale": locale},
        shard=shard, timeout=timeout,
    )
    suggestions = [
        s for s in (data.get("list") or [])
        if isinstance(s, str) and s.strip()
    ]
    top_app = data.get("app")
    if not isinstance(top_app, dict):
        top_app = None
    return {
        "keyword": data.get("keyword") or kw,
        "suggestions": suggestions,
        "top_app": top_app,
    }


__all__ = [
    "AppGallerySession",
    "DEFAULT_CATEGORY_URIS",
    "DEFAULT_MAX_PAGES",
    "DEFAULT_SHARD",
    "INDEX_TTL_SECONDS",
    "MOST_RANKED_LABEL",
    "MOST_RANKED_URI",
    "SHARDS",
    "category_items",
    "complete_search_word",
    "default_cache_dir",
    "fetch_index",
    "get_hot_search_list",
    "get_tab_detail",
    "search",
]
