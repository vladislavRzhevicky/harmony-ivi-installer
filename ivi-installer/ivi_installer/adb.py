"""Subprocess wrapper around the `adb` binary.

Exposes a small synchronous API. UI code consumes this via
`ui/workers.py` (QThread) so adb calls never block the Qt main loop.

Reference: docs/11 §5.1 + docs/12 §3, §7, §12.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# On Windows, a windowed (no-console) exe that calls subprocess.run with
# the default flags pops a transient cmd window for every child process.
# Polling adb every 2s means a flashing console + focus theft. Suppress.
_SUBPROCESS_CREATIONFLAGS = (
    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
)


# Path resolution. We keep a module-level pointer so the UI can swap in
# the bundled binary at startup without touching every call site.
ADB_BINARY: str = "adb"


# ---- bootstrap (download adb on first run) ----

def _platform_tools_url() -> str:
    """Pick the Google-hosted platform-tools archive for this OS."""
    if sys.platform == "darwin":
        suffix = "darwin"
    elif sys.platform == "win32" or os.name == "nt":
        suffix = "windows"
    else:
        suffix = "linux"
    return (
        f"https://dl.google.com/android/repository/"
        f"platform-tools-latest-{suffix}.zip"
    )


PLATFORM_TOOLS_URL = _platform_tools_url()
DEFAULT_ADB_DIR = Path.home() / ".ivi-installer" / "platform-tools"


def _common_adb_locations() -> tuple[str, ...]:
    """Well-known absolute paths to adb across macOS / Windows / Linux.

    Used as a fallback when adb is not on the (often trimmed) PATH that
    a launchd / Explorer launch hands the GUI process.
    """
    home = Path.home()
    if os.name == "nt":  # Windows
        local_app = os.environ.get("LOCALAPPDATA",
                                     str(home / "AppData/Local"))
        program_files = os.environ.get("ProgramFiles", "C:/Program Files")
        return (
            f"{local_app}/Android/Sdk/platform-tools/adb.exe",
            f"{program_files}/Android/Android Studio/sdk/platform-tools/adb.exe",
            "C:/platform-tools/adb.exe",
            "C:/adb/adb.exe",
        )
    # macOS + Linux
    return (
        "/opt/homebrew/bin/adb",                                       # Apple Silicon Homebrew
        "/usr/local/bin/adb",                                          # Intel Homebrew / Linux
        str(home / "Library/Android/sdk/platform-tools/adb"),          # Android Studio macOS
        str(home / "Android/Sdk/platform-tools/adb"),                  # Android Studio Linux
        "/usr/bin/adb",
    )


_COMMON_ADB_LOCATIONS = _common_adb_locations()
_ADB_BINARY_NAME = "adb.exe" if os.name == "nt" else "adb"


def _bundled_adb() -> str | None:
    """Path to the adb shipped inside the wheel.

    Windows MSI bundles `adb.exe` + the two AdbWin*.dll files under
    `resources/platform-tools/windows/`; macOS bundle ships a single
    universal2 `adb` binary under `resources/platform-tools/darwin/`.
    The macOS adb is self-contained (only links to libSystem,
    CoreFoundation, IOKit, Security, libobjc) — no libc++/lib64
    sidecars needed despite the leftover `@loader_path/lib64` rpaths
    in the Mach-O (those are for Google's other platform-tools like
    aapt2). Linux still relies on `ensure_adb()` to download at
    runtime.

    The path is resolved relative to this module so it works from an
    editable install (running out of a checkout) and from the
    briefcase-built bundle (`app_packages/ivi_installer/...`).

    The source binary is committed with mode 100755, so the wheel
    preserves +x on POSIX. As a safety net (in case some build pipeline
    strips the bit, e.g. tarball repacking on Windows), we still
    idempotently re-add +x on first lookup. Failures are logged and
    swallowed so a read-only install location doesn't break the lookup.
    """
    here = Path(__file__).resolve().parent
    base = here / "resources" / "platform-tools"
    if os.name == "nt":
        candidate = base / "windows" / "adb.exe"
    elif sys.platform == "darwin":
        candidate = base / "darwin" / "adb"
    else:
        return None
    if not candidate.exists():
        return None
    if os.name != "nt":
        try:
            st = candidate.stat()
            wanted = st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            if wanted != st.st_mode:
                candidate.chmod(wanted)
        except OSError as e:
            log.warning("could not set +x on bundled adb (%s): %s",
                        candidate, e)
    return str(candidate)


def find_adb() -> str | None:
    """Return a usable adb path or None.

    Order of preference:
    1. Module-level override (if set to an absolute path that exists)
    2. Bundled adb shipped with the package (Windows MSI today)
    3. ~/.ivi-installer/platform-tools/adb (downloader's managed copy)
    4. `adb` on PATH (works in terminal launches)
    5. Well-known absolute paths (works for .app launches where PATH is
       trimmed by launchd)
    """
    if os.path.isabs(ADB_BINARY) and Path(ADB_BINARY).exists():
        return ADB_BINARY
    bundled = _bundled_adb()
    if bundled:
        return bundled
    managed = DEFAULT_ADB_DIR / _ADB_BINARY_NAME
    if managed.exists():
        return str(managed)
    on_path = shutil.which("adb")
    if on_path:
        return on_path
    for candidate in _COMMON_ADB_LOCATIONS:
        if Path(candidate).exists():
            return candidate
    return None


def ensure_adb(progress: callable | None = None) -> Path:
    """Download platform-tools to ~/.ivi-installer/ if no adb is available.

    `progress` is optional `callable(percent: int, status: str)` used to
    update the UI during download/extract.

    Returns the path to a usable adb binary.
    """
    existing = find_adb()
    if existing is not None:
        log.info("adb available at %s", existing)
        return Path(existing)

    DEFAULT_ADB_DIR.parent.mkdir(parents=True, exist_ok=True)
    zip_path = DEFAULT_ADB_DIR.parent / "platform-tools.zip"

    if progress:
        progress(0, "Downloading adb")
    log.info("downloading %s", PLATFORM_TOOLS_URL)
    _download(PLATFORM_TOOLS_URL, zip_path, progress=progress)

    if progress:
        progress(95, "Extracting platform-tools")
    log.info("extracting to %s", DEFAULT_ADB_DIR.parent)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(DEFAULT_ADB_DIR.parent)
    zip_path.unlink(missing_ok=True)

    adb_path = DEFAULT_ADB_DIR / _ADB_BINARY_NAME
    if not adb_path.exists():
        raise RuntimeError(
            f"adb not found at {adb_path} after extracting {PLATFORM_TOOLS_URL}"
        )
    # Ensure executable bit is set.
    adb_path.chmod(adb_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if progress:
        progress(100, "adb ready")
    return adb_path


def _ssl_context():
    """Build an SSL context that works inside the briefcase-bundled Python.

    Briefcase ships its own Python without the system CA bundle, so the
    default urllib SSL verification fails with CERTIFICATE_VERIFY_FAILED.
    Use certifi's CA bundle if available, fall back to the default.
    """
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _download(url: str, dest: Path, progress: callable | None = None) -> None:
    ctx = _ssl_context()
    with urllib.request.urlopen(url, context=ctx) as resp:
        total_size = int(resp.headers.get("Content-Length", 0))
        chunk = 64 * 1024
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                downloaded += len(buf)
                if progress and total_size > 0:
                    pct = min(95, int(downloaded * 95 / total_size))
                    progress(pct, "Downloading adb")


# ---- low-level adb runner ----

@dataclass(frozen=True)
class AdbResult:
    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        """stdout, falling back to stderr when stdout is empty."""
        return self.stdout if self.stdout.strip() else self.stderr


class AdbError(RuntimeError):
    """adb returned a non-zero exit code (when check=True)."""

    def __init__(self, result: AdbResult, message: str | None = None):
        self.result = result
        super().__init__(
            message
            or f"adb {' '.join(result.args)} → exit {result.exit_code}: "
               f"{result.output.strip()}"
        )


def run(
    *args: str,
    serial: str | None = None,
    check: bool = True,
    timeout: int | None = 60,
    binary: str | None = None,
    input: str | None = None,
) -> AdbResult:
    """Run an adb command and return the captured stdout/stderr.

    Args:
        *args: arguments after the implicit `adb [-s SERIAL]` prefix.
        serial: device serial to target. If None, no `-s` is passed.
        check: raise AdbError on non-zero exit.
        timeout: seconds before subprocess.TimeoutExpired is raised.
        binary: override the resolved adb path (rare; tests + bundled adb).
        input: text to send to stdin.

    Raises:
        AdbError: when check=True and exit code != 0.
        TimeoutError: when the call exceeds `timeout` seconds.
        FileNotFoundError: when no adb binary is reachable.
    """
    adb_path = binary or find_adb()
    if not adb_path:
        raise FileNotFoundError(
            "adb not found. Run ensure_adb() or install platform-tools "
            "(brew install android-platform-tools)."
        )
    cmd: list[str] = [adb_path]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    log.debug("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,  # we raise AdbError ourselves so we have AdbResult
            timeout=timeout,
            input=input,
            creationflags=_SUBPROCESS_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(
            f"adb {' '.join(args)} timed out after {timeout}s"
        ) from e
    result = AdbResult(
        args=tuple(args),
        exit_code=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    # Best-effort: feed the file logger so log files are self-explanatory.
    # We import lazily so a `--cli` invocation that doesn't configure the
    # logger never triggers extra setup.
    try:
        from . import logging_setup
        logging_setup.log_adb_invocation(
            cmd, result.exit_code, result.stdout, result.stderr,
        )
    except Exception:  # pragma: no cover — must never fail an install
        pass
    if check and result.exit_code != 0:
        raise AdbError(result)
    return result


# ---- thin convenience helpers (used by installer.py and devices.py) ----

def whoami(serial: str) -> str:
    """`adb -s <serial> shell whoami`. Returns 'root' on dealer-unlocked IVI."""
    return run("shell", "whoami", serial=serial).stdout.strip()


def is_root(serial: str) -> bool:
    return whoami(serial) == "root"


def push(local: Path | str, remote: str, serial: str) -> AdbResult:
    """Push a file. Only `/data/local/tmp/` is an acceptable destination
    in v3 (safe-by-default policy — no /system writes)."""
    return run("push", str(local), remote, serial=serial, timeout=600)


def shell(*args: str, serial: str) -> str:
    return run("shell", *args, serial=serial).stdout


def wait_for_device(timeout: int = 60) -> str:
    """Block until at least one device is in state=device. Return its serial.

    Uses our own `devices.list_devices` to avoid spawning a long-running
    `adb wait-for-device` (which blocks the worker thread without a way
    to time-out portably).
    """
    import time

    from . import devices  # local import to avoid cycle at module load
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        devs = devices.list_devices()
        if devs:
            return devs[0].serial
        time.sleep(0.5)
    raise TimeoutError(f"no adb device after {timeout}s")
