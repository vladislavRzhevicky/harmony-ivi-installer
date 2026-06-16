"""Design tokens + QSS generator for the IVI Installer redesign.

Source of truth for colors / spacing / typography lives in
``design/handoff/tokens.json``. The values here are the Qt-friendly
sRGB-hex fallbacks of those tokens (Qt has no oklch).

Usage::

    from .theme import TOKENS, build_qss, apply_app_theme

    apply_app_theme(QApplication.instance(), theme="dark")

The QSS targets are addressable by ``objectName`` rather than class
selectors, so existing widgets keep working — we just decorate the new
ones with ``setObjectName(...)``.
"""
from __future__ import annotations

import sys
from typing import Literal

from PySide6.QtGui import QFont, QFontDatabase, QPalette, QColor
from PySide6.QtWidgets import QApplication

Theme = Literal["dark", "light"]
OS = Literal["mac", "win", "linux"]


# ---- font stacks ---------------------------------------------------------

_UI_STACK_MAC = ["-apple-system", "SF Pro Text", "Helvetica Neue"]
_UI_STACK_WIN = ["Segoe UI Variable Text", "Segoe UI", "Inter"]
_UI_STACK_LIN = ["Inter", "Cantarell", "DejaVu Sans"]

_MONO_STACK_MAC = ["SF Mono", "Menlo", "Monaco"]
_MONO_STACK_WIN = ["Cascadia Mono", "Consolas", "Lucida Console"]
_MONO_STACK_LIN = ["JetBrains Mono", "DejaVu Sans Mono", "monospace"]


def detect_os() -> OS:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "win"
    return "linux"


def _first_available(candidates: list[str]) -> str:
    families = set(QFontDatabase.families())
    for c in candidates:
        if c in families:
            return c
    return candidates[-1]


def ui_family(os_name: OS | None = None) -> str:
    os_name = os_name or detect_os()
    if os_name == "mac":
        return _first_available(_UI_STACK_MAC)
    if os_name == "win":
        return _first_available(_UI_STACK_WIN)
    return _first_available(_UI_STACK_LIN)


def mono_family(os_name: OS | None = None) -> str:
    os_name = os_name or detect_os()
    if os_name == "mac":
        return _first_available(_MONO_STACK_MAC)
    if os_name == "win":
        return _first_available(_MONO_STACK_WIN)
    return _first_available(_MONO_STACK_LIN)


# ---- tokens (sRGB hex fallbacks of tokens.json) -------------------------

TOKENS: dict[Theme, dict[str, str]] = {
    "dark": {
        "bg":           "#131619",   # tab content (working area)
        "bgRaised":     "#1d1e21",   # cards (install-on / pipeline / matrix / drop zone surround)
        "bgSunken":     "#0a0e0f",   # log body, inputs, drop-zone interior
        "bgChrome":     "#1d1e21",   # top device strip + tab strip
        "bgLogHeader":  "#1e2123",   # log header bar (subtle delta vs chrome)
        "border":       "#3b3f45",
        "borderStrong": "#4d525a",
        "fg":           "#e9eaec",
        "fgMuted":      "#aeb2b8",
        "fgDim":        "#7d8189",
        "accent":       "#5cc7c4",
        "accentDim":    "#3aa2a4",
        "accentFg":     "#1d2026",
        "good":         "#7fd6a4",
        "bad":          "#e8806e",
        "warn":         "#dec07a",
        "warnTint":     "#3a3530",
        "goodTint":     "#2f3a35",
    },
    "light": {
        "bg":           "#f7f8f9",
        "bgRaised":     "#ffffff",
        "bgSunken":     "#f1f2f4",
        "bgChrome":     "#ebedef",
        "bgLogHeader":  "#e8eaed",
        "border":       "#d8dadd",
        "borderStrong": "#bdc1c6",
        "fg":           "#2a2e34",
        "fgMuted":      "#5d626a",
        "fgDim":        "#7c818a",
        "accent":       "#3aa2a4",
        "accentDim":    "#2c8a8c",
        "accentFg":     "#fbfcfd",
        "good":         "#1f9168",
        "bad":          "#c64325",
        "warn":         "#a87a1a",
        "warnTint":     "#fff5e0",
        "goodTint":     "#e6f4ec",
    },
}


def color(theme: Theme, name: str) -> str:
    """Look up a token color (hex)."""
    return TOKENS[theme][name]


# ---- QSS -----------------------------------------------------------------

def build_qss(theme: Theme = "dark") -> str:
    t = TOKENS[theme]
    ui = ui_family()
    mono = mono_family()
    return f"""
/* ============== global =============== */
* {{
    font-family: "{ui}";
    color: {t['fg']};
    selection-background-color: {t['accent']};
    selection-color: {t['accentFg']};
}}
QMainWindow, #centralRoot {{
    background: {t['bg']};
}}
/* Backgrounds belong to containers, not text widgets — otherwise QLabel
   inside a bgRaised card paints a bg-colored rectangle over its text
   and produces visible boxy artefacts on every tab. */
QLabel, QCheckBox, QRadioButton {{
    background: transparent;
}}
QToolTip {{
    background: {t['bgRaised']};
    color: {t['fg']};
    border: 1px solid {t['border']};
    padding: 4px 6px;
}}

/* ============== device strip =============== */
#deviceStrip {{
    background: {t['bgRaised']};
    border-bottom: 1px solid {t['border']};
}}
#deviceStrip[state="unauthorized"] {{ background: {t['warnTint']}; }}
#deviceStrip QLabel {{ background: transparent; }}
#deviceTitle {{
    font-size: 14px;
    font-weight: 600;
    letter-spacing: -0.1px;
    color: {t['fg']};
}}
#deviceMeta, #deviceSerial {{
    font-family: "{mono}";
    font-size: 11px;
    color: {t['fgDim']};
}}
#deviceFw {{
    font-family: "{mono}";
    font-size: 11px;
    color: {t['fgMuted']};
}}
#deviceUser {{
    font-family: "{mono}";
    font-size: 11px;
    color: {t['fgMuted']};
}}
#deviceConnLabel {{
    font-size: 12px;
    color: {t['fgMuted']};
}}

/* ============== tabs =============== */
QTabWidget::pane {{
    border: 0;
    background: {t['bg']};
    top: -1px;
}}
QTabBar {{
    background: {t['bgRaised']};
    border-bottom: 1px solid {t['border']};
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    padding: 9px 16px 8px;
    margin-right: 2px;
    background: transparent;
    color: {t['fgMuted']};
    border: 0;
    border-bottom: 2px solid transparent;
    font-size: 12.5px;
    font-weight: 400;
}}
QTabBar::tab:hover {{ color: {t['fg']}; }}
QTabBar::tab:selected {{
    color: {t['fg']};
    font-weight: 600;
    border-bottom: 2px solid {t['accent']};
}}

/* ============== buttons =============== */
QPushButton {{
    background: {t['bgRaised']};
    color: {t['fg']};
    border: 1px solid {t['border']};
    border-radius: 6px;
    padding: 5px 12px;
    font-size: 12px;
    font-weight: 500;
}}
QPushButton:hover {{ border-color: {t['borderStrong']}; }}
QPushButton:pressed {{ background: {t['bgSunken']}; }}
QPushButton:disabled {{ color: {t['fgDim']}; border-color: {t['border']}; }}
QPushButton#primary {{
    background: {t['accent']};
    color: {t['accentFg']};
    border: 0;
    border-radius: 18px;
    padding: 8px 22px;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.1px;
}}
QPushButton#primary:hover {{ background: {t['accentDim']}; }}
QPushButton#primary:disabled {{
    background: {t['border']};
    color: {t['fgDim']};
}}
QPushButton[ghost="true"] {{
    background: transparent;
    border: 0;
    color: {t['fgMuted']};
    padding: 3px 8px;
}}
QPushButton[ghost="true"]:hover {{ color: {t['fg']}; }}
QPushButton[link="true"] {{
    background: transparent;
    border: 0;
    color: {t['accent']};
    padding: 0;
    font-size: 11.5px;
    text-decoration: underline;
}}

/* ============== inputs =============== */
QLineEdit, QComboBox, QPlainTextEdit, QListWidget {{
    background: {t['bgSunken']};
    color: {t['fg']};
    border: 1px solid {t['border']};
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: {t['accent']};
    selection-color: {t['accentFg']};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QListWidget:focus {{
    border-color: {t['accent']};
}}
QComboBox::drop-down {{ border: 0; width: 20px; }}
QComboBox::down-arrow {{
    image: none;
    width: 8px; height: 8px;
}}

/* ============== list rows =============== */
QListWidget {{ padding: 0; }}
QListWidget::item {{
    padding: 7px 12px;
    border-bottom: 1px solid {t['border']};
    color: {t['fg']};
}}
QListWidget::item:selected {{
    background: {t['bgRaised']};
    color: {t['fg']};
    font-weight: 600;
    border-left: 2px solid {t['accent']};
}}

/* ============== checkbox / radio =============== */
QCheckBox, QRadioButton {{
    color: {t['fg']};
    font-size: 12.5px;
    spacing: 8px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 15px; height: 15px;
    border: 1.5px solid {t['borderStrong']};
    border-radius: 3px;
    background: {t['bgRaised']};
}}
QCheckBox::indicator:checked {{
    background: {t['accent']};
    border-color: {t['accent']};
}}
/* CircularRadioButton in widgets.py paints its own circular indicator;
   this rule keeps native indicator visually hidden so CSS doesn't draw
   over it. Plain QRadioButton instances (the hidden strat radios) are
   never visible so the size-0 indicator is harmless for them. */
CircularRadioButton::indicator {{
    width: 0px; height: 0px;
}}
QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1.5px solid {t['borderStrong']};
    border-radius: 7px;
    background: {t['bgRaised']};
}}
QRadioButton::indicator:checked {{
    border: 4px solid {t['accent']};
    background: {t['bgRaised']};
}}

/* ============== cards / sections =============== */
QFrame#card {{
    background: {t['bgRaised']};
    border: 1px solid {t['border']};
    border-radius: 8px;
}}
QFrame#sunken {{
    background: {t['bgSunken']};
    border: 1px solid {t['border']};
    border-radius: 6px;
}}
QLabel[role="sectionLabel"] {{
    color: {t['fgDim']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1.1px;
}}
QLabel[role="sectionHint"] {{
    color: {t['fgDim']};
    font-size: 11px;
}}
QLabel[role="title"] {{
    color: {t['fg']};
    font-size: 13px;
    font-weight: 600;
}}
QLabel[role="muted"] {{
    color: {t['fgMuted']};
    font-size: 12px;
}}
QLabel[role="dim"] {{
    color: {t['fgDim']};
    font-size: 11.5px;
}}
QLabel[mono="true"] {{
    font-family: "{mono}";
}}

/* ============== log pane =============== */
#logHeader {{
    background: {t['bgLogHeader']};
    border-top: 1px solid {t['border']};
    border-bottom: 1px solid {t['border']};
}}
#logHeaderLabel {{
    color: {t['fgDim']};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1.1px;
}}
#logPathLink {{
    font-family: "{mono}";
    color: {t['accent']};
    background: transparent;
    border: 0;
    text-decoration: underline;
    font-size: 11px;
}}
#logView {{
    background: {t['bgSunken']};
    color: {t['fg']};
    border: 0;
    border-radius: 0;
    font-family: "{mono}";
    font-size: 11px;
    padding: 6px 12px;
}}

/* ============== status bar / progress =============== */
QStatusBar {{
    background: {t['bgChrome']};
    color: {t['fgMuted']};
    border-top: 1px solid {t['border']};
}}
QProgressBar {{
    background: {t['bgSunken']};
    border: 1px solid {t['border']};
    border-radius: 4px;
    text-align: center;
    color: {t['fgMuted']};
    height: 14px;
}}
QProgressBar::chunk {{
    background: {t['accent']};
    border-radius: 3px;
}}

/* ============== scrollbar =============== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t['borderStrong']};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::handle:vertical:hover {{ background: {t['fgDim']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{ height: 10px; background: transparent; }}
QScrollBar::handle:horizontal {{
    background: {t['borderStrong']};
    border-radius: 4px;
    min-width: 20px;
}}

/* ============== dropzone =============== */
#dropZone {{
    background: {t['bgSunken']};
    border: 1px dashed {t['borderStrong']};
    border-radius: 8px;
}}
#dropZone[active="true"] {{
    border-color: {t['accent']};
    background: {t['bgRaised']};
}}

/* ============== banners =============== */
#successBanner {{
    background: {t['goodTint']};
    border: 1px solid {t['good']};
    border-radius: 8px;
}}
#warnBanner {{
    background: {t['warnTint']};
    border: 1px solid {t['warn']};
    border-radius: 8px;
}}

/* ============== splitter (resize handle between tabs and log) =============== */
QSplitter#bodySplitter::handle:vertical {{
    background: {t['bg']};
    border-top: 1px solid {t['border']};
    border-bottom: 1px solid {t['border']};
    height: 6px;
}}
QSplitter#bodySplitter::handle:vertical:hover {{
    background: {t['bgRaised']};
    border-top: 1px solid {t['borderStrong']};
    border-bottom: 1px solid {t['borderStrong']};
}}

/* ============== misc separators =============== */
QFrame[role="hr"] {{
    background: {t['border']};
    max-height: 1px;
    min-height: 1px;
    border: 0;
}}
""".strip()


def apply_app_theme(app: QApplication, theme: Theme = "dark") -> None:
    """Apply the redesign theme — palette + QSS — to a QApplication."""
    app.setStyle("Fusion")
    pal = QPalette()
    t = TOKENS[theme]
    pal.setColor(QPalette.Window, QColor(t["bg"]))
    pal.setColor(QPalette.WindowText, QColor(t["fg"]))
    pal.setColor(QPalette.Base, QColor(t["bgSunken"]))
    pal.setColor(QPalette.AlternateBase, QColor(t["bgRaised"]))
    pal.setColor(QPalette.ToolTipBase, QColor(t["bgRaised"]))
    pal.setColor(QPalette.ToolTipText, QColor(t["fg"]))
    pal.setColor(QPalette.Text, QColor(t["fg"]))
    pal.setColor(QPalette.Button, QColor(t["bgRaised"]))
    pal.setColor(QPalette.ButtonText, QColor(t["fg"]))
    pal.setColor(QPalette.Highlight, QColor(t["accent"]))
    pal.setColor(QPalette.HighlightedText, QColor(t["accentFg"]))
    pal.setColor(QPalette.PlaceholderText, QColor(t["fgDim"]))
    app.setPalette(pal)
    app.setStyleSheet(build_qss(theme))
    # Set a default app font sized to match the design token "body".
    f = QFont(ui_family())
    f.setPixelSize(12)
    app.setFont(f)


__all__ = [
    "TOKENS",
    "Theme",
    "build_qss",
    "apply_app_theme",
    "color",
    "ui_family",
    "mono_family",
    "detect_os",
]
