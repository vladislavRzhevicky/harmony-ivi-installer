"""Tests for ivi_installer.installer — the public install entry points.

The legacy `install_single` / `install_xapk` / streaming-cascade tests
were dropped in v0.7.0 along with the strategies they covered. The
live install path is now exclusively the two strategies in
`strategies.py`; tests that exercise them live in test_strategies.py.

`.xapk` ingestion was dropped entirely in v0.21.0 — the input adapter
now only accepts a single `.apk`.

This module only checks the thin wrapper functions
(``install_cascade`` / ``install_with_strategy``) the UI calls into.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from ivi_installer import installer


def _make_apk(path: Path, content: bytes = b"PK\x03\x04dummy") -> Path:
    path.write_bytes(content)
    return path


# ---- install_cascade --------------------------------------------------------

def test_install_cascade_dispatches_to_run_cascade(tmp_path):
    apk = _make_apk(tmp_path / "x.apk")
    fake_result = MagicMock()
    with patch.object(installer, "run_cascade",
                       return_value=fake_result) as runner:
        out = installer.install_cascade(apk, serial="S1")
    assert out is fake_result
    runner.assert_called_once()
    # primary defaults to None (→ strategies.cascade_order picks the
    # default head, currently pm_disable_install).
    assert runner.call_args.kwargs.get("primary") is None


def test_install_cascade_threads_primary_through(tmp_path):
    apk = _make_apk(tmp_path / "x.apk")
    fake_result = MagicMock()
    with patch.object(installer, "run_cascade",
                       return_value=fake_result) as runner:
        installer.install_cascade(
            apk, serial="S1",
            primary_strategy="pm_disable_install",
        )
    assert runner.call_args.kwargs["primary"] == "pm_disable_install"


def test_install_cascade_threads_force_reinstall(tmp_path):
    apk = _make_apk(tmp_path / "x.apk")
    captured: dict[str, object] = {}

    def fake_build(file_path, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    with patch.object(installer, "build_context_from_path",
                       side_effect=fake_build):
        with patch.object(installer, "run_cascade", return_value=MagicMock()):
            installer.install_cascade(
                apk, serial="S1", force_reinstall=True,
            )
    assert captured["force_reinstall"] is True


# ---- install_with_strategy --------------------------------------------------

def test_install_with_strategy_calls_run_strategy(tmp_path):
    apk = _make_apk(tmp_path / "x.apk")
    fake_result = MagicMock()
    with patch.object(installer, "run_strategy",
                       return_value=fake_result) as runner:
        out = installer.install_with_strategy(
            "hdb_broker_install", apk, serial="S1",
        )
    assert out is fake_result
    assert runner.call_args.args[0] == "hdb_broker_install"


# ---- public re-exports ------------------------------------------------------

def test_module_re_exports_strategy_helpers():
    """The UI imports these names from `installer`; lock that in so a
    refactor doesn't silently break it."""
    for name in (
        "AttemptResult", "AttemptStatus", "CascadedInstallResult",
        "InstallContext", "StrategyDescriptor",
        "build_context_from_path", "list_strategies",
        "run_cascade", "run_strategy",
    ):
        assert hasattr(installer, name), name
