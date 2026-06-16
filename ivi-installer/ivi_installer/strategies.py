"""Install strategies + cascade orchestrator.

Each strategy is a small function that tries one specific way to install
a single APK on a Huawei IVI head unit. The cascade tries them in order
until one succeeds or until a *terminal* failure code says "no point
retrying" (e.g. INSTALL_FAILED_VERSION_DOWNGRADE).

Background: the original `XCodesHuaweiPwner` is a thin client whose
server hands back step plans. We don't have the server, so we replicate
the most likely combinations as fallbacks. See the chat transcript for
the full reverse-engineering notes.

Public API:
    list_strategies() → list[StrategyDescriptor]
    run_cascade(ctx) → CascadedInstallResult
    run_strategy(name, ctx) → CascadedInstallResult
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable

from . import adb
from .devices import DeviceInfo, list_android_users

log = logging.getLogger(__name__)


# ---- result types ----------------------------------------------------------

class AttemptStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"            # strategy.applies() was False
    TERMINAL = "terminal"          # failure code marked as not-worth-retrying


@dataclass(frozen=True)
class AttemptResult:
    """Outcome of one strategy attempt."""
    strategy: str
    status: AttemptStatus
    summary: str                      # one-line "what happened"
    failure_code: str | None = None   # parsed pm-install code, if any
    hint: str | None = None
    log_lines: tuple[str, ...] = field(default_factory=tuple)
    duration_s: float = 0.0


@dataclass(frozen=True)
class StageEvent:
    """Per-stage progress event emitted by install strategies.

    Strategies report their internal pipeline progress through
    ``InstallContext.stage_callback`` so the UI can drive the visual
    pipeline widget. The ``hint`` text is intentionally generic — it
    must NOT reveal commands, file paths, or other reproducible details
    of the bypass. Use phrases like "Staged" or "Authorized", never the
    actual adb command line.
    """
    strategy: str
    index: int
    kind: str                                 # "start" | "done" | "failed" | "skipped"
    hint: str | None = None
    duration_ms: int | None = None
    # Optional structured payload — used by the grant-perms stage to
    # pass {user_id: (ok_count, fail_count)} so the UI can paint the
    # matrix from real data instead of a synthetic demo pattern.
    data: dict | None = None


@dataclass(frozen=True)
class CascadedInstallResult:
    """Aggregate result of one or more strategy attempts."""
    success: bool
    package: str | None
    attempts: tuple[AttemptResult, ...]

    @property
    def winning_strategy(self) -> str | None:
        for a in self.attempts:
            if a.status is AttemptStatus.SUCCESS:
                return a.strategy
        return None

    @property
    def last_failure_code(self) -> str | None:
        for a in reversed(self.attempts):
            if a.failure_code:
                return a.failure_code
        return None

    @property
    def message(self) -> str:
        if self.success:
            return f"Installed via {self.winning_strategy}."
        if not self.attempts:
            return "No strategies were attempted."
        last = self.attempts[-1]
        return f"All strategies failed. Last: {last.strategy} → {last.summary}"


# ---- context ---------------------------------------------------------------

@dataclass
class InstallContext:
    """Everything a strategy needs to do its job.

    `apk_paths` always carries exactly one .apk — multi-APK / .xapk
    inputs were removed because Huawei's bridge command hardcodes the
    Avatr-only installer pkg.
    `log` is a callback emitting one line at a time so the UI gets live
    updates instead of a single dump at the end.
    `force_reinstall` triggers `pm uninstall` for every per-user copy of
    the package before the install — needed to recover from
    INSTALL_FAILED_VERSION_DOWNGRADE without losing more than necessary.
    """
    serial: str
    apk_paths: list[Path]
    package: str | None = None
    # Single-user knob kept for backward compatibility with existing
    # callers (tests, CLI). Use ``target_users`` for the multi-screen
    # selection introduced in 0.8.6 — when both are set, ``target_users``
    # wins.
    target_user: int | None = None
    # Explicit list of multimedia-screen user ids the install should
    # land on. ``None`` keeps the legacy "all multimedia screens"
    # behavior (seed via user 0 + fan-out across the canonical set).
    # ``[N]`` installs only on user N (no fan-out). ``[N, M, …]`` seeds
    # via the first id and fans out to the rest.
    target_users: tuple[int, ...] | None = None
    grant_runtime: bool = True
    allow_downgrade: bool = True
    allow_test: bool = True
    replace: bool = True
    preferred_installer: str | None = None
    device_info: DeviceInfo | None = None
    force_reinstall: bool = False
    log: Callable[[str], None] = lambda _line: None
    # Optional per-stage progress sink. UI sets this to drive the
    # visual pipeline widget; default is a no-op so headless callers
    # (tests, CLI) don't need to care.
    stage_callback: Callable[["StageEvent"], None] = lambda _ev: None


# ---- shared helpers --------------------------------------------------------

_FAILURE_RE = re.compile(r"Failure\s*\[(INSTALL_(?:PARSE_)?FAILED_[A-Z0-9_]+)")

_HINTS: dict[str, str] = {
    "INSTALL_FAILED_VERSION_DOWNGRADE":
        "Installed version is newer. Uninstall first or pick a higher versionCode build.",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE":
        "Existing app has a different signature. Uninstall it first "
        "(use `pm uninstall -k` to keep its data).",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE":
        "Not enough free space on the IVI. Free up storage and retry.",
    "INSTALL_FAILED_ALREADY_EXISTS":
        "Package already installed. Replace flag (-r) is on by default.",
    "INSTALL_FAILED_INVALID_APK":
        "APK is corrupted or unsigned.",
    "INSTALL_FAILED_USER_RESTRICTED":
        "Restricted user — try without --user, or set target_user=0.",
    "INSTALL_FAILED_VERIFICATION_FAILURE":
        "Package verifier rejected the APK. Try the 'Disable verifier' strategy.",
    "INSTALL_FAILED_VERIFICATION_TIMEOUT":
        "Verifier timeout. Try the 'Disable verifier' strategy.",
    "INSTALL_FAILED_ABORTED":
        "Install was aborted by a device-policy check. The IVI most likely "
        "shows 'apps from external sources are not allowed'. Try the "
        "'Bypass unknown-sources policy' strategy and/or set Installer pkg "
        "to com.android.vending or com.huawei.appinstaller.",
    "INSTALL_PARSE_FAILED_NOT_APK":
        "File is not a valid APK.",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES":
        "APK is unsigned. Sign it with apksigner before installing.",
    "INSTALL_PARSE_FAILED_INCONSISTENT_CERTIFICATES":
        "APK certificate chain is inconsistent.",
    "INSTALL_PARSE_FAILED_MANIFEST_MALFORMED":
        "AndroidManifest.xml is corrupted.",
}

# Codes where retrying with a different strategy is pointless: the APK
# itself or the user's environment is wrong, not the install path.
TERMINAL_CODES: frozenset[str] = frozenset({
    "INSTALL_FAILED_VERSION_DOWNGRADE",
    "INSTALL_FAILED_UPDATE_INCOMPATIBLE",
    "INSTALL_FAILED_INSUFFICIENT_STORAGE",
    "INSTALL_FAILED_INVALID_APK",
    "INSTALL_PARSE_FAILED_NOT_APK",
    "INSTALL_PARSE_FAILED_NO_CERTIFICATES",
    "INSTALL_PARSE_FAILED_INCONSISTENT_CERTIFICATES",
    "INSTALL_PARSE_FAILED_MANIFEST_MALFORMED",
    "INSTALL_PARSE_FAILED_BAD_MANIFEST",
})


def _hint_for(code: str | None) -> str | None:
    return _HINTS.get(code) if code else None


def parse_pm_output(text: str) -> tuple[bool, str | None]:
    """Return (success, failure_code) from a pm-install / install-commit output."""
    last_line = ""
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s:
            last_line = s
            break
    if last_line.startswith("Success"):
        return True, None
    m = _FAILURE_RE.search(text)
    return False, m.group(1) if m else None


_REMOTE_NAME_BAD_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_remote_name(name: str) -> str:
    """Return a remote filename safe to drop into an `adb shell` command line.

    Spaces and other shell-active characters in the local filename
    silently break `pm install <remote>` / `am start -d file://...`
    because adb shell tokenises by whitespace. We replace anything that
    isn't `[A-Za-z0-9._-]` with `_`, collapse runs of underscores, and
    fall back to "payload" if no alphanumeric chars survive.
    """
    safe = _REMOTE_NAME_BAD_CHARS.sub("_", name)
    safe = re.sub(r"_+", "_", safe).strip("_")
    if not any(c.isalnum() for c in safe):
        return "payload"
    if safe.startswith("."):
        safe = "payload" + safe
    return safe


class _StageReporter:
    """Tiny helper that wraps ``ctx.stage_callback`` with timing.

    Each stage in a strategy goes through ``start(idx, hint=…)`` then
    one of ``done(idx, hint=…)`` / ``failed(idx, hint=…)`` /
    ``skipped(idx, hint=…)``. ``done`` and ``failed`` carry
    duration_ms so the UI can render the small "180ms" tag on the
    right of the stage row.

    Hints MUST be generic user-facing copy ("Staged", "Authorized",
    "Mirrored to 4 screens") — never command lines, paths, or other
    reproducible details. The full debug log already lives in the log
    pane; the pipeline hint is just a friendly status snippet.
    """
    __slots__ = ("ctx", "strategy", "_t0")

    def __init__(self, ctx: "InstallContext", strategy: str):
        self.ctx = ctx
        self.strategy = strategy
        self._t0: dict[int, float] = {}

    def start(self, index: int, hint: str | None = None) -> None:
        self._t0[index] = time.monotonic()
        self._fire(index, "start", hint, None)

    def done(self, index: int, hint: str | None = None) -> None:
        self._fire(index, "done", hint, self._duration_ms(index))

    def failed(self, index: int, hint: str | None = None) -> None:
        self._fire(index, "failed", hint, self._duration_ms(index))

    def skipped(self, index: int, hint: str | None = None) -> None:
        self._fire(index, "skipped", hint, 0)

    def _duration_ms(self, index: int) -> int | None:
        t0 = self._t0.get(index)
        if t0 is None:
            return None
        return max(0, int((time.monotonic() - t0) * 1000))

    def _fire(self, index: int, kind: str,
              hint: str | None, duration_ms: int | None,
              data: dict | None = None) -> None:
        cb = getattr(self.ctx, "stage_callback", None)
        if cb is None:
            return
        try:
            cb(StageEvent(self.strategy, index, kind, hint,
                          duration_ms, data))
        except Exception:  # pragma: no cover — UI callback must never break us
            log.exception("stage_callback raised")

    def done_with_data(self, index: int, hint: str | None,
                        data: dict | None) -> None:
        self._fire(index, "done", hint, self._duration_ms(index), data)

    def failed_with_data(self, index: int, hint: str | None,
                          data: dict | None) -> None:
        self._fire(index, "failed", hint, self._duration_ms(index), data)


def _resolve_targets(ctx: InstallContext) -> tuple[int, list[int]]:
    """Decide which user gets the install session and which get mirrored.

    Resolution order:
      * ``target_users`` (the new 0.8.6 multi-screen knob) wins if set.
        - 1 entry  → seed there, no fan-out.
        - N>1      → seed via user 0 (the system surface, where Huawei's
                      blessed install path validates everything) and
                      mirror to all N entries via ``pm install-existing``.
                      This preserves the long-tested "all screens" path
                      whenever multiple screens were picked.
      * ``target_user`` (legacy single-user) → seed there, no fan-out.
      * Both unset → seed user 0, fan out to the canonical screen set
        ``HDB_SCREEN_USERS``.
    """
    if ctx.target_users is not None:
        users = list(ctx.target_users)
        if len(users) == 1:
            return users[0], []
        return 0, sorted(set(users))
    if ctx.target_user is not None:
        return ctx.target_user, []
    # Default "all screens": probe the device for the actual multimedia
    # users instead of pinning to (10,11,12,13). Different cars expose
    # different id sets — main user has been seen as 10, 12, or higher
    # than 13 — so a hardcoded tuple silently misses screens or fans
    # out to non-existent users.
    return 0, _live_screen_users(ctx.serial)


def _emit(ctx: InstallContext, line: str, *, accum: list[str]) -> None:
    """Push a line to both the live log and the per-attempt accumulator."""
    accum.append(line)
    try:
        ctx.log(line)
    except Exception:  # pragma: no cover — UI callback must never break us
        log.exception("ctx.log raised")


def _run_adb(
    ctx: InstallContext,
    *args: str,
    accum: list[str],
    timeout: int = 120,
) -> adb.AdbResult:
    """Run an adb command, dump it + its output into the accumulator."""
    _emit(ctx, f"$ adb -s {ctx.serial} {' '.join(args)}", accum=accum)
    result = adb.run(*args, serial=ctx.serial, check=False, timeout=timeout)
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    for line in output.splitlines():
        _emit(ctx, f"  {line}", accum=accum)
    _emit(ctx, f"  [exit={result.exit_code}]", accum=accum)
    return result


def _strategy_diagnose(ctx: InstallContext) -> AttemptResult:
    """Pure diagnostic: dump everything that helps us understand WHY installs
    fail on this device. Does NOT install anything. Saves a verbose report
    to ~/Desktop/ivi-diagnose-<timestamp>.txt.

    Captures:
      * `am get-current-user`
      * `ps -A | grep -E "hdb|adbd|installer"`
      * `dumpsys -l | grep -iE "huawei|hbm|hdb|installer|policy"`
      * `dumpsys user`
      * `appops query-op REQUEST_INSTALL_PACKAGES`
      * `appops query-op REQUEST_INSTALL_PACKAGES --user <current>`
      * `appops get com.android.shell` (full op list)
      * `pm list packages -i | grep -E "(installer|appgallery|appstore)"`
      * `getprop | grep -E "hdb|adb|installer|policy|hw_sc|persist.sys"`
      * `settings list global` / `secure` / `system`
      * `pm get-install-location`
      * `cat /system/etc/sysconfig/*.xml | grep -iE "install|allow"` (best-effort)
      * `which su`, `ls -la /system/bin/su /vendor/bin/su /sbin/su`
    """
    import datetime as _dt
    import os
    import tempfile

    name = "diagnose"
    accum: list[str] = []
    t0 = time.monotonic()

    sections: list[tuple[str, list[str]]] = []

    # Pick a writable output directory up front so subsections that need to
    # write (pulled APKs, the final dump) always have somewhere to land.
    # Order: Desktop (most discoverable) → Documents → ~/.ivi-installer
    # /diagnose (guaranteed to exist, that's where logs already live) →
    # mkdtemp (last-ditch). The first one that lets us create+probe a file
    # wins.
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir: str | None = None
    for candidate in (
        os.path.expanduser("~/Desktop"),
        os.path.expanduser("~/Documents"),
        os.path.expanduser("~/.ivi-installer/diagnose"),
    ):
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, f".ivi-probe-{ts}")
            with open(probe, "w") as _fh:
                _fh.write("")
            os.unlink(probe)
            out_dir = candidate
            break
        except OSError:
            continue
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="ivi-diagnose-")
    _emit(ctx, f"  📁 diagnose output dir: {out_dir}", accum=accum)

    def section(title: str, *cmds: list[str]) -> None:
        out: list[str] = [f"### {title}", ""]
        for cmd in cmds:
            r = _run_adb(ctx, *cmd, accum=accum, timeout=15)
            out.append(f"$ adb shell {' '.join(cmd[1:]) if cmd[0] == 'shell' else ' '.join(cmd)}")
            out.append(r.stdout.rstrip() if r.stdout else "")
            if r.stderr:
                out.append("(stderr) " + r.stderr.rstrip())
            out.append(f"[exit={r.exit_code}]")
            out.append("")
        sections.append((title, out))

    section("current user",
            ["shell", "am", "get-current-user"],
            ["shell", "id"],
            ["shell", "whoami"])

    section("processes",
            ["shell", "ps", "-A"],
            ["shell", "ps", "-A", "-o", "USER,UID,PID,NAME"])

    section("services / binders",
            ["shell", "service", "list"],
            ["shell", "dumpsys", "-l"])

    section("user restrictions / settings",
            ["shell", "dumpsys", "user"],
            ["shell", "settings", "list", "global"],
            ["shell", "settings", "list", "secure"],
            ["shell", "settings", "list", "system"])

    section("appops REQUEST_INSTALL_PACKAGES",
            ["shell", "appops", "query-op", "REQUEST_INSTALL_PACKAGES"],
            ["shell", "appops", "query-op", "INSTALL_PACKAGES"],
            ["shell", "appops", "get", "com.android.shell"],
            ["shell", "appops", "get", "com.android.packageinstaller"])

    section("packages with installer role",
            ["shell", "pm", "list", "packages"],
            ["shell", "pm", "list", "packages", "-i"],
            ["shell", "pm", "get-install-location"])

    section("getprop / hdb / installer hints",
            ["shell", "getprop"])

    section("root access probe",
            ["shell", "which", "su"],
            ["shell", "ls", "-la", "/system/bin/su", "/vendor/bin/su",
             "/sbin/su", "/system/xbin/su"],
            ["shell", "su", "0", "-c", "id"],
            ["shell", "su", "-c", "id"])

    section("install-policy xml",
            ["shell", "ls", "/system/etc/sysconfig/"],
            ["shell", "ls", "/vendor/etc/sysconfig/"],
            ["shell", "ls", "/data/system/install_sources_v2.xml"])

    # ---- discover all activities/services that handle install-package
    #      intents. Pre-5.0 IVI exposed an on-device "install from USB"
    #      Activity in the engineering menu. After firmware 5.0 it's
    #      almost certainly STILL installed (system APK, not removed)
    #      — just hidden. We enumerate every component that resolves an
    #      install-related intent so the user can launch it directly via
    #      `am start -n <pkg>/<activity>` without going through the UI.
    section("install-intent handlers (Activities/Services that install APK)",
            ["shell", "cmd", "package", "query-activities",
             "-a", "android.intent.action.INSTALL_PACKAGE"],
            ["shell", "cmd", "package", "query-activities",
             "-a", "android.intent.action.VIEW",
             "-t", "application/vnd.android.package-archive"],
            ["shell", "cmd", "package", "query-services",
             "-a", "android.intent.action.INSTALL_PACKAGE"],
            ["shell", "cmd", "package", "query-receivers",
             "-a", "android.intent.action.PACKAGE_ADDED"])

    section("Huawei/Avatr/Changan system packages (dealer-mode candidates)",
            ["shell", "pm", "list", "packages", "-s",
             "huawei"],
            ["shell", "pm", "list", "packages", "-s",
             "hwapt"],
            ["shell", "pm", "list", "packages", "-s",
             "changan"],
            ["shell", "pm", "list", "packages", "-s",
             "avatr"],
            ["shell", "pm", "list", "packages", "-s",
             "deepal"],
            ["shell", "pm", "list", "packages", "-s",
             "installer"],
            ["shell", "pm", "list", "packages", "-s",
             "factory"],
            ["shell", "pm", "list", "packages", "-s",
             "engineer"],
            ["shell", "pm", "list", "packages", "-s",
             "dealer"])

    # Now enumerate components of every system package that smells of
    # install / dealer mode. We pull `dumpsys package <pkg>` and harvest
    # Activity names that mention install / usb / engineer / factory /
    # dealer / debug / dev — those are the candidates to launch directly.
    candidate_pkgs: set[str] = set()
    pl_sys = _run_adb(ctx, "shell", "pm", "list", "packages", "-s",
                      accum=accum, timeout=20)
    if pl_sys.exit_code == 0:
        for ln in (pl_sys.stdout or "").splitlines():
            ln = ln.strip()
            if not ln.startswith("package:"):
                continue
            pkg = ln[len("package:"):]
            if any(needle in pkg.lower() for needle in (
                "huawei", "hwapt", "changan", "avatr", "deepal",
                "installer", "factory", "engineer", "dealer", "dev",
                "debug", "usb", "service",
            )):
                candidate_pkgs.add(pkg)

    component_lines: list[str] = ["### system-package install components"]
    component_lines.append(
        f"Found {len(candidate_pkgs)} candidate packages."
    )
    component_lines.append("")
    keyword_re = re.compile(
        r"(install|usb|engineer|factory|dealer|debug|dev[a-z]*mode|"
        r"sideload|apk|setup|workshop|service[a-z]*menu)",
        re.IGNORECASE,
    )
    for pkg in sorted(candidate_pkgs)[:40]:        # cap to keep dump readable
        ds = _run_adb(ctx, "shell", "dumpsys", "package", pkg,
                      accum=accum, timeout=15)
        if ds.exit_code != 0 or not ds.stdout:
            continue
        component_lines.append(f"--- {pkg} ---")
        # Activities are listed under "  Activity Resolver Table:" or as
        # plain `name=...` rows in the Resolved Activities block. We just
        # grep for any line that mentions a component name with a keyword.
        for raw_line in ds.stdout.splitlines():
            stripped = raw_line.strip()
            # Component lines look like:
            #   <hex> <pkg>/<...>.SomeActivity filter <hex>
            # or:
            #   name=com.huawei.foo.bar.UsbInstallActivity ...
            if "/" in stripped and pkg in stripped and keyword_re.search(stripped):
                component_lines.append("  ⊳ " + stripped[:240])
            elif stripped.startswith("name=") and keyword_re.search(stripped):
                component_lines.append("  ⊳ " + stripped[:240])
        component_lines.append("")
    sections.append(("install components", component_lines))

    # ---- forensics: existing 3rd-party packages -----------------------
    # The car already has apps installed via the bypass we're trying to
    # rediscover. Their `dumpsys package` output reveals: the signing
    # certificate hash, the installer package that registered the install,
    # the first-install time, and the install reason. If all of them share
    # one signer or one installerPackageName — that's our trail.
    third_party_pkgs: list[str] = []
    pl = _run_adb(ctx, "shell", "pm", "list", "packages", "-3",
                  accum=accum, timeout=15)
    if pl.exit_code == 0:
        for line in (pl.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("package:"):
                third_party_pkgs.append(line[len("package:"):])

    forensics_lines: list[str] = ["### third-party app forensics", ""]
    forensics_lines.append(
        f"Found {len(third_party_pkgs)} third-party packages."
    )
    forensics_lines.append("")
    pulled_apks: list[str] = []
    desktop_pulls = os.path.join(out_dir, f"ivi-pulled-{ts}")
    for pkg in third_party_pkgs[:30]:           # cap at 30 for the report
        forensics_lines.append(f"--- {pkg} ---")
        # dumpsys package — keep only the install/sign/version block.
        ds = _run_adb(ctx, "shell", "dumpsys", "package", pkg,
                      accum=accum, timeout=15)
        for ln in (ds.stdout or "").splitlines():
            stripped = ln.strip()
            if any(kw in stripped for kw in (
                "versionCode=", "versionName=", "firstInstallTime=",
                "lastUpdateTime=", "installerPackageName=",
                "installInitiator=", "installInitiatingPackageName=",
                "installSource=", "installFlags=", "Signing certificates:",
                "signatures:", "PackageSignatures{", "UID:",
                "uid=", "Hidden system packages:",
            )):
                forensics_lines.append("  " + stripped)
            # The signature line is "Signature: XXXX..." — capture next 1-2 lines
            # too to get the cert summary.
            if "Signing certificates:" in stripped or "signatures:" in stripped:
                forensics_lines.append("  " + stripped)
        forensics_lines.append("")

    # Pull a few of the most recently-installed APKs for offline cert
    # analysis. We cap at 5 so we don't flood the user's Desktop.
    if third_party_pkgs:
        os.makedirs(desktop_pulls, exist_ok=True)
        forensics_lines.append("### pulled APKs for offline cert inspection")
        forensics_lines.append("")
    for pkg in third_party_pkgs[:5]:
        path = _run_adb(ctx, "shell", "pm", "path", pkg,
                        accum=accum, timeout=10)
        if path.exit_code != 0:
            continue
        # `pm path` returns lines like "package:/data/app/<...>/base.apk"
        # plus split paths if any. Pull only the first one.
        first = ""
        for ln in (path.stdout or "").splitlines():
            ln = ln.strip()
            if ln.startswith("package:"):
                first = ln[len("package:"):]
                break
        if not first:
            continue
        local_apk = os.path.join(desktop_pulls, f"{pkg}.apk")
        pull_cmd = _run_adb(ctx, "pull", first, local_apk,
                            accum=accum, timeout=120)
        if pull_cmd.exit_code == 0:
            pulled_apks.append(local_apk)
            forensics_lines.append(f"  ✔ {pkg}: {local_apk}")
        else:
            forensics_lines.append(f"  ✘ {pkg}: pull failed")
    sections.append(("third-party forensics", forensics_lines))

    # Save the dump. out_dir / ts were chosen at the top of the function.
    local_dump = os.path.join(out_dir, f"ivi-diagnose-{ts}.txt")
    try:
        with open(local_dump, "w", encoding="utf-8") as fh:
            for _title, out in sections:
                fh.write("\n".join(out) + "\n")
        _emit(ctx, f"  📄 diagnostic report saved: {local_dump}", accum=accum)
    except OSError as e:
        _emit(ctx, f"  (couldn't save dump: {e})", accum=accum)
        # Fallback: guaranteed-writable spot next to the rolling app log.
        fallback_dir = os.path.expanduser("~/.ivi-installer/diagnose")
        try:
            os.makedirs(fallback_dir, exist_ok=True)
            local_dump = os.path.join(fallback_dir, f"ivi-diagnose-{ts}.txt")
            with open(local_dump, "w", encoding="utf-8") as fh:
                for _title, out in sections:
                    fh.write("\n".join(out) + "\n")
            _emit(ctx, f"  📄 diagnostic report saved (fallback): {local_dump}",
                  accum=accum)
        except OSError as e2:
            _emit(ctx, f"  (fallback dump also failed: {e2})", accum=accum)
            local_dump = ""

    extras = ""
    if pulled_apks:
        extras = f" · pulled {len(pulled_apks)} APKs to {desktop_pulls}/"
    return AttemptResult(
        strategy=name, status=AttemptStatus.SKIPPED,
        summary=("Diagnostic only — no install attempted. " +
                 (f"Dump → {local_dump}" if local_dump else "(dump write failed)") +
                 extras),
        log_lines=tuple(accum),
        duration_s=time.monotonic() - t0,
    )



def _strategy_app_process_helper(ctx: InstallContext) -> AttemptResult:
    """(Re)deploy the AvatrHdbBroker daemon on-device.

    This entry doesn't perform an install — it pushes the bundled
    broker jar (idempotent via md5) and starts ``app_process
    AvatrHdbBroker``, then verifies with PING. Useful as a manual
    "kick the bypass" action from the strategy picker; the same code
    powers the Tools → Bypass health "Redeploy" button.
    """
    name = "app_process_helper"
    accum: list[str] = []
    t0 = time.monotonic()
    _emit(ctx, "── (re)deploy AvatrHdbBroker ──", accum=accum)
    fw = _run_adb(ctx, "forward", f"tcp:{HDB_BROKER_PORT}",
                  f"tcp:{HDB_BROKER_PORT}", accum=accum, timeout=10)
    if fw.exit_code != 0:
        return AttemptResult(
            strategy=name, status=AttemptStatus.FAILED,
            summary="adb forward failed — device may have detached.",
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )
    ok = _broker_deploy(ctx, accum)
    return AttemptResult(
        strategy=name,
        status=AttemptStatus.SUCCESS if ok else AttemptStatus.FAILED,
        summary=("Broker is up." if ok
                 else "Broker won't start — check dealer mode + adbd."),
        log_lines=tuple(accum), duration_s=time.monotonic() - t0,
    )


# ---- HDB broker install (the working path on HarmonySpace 5.0) ------------

# Loopback port that the leftover `app_process64 AvatrHdbBroker` listens on.
HDB_BROKER_PORT = 38787

# The Deepal-side installer package. The Avatr default in the bridge JAR
# (`com.huawei.appmarket.vehicle`) doesn't exist on Deepal S09, so we have
# to pass this explicitly with every single-APK install command.
HDB_DEEPAL_INSTALLER = "com.huawei.appinstaller.car"

# Multimedia-screen Android users we fan the install out to. Each
# physical screen is bound to one user; the HUD has none.
#
# This is the *fallback* set used only when probing the device returned
# nothing usable. Real cabin user ids vary by car model and firmware —
# different builds have been seen with main user as low as 10 (no 13)
# and with extra users above 13. Always prefer ``_live_screen_users``
# over hardcoded references at runtime.
HDB_SCREEN_USERS = (10, 11, 12, 13)


def _live_screen_users(serial: str) -> list[int]:
    """Return the device's actual multimedia-screen user ids.

    Probes ``pm list users`` and keeps every running, non-system user
    (id ≥ 10). Falls back to the hardcoded ``HDB_SCREEN_USERS`` when
    the probe returns nothing — better to attempt the canonical set than
    skip fan-out entirely.

    Sorted ascending so the highest id (typically the active driver
    user) lands at the end and ``[-1]`` matches the competitor's
    ``car_user_id = max(running non-zero)`` pattern.
    """
    try:
        users = list_android_users(serial)
    except Exception:
        log.exception("list_android_users raised; using fallback set")
        return list(HDB_SCREEN_USERS)
    ids = sorted({u.user_id for u in users
                  if u.running and u.user_id >= 10})
    if not ids:
        # Probe ran but returned nothing — keep canonical fallback so
        # cascade still has *some* targets to try.
        return list(HDB_SCREEN_USERS)
    return ids

# On-device location of the helper jar that does runtime permission
# grants via reflection. We push it once per session — sourced from
# `ivi_installer/resources/hw-perm-grant.jar` (built from the Java
# source in the same dir; see tools/hw-perm-grant/build.sh).
HDB_PERM_HELPER_REMOTE = "/data/local/tmp/ivi-perm-grant.jar"
HDB_PERM_HELPER_RESOURCE = "hw-perm-grant.jar"

# On-device location of the AvatrHdbBroker jar. Matches what the
# original 2026-04-28 workshop session left on the car so existing
# tooling that tails the log keeps working.
HDB_BROKER_REMOTE = "/data/local/tmp/avatr-hdb-broker.jar"
HDB_BROKER_LOG = "/data/local/tmp/avatr-hdb-broker.log"
HDB_BROKER_RESOURCE = "avatr-hdb-broker.jar"
HDB_BROKER_MAIN_CLASS = "AvatrHdbBroker"


def _broker_send(payload: bytes, *, timeout: float = 180.0) -> bytes:
    """Send one frame to the on-device broker and return the full response.

    The broker accepts ONE command per connection and closes the socket
    after writing the RESULT line. We deliberately do NOT half-close our
    write side — it confuses the broker's read loop on some firmwares.
    """
    import socket as _socket

    s = _socket.create_connection(("127.0.0.1", HDB_BROKER_PORT),
                                   timeout=timeout)
    try:
        s.sendall(payload)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = s.recv(65536)
            except _socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        try:
            s.close()
        except OSError:
            pass


def _broker_run(args: list[str], *, timeout: float = 180.0) -> tuple[int, str]:
    """Invoke `HuaweiShellBridge.main(args)` via the broker.

    Returns (exit_code, output). exit_code -1 indicates a malformed or
    empty response (broker probably crashed mid-request).
    """
    import base64

    encoded = " ".join(base64.b64encode(a.encode("utf-8")).decode("ascii")
                       for a in args)
    raw = _broker_send(f"RUN {encoded}\n".encode("utf-8"), timeout=timeout)
    text = raw.decode("utf-8", "replace").rstrip("\n")
    parts = text.split(" ", 2)
    if not parts or parts[0] != "RESULT" or len(parts) != 3:
        return -1, text
    try:
        code = int(parts[1])
    except ValueError:
        return -1, text
    payload = base64.b64decode(parts[2]).decode("utf-8", "replace")
    return code, payload


def _broker_ping(*, timeout: float = 2.0) -> bool:
    """Open one connection to the broker and check for a PONG reply.

    Returns True iff the broker answered with ``pong`` (raw or b64 form).
    Catches OSError so callers can use this as a boolean probe without
    try/except.
    """
    try:
        raw = _broker_send(b"PING\n", timeout=timeout)
    except OSError:
        return False
    return b"pong" in raw.lower() or b"cG9uZw" in raw   # b64('pong')


def _load_broker_jar() -> bytes | None:
    """Return the bundled avatr-hdb-broker.jar bytes, or None if missing.

    The jar is shipped via ``[tool.hatch.build.targets.wheel.force-include]``
    in pyproject.toml, mirroring how hw-perm-grant.jar is wired up.
    """
    try:
        from importlib import resources as _resources
    except ImportError:
        return None
    try:
        with _resources.files("ivi_installer.resources").joinpath(
                HDB_BROKER_RESOURCE).open("rb") as fh:
            return fh.read()
    except (FileNotFoundError, ModuleNotFoundError, AttributeError):
        return None


def _md5_hex(blob: bytes) -> str:
    import hashlib
    return hashlib.md5(blob).hexdigest()


def _broker_remote_md5(ctx: InstallContext, accum: list[str]) -> str | None:
    """Read the md5 of the on-device broker jar, or None if absent."""
    r = _run_adb(ctx, "shell", "md5sum", HDB_BROKER_REMOTE,
                 accum=accum, timeout=15)
    out = (r.stdout or "").strip().split()
    if r.exit_code == 0 and out and len(out[0]) == 32:
        return out[0].lower()
    return None


def _broker_pid(ctx: InstallContext, accum: list[str]) -> int | None:
    """Find the PID of the running AvatrHdbBroker app_process, if any.

    `ps -A -o PID,ARGS` works on Huawei toybox; we match on the
    ``AvatrHdbBroker`` argv entry. Returns the smallest matching PID or
    None.
    """
    r = _run_adb(ctx, "shell", "ps", "-A", "-o", "PID,ARGS",
                 accum=accum, timeout=15)
    if r.exit_code != 0 or not r.stdout:
        return None
    pids: list[int] = []
    for line in r.stdout.splitlines():
        if HDB_BROKER_MAIN_CLASS not in line:
            continue
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pids.append(int(parts[0]))
        except ValueError:
            continue
    return min(pids) if pids else None


def _broker_uptime_s(
    ctx: InstallContext, accum: list[str], pid: int,
) -> int | None:
    """Read the broker process's uptime in seconds via ``ps -o etimes``.

    Returns None if the field can't be read.
    """
    r = _run_adb(ctx, "shell", "ps", "-p", str(pid), "-o", "ETIMES=",
                 accum=accum, timeout=10)
    if r.exit_code != 0 or not r.stdout:
        return None
    try:
        return int(r.stdout.strip().split()[0])
    except (ValueError, IndexError):
        return None


def _broker_launch_command() -> str:
    """The shell incantation that starts the broker as a detached daemon.

    Wrapped in `sh -c` because plain `nohup ... &` doesn't reliably
    detach on toybox, and `setsid`/`disown` aren't available on Huawei
    automotive firmwares. Log redirect matches the path the original
    workshop-session broker writes to, so existing tail tooling still
    works.
    """
    return (
        f'nohup sh -c "CLASSPATH={HDB_BROKER_REMOTE} '
        f'app_process /system/bin {HDB_BROKER_MAIN_CLASS} {HDB_BROKER_PORT}" '
        f'>{HDB_BROKER_LOG} 2>&1 &'
    )


def _broker_deploy(ctx: InstallContext, accum: list[str]) -> bool:
    """Push the bundled broker jar (if needed) and start the daemon.

    Steps:
      1. Load the bundled jar; bail if it isn't packaged.
      2. Push to /data/local/tmp/ if remote md5 differs (idempotent).
      3. Launch via wrapped `nohup sh -c "..." &`.
      4. Wait briefly, then PING up to 3 times (1 s spacing).

    Returns True iff the broker answers PONG after launch. Does NOT
    touch `adb forward` — callers handle that in `_broker_health_check`.
    """
    jar = _load_broker_jar()
    if jar is None:
        _emit(ctx, "  ✘ broker jar resource missing from app bundle",
              accum=accum)
        return False
    bundled_md5 = _md5_hex(jar)
    remote_md5 = _broker_remote_md5(ctx, accum)
    if remote_md5 == bundled_md5:
        _emit(ctx, f"  ✓ broker jar already on device (md5={bundled_md5[:12]}…)",
              accum=accum)
    else:
        _emit(ctx,
              f"  pushing broker jar (remote md5={remote_md5 or 'absent'} "
              f"→ bundled md5={bundled_md5[:12]}…)", accum=accum)
        import os as _os
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(
                prefix="ivi-hdb-broker-", suffix=".jar", delete=False
        ) as tmp:
            tmp.write(jar)
            host_path = tmp.name
        try:
            push = _run_adb(ctx, "push", host_path, HDB_BROKER_REMOTE,
                            accum=accum, timeout=60)
            if push.exit_code != 0:
                _emit(ctx, f"  ✘ broker jar push failed (exit={push.exit_code})",
                      accum=accum)
                return False
        finally:
            try: _os.unlink(host_path)
            except OSError: pass

    _emit(ctx, "  starting broker (nohup app_process)…", accum=accum)
    launch = _run_adb(ctx, "shell", _broker_launch_command(),
                      accum=accum, timeout=15)
    if launch.exit_code != 0:
        # Non-fatal: the daemon may still have spawned. Continue to PING.
        _emit(ctx, f"  ⚠ launch returned exit={launch.exit_code}; "
                   "checking PING anyway", accum=accum)

    # Boot loop: the daemon needs a moment to bind tcp:38787 + load the
    # framework JARs. Most firmwares are alive in well under a second
    # but loaded vehicle systems can take ~2 s on cold boot.
    time.sleep(1.5)
    for attempt_n in range(3):
        if _broker_ping(timeout=2.0):
            _emit(ctx, f"  ✓ broker came up (attempt {attempt_n + 1})",
                  accum=accum)
            return True
        time.sleep(1.0)
    return False


def _broker_health_check(ctx: InstallContext, accum: list[str]) -> bool:
    """Ensure adb-forward is set and the broker is reachable.

    On PING failure we transparently auto-deploy the bundled jar — that
    way an install on a freshly-rebooted car (where the leftover daemon
    is gone) still works without user intervention.

    Side-effect: sets up `adb forward tcp:38787 tcp:38787` for this
    device, so the rest of the strategy can talk to the broker.
    """
    fw = _run_adb(ctx, "forward", f"tcp:{HDB_BROKER_PORT}",
                   f"tcp:{HDB_BROKER_PORT}", accum=accum, timeout=10)
    if fw.exit_code != 0:
        _emit(ctx, "  ✘ adb forward failed — is the device still attached?",
              accum=accum)
        return False
    if _broker_ping(timeout=2.0):
        _emit(ctx, f"  ✓ broker alive on tcp:{HDB_BROKER_PORT}", accum=accum)
        return True
    _emit(ctx, f"  broker not responding on tcp:{HDB_BROKER_PORT} — "
               "auto-deploying", accum=accum)
    if _broker_deploy(ctx, accum):
        return True
    _emit(ctx, "  ✘ broker won't start — check that the car is in dealer "
               "mode and adbd is alive.", accum=accum)
    return False


# ---- broker introspection (for the Tools → Bypass health UI) -------------

@dataclass(frozen=True)
class BrokerStatus:
    """Snapshot of the on-device broker's state. All fields are best-effort."""
    forwarded: bool             # `adb forward` is set up on the host
    alive: bool                 # PING returned PONG
    pid: int | None             # running app_process PID, if any
    uptime_s: int | None        # process etime in seconds
    port: int                   # always HDB_BROKER_PORT, included for the UI
    remote_md5: str | None      # md5 of jar on device
    bundled_md5: str | None     # md5 of jar bundled with this app build
    jar_matches: bool           # remote_md5 == bundled_md5

    @property
    def jar_present(self) -> bool:
        return self.remote_md5 is not None


def collect_broker_status(serial: str) -> BrokerStatus:
    """Read the broker's current state. Used by the 'Bypass health' UI.

    Spawns adb commands inline rather than going through InstallContext —
    this is intended for short-lived UI worker calls, not the install
    pipeline. Failures collapse to None / False fields rather than raising.
    """
    accum: list[str] = []
    ctx = InstallContext(serial=serial, apk_paths=[], log=lambda _l: None)
    fw = _run_adb(ctx, "forward", f"tcp:{HDB_BROKER_PORT}",
                  f"tcp:{HDB_BROKER_PORT}", accum=accum, timeout=10)
    forwarded = fw.exit_code == 0
    alive = forwarded and _broker_ping(timeout=2.0)
    pid = _broker_pid(ctx, accum)
    uptime_s = _broker_uptime_s(ctx, accum, pid) if pid else None
    remote_md5 = _broker_remote_md5(ctx, accum)
    bundled = _load_broker_jar()
    bundled_md5 = _md5_hex(bundled) if bundled else None
    return BrokerStatus(
        forwarded=forwarded, alive=alive, pid=pid, uptime_s=uptime_s,
        port=HDB_BROKER_PORT, remote_md5=remote_md5,
        bundled_md5=bundled_md5,
        jar_matches=bool(remote_md5 and remote_md5 == bundled_md5),
    )


def redeploy_broker(serial: str, log: Callable[[str], None]) -> bool:
    """Push + (re)start the broker. Used by the 'Redeploy' Tools button.

    Always re-pushes if the bundled md5 differs from what's on device,
    and always restarts the daemon — even if a PING currently succeeds —
    so the user has a reliable "kick it" affordance.
    """
    accum: list[str] = []
    ctx = InstallContext(serial=serial, apk_paths=[], log=log)
    fw = _run_adb(ctx, "forward", f"tcp:{HDB_BROKER_PORT}",
                  f"tcp:{HDB_BROKER_PORT}", accum=accum, timeout=10)
    if fw.exit_code != 0:
        log("  ✘ adb forward failed — is the device still attached?")
        return False
    return _broker_deploy(ctx, accum)


def _strategy_hdb_broker_install(ctx: InstallContext) -> AttemptResult:
    """Single working install path on HarmonySpace 5.0 (Deepal S09 / HwSAPT).

    Pipeline:
      1. PING the leftover `AvatrHdbBroker` on tcp:38787 (via adb-forward).
      2. Push the APK to /data/local/tmp/iviinstaller_<pkg>.apk.
      3. Bridge call `hdb-session-install-user - <apk> <seed-user> <nonce>
         com.huawei.appinstaller.car 1000` — the HDB-flagged session
         (installFlags |= 262144) which Huawei's HwInstallPolicy hook
         trusts.
      4. If `target_user is None` → fan out to every multimedia-screen user
         (10/11/12/13) via the stock `pm install-existing --user N`. That
         doesn't go through the bridge at all (no policy hook), because
         the APK has already been validated globally in step 3.

    Single .apk only. ``build_context_from_path`` already enforces this
    at the input boundary; the guard here is a defensive backstop in
    case a caller builds an :class:`InstallContext` directly.
    """
    name = "hdb_broker_install"
    accum: list[str] = []
    t0 = time.monotonic()
    rep = _StageReporter(ctx, name)

    # ---- input shape -----------------------------------------------------
    if len(ctx.apk_paths) != 1 or ctx.apk_paths[0].suffix.lower() != ".apk":
        return AttemptResult(
            strategy=name, status=AttemptStatus.TERMINAL,
            summary="Only a single .apk is supported.",
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )

    local_apk = ctx.apk_paths[0]

    # ---- stage 0: broker health -----------------------------------------
    rep.start(0, "Checking on-device helper")
    _emit(ctx, "── stage 0: broker health check ──", accum=accum)
    if not _broker_health_check(ctx, accum):
        rep.failed(0, "Helper not reachable")
        return AttemptResult(
            strategy=name, status=AttemptStatus.FAILED,
            summary=("Broker not reachable on 127.0.0.1:"
                     f"{HDB_BROKER_PORT}. The on-device AvatrHdbBroker "
                     "process needs to be (re)started before installs "
                     "will work."),
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )
    rep.done(0, "Helper online")

    # ---- decide multi-user behaviour ------------------------------------
    seed_user, fan_out = _resolve_targets(ctx)
    if not fan_out:
        mode_label = f"user {seed_user} only"
    else:
        mode_label = (f"seed user {seed_user}, fan-out to "
                      f"{', '.join(str(u) for u in fan_out)}")
    _emit(ctx, f"  install target: {mode_label}", accum=accum)

    # ---- stage 1: push --------------------------------------------------
    rep.start(1, "Uploading package")
    _emit(ctx, "── stage 1: push APK ──", accum=accum)
    pkg_hint = ctx.package or local_apk.stem
    remote_name = _sanitize_remote_name(f"iviinstaller_{pkg_hint}.apk")
    remote = f"/data/local/tmp/{remote_name}"
    push = _run_adb(ctx, "push", str(local_apk), remote,
                    accum=accum, timeout=600)
    if push.exit_code != 0:
        rep.failed(1, "Upload failed")
        return AttemptResult(
            strategy=name, status=AttemptStatus.FAILED,
            summary=f"adb push failed (exit={push.exit_code})",
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )
    rep.done(1, "Package staged")

    # ---- stage 2: bridge install (HDB-flagged session) ------------------
    rep.start(2, "Authorizing install session")
    installer_pkg = ctx.preferred_installer or HDB_DEEPAL_INSTALLER
    nonce = f"ivi-{pkg_hint}-{int(time.time())}"
    _emit(ctx, "── stage 2: hdb-session-install-user via broker ──",
          accum=accum)
    _emit(ctx, f"  installerPackage={installer_pkg}", accum=accum)
    bridge_args = [
        "hdb-session-install-user",
        ctx.package or "-",
        remote,
        str(seed_user),
        nonce,
        installer_pkg,
        "1000",                         # originatingUid = system
    ]
    code, output = _broker_run(bridge_args, timeout=300.0)
    for line in output.splitlines():
        # Surface only the structured legacyTrace lines + the final result
        # line — keeps the UI log readable.
        if line.startswith("legacyTrace ") or line.startswith("hdbSessionInstallUser"):
            _emit(ctx, f"  {line[:240]}", accum=accum)

    if code != 0 or "INSTALL_SUCCEEDED" not in output:
        # Try to pluck a meaningful failure code out of the bridge output.
        m = re.search(r"INSTALL_FAILED_[A-Z0-9_]+", output)
        failure_code = m.group(0) if m else None
        terminal = failure_code in TERMINAL_CODES
        rep.failed(2, "Install rejected")
        # Always best-effort cleanup of the staged file.
        _run_adb(ctx, "shell", "rm", "-f", remote, accum=accum, timeout=10)
        return AttemptResult(
            strategy=name,
            status=AttemptStatus.TERMINAL if terminal else AttemptStatus.FAILED,
            summary=(f"Bridge install failed: "
                     f"{failure_code or 'no INSTALL_SUCCEEDED in output'}"),
            failure_code=failure_code,
            hint=_hint_for(failure_code),
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )

    pkg_resolved = _resolve_installed_package(output, ctx.package or pkg_hint)
    _emit(ctx, f"  ✓ stage 2 success — package {pkg_resolved}", accum=accum)
    rep.done(2, "Install authorized")

    # ---- stage 3: fan-out via pm install-existing -----------------------
    fan_out_failures: list[str] = []
    if fan_out:
        rep.start(3, f"Mirroring to {len(fan_out)} screen(s)")
        _emit(ctx, f"── stage 3: fan-out to users {fan_out} ──", accum=accum)
        for u in fan_out:
            r = _run_adb(ctx, "shell", "pm", "install-existing",
                         "--user", str(u), pkg_resolved,
                         accum=accum, timeout=60)
            stdout = (r.stdout or "").strip()
            if "installed for user" in stdout:
                _emit(ctx, f"  ✓ user {u}", accum=accum)
            elif "not installed" in stdout.lower() or r.exit_code != 0:
                _emit(ctx, f"  ✘ user {u}: {stdout or '(no output)'}",
                      accum=accum)
                fan_out_failures.append(str(u))
        ok_count = len(fan_out) - len(fan_out_failures)
        if fan_out_failures:
            rep.failed(3, f"Mirrored to {ok_count} of {len(fan_out)}")
        else:
            rep.done(3, f"Mirrored to {ok_count} screen(s)")
    else:
        rep.skipped(3, "Single-user install — no mirroring needed")

    # ---- stage 4: grant runtime permissions ----------------------------
    # Without this `pm grant` would do nothing (Huawei policy intercepts
    # the shell command). The helper jar runs as `app_process` and calls
    # `IPackageManager.grantRuntimePermission()` directly via reflection,
    # which is NOT hooked.
    grant_users = [seed_user] + [
        u for u in fan_out if str(u) not in fan_out_failures
    ]
    rep.start(4, f"Granting permissions on {len(grant_users)} user(s)")
    _emit(ctx,
          "── stage 4: grant runtime perms via helper "
          f"(users {grant_users}) ──", accum=accum)
    grant_summary = _grant_runtime_perms(ctx, pkg_resolved, grant_users, accum)
    grant_failed_users = [
        str(u) for u, (_ok, fail) in grant_summary.items() if fail
    ]
    grant_payload = {"summary": dict(grant_summary), "users": list(grant_users)}
    if grant_failed_users:
        rep.failed_with_data(
            4, f"Some permissions denied on user(s) "
               f"{','.join(grant_failed_users)}",
            grant_payload,
        )
    else:
        granted_total = sum(ok for ok, _ in grant_summary.values())
        rep.done_with_data(
            4, (f"Granted {granted_total} permission(s)"
                if granted_total else "No additional permissions needed"),
            grant_payload,
        )

    # ---- cleanup --------------------------------------------------------
    _run_adb(ctx, "shell", "rm", "-f", remote, accum=accum, timeout=10)

    issues: list[str] = []
    if fan_out_failures:
        issues.append(f"fan-out failed for users {','.join(fan_out_failures)}")
    if grant_failed_users:
        issues.append(f"runtime-perm grants failed for users "
                      f"{','.join(grant_failed_users)}")
    if issues:
        return AttemptResult(
            strategy=name, status=AttemptStatus.SUCCESS,
            summary=f"Installed {pkg_resolved}; " + "; ".join(issues) + ".",
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )

    if fan_out:
        summary = (f"Installed {pkg_resolved} on user 0 + screens "
                   f"{','.join(str(u) for u in fan_out)}.")
    else:
        summary = f"Installed {pkg_resolved} for user {seed_user}."
    return AttemptResult(
        strategy=name, status=AttemptStatus.SUCCESS,
        summary=summary,
        log_lines=tuple(accum), duration_s=time.monotonic() - t0,
    )


def _resolve_installed_package(bridge_output: str, fallback: str) -> str:
    """Pluck the package name the bridge reported (`effectivePackage=...`)."""
    m = re.search(r"step=effectivePackage detail=(\S+)", bridge_output)
    if m and m.group(1) and m.group(1) != "null":
        return m.group(1)
    return fallback


# ---- pm-disable-packageinstaller strategy --------------------------------
#
# The competitor (AvatrAppInstaller v1.4) ships a much simpler bypass than
# our HDB-broker dance: temporarily disable `com.android.packageinstaller`
# (where the HwInstallPolicy hook lives on Avatr 11 / HarmonyOS 2.x and
# 4.x) for the *active driver user* (not user 0), run a stock
# `pm install -r -d -g -i <huawei-installer>` *without* `--user`, then
# re-enable on the same user. No broker, no app_process, no jar push.
#
# v0.12.0 — aligned with the competitor recipe after side-by-side analysis
# (see plans/ for reports). Previously we disabled on user 0 (HEADLESS on
# Huawei IVI) and added `--user 0 -t` to `pm install`, which left the
# policy hook live on the active driver user (13) and rendered the
# "not allowed" dialog regardless. Now: per-user disable on
# max(running non-zero), pm install without `--user` / `-t`, symmetric
# re-enable. If disable fails, abort cleanly — cascade falls back to
# the HDB-broker strategy which is known to work on HarmonySpace 5.0.

PACKAGEINSTALLER_PKG = "com.android.packageinstaller"


def _resolve_packageinstaller_user(
    ctx: InstallContext, accum: list[str],
) -> int:
    """Pick the Android user id whose PackageInstaller we should disable.

    Mirrors the competitor's `car_user_id` logic from `_detect_firmware`:
    parse `pm list users`, take `max(running non-zero ids)`. On a Deepal
    S09 with users 0/10/11/12/13 this lands on **13** (the active driver
    user), which is where the HwInstallPolicy dialog actually renders.

    Falls back to 0 if the probe fails or returns no usable users — same
    behaviour as the competitor (and harmless: the dialog might still
    appear, but we degrade gracefully and the cascade can move on to the
    HDB-broker strategy).
    """
    try:
        users = list_android_users(ctx.serial)
    except Exception as e:
        _emit(ctx, f"  pm list users raised ({e}); disabling on user 0",
              accum=accum)
        return 0
    running_non_zero = [u.user_id for u in users
                        if u.running and u.user_id > 0]
    if running_non_zero:
        return max(running_non_zero)
    # Last-ditch fallback: pick the second user from the list (mirrors
    # the competitor's `user_ids[1]` branch when nothing was tagged
    # running). 0 if the list is empty or only contains user 0.
    non_zero = [u.user_id for u in users if u.user_id > 0]
    if non_zero:
        return min(non_zero)
    return 0


def _strategy_pm_disable_install(ctx: InstallContext) -> AttemptResult:
    """`pm disable-user packageinstaller → pm install -i <huawei> → enable`.

    The competitor's primary bypass. Cheaper than HDB session install:
    no broker on the device, no on-device classpath helper. Always
    re-enables PackageInstaller in `finally` so a crash mid-install
    doesn't leave the user with a broken AppGallery.
    """
    name = "pm_disable_install"
    accum: list[str] = []
    t0 = time.monotonic()
    rep = _StageReporter(ctx, name)

    if len(ctx.apk_paths) != 1 or ctx.apk_paths[0].suffix.lower() != ".apk":
        return AttemptResult(
            strategy=name, status=AttemptStatus.SKIPPED,
            summary="Only a single .apk is supported.",
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )

    local_apk = ctx.apk_paths[0]

    # Resolve the Huawei installer pkg by probing the device. Falls back
    # to whatever the user picked (preferred_installer) or the Deepal
    # default. This is the same trick the competitor's `_detect_firmware`
    # does, just delayed until install time so the rest of the UI doesn't
    # need a new pre-install probe step.
    installer = ctx.preferred_installer or HDB_DEEPAL_INSTALLER
    if not ctx.preferred_installer:
        from . import firmware as _firmware
        try:
            profile = _firmware.detect(ctx.serial, timeout=10)
            installer = profile.installer_pkg
            _emit(ctx, f"  detected firmware: {profile.label}", accum=accum)
        except Exception as e:
            _emit(ctx, f"  firmware probe raised ({e}); using default {installer}",
                  accum=accum)

    # ---- decide multi-user behaviour (mirrors the HDB strategy) ----------
    seed_user, fan_out = _resolve_targets(ctx)
    if not fan_out:
        mode_label = f"user {seed_user} only"
    else:
        mode_label = (f"seed user {seed_user}, fan-out to "
                      f"{', '.join(str(u) for u in fan_out)}")
    _emit(ctx, f"  install target: {mode_label}", accum=accum)
    _emit(ctx, f"  installerPackage={installer}", accum=accum)

    # ---- stage 0: stage package on device --------------------------------
    rep.start(0, "Uploading package")
    if ctx.force_reinstall and ctx.package:
        _emit(ctx, f"── stage 0: force-reinstall (uninstall {ctx.package}) ──",
              accum=accum)
        _force_uninstall(ctx, ctx.package, accum)

    _emit(ctx, "── stage 1: push APK ──", accum=accum)
    pkg_hint = ctx.package or local_apk.stem
    remote_name = _sanitize_remote_name(f"iviinstaller_{pkg_hint}.apk")
    remote = f"/data/local/tmp/{remote_name}"
    push = _run_adb(ctx, "push", str(local_apk), remote,
                    accum=accum, timeout=600)
    if push.exit_code != 0:
        rep.failed(0, "Upload failed")
        return AttemptResult(
            strategy=name, status=AttemptStatus.FAILED,
            summary=f"adb push failed (exit={push.exit_code})",
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )
    rep.done(0, "Package staged")

    # Disable PackageInstaller on the *active driver user* — that's where
    # the HwInstallPolicy hook actually renders its "not allowed" dialog.
    # Disabling on user 0 (HEADLESS on Huawei IVI) was the v0.11 bug that
    # left the hook live on user 13. Always re-enable in `finally`, on
    # the same user, even if the install raises.
    pi_user = _resolve_packageinstaller_user(ctx, accum)
    _emit(ctx, f"  PackageInstaller target user: {pi_user}", accum=accum)
    disabled = False
    install_stage_outcome: tuple[str, str] | None = None  # (kind, hint)
    try:
        rep.start(1, "Adjusting installer policy")
        _emit(ctx, f"── stage 2: disable PackageInstaller on user {pi_user} ──",
              accum=accum)
        dis = _run_adb(ctx, "shell", "pm", "disable-user",
                       "--user", str(pi_user), PACKAGEINSTALLER_PKG,
                       accum=accum, timeout=15)
        # Trust exit_code==0 (mirrors competitor's `ok = returncode == 0`).
        # No global-`pm disable` fallback: it's strictly more dangerous
        # (kills PackageInstaller for every user at once) and on Huawei
        # builds where per-user disable fails, the global form fails too.
        disabled = dis.exit_code == 0
        if not disabled:
            _emit(ctx, f"  ⚠ pm disable-user --user {pi_user} failed "
                       f"(exit={dis.exit_code}); aborting so the cascade "
                       f"can fall through to the HDB-broker strategy.",
                  accum=accum)
            # Best-effort re-enable on the same user (mirrors competitor).
            _run_adb(ctx, "shell", "pm", "enable",
                     "--user", str(pi_user), PACKAGEINSTALLER_PKG,
                     accum=accum, timeout=15)
            rep.failed(1, "Policy disable failed")
            return AttemptResult(
                strategy=name, status=AttemptStatus.FAILED,
                summary=(f"pm disable-user failed on user {pi_user} "
                         f"(exit={dis.exit_code}); cascade will try the "
                         f"HDB-broker strategy next."),
                log_lines=tuple(accum), duration_s=time.monotonic() - t0,
            )
        rep.done(1, "Policy temporarily relaxed")

        # ---- stage 3: streamed install via stock pm --------------------
        # Competitor recipe (v0.12.0): pm install -r -d -g -i <installer>
        # <remote>. No `-t`; testOnly APKs are rare and the broker strategy
        # covers them via installFlags if needed.
        #
        # `--user` policy:
        #   * single-screen UI selection (e.g. only Driver) → seed_user=N,
        #     fan_out=[] → install with `--user N` so the package lands
        #     ONLY on the picked user. Without `--user` PMS treats it as
        #     USER_ALL and the package leaks onto every screen — that's
        #     the v0.22 bug we're fixing here.
        #   * multi-screen / legacy `target_user`/`target_users` unset →
        #     fan_out is non-empty (or default canonical set). Keep the
        #     no-`--user` form so PMS does USER_ALL — it covers all
        #     fan-out targets without an extra mirror stage. (pm-disable
        #     strategy has no per-user fan-out step like the HDB one.)
        rep.start(2, "Installing package")
        flags: list[str] = ["-r"]
        if ctx.allow_downgrade:
            flags.append("-d")
        if ctx.grant_runtime:
            flags.append("-g")
        flags += ["-i", installer]
        install_cmd: list[str] = ["shell", "pm", "install"]
        if not fan_out:
            install_cmd += ["--user", str(seed_user)]
            stage_label = (f"── stage 3: pm install --user {seed_user} "
                           f"-r -d -g -i <installer> ──")
        else:
            stage_label = "── stage 3: pm install -r -d -g -i <installer> ──"
        install_cmd += [*flags, remote]
        _emit(ctx, stage_label, accum=accum)
        install = _run_adb(ctx, *install_cmd, accum=accum, timeout=900)
        ok, code = parse_pm_output(install.stdout + "\n" + install.stderr)
        # Alt-installer retry only when PMS reported a real INSTALL_FAILED
        # (mirrors competitor: only on `'INSTALL_FAILED' in out`). Skips
        # the retry on parse-shape failures or transport hiccups.
        if not ok and code is not None:
            alt = ("com.huawei.appmarket.vehicle"
                   if installer == "com.huawei.appinstaller.car"
                   else "com.huawei.appinstaller.car")
            _emit(ctx, f"  primary installer failed ({code}); retrying with "
                       f"{alt}", accum=accum)
            alt_flags = list(flags)
            for i, f in enumerate(alt_flags):
                if f == installer:
                    alt_flags[i] = alt
                    break
            else:
                alt_flags += ["-i", alt]
            alt_cmd: list[str] = ["shell", "pm", "install"]
            if not fan_out:
                alt_cmd += ["--user", str(seed_user)]
            alt_cmd += [*alt_flags, remote]
            install2 = _run_adb(ctx, *alt_cmd, accum=accum, timeout=900)
            ok, code = parse_pm_output(
                install2.stdout + "\n" + install2.stderr)
        if not ok:
            terminal = code in TERMINAL_CODES
            install_stage_outcome = ("failed", "Install rejected")
            return AttemptResult(
                strategy=name,
                status=AttemptStatus.TERMINAL if terminal else AttemptStatus.FAILED,
                summary=f"pm install failed: {code or 'no Success line'}",
                failure_code=code, hint=_hint_for(code),
                log_lines=tuple(accum), duration_s=time.monotonic() - t0,
            )
        install_stage_outcome = ("done", "Package installed")
    finally:
        # Resolve stage 2 outcome — `try` may have returned via the
        # failure path (no marker set) or completed cleanly.
        if install_stage_outcome is None:
            rep.failed(2, "Install aborted")
        else:
            kind, hint = install_stage_outcome
            if kind == "done":
                rep.done(2, hint)
            else:
                rep.failed(2, hint)

        # ALWAYS re-enable PackageInstaller on the same user we disabled,
        # even if anything in the try block raised. Leaving it disabled
        # bricks AppGallery on the car for that user.
        if disabled:
            rep.start(3, "Restoring installer policy")
            _emit(ctx, f"── stage 4: re-enable PackageInstaller on user "
                       f"{pi_user} ──", accum=accum)
            _run_adb(ctx, "shell", "pm", "enable",
                     "--user", str(pi_user), PACKAGEINSTALLER_PKG,
                     accum=accum, timeout=15)
            rep.done(3, "Policy restored")
        else:
            rep.skipped(3, "No policy change to restore")
        # Always best-effort cleanup of the staged file.
        try:
            _run_adb(ctx, "shell", "rm", "-f", remote,
                     accum=accum, timeout=10)
        except Exception:
            pass

    pkg_resolved = ctx.package or pkg_hint

    # ---- stage 4: fan-out via pm install-existing -----------------------
    fan_out_failures: list[str] = []
    if fan_out:
        rep.start(4, f"Mirroring to {len(fan_out)} screen(s)")
        _emit(ctx, f"── stage 5: fan-out to users {fan_out} ──", accum=accum)
        for u in fan_out:
            r = _run_adb(ctx, "shell", "pm", "install-existing",
                         "--user", str(u), pkg_resolved,
                         accum=accum, timeout=60)
            stdout = (r.stdout or "").strip()
            if "installed for user" in stdout:
                _emit(ctx, f"  ✓ user {u}", accum=accum)
            elif "not installed" in stdout.lower() or r.exit_code != 0:
                _emit(ctx, f"  ✘ user {u}: {stdout or '(no output)'}",
                      accum=accum)
                fan_out_failures.append(str(u))
        ok_count = len(fan_out) - len(fan_out_failures)
        if fan_out_failures:
            rep.failed(4, f"Mirrored to {ok_count} of {len(fan_out)}")
        else:
            rep.done(4, f"Mirrored to {ok_count} screen(s)")
    else:
        rep.skipped(4, "Single-user install — no mirroring needed")

    # The `-g` flag at install time should auto-grant runtime perms for
    # user 0; for fan-out users we re-run the same grants via the helper
    # if any are still missing. This keeps parity with the HDB pipeline.
    grant_users = [seed_user] + [u for u in fan_out
                                  if str(u) not in fan_out_failures]
    grant_summary: dict[int, tuple[int, int]] = {}
    if ctx.grant_runtime:
        rep.start(5, f"Granting permissions on {len(grant_users)} user(s)")
        try:
            grant_summary = _grant_runtime_perms(ctx, pkg_resolved,
                                                  grant_users, accum)
        except Exception as e:
            _emit(ctx, f"  (helper grant raised: {e}; -g flag should "
                       "have covered most perms)", accum=accum)
    else:
        rep.skipped(5, "Runtime permissions not requested")
    grant_failed_users = [
        str(u) for u, (_ok, fail) in grant_summary.items() if fail
    ]
    if ctx.grant_runtime:
        grant_payload = {"summary": dict(grant_summary),
                          "users": list(grant_users)}
        if grant_failed_users:
            rep.failed_with_data(
                5, f"Some permissions denied on user(s) "
                   f"{','.join(grant_failed_users)}",
                grant_payload,
            )
        else:
            granted_total = sum(ok for ok, _ in grant_summary.values())
            rep.done_with_data(
                5, (f"Granted {granted_total} permission(s)"
                    if granted_total else "No additional permissions needed"),
                grant_payload,
            )

    issues: list[str] = []
    if fan_out_failures:
        issues.append(f"fan-out failed for users {','.join(fan_out_failures)}")
    if grant_failed_users:
        issues.append(f"runtime-perm grants failed for users "
                      f"{','.join(grant_failed_users)}")
    if issues:
        return AttemptResult(
            strategy=name, status=AttemptStatus.SUCCESS,
            summary=f"Installed {pkg_resolved}; " + "; ".join(issues) + ".",
            log_lines=tuple(accum), duration_s=time.monotonic() - t0,
        )

    if fan_out:
        summary = (f"Installed {pkg_resolved} on user 0 + screens "
                   f"{','.join(str(u) for u in fan_out)} via pm-disable.")
    else:
        summary = f"Installed {pkg_resolved} for user {seed_user} via pm-disable."
    return AttemptResult(
        strategy=name, status=AttemptStatus.SUCCESS,
        summary=summary,
        log_lines=tuple(accum), duration_s=time.monotonic() - t0,
    )


def _force_uninstall(
    ctx: InstallContext, pkg: str, accum: list[str],
) -> None:
    """Uninstall `pkg` from every Android user before re-installing.

    Best-effort: ignores "not installed" errors. Used when the user
    explicitly opts in to force-reinstall via the install tab checkbox.
    """
    # Try a global uninstall first; if Huawei policy refuses, fall through
    # to the per-user form.
    r = _run_adb(ctx, "shell", "pm", "uninstall", pkg,
                 accum=accum, timeout=60)
    if "Success" in (r.stdout or ""):
        _emit(ctx, f"  ✓ uninstalled {pkg} globally", accum=accum)
        return
    for u in (0, *_live_screen_users(ctx.serial)):
        _run_adb(ctx, "shell", "pm", "uninstall", "--user", str(u), pkg,
                 accum=accum, timeout=30)


# ---- IME enrollment via shell `ime` -------------------------------------

def enable_input_method(
    serial: str, *, ime_id: str, users: Iterable[int],
    log_callback: Callable[[str], None] = lambda _l: None,
) -> dict[int, bool]:
    """Run `ime enable --user N <ime-id>` for every user in `users`.

    `ime_id` is the slash-form `<package>/<service-class>` (the
    component name an IMM stub returns from ``getId()``).

    On Avatr 11 / HarmonyOS 2.x and 4.x the `ime enable` shell command
    works without any reflection (the partner installer relies on it).
    On HarmonySpace 5.0 it has not been verified yet, but the same shell
    surface is exposed by every Android 12 image we've seen.

    Returns ``{user_id: ok}``.
    """
    result: dict[int, bool] = {}
    for u in users:
        r = adb.run("shell", "ime", "enable", "--user", str(u), ime_id,
                    serial=serial, check=False, timeout=15)
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.exit_code == 0 and (
            "now enabled" in out.lower() or "enabled" in out.lower()
        )
        log_callback(f"  ime enable user {u}: "
                     f"{'✓' if ok else '✘'} {out.strip() or '(no output)'}")
        result[u] = ok
    return result


def set_default_input_method(
    serial: str, *, ime_id: str, users: Iterable[int],
    log_callback: Callable[[str], None] = lambda _l: None,
) -> dict[int, bool]:
    """Run `ime set --user N <ime-id>` for every user.

    Sets the IME as the default (active) input method. Independent from
    `enable_input_method` — both are usually called together when the
    user opts in to "set as default keyboard".
    """
    result: dict[int, bool] = {}
    for u in users:
        r = adb.run("shell", "ime", "set", "--user", str(u), ime_id,
                    serial=serial, check=False, timeout=15)
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.exit_code == 0 and (
            "now selected" in out.lower() or "selected" in out.lower()
        )
        log_callback(f"  ime set user {u}: "
                     f"{'✓' if ok else '✘'} {out.strip() or '(no output)'}")
        result[u] = ok
    return result


def list_input_methods(serial: str, *, user: int = 0) -> list[tuple[str, bool]]:
    """Return [(ime_id, enabled)] from ``ime list -s -a --user N``.

    The ``-s`` flag prints just the IDs (component names), ``-a`` lists
    every IME — both enabled and disabled. We then probe ``-s`` (enabled
    only) to mark each one's enabled state.

    Falls back to an empty list if the device's `ime` shell command
    isn't usable (very old Android, or hooked).
    """
    enabled = adb.run("shell", "ime", "list", "-s", "--user", str(user),
                      serial=serial, check=False, timeout=15)
    enabled_ids: set[str] = set()
    for line in (enabled.stdout or "").splitlines():
        line = line.strip()
        if line:
            enabled_ids.add(line)

    everything = adb.run("shell", "ime", "list", "-s", "-a",
                         "--user", str(user),
                         serial=serial, check=False, timeout=15)
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for line in (everything.stdout or "").splitlines():
        line = line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        out.append((line, line in enabled_ids))
    return out


def discover_ime_id(
    serial: str, package: str, *, user: int = 0, timeout: int = 15,
) -> str | None:
    """Find the IME component id for `package` from ``ime list -a``.

    The car keyboard apps typically register one InputMethodService;
    `ime list -a` returns lines like
    ``com.huawei.ohos.inputmethod/com.android.inputmethod.latin.LatinIME``.
    """
    r = adb.run("shell", "ime", "list", "-s", "-a", "--user", str(user),
                serial=serial, check=False, timeout=timeout)
    if r.exit_code != 0:
        return None
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if line.startswith(f"{package}/"):
            return line
    return None


# ---- runtime permission grant via on-device helper jar --------------------

# Per-process dedup of helper-jar pushes. The (serial, md5) we've already
# pushed for. md5 lets us re-push if the resource changes between runs.
_PERM_HELPER_PUSHED: set[tuple[str, str]] = set()


def _ensure_perm_helper_pushed(ctx: InstallContext, accum: list[str]) -> bool:
    """Push hw-perm-grant.jar to /data/local/tmp/ once per (device, jar).

    Returns True if the jar is in place, False if we couldn't deploy it.
    Idempotent: skips the push when the same jar is already known to
    be on the device.
    """
    try:
        from importlib import resources as _resources
    except ImportError:
        _resources = None
    import hashlib

    if _resources is None:
        _emit(ctx, "  ✘ importlib.resources unavailable — can't load helper jar",
              accum=accum)
        return False
    try:
        with _resources.files("ivi_installer.resources").joinpath(
                HDB_PERM_HELPER_RESOURCE).open("rb") as fh:
            jar_bytes = fh.read()
    except (FileNotFoundError, ModuleNotFoundError, AttributeError) as e:
        _emit(ctx, f"  ✘ helper jar resource missing: {e}", accum=accum)
        return False
    digest = hashlib.md5(jar_bytes).hexdigest()
    if (ctx.serial, digest) in _PERM_HELPER_PUSHED:
        return True

    # Drop the jar to a host temp first so adb push can read it. Briefcase
    # bundle resources may live inside a zip, so we can't pass them to
    # adb directly — round-trip through a real tempfile.
    import os as _os
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile(
            prefix="ivi-perm-grant-", suffix=".jar", delete=False
    ) as tmp:
        tmp.write(jar_bytes)
        host_path = tmp.name
    try:
        push = _run_adb(ctx, "push", host_path, HDB_PERM_HELPER_REMOTE,
                        accum=accum, timeout=60)
        if push.exit_code != 0:
            _emit(ctx, f"  ✘ adb push helper failed (exit={push.exit_code})",
                  accum=accum)
            return False
    finally:
        try: _os.unlink(host_path)
        except OSError: pass

    _PERM_HELPER_PUSHED.add((ctx.serial, digest))
    return True


_USER_HEADER_RE = re.compile(r"^    User (\d+):")
_PERM_LINE_RE = re.compile(
    r"\s+(android\.permission\.[A-Z0-9_]+|com\.[a-z0-9_.]+\.permission\.[A-Z0-9_]+):\s*granted=(true|false)"
)


def _ungranted_runtime_perms_for_user(
    dumpsys_text: str, user_id: int,
) -> list[str]:
    """Parse `dumpsys package <pkg>` and return the runtime permissions for
    the given user that are currently NOT granted.

    Logic: walk the per-user `User N:` blocks. Inside each block, look
    for a `runtime permissions:` sub-section. Permission lines look like
    `android.permission.RECORD_AUDIO: granted=false`. We collect those
    where granted=false (and the permission name parses).
    """
    perms: list[str] = []
    in_user = False
    in_runtime = False
    for raw in dumpsys_text.splitlines():
        m_user = _USER_HEADER_RE.match(raw)
        if m_user:
            in_user = (int(m_user.group(1)) == user_id)
            in_runtime = False
            continue
        if not in_user:
            continue
        stripped = raw.strip()
        if stripped.startswith("runtime permissions:"):
            in_runtime = True
            continue
        # The runtime block ends at the next non-indented or sibling-key line.
        if in_runtime:
            if not raw.startswith("        "):
                # exited the indented runtime block
                in_runtime = False
                continue
            m = _PERM_LINE_RE.match(raw)
            if m and m.group(2) == "false":
                perms.append(m.group(1))
    # De-dup while preserving order — dumpsys can list a perm twice.
    seen = set()
    unique = []
    for p in perms:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def _grant_runtime_perms(
    ctx: InstallContext, pkg: str, users: list[int], accum: list[str],
) -> dict[int, tuple[int, int]]:
    """Grant every ungranted runtime permission for `pkg` in each user.

    Returns {user_id: (granted_count, failed_count)}. Skips users where
    the package isn't installed or has no ungranted runtime perms.
    """
    if not users:
        return {}
    if not _ensure_perm_helper_pushed(ctx, accum):
        return {}

    dump = _run_adb(ctx, "shell", "dumpsys", "package", pkg,
                    accum=accum, timeout=30)
    if dump.exit_code != 0 or not dump.stdout:
        _emit(ctx, f"  ⚠ couldn't read dumpsys for {pkg}; skipping grants",
              accum=accum)
        return {}

    summary: dict[int, tuple[int, int]] = {}
    for u in users:
        perms = _ungranted_runtime_perms_for_user(dump.stdout, u)
        if not perms:
            continue
        _emit(ctx, f"  user {u}: granting {len(perms)} runtime perm(s) → "
                   f"{', '.join(p.rsplit('.', 1)[-1] for p in perms)}",
              accum=accum)
        # One app_process invocation per user — helper takes
        # <pkg> <userId> <perm1> [perm2 ...].
        cmd = (
            f"CLASSPATH={HDB_PERM_HELPER_REMOTE} "
            f"app_process /system/bin HwPermGrant {pkg} {u} "
            + " ".join(perms)
        )
        r = _run_adb(ctx, "shell", cmd, accum=accum, timeout=60)
        ok = fail = 0
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("granted: "):
                ok += 1
            elif line.startswith("failed "):
                fail += 1
                _emit(ctx, f"    ✘ {line}", accum=accum)
        summary[u] = (ok, fail)
    return summary


# ---- registry --------------------------------------------------------------

@dataclass(frozen=True)
class StrategyDescriptor:
    name: str
    label: str                              # human-readable name for the UI
    description: str
    run: Callable[[InstallContext], AttemptResult]
    applies: Callable[[InstallContext], bool] = lambda _ctx: True
    # Visible pipeline stages for the install tab. Each entry is a
    # ``(code_index, label)`` pair: ``code_index`` is the index the
    # strategy will emit via :class:`StageEvent`, ``label`` is what
    # the UI prints on the Pipeline row. Strategies may emit
    # additional code-indexes that aren't in this tuple (e.g. mirror)
    # — the UI ignores them. Empty tuple = strategy isn't shown in
    # the install pipeline.
    stages: tuple[tuple[int, str], ...] = ()

    def stages_for(
        self,
        target_users: tuple[int, ...] | None = None,
        *, target_user: int | None = None,
    ) -> tuple[tuple[str, int], ...]:
        """Return (label, code_index) pairs for the visible pipeline.

        ``target_users`` / ``target_user`` are accepted for backward
        compatibility but ignored — every strategy now exposes a
        fixed visible stage list. The mirror-to-passenger-screens
        step is intentionally absent from the visible list (its code
        still runs as part of the install) so the pipeline row count
        stays stable when toggling screens or strategies.
        """
        return tuple((label, code_idx) for code_idx, label in self.stages)


_STRATEGIES: tuple[StrategyDescriptor, ...] = (
    StrategyDescriptor(
        name="pm_disable_install",
        label="PM-disable install (primary; lighter than HDB)",
        description=(
            "Temporarily disable com.android.packageinstaller, run a stock "
            "`pm install -r -d -g -i <huawei-installer>` (the Huawei "
            "installer pkg is auto-detected: appinstaller.car for Deepal, "
            "appmarket.vehicle for Avatr), then re-enable PackageInstaller. "
            "Adopted from the AvatrAppInstaller competitor — works without "
            "the on-device broker daemon. Use this first; it's also faster."
        ),
        run=_strategy_pm_disable_install,
        applies=lambda ctx: len(ctx.apk_paths) == 1,
        # Code emits 0..5; index 4 (mirror) is intentionally not
        # exposed in the visible pipeline so toggling Simple/Complex
        # or screen checkboxes never reshapes the row list.
        stages=(
            (0, "Stage package on device"),
            (1, "Adjust installer policy"),
            (2, "Install package"),
            (3, "Restore installer policy"),
            (5, "Grant runtime permissions"),
        ),
    ),
    StrategyDescriptor(
        name="hdb_broker_install",
        label="HDB broker install (fallback; HarmonySpace 5.0 path)",
        description=(
            "Route through the on-device AvatrHdbBroker process "
            "(tcp:38787). The broker creates an HDB-flagged "
            "PackageInstaller session that Huawei's HwInstallPolicy hook "
            "trusts, so the 'external sources not allowed' dialog never "
            "fires. After the install lands in the seed user, fan out "
            "via `pm install-existing`. Single .apk only — the multi-APK "
            "bridge command hardcodes the Avatr-specific installer package."
        ),
        run=_strategy_hdb_broker_install,
        applies=lambda ctx: len(ctx.apk_paths) == 1,
        # Code emits 0..4; index 3 (mirror) is intentionally not
        # exposed in the visible pipeline.
        stages=(
            (0, "Verify on-device helper"),
            (1, "Stage package on device"),
            (2, "Authorize install session"),
            (4, "Grant runtime permissions"),
        ),
    ),
    StrategyDescriptor(
        name="diagnose",
        label="Diagnose (no install — full system dump)",
        description=(
            "Run a battery of read-only probes on the device — current "
            "user, process list, binder services, every Settings.Secure / "
            "Global / System key, all REQUEST_INSTALL_PACKAGES appops, "
            "getprop, root probes — and save the report next to the app "
            "logs. Diagnostic only — no install attempted."
        ),
        run=_strategy_diagnose,
        applies=lambda _ctx: False,         # never auto-runs
    ),
    StrategyDescriptor(
        name="app_process_helper",
        label="(Re)deploy AvatrHdbBroker daemon",
        description=(
            "Push the bundled avatr-hdb-broker.jar to /data/local/tmp/ "
            "(idempotent via md5) and start `app_process AvatrHdbBroker "
            "38787` as a detached daemon. Use this after a car reboot, or "
            "if the bypass health-check reports broker not reachable. "
            "Doesn't install anything."
        ),
        run=_strategy_app_process_helper,
        applies=lambda _ctx: False,         # never auto-runs in cascade
    ),
)

_STRATEGY_INDEX = {s.name: s for s in _STRATEGIES}

# When the user picks one of these as their primary install strategy, the
# cascade is reordered so it runs first and the other becomes the
# fallback. Other strategies (diagnose / app_process_helper) keep their
# `applies=False` and never auto-run.
PRIMARY_STRATEGIES: tuple[str, ...] = (
    "pm_disable_install",
    "hdb_broker_install",
)


def list_strategies() -> tuple[StrategyDescriptor, ...]:
    return _STRATEGIES


def get_strategy(name: str) -> StrategyDescriptor:
    try:
        return _STRATEGY_INDEX[name]
    except KeyError as e:
        raise KeyError(f"unknown install strategy: {name!r}") from e


def cascade_order(primary: str | None = None) -> tuple[StrategyDescriptor, ...]:
    """Return the strategy registry with `primary` moved to the front.

    `primary` defaults to "pm_disable_install" — the lighter,
    competitor-derived path that doesn't need the on-device broker
    daemon. The HwSAPT-proven `hdb_broker_install` runs as the fallback.

    Strategies whose ``applies`` returns False for every ctx (diagnose,
    app_process_helper) keep their tail position; only the install-path
    strategies get reordered.
    """
    primary = primary or "pm_disable_install"
    if primary not in PRIMARY_STRATEGIES:
        raise ValueError(f"unknown primary strategy: {primary!r}")
    head: list[StrategyDescriptor] = []
    tail: list[StrategyDescriptor] = []
    for s in _STRATEGIES:
        if s.name == primary:
            head.insert(0, s)
        elif s.name in PRIMARY_STRATEGIES:
            head.append(s)
        else:
            tail.append(s)
    return tuple(head + tail)


# ---- cascade orchestrator -------------------------------------------------

def run_cascade(
    ctx: InstallContext, *, primary: str | None = None,
) -> CascadedInstallResult:
    """Try each applicable strategy in order until one succeeds or until a
    terminal failure short-circuits the chain.

    `primary` selects the head of the cascade — see ``cascade_order``."""
    attempts: list[AttemptResult] = []
    for descriptor in cascade_order(primary):
        if not descriptor.applies(ctx):
            attempts.append(AttemptResult(
                strategy=descriptor.name, status=AttemptStatus.SKIPPED,
                summary="Not applicable to this input/probe.",
            ))
            continue
        ctx.log(f"── trying strategy: {descriptor.label} ──")
        result = _safe_run(descriptor, ctx)
        attempts.append(result)
        if result.status is AttemptStatus.SUCCESS:
            return CascadedInstallResult(
                success=True, package=ctx.package, attempts=tuple(attempts),
            )
        if result.status is AttemptStatus.TERMINAL:
            ctx.log(
                f"⨯ terminal failure ({result.failure_code}); "
                f"retrying with another strategy is pointless."
            )
            return CascadedInstallResult(
                success=False, package=ctx.package, attempts=tuple(attempts),
            )
        ctx.log(f"… {descriptor.label} did not succeed: {result.summary}")
    return CascadedInstallResult(
        success=False, package=ctx.package, attempts=tuple(attempts),
    )


def run_strategy(name: str, ctx: InstallContext) -> CascadedInstallResult:
    """Run exactly one strategy by name, no fallback.

    The strategy's `applies` predicate is only a cascade filter — when
    the user picks a strategy by name (e.g. via the strategy tab), we
    invoke its body unconditionally. The body itself may still return
    SKIPPED if the inputs don't make sense for it.
    """
    descriptor = get_strategy(name)
    ctx.log(f"── running strategy: {descriptor.label} ──")
    attempt = _safe_run(descriptor, ctx)
    return CascadedInstallResult(
        success=attempt.status is AttemptStatus.SUCCESS,
        package=ctx.package,
        attempts=(attempt,),
    )


def _safe_run(descriptor: StrategyDescriptor, ctx: InstallContext) -> AttemptResult:
    """Run a strategy and convert any uncaught exception into a FAILED attempt."""
    t0 = time.monotonic()
    try:
        return descriptor.run(ctx)
    except Exception as e:
        log.exception("strategy %s raised", descriptor.name)
        return AttemptResult(
            strategy=descriptor.name, status=AttemptStatus.FAILED,
            summary=f"raised {type(e).__name__}: {e}",
            log_lines=(f"unhandled exception: {type(e).__name__}: {e}",),
            duration_s=time.monotonic() - t0,
        )


# ---- input adapter --------------------------------------------------------

def build_context_from_path(
    file_path: Path | str,
    *,
    serial: str,
    grant_runtime: bool = True,
    target_user: int | None = None,
    log_callback: Callable[[str], None] = lambda _line: None,
    device_info: DeviceInfo | None = None,
    preferred_installer: str | None = None,
    force_reinstall: bool = False,
) -> InstallContext:
    """Resolve a single .apk into an :class:`InstallContext`.

    Multi-APK / .xapk inputs are rejected — the Huawei multi-APK bridge
    command hardcodes the Avatr-only installer pkg, so neither bypass
    can land splits on Deepal/Avatr. Callers should pre-filter at the UI
    boundary; this raises :class:`ValueError` as a defensive backstop.
    """
    p = Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(p)

    if p.suffix.lower() != ".apk":
        raise ValueError(
            f"unsupported input extension: {p.suffix} (expected .apk)"
        )

    return InstallContext(
        serial=serial,
        apk_paths=[p],
        package=None,
        target_user=target_user,
        grant_runtime=grant_runtime,
        preferred_installer=preferred_installer,
        device_info=device_info,
        force_reinstall=force_reinstall,
        log=log_callback,
    )
