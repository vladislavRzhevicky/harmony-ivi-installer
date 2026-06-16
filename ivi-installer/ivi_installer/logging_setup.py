"""Centralised logging configuration.

Sets up two destinations:
  * stderr — INFO level, short format (terminal-friendly)
  * rotating file in ~/.ivi-installer/logs/ — DEBUG level, verbose format

Every adb invocation that goes through `adb.run` is logged at DEBUG with
its full argv + truncated stdout/stderr — so when the user sends back a
log file we can replay the exact session that ran on their machine.

Two helpers exposed for the UI:
  * `current_log_file()` — absolute path of the active rotating log
  * `dump_metadata_header()` — writes a one-shot run header (OS, app
    version, device probe summary) so each log file starts self-describing
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__

_LOG_DIR = Path.home() / ".ivi-installer" / "logs"
_LOG_FILE = _LOG_DIR / "ivi-installer.log"
_MAX_BYTES = 2_000_000        # 2 MB per file
_BACKUP_COUNT = 5             # keep the last 5 rotations

_FILE_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s:%(lineno)d  %(message)s"
)
_FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"

_STREAM_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_configured = False


def setup_logging(level: int = logging.INFO) -> Path:
    """Idempotent: configure root once, return the log file path.

    The rotating file handler writes at DEBUG level. The console handler
    inherits the level passed in (defaults to INFO).
    """
    global _configured
    if _configured:
        return _LOG_FILE

    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    # Wipe any pre-existing handlers (basicConfig in __main__ may have
    # added one before we ran).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(level)
    stream.setFormatter(logging.Formatter(_STREAM_FORMAT))
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        _LOG_FILE, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, _FILE_DATEFMT))
    root.addHandler(file_handler)

    _configured = True
    _write_session_header()
    return _LOG_FILE


def current_log_file() -> Path:
    return _LOG_FILE


def log_directory() -> Path:
    return _LOG_DIR


def dump_metadata_header(extra: dict[str, str] | None = None) -> None:
    """Write a separator + a fresh metadata block at the current end of
    the log file. Useful before kicking off a multi-step operation so
    the user-pastable log has clear run boundaries."""
    log = logging.getLogger("ivi_installer.session")
    log.info("=" * 78)
    log.info("ivi-installer %s — run started %s",
             __version__, _dt.datetime.now().isoformat(timespec="seconds"))
    log.info("python %s on %s %s (%s)",
             sys.version.split()[0], platform.system(),
             platform.release(), platform.machine())
    if extra:
        for key, value in extra.items():
            log.info("%s: %s", key, value)
    log.info("=" * 78)


def log_adb_invocation(argv: list[str], exit_code: int,
                       stdout: str, stderr: str,
                       *, max_chars: int = 4000) -> None:
    """Called by `adb.run` after every subprocess. Truncates very long
    output so a chatty pm install doesn't blow up the rotating file."""
    log = logging.getLogger("ivi_installer.adb")
    if not log.isEnabledFor(logging.DEBUG):
        return
    log.debug("$ %s", " ".join(argv))
    log.debug("  exit=%d", exit_code)
    if stdout:
        log.debug("  stdout: %s", _truncate(stdout, max_chars))
    if stderr:
        log.debug("  stderr: %s", _truncate(stderr, max_chars))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return f"{head}\n…[truncated {len(text) - limit} chars]…\n{tail}"


def _write_session_header() -> None:
    log = logging.getLogger("ivi_installer.session")
    log.debug("=" * 78)
    log.debug("ivi-installer %s — process %d started %s",
              __version__, os.getpid(),
              _dt.datetime.now().isoformat(timespec="seconds"))
    log.debug("python %s on %s %s (%s)",
              sys.version.split()[0], platform.system(),
              platform.release(), platform.machine())
    log.debug("=" * 78)
