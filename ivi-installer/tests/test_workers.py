"""Tests for the QThread worker classes.

We never spawn real adb here — the underlying functions are patched.
The QThread plumbing is exercised end-to-end: each test holds strong
refs to both the worker and the thread, and waits for the thread to
finish before letting the test exit.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ivi_installer import adb as adb_module
from ivi_installer import devices as devices_module
from ivi_installer import installer as installer_module
from ivi_installer.devices import Device
from ivi_installer.strategies import (
    AttemptResult,
    AttemptStatus,
    CascadedInstallResult,
)
from ivi_installer.ui import workers


def _wait_thread(thread, qtbot, timeout=2000):
    """Block until the worker QThread has fully exited."""
    qtbot.waitUntil(lambda: thread.isFinished(), timeout=timeout)


def test_device_probe_emits_result(qtbot):
    fake_devs = [
        Device(serial="S1", state="device", product="ICHU3200F2-ADV",
               model="Avatr 12", transport_id="1"),
    ]
    with patch.object(devices_module, "list_devices", return_value=fake_devs):
        worker = workers.DeviceProbeWorker()
        with qtbot.waitSignal(worker.result, timeout=2000) as blocker:
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert blocker.args == [fake_devs]


def test_device_probe_emits_empty_when_no_devices(qtbot):
    with patch.object(devices_module, "list_devices", return_value=[]):
        worker = workers.DeviceProbeWorker()
        with qtbot.waitSignal(worker.result, timeout=2000) as blocker:
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert blocker.args == [[]]


def test_device_probe_emits_error_on_filenotfound(qtbot):
    with patch.object(devices_module, "list_devices",
                      side_effect=FileNotFoundError("adb missing")):
        worker = workers.DeviceProbeWorker()
        with qtbot.waitSignal(worker.error, timeout=2000) as blocker:
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert "adb missing" in blocker.args[0]


def test_root_check_emits_true_for_root(qtbot):
    with patch.object(adb_module, "whoami", return_value="root"):
        worker = workers.RootCheckWorker(serial="S1")
        with qtbot.waitSignal(worker.result, timeout=2000) as blocker:
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert blocker.args == ["S1", True]


def test_root_check_emits_false_for_shell(qtbot):
    with patch.object(adb_module, "whoami", return_value="shell"):
        worker = workers.RootCheckWorker(serial="S1")
        with qtbot.waitSignal(worker.result, timeout=2000) as blocker:
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert blocker.args == ["S1", False]


def _fake_cascade(success: bool = True,
                   attempts: tuple[AttemptResult, ...] = ()) -> CascadedInstallResult:
    if not attempts:
        attempts = (
            AttemptResult(strategy="pm_install_streamed",
                          status=AttemptStatus.SUCCESS if success else AttemptStatus.FAILED,
                          summary="ok" if success else "nope",
                          log_lines=("line-1", "line-2")),
        )
    return CascadedInstallResult(success=success, package=None, attempts=attempts)


def test_install_worker_emits_result(qtbot, tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    fake = _fake_cascade(success=True)
    with patch.object(installer_module, "install_cascade", return_value=fake):
        worker = workers.InstallWorker(file_path=apk, serial="S1")
        with qtbot.waitSignal(worker.result, timeout=2000) as blocker:
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert blocker.args[0].success is True
    assert blocker.args[0].winning_strategy == "pm_install_streamed"


def test_install_worker_runs_specific_strategy(qtbot, tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    fake = _fake_cascade(success=True)
    with patch.object(installer_module, "install_with_strategy",
                      return_value=fake) as m:
        worker = workers.InstallWorker(file_path=apk, serial="S1",
                                        strategy="cmd_package_session")
        with qtbot.waitSignal(worker.result, timeout=2000):
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    # First positional arg is the strategy name.
    assert m.call_args.args[0] == "cmd_package_session"


def test_install_worker_emits_attempt_per_strategy(qtbot, tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    fake = _fake_cascade(attempts=(
        AttemptResult(strategy="pm_install_streamed",
                      status=AttemptStatus.FAILED, summary="meh"),
        AttemptResult(strategy="cmd_package_session",
                      status=AttemptStatus.SUCCESS, summary="ok"),
    ), success=True)
    seen: list[AttemptResult] = []
    with patch.object(installer_module, "install_cascade", return_value=fake):
        worker = workers.InstallWorker(file_path=apk, serial="S1")
        worker.attempt.connect(seen.append)
        with qtbot.waitSignal(worker.result, timeout=2000):
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert [a.strategy for a in seen] == [
        "pm_install_streamed", "cmd_package_session",
    ]


def test_install_worker_emits_error_on_exception(qtbot, tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    with patch.object(installer_module, "install_cascade",
                      side_effect=RuntimeError("boom")):
        worker = workers.InstallWorker(file_path=apk, serial="S1")
        with qtbot.waitSignal(worker.error, timeout=2000) as blocker:
            thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert "boom" in blocker.args[0]


def test_run_in_thread_quits_after_finished(qtbot):
    """Sanity: thread.quit fires once the worker emits `finished`."""
    with patch.object(devices_module, "list_devices", return_value=[]):
        worker = workers.DeviceProbeWorker()
        thread = workers.run_in_thread(worker)
        _wait_thread(thread, qtbot)
    assert thread.isFinished() is True
