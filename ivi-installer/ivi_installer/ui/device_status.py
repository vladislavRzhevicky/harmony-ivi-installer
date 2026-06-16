"""Device status — background poller + redesigned device strip widget.

Public API preserved verbatim from the pre-redesign version:

* exports ``COLOR_RED``/``COLOR_GREEN``/``COLOR_YELLOW``/``COLOR_ORANGE``/``COLOR_GREY``
* widget exposes ``dot_label``, ``main_label``, ``sub_label``, ``badge_label``,
  ``refresh_button``, ``details_frame``, ``set_status``,
  ``set_details_visible``, ``details_visible``
* signals: ``device_changed``, ``capabilities_updated``,
  ``refresh_requested``, ``toggle_details_requested``

Visuals are upgraded to fit the redesign device-strip (vehicle glyph,
two-line title/meta, pulsing dot + connection caption) while leaving the
test-facing attributes alone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import (
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtCore import QThread
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import adb
from .. import devices as devices_module
from ..devices import (
    Device,
    DeviceCapabilities,
    DeviceInfo,
)
from .theme import TOKENS, mono_family
from .widgets import PulsingDot, Spinner, VehicleGlyph

log = logging.getLogger(__name__)


# ---- typed status payload ----

@dataclass(frozen=True)
class DeviceStatus:
    devices: list[Device]
    selected: Device | None
    info: DeviceInfo | None
    adb_present: bool
    multiple: bool

    @property
    def capabilities(self) -> DeviceCapabilities | None:
        return self.info.capabilities if self.info else None


# ---- legacy color exports (used by older callers + tests) ----

DOT = "●"
COLOR_RED = "#dc3545"
COLOR_YELLOW = "#ffc107"
COLOR_GREEN = "#28a745"
COLOR_ORANGE = "#fd7e14"
COLOR_GREY = "#6c757d"


# ---- background poller (unchanged) ----

class DevicePollerWorker(QThread):
    status = Signal(object)
    error = Signal(str)

    def __init__(self, interval_ms: int = 2000, parent=None):
        super().__init__(parent)
        self._interval_ms = interval_ms
        self._timer: QTimer | None = None
        self._paused = False
        self._preferred_serial: str | None = None
        self._stop_requested = False

    @Slot(bool)
    def set_paused(self, paused: bool) -> None:
        self._paused = paused

    @Slot(str)
    def set_preferred_serial(self, serial: str) -> None:
        self._preferred_serial = serial or None

    @Slot()
    def trigger_now(self) -> None:
        if not self._paused:
            self._poll_once()

    @Slot()
    def stop(self) -> None:
        self._stop_requested = True
        self.quit()

    def run(self) -> None:  # type: ignore[override]
        self._timer = QTimer()
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._poll_once)
        self._timer.start()
        QTimer.singleShot(0, self._poll_once)
        self.exec()
        self._timer.stop()
        self._timer.deleteLater()
        self._timer = None

    def _poll_once(self) -> None:
        if self._paused or self._stop_requested:
            return
        try:
            adb_present = adb.find_adb() is not None
            if not adb_present:
                self.status.emit(DeviceStatus(
                    devices=[], selected=None, info=None,
                    adb_present=False, multiple=False,
                ))
                return
            devs = devices_module.list_devices()
            ready = [d for d in devs if d.is_ready]
            selected: Device | None = None
            if self._preferred_serial:
                selected = next(
                    (d for d in ready if d.serial == self._preferred_serial),
                    None,
                )
            if selected is None and ready:
                selected = ready[0]
            info: DeviceInfo | None = None
            if selected is not None:
                info = devices_module.detect_full_info(
                    selected.serial,
                    fallback_product=selected.product,
                )
            self.status.emit(DeviceStatus(
                devices=devs, selected=selected, info=info,
                adb_present=True, multiple=len(ready) > 1,
            ))
        except Exception as e:  # pragma: no cover
            log.exception("device poll failed")
            self.error.emit(f"{type(e).__name__}: {e}")


# ---- redesigned device strip widget ----

class DeviceStatusWidget(QWidget):
    """Top device strip with redesigned visuals + legacy public surface."""

    device_changed = Signal(object)
    capabilities_updated = Signal(object)
    refresh_requested = Signal()
    toggle_details_requested = Signal()

    def __init__(self, parent: QWidget | None = None, theme: str = "dark"):
        super().__init__(parent)
        self._theme = theme
        self._info: DeviceInfo | None = None
        self.setObjectName("deviceStrip")
        self.setProperty("state", "none")
        self.setMinimumHeight(60)
        # Ensure the QSS background actually paints on a custom QWidget.
        self.setAttribute(Qt.WA_StyledBackground, True)
        # Inline fallback so the bg renders even before QSS resolves.
        self.setStyleSheet(
            f"QWidget#deviceStrip {{ background: {TOKENS[self._theme]['bgRaised']}; "
            f"border-bottom: 1px solid {TOKENS[self._theme]['border']}; }}"
        )
        self._build_ui()
        self.set_status(DeviceStatus(
            devices=[], selected=None, info=None,
            adb_present=True, multiple=False,
        ))

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 6, 20, 6)
        outer.setSpacing(14)

        # Vehicle glyph removed per design (kept hidden so any code that
        # touches `self._glyph` keeps working). Spinner sits inline.
        self._glyph = VehicleGlyph(self._theme, self)
        self._glyph.hide()
        self._spinner = Spinner(16, self._theme, "fgMuted", self)
        self._spinner.hide()
        outer.addWidget(self._spinner, alignment=Qt.AlignVCenter)

        # Two-line text column.
        col = QVBoxLayout()
        col.setSpacing(0)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        # Legacy dot_label — small status dot kept for backward
        # compatibility. Re-purposed visually as a tiny pre-text marker;
        # tests read its styleSheet for the color hex.
        self.dot_label = QLabel(DOT)
        self.dot_label.setStyleSheet(f"color: {COLOR_GREY}; font-size: 11pt;")
        self.dot_label.setCursor(QCursor(Qt.PointingHandCursor))
        self.dot_label.mousePressEvent = self._on_status_clicked  # type: ignore[assignment]
        title_row.addWidget(self.dot_label, alignment=Qt.AlignVCenter)

        self.main_label = QLabel("…")
        self.main_label.setObjectName("deviceTitle")
        self.main_label.setCursor(QCursor(Qt.PointingHandCursor))
        self.main_label.mousePressEvent = self._on_status_clicked  # type: ignore[assignment]
        self.main_label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; "
            f"color: {TOKENS[self._theme]['fg']}; background: transparent;"
        )
        title_row.addWidget(self.main_label)

        # Inline badge pill.
        self.badge_label = QLabel("")
        self.badge_label.setVisible(False)
        self._style_badge(self.badge_label, COLOR_GREEN)
        title_row.addWidget(self.badge_label, alignment=Qt.AlignVCenter)

        title_row.addStretch(1)
        col.addLayout(title_row)

        self.sub_label = QLabel("")
        self.sub_label.setStyleSheet(
            f"color: {TOKENS[self._theme]['fgMuted']};"
            f"font-family: '{mono_family()}'; font-size: 11px; background: transparent;"
        )
        col.addWidget(self.sub_label)

        outer.addLayout(col, stretch=1)

        # Right side: pulsing connection dot + connection caption + refresh.
        self._pulsing = PulsingDot(self._theme, self)
        self._pulsing.hide()
        outer.addWidget(self._pulsing, alignment=Qt.AlignVCenter)

        self._right_caption = QLabel("")
        self._right_caption.setStyleSheet(
            f"color: {TOKENS[self._theme]['fgMuted']}; font-size: 12px;"
        )
        outer.addWidget(self._right_caption, alignment=Qt.AlignVCenter)

        self.refresh_button = QPushButton("↻")
        self.refresh_button.setProperty("ghost", "true")
        self.refresh_button.setToolTip("Refresh now")
        self.refresh_button.setFixedWidth(28)
        self.refresh_button.setCursor(Qt.PointingHandCursor)
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        outer.addWidget(self.refresh_button, alignment=Qt.AlignVCenter)

        # Hidden details frame (preserves pre-redesign API).
        self.details_frame = QFrame()
        self.details_frame.setObjectName("card")
        self.details_frame.setVisible(False)
        form = QFormLayout(self.details_frame)
        form.setContentsMargins(8, 8, 8, 8)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(2)
        self._detail_labels: dict[str, QLabel] = {}
        for key, title in (
            ("serial", "Serial"),
            ("model", "Model"),
            ("product", "Product code"),
            ("android", "Android"),
            ("api", "API level"),
            ("harmony", "HarmonyOS"),
            ("adbd", "adbd UID"),
            ("hdbd", "hdbd"),
            ("abi", "Architecture"),
            ("locale", "Locale"),
        ):
            lbl = QLabel("—")
            lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._detail_labels[key] = lbl
            form.addRow(title + ":", lbl)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    # ---- public API ----

    def set_status(self, status: DeviceStatus) -> None:
        self._info = status.info

        if not status.adb_present:
            self._set_state("none")
            self._paint(COLOR_GREY, "adb not found")
            self._set_badge(None)
            self.sub_label.setText("")
            self._right_caption.setText("")
            self._pulsing.hide()
            self._spinner.show()
            self._glyph.set_dimmed(True)
        elif not status.devices:
            self._set_state("searching")
            self._paint(COLOR_RED, "No device connected")
            self._set_badge(None)
            self.sub_label.setText("Searching for vehicle on USB…")
            self._right_caption.setText("adb · usb")
            self._pulsing.hide()
            self._spinner.show()
            self._glyph.set_dimmed(True)
        elif status.multiple:
            self._set_state("connected")
            count = sum(1 for d in status.devices if d.is_ready)
            self._paint(COLOR_ORANGE, f"Multiple devices — select ({count})")
            self._set_badge(None)
            self.sub_label.setText("")
            self._right_caption.setText("Connected via USB")
            self._pulsing.show()
            self._spinner.hide()
            self._glyph.set_dimmed(False)
        elif status.selected is None:
            d = status.devices[0]
            if d.state == "unauthorized":
                self._set_state("unauthorized")
                self._paint(COLOR_YELLOW,
                             "Authorizing… (check phone screen)")
            elif d.state == "offline":
                self._set_state("unauthorized")
                self._paint(COLOR_YELLOW, "Device offline")
            else:
                self._set_state("unauthorized")
                self._paint(COLOR_YELLOW, f"Device state: {d.state}")
            self._set_badge(None)
            self._right_caption.setText("")
            self._pulsing.hide()
            self._spinner.hide()
            self._glyph.set_dimmed(True)
        else:
            self._set_state("connected")
            label = (status.info.label if status.info else "") \
                or status.selected.product or status.selected.serial
            product = status.selected.product or "?"
            self._paint(COLOR_GREEN,
                         f"Connected — {label} ({product})")
            self._set_badge_from_info(status.info)
            self._right_caption.setText("Connected via USB")
            self._pulsing.show()
            self._spinner.hide()
            self._glyph.set_dimmed(False)

        # Sub-line.
        if status.selected is not None:
            adbd = (status.info.adbd_user if status.info else "") or "—"
            android = (status.info.android_release if status.info else None) or "—"
            self.sub_label.setText(
                f"serial {status.selected.serial} · "
                f"adbd {adbd} · Android {android}"
            )
        elif not status.adb_present:
            pass
        elif not status.devices:
            pass

        self._fill_details(status)

        self.device_changed.emit(status.info)
        self.capabilities_updated.emit(
            status.info.capabilities if status.info else None
        )

    def set_details_visible(self, visible: bool) -> None:
        self.details_frame.setVisible(visible)

    def details_visible(self) -> bool:
        return self.details_frame.isVisible()

    # ---- internals ----

    def _set_state(self, state: str) -> None:
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)

    def _paint(self, color: str, text: str) -> None:
        self.dot_label.setStyleSheet(f"color: {color}; font-size: 11pt;")
        self.main_label.setText(text)

    def _style_badge(self, label: QLabel, color: str) -> None:
        label.setStyleSheet(
            f"padding: 2px 8px; border-radius: 8px; "
            f"color: white; background-color: {color}; font-weight: 600; "
            f"font-size: 10.5px;"
        )

    def _set_badge(self, text: str | None,
                    color: str = COLOR_GREEN) -> None:
        if text is None:
            self.badge_label.setVisible(False)
            self.badge_label.setText("")
            return
        self.badge_label.setText(text)
        self._style_badge(self.badge_label, color)
        self.badge_label.setVisible(True)

    def _set_badge_from_info(self, info: DeviceInfo | None) -> None:
        if info is None:
            self._set_badge(None)
            return
        if info.is_test_device:
            self._set_badge("TEST DEVICE", color=COLOR_YELLOW)
        elif info.capabilities.is_avatr_ivi:
            self._set_badge("AVATR/DEEPAL IVI", color=COLOR_GREEN)
        else:
            self._set_badge(None)

    def _fill_details(self, status: DeviceStatus) -> None:
        info = status.info
        sel = status.selected
        d = self._detail_labels
        d["serial"].setText(sel.serial if sel else "—")
        d["model"].setText((info.model_name if info else None) or "—")
        d["product"].setText(
            (info.product_code if info and info.product_code else None)
            or (sel.product if sel else None)
            or "—"
        )
        d["android"].setText((info.android_release if info else None) or "—")
        d["api"].setText(str(info.android_api) if info and info.android_api is not None else "—")
        d["harmony"].setText((info.harmonyos_version if info else None) or "—")
        d["adbd"].setText((info.adbd_user if info else "") or "—")
        d["hdbd"].setText(
            ("present" if info.capabilities.has_hdc else "absent")
            if info else "—"
        )
        d["abi"].setText((info.cpu_abi if info else None) or "—")
        d["locale"].setText((info.locale if info else None) or "—")

    def _on_status_clicked(self, _event) -> None:
        self.set_details_visible(not self.details_visible())
        self.toggle_details_requested.emit()
