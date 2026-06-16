"""Smoke tests for the PySide6 MainWindow.

We never start polling — the long-lived DevicePollerWorker is the
biggest source of flakiness in headless Qt tests. Instead, we feed a
DeviceStatus directly into the public `_on_status` slot and assert the
visible UI ends up in the expected shape.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from PySide6.QtCore import Qt

from ivi_installer import adb as adb_module
from ivi_installer.devices import (
    Device,
    DeviceCapabilities,
    DeviceInfo,
)
from ivi_installer.ui.device_status import DeviceStatus


# ---- factories ----

def _avatr_caps(is_root=True):
    return DeviceCapabilities(
        is_root=is_root, has_hdc=True, is_harmony=True,
        is_avatr_ivi=True, android_api=31,
    )


def _phone_caps():
    return DeviceCapabilities(
        is_root=False, has_hdc=False, is_harmony=True,
        is_avatr_ivi=False, android_api=32,
    )


def _avatr_info(serial="S1", is_root=True):
    return DeviceInfo(
        serial=serial, state="device",
        product_code="ICHU3200F2-ADV", model_name="AVATR_12",
        label="Avatr 12", android_release="12", android_api=31,
        harmonyos_version="3.0.0", cpu_abi="arm64-v8a", locale="zh-CN",
        adbd_user="root" if is_root else "shell", hdbd_count=2,
        is_test_device=False, capabilities=_avatr_caps(is_root=is_root),
    )


def _phone_info(serial="ABCDEF"):
    return DeviceInfo(
        serial=serial, state="device",
        product_code="HWABR", model_name="ABR-AL60",
        label="HWABR", android_release="12", android_api=32,
        harmonyos_version="4.2.0", cpu_abi="arm64-v8a", locale="ru-RU",
        adbd_user="shell", hdbd_count=0, is_test_device=False,
        capabilities=_phone_caps(),
    )


def _avatr_device(serial="S1"):
    return Device(serial=serial, state="device",
                    product="ICHU3200F2-ADV", model="Avatr 12",
                    transport_id="1")


def _phone_device(serial="ABCDEF"):
    return Device(serial=serial, state="device",
                    product="HWABR", model=None, transport_id="1")


def _avatr_status(serial="S1", is_root=True):
    sel = _avatr_device(serial)
    return DeviceStatus(devices=[sel], selected=sel,
                          info=_avatr_info(serial, is_root=is_root),
                          adb_present=True, multiple=False)


def _phone_status(serial="ABCDEF"):
    sel = _phone_device(serial)
    return DeviceStatus(devices=[sel], selected=sel,
                          info=_phone_info(serial),
                          adb_present=True, multiple=False)


def _empty_status():
    return DeviceStatus(devices=[], selected=None, info=None,
                          adb_present=True, multiple=False)


def _unauthorized_status():
    d = Device(serial="X1", state="unauthorized",
                product=None, model=None, transport_id=None)
    return DeviceStatus(devices=[d], selected=None, info=None,
                          adb_present=True, multiple=False)


# ---- fixture ----

@pytest.fixture
def main_window(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(adb_module, "find_adb",
                          lambda: "/usr/local/bin/adb")
    # Isolate the on-disk settings store so test runs don't leak into
    # each other (e.g. the strategy radio writing back to settings.json).
    from ivi_installer import settings as _settings
    monkeypatch.setattr(_settings, "_STORE_PATH",
                          tmp_path / "settings.json")
    from ivi_installer.ui.main_window import MainWindow
    # `_kick_tz_read` would spawn a real QThread that calls `adb run`;
    # in headless tests we want the slot to be a no-op. Tests that need
    # to verify the read flow call `_on_tz_read` directly.
    monkeypatch.setattr(MainWindow, "_kick_tz_read", lambda self: None)
    monkeypatch.setattr(MainWindow, "_kick_device_info_read",
                          lambda self: None)
    w = MainWindow()
    qtbot.addWidget(w)
    # Polling is *not* started in tests — start_polling() is the
    # explicit hook for the production __main__.
    return w


# ---- construction smoke ----

def test_window_builds(main_window):
    # Title carries the package version so the running build is
    # identifiable from the OS taskbar/dock.
    from ivi_installer import __version__
    assert main_window.windowTitle() == f"IVI Installer v{__version__}"
    assert main_window.install_button.isEnabled() is False
    assert "No device" in main_window.status_widget.main_label.text()


def test_install_button_disabled_without_file(main_window):
    main_window._on_status(_avatr_status())
    assert main_window.install_button.isEnabled() is False


def test_install_button_enabled_with_device_and_file(main_window, tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    main_window._on_status(_avatr_status(is_root=False))
    main_window._selected_path = apk
    main_window._update_install_button()
    assert main_window.install_button.isEnabled() is True


# ---- post-5.0 UI: simplified to one strategy + screen choice ----

def test_install_target_screens_default_to_all_three(main_window):
    # The dangerous checkboxes must not exist.
    assert not hasattr(main_window, "system_app_cb")
    assert not hasattr(main_window, "disable_market_cb")
    assert not hasattr(main_window, "reboot_after_cb")
    assert not hasattr(main_window, "package_input")
    # And neither should the per-strategy controls from the old UI.
    assert not hasattr(main_window, "target_user_combo")
    assert not hasattr(main_window, "installer_pkg_edit")
    assert not hasattr(main_window, "auto_run_button")
    # The 0.8.6 UI: one Install button + a 3-checkbox screen grid
    # (Driver / Passenger / Rear), all three checked by default.
    for key in ("driver", "passenger", "rear"):
        assert main_window._screen_checks[key].isChecked() is True
    # All checked → legacy "all multimedia screens" behaviour.
    assert main_window._selected_target_users() is None
    # Untick everything except Driver → returns the resolved user ids
    # for that category.
    main_window._screen_checks["passenger"].setChecked(False)
    main_window._screen_checks["rear"].setChecked(False)
    assert main_window._screen_checks["driver"].isChecked() is True
    targets = main_window._selected_target_users()
    assert targets is not None and 13 in targets


def test_install_enabled_for_phone_without_root(main_window, tmp_path):
    """v3: standard pm install works under shell-user adbd. No root needed."""
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    main_window._on_status(_phone_status())
    main_window._selected_path = apk
    main_window._update_install_button()
    assert main_window.install_button.isEnabled() is True


def test_unauthorized_shows_warning_status(main_window):
    main_window._on_status(_unauthorized_status())
    text = main_window.status_widget.main_label.text()
    assert "Authorizing" in text or "unauthorized" in text.lower()
    # Capabilities are unknown → install is disabled.
    assert main_window.install_button.isEnabled() is False


def test_disconnect_resets_capabilities(main_window):
    main_window._on_status(_phone_status())
    assert main_window._capabilities is not None
    main_window._on_status(_empty_status())
    assert main_window._capabilities is None
    assert main_window._selected_serial is None


# ---- file selection ----

def test_clear_file_resets_label(main_window, tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    main_window._selected_path = apk
    main_window.file_label.setText(str(apk))
    main_window._clear_file()
    assert main_window._selected_path is None
    assert main_window.file_label.text() == "—"


# ---- timezone tab ----

def test_tabs_present(main_window):
    titles = [main_window.tabs.tabText(i) for i in range(main_window.tabs.count())]
    assert "Install APK" in titles
    assert "Keyboards" in titles
    assert "Tools" in titles
    assert "Timezone" in titles
    assert "Device info" in titles


# ---- v0.7.0: competitor parity ----

def test_strategy_radio_defaults_to_pm_disable(main_window):
    """v0.8.0: the lighter pm-disable path is the default primary."""
    assert main_window.strat_pmdisable_radio.isChecked() is True
    assert main_window.strat_hdb_radio.isChecked() is False
    assert main_window._selected_primary_strategy() == "pm_disable_install"
    main_window.strat_hdb_radio.setChecked(True)
    assert main_window._selected_primary_strategy() == "hdb_broker_install"


def test_force_reinstall_checkbox_present_and_unchecked(main_window):
    assert main_window.force_reinstall_check.isChecked() is False


def test_appgallery_input_present(main_window):
    assert main_window.appgallery_input is not None
    assert main_window.appgallery_button is not None
    assert main_window.appgallery_progress.isVisible() is False


def test_source_tabs_default_to_file(main_window):
    """The segmented File / From URL picker defaults to File."""
    assert main_window._source_tab_file.isChecked() is True
    assert main_window._source_tab_url.isChecked() is False
    assert main_window.source_stack.currentIndex() == 0


def test_source_tab_url_switches_stack(main_window):
    main_window._source_tab_url.setChecked(True)
    assert main_window.source_stack.currentIndex() == 1
    main_window._source_tab_file.setChecked(True)
    assert main_window.source_stack.currentIndex() == 0


def test_appgallery_hotkey_focuses_url_tab(main_window):
    """Ctrl+Shift+D should switch to the URL source tab + focus input."""
    main_window._prompt_appgallery()
    assert main_window._source_tab_url.isChecked() is True
    assert main_window.source_stack.currentIndex() == 1


def test_source_tabs_lock_when_file_staged(main_window, tmp_path):
    """Once a file is staged, both source tabs become locked — the
    `locked` property flips to "true" and the cursor swaps to
    ForbiddenCursor. We keep them enabled so they still receive
    enter/leave (otherwise the user wouldn't see the cursor change);
    a `_LockedTabFilter` swallows the actual presses."""
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK\x03\x04")
    from PySide6.QtCore import Qt
    assert main_window._source_tab_file.property("locked") in (None, "false")
    assert main_window._source_tab_url.property("locked") in (None, "false")
    main_window._set_apk_file(apk)
    for btn in (main_window._source_tab_file, main_window._source_tab_url):
        assert btn.property("locked") == "true"
        assert btn.cursor().shape() == Qt.ForbiddenCursor
    main_window._clear_file()
    for btn in (main_window._source_tab_file, main_window._source_tab_url):
        assert btn.property("locked") == "false"
        assert btn.cursor().shape() == Qt.PointingHandCursor


def test_keyboards_tab_has_celia_install_button(main_window):
    assert main_window.kb_install_button.text() == "Install Celia Keyboard"
    assert main_window.kb_set_default_check.isChecked() is False
    # No device → buttons disabled.
    main_window._update_install_button()
    assert main_window.kb_install_button.isEnabled() is False


def test_appgallery_button_enabled_without_device(main_window):
    """Downloads only need network — not a connected car. The button
    is gated on a valid app id being typed, not on device state."""
    main_window.appgallery_input.setText("C101898721")
    main_window._update_install_button()
    assert main_window.appgallery_button.isEnabled() is True


def test_appgallery_button_disabled_when_input_empty(main_window):
    main_window.appgallery_input.setText("")
    main_window._update_install_button()
    assert main_window.appgallery_button.isEnabled() is False


def test_appgallery_button_disabled_for_garbage_input(main_window):
    main_window.appgallery_input.setText("not an id")
    main_window._update_install_button()
    assert main_window.appgallery_button.isEnabled() is False


def test_device_info_render_populates_view(main_window):
    main_window._on_status(_phone_status(serial="ABCDEF"))
    sections = [
        ("Identity", [("Serial", "ABCDEF"), ("Brand", "Huawei")]),
        ("Build & OS", [("Android version", "12")]),
    ]
    main_window._on_device_info_read("ABCDEF", sections)
    text = main_window.info_view.toPlainText()
    assert "Identity" in text
    assert "ABCDEF" in text
    assert "Huawei" in text


def test_device_info_ignores_stale_serial(main_window):
    main_window._on_status(_phone_status(serial="ABCDEF"))
    main_window.info_view.setPlainText("kept")
    main_window._on_device_info_read("OLD", [("Identity", [("x", "y")])])
    assert main_window.info_view.toPlainText() == "kept"


def test_device_info_error_shown_in_view(main_window):
    main_window._on_status(_phone_status(serial="ABCDEF"))
    main_window._on_device_info_error("AdbError: device offline")
    assert "Failed to read device info" in main_window.info_view.toPlainText()


def test_timezone_list_populated(main_window):
    # Should contain at least the common zones.
    n = main_window.tz_list.count()
    assert n > 100  # zoneinfo ships ~400+ entries
    labels = [main_window.tz_list.item(i).data(Qt.UserRole)
              for i in range(n)]
    assert "Europe/Moscow" in labels
    assert "Asia/Shanghai" in labels
    assert "UTC" in labels


def test_apply_tz_disabled_without_device(main_window):
    main_window._on_status(_empty_status())
    # Even after picking a tz, apply stays off without a device.
    for i in range(main_window.tz_list.count()):
        if main_window.tz_list.item(i).data(Qt.UserRole) == "Europe/Moscow":
            main_window.tz_list.setCurrentRow(i)
            break
    assert main_window.apply_tz_button.isEnabled() is False


def test_apply_tz_disabled_when_same_as_current(main_window):
    main_window._on_status(_phone_status())
    main_window._current_tz = "Europe/Moscow"
    # Pre-select Europe/Moscow.
    for i in range(main_window.tz_list.count()):
        if main_window.tz_list.item(i).data(Qt.UserRole) == "Europe/Moscow":
            main_window.tz_list.setCurrentRow(i)
            break
    main_window._update_apply_tz_button()
    assert main_window.apply_tz_button.isEnabled() is False


def test_apply_tz_enabled_when_different_zone_selected(main_window):
    main_window._on_status(_phone_status())
    main_window._current_tz = "Europe/Moscow"
    for i in range(main_window.tz_list.count()):
        if main_window.tz_list.item(i).data(Qt.UserRole) == "Asia/Shanghai":
            main_window.tz_list.setCurrentRow(i)
            break
    main_window._update_apply_tz_button()
    assert main_window.apply_tz_button.isEnabled() is True


def test_tz_filter_hides_non_matching(main_window):
    main_window.tz_search.setText("moscow")
    moscow_visible = False
    shanghai_hidden = False
    for i in range(main_window.tz_list.count()):
        item = main_window.tz_list.item(i)
        tz = item.data(Qt.UserRole)
        if tz == "Europe/Moscow":
            moscow_visible = not item.isHidden()
        if tz == "Asia/Shanghai":
            shanghai_hidden = item.isHidden()
    assert moscow_visible is True
    assert shanghai_hidden is True


def test_tz_filter_clears(main_window):
    main_window.tz_search.setText("moscow")
    main_window.tz_search.setText("")
    # Everything visible again.
    hidden = sum(1 for i in range(main_window.tz_list.count())
                  if main_window.tz_list.item(i).isHidden())
    assert hidden == 0


def test_tz_read_result_updates_label(main_window):
    main_window._on_status(_phone_status(serial="ABCDEF"))
    main_window._on_tz_read("ABCDEF", "Asia/Shanghai")
    assert main_window.current_tz_label.text() == "Asia/Shanghai"
    assert main_window._current_tz == "Asia/Shanghai"


def test_tz_read_result_for_other_serial_ignored(main_window):
    main_window._on_status(_phone_status(serial="ABCDEF"))
    # Stale read for a different serial — must be discarded.
    main_window._on_tz_read("OTHER", "Europe/Berlin")
    assert main_window._current_tz != "Europe/Berlin"


def test_tz_write_success_updates_state_and_persists(main_window, monkeypatch):
    saved = {}
    monkeypatch.setattr(
        "ivi_installer.ui.main_window.settings.set",
        lambda k, v: saved.update({k: v}),
    )
    main_window._on_status(_phone_status())
    main_window._current_tz = "Europe/Moscow"
    main_window._on_tz_write_result(True, "Asia/Shanghai", "Asia/Shanghai")
    assert main_window._current_tz == "Asia/Shanghai"
    assert main_window.current_tz_label.text() == "Asia/Shanghai"
    assert saved.get("last_used_timezone") == "Asia/Shanghai"


def test_tz_write_unchanged_warns(main_window):
    main_window._on_status(_phone_status())
    main_window._current_tz = "Europe/Moscow"
    main_window._on_tz_write_result(False, "Asia/Shanghai", "Europe/Moscow")
    text = main_window.log_view.toPlainText()
    assert "device reports" in text.lower() or "still reports" in text.lower()
    # Verified value is whatever the device returned (kept truthful).
    assert main_window._current_tz == "Europe/Moscow"


# ---- log ----

def test_log_user_appears_in_visible_pane(main_window):
    main_window._log_user("hello")
    main_window._log_user("world")
    text = main_window.log_view.toPlainText()
    assert "hello" in text and "world" in text


def test_log_full_stays_off_visible_pane(main_window):
    # Verbose breadcrumbs must NOT leak to the on-screen log — they
    # would expose adb commands and device replies to the end user.
    main_window._log_full("$ adb shell pm install ...")
    main_window._log_full("device responded: Success")
    text = main_window.log_view.toPlainText()
    assert "adb" not in text
    assert "device responded" not in text


def test_toast_writes_to_status_bar(main_window):
    main_window._toast("hi", kind="info")
    assert "hi" in main_window.statusBar().currentMessage()


# ---- serial-change confirmation ----

def test_serial_change_confirms_and_resets_file(main_window, tmp_path):
    """Pretend the user accepts switching to a new serial: file selection
    is cleared so we don't push to the wrong unit."""
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")

    # First connection: phone A.
    main_window._on_status(_phone_status(serial="AAA"))
    main_window._selected_path = apk
    main_window.file_label.setText(str(apk))

    # User accepts the swap to phone B.
    with patch("ivi_installer.ui.main_window.QMessageBox.question",
                return_value=__import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox.Yes):
        main_window._on_status(_phone_status(serial="BBB"))

    assert main_window._selected_serial == "BBB"
    assert main_window._selected_path is None  # cleared


def test_serial_change_decline_keeps_old(main_window):
    """Decline → state stays pinned to the old serial."""
    main_window._on_status(_phone_status(serial="AAA"))
    main_window._poller = None  # don't try to set_preferred_serial
    QMB = __import__("PySide6.QtWidgets", fromlist=["QMessageBox"]).QMessageBox
    with patch("ivi_installer.ui.main_window.QMessageBox.question",
                return_value=QMB.No):
        main_window._on_status(_phone_status(serial="BBB"))
    assert main_window._selected_serial == "AAA"
