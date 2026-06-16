"""Store tab — curated catalog of apps known to work on the head unit.

Visual reference: ``design/store/store-handoff/`` (HTML mockup +
``README.md``). The tab is built on top of the existing source-resolver
plug-in interface (``ivi_installer.sources``) and the existing
``installer.install_cascade`` pipeline.

The tab owns three logical groups of widgets:

* **Toolbar** — search box, category dropdown, source dropdown,
  "tested only" pill, catalog version, refresh button.
* **Catalog list** — one ``AppRow`` per filtered ``CatalogEntry``,
  inside a ``QScrollArea``. Each row carries its own state machine
  (idle → resolving → downloading → installing → success | failed).
* **Footer** — "showing N of N" + last-updated timestamp.

Install state lives on the row, so two installs run in sequence (the
parent window keeps a single ``_busy`` flag); the design's progress
strip + per-row button states are driven by ``set_phase()``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QObject,
    QPoint,
    QRunnable,
    QStandardPaths,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QCursor,
    QPainter,
    QPalette,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import catalog as _catalog
from ..catalog import (
    CatalogEntry,
    SOURCE_DISPLAY,
)
from ..catalog_store import CatalogStore
from .theme import TOKENS, mono_family

log = logging.getLogger(__name__)

PHASE_IDLE = "idle"
PHASE_RESOLVING = "resolving"
PHASE_DOWNLOADING = "downloading"
PHASE_INSTALLING = "installing"
PHASE_SUCCESS = "success"
PHASE_FAILED = "failed"

SOURCE_FILTERS: list[tuple[str, str]] = [
    ("all", "All"),
    ("appgallery", "AppGallery"),
    ("direct", "Direct"),
]

# Type-ahead debounce — wait this many ms after the last keystroke
# before firing /edge/index/completeSearchWord. AppGallery's web app
# uses a similar window; below ~150 ms the cost-per-keystroke piles
# up without giving the user readable suggestions.
SEARCH_TYPEAHEAD_DEBOUNCE_MS = 200


# =========================================================================
# Icon loader — lazy QNetworkAccessManager with on-disk cache.
# =========================================================================


class _IconFetchSignals(QObject):
    """Signals proxy for ``_IconFetchTask`` — QRunnable can't itself
    declare signals."""
    fetched = Signal(str, bytes)    # url, raw_bytes (b"" on failure)


class _IconFetchTask(QRunnable):
    """Background HTTP GET of a single icon URL.

    Uses stdlib ``urllib`` rather than QtNetwork because the bundled
    macOS .app strips QtNetwork.framework + the TLS plugin to keep size
    down — see ``scripts/trim_qt.sh``. Stdlib ssl + the system trust
    store handle HTTPS without any Qt machinery.
    """

    def __init__(self, url: str, signals: _IconFetchSignals):
        super().__init__()
        self.url = url
        self._signals = signals

    def run(self) -> None:
        import ssl as _ssl
        import urllib.request as _urllib_request
        try:
            req = _urllib_request.Request(self.url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ivi-installer)",
            })
            ctx = _ssl.create_default_context()
            # Same trade-off as the AppGallery / direct resolvers: some
            # CDNs (notably Huawei's appimg.dbankcdn.com) ship cert
            # chains that don't resolve from the default desktop trust
            # store. Icon payloads are inert PNG bytes — nothing they
            # could do is dangerous.
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            with _urllib_request.urlopen(
                    req, context=ctx, timeout=15.0) as resp:
                data = resp.read(4 * 1024 * 1024)   # cap at 4 MiB
        except Exception as e:
            log.debug("icon fetch failed (%s): %s", self.url, e)
            data = b""
        try:
            self._signals.fetched.emit(self.url, data)
        except RuntimeError:
            # Owning IconLoader / signals object was destroyed before
            # we finished — the tab was closed mid-fetch. Drop on the
            # floor; the pixmap is no longer wanted.
            pass


class IconLoader(QObject):
    """Cache catalog icons in memory + on disk; emit a QPixmap when ready.

    A single loader instance is shared across all rows so the same URL
    is requested only once, and so a fetch in flight for a row that
    scrolls off-screen still benefits the next row that asks for the
    same icon (after, e.g., a category filter is re-applied).
    """
    icon_ready = Signal(str, QPixmap)   # url, pixmap

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._cache: dict[str, QPixmap] = {}
        self._inflight: set[str] = set()
        self._cache_dir = self._default_cache_dir()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._signals = _IconFetchSignals(self)
        self._signals.fetched.connect(self._on_fetched)
        self._pool = QThreadPool(self)
        # Cap concurrency so a tab full of icons doesn't fan out 40
        # simultaneous TLS handshakes.
        self._pool.setMaxThreadCount(4)

    @staticmethod
    def _default_cache_dir() -> Path:
        # Tests + first-launch fall back to a sane default if the
        # platform's cache root is unavailable.
        root = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        if not root:
            root = str(Path.home() / ".cache")
        return Path(root) / "ivi-installer" / "store-icons"

    def _disk_path(self, url: str) -> Path:
        import hashlib
        h = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{h}.png"

    def get(self, url: str) -> QPixmap | None:
        """Return the cached pixmap immediately, or kick off a fetch.

        If the icon isn't cached yet, ``icon_ready`` will fire later.
        """
        if not url:
            return None
        if url in self._cache:
            return self._cache[url]
        disk = self._disk_path(url)
        if disk.is_file():
            pm = QPixmap()
            if pm.load(str(disk)) and not pm.isNull():
                self._cache[url] = pm
                return pm
        if url not in self._inflight:
            self._inflight.add(url)
            self._pool.start(_IconFetchTask(url, self._signals))
        return None

    def _on_fetched(self, url: str, data: bytes) -> None:
        self._inflight.discard(url)
        if not data:
            return
        pm = QPixmap()
        if not pm.loadFromData(data) or pm.isNull():
            log.debug("icon decode failed for %s", url)
            return
        self._cache[url] = pm
        try:
            self._disk_path(url).write_bytes(data)
        except OSError as e:
            log.debug("icon disk-cache write failed: %s", e)
        self.icon_ready.emit(url, pm)


# =========================================================================
# Per-row widget
# =========================================================================


def _color_mix(theme: str, fg_token: str, fg_pct: float, bg_token: str) -> str:
    """Return a Qt-friendly hex blended like CSS color-mix(in oklch …).

    Qt has no oklch; sRGB-mix is good enough for the ~12 % / ~45 %
    background/border tints used in the design.
    """
    from PySide6.QtGui import QColor
    a = QColor(TOKENS[theme][fg_token])
    b = QColor(TOKENS[theme][bg_token])
    pct = max(0.0, min(1.0, fg_pct))
    r = int(a.red() * pct + b.red() * (1 - pct))
    g = int(a.green() * pct + b.green() * (1 - pct))
    bl = int(a.blue() * pct + b.blue() * (1 - pct))
    return QColor(r, g, bl).name()


class _ProgressStrip(QWidget):
    """2-pixel strip at the bottom of a row that fills 0→100%."""

    def __init__(self, theme: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._pct = 0.0
        self.setFixedHeight(2)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_progress(self, pct: float) -> None:
        self._pct = max(0.0, min(100.0, pct))
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        from PySide6.QtGui import QColor
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(TOKENS[self._theme]["bgSunken"]))
        if self._pct > 0:
            w = int(self.width() * self._pct / 100.0)
            p.fillRect(0, 0, w, self.height(),
                       QColor(TOKENS[self._theme]["accent"]))


class _IconPlate(QLabel):
    """40×40 rounded square. Renders initials until a real pixmap arrives."""

    def __init__(self, initials: str, theme: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._initials = initials or "··"
        self._pm: QPixmap | None = None
        self.setFixedSize(40, 40)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            f"QLabel {{ background: {TOKENS[theme]['bgSunken']}; "
            f"border: 1px solid {TOKENS[theme]['border']}; "
            f"border-radius: 8px; color: {TOKENS[theme]['fgMuted']}; "
            f"font-family: '{mono_family()}'; "
            f"font-size: 13px; font-weight: 600; }}"
        )
        self.setText(self._initials)

    def set_pixmap(self, pm: QPixmap) -> None:
        self._pm = pm
        if pm.isNull():
            self.setText(self._initials)
            return
        self.setText("")
        self.setPixmap(pm.scaled(
            38, 38, Qt.KeepAspectRatio, Qt.SmoothTransformation))


class AppRow(QFrame):
    """One catalog entry. Owns its own per-phase button state."""
    install_requested = Signal(object)   # CatalogEntry

    def __init__(
        self,
        entry: CatalogEntry,
        theme: str,
        icon_loader: IconLoader,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.entry = entry
        self._theme = theme
        self._phase = PHASE_IDLE
        self._progress_pct = 0.0
        self._icon_loader = icon_loader
        self.setObjectName("storeAppRow")
        self.setStyleSheet(
            f"#storeAppRow {{ background: transparent; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QWidget()
        body.setObjectName("storeAppRowBody")
        h = QHBoxLayout(body)
        h.setContentsMargins(14, 12, 14, 12)
        h.setSpacing(14)

        self._icon = _IconPlate(
            (entry.initials or _catalog.derive_initials(entry.name))[:3],
            theme, self)
        h.addWidget(self._icon, 0, Qt.AlignVCenter)

        # main column
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        name = QLabel(entry.name)
        name.setStyleSheet(
            f"color: {TOKENS[theme]['fg']}; font-size: 13px; font-weight: 600;"
        )
        title_row.addWidget(name)
        if entry.version:
            ver = QLabel(f"v{entry.version}")
            ver.setStyleSheet(
                f"color: {TOKENS[theme]['fgDim']}; "
                f"font-family: '{mono_family()}'; font-size: 11px;"
            )
            title_row.addWidget(ver)
        sep = QLabel("·")
        sep.setStyleSheet(f"color: {TOKENS[theme]['borderStrong']};")
        title_row.addWidget(sep)
        cat = QLabel(entry.category)
        cat.setStyleSheet(
            f"color: {TOKENS[theme]['fgMuted']}; "
            f"font-family: '{mono_family()}'; font-size: 11px;"
        )
        title_row.addWidget(cat)
        title_row.addStretch(1)
        col.addLayout(title_row)

        desc_text = entry.description("ru")
        desc = QLabel(desc_text)
        desc.setStyleSheet(
            f"color: {TOKENS[theme]['fgMuted']}; font-size: 12px;"
        )
        desc.setWordWrap(False)
        desc.setTextFormat(Qt.PlainText)
        desc.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        # Ellipsize with elide mode via QFontMetrics in resizeEvent
        desc.setMinimumWidth(0)
        desc.setMaximumWidth(16777215)
        self._desc_label = desc
        self._desc_full = desc_text
        col.addWidget(desc)

        meta_row = QHBoxLayout()
        meta_row.setContentsMargins(0, 2, 0, 0)
        meta_row.setSpacing(10)
        if entry.size_mb is not None:
            size = QLabel(f"{entry.size_mb:g} MB")
            size.setStyleSheet(
                f"color: {TOKENS[theme]['fgDim']}; "
                f"font-family: '{mono_family()}'; font-size: 10.5px;"
            )
            meta_row.addWidget(size)
        if entry.min_api is not None:
            sep2 = QLabel("·")
            sep2.setStyleSheet(f"color: {TOKENS[theme]['borderStrong']};")
            meta_row.addWidget(sep2)
            api = QLabel(f"min Android {entry.min_api}")
            api.setStyleSheet(
                f"color: {TOKENS[theme]['fgDim']}; "
                f"font-family: '{mono_family()}'; font-size: 10.5px;"
            )
            meta_row.addWidget(api)
        if entry.tested:
            sep3 = QLabel("·")
            sep3.setStyleSheet(f"color: {TOKENS[theme]['borderStrong']};")
            meta_row.addWidget(sep3)
            tested = QLabel("✓ tested")
            tested.setStyleSheet(
                f"color: {TOKENS[theme]['good']}; "
                f"font-family: '{mono_family()}'; font-size: 10.5px;"
            )
            meta_row.addWidget(tested)
        meta_row.addStretch(1)
        col.addLayout(meta_row)

        h.addLayout(col, stretch=1)

        # install button — vertically centred against the row body so it
        # sits in line with the title/description block, not pinned to
        # the top.
        self._button = QPushButton("Install")
        self._button.setObjectName("storeInstallBtn")
        self._button.setCursor(Qt.PointingHandCursor)
        self._button.setMinimumWidth(96)
        self._button.setFixedHeight(30)
        # Click-only — do NOT take keyboard focus on click. Otherwise
        # the busy lock (which calls setEnabled(False) on every row's
        # Install button) yanks focus off the clicked button, and
        # QScrollArea auto-scrolls to whatever focusable widget Qt
        # picks next in the tab chain — usually a button several rows
        # down. From the user's POV the page jumps "to the middle"
        # the moment they click Install, with no obvious cause.
        self._button.setFocusPolicy(Qt.NoFocus)
        self._button.clicked.connect(self._on_clicked)
        h.addWidget(self._button, 0, Qt.AlignVCenter)

        outer.addWidget(body)

        # progress strip (only visible while downloading)
        self._strip = _ProgressStrip(theme, self)
        self._strip.setVisible(False)
        outer.addWidget(self._strip)

        self._apply_button_style()

        # Lazy-load the icon from the remote URL. AppGallery serves
        # icons via their CDN and the disk cache makes second-paint
        # instant.
        if entry.icon_url:
            pm = self._icon_loader.get(entry.icon_url)
            if pm is not None:
                self._icon.set_pixmap(pm)
            self._icon_loader.icon_ready.connect(self._on_icon_ready)

    # ---- public ----

    def set_phase(self, phase: str, *, progress: float = 0.0) -> None:
        self._phase = phase
        self._progress_pct = progress
        self._strip.setVisible(phase == PHASE_DOWNLOADING)
        self._strip.set_progress(progress)
        self._apply_button_style()

    def set_progress(self, seen: int, total: int) -> None:
        if total > 0:
            self._progress_pct = max(
                0.0, min(100.0, seen * 100.0 / total))
        else:
            # Indeterminate — just bump the strip a little so the UI
            # acknowledges activity.
            self._progress_pct = min(95.0, self._progress_pct + 1.0)
        self._strip.set_progress(self._progress_pct)
        if self._phase == PHASE_DOWNLOADING:
            self._button.setText(f"{int(self._progress_pct)}%")

    def set_enabled_install(self, enabled: bool) -> None:
        if self._phase in (PHASE_RESOLVING, PHASE_DOWNLOADING, PHASE_INSTALLING):
            return
        self._button.setEnabled(enabled)

    # ---- internals ----

    def _on_clicked(self) -> None:
        if self._phase in (PHASE_RESOLVING, PHASE_DOWNLOADING, PHASE_INSTALLING):
            return
        self.install_requested.emit(self.entry)

    def _on_icon_ready(self, url: str, pm: QPixmap) -> None:
        if url == self.entry.icon_url and not pm.isNull():
            self._icon.set_pixmap(pm)

    def _apply_button_style(self) -> None:
        t = self._theme
        base = (
            f"QPushButton#storeInstallBtn {{ "
            f"background: {TOKENS[t]['bgRaised']}; "
            f"border: 1px solid {TOKENS[t]['border']}; "
            f"border-radius: 6px; padding: 0 14px; "
            f"font-size: 12.5px; font-weight: 600; }}"
        )
        if self._phase == PHASE_RESOLVING:
            self._button.setText("Resolving…")
            self._button.setEnabled(False)
            self._button.setStyleSheet(
                base + f"QPushButton#storeInstallBtn {{ "
                f"color: {TOKENS[t]['fgMuted']}; }}"
            )
        elif self._phase == PHASE_DOWNLOADING:
            self._button.setText(f"{int(self._progress_pct)}%")
            self._button.setEnabled(False)
            self._button.setStyleSheet(
                base + f"QPushButton#storeInstallBtn {{ "
                f"color: {TOKENS[t]['accent']}; "
                f"font-family: '{mono_family()}'; font-weight: 600; }}"
            )
        elif self._phase == PHASE_INSTALLING:
            self._button.setText("Installing…")
            self._button.setEnabled(False)
            tinted_bg = _color_mix(t, "accent", 0.08, "bgRaised")
            tinted_border = _color_mix(t, "accent", 0.50, "border")
            self._button.setStyleSheet(
                f"QPushButton#storeInstallBtn {{ "
                f"background: {tinted_bg}; border: 1px solid {tinted_border}; "
                f"border-radius: 6px; padding: 0 14px; "
                f"color: {TOKENS[t]['accent']}; "
                f"font-size: 12.5px; font-weight: 600; }}"
            )
        elif self._phase == PHASE_SUCCESS:
            self._button.setText("✓ Installed")
            self._button.setEnabled(True)
            tinted_bg = _color_mix(t, "good", 0.12, "bgRaised")
            tinted_border = _color_mix(t, "good", 0.45, "border")
            self._button.setStyleSheet(
                f"QPushButton#storeInstallBtn {{ "
                f"background: {tinted_bg}; border: 1px solid {tinted_border}; "
                f"border-radius: 6px; padding: 0 14px; "
                f"color: {TOKENS[t]['good']}; "
                f"font-size: 12.5px; font-weight: 600; }}"
            )
        elif self._phase == PHASE_FAILED:
            self._button.setText("× Retry")
            self._button.setEnabled(True)
            tinted_bg = _color_mix(t, "bad", 0.12, "bgRaised")
            tinted_border = _color_mix(t, "bad", 0.45, "border")
            self._button.setStyleSheet(
                f"QPushButton#storeInstallBtn {{ "
                f"background: {tinted_bg}; border: 1px solid {tinted_border}; "
                f"border-radius: 6px; padding: 0 14px; "
                f"color: {TOKENS[t]['bad']}; "
                f"font-size: 12.5px; font-weight: 600; }}"
            )
        else:
            self._button.setText("Install")
            self._button.setEnabled(True)
            self._button.setStyleSheet(
                base + f"QPushButton#storeInstallBtn {{ "
                f"color: {TOKENS[t]['fg']}; }}"
            )

    def resizeEvent(self, e):  # noqa: D401 — Qt API
        # Re-elide the description so it never wraps and never overflows.
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._desc_label.font())
        avail = max(0, self._desc_label.width())
        elided = fm.elidedText(self._desc_full, Qt.ElideRight, avail)
        self._desc_label.setText(elided)
        super().resizeEvent(e)


# =========================================================================
# Search-suggest popup
# =========================================================================


class _SearchSuggestPopup(QFrame):
    """Floating dropdown anchored under the search input.

    Two content modes:

    * ``hot`` — the AppGallery hot-keyword panel shown on focus when
      the input is empty.
    * ``typeahead`` — the per-keystroke result of
      ``/edge/index/completeSearchWord``: a list of keyword
      suggestions plus, when AppGallery has a confident match, a
      single app card the user can click to install directly.
    """
    keyword_clicked = Signal(str)
    app_clicked = Signal(dict)

    def __init__(self, *, anchor: QLineEdit, theme: str,
                 parent: QWidget | None = None):
        # Parented to the StoreTab so it lives in the same Z-stack —
        # NOT a top-level Qt.Popup window, because Popup grabs the
        # mouse and steals events from the search input. We dismiss
        # via outside-click + focus-out logic in the StoreTab.
        super().__init__(parent)
        self._anchor = anchor
        self._theme = theme
        self.setFocusPolicy(Qt.NoFocus)
        self.setObjectName("storeSuggestPopup")
        self.setStyleSheet(
            f"#storeSuggestPopup {{ background: {TOKENS[theme]['bgRaised']}; "
            f"border: 1px solid {TOKENS[theme]['border']}; "
            f"border-radius: 8px; }}"
        )
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(2)
        self.hide()

    def set_content(
        self,
        *,
        kind: str,
        keywords: list[str],
        top_app: dict | None,
    ) -> None:
        # Wipe and rebuild — a popup never holds more than ~10 entries
        # so churn is negligible compared to the network round-trip.
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        if kind == "hot":
            self._add_section_header("Popular searches")
        if top_app:
            appid = str(top_app.get("appid") or top_app.get("id") or "")
            name = str(top_app.get("name") or "")
            kind_name = str(top_app.get("kindName") or "")
            if appid and name:
                self._layout.addWidget(self._build_app_row(
                    item=top_app, name=name, kind_name=kind_name))
                if keywords:
                    self._add_separator()
        for kw in keywords:
            self._layout.addWidget(self._build_keyword_row(kw))

    def show_anchored(self) -> None:
        a = self._anchor
        parent = self.parentWidget()
        if parent is None:
            return
        # Compute the anchor's bottom-left in the parent's coordinate
        # space, then position the popup just below it. Width matches
        # the input so the popup feels glued to it.
        bottom_left = a.mapTo(parent, QPoint(0, a.height() + 2))
        self.setFixedWidth(max(a.width(), 240))
        self.adjustSize()
        self.move(bottom_left)
        if not self.isVisible():
            self.show()
        self.raise_()

    def _add_section_header(self, label: str) -> None:
        t = self._theme
        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color: {TOKENS[t]['fgDim']}; "
            f"font-family: '{mono_family()}'; font-size: 11px; "
            f"padding: 4px 8px;"
        )
        self._layout.addWidget(lbl)

    def _add_separator(self) -> None:
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(
            f"background: {TOKENS[self._theme]['border']};"
        )
        self._layout.addWidget(sep)

    def _build_keyword_row(self, keyword: str) -> QPushButton:
        t = self._theme
        btn = QPushButton(keyword)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFlat(True)
        btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"color: {TOKENS[t]['fg']}; font-size: 12.5px; "
            f"text-align: left; padding: 6px 8px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background: {TOKENS[t]['bgSunken']}; }}"
        )
        btn.clicked.connect(lambda _checked=False, kw=keyword:
                             self.keyword_clicked.emit(kw))
        return btn

    def _build_app_row(
        self, *, item: dict, name: str, kind_name: str,
    ) -> QPushButton:
        t = self._theme
        btn = QPushButton()
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFlat(True)
        btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"text-align: left; padding: 6px 8px; border-radius: 4px; }}"
            f"QPushButton:hover {{ background: {TOKENS[t]['bgSunken']}; }}"
        )
        h = QHBoxLayout(btn)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(8)
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(
            f"color: {TOKENS[t]['fg']}; font-size: 12.5px; "
            f"font-weight: 600; background: transparent;"
        )
        h.addWidget(name_lbl)
        if kind_name:
            sep = QLabel("·")
            sep.setStyleSheet(
                f"color: {TOKENS[t]['borderStrong']}; "
                f"background: transparent;"
            )
            h.addWidget(sep)
            kind_lbl = QLabel(kind_name)
            kind_lbl.setStyleSheet(
                f"color: {TOKENS[t]['fgMuted']}; font-size: 11.5px; "
                f"background: transparent;"
            )
            h.addWidget(kind_lbl)
        h.addStretch(1)
        btn.clicked.connect(lambda _checked=False, _it=item:
                             self.app_clicked.emit(_it))
        return btn


# =========================================================================
# Toolbar + states
# =========================================================================


class StoreTab(QWidget):
    """Top-level Store tab widget — toolbar + scrolling rows + footer."""

    install_requested = Signal(object)   # CatalogEntry
    refresh_requested = Signal()
    # Emitted when the user toggles the AppGallery source pill. Main
    # window owns the actual fetch / store-swap logic; the tab only
    # tells it the user's intent.
    appgallery_toggle_requested = Signal(bool)
    # Emitted when the search input is empty and gains focus — the
    # main window kicks an unauthed call to AppGallery's hot-keyword
    # endpoint and feeds the result back via :meth:`set_hot_keywords`.
    hot_keywords_requested = Signal()
    # Emitted on debounced text-changed when the input is non-empty.
    # Carries the trimmed keyword. The main window posts to
    # /edge/index/completeSearchWord and feeds the suggestions +
    # top-app match back via :meth:`set_search_suggestions`.
    search_suggestions_requested = Signal(str)
    # Emitted when the user clicks a top-match app card in the
    # suggestion dropdown. Carries the raw AppGallery item dict
    # (appid / package / name / icon / kindName / size / …). The
    # main window builds a synthetic CatalogEntry from it and runs
    # the existing install pipeline.
    suggested_app_install_requested = Signal(dict)

    # How many AppRow widgets to materialise per page. The full F-Droid
    # catalog ships ~4000 entries — building them all upfront takes
    # multiple seconds and burns ~50 MB of widget overhead. Render in
    # batches and append more as the user scrolls.
    BATCH_SIZE = 80
    # Trigger the next batch when the scroll bar is this fraction of
    # the way to the bottom (0..1). Slightly above 1.0 to disable.
    LOAD_MORE_THRESHOLD = 0.85

    def __init__(
        self,
        catalog: "_catalog.Catalog | CatalogStore | None",
        theme: str = "dark",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._theme = theme
        # Backed by a SQLite-flavoured CatalogStore. Three accepted
        # ``catalog`` shapes:
        #   * None — empty catalog (loading / error state).
        #   * CatalogStore — the live store, passed straight through
        #     (production: opened readonly off the F-Droid SQLite).
        #   * _catalog.Catalog — back-compat for tests / extras-only
        #     fallback. Wrapped into an in-memory CatalogStore so the
        #     rest of the tab only ever talks to the SQL API.
        self._store: CatalogStore | None = self._coerce_to_store(catalog)
        # ``_rows`` is the per-id cache of materialised AppRow widgets.
        # We don't build a row until it scrolls into view (or close to
        # it); rebuilding on filter change is cheap because we reuse
        # already-built rows.
        self._rows: dict[str, AppRow] = {}
        self._filtered_ids: list[str] = []
        self._rendered_ids: list[str] = []
        self._busy_app_id: str | None = None
        self._icon_loader = IconLoader(self)

        self._build_ui()
        self._apply_filters()

    @staticmethod
    def _coerce_to_store(
        catalog: "_catalog.Catalog | CatalogStore | None",
    ) -> CatalogStore | None:
        if catalog is None:
            return None
        if isinstance(catalog, CatalogStore):
            return catalog
        # Legacy path: a ``Catalog`` dataclass. Build an in-memory
        # SQLite from its entries so the rest of the tab uses one API.
        return CatalogStore.from_apps(
            catalog.apps,
            generated_at=getattr(catalog, "generated_at", "") or "",
        )

    # ---- UI ----

    def _build_ui(self) -> None:
        t = self._theme
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Toolbar
        toolbar = QFrame()
        toolbar.setObjectName("storeToolbar")
        toolbar.setStyleSheet(
            f"#storeToolbar {{ background: {TOKENS[t]['bgRaised']}; "
            f"border-bottom: 1px solid {TOKENS[t]['border']}; }}"
        )
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(18, 12, 18, 12)
        tb.setSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search apps…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedHeight(30)
        self.search_input.setMaximumWidth(280)
        self.search_input.setMinimumWidth(180)
        self.search_input.setStyleSheet(
            f"QLineEdit {{ background: {TOKENS[t]['bgSunken']}; "
            f"border: 1px solid {TOKENS[t]['border']}; border-radius: 6px; "
            f"padding: 0 10px; color: {TOKENS[t]['fg']}; "
            f"font-size: 12.5px; }}"
        )
        self.search_input.textChanged.connect(self._on_search_text_changed)
        # Forward focus events through to the suggestion-dropdown
        # controller so the hot-keywords panel pops open on first
        # focus and closes when focus leaves the input + dropdown
        # together.
        self.search_input.installEventFilter(self)
        tb.addWidget(self.search_input)

        # Debounce timer for type-ahead. textChanged fires per keystroke;
        # we only emit `search_suggestions_requested` once the user
        # pauses for SEARCH_TYPEAHEAD_DEBOUNCE_MS. Filter-against-the-
        # local-SQLite still happens on every keystroke (cheap LIKE),
        # but the network round-trip is gated.
        self._typeahead_timer = QTimer(self)
        self._typeahead_timer.setSingleShot(True)
        self._typeahead_timer.setInterval(SEARCH_TYPEAHEAD_DEBOUNCE_MS)
        self._typeahead_timer.timeout.connect(
            self._on_typeahead_timer_fired)
        self._suggest_dropdown: _SearchSuggestPopup | None = None
        self._hot_keywords: list[str] = []
        self._hot_keywords_requested = False

        self.source_combo = QComboBox()
        for value, label in SOURCE_FILTERS:
            self.source_combo.addItem(label, value)
        self.source_combo.setFixedHeight(30)
        self.source_combo.setStyleSheet(self._combo_qss())
        self.source_combo.currentIndexChanged.connect(self._on_filter_changed)
        tb.addWidget(self._labeled("Source", self.source_combo))

        tb.addStretch(1)

        # AppGallery source toggle — opt-in. The curated extras are
        # useful out of the box; pulling AppGallery's category dump
        # (~30 s on a cold cache) isn't worth doing on every cold
        # start.
        self.appgallery_toggle = self._make_source_toggle(
            "Show all AppGallery", self._on_appgallery_toggled)
        tb.addWidget(self.appgallery_toggle)

        self.catalog_version_label = QLabel(
            self._catalog_version_text())
        self.catalog_version_label.setStyleSheet(
            f"color: {TOKENS[t]['fgDim']}; "
            f"font-family: '{mono_family()}'; font-size: 11px;"
        )
        tb.addWidget(self.catalog_version_label)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setFixedHeight(28)
        self.refresh_button.setCursor(Qt.PointingHandCursor)
        self.refresh_button.setStyleSheet(
            f"QPushButton {{ background: {TOKENS[t]['bgRaised']}; "
            f"border: 1px solid {TOKENS[t]['border']}; border-radius: 6px; "
            f"padding: 0 10px; color: {TOKENS[t]['fgMuted']}; "
            f"font-size: 12px; font-weight: 500; }}"
            f"QPushButton:hover {{ color: {TOKENS[t]['fg']}; }}"
        )
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        tb.addWidget(self.refresh_button)

        v.addWidget(toolbar)

        # Body — stacked: catalog view | empty | error | loading.
        self._body = QStackedWidget()

        # Catalog page
        catalog_page = QWidget()
        cp = QVBoxLayout(catalog_page)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {TOKENS[t]['bg']}; }}"
        )
        scroll_inner = QWidget()
        scroll_inner.setStyleSheet(
            f"background: {TOKENS[t]['bg']};"
        )
        self._inner_layout = QVBoxLayout(scroll_inner)
        self._inner_layout.setContentsMargins(18, 10, 18, 4)
        self._inner_layout.setSpacing(0)

        self._rows_holder = QFrame()
        self._rows_holder.setObjectName("storeRows")
        self._rows_holder.setStyleSheet(
            f"#storeRows {{ background: {TOKENS[t]['bgRaised']}; "
            f"border: 1px solid {TOKENS[t]['border']}; border-radius: 8px; }}"
        )
        self._rows_layout = QVBoxLayout(self._rows_holder)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        self._inner_layout.addWidget(self._rows_holder)
        self._inner_layout.addStretch(1)

        self._scroll.setWidget(scroll_inner)
        # Auto-load the next batch of rows when the user scrolls near
        # the bottom — keeps the lazy-rendering invisible from the
        # user's perspective.
        self._scroll.verticalScrollBar().valueChanged.connect(
            self._on_scroll_changed)
        cp.addWidget(self._scroll, stretch=1)

        # Footer
        footer = QFrame()
        fl = QHBoxLayout(footer)
        fl.setContentsMargins(20, 8, 20, 12)
        fl.setSpacing(0)
        self.footer_count_label = QLabel("showing 0 of 0")
        self.footer_count_label.setStyleSheet(
            f"color: {TOKENS[t]['fgDim']}; "
            f"font-family: '{mono_family()}'; font-size: 11px;"
        )
        fl.addWidget(self.footer_count_label)
        fl.addStretch(1)
        self.footer_updated_label = QLabel(
            self._footer_updated_text())
        self.footer_updated_label.setStyleSheet(
            f"color: {TOKENS[t]['fgDim']}; "
            f"font-family: '{mono_family()}'; font-size: 11px;"
        )
        fl.addWidget(self.footer_updated_label)
        cp.addWidget(footer)

        self._body.addWidget(catalog_page)

        # Empty state
        self._empty_page = self._build_message_page(
            symbol="🔍",
            color_token="fgDim",
            title="No apps match these filters.",
            subtitle="",
            button_text="Reset filters",
            on_click=self._reset_filters,
        )
        self._body.addWidget(self._empty_page)

        # Error state
        self._error_page = self._build_message_page(
            symbol="⚠",
            color_token="bad",
            title="Couldn't load catalog.",
            subtitle="",
            button_text="Retry",
            on_click=self.refresh_requested.emit,
        )
        self._body.addWidget(self._error_page)

        # Loading state
        self._loading_page = self._build_message_page(
            symbol="◌",
            color_token="accent",
            title="Fetching catalog index…",
            subtitle="",
            button_text=None,
            on_click=None,
        )
        self._body.addWidget(self._loading_page)

        v.addWidget(self._body, stretch=1)

        # Don't build rows here — _apply_filters() in __init__ kicks
        # the lazy renderer which materialises only the first BATCH_SIZE.
        if self._store is None or self._store.total() == 0:
            self._show_view("empty")
        else:
            self._show_view("catalog")

    def _combo_qss(self) -> str:
        # Combo style used INSIDE _labeled() — no border/background of
        # its own (the surrounding QFrame paints those), and the native
        # dropdown indicator is hidden so we can paint a custom chevron
        # that visually matches the design (▾, mono, dim).
        t = self._theme
        return (
            f"QComboBox {{ background: transparent; "
            f"border: none; padding: 0 0 0 0; "
            f"color: {TOKENS[t]['fg']}; font-size: 12px; "
            f"font-weight: 500; }}"
            f"QComboBox::drop-down {{ width: 0px; border: none; }}"
            f"QComboBox::down-arrow {{ image: none; width: 0; height: 0; }}"
            f"QComboBox QAbstractItemView {{ "
            f"background: {TOKENS[t]['bgRaised']}; "
            f"border: 1px solid {TOKENS[t]['border']}; "
            f"selection-background-color: {TOKENS[t]['accent']}; "
            f"color: {TOKENS[t]['fg']}; padding: 4px 0; }}"
        )

    def _labeled(self, label: str, combo: QComboBox) -> QWidget:
        # Replicates the design's FilterDropdown: a single rounded pill
        # showing "label  value  ▾" with no native combo chrome
        # leaking through. The QComboBox keeps its full API (count(),
        # itemData(), setCurrentIndex(), currentData()) so the filter
        # logic and tests are unchanged.
        t = self._theme
        w = QFrame()
        w.setObjectName("storeFilterPill")
        w.setStyleSheet(
            f"#storeFilterPill {{ background: {TOKENS[t]['bgRaised']}; "
            f"border: 1px solid {TOKENS[t]['border']}; "
            f"border-radius: 6px; }}"
        )
        h = QHBoxLayout(w)
        h.setContentsMargins(10, 0, 8, 0)
        h.setSpacing(6)
        h.setAlignment(Qt.AlignVCenter)
        lbl = QLabel(label)
        lbl.setStyleSheet(
            f"color: {TOKENS[t]['fgDim']}; "
            f"font-size: 12px; border: none; background: transparent;"
        )
        h.addWidget(lbl)
        combo.setFrame(False)
        combo.setStyleSheet(self._combo_qss())
        combo.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        combo.setMinimumContentsLength(6)
        # Default popup width = combo width, which forces Qt to elide
        # long item text ("navigation" → "na...on"). Force the popup
        # to size to its widest item so every option is fully readable
        # regardless of how narrow the inline pill is.
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(combo.font())
        widest = max(
            (fm.horizontalAdvance(combo.itemText(i))
             for i in range(combo.count())),
            default=80,
        )
        combo.view().setMinimumWidth(widest + 32)
        combo.view().setTextElideMode(Qt.ElideNone)
        h.addWidget(combo)
        chevron = QLabel("▾")
        chevron.setStyleSheet(
            f"color: {TOKENS[t]['fgDim']}; "
            f"font-size: 11px; border: none; background: transparent;"
        )
        h.addWidget(chevron)
        w.setFixedHeight(30)
        # Click anywhere on the pill — including the label + chevron —
        # opens the combo's popup, so the whole shape feels like one
        # interactive widget rather than three glued together.
        from PySide6.QtCore import QEvent

        class _PopupForwarder(QObject):
            def __init__(self, target: QComboBox):
                super().__init__(target)
                self._target = target

            def eventFilter(self, obj, ev):  # noqa: D401 — Qt API
                if ev.type() == QEvent.MouseButtonPress:
                    self._target.showPopup()
                    return True
                return False

        forwarder = _PopupForwarder(combo)
        for child in (w, lbl, chevron):
            child.installEventFilter(forwarder)
            child.setCursor(Qt.PointingHandCursor)
        return w

    def _build_message_page(
        self,
        *,
        symbol: str,
        color_token: str,
        title: str,
        subtitle: str,
        button_text: str | None,
        on_click: Callable | None,
    ) -> QWidget:
        t = self._theme
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(24, 24, 24, 24)
        v.setSpacing(10)
        v.setAlignment(Qt.AlignCenter)
        glyph = QLabel(symbol)
        glyph.setAlignment(Qt.AlignCenter)
        glyph.setStyleSheet(
            f"color: {TOKENS[t][color_token]}; font-size: 26px;"
        )
        v.addWidget(glyph)
        title_lbl = QLabel(title)
        title_lbl.setAlignment(Qt.AlignCenter)
        title_lbl.setStyleSheet(
            f"color: {TOKENS[t]['fg']}; font-size: 14px; font-weight: 500;"
        )
        v.addWidget(title_lbl)
        subtitle_lbl = QLabel(subtitle or " ")
        subtitle_lbl.setAlignment(Qt.AlignCenter)
        subtitle_lbl.setStyleSheet(
            f"color: {TOKENS[t]['fgMuted']}; font-size: 12px; "
            f"font-family: '{mono_family()}';"
        )
        subtitle_lbl.setWordWrap(True)
        v.addWidget(subtitle_lbl)
        page.setProperty("subtitle_lbl", subtitle_lbl)
        if button_text and on_click is not None:
            btn = QPushButton(button_text)
            btn.setFixedHeight(30)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                f"QPushButton {{ background: {TOKENS[t]['bgRaised']}; "
                f"border: 1px solid {TOKENS[t]['border']}; "
                f"border-radius: 6px; padding: 0 16px; "
                f"color: {TOKENS[t]['fg']}; font-size: 12.5px; }}"
            )
            btn.clicked.connect(on_click)
            v.addWidget(btn, alignment=Qt.AlignCenter)
        return page

    # ---- catalog wiring ----

    def set_catalog(
        self,
        catalog: "_catalog.Catalog | CatalogStore | None",
    ) -> None:
        # NOTE: we no longer close the previous store here. Main window
        # holds long-lived references to ``_extras_store`` and
        # ``_full_store`` and switches between them on F-Droid toggle —
        # closing on swap would invalidate the other handle on the
        # next switch. Lifecycle ownership is the caller's job.
        new_store = self._coerce_to_store(catalog)
        self._store = new_store
        self.catalog_version_label.setText(self._catalog_version_text())
        self.footer_updated_label.setText(self._footer_updated_text())
        # Drop any cached AppRow widgets — entries may have changed
        # version / icon / category between snapshots.
        self._discard_cached_rows()
        if self._store is None or self._store.total() == 0:
            self._show_view("empty")
            self._update_footer(0, 0)
        else:
            self._show_view("catalog")
            self._apply_filters()

    def show_loading(self, hint: str = "") -> None:
        self._set_subtitle(self._loading_page, hint)
        self._show_view("loading")

    def show_error(self, message: str) -> None:
        self._set_subtitle(self._error_page, message)
        self._show_view("error")

    def set_install_phase(
        self, app_id: str, phase: str, *, progress: float = 0.0,
    ) -> None:
        # Materialise the row even if it's not currently in view —
        # phase events still need to flow to it so when the user scrolls
        # back the per-row state is correct. ``_ensure_row`` is a no-op
        # if the row already exists. When ``app_id`` came from the
        # search-suggestion popup (synthetic CatalogEntry whose package
        # isn't in the active store), ``_ensure_row`` returns None and
        # we drop the phase update silently — the install pipeline is
        # still running off-thread, the toolbar toast tells the user
        # which app is in flight.
        row = self._ensure_row(app_id)
        if row is None:
            log.debug(
                "store: phase=%s for app_id=%r dropped — no row in catalog",
                phase, app_id)
            return
        row.set_phase(phase, progress=progress)
        if phase in (PHASE_RESOLVING, PHASE_DOWNLOADING, PHASE_INSTALLING):
            self._busy_app_id = app_id
            self._refresh_install_buttons()
        else:
            if self._busy_app_id == app_id:
                self._busy_app_id = None
                self._refresh_install_buttons()

    def set_install_progress(
        self, app_id: str, seen: int, total: int,
    ) -> None:
        row = self._rows.get(app_id)
        if row is not None:
            row.set_progress(seen, total)

    def set_global_busy(self, busy: bool) -> None:
        """Disable Install buttons while *any* operation is running."""
        self._refresh_install_buttons(force_disable=busy)

    def set_appgallery_toggle(self, checked: bool) -> None:
        """Reflect the AppGallery loaded state on the toggle without
        re-emitting the signal — used by main_window to sync the UI
        after async fetches complete (or after restoring the saved
        preference at launch)."""
        self._set_source_toggle_state(self.appgallery_toggle, checked)

    def _make_source_toggle(self, label: str, slot) -> QPushButton:
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(28)
        btn.toggled.connect(slot)
        self._apply_source_toggle_style(btn)
        return btn

    def _set_source_toggle_state(self, btn: QPushButton, checked: bool) -> None:
        if btn.isChecked() == checked:
            return
        btn.blockSignals(True)
        try:
            btn.setChecked(checked)
        finally:
            btn.blockSignals(False)
        self._apply_source_toggle_style(btn)

    def _on_appgallery_toggled(self, checked: bool) -> None:
        self._apply_source_toggle_style(self.appgallery_toggle)
        self.appgallery_toggle_requested.emit(checked)

    def _apply_source_toggle_style(self, btn: QPushButton) -> None:
        t = self._theme
        if btn.isChecked():
            tinted_bg = _color_mix(t, "accent", 0.12, "bgRaised")
            tinted_border = _color_mix(t, "accent", 0.55, "border")
            btn.setStyleSheet(
                f"QPushButton {{ background: {tinted_bg}; "
                f"border: 1px solid {tinted_border}; "
                f"border-radius: 14px; padding: 0 14px; "
                f"color: {TOKENS[t]['accent']}; "
                f"font-size: 12px; font-weight: 600; }}"
            )
        else:
            btn.setStyleSheet(
                f"QPushButton {{ background: {TOKENS[t]['bgRaised']}; "
                f"border: 1px solid {TOKENS[t]['border']}; "
                f"border-radius: 14px; padding: 0 14px; "
                f"color: {TOKENS[t]['fgMuted']}; "
                f"font-size: 12px; font-weight: 500; }}"
                f"QPushButton:hover {{ color: {TOKENS[t]['fg']}; }}"
            )

    # ---- internals ----

    def _discard_cached_rows(self) -> None:
        """Remove every AppRow from the layout AND drop the cache.

        Called from ``set_catalog`` when the underlying entries change
        — cached rows wouldn't reflect the new version/icon/category.
        ``_apply_filters`` then re-renders the first batch.
        """
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._rows.clear()
        self._rendered_ids = []
        self._filtered_ids = []

    def _detach_rendered(self) -> None:
        """Remove rendered rows from the layout but keep the cache.

        Used on filter-change: the row widgets we already built stay
        alive in ``self._rows``, but the layout order needs to follow
        the new filter result. We hide-then-take (NOT ``setParent(None)``
        — reparenting a visible widget to None promotes it to a
        top-level OS window, which on macOS spawns a real titlebar
        per row and is exactly what happened in v0.14.0/0.14.1).
        """
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
        self._rendered_ids = []

    def _build_row(self, entry: CatalogEntry) -> AppRow:
        row = AppRow(entry, self._theme, self._icon_loader, self)
        row.install_requested.connect(self.install_requested.emit)
        self._rows[entry.id] = row
        return row

    def _ensure_row(self, app_id: str) -> AppRow | None:
        """Return the AppRow for ``app_id``, building it if necessary.

        Used by ``set_install_phase`` so a row that's outside the
        currently-rendered batch (e.g. user clicked Install then
        scrolled away) keeps receiving phase updates.
        """
        row = self._rows.get(app_id)
        if row is not None:
            return row
        if self._store is None:
            return None
        entry = self._store.fetch(app_id)
        if entry is None:
            return None
        return self._build_row(entry)

    def _render_more(self, count: int | None = None) -> None:
        """Append the next ``count`` filtered entries to the layout."""
        if not self._filtered_ids or self._store is None:
            return
        count = count or self.BATCH_SIZE
        already = len(self._rendered_ids)
        target = min(len(self._filtered_ids), already + count)
        if target <= already:
            return
        # Fetch only the ids we're about to materialise — a single
        # SELECT ... WHERE id IN (...) instead of holding 4000+ entries
        # in memory just to look one up.
        ids_window = self._filtered_ids[already:target]
        # Re-use already-built rows where possible to avoid a SQL
        # round-trip for entries the user just scrolled past.
        missing = [i for i in ids_window if i not in self._rows]
        for entry in self._store.fetch_many(missing):
            self._build_row(entry)
        for i in range(already, target):
            app_id = self._filtered_ids[i]
            row = self._rows.get(app_id)
            if row is None:
                continue
            # Top-row separator: every row past the first paints a
            # 1px border across the top so the list reads as a single
            # banded card, matching the design.
            if i == 0:
                row.setStyleSheet(
                    "#storeAppRow { background: transparent; }")
            else:
                row.setStyleSheet(
                    "#storeAppRow { background: transparent; "
                    f"border-top: 1px solid "
                    f"{TOKENS[self._theme]['border']}; }}"
                )
            self._rows_layout.addWidget(row)
            row.setVisible(True)
            self._rendered_ids.append(app_id)
        # Newly-built rows start enabled — re-apply the busy lock so
        # they don't accidentally allow a parallel install while
        # something is already running.
        self._refresh_install_buttons()

    def _on_scroll_changed(self, _value: int) -> None:
        if len(self._rendered_ids) >= len(self._filtered_ids):
            return
        bar = self._scroll.verticalScrollBar()
        if bar.maximum() <= 0:
            return
        if bar.value() / bar.maximum() >= self.LOAD_MORE_THRESHOLD:
            self._render_more()

    def _on_filter_changed(self, *_args) -> None:
        self._apply_filters()

    # ---- search dropdown / suggestions ----

    def _on_search_text_changed(self, _text: str) -> None:
        # Local filter is cheap (a LIKE against a few thousand rows
        # in SQLite); run it on every keystroke so the catalog list
        # tracks the user's typing in real time.
        self._apply_filters()
        # The network round-trip is gated by a debounce timer — see
        # _on_typeahead_timer_fired for the actual emit.
        kw = self.search_input.text().strip()
        if not kw:
            # Empty input: dismiss any in-flight suggestions and fall
            # back to the hot-keyword panel if the input is focused.
            self._typeahead_timer.stop()
            if self.search_input.hasFocus():
                self._show_hot_keywords_dropdown()
            else:
                self._hide_suggest_dropdown()
            return
        # Restart the debounce window — only the LAST keystroke in a
        # burst leads to a network call.
        self._typeahead_timer.start()

    def _on_typeahead_timer_fired(self) -> None:
        kw = self.search_input.text().strip()
        if not kw:
            return
        self.search_suggestions_requested.emit(kw)

    def eventFilter(self, obj, event):  # noqa: D401 — Qt API
        from PySide6.QtCore import QEvent
        if obj is self.search_input:
            if event.type() == QEvent.FocusIn:
                if not self.search_input.text().strip():
                    self._show_hot_keywords_dropdown()
            elif event.type() == QEvent.FocusOut:
                # Defer hide via singleShot(0) so a click that's
                # landing on a button INSIDE the popup gets to fire
                # before we tear it down. The keyword/app handlers
                # explicitly hide the popup themselves.
                QTimer.singleShot(0, self._maybe_hide_suggest)
        return super().eventFilter(obj, event)

    def _maybe_hide_suggest(self) -> None:
        # If a button inside the popup just took focus, leave it open
        # — its clicked() handler will hide it when done.
        if (self._suggest_dropdown is not None
                and self._suggest_dropdown.isVisible()):
            from PySide6.QtWidgets import QApplication
            focus_widget = QApplication.focusWidget()
            if focus_widget is not None:
                w = focus_widget
                while w is not None:
                    if w is self._suggest_dropdown:
                        return
                    w = w.parentWidget()
        self._hide_suggest_dropdown()

    def _show_hot_keywords_dropdown(self) -> None:
        # First focus: ask the main window to fetch the hot keywords.
        # Subsequent focuses use the cached list.
        if not self._hot_keywords and not self._hot_keywords_requested:
            self._hot_keywords_requested = True
            self.hot_keywords_requested.emit()
        if self._hot_keywords:
            self._render_suggest_dropdown(
                kind="hot", keywords=self._hot_keywords, top_app=None)

    def _hide_suggest_dropdown(self) -> None:
        if self._suggest_dropdown is not None:
            self._suggest_dropdown.hide()

    def _render_suggest_dropdown(
        self,
        *,
        kind: str,                       # 'hot' | 'typeahead'
        keywords: list[str],
        top_app: dict | None,
    ) -> None:
        if self._suggest_dropdown is None:
            self._suggest_dropdown = _SearchSuggestPopup(
                anchor=self.search_input, theme=self._theme, parent=self)
            self._suggest_dropdown.keyword_clicked.connect(
                self._on_suggestion_keyword_clicked)
            self._suggest_dropdown.app_clicked.connect(
                self._on_suggestion_app_clicked)
        self._suggest_dropdown.set_content(
            kind=kind, keywords=keywords, top_app=top_app)
        self._suggest_dropdown.show_anchored()

    def _on_suggestion_keyword_clicked(self, keyword: str) -> None:
        # Fill the input — this triggers _on_search_text_changed which
        # re-runs the local filter. Then dismiss the dropdown so the
        # user sees the catalog list react to the chosen keyword.
        self.search_input.blockSignals(True)
        self.search_input.setText(keyword)
        self.search_input.blockSignals(False)
        self._apply_filters()
        self._hide_suggest_dropdown()

    def _on_suggestion_app_clicked(self, item: dict) -> None:
        self._hide_suggest_dropdown()
        if item:
            self.suggested_app_install_requested.emit(item)

    def set_hot_keywords(self, keywords: list[str]) -> None:
        """Public hook used by the main window after fetching hot
        keywords from /edge/index/getnewhotsearchlist on the worker
        thread."""
        self._hot_keywords = list(keywords or [])
        # If the input is currently focused with no text, show the
        # panel right away. Otherwise the cached list will be used the
        # next time the input gains focus.
        if self.search_input.hasFocus() and not self.search_input.text().strip():
            self._render_suggest_dropdown(
                kind="hot", keywords=self._hot_keywords, top_app=None)

    def set_search_suggestions(
        self, keyword: str, suggestions: list[str], top_app: dict | None,
    ) -> None:
        """Public hook used by the main window after fetching type-
        ahead suggestions from /edge/index/completeSearchWord.

        The result is dropped if the user has since cleared the input
        or started typing a different word — that's tracked by
        comparing ``keyword`` against the current input text.
        """
        current = self.search_input.text().strip()
        if not current or current.casefold() != (keyword or "").casefold():
            return
        if not suggestions and not top_app:
            self._hide_suggest_dropdown()
            return
        self._render_suggest_dropdown(
            kind="typeahead", keywords=suggestions, top_app=top_app)

    def _apply_filters(self) -> None:
        if self._store is None or self._store.total() == 0:
            self._filtered_ids = []
            self._update_footer(0, 0)
            return
        src = self.source_combo.currentData() or "all"
        query = self.search_input.text().strip()
        # Pull only the id sequence in AppGallery's own order. Rows
        # are fetched on demand by _render_more in BATCH_SIZE chunks.
        # The full ID list is small (~150 KB for 1000 entries) and
        # lets _render_more stay synchronous.
        self._filtered_ids = self._store.query_ids(
            query=query,
            source=src,
        )
        filtered_count = len(self._filtered_ids)

        # Re-seat the layout: detach previously rendered rows, then
        # render the first batch from the new filter order. Cached rows
        # for entries not in the filter stay in ``_rows`` (cheap to
        # keep around — they might come back when the user clears the
        # filter), they just aren't in the layout right now.
        self._detach_rendered()
        # Reset scroll to top so the first page of results is visible —
        # otherwise a long scroll position from the previous filter
        # leaves the view blank.
        self._scroll.verticalScrollBar().setValue(0)
        self._render_more()

        total_count = self._store.total() if self._store else 0
        self._update_footer(filtered_count, total_count)
        if not self._filtered_ids:
            self._show_view("empty")
        else:
            self._show_view("catalog")

    def _update_footer(self, shown: int, total: int) -> None:
        self.footer_count_label.setText(f"showing {shown} of {total}")

    def _show_view(self, view: str) -> None:
        idx = {"catalog": 0, "empty": 1, "error": 2, "loading": 3}.get(view, 0)
        self._body.setCurrentIndex(idx)

    def _reset_filters(self) -> None:
        self.search_input.setText("")
        self.source_combo.setCurrentIndex(0)

    def _set_subtitle(self, page: QWidget, text: str) -> None:
        sub = page.property("subtitle_lbl")
        if sub is not None:
            sub.setText(text or " ")

    def _refresh_install_buttons(self, *, force_disable: bool = False) -> None:
        for app_id, row in self._rows.items():
            if force_disable and self._busy_app_id != app_id:
                row.set_enabled_install(False)
            elif self._busy_app_id is not None and self._busy_app_id != app_id:
                row.set_enabled_install(False)
            else:
                row.set_enabled_install(True)

    def _generated_at(self) -> str:
        return self._store.generated_at() if self._store else ""

    def _catalog_version_text(self) -> str:
        ts = self._generated_at()
        if not ts:
            return "catalog v—"
        return f"catalog v{ts[:10]}"

    def _footer_updated_text(self) -> str:
        ts = self._generated_at()
        if not ts:
            return "last updated —"
        return f"last updated {ts.replace('T', ' ').replace('Z', ' UTC')}"


__all__ = ["StoreTab", "AppRow", "IconLoader"]
