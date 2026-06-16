"""Custom widgets used by the redesigned ``main_window``.

Each component is a thin ``QWidget`` subclass. Rendering uses QSS where
possible (see ``theme.build_qss``) and ``QPainter`` only for things QSS
can't do — diagonal stripe pattern, the abstract sedan glyph, blinking
caret, pulsing connection dot, the install pipeline rail, the perms
matrix grid.

Anything that's a *visual stub* — i.e. the design shows it but the
existing backend doesn't drive it yet — is documented in
``CODER_NOTES.md``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from PySide6.QtCore import (
    QEvent,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .theme import TOKENS, Theme, mono_family, ui_family


# ---- shared helpers ------------------------------------------------------

def _qcolor(theme: Theme, name: str, alpha: float = 1.0) -> QColor:
    c = QColor(TOKENS[theme][name])
    if alpha < 1.0:
        c.setAlphaF(alpha)
    return c


# =========================================================================
# Vehicle glyph — abstract sedan with an accent "screen" inside windshield.
# =========================================================================

class VehicleGlyph(QWidget):
    """48×30 abstract sedan icon. Greyed out at 45% when ``dimmed=True``."""

    def __init__(self, theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme: Theme = theme
        self._dimmed = False
        self.setFixedSize(48, 30)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def set_dimmed(self, dimmed: bool) -> None:
        self._dimmed = dimmed
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._dimmed:
            p.setOpacity(0.45)

        body = _qcolor(self._theme, "fg")
        accent = _qcolor(self._theme, "accent")

        pen = QPen(body, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)

        # Body silhouette — simplified sedan profile.
        path = QPainterPath()
        path.moveTo(3, 22)
        path.lineTo(8, 22)
        path.cubicTo(9, 14, 14, 9, 20, 9)
        path.lineTo(32, 9)
        path.cubicTo(38, 9, 42, 14, 44, 22)
        path.lineTo(45, 22)
        p.drawPath(path)

        # Underside.
        p.drawLine(3, 22, 45, 22)

        # Wheels.
        p.setBrush(QBrush(body))
        p.drawEllipse(10, 20, 7, 7)
        p.drawEllipse(31, 20, 7, 7)
        p.setBrush(Qt.NoBrush)

        # Windshield — tinted accent rectangle.
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(accent))
        p.drawRoundedRect(QRectF(20, 12, 12, 6), 1.5, 1.5)
        p.end()


# =========================================================================
# Pulsing connection dot.
# =========================================================================

class PulsingDot(QWidget):
    """7-px circle with an animated outward pulse ring (1.6s loop)."""

    def __init__(self, theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._phase = 0.0
        self.setFixedSize(16, 16)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.03) % 1.0
        self.update()

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        good = _qcolor(self._theme, "good")

        # Pulse ring.
        ring = QColor(good)
        radius = 3.5 + self._phase * 5.5
        ring.setAlphaF(max(0.0, 0.45 * (1 - self._phase)))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(ring))
        p.drawEllipse(QRectF(cx - radius, cy - radius, radius * 2, radius * 2))

        # Solid dot.
        p.setBrush(QBrush(good))
        p.drawEllipse(QRectF(cx - 3.5, cy - 3.5, 7, 7))
        p.end()


# =========================================================================
# Spinner — clean Lucide-style spinning arc.
# =========================================================================

class Spinner(QWidget):
    def __init__(self, size: int = 14, theme: Theme = "dark",
                 color_token: str = "accent",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._token = color_token
        self._angle = 0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self._angle = (self._angle + 24) % 360
        self.update()

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        col = _qcolor(self._theme, self._token)
        pen = QPen(col, 1.8)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        # Sweep an arc from `angle` for 270 degrees.
        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        p.drawArc(rect, -self._angle * 16, 270 * 16)
        p.end()


# =========================================================================
# Drop zone — dashed border + diagonal-stripe paint + drag-drop.
# =========================================================================

class DropZone(QFrame):
    """Drag-drop zone for an .apk file.

    Emits ``fileDropped(str)`` with the local path. Also emits
    ``browseRequested()`` when the "Choose file…" link is clicked.
    """
    fileDropped = Signal(str)
    browseRequested = Signal()

    def __init__(self, theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # The whole zone acts as a click target — cursor reflects that.
        self.setCursor(Qt.PointingHandCursor)

        v = QVBoxLayout(self)
        v.setContentsMargins(12, 18, 12, 18)
        v.setSpacing(6)
        v.addStretch(1)

        # Upload glyph + label.
        self._upload = _UploadGlyph(theme, self)
        glyph_row = QHBoxLayout()
        glyph_row.addStretch(1)
        glyph_row.addWidget(self._upload)
        glyph_row.addStretch(1)
        v.addLayout(glyph_row)

        text_row = QHBoxLayout()
        text_row.setSpacing(0)
        text_row.addStretch(1)
        lead = QLabel("Drop an ")
        lead.setProperty("role", "muted")
        text_row.addWidget(lead)
        apk = QLabel(".apk")
        apk.setProperty("mono", "true")
        apk.setStyleSheet(
            f"color: {TOKENS[theme]['fg']}; font-family: '{mono_family()}';"
        )
        text_row.addWidget(apk)
        mid = QLabel(" here  or  ")
        mid.setProperty("role", "muted")
        text_row.addWidget(mid)
        self._browse_btn = QPushButton("Choose file…")
        self._browse_btn.setProperty("link", "true")
        self._browse_btn.setCursor(Qt.PointingHandCursor)
        self._browse_btn.clicked.connect(self.browseRequested.emit)
        text_row.addWidget(self._browse_btn)
        text_row.addStretch(1)
        v.addLayout(text_row)
        v.addStretch(1)

    def mousePressEvent(self, e):
        # Clicking anywhere inside the zone (outside the inner button)
        # opens the file picker.
        if e.button() == Qt.LeftButton:
            self.browseRequested.emit()
            e.accept()
            return
        super().mousePressEvent(e)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._upload.set_theme(theme)
        self.update()

    # ---- diagonal stripes painted *under* the QSS background.
    def paintEvent(self, e: QPaintEvent) -> None:
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        pen = QPen(_qcolor(self._theme, "border"), 1)
        pen.setStyle(Qt.SolidLine)
        p.setPen(Qt.NoPen)
        # Light stripes at 8px diagonal step.
        stripe = QColor(_qcolor(self._theme, "bgRaised"))
        stripe.setAlphaF(0.35)
        p.setBrush(QBrush(stripe))
        step = 8
        w, h = self.width(), self.height()
        # Diagonals as parallelograms.
        for off in range(-h, w + h, step * 2):
            path = QPainterPath()
            path.moveTo(off, 0)
            path.lineTo(off + step, 0)
            path.lineTo(off + step + h, h)
            path.lineTo(off + h, h)
            path.closeSubpath()
            p.fillPath(path, QBrush(stripe))
        p.end()

    # ---- drag-drop handlers.
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            urls = [u for u in e.mimeData().urls() if u.isLocalFile()]
            if any(u.toLocalFile().lower().endswith(".apk") for u in urls):
                e.acceptProposedAction()
                self.setProperty("active", "true")
                self.style().unpolish(self)
                self.style().polish(self)
                return
        e.ignore()

    def dragLeaveEvent(self, _e):
        self.setProperty("active", "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def dropEvent(self, e):
        self.setProperty("active", "false")
        self.style().unpolish(self)
        self.style().polish(self)
        for u in e.mimeData().urls():
            if u.isLocalFile():
                self.fileDropped.emit(u.toLocalFile())
                e.acceptProposedAction()
                return
        e.ignore()


class _UploadGlyph(QWidget):
    def __init__(self, theme: Theme, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self.setFixedSize(28, 28)

    def set_theme(self, t: Theme) -> None:
        self._theme = t
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(_qcolor(self._theme, "fgMuted"), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        # Up-arrow with tray.
        cx = self.width() / 2
        p.drawLine(cx, 6, cx, 18)
        p.drawLine(cx, 6, cx - 5, 11)
        p.drawLine(cx, 6, cx + 5, 11)
        p.drawLine(6, 22, self.width() - 6, 22)
        p.end()


# =========================================================================
# APK summary card.
# =========================================================================

class ApkCard(QFrame):
    """Row card showing the picked APK — icon, package, version, size + ✕."""
    cleared = Signal()

    def __init__(self, theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("card")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 10, 10, 10)
        h.setSpacing(12)

        self._icon = _ApkIcon(theme, "AP", self)
        h.addWidget(self._icon)

        col = QVBoxLayout()
        col.setSpacing(2)
        self._title = QLabel("—")
        self._title.setProperty("role", "title")
        col.addWidget(self._title)
        self._meta = QLabel("")
        self._meta.setProperty("mono", "true")
        self._meta.setStyleSheet(
            f"color: {TOKENS[theme]['fgMuted']}; font-family: '{mono_family()}'; font-size: 11px;"
        )
        col.addWidget(self._meta)
        h.addLayout(col, stretch=1)

        clear = QPushButton("✕")
        clear.setProperty("ghost", "true")
        clear.setFixedSize(22, 22)
        clear.setCursor(Qt.PointingHandCursor)
        clear.clicked.connect(self.cleared.emit)
        h.addWidget(clear)

    def set_apk(self, *, name: str, package: str = "",
                 version: str = "", size_mb: float | None = None,
                 initials: str | None = None) -> None:
        self._title.setText(name or "—")
        bits: list[str] = []
        if package:
            bits.append(package)
        if version:
            bits.append(f"v{version}")
        if size_mb is not None:
            bits.append(f"{size_mb:.1f} MB")
        self._meta.setText(" · ".join(bits))
        if initials:
            self._icon.set_initials(initials[:2].upper())
        elif name:
            base = "".join(c for c in name if c.isalnum())[:2].upper() or "AP"
            self._icon.set_initials(base)


class _ApkIcon(QWidget):
    def __init__(self, theme: Theme, initials: str = "AP",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._initials = initials
        self.setFixedSize(36, 36)

    def set_initials(self, s: str) -> None:
        self._initials = s
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        accent = _qcolor(self._theme, "accent")
        accent2 = _qcolor(self._theme, "accentDim")
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0, accent)
        grad.setColorAt(1, accent2)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(self.rect(), 7, 7)
        p.setPen(_qcolor(self._theme, "accentFg"))
        f = QFont(mono_family())
        f.setPixelSize(14)
        f.setBold(True)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, self._initials)
        p.end()


# =========================================================================
# Install-on row — fancy radio + screens diagram on the right.
# =========================================================================

class ScreensDiagram(QWidget):
    """HUD chip + cabin-shaped chips for driver / passenger / rear.

    Cabin layout drawn as an inverted pyramid (looking down on the
    car from above): two seats up front (driver + passenger), one
    bench in the back. Each chip lights up to reflect ``mode``:

    * ``driver``    — top-left chip lit
    * ``passenger`` — top-right chip lit
    * ``rear``      — bottom chip lit
    * ``all``       — every chip lit
    * ``off``       — nothing lit (card unchecked)
    """

    def __init__(self, theme: Theme = "dark", mode: str = "all",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._mode = mode
        # Wider + taller now that cards house the diagram with more
        # vertical breathing room (v0.8.8).
        self.setFixedSize(72, 28)

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        accent = _qcolor(self._theme, "accent")
        outline = _qcolor(self._theme, "borderStrong")

        # HUD chip on the left, vertically centered.
        hud_y = (self.height() - 10) / 2
        p.setPen(QPen(outline, 1.2))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(0, hud_y, 16, 10), 1.5, 1.5)
        p.setPen(_qcolor(self._theme, "fgDim"))
        f = QFont(mono_family())
        f.setPixelSize(7)
        p.setFont(f)
        p.drawText(QRectF(0, hud_y, 16, 10), Qt.AlignCenter, "HUD")

        # Divider between HUD and cabin pyramid.
        p.setPen(QPen(outline, 1))
        p.drawLine(20, 4, 20, self.height() - 4)

        # Inverted-pyramid cabin layout.
        chip_w, chip_h = 18, 9
        gap_x = 4
        gap_y = 2
        top_y = (self.height() - (chip_h * 2 + gap_y)) / 2
        bottom_y = top_y + chip_h + gap_y
        left_x = 26
        right_x = left_x + chip_w + gap_x
        center_x = (left_x + right_x) / 2

        chip_modes = {
            "all":       (True, True, True),
            "driver":    (True, False, False),
            "passenger": (False, True, False),
            "rear":      (False, False, True),
            "off":       (False, False, False),
        }
        lit = chip_modes.get(self._mode, (False, False, False))
        positions = (
            (left_x, top_y),       # driver  — top-left
            (right_x, top_y),      # passenger — top-right
            (center_x, bottom_y),  # rear    — bottom-center
        )
        for (x, y), is_on in zip(positions, lit):
            if is_on:
                p.setPen(Qt.NoPen)
                p.setBrush(QBrush(accent))
            else:
                p.setPen(QPen(outline, 1.2))
                p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(x, y, chip_w, chip_h), 2, 2)
        p.end()


# =========================================================================
# Pipeline — 5-stage rail. Visual stub bound to a "step" int.
# =========================================================================

@dataclass
class StageState:
    label: str
    hint: str
    state: str = "idle"   # idle | running | done | failed | skipped
    timing: str = ""


class Pipeline(QFrame):
    """N-row pipeline view driven by per-stage events from the backend.

    Stages are dynamic — call :meth:`set_stages` whenever the active
    install strategy changes (the two strategies have different stage
    counts). The backend then drives the rows through
    :meth:`mark_running`, :meth:`mark_done`, :meth:`mark_failed`, and
    :meth:`mark_skipped`, each of which optionally accepts a generic
    one-line hint.
    """

    def __init__(self, stages: Sequence[str],
                 theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 6, 0, 6)
        self._layout.setSpacing(0)
        self._rows: list[_PipelineRow] = []
        self.set_stages(stages)

    def set_stages(self, stages: Sequence[str]) -> None:
        """Rebuild the row list. Called when the user switches strategies."""
        for row in self._rows:
            self._layout.removeWidget(row)
            row.deleteLater()
        self._rows = []
        for i, label in enumerate(stages):
            row = _PipelineRow(i, label, self._theme, self)
            self._rows.append(row)
            self._layout.addWidget(row)

    def stage_count(self) -> int:
        return len(self._rows)

    def reset(self) -> None:
        for row in self._rows:
            row.set_state("idle")
            row.set_hint("")
            row.set_timing("")

    def mark_running(self, stage: int, hint: str | None = None) -> None:
        if 0 <= stage < len(self._rows):
            self._rows[stage].set_state("running")
            if hint is not None:
                self._rows[stage].set_hint(hint)
            self._rows[stage].set_timing("")

    def mark_done(self, stage: int, hint: str | None = None,
                   timing_ms: int | None = None) -> None:
        if 0 <= stage < len(self._rows):
            self._rows[stage].set_state("done")
            if hint is not None:
                self._rows[stage].set_hint(hint)
            self._rows[stage].set_timing(_format_timing(timing_ms))

    def mark_failed(self, stage: int, hint: str | None = None,
                     timing_ms: int | None = None) -> None:
        if 0 <= stage < len(self._rows):
            self._rows[stage].set_state("failed")
            if hint is not None:
                self._rows[stage].set_hint(hint)
            self._rows[stage].set_timing(_format_timing(timing_ms))

    def mark_skipped(self, stage: int, hint: str | None = None) -> None:
        if 0 <= stage < len(self._rows):
            self._rows[stage].set_state("skipped")
            if hint is not None:
                self._rows[stage].set_hint(hint)
            self._rows[stage].set_timing("")


def _format_timing(ms: int | None) -> str:
    """Mono-friendly duration label for the right-hand stage column."""
    if ms is None:
        return ""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60_000}m {(ms % 60_000) // 1000}s"


class _PipelineRow(QWidget):
    def __init__(self, index: int, label: str, theme: Theme,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._state = "idle"
        self._timing = ""
        self.setFixedHeight(40)

        h = QHBoxLayout(self)
        h.setContentsMargins(14, 6, 14, 6)
        h.setSpacing(12)

        self._icon = _StageIcon(theme, self)
        h.addWidget(self._icon)

        col = QVBoxLayout()
        col.setSpacing(0)
        self._label_widget = QLabel(f"<span style='color:{TOKENS[theme]['fgDim']};font-family:{mono_family()}'>{index:02d}</span> "
                                      f"<span style='color:{TOKENS[theme]['fg']}; font-weight:500'>{label}</span>")
        self._label_widget.setTextFormat(Qt.RichText)
        col.addWidget(self._label_widget)
        # Hint line is empty until the backend reports a status snippet.
        # Per design: hint is only meaningful when the stage is active or
        # has finished; idle stages should look quiet, so we hide the row
        # entirely until set_hint() puts text in it.
        self._hint = QLabel("")
        self._hint.setProperty("mono", "true")
        self._hint.setStyleSheet(
            f"color: {TOKENS[theme]['fgDim']}; font-family: '{mono_family()}'; font-size: 10.5px;"
        )
        self._hint.hide()
        col.addWidget(self._hint)
        h.addLayout(col, stretch=1)

        self._timing_label = QLabel("")
        self._timing_label.setProperty("mono", "true")
        self._timing_label.setStyleSheet(
            f"color: {TOKENS[theme]['fgDim']}; font-family: '{mono_family()}'; font-size: 10.5px;"
        )
        h.addWidget(self._timing_label)

    def set_state(self, state: str) -> None:
        self._state = state
        self._icon.set_state(state)
        self.update()

    def set_hint(self, text: str) -> None:
        self._hint.setText(text)
        self._hint.setVisible(bool(text))

    def set_timing(self, t: str) -> None:
        self._timing = t
        self._timing_label.setText(t)

    def paintEvent(self, e: QPaintEvent) -> None:
        if self._state == "running":
            p = QPainter(self)
            tint = QColor(_qcolor(self._theme, "accent"))
            tint.setAlphaF(0.06)
            p.fillRect(self.rect(), tint)
            p.fillRect(0, 0, 2, self.height(), _qcolor(self._theme, "accent"))
            p.end()
        super().paintEvent(e)


class _StageIcon(QWidget):
    def __init__(self, theme: Theme, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._state = "idle"
        self._spinner: Spinner | None = None
        self.setFixedSize(18, 18)

    def set_state(self, state: str) -> None:
        self._state = state
        if state == "running" and self._spinner is None:
            self._spinner = Spinner(13, self._theme, "accent", self)
            self._spinner.move(2, 2)
            self._spinner.show()
        elif state != "running" and self._spinner is not None:
            self._spinner.hide()
            self._spinner.deleteLater()
            self._spinner = None
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        if self._state == "running":
            return  # Spinner child handles drawing.
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._state == "done":
            pen = QPen(_qcolor(self._theme, "good"), 2.4)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.drawLine(4, 9, 8, 13)
            p.drawLine(8, 13, 14, 5)
        elif self._state == "failed":
            pen = QPen(_qcolor(self._theme, "bad"), 2.4)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(4, 4, 14, 14)
            p.drawLine(14, 4, 4, 14)
        elif self._state == "skipped":
            pen = QPen(_qcolor(self._theme, "fgDim"), 2)
            p.setPen(pen)
            p.drawLine(4, 9, 14, 9)
        else:
            # Idle ring.
            pen = QPen(_qcolor(self._theme, "fgDim"), 1.6)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(3, 3, 12, 12)
        p.end()


# =========================================================================
# Grant matrix — 8 perms × 5 users. Visual stub.
# =========================================================================

class GrantMatrix(QFrame):
    PERMS = [
        "POST_NOTIFICATIONS", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION",
        "RECORD_AUDIO", "CAMERA", "READ_MEDIA_AUDIO", "READ_MEDIA_IMAGES",
        "BLUETOOTH_CONNECT",
    ]
    USERS = ["u0", "u10", "u11", "u12", "u13"]
    # Index of each visible column → real Android user id.
    USER_IDS = (0, 10, 11, 12, 13)

    def __init__(self, theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("card")
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(0)

        # Header.
        head = QLabel("PERMISSION")
        head.setStyleSheet(
            f"color: {TOKENS[theme]['fgDim']}; font-family: '{mono_family()}';"
            f"font-size: 10.5px; font-weight: 600; padding: 6px 10px;"
            f"background: {TOKENS[theme]['bgSunken']}; "
            f"border-bottom: 1px solid {TOKENS[theme]['border']};"
        )
        grid.addWidget(head, 0, 0)
        for col, u in enumerate(self.USERS, start=1):
            lbl = QLabel(u)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(
                f"color: {TOKENS[theme]['fgDim']}; font-family: '{mono_family()}';"
                f"font-size: 10.5px; font-weight: 600; padding: 6px 0;"
                f"background: {TOKENS[theme]['bgSunken']}; "
                f"border-bottom: 1px solid {TOKENS[theme]['border']};"
            )
            lbl.setMinimumWidth(36)
            grid.addWidget(lbl, 0, col)

        self._cells: dict[tuple[int, int], QLabel] = {}
        for r, perm in enumerate(self.PERMS, start=1):
            tint = (TOKENS[theme]['bgSunken']
                     if r % 2 == 0 else TOKENS[theme]['bgRaised'])
            name = QLabel(perm)
            name.setStyleSheet(
                f"color: {TOKENS[theme]['fg']}; font-family: '{mono_family()}';"
                f"font-size: 10.5px; padding: 5px 10px; background: {tint};"
            )
            grid.addWidget(name, r, 0)
            for c in range(len(self.USERS)):
                cell = QLabel("—")
                cell.setAlignment(Qt.AlignCenter)
                cell.setStyleSheet(
                    f"color: {TOKENS[theme]['fgDim']}; font-family: '{mono_family()}';"
                    f"font-size: 11px; padding: 5px 0; background: {tint};"
                )
                grid.addWidget(cell, r, c + 1)
                self._cells[(r - 1, c)] = cell

        grid.setColumnStretch(0, 1)

    def reset(self) -> None:
        for cell in self._cells.values():
            cell.setText("—")
            cell.setStyleSheet(
                cell.styleSheet().replace(
                    f"color: {TOKENS[self._theme]['good']}",
                    f"color: {TOKENS[self._theme]['fgDim']}",
                ).replace(
                    f"color: {TOKENS[self._theme]['bad']}",
                    f"color: {TOKENS[self._theme]['fgDim']}",
                )
            )

    def fill_from_summary(
        self, summary: dict[int, tuple[int, int]],
        *, attempted_users: list[int] | None = None,
    ) -> None:
        """Paint each user column based on the per-user grant summary.

        Per design, the matrix is simplified to a column-level signal:
        every cell in a user's column reads green ✓ if all permission
        grants on that user succeeded, red ✗ if at least one failed,
        and stays as the idle em-dash for users that weren't included
        in this run.

        ``summary`` is what backend strategies emit through
        ``StageEvent.data["summary"]`` — ``{user_id: (ok, fail)}``.
        ``attempted_users`` (optional) is the list of users we tried to
        grant on; users present here but missing from ``summary`` are
        treated as "no extra perms needed" (green).
        """
        good = TOKENS[self._theme]['good']
        bad = TOKENS[self._theme]['bad']
        attempted = set(attempted_users or [])
        attempted.update(summary.keys())
        for c, user_id in enumerate(self.USER_IDS):
            ok, fail = summary.get(user_id, (0, 0))
            included = user_id in attempted
            if not included:
                self._paint_column(c, "—", TOKENS[self._theme]['fgDim'])
            elif fail > 0:
                self._paint_column(c, "✗", bad)
            else:
                self._paint_column(c, "✓", good)

    def _paint_column(self, col: int, glyph: str, color: str) -> None:
        for r in range(len(self.PERMS)):
            cell = self._cells[(r, col)]
            cell.setText(glyph)
            cell.setStyleSheet(
                f"font-family: '{mono_family()}'; font-size: 11px; "
                f"padding: 5px 0; color: {color}; background: transparent;"
            )

    def fill_demo_success(self) -> None:
        """Render the design's "filled with one synthetic CAMERA×u0 fail".

        Used only by the design preview / screenshot rig — the live UI
        now calls :meth:`fill_from_summary` with real grant data.
        """
        good = TOKENS[self._theme]['good']
        bad = TOKENS[self._theme]['bad']
        for (r, c), cell in self._cells.items():
            ok = not (r == 4 and c == 0)  # CAMERA × u0 => fail
            cell.setText("✓" if ok else "✗")
            base = cell.styleSheet().rsplit(";", 1)[0]
            new_color = good if ok else bad
            cell.setStyleSheet(
                f"font-family: '{mono_family()}'; font-size: 11px; padding: 5px 0;"
                f"color: {new_color}; background: transparent;"
            )


# =========================================================================
# Banners — install success.
# =========================================================================

class SuccessBanner(QFrame):
    linkClicked = Signal()

    def __init__(self, theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self.setObjectName("successBanner")
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 9, 12, 9)
        h.setSpacing(10)
        self._check = _CheckGlyph(theme, self)
        h.addWidget(self._check)
        self._body = QLabel("—")
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.RichText)
        h.addWidget(self._body, stretch=1)
        link = QPushButton("View detailed log")
        link.setProperty("link", "true")
        link.setCursor(Qt.PointingHandCursor)
        link.clicked.connect(self.linkClicked.emit)
        h.addWidget(link)
        self.hide()

    def show_success(self, *, package: str, scope: str = "",
                     tail: str = "") -> None:
        mono = mono_family()
        body = (
            f"<span style='color:{TOKENS[self._theme]['fg']};'>Installed </span>"
            f"<span style='font-family:{mono}; color:{TOKENS[self._theme]['fg']};'>{package}</span>"
        )
        if scope:
            body += f" <span style='color:{TOKENS[self._theme]['fgMuted']};'>{scope}</span>"
        if tail:
            body += f" <span style='color:{TOKENS[self._theme]['fgMuted']};'>{tail}</span>"
        self._body.setText(body)
        self.show()


class _CheckGlyph(QWidget):
    def __init__(self, theme: Theme, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self.setFixedSize(18, 18)

    def paintEvent(self, _e: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(_qcolor(self._theme, "good"), 2.4)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawLine(3, 10, 7, 14)
        p.drawLine(7, 14, 15, 5)
        p.end()


# =========================================================================
# Log pane — header + monospace view + blinking caret line.
# =========================================================================

class LogPane(QFrame):
    """Header bar with file path link + ghost actions (copy/save/clear)
    over a ``QPlainTextEdit`` log view. Embeds a blinking caret as the
    last "line" so the terminal-like aesthetic from the design comes
    through.

    The actual log text is appended via ``append_line(line: str)``. To
    preserve the existing ``main_window`` API, the underlying
    ``QPlainTextEdit`` is exposed as ``self.view`` (the host class can
    keep using it directly).
    """
    copyRequested = Signal()
    saveRequested = Signal()
    clearRequested = Signal()
    pathClicked = Signal()

    def __init__(self, theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._auto_scroll = True
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QFrame()
        header.setObjectName("logHeader")
        header.setFixedHeight(34)
        h = QHBoxLayout(header)
        h.setContentsMargins(14, 0, 10, 0)
        h.setSpacing(12)

        label = QLabel("LOG")
        label.setObjectName("logHeaderLabel")
        h.addWidget(label)

        self.path_button = QPushButton("—")
        self.path_button.setObjectName("logPathLink")
        self.path_button.setCursor(Qt.PointingHandCursor)
        self.path_button.setFlat(True)
        self.path_button.setStyleSheet(
            f"QPushButton#logPathLink {{"
            f"  font-family: '{mono_family()}';"
            f"  color: {TOKENS[theme]['accent']};"
            f"  background: transparent; border: 0; padding: 0;"
            f"  text-decoration: underline; text-align: left;"
            f"  font-size: 11px;"
            f"}}"
        )
        self.path_button.clicked.connect(self.pathClicked.emit)
        h.addWidget(self.path_button, stretch=1)

        # Auto-scroll pill.
        self._auto_pill = _AutoScrollPill(theme, self)
        self._auto_pill.toggled.connect(self._on_autoscroll_toggle)
        h.addWidget(self._auto_pill)

        # Ghost action buttons — Copy / Save / Clear with subtle separators
        # between them, in line with the design's terminal-toolbar feel.
        actions = QFrame()
        actions.setObjectName("logActions")
        a = QHBoxLayout(actions)
        a.setContentsMargins(0, 0, 0, 0)
        a.setSpacing(2)
        for label_text, sig, with_icon in (
            ("Copy", self.copyRequested, True),
            ("Save", self.saveRequested, False),
            ("Clear", self.clearRequested, False),
        ):
            btn = QPushButton(label_text)
            btn.setProperty("logAction", "true")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFlat(True)
            btn.setStyleSheet(
                f"QPushButton[logAction=\"true\"] {{"
                f"  background: transparent;"
                f"  border: 0;"
                f"  color: {TOKENS[theme]['fgMuted']};"
                f"  padding: 4px 10px;"
                f"  border-radius: 4px;"
                f"  font-size: 11.5px;"
                f"}}"
                f"QPushButton[logAction=\"true\"]:hover {{"
                f"  color: {TOKENS[theme]['fg']};"
                f"  background: {TOKENS[theme]['bgRaised']};"
                f"}}"
            )
            btn.clicked.connect(sig.emit)
            a.addWidget(btn)
        h.addWidget(actions)

        v.addWidget(header)

        self.view = QPlainTextEdit()
        self.view.setObjectName("logView")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        self.view.setMinimumHeight(140)
        v.addWidget(self.view, stretch=1)

    def _on_autoscroll_toggle(self, on: bool) -> None:
        self._auto_scroll = on

    def set_log_path(self, path: str) -> None:
        self.path_button.setText(path)

    def append_line(self, line: str) -> None:
        self.view.appendPlainText(line)
        if self._auto_scroll:
            sb = self.view.verticalScrollBar()
            sb.setValue(sb.maximum())

    def clear(self) -> None:
        self.view.clear()


# Small "● Auto-scroll" toggleable pill used in the LogPane header.

class _AutoScrollPill(QPushButton):
    def __init__(self, theme: Theme, parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self.setCheckable(True)
        self.setChecked(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setText("Auto-scroll")
        self._apply_styles()

    def _apply_styles(self) -> None:
        t = TOKENS[self._theme]
        # Spacing left of the text leaves room for the ●.
        self.setStyleSheet(
            f"_AutoScrollPill, QPushButton {{"
            f"  background: transparent;"
            f"  border: 0;"
            f"  padding: 4px 10px 4px 18px;"
            f"  border-radius: 10px;"
            f"  font-size: 11px;"
            f"  color: {t['fgMuted']};"
            f"  text-align: left;"
            f"}}"
            f":hover {{ color: {t['fg']}; }}"
        )

    def paintEvent(self, e: QPaintEvent) -> None:
        super().paintEvent(e)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        col = (_qcolor(self._theme, "accent") if self.isChecked()
               else _qcolor(self._theme, "fgDim"))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(col))
        cy = self.height() / 2
        p.drawEllipse(QRectF(8, cy - 3, 6, 6))
        p.end()


# =========================================================================
# CircularRadioButton — perfect-circle indicator (Qt's native QSS
# `border-radius` doesn't always render a true circle for small sizes).
# =========================================================================

class CircularRadioButton(QRadioButton):
    """QRadioButton with a hand-painted 15px circular indicator."""

    INDICATOR_SIZE = 16
    INDICATOR_PAD = 3  # gap between indicator and label

    def __init__(self, theme: Theme = "dark", parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        # Push the text past the indicator we draw ourselves.
        self.setStyleSheet(
            f"QRadioButton {{ padding-left: {self.INDICATOR_SIZE + self.INDICATOR_PAD * 2}px; }}"
        )
        # Indicator-only usage (empty text) still needs visible bounds.
        self.setMinimumSize(self.INDICATOR_SIZE + self.INDICATOR_PAD * 2,
                              self.INDICATOR_SIZE + self.INDICATOR_PAD * 2)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _e: QPaintEvent) -> None:
        # Paint label using the default QRadioButton text rendering, then
        # overlay our circle. We can't easily defer to super().paintEvent
        # because it also paints its own indicator. Easiest: paint text
        # ourselves with QPainter.drawText.
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        size = self.INDICATOR_SIZE
        cy = self.height() / 2
        x, y = self.INDICATOR_PAD, cy - size / 2
        ring = QRectF(x, y, size, size)

        outer = _qcolor(self._theme, "borderStrong")
        accent = _qcolor(self._theme, "accent")
        bg = _qcolor(self._theme, "bgRaised")

        if self.isChecked():
            # Outer ring filled accent.
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(accent))
            p.drawEllipse(ring)
            # Inner hole.
            inner = ring.adjusted(4, 4, -4, -4)
            p.setBrush(QBrush(bg))
            p.drawEllipse(inner)
            # Center dot.
            dot = ring.adjusted(5.5, 5.5, -5.5, -5.5)
            p.setBrush(QBrush(accent))
            p.drawEllipse(dot)
        else:
            p.setPen(QPen(outer, 1.5))
            p.setBrush(QBrush(bg))
            p.drawEllipse(ring.adjusted(0.75, 0.75, -0.75, -0.75))

        # Text.
        p.setPen(_qcolor(self._theme, "fg"))
        font = QFont(ui_family())
        font.setPixelSize(13)
        font.setWeight(QFont.Medium if self.isChecked() else QFont.Normal)
        p.setFont(font)
        text_rect = self.rect().adjusted(
            self.INDICATOR_SIZE + self.INDICATOR_PAD * 2, 0, 0, 0
        )
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self.text())
        p.end()

    def sizeHint(self):
        s = super().sizeHint()
        # Reserve room for the indicator we draw ourselves.
        return s.expandedTo(
            type(s)(self.INDICATOR_SIZE + self.INDICATOR_PAD * 2 + 8, 22)
        )


# =========================================================================
# MacSegmentedTabBar — pill-shaped segmented control. Drives an external
# QTabWidget via currentChanged signal.
# =========================================================================

class MacSegmentedTabBar(QWidget):
    """macOS-style segmented control, centered, replaces QTabBar on mac."""

    currentChanged = Signal(int)

    def __init__(self, labels: Sequence[str], theme: Theme = "dark",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._theme = theme
        self._current = 0
        self._buttons: list[QPushButton] = []
        self._locked: list[bool] = [False] * len(labels)

        self.setFixedHeight(58)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 10, 0, 11)
        outer.setSpacing(0)
        outer.addStretch(1)

        self._pill = QFrame()
        self._pill.setObjectName("macSegPill")
        pill_layout = QHBoxLayout(self._pill)
        pill_layout.setContentsMargins(3, 3, 3, 3)
        pill_layout.setSpacing(0)
        for i, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setProperty("macSegItem", "true")
            btn.setProperty("locked", "false")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.clicked.connect(lambda _=False, idx=i: self.setCurrentIndex(idx))
            pill_layout.addWidget(btn)
            self._buttons.append(btn)

        if self._buttons:
            self._buttons[0].setChecked(True)
        outer.addWidget(self._pill, alignment=Qt.AlignCenter)
        outer.addStretch(1)

        self._apply_styles()

    def _apply_styles(self) -> None:
        t = TOKENS[self._theme]
        self._pill.setStyleSheet(
            f"QFrame#macSegPill {{"
            f"  background: {t['bgSunken']};"
            f"  border: 1px solid {t['border']};"
            f"  border-radius: 9px;"
            f"}}"
            f"QPushButton[macSegItem=\"true\"] {{"
            f"  background: transparent;"
            f"  border: 0;"
            f"  border-radius: 7px;"
            f"  color: {t['fgMuted']};"
            f"  padding: 9px 22px;"
            f"  font-size: 13px;"
            f"  font-weight: 500;"
            f"}}"
            f"QPushButton[macSegItem=\"true\"]:checked {{"
            f"  background: {t['bgRaised']};"
            f"  color: {t['fg']};"
            f"  font-weight: 600;"
            f"}}"
            f"QPushButton[macSegItem=\"true\"]:hover:!checked {{"
            f"  color: {t['fg']};"
            f"}}"
            f"QPushButton[macSegItem=\"true\"][locked=\"true\"] {{"
            f"  color: {t['fgMuted']};"
            f"}}"
            f"QPushButton[macSegItem=\"true\"][locked=\"true\"]:hover {{"
            f"  background: transparent;"
            f"  color: {t['fgMuted']};"
            f"}}"
        )

    def setCurrentIndex(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._buttons) or idx == self._current:
            if 0 <= idx < len(self._buttons):
                self._buttons[idx].setChecked(True)
            return
        if self._locked[idx]:
            # Locked tab — don't switch, and re-check the previous
            # button so the autoexclusive group never lands on the
            # locked item visually.
            self._buttons[self._current].setChecked(True)
            return
        self._current = idx
        self._buttons[idx].setChecked(True)
        self.currentChanged.emit(idx)

    def setTabEnabled(self, idx: int, enabled: bool) -> None:
        # We deliberately keep the button widget enabled even when
        # locked: a disabled QPushButton swallows hover events on
        # macOS, and the ForbiddenCursor we set never appears. The
        # click-gate lives in setCurrentIndex / the lambda below.
        if not (0 <= idx < len(self._buttons)):
            return
        btn = self._buttons[idx]
        self._locked[idx] = not enabled
        btn.setProperty("locked", "true" if not enabled else "false")
        btn.setCursor(Qt.ForbiddenCursor if not enabled
                      else Qt.PointingHandCursor)
        btn.style().unpolish(btn)
        btn.style().polish(btn)

    def currentIndex(self) -> int:
        return self._current


__all__ = [
    "VehicleGlyph",
    "PulsingDot",
    "Spinner",
    "DropZone",
    "ApkCard",
    "ScreensDiagram",
    "Pipeline",
    "GrantMatrix",
    "SuccessBanner",
    "LogPane",
    "CircularRadioButton",
    "MacSegmentedTabBar",
]
