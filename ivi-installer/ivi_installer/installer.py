"""Public install entry points used by the UI and tests.

Two strategies live in `strategies.py` for the live install path:

  * ``pm_disable_install`` — adopted from the AvatrAppInstaller
    competitor; lighter (no on-device broker daemon needed). Set as
    the default primary.
  * ``hdb_broker_install`` — our verified path on HarmonySpace 5.0
    (Deepal S09 / HwSAPT). Acts as the fallback if the primary fails.

Earlier versions of this module shipped a 10-strategy cascade
(streamed pm install, intent dispatch, root install, verifier
disable, …). All of that was dead code on the only firmware we
target — it has been removed. If you need any of it back the git
history holds the v0.6.x implementations.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from . import strategies as _strategies
from .strategies import (
    AttemptResult,
    AttemptStatus,
    CascadedInstallResult,
    InstallContext,
    StageEvent,
    StrategyDescriptor,
    build_context_from_path,
    get_strategy,
    list_strategies,
    run_cascade,
    run_strategy,
)

log = logging.getLogger(__name__)


def install_cascade(
    file_path: Path | str,
    *,
    serial: str,
    grant_runtime: bool = True,
    target_user: int | None = None,
    target_users: tuple[int, ...] | None = None,
    preferred_installer: str | None = None,
    primary_strategy: str | None = None,
    force_reinstall: bool = False,
    log_callback: Callable[[str], None] = lambda _line: None,
    stage_callback: Callable[[StageEvent], None] = lambda _ev: None,
) -> CascadedInstallResult:
    """Run the primary strategy, then the other one as fallback if needed.

    `primary_strategy` selects which one runs first
    (``pm_disable_install`` by default). `log_callback` receives one
    line at a time so the UI can stream progress; `stage_callback`
    receives :class:`StageEvent` messages so the UI can drive the
    visual install pipeline. ``target_users`` selects an explicit set
    of multimedia-screen user ids; ``target_user`` is the legacy
    single-user knob and only consulted when ``target_users`` is None.
    """
    ctx = build_context_from_path(
        file_path,
        serial=serial,
        grant_runtime=grant_runtime,
        target_user=target_user,
        preferred_installer=preferred_installer,
        force_reinstall=force_reinstall,
        log_callback=log_callback,
    )
    ctx.target_users = target_users
    ctx.stage_callback = stage_callback
    return run_cascade(ctx, primary=primary_strategy)


def install_with_strategy(
    strategy: str,
    file_path: Path | str,
    *,
    serial: str,
    grant_runtime: bool = True,
    target_user: int | None = None,
    target_users: tuple[int, ...] | None = None,
    preferred_installer: str | None = None,
    force_reinstall: bool = False,
    log_callback: Callable[[str], None] = lambda _line: None,
    stage_callback: Callable[[StageEvent], None] = lambda _ev: None,
) -> CascadedInstallResult:
    """Run exactly one strategy by name. No fallback."""
    ctx = build_context_from_path(
        file_path,
        serial=serial,
        grant_runtime=grant_runtime,
        target_user=target_user,
        preferred_installer=preferred_installer,
        force_reinstall=force_reinstall,
        log_callback=log_callback,
    )
    ctx.target_users = target_users
    ctx.stage_callback = stage_callback
    return run_strategy(strategy, ctx)


__all__ = [
    "AttemptResult",
    "AttemptStatus",
    "CascadedInstallResult",
    "InstallContext",
    "StageEvent",
    "StrategyDescriptor",
    "build_context_from_path",
    "get_strategy",
    "install_cascade",
    "install_with_strategy",
    "list_strategies",
    "run_cascade",
    "run_strategy",
]
