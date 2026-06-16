"""QThread workers wrapping the synchronous adb/installer calls.

The Qt main loop must never block on a subprocess. Every long-running
adb operation runs on its own QThread and reports back via signals.

We use the canonical QObject-on-QThread pattern (object moved with
moveToThread). Each worker owns its own thread; the UI keeps a strong
reference to both until `finished` fires.
"""
from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal

from .. import adb, device_info, devices, diag, installer
from ..sources import appgallery
from .. import timezone as tz_module
from ..installer import (
    AttemptResult,
    CascadedInstallResult,
    StageEvent,
)

log = logging.getLogger(__name__)


class _WorkerBase(QObject):
    """Common signals shared by every worker.

    Subclasses connect their main entry point to `start()` so the host
    thread can fire it from `QThread.started`.
    """
    log_line = Signal(str)
    error = Signal(str)
    finished = Signal()


class DeviceProbeWorker(_WorkerBase):
    """Run a single `adb devices -l` and emit the parsed list.

    `result` carries `list[devices.Device]`. Empty list means nothing
    plugged in (UI shows the "no device" status).
    """
    result = Signal(list)

    def run(self) -> None:
        try:
            devs = devices.list_devices()
            self.result.emit(devs)
        except FileNotFoundError as e:
            # adb missing — UI bootstrap will offer to download.
            self.error.emit(str(e))
        except Exception as e:
            log.exception("device probe failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class RootCheckWorker(_WorkerBase):
    """`adb -s <serial> shell whoami` → bool.

    Cheap (~100 ms) but blocking; we still run it off the UI thread for
    consistency with everything else that talks to adb.
    """
    result = Signal(str, bool)   # serial, is_root

    def __init__(self, serial: str):
        super().__init__()
        self.serial = serial

    def run(self) -> None:
        try:
            user = adb.whoami(self.serial)
            self.result.emit(self.serial, user == "root")
        except Exception as e:
            log.exception("root check failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class InstallWorker(_WorkerBase):
    """Run an install (primary + fallback, or one strategy by name).

    Emits `log_line` for every step the strategy pipeline produces, plus
    a final `attempt` signal per strategy so the UI can paint a per-tab
    summary. `result` carries a CascadedInstallResult.

    Pass `strategy=None` (or "auto") to run the primary strategy and
    fall back to the other one if it fails, or pass the name of a
    specific strategy from `installer.list_strategies()` to run only it.
    """
    result = Signal(object)        # CascadedInstallResult
    attempt = Signal(object)       # AttemptResult — one per strategy run
    stage = Signal(object)         # StageEvent — fine-grained pipeline progress

    def __init__(self, file_path: Path, *, serial: str,
                 grant_runtime: bool = True,
                 target_user: int | None = None,
                 target_users: tuple[int, ...] | None = None,
                 preferred_installer: str | None = None,
                 strategy: str | None = None,
                 primary_strategy: str | None = None,
                 force_reinstall: bool = False):
        super().__init__()
        self.file_path = file_path
        self.serial = serial
        self.grant_runtime = grant_runtime
        self.target_user = target_user
        self.target_users = target_users
        self.preferred_installer = preferred_installer
        self.strategy = strategy or "auto"
        self.primary_strategy = primary_strategy
        self.force_reinstall = force_reinstall

    def run(self) -> None:
        try:
            self.log_line.emit(
                f"→ Installing {self.file_path.name} via "
                f"{self.primary_strategy or self.strategy}"
                + (" (with fallback)" if self.strategy == "auto" else "")
            )
            common_kwargs = dict(
                serial=self.serial,
                grant_runtime=self.grant_runtime,
                target_user=self.target_user,
                target_users=self.target_users,
                preferred_installer=self.preferred_installer,
                force_reinstall=self.force_reinstall,
                log_callback=self.log_line.emit,
                stage_callback=self.stage.emit,
            )
            if self.strategy == "auto":
                cascaded = installer.install_cascade(
                    self.file_path,
                    primary_strategy=self.primary_strategy,
                    **common_kwargs,
                )
            else:
                cascaded = installer.install_with_strategy(
                    self.strategy, self.file_path, **common_kwargs,
                )
            for attempt in cascaded.attempts:
                self.attempt.emit(attempt)
            self.result.emit(cascaded)
        except Exception as e:
            log.exception("install failed")
            self.log_line.emit(traceback.format_exc())
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class ThirdPartyPackagesWorker(_WorkerBase):
    """Union of third-party packages installed across all multimedia
    screen users on the device.

    Why per-user, not a single global call: Huawei automotive heads run
    user 0 as HEADLESS — no screens, no app bindings. User-installed
    apps live under screen users (typically 10/11/12/13). A plain
    ``pm list packages -3`` runs as the shell user (mapped to user 0),
    so it returns roughly nothing on these devices regardless of what
    you've installed. Probing each running non-zero user and unioning
    the lists is the only way to see the apps you actually installed.

    Used by the Tools tab to populate the 'grant runtime perms' package
    picker.
    """
    result = Signal(str, list)   # serial, list[str]

    def __init__(self, serial: str):
        super().__init__()
        self.serial = serial

    def run(self) -> None:
        try:
            from .. import strategies
            # Same probe-with-fallback logic the install strategies use:
            # any running, non-system user id. Falls back to the canonical
            # Deepal/Avatr screen-user set when the probe came back empty.
            target_users = strategies._live_screen_users(self.serial)

            packages: set[str] = set()
            errors: list[str] = []
            for uid in target_users:
                r = adb.run("shell", "pm", "list", "packages", "-3",
                            "--user", str(uid),
                            serial=self.serial, check=False, timeout=20)
                if r.exit_code != 0:
                    errors.append(f"user {uid}: {(r.stderr or '').strip()}")
                    continue
                for line in (r.stdout or "").splitlines():
                    line = line.strip()
                    if line.startswith("package:"):
                        packages.add(line[len("package:"):])

            if not packages and errors:
                self.error.emit("pm list packages -3 failed for every "
                                f"user ({'; '.join(errors)})")
                self.result.emit(self.serial, [])
                return
            self.result.emit(self.serial, sorted(packages))
        except Exception as e:
            log.exception("pm list packages failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class GrantRuntimePermsWorker(_WorkerBase):
    """Grant every ungranted runtime permission for `package` across all
    multimedia-screen users (and user 0).

    Wraps `strategies._grant_runtime_perms`, which internally pushes the
    helper jar (idempotent) and runs `app_process /system/bin HwPermGrant`
    once per user with the list of perms parsed from `dumpsys package`.
    """
    result = Signal(str, str, dict)   # serial, package, summary {user_id: (ok, fail)}

    def __init__(self, serial: str, package: str):
        super().__init__()
        self.serial = serial
        self.package = package

    def run(self) -> None:
        try:
            from .. import strategies
            ctx = strategies.InstallContext(
                serial=self.serial,
                apk_paths=[],
                log=self.log_line.emit,
            )
            users = strategies._live_screen_users(self.serial) + [0]
            summary = strategies._grant_runtime_perms(
                ctx, self.package, users, accum=[])
            self.result.emit(self.serial, self.package, summary)
        except Exception as e:
            log.exception("grant runtime perms failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class BrokerHealthWorker(_WorkerBase):
    """Read the on-device broker's status (PID, uptime, port, jar md5).

    Wraps `strategies.collect_broker_status` — see that function for the
    detailed shape. The `result` signal carries the immutable
    BrokerStatus snapshot back to the UI.
    """
    result = Signal(str, object)   # serial, BrokerStatus

    def __init__(self, serial: str):
        super().__init__()
        self.serial = serial

    def run(self) -> None:
        try:
            from .. import strategies
            status = strategies.collect_broker_status(self.serial)
            self.result.emit(self.serial, status)
        except Exception as e:
            log.exception("broker health probe failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class BrokerRedeployWorker(_WorkerBase):
    """Push the bundled broker jar (idempotent) and (re)start the daemon.

    Always re-runs the launch command, even when PING currently
    succeeds, so the user has a reliable kick-it affordance from the
    Tools tab.
    """
    result = Signal(str, bool)     # serial, ok

    def __init__(self, serial: str):
        super().__init__()
        self.serial = serial

    def run(self) -> None:
        try:
            from .. import strategies
            ok = strategies.redeploy_broker(self.serial, self.log_line.emit)
            self.result.emit(self.serial, ok)
        except Exception as e:
            log.exception("broker redeploy failed")
            self.log_line.emit(traceback.format_exc())
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class UsersListWorker(_WorkerBase):
    """`pm list users` + `dumpsys display` + `dumpsys window displays`.

    Returns both the user list and the per-display→user mapping so the
    install tab can resolve Driver/Passenger/Rear from physical display
    names (most reliable on Huawei IVI) and fall back to user-name
    heuristics when display probing fails.

    Used by the install tab to populate the Target user dropdown and
    the per-screen checkbox grid. Empty lists on failure — the UI
    falls back to static defaults.
    """
    # serial, list[AndroidUser], list[DisplayInfo]
    result = Signal(str, list, list)

    def __init__(self, serial: str):
        super().__init__()
        self.serial = serial

    def run(self) -> None:
        try:
            users = devices.list_android_users(self.serial)
            try:
                displays = devices.list_displays(self.serial)
            except Exception:
                # Display probe is best-effort: a failure must not
                # break the user-list fetch. Heuristic fallback
                # still works on the user names we already have.
                log.exception("displays list failed (non-fatal)")
                displays = []
            self.result.emit(self.serial, users, displays)
        except Exception as e:
            log.exception("users list failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class TimezoneReadWorker(_WorkerBase):
    """Read the device's current IANA timezone."""
    result = Signal(str, str)  # serial, tz

    def __init__(self, serial: str):
        super().__init__()
        self.serial = serial

    def run(self) -> None:
        try:
            tz = tz_module.get_current(self.serial)
            self.result.emit(self.serial, tz)
        except Exception as e:
            log.exception("timezone read failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class TimezoneWriteWorker(_WorkerBase):
    """Apply a new timezone, then re-read to verify the device picked it up."""
    result = Signal(bool, str, str)  # ok, applied_tz, verified_tz

    def __init__(self, serial: str, new_tz: str):
        super().__init__()
        self.serial = serial
        self.new_tz = new_tz

    def run(self) -> None:
        try:
            for line in tz_module.set_timezone(self.serial, self.new_tz):
                self.log_line.emit(line)
            verified = tz_module.get_current(self.serial)
            self.result.emit(verified == self.new_tz, self.new_tz, verified)
        except Exception as e:
            log.exception("timezone write failed")
            self.log_line.emit(traceback.format_exc())
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class DeviceInfoWorker(_WorkerBase):
    """Run all the read-only probes from `device_info.collect`."""
    result = Signal(str, list)  # serial, [(section, [(label, value), ...]), ...]

    def __init__(self, serial: str):
        super().__init__()
        self.serial = serial

    def run(self) -> None:
        try:
            sections = device_info.collect(self.serial)
            self.result.emit(self.serial, sections)
        except Exception as e:
            log.exception("device_info collect failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class AppGalleryDownloadWorker(_WorkerBase):
    """Download an APK from Huawei AppGallery by app id.

    Pastes from the user are normalised by ``appgallery.parse_app_id``;
    pass either the bare id ("C12345") or the URL form. Streams progress
    via ``progress(bytes, total)``.
    """
    progress = Signal(int, int)         # bytes_so_far, total_bytes (0 = unknown)
    result = Signal(str)                # local path of saved APK

    def __init__(self, raw_input: str, out_dir: Path):
        super().__init__()
        self.raw_input = raw_input
        self.out_dir = out_dir

    def run(self) -> None:
        try:
            app_id = appgallery.parse_app_id(self.raw_input)
            if not app_id:
                self.error.emit(
                    "Couldn't extract a 'C12345' app id from your input. "
                    "Paste an AppGallery link or the bare id."
                )
                return
            self.log_line.emit(f"Downloading {app_id} from AppGallery…")
            path = appgallery.download(
                app_id, out_dir=self.out_dir,
                progress=self.progress.emit,
            )
            self.result.emit(str(path))
        except Exception as e:
            log.exception("appgallery download failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class CatalogFetchWorker(_WorkerBase):
    """Pull the AppGallery catalog and rebuild the SQLite store.

    The AppGallery JSON cache is 24 h on disk; ``force=True`` bypasses
    it. The resulting catalog (extras overlaid on the live AppGallery
    pull) is written to ``catalog_store.default_db_path()`` inside one
    transaction (WAL means the previous reader keeps working until
    COMMIT). The ``result`` signal carries the SQLite path so the UI
    thread can open it read-only.
    """
    result = Signal(str)               # path to the SQLite store

    def __init__(self, *, force: bool = False):
        super().__init__()
        self.force = force

    def run(self) -> None:
        try:
            from .. import catalog as _catalog
            from .. import catalog_store as _store_mod
            from ..sources import appgallery_index as _ag
            self.log_line.emit(
                "store: refreshing catalog…" if self.force
                else "store: loading catalog…")

            extras_apps = list(_catalog.load_extras().apps)

            index = _ag.fetch_index(
                force=self.force, log_callback=self.log_line.emit)
            ag_apps = _catalog.from_appgallery_index(index)
            self.log_line.emit(
                f"store: parsed {len(ag_apps)} AppGallery entries")

            # AppGallery doesn't expose a global timestamp; use
            # ``fetched_at`` as a coarse "as of" marker for the
            # "catalog vYYYY-MM-DD" label.
            generated_at = ""
            fetched = index.get("fetched_at") or 0
            if fetched:
                from datetime import datetime, timezone
                generated_at = (
                    datetime.fromtimestamp(int(fetched), tz=timezone.utc)
                    .isoformat().replace("+00:00", "Z"))

            # Extras are the curated overlay — they win on id collisions
            # so a hand-typed Russian description / tested flag wins
            # over what AppGallery shipped.
            merged = _store_mod.merge_apps(ag_apps, extras_apps)
            db_path = _store_mod.default_db_path()
            self.log_line.emit(f"store: building SQLite at {db_path}")
            _store_mod.CatalogStore.build_to_path(
                db_path,
                apps=merged,
                generated_at=generated_at,
            )
            self.log_line.emit(
                f"store: catalog ready ({len(merged)} entries)")
            self.result.emit(str(db_path))
        except Exception as e:
            log.exception("catalog fetch failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class HotSearchKeywordsWorker(_WorkerBase):
    """One-shot fetch of AppGallery's "popular searches" list.

    Hits the unauthenticated ``/edge/index/getnewhotsearchlist``
    endpoint so it doesn't pay the JWT bootstrap cost. Used by the
    Store tab's search dropdown the first time the user clicks into
    the input.
    """
    result = Signal(list)   # list[str]

    def run(self) -> None:
        try:
            from ..sources import appgallery_index as _ag
            keywords = _ag.get_hot_search_list()
            self.result.emit(list(keywords))
        except Exception as e:
            log.exception("hot keywords fetch failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class CompleteSearchWordWorker(_WorkerBase):
    """Type-ahead search completion via /edge/index/completeSearchWord.

    Returns the keyword (echoed back from the server), the list of
    suggested keywords, and (when AppGallery has a confident match)
    the raw "top app" item dict so the UI can show a clickable card.
    """
    # keyword, suggestions, top_app_or_none
    result = Signal(str, list, object)

    def __init__(self, keyword: str):
        super().__init__()
        self.keyword = keyword

    def run(self) -> None:
        try:
            from ..sources import appgallery_index as _ag
            data = _ag.complete_search_word(self.keyword)
            self.result.emit(
                str(data.get("keyword") or self.keyword),
                list(data.get("suggestions") or []),
                data.get("top_app"),
            )
        except Exception as e:
            log.exception("complete_search_word failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class StoreInstallWorker(_WorkerBase):
    """Resolve a catalog entry → run the existing install pipeline.

    Tries each ``entry.sources`` slot in order. The first one that
    produces a local APK wins; the install is then handed off to
    ``installer.install_cascade``. Source-resolution and install phases
    are surfaced separately so the UI can show "Resolving…" /
    "Downloading NN%" / "Installing…" per the design states.
    """

    # phases: 'resolving' | 'downloading' | 'installing' | 'success' | 'failed'
    phase = Signal(str)
    progress = Signal(int, int)            # bytes_so_far, total_bytes (0 = unknown)
    attempt = Signal(object)               # AttemptResult, one per strategy
    result = Signal(object)                # CascadedInstallResult

    def __init__(
        self,
        entry: object,                      # ivi_installer.catalog.CatalogEntry
        *,
        serial: str,
        primary_strategy: str | None = None,
        target_user: int | None = None,
        target_users: tuple[int, ...] | None = None,
        force_reinstall: bool = False,
        out_dir: Path | None = None,
    ):
        super().__init__()
        self.entry = entry
        self.serial = serial
        self.primary_strategy = primary_strategy
        self.target_user = target_user
        self.target_users = target_users
        self.force_reinstall = force_reinstall
        self.out_dir = out_dir or (Path.home() / "Downloads" / "ivi-installer")

    def run(self) -> None:
        from .. import sources as _sources
        try:
            entry = self.entry
            apk_path: Path | None = None
            last_err: Exception | None = None
            for idx, source in enumerate(entry.sources):
                kind = source.get("kind", "?")
                self.log_line.emit(
                    f"→ source {idx + 1}/{len(entry.sources)}: {kind}")
                self.phase.emit("resolving")
                try:
                    self.phase.emit("downloading")
                    apk_path = _sources.resolve(
                        source,
                        out_dir=self.out_dir,
                        progress=self.progress.emit,
                        log_callback=self.log_line.emit,
                    )
                    break
                except Exception as e:  # pylint: disable=broad-except
                    last_err = e
                    log.exception("source %s failed for %s", kind, entry.id)
                    self.log_line.emit(f"  ✘ {kind}: {e}")
            if apk_path is None:
                self.phase.emit("failed")
                self.error.emit(
                    f"all sources failed: {last_err}" if last_err
                    else "no sources configured")
                return

            self.phase.emit("installing")
            self.log_line.emit(
                f"→ installing {apk_path.name} via "
                f"{self.primary_strategy or 'default'}")
            cascaded = installer.install_cascade(
                apk_path,
                serial=self.serial,
                grant_runtime=True,
                target_user=self.target_user,
                target_users=self.target_users,
                preferred_installer=None,
                primary_strategy=self.primary_strategy,
                force_reinstall=self.force_reinstall,
                log_callback=self.log_line.emit,
            )
            for attempt in cascaded.attempts:
                self.attempt.emit(attempt)
            self.phase.emit("success" if cascaded.success else "failed")
            self.result.emit(cascaded)
        except Exception as e:
            log.exception("store install failed")
            self.log_line.emit(traceback.format_exc())
            self.phase.emit("failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class IMEEnableWorker(_WorkerBase):
    """Enable + (optionally) set-default an IME via shell `ime` commands.

    Wraps ``strategies.enable_input_method`` /
    ``strategies.set_default_input_method``. Returns a per-user summary
    so the UI can paint mixed success/failure outcomes.
    """
    result = Signal(str, str, dict, dict)  # serial, ime_id, enable_summary, set_default_summary

    def __init__(self, serial: str, *, ime_id: str,
                 users: list[int], set_as_default: bool = False):
        super().__init__()
        self.serial = serial
        self.ime_id = ime_id
        self.users = users
        self.set_as_default = set_as_default

    def run(self) -> None:
        try:
            from .. import strategies
            self.log_line.emit(
                f"→ Enabling IME {self.ime_id} for users {self.users} "
                f"(set as default: {self.set_as_default})"
            )
            enable = strategies.enable_input_method(
                self.serial, ime_id=self.ime_id, users=self.users,
                log_callback=self.log_line.emit,
            )
            default: dict[int, bool] = {}
            if self.set_as_default:
                default = strategies.set_default_input_method(
                    self.serial, ime_id=self.ime_id, users=self.users,
                    log_callback=self.log_line.emit,
                )
            self.result.emit(self.serial, self.ime_id, enable, default)
        except Exception as e:
            log.exception("ime enable failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class EnsureAdbWorker(_WorkerBase):
    """Download platform-tools on first launch when adb is missing.

    Reports progress via `progress(percent, status)`.
    """
    progress = Signal(int, str)
    result = Signal(str)            # absolute path to adb

    def run(self) -> None:
        try:
            path = adb.ensure_adb(progress=self._on_progress)
            self.result.emit(str(path))
        except Exception as e:
            log.exception("ensure_adb failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()

    def _on_progress(self, pct: int, status: str) -> None:
        self.progress.emit(pct, status)


class EnableAdbWorker(_WorkerBase):
    """Run `diag.enable_adb` / `diag.disable_adb` off the UI thread.

    Bridges the synchronous DoIP/UDS sequence to Qt: `stage` fires for
    each pipeline transition, `log_line` for free-form status text, and
    `result` for the final outcome. `action` selects between the
    startRoutine ("enable") and stopRoutine ("disable") variants.
    """
    stage = Signal(int, str, str)              # stage_idx, state, hint
    result = Signal(bool, str)                 # ok, message

    def __init__(self, doip_gateway: str, tls_gateway: str,
                 action: str = "enable"):
        super().__init__()
        self.doip_gateway = doip_gateway
        self.tls_gateway = tls_gateway
        self.action = action

    def run(self) -> None:
        try:
            fn = diag.enable_adb if self.action == "enable" else diag.disable_adb
            res = fn(
                doip_gateway=self.doip_gateway,
                tls_gateway=self.tls_gateway,
                stage_cb=lambda i, s, h: self.stage.emit(i, s, h),
                log_cb=self.log_line.emit,
            )
            self.result.emit(res.ok, res.msg)
        except Exception as e:
            log.exception("%s_adb crashed", self.action)
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


class DiscoverGatewaysWorker(_WorkerBase):
    """UDP-broadcast DoIP VehicleIdentificationRequest. Returns a list
    of `diag.Gateway` (ip, vin, logical_addr).
    """
    result = Signal(list)

    def __init__(self, timeout: float = 2.0):
        super().__init__()
        self.timeout = timeout

    def run(self) -> None:
        try:
            gws = diag.discover_gateways(timeout=self.timeout)
            self.result.emit(list(gws))
        except Exception as e:
            log.exception("discover_gateways failed")
            self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.finished.emit()


# ---- helper that wires up worker + thread ----

def run_in_thread(worker: QObject) -> QThread:
    """Move `worker` to a new QThread, start it, and return the thread.

    The caller is responsible for:
      * keeping strong references to both `worker` and the returned
        QThread until the work is done (or until the parent QObject is
        destroyed),
      * calling deleteLater on both in a `finished` slot if appropriate.

    This intentionally does *not* wire up auto-deletion: doing so races
    with pytest-qt's signal listeners and produces hard crashes when the
    worker is GC'd before qtbot has had a chance to see the last signal.
    Convention: `worker` exposes a `run` method connected to
    `thread.started` and a `finished` signal that triggers `thread.quit`.
    """
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)         # type: ignore[attr-defined]
    if hasattr(worker, "finished"):
        worker.finished.connect(thread.quit)   # type: ignore[attr-defined]
    thread.start()
    return thread
