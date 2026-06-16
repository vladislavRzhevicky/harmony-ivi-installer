"""Tests for ivi_installer.adb.

We mock subprocess.run wholesale; nothing here actually invokes adb.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from ivi_installer import adb


# ---- helpers ----

def fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


@pytest.fixture
def fake_adb_path(tmp_path, monkeypatch):
    """Pretend an adb binary exists at a known path so `find_adb` succeeds."""
    fake = tmp_path / "adb"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(adb, "find_adb", lambda: str(fake))
    return str(fake)


# ---- run() basics ----

def test_run_invokes_adb_with_serial(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stdout="ok")) as m:
        result = adb.run("shell", "whoami", serial="DEVICE1")
    args = m.call_args.args[0]
    assert args[0] == fake_adb_path
    assert args[1:3] == ["-s", "DEVICE1"]
    assert args[3:] == ["shell", "whoami"]
    assert result.stdout == "ok"
    assert result.exit_code == 0


def test_run_without_serial_omits_dash_s(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stdout="ok")) as m:
        adb.run("devices", "-l")
    args = m.call_args.args[0]
    assert "-s" not in args


def test_run_raises_when_check_and_nonzero(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stderr="bad", returncode=2)):
        with pytest.raises(adb.AdbError) as exc:
            adb.run("install", "x.apk")
    assert exc.value.result.exit_code == 2
    assert "bad" in str(exc.value)


def test_run_with_check_false_does_not_raise(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stderr="bad", returncode=2)):
        result = adb.run("install", "x.apk", check=False)
    assert result.exit_code == 2


def test_run_propagates_timeout(fake_adb_path):
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=[], timeout=5)):
        with pytest.raises(TimeoutError, match="timed out after 5"):
            adb.run("install", "x.apk", timeout=5)


def test_run_no_binary_raises_filenotfound(monkeypatch):
    monkeypatch.setattr(adb, "find_adb", lambda: None)
    with pytest.raises(FileNotFoundError, match="adb not found"):
        adb.run("devices")


def test_adb_result_output_prefers_stdout():
    r = adb.AdbResult(args=(), exit_code=0, stdout="hi", stderr="ignored")
    assert r.output == "hi"


def test_adb_result_output_falls_back_to_stderr():
    r = adb.AdbResult(args=(), exit_code=0, stdout="   ", stderr="from stderr")
    assert r.output == "from stderr"


# ---- helpers wired around run() ----

def test_whoami_returns_stdout_stripped(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stdout="root\n")):
        assert adb.whoami("S1") == "root"


def test_is_root_true_for_root(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stdout="root\n")):
        assert adb.is_root("S1") is True


def test_is_root_false_for_shell(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stdout="shell\n")):
        assert adb.is_root("S1") is False


def test_push_passes_paths(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stdout="pushed\n")) as m:
        adb.push("/tmp/x.apk", "/sdcard/x.apk", serial="S1")
    args = m.call_args.args[0]
    assert args[3:6] == ["push", "/tmp/x.apk", "/sdcard/x.apk"]


def test_shell_returns_stdout_only(fake_adb_path):
    with patch("subprocess.run", return_value=fake_completed(stdout="hello\n", stderr="warn\n")):
        out = adb.shell("echo", "hello", serial="S1")
    assert out == "hello\n"


# ---- find_adb / ensure_adb ----


@pytest.fixture
def no_bundled_adb(monkeypatch):
    """Pretend the wheel-bundled adb.exe (Windows MSI) is absent.

    On Windows checkouts the file actually exists at
    `ivi_installer/resources/platform-tools/windows/adb.exe`, so without
    this fixture the bundled-path branch in `find_adb` short-circuits
    every other lookup.
    """
    monkeypatch.setattr(adb, "_bundled_adb", lambda: None)


def test_find_adb_prefers_bundled_over_managed(monkeypatch, tmp_path):
    """The wheel-bundled adb wins over the downloader's managed copy."""
    managed_root = tmp_path / "platform-tools"
    managed_root.mkdir()
    managed_adb = managed_root / "adb"
    managed_adb.write_text("")
    managed_adb.chmod(0o755)
    bundled = tmp_path / "bundled-adb"
    bundled.write_text("")
    monkeypatch.setattr(adb, "DEFAULT_ADB_DIR", managed_root)
    monkeypatch.setattr(adb, "ADB_BINARY", "adb")
    monkeypatch.setattr(adb, "_bundled_adb", lambda: str(bundled))
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/adb")
    assert adb.find_adb() == str(bundled)


def test_find_adb_prefers_managed_dir(no_bundled_adb, monkeypatch, tmp_path):
    managed_root = tmp_path / "platform-tools"
    managed_root.mkdir()
    fake = managed_root / adb._ADB_BINARY_NAME
    fake.write_text("")
    fake.chmod(0o755)
    monkeypatch.setattr(adb, "DEFAULT_ADB_DIR", managed_root)
    monkeypatch.setattr(adb, "ADB_BINARY", "adb")
    # Even if `which adb` would find something else, our managed path wins.
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/adb")
    assert adb.find_adb() == str(fake)


def test_find_adb_falls_back_to_path(no_bundled_adb, monkeypatch, tmp_path):
    # No managed adb on disk.
    monkeypatch.setattr(adb, "DEFAULT_ADB_DIR", tmp_path / "no-such")
    monkeypatch.setattr(adb, "ADB_BINARY", "adb")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/adb"
                        if name == "adb" else None)
    assert adb.find_adb() == "/usr/local/bin/adb"


def test_find_adb_returns_none_when_nothing(no_bundled_adb, monkeypatch, tmp_path):
    monkeypatch.setattr(adb, "DEFAULT_ADB_DIR", tmp_path / "no-such")
    monkeypatch.setattr(adb, "ADB_BINARY", "adb")
    monkeypatch.setattr("shutil.which", lambda _: None)
    # Point common-location fallbacks at non-existent paths so the test
    # stays deterministic on machines that actually have adb installed.
    monkeypatch.setattr(adb, "_COMMON_ADB_LOCATIONS",
                          (str(tmp_path / "no-such-adb"),))
    assert adb.find_adb() is None


def test_find_adb_falls_back_to_common_location(no_bundled_adb, monkeypatch, tmp_path):
    """When PATH is trimmed (e.g. .app launch), absolute fallbacks kick in."""
    monkeypatch.setattr(adb, "DEFAULT_ADB_DIR", tmp_path / "no-such")
    monkeypatch.setattr(adb, "ADB_BINARY", "adb")
    monkeypatch.setattr("shutil.which", lambda _: None)
    fake = tmp_path / "homebrew-adb"
    fake.write_text("")
    monkeypatch.setattr(adb, "_COMMON_ADB_LOCATIONS",
                          (str(tmp_path / "missing"), str(fake)))
    assert adb.find_adb() == str(fake)


def test_ensure_adb_returns_existing_path(monkeypatch, tmp_path):
    fake = tmp_path / "adb"
    fake.write_text("")
    fake.chmod(0o755)
    monkeypatch.setattr(adb, "find_adb", lambda: str(fake))
    assert adb.ensure_adb() == Path(str(fake))


def test_ensure_adb_downloads_and_extracts(monkeypatch, tmp_path):
    """Simulate a clean machine: no adb anywhere; ensure_adb must
    fetch the zip and extract it in DEFAULT_ADB_DIR."""
    import zipfile

    # 1. Pretend no adb is reachable.
    monkeypatch.setattr(adb, "find_adb", lambda: None)
    monkeypatch.setattr(adb, "DEFAULT_ADB_DIR", tmp_path / "platform-tools")

    # 2. Build a fake platform-tools.zip with a dummy adb file matching
    # the OS-appropriate binary name (`adb.exe` on Windows, `adb` else).
    src_zip = tmp_path / "src.zip"
    with zipfile.ZipFile(src_zip, "w") as z:
        z.writestr(f"platform-tools/{adb._ADB_BINARY_NAME}",
                   "#!/bin/sh\nexit 0\n")

    # 3. Stub out `_download` so no actual network I/O happens. The
    # signature matches `_download(url, dest, progress=None)`.
    def fake_download(url, dest, progress=None):
        Path(dest).write_bytes(src_zip.read_bytes())
        if progress:
            progress(50, "Downloading adb")

    monkeypatch.setattr(adb, "_download", fake_download)

    progress_calls: list[tuple[int, str]] = []
    out = adb.ensure_adb(progress=lambda pct, msg: progress_calls.append((pct, msg)))

    assert out.exists()
    assert out.name == adb._ADB_BINARY_NAME
    # progress hook was used at least for the start and end markers
    assert progress_calls[0][0] == 0
    assert progress_calls[-1][0] == 100
