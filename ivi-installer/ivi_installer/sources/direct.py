"""Direct-URL APK source.

Trivial ``urllib`` wrapper for entries that point at a fixed APK URL —
used for vendor builds (telegram.org), one-off mirrors, and pinned
older AppGallery versions. The optional ``sha256`` field, if present,
is verified after download.

Catalog entry shape::

    {"kind": "direct",
     "url":  "https://example.org/app-1.2.3.apk",
     "version": "1.2.3",          # optional, display only
     "sha256":  "abc…"           # optional, verified if present
    }
"""
from __future__ import annotations

import hashlib
import logging
import re
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Mirror the AppGallery threshold — anything smaller is almost certainly
# an HTML error page served as octet-stream by a broken CDN.
_MIN_APK_BYTES = 10_000

_BAD_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    safe = _BAD_NAME_CHARS.sub("_", base).strip("_.") or "download.apk"
    if not safe.lower().endswith(".apk"):
        safe += ".apk"
    return safe


def _filename_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = parsed.path.rsplit("/", 1)[-1] or "download.apk"
    return _safe_filename(urllib.parse.unquote(name))


def resolve(
    entry: dict,
    *,
    out_dir: Path,
    progress: Callable[[int, int], None] = lambda _b, _t: None,
    log_callback: Callable[[str], None] = lambda _l: None,
    timeout: float = 120.0,
) -> Path:
    """Download ``entry["url"]`` to ``out_dir`` and return the file path."""
    url = entry.get("url")
    if not url:
        raise ValueError("direct source entry missing 'url'")
    expected_sha = (entry.get("sha256") or "").lower().strip() or None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = _filename_from_url(url)
    target = out_dir / name
    tmp = target.with_suffix(target.suffix + ".part")

    log_callback(f"direct: GET {url}")

    # Match the AppGallery resolver: the desktop trust store doesn't
    # always reach Huawei / Russian CDNs; verifying hostname/cert here
    # was the difference between "downloads work everywhere" and
    # "downloads work on the dev's machine". The payload itself is
    # validated below (size, ZIP magic, optional sha256).
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36"),
    })
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "html" in ctype.lower():
            raise RuntimeError(
                f"direct source returned HTML ({ctype}) for {url} — "
                "the link is probably stale or behind an interstitial.")
        total = int(resp.headers.get("Content-Length") or 0)
        seen = 0
        progress(seen, total)
        sha = hashlib.sha256()
        try:
            with open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    sha.update(chunk)
                    seen += len(chunk)
                    progress(seen, total)
            if seen < _MIN_APK_BYTES:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"direct download too small ({seen} bytes) — server "
                    "likely returned an error response.")
            with open(tmp, "rb") as fh:
                head = fh.read(4)
            if head[:2] != b"PK":
                tmp.unlink(missing_ok=True)
                raise RuntimeError(
                    f"direct download is not a ZIP/APK (magic={head!r}) "
                    "— the URL probably serves an error page.")
            if expected_sha:
                got = sha.hexdigest().lower()
                if got != expected_sha:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(
                        f"sha256 mismatch: expected {expected_sha[:16]}…, "
                        f"got {got[:16]}…")
            tmp.replace(target)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
    log_callback(f"direct: ok ({seen} bytes) → {target.name}")
    return target
