"""Tests for ivi_installer.strategies.

The strategies talk to adb via `adb.run`, which we patch wholesale.
None of these tests touch a real device or filesystem beyond `tmp_path`.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ivi_installer import adb
from ivi_installer import strategies as st
from ivi_installer.strategies import (
    AttemptStatus,
    InstallContext,
    build_context_from_path,
    list_strategies,
    run_cascade,
    run_strategy,
)


# ---- helpers ---------------------------------------------------------------

def _ok(stdout="Success\n", stderr=""):
    return adb.AdbResult(args=(), exit_code=0, stdout=stdout, stderr=stderr)


def _fail(stdout="", stderr="", code=1):
    return adb.AdbResult(args=(), exit_code=code, stdout=stdout, stderr=stderr)


def _ctx(tmp_path: Path, *, apk_name="x.apk", package=None):
    apk = tmp_path / apk_name
    apk.write_bytes(b"PK\x03\x04dummy")
    lines: list[str] = []
    return InstallContext(
        serial="S1",
        apk_paths=[apk],
        package=package,
        log=lines.append,
    ), lines


# ---- _sanitize_remote_name -------------------------------------------------

def test_sanitize_remote_name_basic():
    assert st._sanitize_remote_name("Celia Keyboard 1.3.12.apk") == "Celia_Keyboard_1.3.12.apk"
    assert st._sanitize_remote_name("plain.apk") == "plain.apk"
    assert st._sanitize_remote_name("with $weird#$ chars!") == "with_weird_chars"
    # Empty / pure-junk / no-alnum inputs fall back to "payload".
    assert st._sanitize_remote_name("") == "payload"
    assert st._sanitize_remote_name("...") == "payload"
    assert st._sanitize_remote_name("___") == "payload"
    # Cyrillic chars → underscore; leading dot prefixed so file isn't hidden.
    assert st._sanitize_remote_name("Привет.apk") == "payload.apk"


# ---- app_process_helper ----------------------------------------------------

def test_app_process_helper_redeploys_broker(tmp_path, monkeypatch):
    """The strategy now (re)deploys the broker on demand instead of being
    a stub. We mock _broker_deploy so the test stays adb-free."""
    ctx, _ = _ctx(tmp_path)
    monkeypatch.setattr(st, "_broker_deploy", lambda _ctx, _a: True)
    with patch.object(adb, "run", return_value=_ok()):
        attempt = st._strategy_app_process_helper(ctx)
    assert attempt.status is AttemptStatus.SUCCESS
    assert "up" in attempt.summary.lower()


# ---- cascade ---------------------------------------------------------------

def test_hdb_broker_install_refuses_multi_apk(tmp_path):
    """hdb_broker_install can't handle multi-APK inputs (the bridge
    multi-command hardcodes the wrong installer pkg). It must return
    TERMINAL so the UI surfaces the 'use a single .apk' guidance."""
    apk1 = tmp_path / "base.apk"; apk1.write_bytes(b"PK")
    apk2 = tmp_path / "split.apk"; apk2.write_bytes(b"PK")
    ctx = st.InstallContext(serial="x", apk_paths=[apk1, apk2])
    attempt = st._strategy_hdb_broker_install(ctx)
    assert attempt.status is AttemptStatus.TERMINAL
    assert "single" in attempt.summary.lower() or ".apk" in attempt.summary


# ---- runtime permission grant helper --------------------------------------

_DUMPSYS_FIXTURE = """\
Packages:
  Package [com.example.foo] (1234567):
    requested permissions:
      android.permission.INTERNET
      android.permission.ACCESS_FINE_LOCATION
      android.permission.RECORD_AUDIO
    install permissions:
      android.permission.INTERNET: granted=true
    User 0: ceDataInode=0 installed=false hidden=false suspended=false
      runtime permissions:
        android.permission.ACCESS_FINE_LOCATION: granted=false
        android.permission.RECORD_AUDIO: granted=false
    User 13: ceDataInode=12345 installed=true hidden=false suspended=false
      gids=[3003]
      runtime permissions:
        android.permission.ACCESS_FINE_LOCATION: granted=false
        android.permission.RECORD_AUDIO: granted=true
        android.permission.POST_NOTIFICATIONS: granted=false
"""


def test_ungranted_runtime_perms_filters_per_user():
    """The parser must isolate one user's runtime block and only
    return perms with granted=false."""
    perms_user13 = st._ungranted_runtime_perms_for_user(_DUMPSYS_FIXTURE, 13)
    # FINE_LOCATION and POST_NOTIFICATIONS are ungranted for user 13;
    # RECORD_AUDIO is already granted.
    assert perms_user13 == [
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.POST_NOTIFICATIONS",
    ]
    perms_user0 = st._ungranted_runtime_perms_for_user(_DUMPSYS_FIXTURE, 0)
    assert perms_user0 == [
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.RECORD_AUDIO",
    ]
    # Non-existent user → empty list.
    assert st._ungranted_runtime_perms_for_user(_DUMPSYS_FIXTURE, 7) == []


def test_perm_helper_jar_is_packaged():
    """The helper jar must be present as a package resource — without it
    the post-install grant step silently no-ops."""
    from importlib import resources as _resources
    files = _resources.files("ivi_installer.resources")
    jar = files.joinpath(st.HDB_PERM_HELPER_RESOURCE)
    with jar.open("rb") as fh:
        data = fh.read()
    # Sanity: a valid JAR is a ZIP, so it starts with PK\x03\x04.
    assert data[:4] == b"PK\x03\x04"
    # And it should contain a classes.dex entry (DEX magic dex\n035).
    import io, zipfile as _zf
    with _zf.ZipFile(io.BytesIO(data)) as zf:
        assert "classes.dex" in zf.namelist()


def test_cascade_runs_specific_strategy(tmp_path):
    """Pick a strategy by name and run only it."""
    ctx, _ = _ctx(tmp_path)
    # app_process_helper is a stub — easy to drive without device mocks.
    result = run_strategy("app_process_helper", ctx)
    assert len(result.attempts) == 1
    assert result.attempts[0].strategy == "app_process_helper"


def test_run_strategy_invokes_body_even_when_cascade_skips(tmp_path,
                                                            monkeypatch):
    """Diagnostic / utility strategies have applies=False (cascade skips
    them) but direct invocation must still call the body."""
    ctx, _ = _ctx(tmp_path)
    # `diagnose` has applies=False; mock adb.run so the body runs without
    # touching a real device. `_strategy_diagnose` would otherwise try to
    # spawn dozens of probes — we just want to assert the dispatcher
    # didn't gate on `applies()`.
    sentinel_called = {"v": False}
    def _fake_diag(ctx):
        sentinel_called["v"] = True
        return st.AttemptResult(
            strategy="diagnose", status=AttemptStatus.SUCCESS,
            summary="ok",
        )
    monkeypatch.setattr(st, "_strategy_diagnose", _fake_diag)
    # Re-register so the dispatcher picks up the patched body.
    desc = st._STRATEGY_INDEX["diagnose"]
    monkeypatch.setitem(
        st._STRATEGY_INDEX, "diagnose",
        st.StrategyDescriptor(name=desc.name, label=desc.label,
                              description=desc.description,
                              run=_fake_diag, applies=desc.applies),
    )
    result = run_strategy("diagnose", ctx)
    assert sentinel_called["v"] is True
    assert result.attempts[0].strategy == "diagnose"


def test_unknown_strategy_raises(tmp_path):
    ctx, _ = _ctx(tmp_path)
    with pytest.raises(KeyError):
        run_strategy("does-not-exist", ctx)


def test_list_strategies_has_expected_names():
    names = [s.name for s in list_strategies()]
    assert names == [
        "pm_disable_install",
        "hdb_broker_install",
        "diagnose",
        "app_process_helper",
    ]


def test_cascade_order_default_puts_pm_disable_first():
    """Default primary is pm-disable — the lighter competitor-derived path."""
    from ivi_installer import strategies as st
    names = [d.name for d in st.cascade_order()]
    assert names[:2] == ["pm_disable_install", "hdb_broker_install"]


def test_cascade_order_swap_puts_hdb_first():
    from ivi_installer import strategies as st
    names = [d.name for d in st.cascade_order(primary="hdb_broker_install")]
    assert names[:2] == ["hdb_broker_install", "pm_disable_install"]


def test_cascade_order_rejects_unknown_primary():
    import pytest
    from ivi_installer import strategies as st
    with pytest.raises(ValueError):
        st.cascade_order(primary="nope")


# ---- build_context_from_path ----------------------------------------------

def test_build_context_for_apk(tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    ctx = build_context_from_path(apk, serial="S1")
    assert ctx.apk_paths == [apk]
    assert ctx.package is None


def test_build_context_rejects_xapk(tmp_path):
    """`.xapk` was dropped in 0.21.0 — multi-APK installs can't land on
    Deepal/Avatr because the bridge command hardcodes the wrong installer
    pkg. The UI filters at the picker, but the input adapter must reject
    too as a defensive backstop."""
    f = tmp_path / "a.xapk"
    f.write_bytes(b"PK")
    with pytest.raises(ValueError):
        build_context_from_path(f, serial="S1")


def test_build_context_unknown_extension(tmp_path):
    f = tmp_path / "foo.zip"
    f.write_bytes(b"")
    with pytest.raises(ValueError):
        build_context_from_path(f, serial="S1")


# ---- broker resilience -----------------------------------------------------

def test_broker_jar_is_packaged():
    """The avatr-hdb-broker.jar must ship with the wheel/.app — without
    it, an auto-deploy on a freshly-rebooted car silently no-ops."""
    from importlib import resources as _resources
    files = _resources.files("ivi_installer.resources")
    jar = files.joinpath(st.HDB_BROKER_RESOURCE)
    with jar.open("rb") as fh:
        data = fh.read()
    assert data[:4] == b"PK\x03\x04"
    import io, zipfile as _zf
    with _zf.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        # Broker is dexed via d8 like the perm-grant helper.
        assert "classes.dex" in names


def test_broker_launch_command_shape():
    """The launch command is the most fragile part of the deploy: must be
    `nohup sh -c "..." &` with the right CLASSPATH/main-class/port and
    log redirected to the same path the workshop-session broker uses."""
    cmd = st._broker_launch_command()
    assert cmd.startswith("nohup sh -c ")
    assert cmd.endswith("&")
    assert f"CLASSPATH={st.HDB_BROKER_REMOTE}" in cmd
    assert f"app_process /system/bin {st.HDB_BROKER_MAIN_CLASS}" in cmd
    assert f" {st.HDB_BROKER_PORT}" in cmd
    assert f">{st.HDB_BROKER_LOG} 2>&1" in cmd
    # The CLASSPATH+app_process bits must be inside the sh -c quotes,
    # otherwise the daemon won't detach when adb shell closes.
    assert 'sh -c "CLASSPATH=' in cmd


def test_broker_deploy_skips_push_when_md5_matches(monkeypatch, tmp_path):
    """If the on-device jar's md5 already matches the bundled jar, we
    must NOT push — the broker is then just (re)started in place."""
    ctx, _ = _ctx(tmp_path)
    # Stub the bundled jar so we know the expected md5.
    fake_jar = b"\x50\x4b\x03\x04fakejar"
    monkeypatch.setattr(st, "_load_broker_jar", lambda: fake_jar)
    bundled_md5 = st._md5_hex(fake_jar)
    monkeypatch.setattr(st, "_broker_ping", lambda timeout=2.0: True)
    monkeypatch.setattr(st.time, "sleep", lambda _s: None)

    pushed = []
    def fake_run(*args, serial=None, check=False, timeout=60):
        if args and args[0] == "shell" and args[1] == "md5sum":
            return adb.AdbResult(
                args=args, exit_code=0,
                stdout=f"{bundled_md5}  {st.HDB_BROKER_REMOTE}\n",
                stderr="",
            )
        if args and args[0] == "push":
            pushed.append(args)
            return _ok()
        return _ok()

    with patch.object(adb, "run", side_effect=fake_run):
        ok = st._broker_deploy(ctx, accum=[])
    assert ok is True
    assert pushed == [], "must not push when md5 already matches"


def test_broker_deploy_pushes_when_md5_differs(monkeypatch, tmp_path):
    """When md5 differs (or jar is absent), we push the bundled jar to
    /data/local/tmp/ before starting the daemon."""
    ctx, _ = _ctx(tmp_path)
    fake_jar = b"\x50\x4b\x03\x04fakejar-v2"
    monkeypatch.setattr(st, "_load_broker_jar", lambda: fake_jar)
    monkeypatch.setattr(st, "_broker_ping", lambda timeout=2.0: True)
    monkeypatch.setattr(st.time, "sleep", lambda _s: None)

    push_calls = []
    def fake_run(*args, serial=None, check=False, timeout=60):
        if args and args[0] == "shell" and args[1] == "md5sum":
            # No jar on device.
            return adb.AdbResult(args=args, exit_code=1,
                                  stdout="", stderr="No such file\n")
        if args and args[0] == "push":
            push_calls.append(args)
            return _ok()
        return _ok()

    with patch.object(adb, "run", side_effect=fake_run):
        ok = st._broker_deploy(ctx, accum=[])
    assert ok is True
    assert len(push_calls) == 1
    # The push target must be the canonical /data/local/tmp/ location.
    assert push_calls[0][2] == st.HDB_BROKER_REMOTE


def test_broker_deploy_fails_when_ping_never_recovers(monkeypatch, tmp_path):
    """If the daemon won't bind tcp:38787 even after retries, deploy
    must return False — _broker_health_check then surfaces the
    'broker won't start' message in the install log."""
    ctx, _ = _ctx(tmp_path)
    monkeypatch.setattr(st, "_load_broker_jar", lambda: b"\x50\x4b\x03\x04x")
    monkeypatch.setattr(st, "_broker_ping", lambda timeout=2.0: False)
    monkeypatch.setattr(st.time, "sleep", lambda _s: None)
    with patch.object(adb, "run", return_value=_ok()):
        ok = st._broker_deploy(ctx, accum=[])
    assert ok is False


def test_broker_health_check_auto_deploys_on_ping_fail(monkeypatch, tmp_path):
    """On PING fail, the health check must call _broker_deploy
    transparently — that's what makes a freshly-rebooted car JustWork™
    for the next install."""
    ctx, _ = _ctx(tmp_path)
    pings = iter([False, True])  # initial PING fails, then succeeds post-deploy
    monkeypatch.setattr(st, "_broker_ping",
                        lambda timeout=2.0: next(pings, True))
    deploy_called = {"v": False}
    def fake_deploy(_ctx, _a):
        deploy_called["v"] = True
        return True
    monkeypatch.setattr(st, "_broker_deploy", fake_deploy)
    with patch.object(adb, "run", return_value=_ok()):
        ok = st._broker_health_check(ctx, accum=[])
    assert ok is True
    assert deploy_called["v"] is True


def test_broker_pid_parses_ps_output(monkeypatch, tmp_path):
    """PID extraction must pick the AvatrHdbBroker line out of ps -A."""
    ctx, _ = _ctx(tmp_path)
    ps_output = (
        "  PID ARGS\n"
        "    1 init\n"
        " 1234 /system/bin/somethingelse\n"
        "24354 app_process64 /system/bin AvatrHdbBroker 38787\n"
        "31000 grep AvatrHdbBroker\n"   # also matches; lowest PID wins
    )
    with patch.object(adb, "run", return_value=_ok(stdout=ps_output)):
        pid = st._broker_pid(ctx, accum=[])
    assert pid == 24354


def test_collect_broker_status_assembles_snapshot(monkeypatch):
    """Smoke-test the BrokerStatus snapshot the UI consumes — verifies
    field plumbing, not adb behavior."""
    fake_jar = b"\x50\x4b\x03\x04fakejar"
    monkeypatch.setattr(st, "_load_broker_jar", lambda: fake_jar)
    md5 = st._md5_hex(fake_jar)
    monkeypatch.setattr(st, "_broker_ping", lambda timeout=2.0: True)

    def fake_run(*args, serial=None, check=False, timeout=60):
        if args and args[0] == "forward":
            return _ok()
        if args[:2] == ("shell", "md5sum"):
            return _ok(stdout=f"{md5}  {st.HDB_BROKER_REMOTE}\n")
        if args[:3] == ("shell", "ps", "-A"):
            return _ok(stdout=(
                "  PID ARGS\n"
                "24354 app_process64 /system/bin AvatrHdbBroker 38787\n"
            ))
        if args[:3] == ("shell", "ps", "-p"):
            return _ok(stdout="700123\n")
        return _ok()

    with patch.object(adb, "run", side_effect=fake_run):
        status = st.collect_broker_status("S1")
    assert status.alive is True
    assert status.forwarded is True
    assert status.pid == 24354
    assert status.uptime_s == 700123
    assert status.port == st.HDB_BROKER_PORT
    assert status.remote_md5 == md5
    assert status.bundled_md5 == md5
    assert status.jar_matches is True
    assert status.jar_present is True


# ---- pm-disable strategy ---------------------------------------------------

_DEEPAL_PM_LIST_USERS = (
    "Users:\n"
    "\tUserInfo{0:Owner:13} running\n"
    "\tUserInfo{10:Driver:0} running\n"
    "\tUserInfo{11:Passenger:0} running\n"
    "\tUserInfo{12:Rear:0} running\n"
    "\tUserInfo{13:Driver-Active:0} running\n"
)


def test_pm_disable_strategy_disables_installs_and_reenables(tmp_path):
    """Competitor-aligned recipe (v0.12.0) + per-screen honor (v0.23.0):
    disable PackageInstaller on the active driver user, pm install with
    `--user N` when a single screen was picked, then re-enable
    symmetrically on the same user. We assert the command shape and
    ordering match the competitor's `install_apk` plus the new --user
    contract."""
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    ctx = st.InstallContext(
        serial="S1", apk_paths=[apk], target_user=13,
        preferred_installer="com.huawei.appinstaller.car",
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(*args, serial=None, check=False, timeout=60):
        calls.append(args)
        if args[:1] == ("push",):
            return _ok()
        if args[:4] == ("shell", "pm", "list", "users"):
            return _ok(stdout=_DEEPAL_PM_LIST_USERS)
        if args[:3] == ("shell", "pm", "disable-user"):
            return _ok(stdout="Package com.android.packageinstaller new state: disabled-user")
        if args[:3] == ("shell", "pm", "install"):
            return _ok(stdout="Success\n")
        if args[:3] == ("shell", "pm", "enable"):
            return _ok(stdout="Package com.android.packageinstaller new state: enabled")
        if args[:3] == ("shell", "pm", "install-existing"):
            return _ok(stdout="Package x.apk installed for user: 13\n")
        if args[:3] == ("shell", "rm", "-f"):
            return _ok()
        if args[:2] == ("shell", "dumpsys"):
            return _ok(stdout="")  # no perms to grant
        return _ok()

    with patch.object(adb, "run", side_effect=fake_run):
        attempt = st._strategy_pm_disable_install(ctx)
    assert attempt.status is AttemptStatus.SUCCESS, attempt.summary
    cmds = [" ".join(c) for c in calls]
    # Disable, install, re-enable — in that order.
    disable_idx = next(i for i, c in enumerate(cmds)
                       if "pm disable-user" in c)
    install_idx = next(i for i, c in enumerate(cmds)
                       if c.startswith("shell pm install"))
    enable_idx = next(i for i, c in enumerate(cmds)
                      if "pm enable" in c
                      and "com.android.packageinstaller" in c)
    assert disable_idx < install_idx < enable_idx
    # Disable lands on the active driver (user 13), not user 0.
    assert "--user 13" in cmds[disable_idx]
    # Install carries the Huawei installer pkg, --user 13 (per-screen
    # honor), no `-t`.
    install_cmd = cmds[install_idx]
    assert "-i com.huawei.appinstaller.car" in install_cmd
    assert "--user 13" in install_cmd
    assert " -t " not in f" {install_cmd} "
    # Re-enable is symmetric: same user that was disabled.
    assert "--user 13" in cmds[enable_idx]


def test_pm_disable_strategy_omits_user_for_multi_screen_fanout(tmp_path):
    """Multi-screen selection routes through `_resolve_targets` as
    seed_user=0 + non-empty fan_out. pm-disable strategy has no
    per-user fan-out stage, so it relies on USER_ALL semantics — i.e.
    pm install WITHOUT `--user` to cover every selected screen at once.
    Verify --user is omitted in that case."""
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    ctx = st.InstallContext(
        serial="S1", apk_paths=[apk],
        target_users=(11, 13),  # multi-screen → seed=0, fan_out=[11,13]
        preferred_installer="com.huawei.appinstaller.car",
    )
    calls: list[tuple[str, ...]] = []

    def fake_run(*args, serial=None, check=False, timeout=60):
        calls.append(args)
        if args[:1] == ("push",):
            return _ok()
        if args[:4] == ("shell", "pm", "list", "users"):
            return _ok(stdout=_DEEPAL_PM_LIST_USERS)
        if args[:3] == ("shell", "pm", "disable-user"):
            return _ok(stdout="disabled-user")
        if args[:3] == ("shell", "pm", "install"):
            return _ok(stdout="Success\n")
        if args[:3] == ("shell", "pm", "enable"):
            return _ok(stdout="enabled")
        return _ok()

    with patch.object(adb, "run", side_effect=fake_run):
        attempt = st._strategy_pm_disable_install(ctx)
    assert attempt.status is AttemptStatus.SUCCESS, attempt.summary
    install_cmd = next(" ".join(c) for c in calls
                       if c[:3] == ("shell", "pm", "install"))
    assert "--user" not in install_cmd


def test_pm_disable_strategy_reenables_after_install_failure(tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    ctx = st.InstallContext(
        serial="S1", apk_paths=[apk], target_user=13,
        preferred_installer="com.huawei.appinstaller.car",
    )
    enabled_after_failure = {"flag": False}

    def fake_run(*args, serial=None, check=False, timeout=60):
        if args[:1] == ("push",):
            return _ok()
        if args[:3] == ("shell", "pm", "disable-user"):
            return _ok(stdout="disabled-user")
        if args[:3] == ("shell", "pm", "install"):
            return _ok(stdout="Failure [INSTALL_FAILED_INVALID_APK]\n")
        if args[:3] == ("shell", "pm", "enable"):
            enabled_after_failure["flag"] = True
            return _ok()
        return _ok()

    with patch.object(adb, "run", side_effect=fake_run):
        attempt = st._strategy_pm_disable_install(ctx)
    # INSTALL_FAILED_INVALID_APK is in TERMINAL_CODES.
    assert attempt.status is AttemptStatus.TERMINAL
    # Even though the install failed, PackageInstaller MUST be re-enabled.
    assert enabled_after_failure["flag"] is True


def test_pm_disable_strategy_skips_for_multi_apk(tmp_path):
    apk1 = tmp_path / "a.apk"; apk1.write_bytes(b"PK")
    apk2 = tmp_path / "b.apk"; apk2.write_bytes(b"PK")
    ctx = st.InstallContext(serial="S1", apk_paths=[apk1, apk2])
    attempt = st._strategy_pm_disable_install(ctx)
    assert attempt.status is AttemptStatus.SKIPPED


def test_pm_disable_strategy_tries_alt_installer_on_first_failure(tmp_path):
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    ctx = st.InstallContext(
        serial="S1", apk_paths=[apk], target_user=13,
        preferred_installer="com.huawei.appinstaller.car",
    )
    install_calls: list[str] = []

    def fake_run(*args, serial=None, check=False, timeout=60):
        if args[:1] == ("push",):
            return _ok()
        if args[:3] == ("shell", "pm", "disable-user"):
            return _ok(stdout="disabled-user")
        if args[:3] == ("shell", "pm", "install"):
            install_calls.append(" ".join(args))
            # First call (with appinstaller.car) fails with a non-terminal
            # code; second call (with appmarket.vehicle) succeeds.
            if "com.huawei.appinstaller.car" in args:
                return _ok(stdout="Failure [INSTALL_FAILED_INTERNAL_ERROR]\n")
            return _ok(stdout="Success\n")
        if args[:3] == ("shell", "pm", "enable"):
            return _ok()
        if args[:3] == ("shell", "pm", "install-existing"):
            return _ok(stdout="Package installed for user: 13\n")
        if args[:2] == ("shell", "dumpsys"):
            return _ok(stdout="")
        return _ok()

    with patch.object(adb, "run", side_effect=fake_run):
        attempt = st._strategy_pm_disable_install(ctx)
    assert attempt.status is AttemptStatus.SUCCESS
    assert len(install_calls) == 2
    assert "com.huawei.appinstaller.car" in install_calls[0]
    assert "com.huawei.appmarket.vehicle" in install_calls[1]


# ---- IME enrollment via shell `ime` ---------------------------------------

def test_enable_input_method_calls_ime_enable_for_each_user():
    calls: list[tuple] = []

    def fake_run(*args, serial=None, check=False, timeout=60):
        calls.append(args)
        return _ok(stdout="Input method com.foo/.Bar: now enabled")

    with patch.object(adb, "run", side_effect=fake_run):
        result = st.enable_input_method(
            "S1", ime_id="com.foo/.Bar", users=[0, 10, 13],
        )
    assert result == {0: True, 10: True, 13: True}
    assert len(calls) == 3
    for c in calls:
        assert c[1] == "ime"
        assert c[2] == "enable"
    assert any(c[4] == "0" for c in calls)
    assert any(c[4] == "13" for c in calls)


def test_enable_input_method_reports_failures():
    def fake_run(*args, serial=None, check=False, timeout=60):
        # User 11: nonzero exit; user 0: success.
        u = args[args.index("--user") + 1]
        if u == "11":
            return adb.AdbResult(args=args, exit_code=1,
                                  stdout="", stderr="error")
        return _ok(stdout="now enabled")

    with patch.object(adb, "run", side_effect=fake_run):
        result = st.enable_input_method(
            "S1", ime_id="com.foo/.Bar", users=[0, 11],
        )
    assert result == {0: True, 11: False}


def test_set_default_input_method_calls_ime_set():
    seen: list[str] = []

    def fake_run(*args, serial=None, check=False, timeout=60):
        seen.append(args[2])  # 'set'
        return _ok(stdout="Input method com.foo/.Bar: now selected")

    with patch.object(adb, "run", side_effect=fake_run):
        st.set_default_input_method(
            "S1", ime_id="com.foo/.Bar", users=[0])
    assert seen == ["set"]


def test_list_input_methods_marks_enabled_state():
    calls: list[tuple] = []

    def fake_run(*args, serial=None, check=False, timeout=60):
        calls.append(args)
        # First call: -s --user 0 → enabled list
        # Second call: -s -a --user 0 → all
        if "-a" in args:
            return _ok(stdout=(
                "com.android.inputmethod.latin/.LatinIME\n"
                "com.huawei.ohos.inputmethod/com.android.inputmethod.latin.LatinIME\n"
            ))
        return _ok(stdout=(
            "com.android.inputmethod.latin/.LatinIME\n"
        ))

    with patch.object(adb, "run", side_effect=fake_run):
        out = st.list_input_methods("S1", user=0)
    assert ("com.android.inputmethod.latin/.LatinIME", True) in out
    assert (
        "com.huawei.ohos.inputmethod/com.android.inputmethod.latin.LatinIME",
        False,
    ) in out


def test_discover_ime_id_picks_match_for_package():
    listing = (
        "com.android.inputmethod.latin/.LatinIME\n"
        "com.huawei.ohos.inputmethod/com.android.inputmethod.latin.LatinIME\n"
    )
    with patch.object(adb, "run", return_value=_ok(stdout=listing)):
        ime = st.discover_ime_id("S1", "com.huawei.ohos.inputmethod")
    assert ime == (
        "com.huawei.ohos.inputmethod/com.android.inputmethod.latin.LatinIME"
    )


def test_discover_ime_id_returns_none_for_missing_package():
    with patch.object(adb, "run", return_value=_ok(stdout="other/.X\n")):
        assert st.discover_ime_id("S1", "com.missing") is None


# ---- _live_screen_users / _resolve_targets dynamic fallback ----------------

def test_live_screen_users_picks_running_non_system_ids():
    """The probe keeps every running user with id >= 10 — 0 (system) and
    non-running users are filtered out. This mirrors what the install
    fan-out actually wants to land on."""
    pm_list = (
        "Users:\n"
        "\tUserInfo{0:Owner:13} running\n"
        "\tUserInfo{10:Driver:0} running\n"
        "\tUserInfo{11:Passenger:0} running\n"
        "\tUserInfo{12:Rear:0}\n"        # not running
        "\tUserInfo{14:Extra:0} running\n"  # above 13 — must be kept
    )
    with patch.object(adb, "run", return_value=_ok(stdout=pm_list)):
        ids = st._live_screen_users("S1")
    assert ids == [10, 11, 14]


def test_live_screen_users_falls_back_when_probe_empty():
    """If `pm list users` errored or returned nothing usable we still
    need *some* fan-out targets — fall back to the canonical Deepal set
    so the cascade has something to try."""
    with patch.object(adb, "run", return_value=_fail(stderr="oops")):
        ids = st._live_screen_users("S1")
    assert ids == list(st.HDB_SCREEN_USERS)


def test_resolve_targets_default_uses_live_users(tmp_path):
    """With no explicit ``target_users`` / ``target_user`` set, the
    default branch must seed via 0 and fan out to the device's actual
    multimedia users — not the hardcoded (10,11,12,13) tuple. Cars with
    main user 12 (no 13) used to silently miss the driver screen."""
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    ctx = st.InstallContext(serial="S1", apk_paths=[apk])
    pm_list = (
        "Users:\n"
        "\tUserInfo{0:Owner:13} running\n"
        "\tUserInfo{10:Driver:0} running\n"
        "\tUserInfo{11:Passenger:0} running\n"
        "\tUserInfo{12:Active-Driver:0} running\n"  # main = 12, no 13
    )
    with patch.object(adb, "run", return_value=_ok(stdout=pm_list)):
        seed, fan_out = st._resolve_targets(ctx)
    assert seed == 0
    assert fan_out == [10, 11, 12]
    # Crucially: not the hardcoded tuple — no phantom user 13.
    assert 13 not in fan_out


def test_resolve_targets_explicit_users_skips_probe(tmp_path):
    """When the UI passed explicit target_users, no device probe should
    be needed — the explicit list wins verbatim."""
    apk = tmp_path / "x.apk"
    apk.write_bytes(b"PK")
    ctx = st.InstallContext(serial="S1", apk_paths=[apk],
                             target_users=(11, 12))
    # adb.run would assert if called — make sure it isn't.
    with patch.object(adb, "run") as run_mock:
        seed, fan_out = st._resolve_targets(ctx)
    assert run_mock.call_count == 0
    assert seed == 0
    assert fan_out == [11, 12]
