"""APK download sources for the in-app Store catalog.

Each source module exposes a ``resolve(entry, *, out_dir, progress,
log_callback) -> Path`` that fetches the APK described by a single
catalog-entry dict and returns a local path. The Store tab uses the
``RESOLVERS`` registry to dispatch by ``kind`` without knowing how any
specific source works.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import appgallery, direct

ResolveFn = Callable[..., Path]

RESOLVERS: dict[str, ResolveFn] = {
    "appgallery": appgallery.resolve,
    "direct":     direct.resolve,
}


def resolve(
    entry: dict,
    *,
    out_dir: Path,
    progress: Callable[[int, int], None] = lambda _b, _t: None,
    log_callback: Callable[[str], None] = lambda _l: None,
) -> Path:
    """Dispatch a single source-entry dict to its registered resolver.

    Raises ``KeyError`` for an unknown ``kind`` and ``ValueError`` for a
    malformed entry (missing ``kind``). Source modules raise their own
    errors for network / payload issues.
    """
    kind = entry.get("kind")
    if not kind:
        raise ValueError("source entry missing 'kind'")
    fn = RESOLVERS.get(kind)
    if fn is None:
        raise KeyError(f"unknown source kind: {kind!r}")
    return fn(
        entry,
        out_dir=out_dir,
        progress=progress,
        log_callback=log_callback,
    )


__all__ = ["RESOLVERS", "resolve", "appgallery", "direct"]
