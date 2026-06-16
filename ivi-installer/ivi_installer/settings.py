"""Per-user persistent settings (last-used timezone, etc.).

Tiny JSON store in `~/.ivi-installer/settings.json` — same directory
where `adb.ensure_adb()` keeps platform-tools, so all our state lives
in one well-known place.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_STORE_PATH = Path.home() / ".ivi-installer" / "settings.json"


def _read() -> dict:
    try:
        return json.loads(_STORE_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("settings unreadable, ignoring: %s", e)
        return {}


def _write(data: dict) -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STORE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))
    except OSError as e:
        log.warning("settings write failed: %s", e)


def get(key: str, default: str | None = None) -> str | None:
    return _read().get(key, default)


def set(key: str, value: str) -> None:
    data = _read()
    data[key] = value
    _write(data)
