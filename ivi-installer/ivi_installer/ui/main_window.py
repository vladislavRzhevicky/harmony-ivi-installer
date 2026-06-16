"""Main PySide6 window for IVI Installer (redesign).

Visual layout follows the handoff in ``design/handoff/``:

    DeviceStrip                                  (status_widget — top, 72)
    QTabWidget [Install APK · Tools · Timezone · Device info · Keyboards]
       └── tab content area
    LogPane                                       (always-on, ~50% of body)
    QStatusBar                                    (transient toasts)

The Install tab is a two-column grid: left = APK input → install-on
radios → primary CTA; right = pipeline + grant matrix + success banner.

A handful of non-design widgets are kept so the existing backend keeps
working and the test suite still passes:

* ``appgallery_input``, ``appgallery_button``, ``appgallery_progress``
  — URL-download for an APK by Huawei AppGallery id. Surfaced as the
  "From URL" tab inside the source-card (``source_stack``). A
  ``Ctrl+Shift+D`` shortcut still jumps straight to that tab and
  focuses the input.
* ``strat_pmdisable_radio`` / ``strat_hdb_radio`` — primary install
  strategy. Hidden; setting persists via ``settings.set``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from .. import adb
from .. import catalog as catalog_module
from .. import device_info as device_info_module
from .. import diag
from .. import logging_setup
from .. import settings
from ..devices import (
    DeviceCapabilities,
    DeviceInfo,
    ScreenCategory,
    categorize_screens,
)
from .. import installer
from ..installer import (
    AttemptResult,
    AttemptStatus,
    CascadedInstallResult,
)
from . import workers
from .device_status import DevicePollerWorker, DeviceStatus, DeviceStatusWidget
from .store_tab import (
    PHASE_DOWNLOADING,
    PHASE_FAILED,
    PHASE_IDLE,
    PHASE_INSTALLING,
    PHASE_RESOLVING,
    PHASE_SUCCESS,
    StoreTab,
)
from .theme import TOKENS, detect_os, mono_family
from .widgets import (
    ApkCard,
    DropZone,
    GrantMatrix,
    LogPane,
    MacSegmentedTabBar,
    Pipeline,
    ScreensDiagram,
    SuccessBanner,
)

log = logging.getLogger(__name__)

DEVICE_POLL_MS = 2000
TOAST_MS = 5000
APK_FILTER = "APK (*.apk);;All files (*)"
SETTING_LAST_TZ = "last_used_timezone"
SETTING_PRIMARY_STRATEGY = "primary_install_strategy"
DEFAULT_PRIMARY_STRATEGY = "pm_disable_install"
SETTING_SELECTED_SCREENS = "install_target_screens"
DEFAULT_SELECTED_SCREENS = ("driver", "passenger", "rear")
SETTING_DOIP_GATEWAY = "diag_doip_gateway"
SETTING_TLS_GATEWAY = "diag_tls_gateway"

_APPGALLERY_ID_RE = re.compile(r"^C\d+$", re.IGNORECASE)


def _looks_like_appgallery_id(stem: str) -> bool:
    """`C100315379`-style AppGallery ids are useless as a display name —
    we'd rather fall back to a package-derived heuristic when the file
    came from an URL paste. Used by `_set_apk_file`."""
    return bool(_APPGALLERY_ID_RE.match(stem.strip()))


class _ForbiddenCursorOnDisabledTab(QObject):
    """Hover filter for the native ``QTabBar``. A disabled tab is not
    a separate widget, so per-tab cursors aren't a thing; we have to
    recompute the cursor on every MouseMove using ``tabAt(pos)`` and
    swap it to ``ForbiddenCursor`` whenever the cursor is over a tab
    that the tab widget has marked disabled."""

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseMove:
            tab_bar = obj
            try:
                idx = tab_bar.tabAt(event.position().toPoint())
            except AttributeError:
                idx = tab_bar.tabAt(event.pos())
            if idx >= 0 and not tab_bar.isTabEnabled(idx):
                tab_bar.setCursor(Qt.ForbiddenCursor)
            else:
                tab_bar.unsetCursor()
        elif event.type() == QEvent.Leave:
            obj.unsetCursor()
        return False


class _LockedTabFilter(QObject):
    """Swallows mouse + key input on a button while its ``locked``
    property is ``"true"``. Used by the source-tabs so that, when a
    file is staged, the tabs stop responding to clicks but still
    receive enter/leave events — which lets us swap their cursor to
    `Qt.ForbiddenCursor` (disabled widgets in Qt don't get those
    events, so the standard `setEnabled(False)` route loses the
    cursor signal entirely)."""

    _BLOCKED = frozenset({
        QEvent.MouseButtonPress,
        QEvent.MouseButtonRelease,
        QEvent.MouseButtonDblClick,
        QEvent.KeyPress,
        QEvent.KeyRelease,
    })

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() in self._BLOCKED \
                and obj.property("locked") == "true":
            event.accept()
            return True
        return super().eventFilter(obj, event)


CELIA_APK_RESOURCE = "celia-keyboard-11-0-5-352.apk"
CELIA_PKG = "com.huawei.ohos.inputmethod"
CELIA_IME_ID = (
    "com.huawei.ohos.inputmethod/"
    "com.android.inputmethod.latin.LatinIME"
)

THEME = "dark"

def _format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        return f"{h}h {rem // 60}m"
    d, rem = divmod(seconds, 86400)
    return f"{d}d {rem // 3600}h"


def _format_broker_status(status) -> str:
    head = "✓ alive" if status.alive else "✘ down"
    pid = str(status.pid) if status.pid is not None else "—"
    uptime = _format_uptime(status.uptime_s)
    if status.remote_md5 is None:
        jar = "jar absent"
    elif status.jar_matches:
        jar = "jar matches bundle"
    else:
        jar = (f"jar mismatch (remote={status.remote_md5[:8]}…, "
               f"bundled={(status.bundled_md5 or '')[:8]}…)")
    fields = [head, f"pid={pid}", f"uptime={uptime}",
              f"port={status.port}", jar]
    if not status.forwarded:
        fields.append("forward=fail")
    return " · ".join(fields)


def _format_tz_label(tz: str) -> str:
    try:
        zi = ZoneInfo(tz)
    except Exception:
        return tz
    now = datetime.now(zi)
    base = now.utcoffset()
    base_str = _offset_str(base)
    year = now.year
    jan = datetime(year, 1, 15, tzinfo=zi).utcoffset()
    jul = datetime(year, 7, 15, tzinfo=zi).utcoffset()
    suffix = ""
    if jan is not None and jul is not None and jan != jul:
        other = jul if base == jan else jan
        suffix = f" / DST {_offset_str(other)}"
    return f"{tz:<30} (UTC{base_str}{suffix})"


def _offset_str(off) -> str:
    if off is None:
        return "?"
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    h, rem = divmod(abs(total), 3600)
    m = rem // 60
    if m:
        return f"{sign}{h}:{m:02d}"
    return f"{sign}{h}"


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setProperty("role", "sectionLabel")
    return lbl


# =========================================================================
# MainWindow
# =========================================================================

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        # Native OS chrome carries the title + version — no in-window
        # title strip; that would just duplicate the system frame.
        self.setWindowTitle(f"IVI Installer v{__version__}")
        # Default size: 3:2 (the 11-inch 2K tablet ratio), scaled to
        # 1500×1000 — fits comfortably on a 14" Retina laptop while
        # giving the install tab enough room for the two-column grid
        # at full width. Qt clamps to the available screen on smaller
        # displays. `setMinimumSize` keeps the layout from breaking
        # if the user squeezes the window — sized so neither the
        # install-tab body nor the grant matrix needs a scrollbar at
        # the minimum (~1280 wide for the right column's mono lines,
        # ~1050 tall for APK + install-on + pipeline + matrix + log).
        self.resize(1500, 1000)
        self.setMinimumSize(1280, 1050)
        self.setMinimumSize(960, 640)

        # ---- state ----
        self._selected_path: Path | None = None
        self._capabilities: DeviceCapabilities | None = None
        self._info: DeviceInfo | None = None
        self._selected_serial: str | None = None
        self._busy: bool = False
        self._was_connected: bool = False
        self._was_unauthorized: bool = False
        self._current_tz: str | None = None
        self._active_thread = None
        self._active_worker = None
        # Strong, non-circular refs to fire-and-forget (thread, worker)
        # pairs. The lambda-closure-only pattern (`worker.finished.connect(
        # lambda: self._drop_refs(thread, worker))`) can't hold these alive
        # reliably — Python's gc walks the worker→connection→lambda→worker
        # cycle and can finalise the QThread Python wrapper while the C++
        # thread is still mid-call, tripping `QThread::~QThread()`'s
        # qFatal. Keep an explicit list and drop from it in `_drop_refs`.
        # See changelog/2026-05-08-v0.24.3-side-worker-thread-crash.md.
        self._inflight: list[tuple] = []
        self._poller: DevicePollerWorker | None = None
        self._celia_cache: Path | None = None
        # Empty maps for legacy attempt-status code paths.
        self._strategy_widgets: dict[str, QWidget] = {}
        self._strategy_status_labels: dict[str, QLabel] = {}

        self._build_ui()
        # Initial paint of screen-category cards so the diagrams + accent
        # borders match the saved checkbox state on first launch.
        self._update_install_option_visuals()
        self._refresh_screen_tooltips()
        self._update_install_button()
        self._update_apply_tz_button()

        # Hidden hotkey (Cmd/Ctrl+Shift+D) to surface the AppGallery
        # download dialog. Kept off the main UI per the redesign.
        sc = QShortcut(QKeySequence("Ctrl+Shift+D"), self)
        sc.activated.connect(self._prompt_appgallery)

    # ---- UI construction ----

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("centralRoot")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Device strip (replaces the pre-redesign header).
        self.status_widget = DeviceStatusWidget(self, theme=THEME)
        self.status_widget.refresh_requested.connect(self._refresh_now)
        outer.addWidget(self.status_widget)

        # Tabs. macOS gets a centered segmented control above the (hidden)
        # native QTabBar; Windows/Linux keep the underline-style native bar.
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.setTabPosition(QTabWidget.North)
        tab_titles = ["Install APK", "Store", "Timezone", "Device info",
                       "Keyboards", "Tools", "Enable ADB"]
        self.tabs.addTab(self._build_install_tab(), tab_titles[0])
        self.tabs.addTab(self._build_store_tab(), tab_titles[1])
        self.tabs.addTab(self._build_timezone_tab(), tab_titles[2])
        self.tabs.addTab(self._build_device_info_tab(), tab_titles[3])
        self.tabs.addTab(self._build_keyboards_tab(), tab_titles[4])
        self.tabs.addTab(self._build_tools_tab(), tab_titles[5])
        self.tabs.addTab(self._build_enable_adb_tab(), tab_titles[6])
        # Lock the trailing two tabs from end-user selection. Tools and
        # Enable ADB expose internal device controls that shouldn't be
        # reachable in the regular UI; the tabs stay listed (so the
        # layout matches screenshots/docs) but are unclickable.
        self.tabs.setTabEnabled(5, False)
        self.tabs.setTabEnabled(6, False)
        # Native QTabBar — show ForbiddenCursor on hover over disabled
        # tabs. Mouse tracking has to be on or we only get MouseMove
        # while a button is held.
        native_tab_bar = self.tabs.tabBar()
        native_tab_bar.setMouseTracking(True)
        self._forbidden_tab_filter = _ForbiddenCursorOnDisabledTab(self)
        native_tab_bar.installEventFilter(self._forbidden_tab_filter)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        if detect_os() == "mac":
            self.tabs.tabBar().hide()
            self._mac_tabbar = MacSegmentedTabBar(tab_titles, THEME, self)
            self._mac_tabbar.setTabEnabled(5, False)
            self._mac_tabbar.setTabEnabled(6, False)
            self._mac_tabbar.currentChanged.connect(self.tabs.setCurrentIndex)
            self.tabs.currentChanged.connect(self._mac_tabbar.setCurrentIndex)
            # Background strip the segmented control sits on.
            tab_strip = QFrame()
            tab_strip.setObjectName("tabStrip")
            tab_strip.setStyleSheet(
                f"QFrame#tabStrip {{ background: {TOKENS[THEME]['bgRaised']}; "
                f"border-bottom: 1px solid {TOKENS[THEME]['border']}; }}"
            )
            strip_layout = QVBoxLayout(tab_strip)
            strip_layout.setContentsMargins(0, 0, 0, 0)
            strip_layout.addWidget(self._mac_tabbar)
            outer.addWidget(tab_strip)

        # Tabs + log pane sit in a vertical splitter so the user can
        # drag the log pane up/down. The splitter handle styling is
        # defined in theme.py (QSplitter::handle:vertical).
        self._body_splitter = QSplitter(Qt.Vertical, self)
        self._body_splitter.setObjectName("bodySplitter")
        self._body_splitter.setChildrenCollapsible(False)
        self._body_splitter.setHandleWidth(6)
        self._body_splitter.addWidget(self.tabs)

        # Log pane.
        self.log_pane = LogPane(THEME, self)
        self.log_view = self.log_pane.view  # tests + handlers use log_view
        self.log_pane.set_log_path(str(logging_setup.current_log_file()))
        self.log_pane.copyRequested.connect(self._copy_log)
        self.log_pane.saveRequested.connect(self._save_log)
        self.log_pane.clearRequested.connect(lambda: self.log_view.clear())
        self._body_splitter.addWidget(self.log_pane)
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 0)
        # Reasonable initial split — tabs get the lion's share, log gets ~330px.
        self._body_splitter.setSizes([600, 330])
        outer.addWidget(self._body_splitter, stretch=1)

        # Hidden helper preserved for tests + legacy handlers.
        self.log_path_label = QLabel(f"📁 {logging_setup.current_log_file()}")
        self.log_path_label.setVisible(False)
        self.copy_log_button = QPushButton("Copy log")
        self.copy_log_button.clicked.connect(self._copy_log)
        self.copy_log_button.setVisible(False)
        self.save_log_button = QPushButton("Save log…")
        self.save_log_button.clicked.connect(self._save_log)
        self.save_log_button.setVisible(False)

        self.setStatusBar(QStatusBar(self))

    # =====================================================================
    # Install tab
    # =====================================================================

    def _build_install_tab(self) -> QWidget:
        outer = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body.setProperty("class", "tabContent")
        grid = QGridLayout(body)
        grid.setContentsMargins(18, 14, 18, 18)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(14)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # ---- LEFT column ----
        left = QVBoxLayout()
        left.setSpacing(10)

        left.addWidget(_section_label("APK"))

        # Source card — segmented "File | From URL" tabs over a
        # stacked body. Mirrors `design/exports/searching-no-device-
        # handoff/searching-screen.html` (.source-card / .source-tabs).
        self.source_card = QFrame()
        self.source_card.setObjectName("sourceCard")
        self.source_card.setStyleSheet(
            f"QFrame#sourceCard {{ background: {TOKENS[THEME]['bgRaised']}; "
            f"border: 1px solid {TOKENS[THEME]['border']}; "
            f"border-radius: 8px; }} "
            f"QFrame#sourceTabs {{ background: {TOKENS[THEME]['bgSunken']}; "
            f"border-bottom: 1px solid {TOKENS[THEME]['border']}; "
            f"border-top-left-radius: 8px; "
            f"border-top-right-radius: 8px; }} "
            f"QPushButton[sourceTab=\"true\"] {{ "
            f"background: transparent; border: 0; border-radius: 0; "
            f"border-bottom: 2px solid transparent; "
            f"color: {TOKENS[THEME]['fgMuted']}; "
            f"padding: 7px 0; font-size: 12px; font-weight: 500; }} "
            f"QPushButton[sourceTab=\"true\"]:checked {{ "
            f"color: {TOKENS[THEME]['fg']}; font-weight: 600; "
            f"background: {TOKENS[THEME]['bgRaised']}; "
            f"border-bottom-color: {TOKENS[THEME]['accent']}; }} "
            f"QPushButton[sourceTab=\"true\"]:hover:!checked {{ "
            f"color: {TOKENS[THEME]['fg']}; }} "
            # Locked state — file is staged, picker is hidden, tabs
            # are inert. Keep the active tab readable but mute the
            # other one and override the hover so the buttons don't
            # invite clicks.
            f"QPushButton[sourceTab=\"true\"][locked=\"true\"]:!checked {{ "
            f"color: {TOKENS[THEME]['fgDim']}; }} "
            f"QPushButton[sourceTab=\"true\"][locked=\"true\"]:hover {{ "
            f"color: inherit; }}"
        )
        sc_lay = QVBoxLayout(self.source_card)
        sc_lay.setContentsMargins(0, 0, 0, 0)
        sc_lay.setSpacing(0)

        tabs_row = QFrame()
        tabs_row.setObjectName("sourceTabs")
        tr_lay = QHBoxLayout(tabs_row)
        tr_lay.setContentsMargins(0, 0, 0, 0)
        tr_lay.setSpacing(0)
        self._source_tab_file = QPushButton("File")
        self._source_tab_url = QPushButton("From URL")
        for btn in (self._source_tab_file, self._source_tab_url):
            btn.setProperty("sourceTab", "true")
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setCursor(Qt.PointingHandCursor)
            tr_lay.addWidget(btn, stretch=1)
        self._source_tab_file.setChecked(True)
        sc_lay.addWidget(tabs_row)

        self.source_stack = QStackedWidget()
        sc_lay.addWidget(self.source_stack)

        # Compact APK summary — sibling of `source_stack` (tabs stay
        # above it). Hidden by default. When the user stages a file
        # (drop / pick / URL download), `_set_apk_file` hides the stack
        # body and shows this card in its place; `_clear_file` puts
        # the stack back. The tabs remain visible throughout.
        apk_card_holder = QWidget()
        ach_lay = QVBoxLayout(apk_card_holder)
        ach_lay.setContentsMargins(12, 12, 12, 12)
        ach_lay.setSpacing(0)
        self.apk_card = ApkCard(THEME)
        self.apk_card.cleared.connect(self._clear_file)
        ach_lay.addWidget(self.apk_card)
        self._apk_card_holder = apk_card_holder
        self._apk_card_holder.hide()
        sc_lay.addWidget(apk_card_holder)

        # Page 0 — local file: drop zone (the apk_card lives outside
        # the stack — see `apk_card_holder` above).
        file_page = QWidget()
        fp_lay = QVBoxLayout(file_page)
        fp_lay.setContentsMargins(12, 12, 12, 12)
        fp_lay.setSpacing(8)
        self.drop_zone = DropZone(THEME)
        self.drop_zone.fileDropped.connect(self._on_file_dropped)
        self.drop_zone.browseRequested.connect(self._choose_file)
        fp_lay.addWidget(self.drop_zone)
        self.source_stack.addWidget(file_page)

        # Page 1 — AppGallery URL.
        url_page = QWidget()
        up_lay = QVBoxLayout(url_page)
        up_lay.setContentsMargins(12, 12, 12, 12)
        up_lay.setSpacing(8)

        self.appgallery_input = QLineEdit()
        self.appgallery_input.setPlaceholderText(
            "AppGallery link or id — e.g. "
            "https://appgallery.huawei.com/app/C101898721"
        )
        self.appgallery_input.setClearButtonEnabled(True)
        self.appgallery_input.returnPressed.connect(
            self._on_appgallery_download)
        self.appgallery_input.textChanged.connect(
            lambda _t: self._update_appgallery_button())
        up_lay.addWidget(self.appgallery_input)

        url_hint = QLabel(
            "Paste a Huawei AppGallery URL or the bare app id "
            "(format: C + digits). Free apps only — paid apps are "
            "blocked at Huawei's CDN.")
        url_hint.setProperty("role", "dim")
        url_hint.setWordWrap(True)
        url_hint.setStyleSheet(
            f"color: {TOKENS[THEME]['fgDim']}; font-size: 11.5px;"
        )
        up_lay.addWidget(url_hint)

        dl_row = QHBoxLayout()
        dl_row.setSpacing(8)
        self.appgallery_button = QPushButton("Download APK")
        self.appgallery_button.setObjectName("primary")
        self.appgallery_button.setCursor(Qt.PointingHandCursor)
        self.appgallery_button.clicked.connect(self._on_appgallery_download)
        dl_row.addWidget(self.appgallery_button)
        self.appgallery_progress = QProgressBar()
        self.appgallery_progress.setRange(0, 100)
        self.appgallery_progress.setVisible(False)
        dl_row.addWidget(self.appgallery_progress, stretch=1)
        up_lay.addLayout(dl_row)
        up_lay.addStretch(1)
        self.source_stack.addWidget(url_page)

        # Tab → stack page binding. The `pressed` filter below blocks
        # presses while a file is staged, so we don't have to special-
        # case the toggle handler.
        self._source_tab_file.toggled.connect(
            lambda checked: checked and self.source_stack.setCurrentIndex(0))
        self._source_tab_url.toggled.connect(
            lambda checked: checked and self.source_stack.setCurrentIndex(1))
        # Event filter that swallows mouse + key presses on the tab
        # button when its `locked` property is "true". Disabled
        # buttons in Qt don't receive enter/leave events, so we can't
        # show a forbidden cursor that way — keep them enabled and
        # block the input directly. See `_set_source_tabs_locked`.
        self._tab_lock_filter = _LockedTabFilter(self)
        self._source_tab_file.installEventFilter(self._tab_lock_filter)
        self._source_tab_url.installEventFilter(self._tab_lock_filter)

        left.addWidget(self.source_card)

        # Hidden file_label preserved for tests.
        self.file_label = QLabel("—")
        self.file_label.setVisible(False)

        # Hidden file picker buttons (the drop zone is the canonical
        # entry point now; tests assert these exist via clear/choose
        # button reference paths only via _set_busy → keep them).
        self.choose_button = QPushButton("Choose…")
        self.choose_button.clicked.connect(self._choose_file)
        self.choose_button.setVisible(False)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self._clear_file)
        self.clear_button.setVisible(False)
        self._update_appgallery_button()

        # ---- Install on (1×3 checkbox grid: driver / passenger / rear).
        left.addSpacing(6)
        left.addWidget(_section_label("Install on"))

        self._screen_categories: tuple[ScreenCategory, ...] = (
            categorize_screens([])  # initial fallback layout until probe lands
        )
        saved_keys = settings.get(SETTING_SELECTED_SCREENS)
        if not isinstance(saved_keys, (list, tuple)) or not saved_keys:
            saved_keys = list(DEFAULT_SELECTED_SCREENS)
        self._selected_screen_keys: set[str] = {
            str(k) for k in saved_keys
            if k in {"driver", "passenger", "rear"}
        } or set(DEFAULT_SELECTED_SCREENS)

        self._screen_cards: dict[str, QFrame] = {}
        self._screen_checks: dict[str, QCheckBox] = {}
        screen_grid = QGridLayout()
        screen_grid.setContentsMargins(0, 0, 0, 0)
        screen_grid.setHorizontalSpacing(8)
        for col, key in enumerate(("driver", "passenger", "rear")):
            check = QCheckBox()
            check.setChecked(key in self._selected_screen_keys)
            check.setCursor(Qt.PointingHandCursor)
            card = self._make_screen_option(check, key)
            self._screen_cards[key] = card
            self._screen_checks[key] = check
            check.toggled.connect(self._on_screen_toggle)
            screen_grid.addWidget(card, 0, col)
            screen_grid.setColumnStretch(col, 1)
        left.addLayout(screen_grid)

        # ---- Strategy radios (canonical state — drive settings + tests).
        # The visible UI is the segmented pill below; these radios stay
        # hidden but track the same checked-state via toggled signals.
        saved_primary = (settings.get(SETTING_PRIMARY_STRATEGY)
                          or DEFAULT_PRIMARY_STRATEGY)
        self.strat_pmdisable_radio = QRadioButton("pm-disable (lighter)")
        self.strat_hdb_radio = QRadioButton("HDB broker (5.0 path)")
        self._strategy_group = QButtonGroup(self)
        self._strategy_group.addButton(self.strat_pmdisable_radio)
        self._strategy_group.addButton(self.strat_hdb_radio)
        if saved_primary == "hdb_broker_install":
            self.strat_hdb_radio.setChecked(True)
        else:
            self.strat_pmdisable_radio.setChecked(True)
        self.strat_pmdisable_radio.toggled.connect(
            lambda c: c and settings.set(
                SETTING_PRIMARY_STRATEGY, "pm_disable_install"))
        self.strat_hdb_radio.toggled.connect(
            lambda c: c and settings.set(
                SETTING_PRIMARY_STRATEGY, "hdb_broker_install"))
        self.strat_pmdisable_radio.setVisible(False)
        self.strat_hdb_radio.setVisible(False)

        # ---- Force reinstall row — checkbox on the left, strategy
        # segmented pill on the right (mirrors the hidden radios above).
        left.addSpacing(14)
        force_row = QHBoxLayout()
        force_row.setSpacing(6)
        self.force_reinstall_check = QCheckBox("Force reinstall (uninstall first)")
        self.force_reinstall_check.setToolTip(
            "Recovers from INSTALL_FAILED_VERSION_DOWNGRADE. WARNING: "
            "removes the app's data on every screen."
        )
        force_row.addWidget(self.force_reinstall_check)
        force_hint = QLabel("— for downgrades")
        force_hint.setProperty("role", "dim")
        force_row.addWidget(force_hint)
        force_row.addStretch(1)

        # Strategy segmented pill (visible).
        strat_label = QLabel("STRATEGY")
        strat_label.setProperty("role", "sectionLabel")
        force_row.addWidget(strat_label)

        strat_pill = QFrame()
        strat_pill.setObjectName("stratPill")
        strat_pill.setStyleSheet(
            f"QFrame#stratPill {{ background: {TOKENS[THEME]['bgSunken']}; "
            f"border: 1px solid {TOKENS[THEME]['border']}; "
            f"border-radius: 7px; }} "
            f"QPushButton[stratItem=\"true\"] {{ "
            f"background: transparent; border: 0; border-radius: 5px; "
            f"color: {TOKENS[THEME]['fgMuted']}; "
            f"padding: 4px 12px; font-size: 12px; font-weight: 500; }} "
            f"QPushButton[stratItem=\"true\"]:checked {{ "
            f"background: {TOKENS[THEME]['bgRaised']}; "
            f"color: {TOKENS[THEME]['fg']}; font-weight: 600; }} "
            f"QPushButton[stratItem=\"true\"]:hover:!checked {{ "
            f"color: {TOKENS[THEME]['fg']}; }}"
        )
        sp_l = QHBoxLayout(strat_pill)
        sp_l.setContentsMargins(3, 3, 3, 3)
        sp_l.setSpacing(0)
        self._strat_btn_pm = QPushButton("Simple")
        self._strat_btn_pm.setProperty("stratItem", "true")
        self._strat_btn_pm.setCheckable(True)
        self._strat_btn_pm.setAutoExclusive(True)
        self._strat_btn_pm.setCursor(Qt.PointingHandCursor)
        self._strat_btn_pm.setToolTip(
            "Lighter path: temporarily disables the system installer guard."
        )
        sp_l.addWidget(self._strat_btn_pm)
        self._strat_btn_hdb = QPushButton("Complex")
        self._strat_btn_hdb.setProperty("stratItem", "true")
        self._strat_btn_hdb.setCheckable(True)
        self._strat_btn_hdb.setAutoExclusive(True)
        self._strat_btn_hdb.setCursor(Qt.PointingHandCursor)
        self._strat_btn_hdb.setToolTip(
            "Avatr-HDB broker path — works on HarmonyOS 5.0 firmware."
        )
        sp_l.addWidget(self._strat_btn_hdb)

        # Initial state mirrors the canonical hidden radios.
        if self.strat_hdb_radio.isChecked():
            self._strat_btn_hdb.setChecked(True)
        else:
            self._strat_btn_pm.setChecked(True)

        # Two-way binding. The hidden radios persist settings via their
        # own toggled handlers; the visible buttons just drive them.
        self._strat_btn_pm.toggled.connect(self.strat_pmdisable_radio.setChecked)
        self._strat_btn_hdb.toggled.connect(self.strat_hdb_radio.setChecked)
        # And keep the visible UI in sync if the hidden state is changed
        # programmatically (e.g. from tests).
        self.strat_pmdisable_radio.toggled.connect(self._strat_btn_pm.setChecked)
        self.strat_hdb_radio.toggled.connect(self._strat_btn_hdb.setChecked)
        # Repaint the pipeline stages when the active strategy changes —
        # the two strategies have different stage shapes.
        self.strat_pmdisable_radio.toggled.connect(
            lambda checked: checked and self._refresh_pipeline_stages())
        self.strat_hdb_radio.toggled.connect(
            lambda checked: checked and self._refresh_pipeline_stages())

        force_row.addWidget(strat_pill)
        left.addLayout(force_row)
        left.addSpacing(14)

        # ---- Primary CTA + helper text.
        cta_row = QHBoxLayout()
        cta_row.setSpacing(10)
        self.install_button = QPushButton("Install")
        self.install_button.setObjectName("primary")
        self.install_button.setCursor(Qt.PointingHandCursor)
        self.install_button.clicked.connect(self._on_install)
        cta_row.addWidget(self.install_button)
        self._cta_hint = QLabel("Drop an APK to enable.")
        self._cta_hint.setProperty("role", "dim")
        cta_row.addWidget(self._cta_hint)
        cta_row.addStretch(1)
        left.addLayout(cta_row)

        # Hidden attempts view preserved (handler appendPlainText writes to it).
        self.attempts_view = QPlainTextEdit()
        self.attempts_view.setReadOnly(True)
        self.attempts_view.setMaximumBlockCount(400)
        self.attempts_view.setVisible(False)

        left.addStretch(1)

        # ---- RIGHT column ----
        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(_section_label("Pipeline"))
        initial_labels, initial_index_map = self._pipeline_stages_for(
            self._selected_primary_strategy(),
            self._selected_target_users(),
        )
        self._stage_index_map: dict[int, int] = initial_index_map
        self.pipeline = Pipeline(initial_labels, THEME)
        right.addWidget(self.pipeline)

        matrix_row = QHBoxLayout()
        matrix_row.setSpacing(10)
        matrix_row.addWidget(_section_label("Grant matrix"))
        matrix_hint = QLabel("permissions × users")
        matrix_hint.setProperty("role", "sectionHint")
        matrix_row.addWidget(matrix_hint)
        matrix_row.addStretch(1)
        right.addLayout(matrix_row)
        self.grant_matrix = GrantMatrix(THEME)
        right.addWidget(self.grant_matrix)

        self.success_banner = SuccessBanner(THEME)
        self.success_banner.linkClicked.connect(self._show_log_file_in_finder)
        right.addWidget(self.success_banner)

        right.addStretch(1)

        # Wrap columns in widgets so addLayout/addStretch work consistently.
        left_w = QWidget()
        left_w.setLayout(left)
        right_w = QWidget()
        right_w.setLayout(right)
        grid.addWidget(left_w, 0, 0)
        grid.addWidget(right_w, 0, 1)

        scroll.setWidget(body)
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.addWidget(scroll)
        return outer

    _SCREEN_LABELS: dict[str, str] = {
        "driver": "Driver",
        "passenger": "Passenger",
        "rear": "Rear",
    }

    def _make_screen_option(self, check: QCheckBox, key: str) -> QFrame:
        """Compact card for a single screen-category checkbox.

        Layout: [✓] Title  ·  small ScreensDiagram. No sub-text per
        the v0.8.6 redesign — the 3-card grid would otherwise wrap
        awkwardly on narrow windows. The actual Android user ids
        backing this category live in the tooltip so users can
        sanity-check the cabin mapping after a real-device install
        (and report back if it's wrong on a model we haven't seen).
        """
        card = QFrame()
        card.setObjectName("card")
        # Taller layout per v0.8.8 — gives the inverted-pyramid cabin
        # diagram room to breathe and reads less cramped next to the
        # title.
        card.setMinimumHeight(58)
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(10)

        h.addWidget(check, alignment=Qt.AlignVCenter)

        title_lbl = QLabel(self._SCREEN_LABELS.get(key, key.title()))
        title_lbl.setProperty("role", "title")
        h.addWidget(title_lbl, stretch=1)

        diagram = ScreensDiagram(THEME, mode=key if check.isChecked() else "off")
        h.addWidget(diagram, alignment=Qt.AlignVCenter)

        card._check = check  # type: ignore[attr-defined]
        card._diagram = diagram  # type: ignore[attr-defined]
        card._key = key  # type: ignore[attr-defined]
        card._title_lbl = title_lbl  # type: ignore[attr-defined]

        # Whole-row click toggles the checkbox (label + diagram are
        # transparent to clicks otherwise).
        def _toggle(_e):
            check.setChecked(not check.isChecked())
        card.mousePressEvent = _toggle  # type: ignore[assignment]
        return card

    def _refresh_screen_tooltips(self) -> None:
        """Update each card's tooltip with the resolved Android user ids.

        Called whenever ``_screen_categories`` changes — initial paint
        plus after every device probe. Hover the card to see e.g.
        "→ user 13" on Driver, "→ users 10, 12" on Rear.
        """
        by_key = {cat.key: cat for cat in self._screen_categories}
        for key, card in getattr(self, "_screen_cards", {}).items():
            cat = by_key.get(key)
            if cat is None or not cat.user_ids:
                tip = f"{self._SCREEN_LABELS.get(key, key.title())} — no matching screen"
            else:
                ids = ", ".join(str(u) for u in cat.user_ids)
                noun = "user" if len(cat.user_ids) == 1 else "users"
                names = [n for n in (cat.user_labels or ()) if n]
                if names and any(n.lower() not in {"", "owner"}
                                  and not n.lower().startswith("user ")
                                  for n in names):
                    name_hint = f"  ({', '.join(names)})"
                else:
                    name_hint = ""
                tip = (f"{self._SCREEN_LABELS.get(key, key.title())} "
                        f"→ {noun} {ids}{name_hint}")
            card.setToolTip(tip)
            check = getattr(card, "_check", None)
            if check is not None:
                check.setToolTip(tip)

    def _on_screen_toggle(self) -> None:
        """Recompute selected set after any checkbox toggle."""
        self._selected_screen_keys = {
            key for key, cb in self._screen_checks.items() if cb.isChecked()
        }
        # Persist as a sorted list so the order is stable across runs.
        settings.set(
            SETTING_SELECTED_SCREENS,
            sorted(self._selected_screen_keys),
        )
        self._update_install_option_visuals()
        self._refresh_pipeline_stages()
        self._update_install_button()

    def _update_install_option_visuals(self) -> None:
        """Reflect the current checkbox state on each screen card.

        The border stays a constant 1px in both states (only the color
        swaps between ``border`` and ``accent``) so toggling never
        shifts the layout. We explicitly redeclare background +
        border-radius in the override too — without them, setting
        stylesheet on the child QFrame masks the theme rule and the
        card visibly loses its rounded corners on first toggle.
        """
        toks = TOKENS[THEME]
        bg = toks["bgRaised"]
        radius = "8px"
        for key, card in getattr(self, "_screen_cards", {}).items():
            check = getattr(card, "_check", None)
            diagram = getattr(card, "_diagram", None)
            on = bool(check and check.isChecked())
            if diagram is not None:
                diagram.set_mode(key if on else "off")
            border_color = toks["accent"] if on else toks["border"]
            card.setStyleSheet(
                f"QFrame#card {{ background: {bg}; "
                f"border: 1px solid {border_color}; "
                f"border-radius: {radius}; }}"
            )

    # =====================================================================
    # Tools tab
    # =====================================================================

    def _build_tools_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(18, 14, 18, 18)
        v.setSpacing(12)

        # ---- Grant runtime permissions card.
        grant_card = QFrame()
        grant_card.setObjectName("card")
        g = QVBoxLayout(grant_card)
        g.setContentsMargins(14, 14, 14, 14)
        g.setSpacing(8)
        g.addWidget(self._title_label("Grant runtime permissions"))
        g.addWidget(self._muted_label(
            "For an already-installed app. Lists every third-party package on the device."))

        pkg_row = QHBoxLayout()
        pkg_row.setSpacing(8)
        self.grant_pkg_combo = QComboBox()
        self.grant_pkg_combo.setEditable(True)
        self.grant_pkg_combo.lineEdit().setPlaceholderText(
            "Pick an installed package, or type a name (Refresh re-fetches)"
        )
        pkg_row.addWidget(self.grant_pkg_combo, stretch=1)
        self.grant_pkg_refresh = QPushButton("Refresh")
        self.grant_pkg_refresh.clicked.connect(self._kick_pkg_list)
        pkg_row.addWidget(self.grant_pkg_refresh)
        g.addLayout(pkg_row)

        self.grant_button = QPushButton("Grant runtime permissions")
        self.grant_button.setObjectName("primary")
        self.grant_button.setCursor(Qt.PointingHandCursor)
        self.grant_button.clicked.connect(self._on_grant_perms)
        cta_row = QHBoxLayout()
        cta_row.addWidget(self.grant_button)
        cta_row.addStretch(1)
        g.addLayout(cta_row)

        self.grant_status_label = QLabel("Last run · —")
        self.grant_status_label.setProperty("mono", "true")
        self.grant_status_label.setStyleSheet(
            f"font-family: '{mono_family()}'; color: {TOKENS[THEME]['fgMuted']};"
            f"background: {TOKENS[THEME]['bgSunken']}; "
            f"border: 1px solid {TOKENS[THEME]['border']}; "
            f"border-radius: 6px; padding: 8px 10px;"
        )
        g.addWidget(self.grant_status_label)
        v.addWidget(grant_card)

        # ---- Diagnose + Bypass health (side by side).
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(12)

        diag_card = QFrame()
        diag_card.setObjectName("card")
        d = QVBoxLayout(diag_card)
        d.setContentsMargins(14, 14, 14, 14)
        d.setSpacing(8)
        d.addWidget(self._title_label("Diagnose"))
        d.addWidget(self._muted_label(
            "Reads device state, services, settings, install handlers, "
            "forensics on installed apps. Saves a report locally. "
            "Read-only."))
        self.diagnose_button = QPushButton("Run diagnose")
        self.diagnose_button.clicked.connect(
            lambda: self._on_install("diagnose"))
        diag_btn_row = QHBoxLayout()
        diag_btn_row.addWidget(self.diagnose_button)
        diag_btn_row.addStretch(1)
        d.addLayout(diag_btn_row)
        d.addStretch(1)
        bottom_row.addWidget(diag_card, stretch=1)

        bypass_card = QFrame()
        bypass_card.setObjectName("card")
        bp = QVBoxLayout(bypass_card)
        bp.setContentsMargins(14, 14, 14, 14)
        bp.setSpacing(8)
        bp.addWidget(self._title_label("Bypass health"))
        self.bypass_status_label = QLabel("Status: —")
        self.bypass_status_label.setStyleSheet(
            f"font-family: '{mono_family()}'; color: {TOKENS[THEME]['fgMuted']};"
        )
        self.bypass_status_label.setWordWrap(True)
        bp.addWidget(self.bypass_status_label)
        b_row = QHBoxLayout()
        self.bypass_refresh_button = QPushButton("Refresh")
        self.bypass_refresh_button.clicked.connect(self._on_bypass_refresh)
        b_row.addWidget(self.bypass_refresh_button)
        self.bypass_redeploy_button = QPushButton("Redeploy broker")
        self.bypass_redeploy_button.clicked.connect(self._on_bypass_redeploy)
        b_row.addWidget(self.bypass_redeploy_button)
        b_row.addStretch(1)
        bp.addLayout(b_row)
        bp.addStretch(1)
        bottom_row.addWidget(bypass_card, stretch=1)

        v.addLayout(bottom_row)
        v.addStretch(1)

        scroll.setWidget(body)
        return scroll

    # =====================================================================
    # Timezone tab
    # =====================================================================

    def _build_timezone_tab(self) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(18, 14, 18, 18)
        v.setSpacing(12)

        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        cur_col = QVBoxLayout()
        cur_col.setSpacing(2)
        cur_col.addWidget(_section_label("Current timezone"))
        self.current_tz_label = QLabel("—")
        self.current_tz_label.setProperty("role", "title")
        self.current_tz_label.setProperty("mono", "true")
        self.current_tz_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.current_tz_label.setStyleSheet(
            f"font-family: '{mono_family()}'; color: {TOKENS[THEME]['fg']};"
            f"font-size: 14px; font-weight: 600;"
        )
        cur_col.addWidget(self.current_tz_label)
        head_row.addLayout(cur_col, stretch=1)

        self.apply_tz_button = QPushButton("Apply")
        self.apply_tz_button.setObjectName("primary")
        self.apply_tz_button.setCursor(Qt.PointingHandCursor)
        self.apply_tz_button.clicked.connect(self._on_apply_timezone)
        head_row.addWidget(self.apply_tz_button, alignment=Qt.AlignVCenter)
        v.addLayout(head_row)

        self.tz_search = QLineEdit()
        self.tz_search.setPlaceholderText("Filter timezones…")
        self.tz_search.textChanged.connect(self._on_tz_filter)
        v.addWidget(self.tz_search)

        self.tz_list = QListWidget()
        self.tz_list.setUniformItemSizes(True)
        self.tz_list.setStyleSheet(
            f"QListWidget {{ font-family: '{mono_family()}'; }}"
        )
        self.tz_list.itemSelectionChanged.connect(self._update_apply_tz_button)
        self.tz_list.itemDoubleClicked.connect(lambda _: self._on_apply_timezone())
        self._populate_tz_list()
        v.addWidget(self.tz_list, stretch=1)
        return body

    # =====================================================================
    # Device info tab
    # =====================================================================

    def _build_device_info_tab(self) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(18, 14, 18, 18)
        v.setSpacing(10)

        head_row = QHBoxLayout()
        head_row.addWidget(_section_label("Device facts"))
        head_row.addStretch(1)
        self.info_refresh_button = QPushButton("Refresh")
        self.info_refresh_button.clicked.connect(self._kick_device_info_read)
        head_row.addWidget(self.info_refresh_button)
        self.info_copy_button = QPushButton("Copy")
        self.info_copy_button.clicked.connect(self._copy_device_info)
        head_row.addWidget(self.info_copy_button)
        v.addLayout(head_row)

        self.info_view = QPlainTextEdit()
        self.info_view.setReadOnly(True)
        self.info_view.setStyleSheet(
            f"QPlainTextEdit {{ font-family: '{mono_family()}'; "
            f"background: {TOKENS[THEME]['bgSunken']}; "
            f"border: 1px solid {TOKENS[THEME]['border']}; "
            f"border-radius: 6px; padding: 12px 14px; line-height: 1.55;}}"
        )
        self.info_view.setPlaceholderText(
            "Connect a device and switch to this tab — info will load."
        )
        v.addWidget(self.info_view, stretch=1)
        return body

    # =====================================================================
    # Keyboards tab — kept (5th tab) per CODER_NOTES.md
    # =====================================================================

    def _build_keyboards_tab(self) -> QWidget:
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(18, 14, 18, 18)
        v.setSpacing(12)

        # Intro card.
        intro_card = QFrame()
        intro_card.setObjectName("card")
        ic = QVBoxLayout(intro_card)
        ic.setContentsMargins(14, 14, 14, 14)
        ic.setSpacing(8)
        ic.addWidget(self._title_label(
            "Celia Keyboard (com.huawei.ohos.inputmethod)"))
        ic.addWidget(self._muted_label(
            "The stock Avatr/Deepal keyboard ships without Russian. "
            "Install the full Celia IME and register it on every screen."))

        self.kb_set_default_check = QCheckBox(
            "Set Celia as the default keyboard (replaces stock Huawei IME)")
        ic.addWidget(self.kb_set_default_check)

        kb_btn_row = QHBoxLayout()
        self.kb_install_button = QPushButton("Install Celia Keyboard")
        self.kb_install_button.setObjectName("primary")
        self.kb_install_button.clicked.connect(self._on_install_celia)
        kb_btn_row.addWidget(self.kb_install_button)
        self.kb_enroll_button = QPushButton("Enable / set default only")
        self.kb_enroll_button.clicked.connect(self._on_enroll_celia)
        kb_btn_row.addWidget(self.kb_enroll_button)
        kb_btn_row.addStretch(1)
        ic.addLayout(kb_btn_row)

        self.kb_status_label = QLabel("Status: —")
        self.kb_status_label.setProperty("role", "muted")
        ic.addWidget(self.kb_status_label)
        v.addWidget(intro_card)

        # IME list card.
        ime_card = QFrame()
        ime_card.setObjectName("card")
        ic2 = QVBoxLayout(ime_card)
        ic2.setContentsMargins(14, 14, 14, 14)
        ic2.setSpacing(8)
        head = QHBoxLayout()
        head.addWidget(self._title_label("Installed input methods (user 0)"))
        head.addStretch(1)
        self.ime_refresh_button = QPushButton("Refresh")
        self.ime_refresh_button.clicked.connect(self._on_refresh_imes)
        head.addWidget(self.ime_refresh_button)
        ic2.addLayout(head)
        self.ime_list = QListWidget()
        self.ime_list.setUniformItemSizes(True)
        self.ime_list.setStyleSheet(
            f"QListWidget {{ font-family: '{mono_family()}'; }}"
        )
        ic2.addWidget(self.ime_list)
        v.addWidget(ime_card)

        v.addStretch(1)
        return body

    # =====================================================================
    # Store tab
    # =====================================================================

    def _build_store_tab(self) -> QWidget:
        # The store now opens on the curated extras (~60 entries) by
        # default. The full AppGallery catalog is opt-in via a
        # toggle in the toolbar — pulling the 50 MB index isn't
        # something we want to do on every cold start.
        from PySide6.QtCore import QSettings
        from .. import catalog_store as _store_mod
        self._store_load_error = None
        # Always have the extras-only store ready. Cheap (in-memory).
        try:
            self._extras_store = _store_mod.CatalogStore.from_apps(
                catalog_module.load_extras().apps,
                generated_at="",
            )
        except Exception as e:
            log.exception("extras load failed")
            self._store_load_error = f"{type(e).__name__}: {e}"
            self._extras_store = None
        # The full-catalog (extras + F-Droid) lives on disk. Open
        # readonly if the previous run already built it; otherwise it
        # stays None and the toggle has to kick a fetch on first click.
        self._full_store = None
        db_path = _store_mod.default_db_path()
        if db_path.is_file():
            try:
                full = _store_mod.CatalogStore.open_readonly(db_path)
                if full.total() <= 8:
                    full.close()
                else:
                    self._full_store = full
            except Exception:
                log.exception("opening cached catalog SQLite failed")

        settings = QSettings("deepal2", "ivi-installer")
        self._store_wants_appgallery = settings.value(
            "store/appgallery_enabled", False, type=bool)

        initial_store = (
            self._full_store
            if (self._store_wants_appgallery and self._full_store is not None)
            else self._extras_store
        )
        self.store_tab = StoreTab(initial_store, theme=THEME, parent=self)
        self.store_tab.set_appgallery_toggle(
            initial_store is self._full_store
            and self._store_wants_appgallery)
        self.store_tab.install_requested.connect(self._on_store_install)
        self.store_tab.refresh_requested.connect(self._on_store_refresh)
        self.store_tab.appgallery_toggle_requested.connect(
            self._on_store_appgallery_toggled)
        self.store_tab.hot_keywords_requested.connect(
            self._on_store_hot_keywords_requested)
        self.store_tab.search_suggestions_requested.connect(
            self._on_store_search_suggestions_requested)
        self.store_tab.suggested_app_install_requested.connect(
            self._on_store_suggested_app_install)
        # Search-suggestion workers run on QThreads that MUST outlive
        # the in-flight network call; tearing them down while still
        # running trips Qt's "Destroyed while thread is still running"
        # qFatal and aborts the process. We track every in-flight
        # (thread, worker) pair in a list and remove on `finished`.
        # Stale results are dropped at the StoreTab side via the
        # current-input check inside ``set_search_suggestions``.
        self._hot_keywords_thread = None
        self._hot_keywords_worker = None
        self._search_suggest_inflight: list = []
        if self._store_load_error:
            self.store_tab.show_error(self._store_load_error)
        self._store_active_app_id: str | None = None
        # Track the in-flight catalog fetch so a second tab build (or
        # rapid refresh clicks) doesn't stack up parallel index pulls.
        self._catalog_thread = None
        self._catalog_worker = None
        # AppGallery toggle was on at launch but no on-disk SQLite
        # exists yet — kick the fetch so the user lands on the live
        # catalog without re-clicking.
        if self._store_wants_appgallery and self._full_store is None:
            self._kick_catalog_fetch(force=False)
        return self.store_tab

    def _on_store_appgallery_toggled(self, checked: bool) -> None:
        self._store_wants_appgallery = checked
        from PySide6.QtCore import QSettings
        QSettings("deepal2", "ivi-installer").setValue(
            "store/appgallery_enabled", checked)
        if not checked:
            if self._extras_store is not None:
                self.store_tab.set_catalog(self._extras_store)
            return
        # Toggle on: switch instantly to a cached SQLite if we have
        # one, else kick a fetch.
        if self._full_store is not None:
            self.store_tab.set_catalog(self._full_store)
            self._toast(
                f"Showing AppGallery catalog "
                f"({self._full_store.total()} apps)",
                kind="info",
            )
        else:
            self._kick_catalog_fetch(force=False)

    def _kick_catalog_fetch(self, *, force: bool) -> None:
        """Start a background AppGallery fetch + SQLite rebuild.

        No-op if one is already in flight — the user can spam Refresh,
        only the first request actually does work; subsequent clicks
        wait it out and pick up the same result.
        """
        if self._catalog_worker is not None:
            self._log_full("store: catalog fetch already in flight — skipping")
            return
        hint = (
            "Refreshing AppGallery catalog…" if force
            else "Loading AppGallery catalog…"
        )
        self.store_tab.show_loading(hint)
        worker = workers.CatalogFetchWorker(force=force)
        worker.log_line.connect(self._log_full)
        worker.result.connect(self._on_catalog_fetch_result)
        worker.error.connect(self._on_catalog_fetch_error)
        worker.finished.connect(self._on_catalog_fetch_finished)
        thread = workers.run_in_thread(worker)
        self._catalog_thread = thread
        self._catalog_worker = worker

    def _on_catalog_fetch_result(self, db_path_str) -> None:
        from pathlib import Path as _Path
        from .. import catalog_store as _store_mod
        db_path = _Path(db_path_str)
        if not db_path.is_file():
            self.store_tab.show_error(
                "Catalog SQLite missing after build — try Refresh.")
            return
        try:
            store = _store_mod.CatalogStore.open_readonly(db_path)
        except Exception as e:
            log.exception("opening freshly-built catalog failed")
            self.store_tab.show_error(f"{type(e).__name__}: {e}")
            return
        if store.total() == 0:
            store.close()
            self.store_tab.show_error(
                "Catalog came back empty — try Refresh later.")
            return
        total = store.total()
        # Replace the old full-store handle, close the previous one if
        # any. ``store_tab.set_catalog`` no longer closes — main_window
        # owns lifecycle.
        if self._full_store is not None:
            try:
                self._full_store.close()
            except Exception:
                log.exception("closing previous full store failed")
        self._full_store = store
        self.store_tab.set_catalog(store)
        self.store_tab.set_appgallery_toggle(
            bool(getattr(self, "_store_wants_appgallery", False)))
        self._toast(
            f"Store catalog loaded ({total} apps)",
            kind="info",
        )

    def _on_catalog_fetch_error(self, msg: str) -> None:
        # Keep the placeholder extras visible and surface the error in
        # a toast — replacing the whole view with an error page would
        # hide rows that are still installable.
        self._log_full(f"✘ store catalog fetch: {msg}")
        self._toast(f"Catalog refresh failed: {msg}", kind="warn")
        # Roll the toggle back so the UI doesn't show "AppGallery
        # loaded" while we're still on extras.
        has_full = self._full_store is not None
        self.store_tab.set_appgallery_toggle(has_full and bool(
            getattr(self, "_store_wants_appgallery", False)))
        if self._extras_store is not None and not has_full:
            self.store_tab.set_catalog(self._extras_store)
        # If the placeholder is empty (extras failed to load too) the
        # user has nothing — fall back to the error page.
        store = getattr(self.store_tab, "_store", None)
        if store is None or store.total() == 0:
            self.store_tab.show_error(msg)

    def _on_catalog_fetch_finished(self) -> None:
        thread = self._catalog_thread
        worker = self._catalog_worker
        self._catalog_thread = None
        self._catalog_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)

    def _on_store_install(self, entry) -> None:
        # Verbose breadcrumb up front — gives us the click → busy →
        # phase ordering in the in-app log if a user reports "nothing
        # happens" later.
        self._log_full(
            f"store: install click — id={entry.id!r} name={entry.name!r} "
            f"sources={[s.get('kind') for s in (entry.sources or ())]}")
        if self._busy:
            self._toast("Wait for the current operation to finish.",
                        kind="warn")
            self._log_full("store: install rejected — _busy=True")
            return
        if self._selected_serial is None:
            self._toast("Connect the head unit first.", kind="warn")
            self._log_full("store: install rejected — no head-unit serial")
            return
        self._set_busy(True)
        self._store_active_app_id = entry.id
        self.store_tab.set_install_phase(entry.id, PHASE_RESOLVING)
        self._log_full(f"=== Store install: {entry.name} ({entry.id}) ===")
        self._log_user(f"Installing {entry.name}…")
        # Up-front toast so the user knows the click registered even
        # when the row isn't visible (search-suggestion popup install,
        # or a row scrolled off-screen). The phase changes paint the
        # row state when it IS visible; the toast is the safety net.
        self._toast(f"Installing {entry.name}…", kind="info")
        worker = workers.StoreInstallWorker(
            entry,
            serial=self._selected_serial,
            primary_strategy=self._selected_primary_strategy(),
            target_users=self._selected_target_users(),
            force_reinstall=False,
        )
        worker.log_line.connect(self._log_full)
        worker.phase.connect(
            lambda phase, app_id=entry.id:
                self.store_tab.set_install_phase(app_id, phase))
        worker.progress.connect(
            lambda seen, total, app_id=entry.id:
                self.store_tab.set_install_progress(app_id, seen, total))
        worker.error.connect(
            lambda msg, app_id=entry.id:
                self._on_store_install_error(app_id, msg))
        worker.result.connect(
            lambda result, app_id=entry.id:
                self._on_store_install_result(app_id, result))
        # Wire EVERY worker.finished receiver BEFORE thread.start()
        # so that a worker which raises inside ``run()`` (before our
        # main thread can attach more handlers) doesn't lose the
        # ``finished`` emission. Previously the handler was connected
        # AFTER ``run_in_thread`` returned — fast-failing workers then
        # left ``_busy`` stuck on True forever, and every subsequent
        # click toasted "wait for the current operation".
        from PySide6.QtCore import QThread
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(
            lambda t=thread, w=worker: self._on_op_finished(t, w))
        thread.start()
        self._active_thread = thread
        self._active_worker = worker

    def _on_store_install_result(self, app_id: str, result) -> None:
        if result.success:
            self._toast(f"Installed → {app_id}", kind="info")
            self._log_full(f"✔ Store install: {app_id} ok")
            self._log_user("✔ Install complete")
            self.store_tab.set_install_phase(app_id, PHASE_SUCCESS)
        else:
            self._toast(f"Install failed: {result.message}", kind="error")
            self._log_full(f"✘ Store install: {result.message}")
            self._log_user("✘ Install failed")
            self.store_tab.set_install_phase(app_id, PHASE_FAILED)

    def _on_store_install_error(self, app_id: str, msg: str) -> None:
        self._log_full(f"✘ Store: {msg}")
        self._toast(f"Install failed: {msg}", kind="error")
        self.store_tab.set_install_phase(app_id, PHASE_FAILED)

    def _on_store_refresh(self) -> None:
        # User-triggered refresh: bypass the 24h disk cache and re-pull
        # the F-Droid index from the network.
        self._kick_catalog_fetch(force=True)

    # ---- search-dropdown plumbing ----

    def _on_store_hot_keywords_requested(self) -> None:
        if self._hot_keywords_worker is not None:
            return
        worker = workers.HotSearchKeywordsWorker()
        worker.result.connect(self._on_hot_keywords_result)
        worker.error.connect(lambda msg: self._log_full(
            f"store: hot keywords fetch failed: {msg}"))
        worker.finished.connect(self._on_hot_keywords_finished)
        thread = workers.run_in_thread(worker)
        self._hot_keywords_thread = thread
        self._hot_keywords_worker = worker

    def _on_hot_keywords_result(self, keywords: list) -> None:
        self.store_tab.set_hot_keywords(list(keywords or []))

    def _on_hot_keywords_finished(self) -> None:
        thread = self._hot_keywords_thread
        worker = self._hot_keywords_worker
        self._hot_keywords_thread = None
        self._hot_keywords_worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)
        if worker is not None:
            worker.deleteLater()

    def _on_store_search_suggestions_requested(self, keyword: str) -> None:
        # Don't try to cancel an in-flight worker — the QThread can't
        # be destroyed mid-network-call without crashing. We let it
        # finish naturally, drop stale results at the StoreTab via
        # the input-text comparison in ``set_search_suggestions``,
        # and just kick a parallel worker for the new keyword.
        worker = workers.CompleteSearchWordWorker(keyword)
        worker.result.connect(self._on_search_suggest_result)
        worker.error.connect(lambda msg: self._log_full(
            f"store: search suggestions failed: {msg}"))
        thread = workers.run_in_thread(worker)
        entry = (thread, worker)
        self._search_suggest_inflight.append(entry)
        # Keep references alive via the lambda closure until the
        # finished signal arrives on the main thread; only then is
        # it safe to drop the QThread.
        worker.finished.connect(
            lambda _e=entry: self._on_search_suggest_finished(_e))

    def _on_search_suggest_result(
        self, keyword: str, suggestions: list, top_app,
    ) -> None:
        self.store_tab.set_search_suggestions(
            keyword, list(suggestions or []),
            top_app if isinstance(top_app, dict) else None)

    def _on_search_suggest_finished(self, entry) -> None:
        thread, worker = entry
        try:
            self._search_suggest_inflight.remove(entry)
        except ValueError:
            pass
        if thread is not None:
            thread.quit()
            thread.wait(2000)
        if worker is not None:
            worker.deleteLater()

    def _on_store_suggested_app_install(self, item: dict) -> None:
        """Build a synthetic CatalogEntry from a ``completeSearchWord``
        top-app dict and run it through the existing store-install
        pipeline. Lets the user install AppGallery's #1 search match
        without scrolling the catalog list to find it.
        """
        from .. import catalog as _catalog_mod
        appid = str(item.get("appid") or item.get("id") or "")
        package = str(item.get("package") or "")
        name = str(item.get("name") or package or appid)
        if not appid or not package:
            self._toast("Search match has no installable id.", kind="warn")
            return
        size_raw = item.get("size") or item.get("fullSize")
        try:
            size_mb = (round(int(size_raw) / (1024 * 1024), 1)
                       if size_raw else None)
        except (TypeError, ValueError):
            size_mb = None
        entry = _catalog_mod.CatalogEntry(
            id=package,
            name=name,
            category="search",
            sources=({"kind": "appgallery", "id": appid},),
            description_ru=item.get("memo") or None,
            description_en=item.get("memo") or None,
            icon_url=item.get("icon") or None,
            tested=False,
            size_mb=size_mb,
            version=item.get("version")
                     or item.get("appVersionName") or None,
            initials=_catalog_mod.derive_initials(name),
        )
        self._on_store_install(entry)

    # =====================================================================
    # Small label helpers (Tools / Keyboards card titles).
    # =====================================================================

    def _title_label(self, text: str) -> QLabel:
        l = QLabel(text)
        l.setProperty("role", "title")
        return l

    def _muted_label(self, text: str) -> QLabel:
        l = QLabel(text)
        l.setProperty("role", "muted")
        l.setWordWrap(True)
        return l

    def _h_separator(self) -> QFrame:
        line = QFrame()
        line.setProperty("role", "hr")
        line.setFixedHeight(1)
        return line

    # =====================================================================
    # File handling
    # =====================================================================

    def _on_file_dropped(self, path_str: str) -> None:
        path = Path(path_str)
        if path.suffix.lower() != ".apk":
            self._toast(f"Not an .apk: {path.suffix or '<no extension>'}",
                        kind="warn")
            return
        self._set_apk_file(path)

    def _choose_file(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Pick APK", "", APK_FILTER
        )
        if not path_str:
            return
        path = Path(path_str)
        if path.suffix.lower() != ".apk":
            QMessageBox.warning(
                self, "Not an APK",
                f"Pick a .apk file (got {path.suffix or '<no extension>'}).",
            )
            return
        self._set_apk_file(path)

    def _set_apk_file(self, path: Path) -> None:
        try:
            size_mb = path.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = None
        self._selected_path = path
        self.file_label.setText(str(path))
        self.file_label.setToolTip(str(path))
        # Read the manifest so the APK card can render the same
        # `name · package · version` block as the Figma handoff
        # (instead of just the file stem). Best-effort — if the parse
        # fails, `read_meta` returns blanks and we fall through to the
        # filename-derived display.
        from .. import apk_meta
        meta = apk_meta.read_meta(path)
        # If the file stem looks like a raw AppGallery id (e.g.
        # `C100315379`), don't seed it as the fallback — the
        # package-derived heuristic ("Spotify" from
        # `com.spotify.music`) reads better.
        stem = path.stem
        fallback = "" if _looks_like_appgallery_id(stem) else stem
        display = apk_meta.derive_display_name(meta, fallback=fallback)
        version_text = meta.version_name
        if not version_text and meta.version_code is not None:
            version_text = f"({meta.version_code})"
        # Swap the source-card body (drop-zone / URL form) for the
        # compact APK card — tabs above stay visible so the user can
        # always switch sources or clear back to the picker.
        self.apk_card.set_apk(
            name=display or stem,
            package=meta.package,
            version=version_text,
            size_mb=size_mb,
            initials=apk_meta.derive_initials(
                display or stem, meta.package),
        )
        self.source_stack.hide()
        self._apk_card_holder.show()
        # Lock the source tabs while a file is staged. The picker is
        # hidden, so a tab click would silently change which page the
        # user returns to after dismissing the card. We don't use
        # `setEnabled(False)` because Qt then stops sending mouse
        # events to the widget, and the user gets no visual cue
        # (cursor stays as the pointing-hand pointer).
        self._set_source_tabs_locked(True)
        if meta.package:
            self._log_full(
                f"Selected {path.name} — {meta.package} "
                f"v{meta.version_name or '?'} "
                f"({path.stat().st_size:,} bytes)")
        else:
            self._log_full(
                f"Selected {path.name} ({path.stat().st_size:,} bytes)")
        self._log_user(f"Selected file: {path.name}")
        self._update_install_button()

    def _set_source_tabs_locked(self, locked: bool) -> None:
        """Flip the lock state on both source tabs.

        Locked tabs: ForbiddenCursor on hover, dimmed text on the
        non-active one, ignore mouse + key presses (handled by
        `_LockedTabFilter`). The QSS uses `[locked="true"]` so we
        re-polish each button to pick up the new property.
        """
        cursor = Qt.ForbiddenCursor if locked else Qt.PointingHandCursor
        flag = "true" if locked else "false"
        for btn in (self._source_tab_file, self._source_tab_url):
            btn.setProperty("locked", flag)
            btn.setCursor(cursor)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _clear_file(self) -> None:
        self._selected_path = None
        self.file_label.setText("—")
        self.file_label.setToolTip("")
        if hasattr(self, "_apk_card_holder"):
            self._apk_card_holder.hide()
        if hasattr(self, "source_stack"):
            self.source_stack.show()
        if hasattr(self, "_source_tab_file"):
            self._set_source_tabs_locked(False)
        self._update_install_button()

    # =====================================================================
    # Install-on / strategy resolvers
    # =====================================================================

    def _selected_target_users(self) -> tuple[int, ...] | None:
        """Resolve checkbox selection into the explicit user-id list.

        Returns None when every category is selected — that's the
        legacy "all multimedia screens" path which seeds via user 0
        and fans out across the canonical screen set. A subset returns
        a sorted tuple of user ids for the chosen categories.
        """
        if not self._selected_screen_keys:
            return ()  # no targets; install button stays disabled
        all_keys = {"driver", "passenger", "rear"}
        if self._selected_screen_keys >= all_keys:
            # All three checked → keep historical "all" behaviour.
            return None
        ids: list[int] = []
        for cat in self._screen_categories:
            if cat.key in self._selected_screen_keys:
                ids.extend(cat.user_ids)
        # De-dup while preserving sorted order — strategies treat the
        # first id as "seed" so we want a deterministic order.
        seen: set[int] = set()
        out: list[int] = []
        for uid in sorted(ids):
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
        return tuple(out)

    def _selected_primary_strategy(self) -> str:
        if (hasattr(self, "strat_hdb_radio")
                and self.strat_hdb_radio.isChecked()):
            return "hdb_broker_install"
        return "pm_disable_install"

    def _pipeline_stages_for(
        self, strategy_name: str,
        target_users: tuple[int, ...] | None = None,
    ) -> tuple[list[str], dict[int, int]]:
        """Visible labels + ``code_index → visible_index`` map.

        Stages flagged as ``single_user_skips`` on the descriptor are
        hidden when the resolved target lands on a single user (no
        fan-out). Falls back to the HDB descriptor if the strategy
        isn't found — keeps the widget non-empty in degenerate states.
        """
        try:
            descriptor = installer.get_strategy(strategy_name)
        except KeyError:
            descriptor = installer.get_strategy("hdb_broker_install")
        pairs = descriptor.stages_for(target_users)
        if not pairs:
            pairs = installer.get_strategy(
                "hdb_broker_install").stages_for(target_users)
        labels = [label for label, _idx in pairs]
        index_map = {code_idx: visible_idx
                      for visible_idx, (_label, code_idx) in enumerate(pairs)}
        return labels, index_map

    def _refresh_pipeline_stages(self) -> None:
        """Repaint the pipeline rows for the current (strategy, targets)."""
        if not hasattr(self, "pipeline"):
            return
        labels, index_map = self._pipeline_stages_for(
            self._selected_primary_strategy(),
            self._selected_target_users(),
        )
        self.pipeline.set_stages(labels)
        self._stage_index_map = index_map
        self._stage_strategy = None
        self._stage_failed_indexes = set()

    # =====================================================================
    # Enable ADB over Ethernet (DoIP/UDS, see ivi_installer.diag)
    # =====================================================================

    def _build_enable_adb_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        v = QVBoxLayout(body)
        v.setContentsMargins(18, 14, 18, 18)
        v.setSpacing(12)

        # ---- Header.
        header = QLabel("Enable ADB over Ethernet")
        header.setProperty("role", "title")
        header.setStyleSheet(
            f"color: {TOKENS[THEME]['fg']}; font-size: 20px; "
            f"font-weight: 600;"
        )
        v.addWidget(header)
        sub = QLabel(
            "Activate ADB through the diagnostic protocol on cars where "
            "USB-ADB is locked.")
        sub.setProperty("role", "muted")
        sub.setWordWrap(True)
        v.addWidget(sub)

        # ---- "When to use this" info card.
        when_card = QFrame()
        when_card.setObjectName("card")
        w = QVBoxLayout(when_card)
        w.setContentsMargins(14, 14, 14, 14)
        w.setSpacing(6)
        w.addWidget(self._title_label("ⓘ  When to use this"))
        when_text = QLabel(
            "If the Device tab can't see the IVI over USB and the head "
            "unit's settings have no ADB toggle — ADB is locked at "
            "the firmware level. This tab runs the same diagnostic "
            "routine (0x030C on ECU 0x0300) that the dealer "
            "diag tool fires when a technician unlocks ADB on the "
            "bench. Once it succeeds, the IVI's USB ADB endpoint "
            "comes back online and the Device tab will see it."
        )
        when_text.setWordWrap(True)
        when_text.setStyleSheet(
            f"color: {TOKENS[THEME]['fgMuted']}; line-height: 1.4;"
        )
        w.addWidget(when_text)
        v.addWidget(when_card)

        # ---- "Cable & network setup" info card.
        setup_card = QFrame()
        setup_card.setObjectName("card")
        s = QVBoxLayout(setup_card)
        s.setContentsMargins(14, 14, 14, 14)
        s.setSpacing(6)
        s.addWidget(self._title_label("🔌  Cable & network setup"))
        steps = QLabel(
            "<ol style='margin-left: -20px; line-height: 1.55;'>"
            "<li>Plug a USB-Ethernet adapter into the laptop "
            "(any Realtek / ASIX chipset works).</li>"
            "<li>Run an OBD-II ↔ RJ45 cable into the car's OBD port "
            "(diagnostic Ethernet, pins 3 + 11 / 12 + 13 — same "
            "pin-out as a BMW ENET cable).</li>"
            "<li>Ignition <b>ON</b> (engine doesn't need to run).</li>"
            "<li>On macOS, open <i>System Settings → Network</i>; on "
            "Windows, <i>Adapter Properties → IPv4</i>:"
            "<ul style='margin-top: 4px'>"
            "<li><b>DHCP first</b> — the gateway hands out an "
            "address on most cars.</li>"
            "<li>If DHCP doesn't bite, set a static IP "
            "<code>192.168.69.71</code>, mask "
            "<code>255.255.255.0</code>, leave gateway/DNS blank.</li>"
            "</ul></li>"
            "<li>Disable the VPN if it routes "
            "<code>192.168.69.0/24</code>.</li>"
            "<li>Quit Avatr Service Diagnostic if it's running — it "
            "holds an exclusive TLS session and your handshake will "
            "stall.</li>"
            "<li>After connecting, the laptop should sit on a "
            "<code>192.168.69.x</code> address; the car's gateway "
            "usually answers on <code>192.168.69.6</code>.</li>"
            "</ol>"
        )
        steps.setTextFormat(Qt.RichText)
        steps.setWordWrap(True)
        steps.setStyleSheet(
            f"color: {TOKENS[THEME]['fgMuted']};"
        )
        s.addWidget(steps)
        v.addWidget(setup_card)

        # ---- Run card: IPs + buttons + pipeline.
        run_card = QFrame()
        run_card.setObjectName("card")
        c = QVBoxLayout(run_card)
        c.setContentsMargins(14, 14, 14, 14)
        c.setSpacing(8)
        c.addWidget(self._title_label("▶  Run"))
        c.addWidget(self._muted_label(
            "Defaults match Avatr/Deepal factory firmware. If the "
            "fields don't work for your car, hit Discover — it "
            "broadcasts a DoIP VehicleIdentificationRequest and "
            "auto-fills the gateway IP."))

        ip_grid = QGridLayout()
        ip_grid.setHorizontalSpacing(10)
        ip_grid.setVerticalSpacing(6)
        ip_grid.setColumnStretch(1, 1)

        ip_grid.addWidget(QLabel("DoIP gateway:"), 0, 0)
        self.diag_doip_input = QLineEdit()
        self.diag_doip_input.setPlaceholderText(diag.DEFAULT_DOIP_GATEWAY)
        self.diag_doip_input.setText(
            settings.get(SETTING_DOIP_GATEWAY) or diag.DEFAULT_DOIP_GATEWAY)
        ip_grid.addWidget(self.diag_doip_input, 0, 1)

        ip_grid.addWidget(QLabel("TLS gateway:"), 1, 0)
        self.diag_tls_input = QLineEdit()
        self.diag_tls_input.setPlaceholderText(diag.DEFAULT_TLS_GATEWAY)
        self.diag_tls_input.setText(
            settings.get(SETTING_TLS_GATEWAY) or diag.DEFAULT_TLS_GATEWAY)
        ip_grid.addWidget(self.diag_tls_input, 1, 1)

        self.diag_discover_button = QPushButton("Discover")
        self.diag_discover_button.setToolTip(
            "Broadcast a DoIP VehicleIdentificationRequest and auto-fill "
            "the DoIP gateway from the first responder.")
        self.diag_discover_button.clicked.connect(self._on_diag_discover)
        ip_grid.addWidget(self.diag_discover_button, 0, 2, 2, 1)

        c.addLayout(ip_grid)

        cta_row = QHBoxLayout()
        self.diag_enable_button = QPushButton("Enable ADB")
        self.diag_enable_button.setObjectName("primary")
        self.diag_enable_button.setCursor(Qt.PointingHandCursor)
        self.diag_enable_button.clicked.connect(self._on_enable_adb)
        cta_row.addWidget(self.diag_enable_button)
        self.diag_disable_button = QPushButton("Disable ADB")
        self.diag_disable_button.setCursor(Qt.PointingHandCursor)
        self.diag_disable_button.setToolTip(
            "Send UDS stopRoutine on 0x030C (ISO 14229 sub-function "
            "0x02). Safest plausible reverse of Enable. If the ECU "
            "doesn't support it, you'll see a clean NRC byte in the "
            "pipeline and nothing else changes."
        )
        self.diag_disable_button.clicked.connect(self._on_disable_adb)
        cta_row.addWidget(self.diag_disable_button)
        cta_row.addStretch(1)
        c.addLayout(cta_row)

        self.diag_status_label = QLabel("Idle")
        self.diag_status_label.setProperty("mono", "true")
        self.diag_status_label.setWordWrap(True)
        self.diag_status_label.setStyleSheet(
            f"font-family: '{mono_family()}'; "
            f"color: {TOKENS[THEME]['fgMuted']};"
            f"background: {TOKENS[THEME]['bgSunken']}; "
            f"border: 1px solid {TOKENS[THEME]['border']}; "
            f"border-radius: 6px; padding: 8px 10px;"
        )
        c.addWidget(self.diag_status_label)

        # ---- Pipeline of the six enable-ADB stages, inside the run card.
        c.addSpacing(4)
        c.addWidget(_section_label("Pipeline"))
        self.diag_pipeline = Pipeline(diag.STAGES, THEME, self)
        c.addWidget(self.diag_pipeline)

        v.addWidget(run_card)
        v.addStretch(1)

        scroll.setWidget(body)
        return scroll

    def _on_diag_discover(self) -> None:
        if self._busy:
            self._toast("Wait for the current operation to finish.",
                        kind="warn")
            return
        self.diag_discover_button.setEnabled(False)
        self.diag_status_label.setText("Discovering DoIP gateways…")
        worker = workers.DiscoverGatewaysWorker(timeout=2.0)
        worker.result.connect(self._on_diag_discover_result)
        worker.error.connect(self._on_diag_discover_error)
        worker.log_line.connect(self._log_full)
        worker.finished.connect(
            lambda: self.diag_discover_button.setEnabled(True))
        self._active_worker = worker
        self._active_thread = workers.run_in_thread(worker)

    def _on_diag_discover_result(self, gws: list) -> None:
        if not gws:
            self.diag_status_label.setText(
                "No gateways answered. Check the cable, ignition, "
                "and the laptop's IP address.")
            return
        first = gws[0]
        self.diag_doip_input.setText(first.ip)
        self.diag_status_label.setText(
            f"Found {len(gws)} gateway(s). DoIP IP set to {first.ip} "
            f"(VIN {first.vin}, logical 0x{first.logical_addr:04x})."
        )

    def _on_diag_discover_error(self, msg: str) -> None:
        self.diag_status_label.setText(f"Discover failed: {msg}")

    def _on_enable_adb(self) -> None:
        self._run_diag_action("enable")

    def _on_disable_adb(self) -> None:
        self._run_diag_action("disable")

    def _run_diag_action(self, action: str) -> None:
        if self._busy:
            self._toast("Wait for the current operation to finish.",
                        kind="warn")
            return
        doip_ip = (self.diag_doip_input.text().strip()
                    or diag.DEFAULT_DOIP_GATEWAY)
        tls_ip = (self.diag_tls_input.text().strip()
                   or diag.DEFAULT_TLS_GATEWAY)
        settings.set(SETTING_DOIP_GATEWAY, doip_ip)
        settings.set(SETTING_TLS_GATEWAY, tls_ip)

        self._busy = True
        self.diag_enable_button.setEnabled(False)
        self.diag_disable_button.setEnabled(False)
        self.diag_discover_button.setEnabled(False)
        # Rebuild pipeline labels so the trailing stage matches the action.
        self.diag_pipeline.set_stages(diag.stages_for(action))
        self.diag_status_label.setText(
            f"Running {action}… DoIP={doip_ip}, TLS={tls_ip}")
        self._log_full(
            f"--- {action.title()} ADB sequence: DoIP={doip_ip} TLS={tls_ip} ---")
        self._log_user(f"{action.title()} ADB…")

        worker = workers.EnableAdbWorker(
            doip_gateway=doip_ip, tls_gateway=tls_ip, action=action)
        worker.stage.connect(self._on_diag_stage)
        worker.log_line.connect(self._log_full)
        worker.result.connect(self._on_enable_adb_result)
        worker.error.connect(self._on_enable_adb_error)
        worker.finished.connect(self._on_enable_adb_finished)
        self._active_worker = worker
        self._active_thread = workers.run_in_thread(worker)

    def _on_diag_stage(self, idx: int, state: str, hint: str) -> None:
        p = self.diag_pipeline
        if state == "running":
            p.mark_running(idx, hint or None)
        elif state == "done":
            p.mark_done(idx, hint or None)
        elif state == "failed":
            p.mark_failed(idx, hint or None)
        elif state == "skipped":
            p.mark_skipped(idx, hint or None)

    def _on_enable_adb_result(self, ok: bool, msg: str) -> None:
        self.diag_status_label.setText(msg)
        if ok:
            self._toast("ADB enabled. Plug in USB.", kind="info")
            self._log_full(f"+++ {msg}")
            self._log_user("✔ ADB enabled")
            QTimer.singleShot(500, self._refresh_now)
        else:
            self._toast("Enable ADB failed.", kind="error")
            self._log_full(f"!!! {msg}")
            self._log_user("✘ Enable ADB failed")

    def _on_enable_adb_error(self, msg: str) -> None:
        self.diag_status_label.setText(f"Worker error: {msg}")
        self._log_full(f"!!! Worker error: {msg}")
        self._toast("Enable ADB crashed.", kind="error")

    def _on_enable_adb_finished(self) -> None:
        self._busy = False
        self.diag_enable_button.setEnabled(True)
        self.diag_disable_button.setEnabled(True)
        self.diag_discover_button.setEnabled(True)

    # =====================================================================
    # AppGallery (hidden surface; reachable via Cmd/Ctrl+Shift+D).
    # =====================================================================

    def _prompt_appgallery(self) -> None:
        """Ctrl+Shift+D — jump to the URL source tab and focus the input."""
        if hasattr(self, "_source_tab_url"):
            self._source_tab_url.setChecked(True)
        if hasattr(self, "appgallery_input"):
            self.appgallery_input.setFocus()
            self.appgallery_input.selectAll()

    def _update_appgallery_button(self) -> None:
        """Enable the Download button only when the input parses to a
        valid AppGallery id (and we're not already busy)."""
        if not hasattr(self, "appgallery_button"):
            return
        from ..sources import appgallery as _appgallery
        raw = (self.appgallery_input.text().strip()
               if hasattr(self, "appgallery_input") else "")
        ok = bool(_appgallery.parse_app_id(raw)) and not self._busy
        self.appgallery_button.setEnabled(ok)

    def _on_appgallery_download(self) -> None:
        if self._busy:
            return
        from ..sources import appgallery
        raw = self.appgallery_input.text().strip()
        if not raw:
            self._toast("Paste a Huawei AppGallery link or id first",
                        kind="warn")
            return
        app_id = appgallery.parse_app_id(raw)
        if not app_id:
            self._toast("Couldn't read a 'C12345' id from that input",
                        kind="error")
            return
        out_dir = Path.home() / "Downloads" / "ivi-installer"
        self._set_busy(True)
        self.appgallery_progress.setVisible(True)
        self.appgallery_progress.setValue(0)
        self.appgallery_progress.setFormat(f"{app_id}: %p%")
        self._log_full(f"=== AppGallery download {app_id} → {out_dir} ===")
        self._log_user(f"Downloading {app_id}…")
        worker = workers.AppGalleryDownloadWorker(raw, out_dir)
        worker.log_line.connect(self._log_full)
        worker.progress.connect(self._on_appgallery_progress)
        worker.result.connect(self._on_appgallery_result)
        worker.error.connect(self._on_appgallery_error)
        thread = workers.run_in_thread(worker)
        worker.finished.connect(lambda: self._on_op_finished(thread, worker))
        self._active_thread = thread
        self._active_worker = worker

    def _on_appgallery_progress(self, seen: int, total: int) -> None:
        if total > 0:
            pct = int(seen * 100 / total)
            self.appgallery_progress.setValue(min(pct, 100))
        else:
            self.appgallery_progress.setFormat(
                f"{seen / 1024 / 1024:.1f} MB downloaded")

    def _on_appgallery_result(self, local_path: str) -> None:
        path = Path(local_path)
        self._log_full(
            f"✔ Downloaded {path.name} ({path.stat().st_size:,} bytes)")
        self._log_user(f"✔ Downloaded {path.name}")
        self._toast(f"Downloaded → {path.name}", kind="info")
        self.appgallery_progress.setValue(100)
        # Stage the file. `_set_apk_file` hides the whole source-card
        # (URL input + tabs) and renders the compact APK card in its
        # place — same surface the user sees after a local pick. From
        # there it's one click on the already-visible Install CTA.
        self._set_apk_file(path)
        QTimer.singleShot(800, lambda: self.appgallery_progress.setVisible(False))

    def _on_appgallery_error(self, msg: str) -> None:
        self._log_full(f"✘ AppGallery: {msg}")
        self._log_user("✘ Download failed")
        self._toast(f"Download failed: {msg}", kind="error")
        self.appgallery_progress.setVisible(False)

    # =====================================================================
    # Keyboards-tab handlers (unchanged from pre-redesign)
    # =====================================================================

    def _bundled_celia_path(self) -> Path | None:
        try:
            from importlib import resources as _resources
        except ImportError:
            return None
        try:
            with _resources.files("ivi_installer.resources").joinpath(
                    CELIA_APK_RESOURCE).open("rb") as fh:
                data = fh.read()
        except Exception:
            return None
        if hasattr(self, "_celia_cache") and self._celia_cache is not None:
            return self._celia_cache
        import tempfile as _tempfile
        tmp = Path(_tempfile.mkdtemp(prefix="ivi-celia-"))
        target = tmp / CELIA_APK_RESOURCE
        target.write_bytes(data)
        self._celia_cache = target
        return target

    def _on_install_celia(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        path = self._bundled_celia_path()
        if path is None:
            QMessageBox.critical(
                self, "Celia APK missing",
                "The bundled Celia APK couldn't be loaded — the app "
                "build is incomplete. Reinstall IVI Installer.",
            )
            return
        self._set_busy(True)
        self._reset_attempt_states()
        self.kb_status_label.setText("Status: installing Celia…")
        self._log_full(f"=== install Celia Keyboard from {path} ===")
        self._log_user("Installing keyboard…")
        worker = workers.InstallWorker(
            file_path=path,
            serial=self._selected_serial,
            grant_runtime=True,
            target_user=None,
            preferred_installer=None,
            strategy="auto",
            primary_strategy=self._selected_primary_strategy(),
            force_reinstall=False,
        )
        worker.log_line.connect(self._log_full)
        worker.attempt.connect(self._on_install_attempt)
        worker.error.connect(self._on_celia_install_error)
        worker.result.connect(self._on_celia_install_result)
        thread = workers.run_in_thread(worker)
        worker.finished.connect(lambda: self._on_op_finished(thread, worker))
        self._active_thread = thread
        self._active_worker = worker

    def _on_celia_install_result(self, result: CascadedInstallResult) -> None:
        if not result.success:
            self.kb_status_label.setText(f"✘ install failed: {result.message}")
            self._log_full(f"✘ Celia install: {result.message}")
            self._log_user("✘ Keyboard install failed")
            return
        self._log_full("✔ Celia installed; running IME enrollment…")
        self._log_user("✔ Keyboard installed")
        self._enroll_celia(set_default=self.kb_set_default_check.isChecked())

    def _on_celia_install_error(self, msg: str) -> None:
        self.kb_status_label.setText(f"✘ {msg}")
        self._log_full(f"✘ Celia install error: {msg}")
        self._log_user("✘ Keyboard install failed")

    def _on_enroll_celia(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        self._enroll_celia(set_default=self.kb_set_default_check.isChecked())

    def _enroll_celia(self, *, set_default: bool) -> None:
        from .. import strategies
        users = [0, *strategies._live_screen_users(self._selected_serial)]
        self.kb_status_label.setText("Status: enabling Celia as IME…")
        self._log_full(f"=== enable IME {CELIA_IME_ID} for users {users} "
                       f"(default: {set_default}) ===")
        self._log_user("Activating keyboard…")
        worker = workers.IMEEnableWorker(
            serial=self._selected_serial,
            ime_id=CELIA_IME_ID, users=users,
            set_as_default=set_default,
        )
        worker.log_line.connect(self._log_full)
        worker.error.connect(self._on_celia_install_error)
        worker.result.connect(self._on_celia_enroll_result)
        thread = workers.run_in_thread(worker)
        worker.finished.connect(lambda: self._on_op_finished(thread, worker))
        self._active_thread = thread
        self._active_worker = worker

    def _on_celia_enroll_result(self, serial, ime_id, enable, default) -> None:
        if serial != self._selected_serial:
            return
        ok_count = sum(1 for v in enable.values() if v)
        fail_count = len(enable) - ok_count
        msg = f"✔ enabled on {ok_count}/{len(enable)} users"
        if default:
            d_ok = sum(1 for v in default.values() if v)
            msg += f"; set as default on {d_ok}/{len(default)} users"
        self.kb_status_label.setText(f"Status: {msg}")
        self._log_full(f"Celia: {msg}")
        self._log_user("✔ Keyboard activated")
        self._on_refresh_imes()

    def _on_refresh_imes(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        self.ime_refresh_button.setEnabled(False)
        self.ime_list.clear()
        self.ime_list.addItem("Loading…")
        from .. import strategies
        import threading as _threading

        def _probe() -> None:
            try:
                imes = strategies.list_input_methods(
                    self._selected_serial, user=0)
            except Exception as e:
                imes = []
                err = f"{type(e).__name__}: {e}"
            else:
                err = None
            QTimer.singleShot(0, lambda: self._render_imes(imes, err))

        _threading.Thread(target=_probe, daemon=True).start()

    def _render_imes(self, imes, err) -> None:
        self.ime_list.clear()
        if err:
            self.ime_list.addItem(f"✘ {err}")
        elif not imes:
            self.ime_list.addItem("(no IMEs reported by `ime list`)")
        else:
            for ime_id, enabled in imes:
                tick = "✓" if enabled else "·"
                self.ime_list.addItem(f"{tick} {ime_id}")
        self.ime_refresh_button.setEnabled(True)

    # =====================================================================
    # Tools-tab handlers
    # =====================================================================

    def _kick_pkg_list(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        self.grant_pkg_refresh.setEnabled(False)
        worker = workers.ThirdPartyPackagesWorker(self._selected_serial)
        worker.result.connect(self._on_pkg_list)
        worker.error.connect(self._on_worker_error)
        thread = workers.run_in_thread(worker)
        self._inflight.append((thread, worker))
        worker.finished.connect(
            lambda: (self.grant_pkg_refresh.setEnabled(True),
                      self._drop_refs(thread, worker)))

    def _on_pkg_list(self, serial, packages) -> None:
        if serial != self._selected_serial:
            return
        prev = self.grant_pkg_combo.currentText()
        self.grant_pkg_combo.blockSignals(True)
        self.grant_pkg_combo.clear()
        for p in packages:
            self.grant_pkg_combo.addItem(p)
        if prev:
            idx = self.grant_pkg_combo.findText(prev)
            if idx >= 0:
                self.grant_pkg_combo.setCurrentIndex(idx)
            else:
                self.grant_pkg_combo.setEditText(prev)
        self.grant_pkg_combo.blockSignals(False)
        self._log_full(
            f"Refreshed package list — {len(packages)} third-party packages")
        self._log_user(f"Apps list refreshed ({len(packages)})")

    def _on_grant_perms(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        package = self.grant_pkg_combo.currentText().strip()
        if not package:
            self._toast("Type or pick a package name first", kind="warn")
            return
        self._set_busy(True)
        self.grant_status_label.setText("Running…")
        self._log_full(f"=== grant runtime perms for {package} ===")
        self._log_user(f"Granting permissions for {package}…")
        worker = workers.GrantRuntimePermsWorker(
            serial=self._selected_serial, package=package,
        )
        worker.log_line.connect(self._log_full)
        worker.error.connect(self._on_grant_error)
        worker.result.connect(self._on_grant_result)
        thread = workers.run_in_thread(worker)
        worker.finished.connect(lambda: self._on_op_finished(thread, worker))
        self._active_thread = thread
        self._active_worker = worker

    def _on_grant_result(self, serial, package, summary) -> None:
        if not summary:
            self.grant_status_label.setText(
                f"⚠ {package}: no perms granted (package not installed, "
                f"no dangerous perms requested, or helper failed)"
            )
            self._log_full(f"⚠ no perms granted for {package}")
            self._log_user(f"⚠ No permissions granted for {package}")
            return
        ok_total = sum(ok for ok, _ in summary.values())
        fail_total = sum(fail for _, fail in summary.values())
        users_touched = ", ".join(str(u) for u in sorted(summary.keys()))
        if fail_total == 0:
            msg = f"✔ {package}: granted {ok_total} perm(s) across users {users_touched}"
        else:
            msg = (f"⚠ {package}: granted {ok_total}, "
                   f"failed {fail_total} (users {users_touched})")
        self.grant_status_label.setText(msg)
        self._log_full(msg)
        if fail_total == 0:
            self._log_user(f"✔ Permissions granted for {package}")
        else:
            self._log_user(f"⚠ Permissions partially granted for {package}")

    def _on_grant_error(self, message) -> None:
        self.grant_status_label.setText(f"✘ {message}")
        self._log_full(f"✘ grant: {message}")
        self._log_user("✘ Granting permissions failed")

    def _on_bypass_refresh(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        self.bypass_refresh_button.setEnabled(False)
        self.bypass_redeploy_button.setEnabled(False)
        self.bypass_status_label.setText("Status: probing…")
        worker = workers.BrokerHealthWorker(self._selected_serial)
        worker.result.connect(self._on_bypass_status)
        worker.error.connect(self._on_bypass_error)
        thread = workers.run_in_thread(worker)
        self._inflight.append((thread, worker))
        worker.finished.connect(
            lambda: (self._update_install_button(),
                      self._drop_refs(thread, worker)))

    def _on_bypass_status(self, serial, status) -> None:
        if serial != self._selected_serial:
            return
        self.bypass_status_label.setText(_format_broker_status(status))

    def _on_bypass_error(self, message) -> None:
        self.bypass_status_label.setText(f"✘ {message}")
        self._log_full(f"✘ bypass health: {message}")

    def _on_bypass_redeploy(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        confirm = QMessageBox.question(
            self, "Redeploy broker?",
            "This pushes the bundled avatr-hdb-broker.jar to "
            "/data/local/tmp/ and restarts the on-device daemon.\n\n"
            "Existing installs are unaffected; the broker is just the "
            "loopback service the install pipeline talks to.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        self._set_busy(True)
        self.bypass_status_label.setText("Status: redeploying…")
        self._log_full("=== redeploy AvatrHdbBroker ===")
        worker = workers.BrokerRedeployWorker(self._selected_serial)
        worker.log_line.connect(self._log_full)
        worker.result.connect(self._on_bypass_redeploy_result)
        worker.error.connect(self._on_bypass_error)
        thread = workers.run_in_thread(worker)
        worker.finished.connect(lambda: self._on_op_finished(thread, worker))
        self._active_thread = thread
        self._active_worker = worker

    def _on_bypass_redeploy_result(self, serial, ok) -> None:
        if ok:
            self._log_full("✔ broker redeployed and PINGing.")
            self._toast("Broker redeployed", kind="info")
        else:
            self._log_full("✘ broker redeploy failed — see log.")
            self._toast("Broker redeploy failed", kind="error")
        QTimer.singleShot(200, self._on_bypass_refresh)

    # =====================================================================
    # Polling lifecycle
    # =====================================================================

    def start_polling(self) -> None:
        if self._poller is not None:
            return
        worker = DevicePollerWorker(interval_ms=DEVICE_POLL_MS)
        worker.status.connect(self._on_status)
        worker.error.connect(self._on_worker_error)
        worker.start()
        self._poller = worker
        QTimer.singleShot(50, self._ensure_adb_present)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._poller is not None:
            self._poller.stop()
            self._poller.wait(2000)
            self._poller = None
        super().closeEvent(event)

    # =====================================================================
    # Logging / toast helpers
    # =====================================================================

    def _log_full(self, line: str) -> None:
        # Verbose breadcrumb — file only. Never reaches the on-screen
        # log pane. This is where worker.log_line emissions land
        # (commands, device replies, internal phase detail).
        log.info(line)

    def _log_user(self, line: str) -> None:
        # Short, sanitized message shown to the user. Also recorded in
        # the file log so the support transcript has the same anchors
        # the user saw, in order.
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_view.appendPlainText(f"[{ts}] {line}")
        log.info("[user] %s", line)

    def _toast(self, message: str, *, kind: str = "info") -> None:
        prefix = {"info": "🟢", "warn": "🟡", "error": "🔴"}.get(kind, "")
        self.statusBar().showMessage(f"{prefix} {message}".strip(), TOAST_MS)

    def _copy_log(self) -> None:
        # Copy the FULL on-disk log, not just the visible short summary,
        # so the user can paste a complete diagnostic transcript even
        # though the on-screen pane intentionally shows very little.
        try:
            text = logging_setup.current_log_file().read_text(
                encoding="utf-8", errors="replace")
        except OSError as e:
            self._log_user(f"⚠ couldn't read log file: {e}")
            return
        QApplication.clipboard().setText(text)
        self._log_user("Log copied to clipboard.")

    def _save_log(self) -> None:
        from shutil import copyfile
        src = logging_setup.current_log_file()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        suggested = str(Path.home() / f"ivi-installer-{ts}.log")
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Save log", suggested, "Log files (*.log);;All files (*)",
        )
        if not path_str:
            return
        try:
            copyfile(src, path_str)
            self._log_user(f"Saved log to {path_str}")
            self._toast(f"Saved log → {path_str}", kind="info")
        except OSError as e:
            self._log_user(f"⚠ failed to save log: {e}")
            QMessageBox.critical(self, "Save log failed", str(e))

    def _show_log_file_in_finder(self) -> None:
        # Click target for the success-banner "View detailed log" link.
        path = logging_setup.current_log_file()
        QApplication.clipboard().setText(str(path))
        self._toast(f"Log path copied: {path}", kind="info")

    # =====================================================================
    # Gating
    # =====================================================================

    def _device_ready(self) -> bool:
        return (
            self._selected_serial is not None
            and self._capabilities is not None
        )

    def _update_install_button(self) -> None:
        device_ready = self._device_ready() and not self._busy
        screens_picked = bool(getattr(self, "_selected_screen_keys", None))
        if hasattr(self, "install_button"):
            ok = (device_ready and self._selected_path is not None
                   and screens_picked)
            self.install_button.setEnabled(ok)
            if hasattr(self, "_cta_hint"):
                if not device_ready:
                    self._cta_hint.setText("Connect a device to enable.")
                elif self._selected_path is None:
                    self._cta_hint.setText("Drop an APK to enable.")
                elif not screens_picked:
                    self._cta_hint.setText("Pick at least one screen.")
                else:
                    self._cta_hint.setText("")
        if hasattr(self, "diagnose_button"):
            self.diagnose_button.setEnabled(device_ready)
        if hasattr(self, "grant_button"):
            self.grant_button.setEnabled(device_ready)
        if hasattr(self, "grant_pkg_refresh"):
            self.grant_pkg_refresh.setEnabled(device_ready)
        if hasattr(self, "bypass_refresh_button"):
            self.bypass_refresh_button.setEnabled(device_ready)
        if hasattr(self, "bypass_redeploy_button"):
            self.bypass_redeploy_button.setEnabled(device_ready)
        if hasattr(self, "appgallery_button"):
            self._update_appgallery_button()
        if hasattr(self, "kb_install_button"):
            self.kb_install_button.setEnabled(device_ready)
        if hasattr(self, "kb_enroll_button"):
            self.kb_enroll_button.setEnabled(device_ready)
        if hasattr(self, "ime_refresh_button"):
            self.ime_refresh_button.setEnabled(device_ready)

    def _update_apply_tz_button(self) -> None:
        item = self.tz_list.currentItem() if hasattr(self, "tz_list") else None
        chosen = item.data(Qt.UserRole) if item else None
        ok = (
            self._device_ready()
            and chosen is not None
            and chosen != self._current_tz
            and not self._busy
        )
        if hasattr(self, "apply_tz_button"):
            self.apply_tz_button.setEnabled(ok)

    def _apply_capabilities(self, caps) -> None:
        self._update_install_button()
        self._update_apply_tz_button()

    # =====================================================================
    # Device status callbacks (unchanged)
    # =====================================================================

    def _on_status(self, status: DeviceStatus) -> None:
        self.status_widget.set_status(status)

        if not status.adb_present:
            if self._was_connected:
                self._toast("adb not found", kind="error")
                self._was_connected = False
            self._reset_device_state()
            return

        ready = [d for d in status.devices if d.is_ready]
        has_unauthorized = any(d.state == "unauthorized" for d in status.devices)
        if has_unauthorized and not self._was_unauthorized:
            self._toast("Device unauthorized — check phone screen", kind="warn")
        self._was_unauthorized = has_unauthorized

        if not ready and self._was_connected:
            self._toast("Device disconnected", kind="warn")
            self._reset_device_state()
            return

        if not ready or status.multiple or status.selected is None:
            self._capabilities = None
            self._info = None
            self._current_tz = None
            self._update_current_tz_label()
            if status.multiple and not self._was_connected:
                self._toast("Multiple devices connected — using first ready",
                             kind="warn")
            self._apply_capabilities(None)
            return

        new_serial = status.selected.serial
        prev_serial = self._selected_serial

        if prev_serial is not None and prev_serial != new_serial:
            confirmed = self._confirm_serial_change(prev_serial, new_serial)
            if not confirmed:
                if self._poller is not None:
                    self._poller.set_preferred_serial(prev_serial)
                return
            if self._selected_path is not None:
                self._clear_file()

        serial_changed = new_serial != prev_serial
        self._selected_serial = new_serial
        self._info = status.info
        self._capabilities = status.capabilities
        if self._poller is not None:
            self._poller.set_preferred_serial(new_serial)

        if not self._was_connected:
            label = (status.info.label if status.info else None) \
                or status.selected.product or "Device"
            self._log_full(f"Device connected: {label} ({new_serial})")
            self._log_user(f"Device connected: {label}")
            self._toast(f"{label} connected", kind="info")
            self._was_connected = True

        if serial_changed:
            self._kick_tz_read()
            self._kick_screen_categories_probe()
            # Drop the previous device's package list. If the user is
            # already on the Tools tab, refresh immediately; otherwise
            # the next tab-open will repopulate via _on_tab_changed.
            self.grant_pkg_combo.clear()
            if self.tabs.tabText(self.tabs.currentIndex()) == "Tools":
                self._kick_pkg_list()

        self._apply_capabilities(status.capabilities)

    def _reset_device_state(self) -> None:
        self._capabilities = None
        self._info = None
        self._selected_serial = None
        self._current_tz = None
        self._was_connected = False
        self._update_current_tz_label()
        self._apply_capabilities(None)

    def _confirm_serial_change(self, old: str, new: str) -> bool:
        reply = QMessageBox.question(
            self, "Connected device changed",
            f"Connected device changed:\n\nfrom  {old}\nto    {new}.\n\n"
            f"Continue with the new device?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        return reply == QMessageBox.Yes

    def _refresh_now(self) -> None:
        if self._poller is not None:
            self._poller.trigger_now()
        if self._device_ready():
            self._kick_tz_read()

    def _on_tab_changed(self, index: int) -> None:
        title = self.tabs.tabText(index)
        if not self._device_ready():
            return
        if title == "Timezone":
            self._kick_tz_read()
        elif title == "Device info":
            self._kick_device_info_read()
        elif title == "Keyboards":
            self._on_refresh_imes()
        elif title == "Tools":
            # Auto-populate the grant-perms package picker on first open
            # so the user doesn't have to remember the exact package name
            # (and doesn't have to know to click Refresh first).
            if self.grant_pkg_combo.count() == 0:
                self._kick_pkg_list()

    # =====================================================================
    # Install action
    # =====================================================================

    def _on_install(self, strategy: str | None = None) -> None:
        if self._selected_serial is None:
            return
        if strategy and strategy != "diagnose" and self._selected_path is None:
            return
        if strategy is None and self._selected_path is None:
            return

        is_diagnose = strategy == "diagnose"
        worker_strategy = (
            "diagnose" if is_diagnose
            else ("auto" if strategy is None else strategy)
        )
        primary_strategy = self._selected_primary_strategy() if not is_diagnose else None
        target_users = (
            None if is_diagnose else self._selected_target_users()
        )
        # Empty selection should never make it here (install button is
        # disabled), but guard anyway so we don't fire a no-op install.
        if not is_diagnose and target_users is not None and not target_users:
            return

        self._set_busy(True)
        self._reset_attempt_states()
        self.success_banner.hide()
        # Repaint stages for whichever strategy will run, filtered by
        # target_users so single-screen installs don't show the mirror
        # row. Backend stage events drive everything from here on.
        active_strategy = (
            worker_strategy if worker_strategy != "auto"
            else (primary_strategy or self._selected_primary_strategy())
        )
        labels, self._stage_index_map = self._pipeline_stages_for(
            active_strategy, target_users,
        )
        self.pipeline.set_stages(labels)
        self.pipeline.reset()
        self.grant_matrix.reset()
        self._stage_strategy = None
        self._stage_failed_indexes = set()
        self._stage_target_users = target_users

        if target_users is None:
            target_label = "all multimedia screens"
        elif len(target_users) == 1:
            target_label = f"user {target_users[0]}"
        else:
            target_label = (
                "users " + ", ".join(str(u) for u in target_users)
            )
        self._log_full(
            f"=== {worker_strategy} "
            f"({primary_strategy or '—'}): "
            f"{self._selected_path.name if self._selected_path else '(no apk)'} "
            f"→ {target_label} ==="
        )
        if is_diagnose:
            self._log_user("Running diagnostics…")
        elif self._selected_path is not None:
            self._log_user(f"Installing {self._selected_path.name}…")
        else:
            self._log_user("Installing…")
        logging_setup.dump_metadata_header({
            "device": self._selected_serial,
            "file": str(self._selected_path) if self._selected_path else "",
            "strategy": worker_strategy,
            "primary_strategy": primary_strategy or "",
            "target_users": (
                "" if target_users is None
                else ",".join(str(u) for u in target_users)
            ),
            "force_reinstall": (
                "true" if (
                    not is_diagnose and self.force_reinstall_check.isChecked()
                ) else "false"
            ),
            "installer_pkg": "",
        })

        worker = workers.InstallWorker(
            file_path=self._selected_path,
            serial=self._selected_serial,
            grant_runtime=True,
            target_users=target_users,
            preferred_installer=None,
            strategy=worker_strategy,
            primary_strategy=primary_strategy,
            force_reinstall=(
                not is_diagnose and self.force_reinstall_check.isChecked()
            ),
        )
        worker.log_line.connect(self._log_full)
        worker.attempt.connect(self._on_install_attempt)
        worker.error.connect(self._on_install_error)
        worker.result.connect(self._on_install_result)
        worker.stage.connect(self._on_install_stage)
        thread = workers.run_in_thread(worker)
        worker.finished.connect(lambda: self._on_op_finished(thread, worker))
        self._active_thread = thread
        self._active_worker = worker

    def _reset_attempt_states(self) -> None:
        if hasattr(self, "attempts_view"):
            self.attempts_view.clear()
        for label in self._strategy_status_labels.values():
            label.setText("Last run: —")

    _STATUS_ICONS = {
        AttemptStatus.SUCCESS: "✔",
        AttemptStatus.FAILED:  "✘",
        AttemptStatus.SKIPPED: "⏭",
        AttemptStatus.TERMINAL: "⛔",
    }
    _STATUS_COLOURS = {
        AttemptStatus.SUCCESS: "color: #2e7d32;",
        AttemptStatus.FAILED:  "color: #c62828;",
        AttemptStatus.SKIPPED: "color: gray;",
        AttemptStatus.TERMINAL: "color: #c62828; font-weight: bold;",
    }

    def _on_install_attempt(self, attempt: AttemptResult) -> None:
        icon = self._STATUS_ICONS.get(attempt.status, "·")
        line = (
            f"{icon} {attempt.strategy:<26}  "
            f"{attempt.status.value:<8}  "
            f"{attempt.duration_s:5.1f}s  {attempt.summary}"
        )
        if hasattr(self, "attempts_view"):
            self.attempts_view.appendPlainText(line)

    def _on_install_stage(self, event) -> None:
        """Drive the visual pipeline from a backend StageEvent.

        Strategy code emits hard-coded indexes (0..N for each
        strategy's full stage list); the UI translates those through
        ``_stage_index_map`` to visible row positions, which lets us
        hide irrelevant rows (e.g. mirror-to-passenger when the user
        picked a single screen). If a fallback strategy kicks in,
        swap the pipeline rows to its stage shape before processing.
        Hint copy must stay generic — see _StageReporter docstring.
        """
        if event.strategy != getattr(self, "_stage_strategy", None):
            self._stage_strategy = event.strategy
            self._stage_failed_indexes = set()
            labels, self._stage_index_map = self._pipeline_stages_for(
                event.strategy,
                getattr(self, "_stage_target_users", None),
            )
            self.pipeline.set_stages(labels)
            self.pipeline.reset()
        visible_idx = self._stage_index_map.get(event.index)
        if visible_idx is None:
            return  # event for a hidden stage (e.g. mirror on driver-only)
        if event.kind == "start":
            self.pipeline.mark_running(visible_idx, event.hint)
        elif event.kind == "done":
            self.pipeline.mark_done(visible_idx, event.hint, event.duration_ms)
        elif event.kind == "failed":
            self._stage_failed_indexes.add(visible_idx)
            self.pipeline.mark_failed(visible_idx, event.hint, event.duration_ms)
        elif event.kind == "skipped":
            self.pipeline.mark_skipped(visible_idx, event.hint)
        # Grant matrix payload — only emitted by the runtime-perms stage.
        if event.data and "summary" in event.data:
            self.grant_matrix.fill_from_summary(
                event.data["summary"],
                attempted_users=event.data.get("users"),
            )

    def _on_install_result(self, result: CascadedInstallResult) -> None:
        if result.success:
            self._log_full(f"✔ {result.message}")
            self._log_user("✔ Install complete")
            pkg = self._selected_path.stem if self._selected_path else "package"
            target_users = self._selected_target_users()
            if target_users is None:
                scope = "on every multimedia screen."
            elif len(target_users) == 1:
                scope = f"on user {target_users[0]}."
            else:
                scope = ("on users "
                         + ", ".join(str(u) for u in target_users) + ".")
            self.success_banner.show_success(
                package=pkg,
                scope=scope,
                tail="",
            )
            QMessageBox.information(self, "Install complete", result.message)
            return
        last = result.attempts[-1] if result.attempts else None
        hint = last.hint if last else None
        tail = f"\n\nHint: {hint}" if hint else ""
        self._log_full(f"✘ {result.message}")
        self._log_user("✘ Install failed")
        QMessageBox.critical(self, "Install failed",
                              f"{result.message}{tail}")

    def _on_install_error(self, msg: str) -> None:
        self._log_full(f"✘ {msg}")
        self._log_user("✘ Install error")
        # Surface the failure on whichever stage was running last.
        # We don't know its index for sure, so the backend's failed()
        # event is the source of truth; this is just a safety net for
        # crashes that bypass stage events entirely.
        QMessageBox.critical(self, "Install error", msg)

    # =====================================================================
    # Timezone actions (unchanged)
    # =====================================================================

    def _populate_tz_list(self) -> None:
        self.tz_list.clear()
        last = settings.get(SETTING_LAST_TZ)
        for tz in sorted(available_timezones()):
            item = QListWidgetItem(_format_tz_label(tz))
            item.setData(Qt.UserRole, tz)
            self.tz_list.addItem(item)
            if last and tz == last:
                self.tz_list.setCurrentItem(item)
                self.tz_list.scrollToItem(item)

    def _on_tz_filter(self, query: str) -> None:
        q = query.strip().lower()
        for i in range(self.tz_list.count()):
            item = self.tz_list.item(i)
            item.setHidden(bool(q) and q not in item.text().lower())

    def _kick_screen_categories_probe(self) -> None:
        """Pull `pm list users` to refresh Driver/Passenger/Rear mapping.

        The cards default to a hardcoded fallback layout; once the
        device replies with real names, ``categorize_screens`` builds
        a more accurate mapping (e.g. on Huawei firmwares the user
        names actually carry "Driver" / "Passenger" / "Rear" hints).
        Failures are silent — we keep the fallback in place.
        """
        if self._selected_serial is None or self._busy:
            return
        worker = workers.UsersListWorker(self._selected_serial)
        worker.result.connect(self._on_screen_categories_loaded)
        worker.error.connect(self._on_worker_error)
        thread = workers.run_in_thread(worker)
        self._inflight.append((thread, worker))
        worker.finished.connect(lambda: self._drop_refs(thread, worker))

    def _on_screen_categories_loaded(
        self, serial: str, users: list, displays: list,
    ) -> None:
        if serial != self._selected_serial:
            return
        new_categories = categorize_screens(users, displays=displays)
        self._screen_categories = new_categories
        # Pipeline rows depend on the resolved target — a category that
        # turns out to be empty would change the visible stage shape.
        self._refresh_pipeline_stages()
        self._refresh_screen_tooltips()
        self._update_install_button()

    def _kick_tz_read(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        worker = workers.TimezoneReadWorker(self._selected_serial)
        worker.result.connect(self._on_tz_read)
        worker.error.connect(self._on_worker_error)
        thread = workers.run_in_thread(worker)
        self._inflight.append((thread, worker))
        worker.finished.connect(lambda: self._drop_refs(thread, worker))

    def _on_tz_read(self, serial, tz) -> None:
        if serial != self._selected_serial:
            return
        self._current_tz = tz
        self._update_current_tz_label()
        self._update_apply_tz_button()

    def _update_current_tz_label(self) -> None:
        if not hasattr(self, "current_tz_label"):
            return
        if self._current_tz is None:
            self.current_tz_label.setText("—")
        elif not self._current_tz or self._current_tz == "UTC" \
                and self._selected_serial is None:
            self.current_tz_label.setText("Unknown")
        else:
            self.current_tz_label.setText(self._current_tz)

    def _on_apply_timezone(self) -> None:
        item = self.tz_list.currentItem()
        if item is None or self._selected_serial is None:
            return
        new_tz = item.data(Qt.UserRole)
        if new_tz == self._current_tz:
            return
        old = self._current_tz or "Unknown"
        confirm = QMessageBox.question(
            self, "Change timezone?",
            f"Change timezone from {old} to {new_tz}?\n\n"
            f"This will affect clock display and time-based notifications "
            f"on the device.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if confirm != QMessageBox.Yes:
            return
        self._set_busy(True)
        self._log_full(f"Setting timezone: {old} → {new_tz}")
        self._log_user(f"Changing timezone to {new_tz}…")
        worker = workers.TimezoneWriteWorker(self._selected_serial, new_tz)
        worker.log_line.connect(self._log_full)
        worker.error.connect(self._on_tz_write_error)
        worker.result.connect(self._on_tz_write_result)
        thread = workers.run_in_thread(worker)
        worker.finished.connect(lambda: self._on_op_finished(thread, worker))
        self._active_thread = thread
        self._active_worker = worker

    def _on_tz_write_result(self, ok, applied, verified) -> None:
        self._current_tz = verified
        self._update_current_tz_label()
        if ok:
            self._log_user(f"✔ Timezone is now {verified}")
            self._toast(f"✓ Timezone changed to {applied}", kind="info")
            settings.set(SETTING_LAST_TZ, applied)
        else:
            self._log_full(
                f"⚠ Sent {applied}, but device reports {verified}. "
                f"This device may not support runtime timezone change."
            )
            self._log_user(
                f"⚠ Sent {applied}, but device still reports {verified}"
            )
            self._toast(
                f"Timezone command sent, but device still reports {verified}",
                kind="warn",
            )

    def _on_tz_write_error(self, msg) -> None:
        self._log_full(f"✘ Timezone change failed: {msg}")
        self._log_user("✘ Timezone change failed")
        self._toast(f"Failed: {msg}", kind="error")

    # =====================================================================
    # Device-info actions (unchanged)
    # =====================================================================

    def _kick_device_info_read(self) -> None:
        if self._selected_serial is None or self._busy:
            return
        self.info_refresh_button.setEnabled(False)
        self.info_view.setPlainText("Loading…")
        worker = workers.DeviceInfoWorker(self._selected_serial)
        worker.result.connect(self._on_device_info_read)
        worker.error.connect(self._on_device_info_error)
        thread = workers.run_in_thread(worker)
        self._inflight.append((thread, worker))
        worker.finished.connect(
            lambda: (self.info_refresh_button.setEnabled(True),
                      self._drop_refs(thread, worker))
        )

    def _on_device_info_read(self, serial, sections) -> None:
        if serial != self._selected_serial:
            return
        self.info_view.setPlainText(device_info_module.format_sections(sections))

    def _on_device_info_error(self, msg) -> None:
        self.info_view.setPlainText(f"Failed to read device info: {msg}")
        self._log_full(f"⚠ device info: {msg}")

    def _copy_device_info(self) -> None:
        text = self.info_view.toPlainText()
        if text and text != "Loading…":
            QApplication.clipboard().setText(text)
            self._toast("Device info copied", kind="info")

    # =====================================================================
    # Shared op lifecycle
    # =====================================================================

    def _on_op_finished(self, thread, worker) -> None:
        self._set_busy(False)
        self._drop_refs(thread, worker)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if hasattr(self, "choose_button"):
            self.choose_button.setEnabled(not busy)
        if hasattr(self, "clear_button"):
            self.clear_button.setEnabled(not busy)
        if self._poller is not None:
            self._poller.set_paused(busy)
        if hasattr(self, "store_tab"):
            self.store_tab.set_global_busy(busy)
        self._update_install_button()
        self._update_apply_tz_button()
        if not busy:
            self._refresh_now()

    # =====================================================================
    # adb bootstrap
    # =====================================================================

    def _ensure_adb_present(self) -> None:
        if adb.find_adb() is not None:
            return
        confirm = QMessageBox.question(
            self, "adb not found",
            "Android platform-tools are not installed. Download them now "
            "(~5 MB from dl.google.com)?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
        )
        if confirm != QMessageBox.Yes:
            return
        worker = workers.EnsureAdbWorker()
        worker.progress.connect(
            lambda pct, msg: self._log_full(f"[adb-bootstrap {pct}%] {msg}"))
        worker.result.connect(
            lambda path: (self._log_full(f"adb installed at {path}"),
                           self._log_user("✔ ADB tools installed")))
        worker.error.connect(self._on_worker_error)
        thread = workers.run_in_thread(worker)
        self._inflight.append((thread, worker))
        worker.finished.connect(lambda: self._drop_refs(thread, worker))

    # =====================================================================
    # Generic
    # =====================================================================

    def _on_worker_error(self, msg) -> None:
        self._log_full(f"⚠ {msg}")

    def _drop_refs(self, thread, worker) -> None:
        thread.wait(2000)
        worker.deleteLater()
        thread.deleteLater()
        if self._active_thread is thread:
            self._active_thread = None
            self._active_worker = None
        try:
            self._inflight.remove((thread, worker))
        except ValueError:
            pass
