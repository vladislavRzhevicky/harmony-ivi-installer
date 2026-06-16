"""Entry point.

Default: launches the PySide6 GUI.
`python -m ivi_installer --cli` prints the device list and exits — useful
on a developer's machine before the GUI exists, and as a sanity check
that adb resolution works.
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import adb, devices


def _run_cli() -> int:
    found = adb.find_adb()
    if not found:
        print(
            "adb not found. Run: brew install android-platform-tools "
            "(or launch the GUI to download it).",
            file=sys.stderr,
        )
        return 1
    print(f"adb: {found}")
    devs = devices.list_devices()
    if not devs:
        print("No devices. Connect USB-C to a dealer-unlocked IVI and try again.")
        return 1
    for d in devs:
        label = d.model or d.product or "—"
        print(f"  {d.serial:<16} state={d.state:<14} {label}")
    return 0


def _run_gui() -> int:
    # Defer the PySide6 import so `--cli` works without Qt installed
    # (relevant for headless CI / older venvs).
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui.theme import apply_app_theme

    app = QApplication(sys.argv)
    app.setApplicationName("IVI Installer")
    apply_app_theme(app, theme="dark")
    window = MainWindow()
    window.show()
    window.start_polling()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    from . import logging_setup
    log_path = logging_setup.setup_logging(level=logging.INFO)
    logging.getLogger(__name__).info("logging to %s", log_path)
    parser = argparse.ArgumentParser(prog="ivi-installer")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="print connected devices and exit (no GUI)",
    )
    args = parser.parse_args(argv)
    return _run_cli() if args.cli else _run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
