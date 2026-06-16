"""Download an APK directly from Huawei AppGallery by app ID.

The competitor (AvatrAppInstaller v1.4) lets the user paste an
AppGallery link or a "C12345"-style app id and grabs the APK from
``https://appgallery.cloud.huawei.com/appdl/{id}``. The endpoint
returns a 302 to a CDN URL that serves the raw .apk; following the
redirect lets us drop the file into the user's Downloads folder
without needing the AppGallery app on the host.

This module is intentionally tiny and synchronous: a single download,
no ranges, no resumes. The UI runs it inside a worker so the Qt main
loop stays responsive.

The Huawei AppGallery URL format:

  * ``https://appgallery.huawei.com/app/C12345``     — full URL form
  * ``https://appgallery.cloud.huawei.com/appdl/C12345`` — direct dl
  * ``C12345`` — bare id (the C is significant; case-insensitive)

The endpoint sometimes redirects, sometimes returns the file directly;
both are handled by ``urllib.request``'s default redirect handler.
"""
from __future__ import annotations

import logging
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

APPGALLERY_DL_URL = "https://appgallery.cloud.huawei.com/appdl/{id}"

# Anything smaller than this from the CDN is treated as an error
# response (the AppGallery occasionally serves tiny "app not found"
# pages with `application/octet-stream`). Borrowed from the
# AvatrAppInstaller competitor's check.
_MIN_APK_BYTES = 10_000

# Tolerant: accepts a full URL, an /app/<id> URL, or a bare id with or
# without surrounding whitespace. The id itself is "C" followed by
# digits (length varies; we don't enforce it).
_ID_RE = re.compile(r"\b(C\d+)\b", re.IGNORECASE)


def parse_app_id(raw: str) -> str | None:
    """Return the bare app id from anything the user pastes, or None.

    Accepts:
      * ``C101898721``
      * ``https://appgallery.huawei.com/app/C101898721``
      * ``https://appgallery.cloud.huawei.com/appdl/C101898721``
      * any URL or text containing a `C\\d+` token

    The leading ``C`` is uppercased on return so callers can compare
    ids reliably.
    """
    if not raw:
        return None
    m = _ID_RE.search(raw.strip())
    if not m:
        return None
    return m.group(1).upper()


def download(
    app_id: str,
    *,
    out_dir: Path,
    progress: Callable[[int, int], None] = lambda _b, _t: None,
    user_agent: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36"),
    timeout: float = 120.0,
) -> Path:
    """Download the APK for `app_id` to `out_dir`.

    Returns the local path of the saved file. Raises on HTTP errors or
    when the response isn't a ZIP/APK (the AppGallery occasionally
    returns an HTML "app not found" page with status 200).

    `progress(bytes_so_far, total_bytes)` fires on each chunk; total
    bytes is 0 if the server didn't send Content-Length.

    Network-level details mirror the AvatrAppInstaller competitor:
    Windows Chrome User-Agent, no `Accept` header, 120s timeout, and
    SSL hostname/cert verification disabled. The Huawei CDN's cert
    chain is occasionally unreachable from desktop trust stores —
    skipping verification keeps the download working in practice
    (we still validate the payload itself: HTML/tiny-file/non-ZIP
    are all rejected before the file lands in the user's Downloads).
    """
    if not app_id or not _ID_RE.fullmatch(app_id):
        raise ValueError(f"invalid app id: {app_id!r}")
    url = APPGALLERY_DL_URL.format(id=app_id)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent},
    )
    log.info("appgallery download start: %s", url)
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        # The CDN serves the APK as application/vnd.android.package-archive
        # or application/octet-stream; HTML responses (=app not found)
        # have text/html.
        if "html" in ctype.lower():
            raise RuntimeError(
                f"AppGallery returned HTML ({ctype}) for {app_id} — "
                "the app id is probably wrong or the app isn't free.")
        # Resolve a sensible filename. AppGallery sets Content-Disposition
        # with the package name; if missing, we default to the app id.
        cd = resp.headers.get("Content-Disposition", "")
        name = _filename_from_disposition(cd) or f"{app_id}.apk"
        if not name.lower().endswith(".apk"):
            name += ".apk"
        # Sanitize: strip path traversal and reserved chars.
        name = _safe_filename(name)
        target = out_dir / name

        total = int(resp.headers.get("Content-Length") or 0)
        seen = 0
        progress(seen, total)
        # Stream to a temp file in the same dir so a partial download
        # can't overwrite a previous good file.
        tmp = target.with_suffix(target.suffix + ".part")
        try:
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    seen += len(chunk)
                    progress(seen, total)
            # Tiny-payload guard: the CDN sometimes returns a tiny
            # error blob with `application/octet-stream` instead of
            # an HTML 404. The competitor uses this same threshold.
            if seen < _MIN_APK_BYTES:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"downloaded payload is too small ({seen} bytes) "
                    f"— the server likely returned an error response "
                    f"for {app_id}.")
            # APK = ZIP starting with "PK\x03\x04". Quick sanity check
            # so we surface "AppGallery served an error page" early.
            with open(tmp, "rb") as fh:
                head = fh.read(4)
            if head[:2] != b"PK":
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"downloaded payload is not a ZIP/APK "
                    f"(magic={head!r}) — the server likely returned "
                    "an error page.")
            tmp.replace(target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
    log.info("appgallery download ok: %s (%d bytes)", target, seen)
    return target


_DISPOSITION_FILENAME_RE = re.compile(
    r"""filename\*?=(?:UTF-8''|"|)([^";]+)""", re.IGNORECASE,
)


def _filename_from_disposition(value: str) -> str | None:
    if not value:
        return None
    m = _DISPOSITION_FILENAME_RE.search(value)
    if not m:
        return None
    name = m.group(1).strip().strip('"')
    # RFC 5987 percent-encoded form
    return urllib.parse.unquote(name)


_BAD_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    safe = _BAD_NAME_CHARS.sub("_", base).strip("_.") or "download.apk"
    return safe


def resolve(
    entry: dict,
    *,
    out_dir: Path,
    progress: Callable[[int, int], None] = lambda _b, _t: None,
    log_callback: Callable[[str], None] = lambda _l: None,
) -> Path:
    """Catalog-source entry point. ``entry`` shape: ``{"id": "C12345"}``."""
    app_id = entry.get("id")
    if not app_id:
        raise ValueError("appgallery source entry missing 'id'")
    log_callback(f"AppGallery: resolving {app_id}")
    return download(app_id, out_dir=out_dir, progress=progress)
