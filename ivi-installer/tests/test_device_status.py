"""Tests for the DeviceStatusWidget and DevicePollerWorker."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from ivi_installer import devices as devices_module
from ivi_installer.devices import (
    Device,
    DeviceCapabilities,
    DeviceInfo,
)
from ivi_installer.ui.device_status import (
    COLOR_GREEN,
    COLOR_GREY,
    COLOR_ORANGE,
    COLOR_RED,
    COLOR_YELLOW,
    DevicePollerWorker,
    DeviceStatus,
    DeviceStatusWidget,
)


# ---- factories ----

def _avatr_status(serial="S1") -> DeviceStatus:
    selected = Device(
        serial=serial, state="device", product="ICHU3200F2-ADV",
        model="Avatr 12", transport_id="1",
    )
    info = DeviceInfo(
        serial=serial, state="device",
        product_code="ICHU3200F2-ADV", model_name="AVATR_12",
        label="Avatr 12", android_release="12", android_api=31,
        harmonyos_version="3.0.0", cpu_abi="arm64-v8a", locale="zh-CN",
        adbd_user="root", hdbd_count=2, is_test_device=False,
        capabilities=DeviceCapabilities(
            is_root=True, has_hdc=True, is_harmony=True,
            is_avatr_ivi=True, android_api=31,
        ),
    )
    return DeviceStatus(devices=[selected], selected=selected, info=info,
                         adb_present=True, multiple=False)


def _phone_status(serial="ABCDEF") -> DeviceStatus:
    selected = Device(
        serial=serial, state="device", product="HWABR",
        model=None, transport_id="1",
    )
    info = DeviceInfo(
        serial=serial, state="device",
        product_code="HWABR", model_name="ABR-AL60",
        label="HWABR", android_release="12", android_api=32,
        harmonyos_version="4.2.0", cpu_abi="arm64-v8a", locale="ru-RU",
        adbd_user="shell", hdbd_count=0, is_test_device=False,
        capabilities=DeviceCapabilities(
            is_root=False, has_hdc=False, is_harmony=True,
            is_avatr_ivi=False, android_api=32,
        ),
    )
    return DeviceStatus(devices=[selected], selected=selected, info=info,
                         adb_present=True, multiple=False)


def _empty_status() -> DeviceStatus:
    return DeviceStatus(devices=[], selected=None, info=None,
                         adb_present=True, multiple=False)


def _no_adb_status() -> DeviceStatus:
    return DeviceStatus(devices=[], selected=None, info=None,
                         adb_present=False, multiple=False)


def _unauthorized_status() -> DeviceStatus:
    d = Device(serial="X1", state="unauthorized",
                product=None, model=None, transport_id=None)
    return DeviceStatus(devices=[d], selected=None, info=None,
                         adb_present=True, multiple=False)


def _offline_status() -> DeviceStatus:
    d = Device(serial="X1", state="offline",
                product=None, model=None, transport_id=None)
    return DeviceStatus(devices=[d], selected=None, info=None,
                         adb_present=True, multiple=False)


def _multiple_status() -> DeviceStatus:
    a = Device(serial="A", state="device", product="HWABR",
                model=None, transport_id="1")
    b = Device(serial="B", state="device", product="HWABR",
                model=None, transport_id="2")
    return DeviceStatus(devices=[a, b], selected=a, info=None,
                         adb_present=True, multiple=True)


# ---- DeviceStatusWidget rendering ----

@pytest.fixture
def widget(qtbot):
    w = DeviceStatusWidget()
    qtbot.addWidget(w)
    return w


def _dot_color(widget) -> str:
    return widget.dot_label.styleSheet()


def test_widget_initial_state_shows_no_device(widget):
    # ctor calls set_status with an empty DeviceStatus.
    assert "No device" in widget.main_label.text()
    assert COLOR_RED in _dot_color(widget)


def test_widget_no_device_state(widget):
    widget.set_status(_empty_status())
    assert "No device" in widget.main_label.text()
    assert COLOR_RED in _dot_color(widget)
    assert widget.badge_label.isHidden() is True


def test_widget_unauthorized_state(widget):
    widget.set_status(_unauthorized_status())
    assert "Authorizing" in widget.main_label.text()
    assert COLOR_YELLOW in _dot_color(widget)


def test_widget_offline_state(widget):
    widget.set_status(_offline_status())
    assert "offline" in widget.main_label.text().lower()
    assert COLOR_YELLOW in _dot_color(widget)


def test_widget_connected_avatr_shows_green_and_badge(widget):
    widget.set_status(_avatr_status())
    assert "Connected" in widget.main_label.text()
    assert "Avatr 12" in widget.main_label.text()
    assert COLOR_GREEN in _dot_color(widget)
    assert widget.badge_label.isHidden() is False
    assert "AVATR" in widget.badge_label.text()


def test_widget_connected_phone_no_avatr_badge(widget):
    widget.set_status(_phone_status())
    assert "Connected" in widget.main_label.text()
    assert COLOR_GREEN in _dot_color(widget)
    assert widget.badge_label.isHidden() is True


def test_widget_test_device_shows_yellow_badge(widget):
    s = _phone_status()
    test_info = s.info._replace if hasattr(s.info, '_replace') else None  # not a NamedTuple
    # Build a fresh status with is_test_device=True (DeviceInfo is frozen).
    info = DeviceInfo(
        serial=s.info.serial, state="device",
        product_code="HWABR-QL", model_name="ABR-test",
        label="HWABR-QL", android_release="12", android_api=31,
        harmonyos_version="4.0", cpu_abi="arm64-v8a", locale="en-US",
        adbd_user="shell", hdbd_count=0, is_test_device=True,
        capabilities=s.info.capabilities,
    )
    sel = Device(serial=info.serial, state="device", product="HWABR-QL",
                  model=None, transport_id="1")
    status = DeviceStatus(devices=[sel], selected=sel, info=info,
                            adb_present=True, multiple=False)
    widget.set_status(status)
    assert widget.badge_label.isHidden() is False
    assert "TEST" in widget.badge_label.text()


def test_widget_multiple_devices_orange(widget):
    widget.set_status(_multiple_status())
    assert "Multiple" in widget.main_label.text()
    assert COLOR_ORANGE in _dot_color(widget)


def test_widget_no_adb_grey(widget):
    widget.set_status(_no_adb_status())
    assert "adb not found" in widget.main_label.text()
    assert COLOR_GREY in _dot_color(widget)


def test_widget_subline_has_serial_adbd_android(widget):
    widget.set_status(_avatr_status(serial="S99"))
    sub = widget.sub_label.text()
    assert "S99" in sub and "root" in sub and "Android 12" in sub


def test_widget_details_form_filled(widget):
    widget.set_status(_phone_status(serial="PHN1"))
    assert widget._detail_labels["serial"].text() == "PHN1"
    assert widget._detail_labels["model"].text() == "ABR-AL60"
    assert widget._detail_labels["product"].text() == "HWABR"
    assert widget._detail_labels["android"].text() == "12"
    assert widget._detail_labels["api"].text() == "32"
    assert widget._detail_labels["harmony"].text() == "4.2.0"
    assert widget._detail_labels["adbd"].text() == "shell"
    assert widget._detail_labels["hdbd"].text() == "absent"
    assert widget._detail_labels["abi"].text() == "arm64-v8a"
    assert widget._detail_labels["locale"].text() == "ru-RU"


def test_widget_details_toggle(widget):
    assert widget.details_frame.isHidden() is True
    widget.set_details_visible(True)
    assert widget.details_frame.isHidden() is False


def test_widget_emits_capabilities_on_set_status(qtbot, widget):
    received = []
    widget.capabilities_updated.connect(received.append)
    widget.set_status(_avatr_status())
    assert received == [_avatr_status().capabilities]


def test_widget_refresh_button_emits(qtbot, widget):
    with qtbot.waitSignal(widget.refresh_requested, timeout=1000):
        widget.refresh_button.click()


# ---- DevicePollerWorker ----

def test_poller_emits_status_on_first_tick(qtbot):
    fake_devs = [Device(serial="S1", state="device",
                          product="ICHU3200F2-ADV", model="Avatr 12",
                          transport_id="1")]
    fake_info = _avatr_status().info
    with patch.object(devices_module, "list_devices",
                      return_value=fake_devs), \
         patch.object(devices_module, "detect_full_info",
                      return_value=fake_info), \
         patch("ivi_installer.ui.device_status.adb.find_adb",
               return_value="/usr/bin/adb"):
        worker = DevicePollerWorker(interval_ms=1000)
        received: list = []
        worker.status.connect(received.append)
        worker._poll_once()
    assert len(received) == 1
    assert received[0].selected.serial == "S1"
    assert received[0].info.label == "Avatr 12"


def test_poller_emits_no_adb_when_missing(qtbot):
    with patch("ivi_installer.ui.device_status.adb.find_adb",
               return_value=None):
        worker = DevicePollerWorker(interval_ms=1000)
        received: list = []
        worker.status.connect(received.append)
        worker._poll_once()
    assert len(received) == 1
    s = received[0]
    assert s.adb_present is False
    assert s.devices == []


def test_poller_paused_skips_emit(qtbot):
    """When paused, _poll_once is a no-op — nothing emitted."""
    with patch.object(devices_module, "list_devices", return_value=[]), \
         patch("ivi_installer.ui.device_status.adb.find_adb",
               return_value="/usr/bin/adb"):
        worker = DevicePollerWorker(interval_ms=200)
        worker.set_paused(True)
        received: list = []
        worker.status.connect(received.append)
        worker._poll_once()
    assert received == []


def test_poller_preferred_serial_chooses_match(qtbot):
    a = Device(serial="A", state="device", product="HWABR",
                model=None, transport_id="1")
    b = Device(serial="B", state="device", product="ICHU3200F2-ADV",
                model="Avatr 12", transport_id="2")

    def fake_info(serial, fallback_product=None):
        return DeviceInfo(serial=serial, state="device",
                           product_code=fallback_product, label="x",
                           capabilities=DeviceCapabilities(
                               is_root=False, has_hdc=False,
                               is_harmony=False, is_avatr_ivi=False,
                               android_api=-1))

    with patch.object(devices_module, "list_devices", return_value=[a, b]), \
         patch.object(devices_module, "detect_full_info",
                      side_effect=fake_info), \
         patch("ivi_installer.ui.device_status.adb.find_adb",
               return_value="/usr/bin/adb"):
        worker = DevicePollerWorker(interval_ms=200)
        worker.set_preferred_serial("B")
        received: list = []
        worker.status.connect(received.append)
        worker._poll_once()
    assert len(received) == 1
    assert received[0].selected.serial == "B"
