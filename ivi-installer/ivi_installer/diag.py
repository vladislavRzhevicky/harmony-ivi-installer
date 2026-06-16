"""Enable ADB on Avatr/Deepal IVI via DoIP/UDS over Ethernet.

When OTA firmware updates kill the USB ADB socket, the only
remaining channel into the IVI is the diagnostic Ethernet bus
(192.168.69.0/24). This module reproduces the official-tester
sequence that the dealer software uses to flip the ADB-enable
RoutineControl on ECU 0x0300:

    1. mTLS handshake on TLS-gateway:30504 — wakes the gateway
       from deep-sleep so it keeps the DoIP TCP listener alive
       past its idle timeout (~10s without TLS).
    2. DoIP TCP connect on gateway:13400, RoutingActivation.
    3. UDS DiagSession 0x03 (extended).
    4. UDS SecurityAccess: request seed → derive key → send key.
    5. UDS RoutineControl 0x030C — Enable ADB.

NOTE (public build): the per-manufacturer secrets this path needs —
the seed→key mask and the mTLS Tester certificate/key — are REDACTED
from this source tree. The DoIP/UDS method itself (framing, routing
activation, session/security-access/routine-control sequence) is
published in full; supply your own credentials via the
``IVI_DOIP_*`` environment variables to make it functional. See the
"REDACTED in the public release" block below.

Reference: ISO 13400 (DoIP), ISO 14229-1 (UDS).
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
import struct
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

# DoIP / UDS protocol constants ------------------------------------------------

PORT_DOIP = 13400
PORT_TLS = 30504

TESTER_ADDR = 0x0E80   # 3712 — tester logical address
TARGET_ADDR = 0x0300   # 768  — ECU that owns the ADB-enable routine
ROUTINE_ID = 0x030C    # 780  — "Enable ADB" routine

DEFAULT_DOIP_GATEWAY = "192.168.69.6"
DEFAULT_TLS_GATEWAY = "192.168.69.21"

# DoIP payload types
PT_VEHICLE_ID_REQ = 0x0001
PT_VEHICLE_ID_RESP = 0x0004
PT_ROUTING_ACT_REQ = 0x0005
PT_ROUTING_ACT_RESP = 0x0006
PT_DIAG_MSG = 0x8001
PT_DIAG_ACK = 0x8002
PT_DIAG_NACK = 0x8003

ROUTING_ACT_OK = 0x10  # response code: routing activated

# ─────────────────────────────────────────────────────────────────────────────
# REDACTED in the public release.
#
# The DoIP/UDS ADB-enable path needs two things that are NOT shipped here:
#
#   1. SEED_KEY_MASK    — the seed→key transform for the SecurityAccess
#                         challenge on ECU 0x0300.
#   2. TESTER_CHAIN_PEM / TESTER_PRIVATE_KEY_PEM — the mTLS client identity
#                         that wakes the diagnostic gateway on port 30504.
#
# These are vehicle-manufacturer cryptographic credentials. They are
# intentionally left out of the public source tree. Supply your own (e.g.
# extracted from your own dealer-tooling, or issued to you) via environment
# variables or files, and this module becomes functional again. With the
# placeholders below it raises a clear error instead of attempting the
# handshake.
#
# Provide:
#   IVI_DOIP_SEED_KEY_MASK   = comma- or hex-encoded 4-byte mask, e.g. "1,2,3,4"
#   IVI_DOIP_TESTER_CHAIN    = path to a PEM file with the tester cert chain
#   IVI_DOIP_TESTER_KEY      = path to a PEM file with the tester private key
#
# Everything ELSE in this file — the DoIP framing, RoutingActivation, the UDS
# session/SecurityAccess/RoutineControl sequence — is the documented method and
# is published in full. Only the per-manufacturer secrets are withheld.
# ─────────────────────────────────────────────────────────────────────────────

def _load_seed_key_mask() -> bytes | None:
    raw = os.environ.get("IVI_DOIP_SEED_KEY_MASK", "").strip()
    if not raw:
        return None
    try:
        if "," in raw:
            return bytes(int(x, 0) & 0xFF for x in raw.split(","))
        return bytes.fromhex(raw.replace("0x", "").replace(" ", ""))
    except ValueError:
        log.warning("IVI_DOIP_SEED_KEY_MASK set but unparseable; ignoring")
        return None


def _load_pem(env_var: str) -> str | None:
    path = os.environ.get(env_var, "").strip()
    if not path:
        return None
    try:
        with open(path, "r", encoding="ascii") as fh:
            return fh.read()
    except OSError as exc:
        log.warning("%s set to %r but unreadable: %s", env_var, path, exc)
        return None


SEED_KEY_MASK = _load_seed_key_mask()
TESTER_CHAIN_PEM = _load_pem("IVI_DOIP_TESTER_CHAIN")
TESTER_PRIVATE_KEY_PEM = _load_pem("IVI_DOIP_TESTER_KEY")


class CredentialsUnavailableError(RuntimeError):
    """Raised when the redacted DoIP credentials were not supplied."""


def _require_credentials() -> None:
    missing = [
        name for name, val in (
            ("IVI_DOIP_SEED_KEY_MASK", SEED_KEY_MASK),
            ("IVI_DOIP_TESTER_CHAIN", TESTER_CHAIN_PEM),
            ("IVI_DOIP_TESTER_KEY", TESTER_PRIVATE_KEY_PEM),
        ) if not val
    ]
    if missing:
        raise CredentialsUnavailableError(
            "DoIP ADB-enable credentials are redacted from the public build. "
            "Supply your own via: " + ", ".join(missing) + ". "
            "See diag.py header and docs/ for the method."
        )


# Stage labels — used by the UI to render the pipeline. Order matters
# (the worker emits stage indices into this list). The last stage's
# label is action-specific; use `stages_for(action)` to get the right
# tuple. `STAGES` is kept as the enable variant for backwards-compat
# with imports / tests.
_STAGES_PREFIX = (
    "mTLS handshake",
    "DoIP TCP connect",
    "Routing activation",
    "Diagnostic session",
    "Security access (seed → key)",
)
STAGES = _STAGES_PREFIX + ("Routine 0x030C — Enable ADB",)


def stages_for(action: str = "enable") -> tuple[str, ...]:
    """Pipeline labels for a given action.

    `action` is "enable" or "disable" — only the trailing stage label
    differs. The UI rebuilds the Pipeline widget via `set_stages` when
    the user picks an action so the right label shows up.
    """
    if action == "enable":
        last = "Routine 0x030C — Enable ADB"
    elif action == "disable":
        last = "Routine 0x030C — Disable ADB (stopRoutine)"
    else:
        raise ValueError(f"unknown action: {action!r}")
    return _STAGES_PREFIX + (last,)


# Stage callback type: (stage_idx, state, hint).
# state is one of "running", "done", "failed", "skipped".
StageCb = Callable[[int, str, str], None]


@dataclass
class Gateway:
    ip: str
    vin: str
    logical_addr: int


# ---- Frame helpers (DoIP) ---------------------------------------------------

def _doip(ptype: int, payload: bytes) -> bytes:
    """Wrap a DoIP frame: version(2) | inv_version(0xFD) | ptype:H | len:I | payload."""
    return struct.pack(">BBHI", 0x02, 0xFD, ptype, len(payload)) + payload


def _routing_activation_payload() -> bytes:
    """RoutingActivationRequest: tester:H | type:B | reserved:I | oem:4s.
    type=0 → default (no auth), reserved=0, oem=zero — what the dealer
    tool sends.
    """
    return struct.pack(">HBI4s", TESTER_ADDR, 0, 0, b"\x00\x00\x00\x00")


def _diag_msg(uds: bytes, target: int = TARGET_ADDR) -> bytes:
    """DoIP DiagnosticMessage: source:H | target:H | uds[]"""
    return _doip(PT_DIAG_MSG, struct.pack(">HH", TESTER_ADDR, target) + uds)


def _recv_frame(sock, timeout: float = 5.0) -> tuple[int, bytes]:
    """Read one full DoIP frame from `sock`. Returns (ptype, payload)."""
    sock.settimeout(timeout)
    hdr = b""
    while len(hdr) < 8:
        chunk = sock.recv(8 - len(hdr))
        if not chunk:
            raise ConnectionError("сокет закрыт")
        hdr += chunk
    if hdr[0] != 0x02 or hdr[1] != 0xFD:
        raise ValueError(f"некорректный DoIP заголовок: {hdr.hex()}")
    ptype = struct.unpack(">H", hdr[2:4])[0]
    plen = struct.unpack(">I", hdr[4:8])[0]
    payload = b""
    while len(payload) < plen:
        chunk = sock.recv(plen - len(payload))
        if not chunk:
            raise ConnectionError("сокет закрыт во время приёма payload")
        payload += chunk
    return ptype, payload


# ---- Seed → key -------------------------------------------------------------

def seed_to_key(seed: bytes) -> bytes:
    """Compute the SecurityAccess key from a 4-byte seed.

    Algorithm (reverse-engineered from AWT_SA.SeedKey.Seed2Key):

        m = SEED_KEY_MASK            # 4-byte mask, redacted (see header)
        arr1[i] = (seed[i] ^ m[3-i]) & 0xFF                  for i in 0..3
        seed2 = bit-reversed seed-as-uint32
        sb2 = seed2.to_bytes(4, 'big')
        arr2[i] = (sb2[i] ^ m[3-i]) & 0xFF                   for i in 0..3
        key = (arr1_uint32 + arr2_uint32) & 0xFFFFFFFF
        return key.to_bytes(4, 'big')
    """
    if len(seed) != 4:
        raise ValueError(f"seed must be 4 bytes, got {len(seed)}")
    m = SEED_KEY_MASK
    arr1 = bytes((seed[i] ^ m[3 - i]) & 0xFF for i in range(4))
    seed_int = int.from_bytes(seed, "big")
    seed2_int = int(format(seed_int, "032b")[::-1], 2)
    sb2 = seed2_int.to_bytes(4, "big")
    arr2 = bytes((sb2[i] ^ m[3 - i]) & 0xFF for i in range(4))
    k = (int.from_bytes(arr1, "big") + int.from_bytes(arr2, "big")) & 0xFFFFFFFF
    return k.to_bytes(4, "big")


# ---- TLS session ------------------------------------------------------------

def _build_tls_context() -> ssl.SSLContext:
    """Build a CERT_NONE / mTLS-client SSLContext loaded with the Tester
    chain + private key. Hostname verification is disabled — the IVI
    side uses self-signed Avatr certs, and the test harness doesn't
    verify them either.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Some firmware images run an old TLS stack that needs legacy
    # renegotiation; AvatrAppInstaller sets this flag, so we mirror it.
    legacy_flag = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
    if legacy_flag:
        ctx.options |= legacy_flag

    # ssl.SSLContext.load_cert_chain wants paths, not strings — write the
    # PEMs to short-lived tempfiles and unlink immediately after the
    # load.
    cert_f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".pem", delete=False)
    try:
        cert_f.write(TESTER_CHAIN_PEM)
        cert_f.close()
        key_f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".pem", delete=False)
        try:
            key_f.write(TESTER_PRIVATE_KEY_PEM)
            key_f.close()
            ctx.load_cert_chain(certfile=cert_f.name, keyfile=key_f.name)
        finally:
            try:
                os.unlink(key_f.name)
            except OSError:
                pass
    finally:
        try:
            os.unlink(cert_f.name)
        except OSError:
            pass
    return ctx


def open_tls_session(
    tls_ip: str, timeout: float = 10.0
) -> tuple[ssl.SSLSocket, str]:
    """mTLS handshake on `tls_ip:30504`. Returns (socket, info_str).

    The TLS session is what keeps the DoIP gateway alive — without an
    active mTLS connection on 30504 the gateway closes the 13400 TCP
    listener after ~10s of idle. The caller must keep the returned
    socket open for the duration of the diag session.
    """
    ctx = _build_tls_context()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.settimeout(timeout)
    raw.connect((tls_ip, PORT_TLS))
    tls = ctx.wrap_socket(
        raw, server_hostname=tls_ip, do_handshake_on_connect=True)
    cipher = tls.cipher()
    info = f"{tls.version()} {cipher[0] if cipher else ''}"
    return tls, info


# ---- Discovery --------------------------------------------------------------

def discover_gateways(timeout: float = 2.0) -> list[Gateway]:
    """UDP-broadcast DoIP VehicleIdentificationRequest. Returns a list
    of (ip, vin, logical_addr) tuples for everyone that answered.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.sendto(_doip(PT_VEHICLE_ID_REQ, b""),
                    ("255.255.255.255", PORT_DOIP))
        results: list[Gateway] = []
        seen: set[str] = set()
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(1500)
            except socket.timeout:
                break
            if addr[0] in seen:
                continue
            if (len(data) < 40 or data[0] != 0x02 or data[1] != 0xFD):
                continue
            ptype = struct.unpack(">H", data[2:4])[0]
            if ptype != PT_VEHICLE_ID_RESP:
                continue
            payload = data[8:]
            vin = payload[:17].decode("ascii", errors="replace")
            logical = struct.unpack(">H", payload[17:19])[0]
            seen.add(addr[0])
            results.append(Gateway(addr[0], vin, logical))
        return results
    finally:
        sock.close()


# ---- enable_adb -------------------------------------------------------------

class EnableAdbError(Exception):
    """Raised by `enable_adb` on a non-recoverable protocol failure."""


@dataclass
class EnableAdbResult:
    ok: bool
    msg: str
    vin: Optional[str] = None
    cipher: Optional[str] = None


def _diag_exchange(
    sock,
    self_target: int,
    uds_bytes: bytes,
    expect_sid: int,
    label: str,
    cb: Optional[StageCb],
    stage: int,
    deadline_after: float = 8.0,
) -> tuple[Optional[bytes], Optional[str]]:
    """Send a UDS message and wait for a positive response.

    Returns (uds_payload, None) on success or (None, error_msg) on
    NACK/timeout/UDS-negative-response. Mirrors the inner closure in
    AVATR's enable_adb.
    """
    sock.sendall(_diag_msg(uds_bytes, target=self_target))
    deadline = time.time() + deadline_after
    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            pt, pl = _recv_frame(sock, timeout=max(0.1, remaining))
        except socket.timeout:
            return None, "таймаут ожидания ответа"
        if pt == PT_DIAG_ACK:
            # Positive ACK from gateway — keep waiting for the actual
            # diagnostic response.
            continue
        if pt == PT_DIAG_NACK:
            return None, f"DoIP NACK: {pl.hex()}"
        if pt != PT_DIAG_MSG or len(pl) < 5:
            continue
        # DiagMessage: source:H | target:H | uds[]
        uds = pl[4:]
        if len(uds) >= 3 and uds[0] == 0x7F:
            # Negative response. NRC 0x78 = "responsePending" — keep
            # waiting, the ECU is still working.
            if uds[2] == 0x78:
                continue
            return None, f"NEG SID=0x{uds[1]:02x} NRC=0x{uds[2]:02x}"
        if uds[0] == expect_sid:
            return uds, None
        return None, f"неожиданный UDS-ответ: {uds.hex()}"
    return None, "таймаут ожидания ответа"


def _run_adb_routine(
    action: str,
    doip_gateway: str = DEFAULT_DOIP_GATEWAY,
    tls_gateway: str = DEFAULT_TLS_GATEWAY,
    target_addr: int = TARGET_ADDR,
    stage_cb: Optional[StageCb] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> EnableAdbResult:
    """Shared core of `enable_adb` / `disable_adb`. Blocking.

    Stages 1–5 are identical for both actions (mTLS → DoIP →
    RoutingActivation → DiagSession → SecurityAccess). Stage 6 swaps
    the UDS RoutineControl sub-function:

    - "enable":  `31 01 03 0C` (startRoutine) — what the dealer tool
      sends. ECU returns a positive 0x71 with status=0x00 on success.
    - "disable": `31 02 03 0C` (stopRoutine) — the standard UDS
      "reverse" of startRoutine. The dealer tool doesn't ship a
      disable function, so we don't have a confirmed status-byte
      contract; we accept any positive 0x71 response. If the ECU
      doesn't support stopRoutine on this ID it returns NRC 0x24
      (requestSequenceError) or 0x12 (subFunctionNotSupported), which
      we surface verbatim — nothing harmful happens.
    """
    _require_credentials()  # redacted in the public build — see module header
    if action == "enable":
        routine_uds = b"\x31\x01" + struct.pack(">H", ROUTINE_ID)
        routine_label = "RoutineControl Enable ADB"
        success_msg = "ADB включён! Подключите USB-кабель."
        # Avatr's ECU returns status=0x00 on success for the dealer
        # routine; treat anything else as a failure.
        strict_status = True
    elif action == "disable":
        routine_uds = b"\x31\x02" + struct.pack(">H", ROUTINE_ID)
        routine_label = "RoutineControl Disable ADB (stopRoutine)"
        success_msg = "Disable routine принята ECU."
        # stopRoutine semantics are looser — we accept any positive
        # 0x71 response. If a status byte is present and nonzero we
        # surface it as a hint but don't fail.
        strict_status = False
    else:
        raise ValueError(f"unknown action: {action!r}")
    def _stage(i: int, state: str, hint: str = "") -> None:
        if stage_cb is not None:
            try:
                stage_cb(i, state, hint)
            except Exception:
                log.exception("stage_cb raised")
    def _log(line: str) -> None:
        log.info("[diag] %s", line)
        if log_cb is not None:
            try:
                log_cb(line)
            except Exception:
                log.exception("log_cb raised")

    tls_sock: Optional[ssl.SSLSocket] = None
    sock: Optional[socket.socket] = None
    try:
        # ---- 1. mTLS wake-up
        _stage(0, "running", f"{tls_gateway}:{PORT_TLS}")
        _log(f"mTLS handshake → {tls_gateway}:{PORT_TLS}")
        try:
            tls_sock, tls_info = open_tls_session(tls_gateway, timeout=10.0)
        except (socket.timeout, ConnectionError, OSError, ssl.SSLError) as e:
            hint = (f"{type(e).__name__}: {e}. "
                    "Проверь: зажигание ON, кабель в OBD, "
                    "IP в 192.168.69.x, VPN отключён, "
                    "ASD не запущен (он держит свою сессию).")
            _stage(0, "failed", str(e))
            return EnableAdbResult(False, f"mTLS wake-up: {hint}")
        _stage(0, "done", tls_info)
        _log(f"TLS up: {tls_info}")

        # ---- 2. DoIP TCP connect (with one retry — gateway sometimes
        # needs a moment between TLS handshake and listening on 13400)
        _stage(1, "running", f"{doip_gateway}:{PORT_DOIP}")
        connect_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                sock = socket.create_connection(
                    (doip_gateway, PORT_DOIP), timeout=5.0)
                connect_err = None
                break
            except (socket.timeout, ConnectionError, OSError) as e:
                connect_err = e
                _log(f"DoIP TCP attempt {attempt + 1}: {e}")
                time.sleep(0.5)
        if sock is None or connect_err is not None:
            _stage(1, "failed", str(connect_err) if connect_err else "")
            return EnableAdbResult(
                False, f"DoIP TCP connect: {doip_gateway}:{PORT_DOIP} ({connect_err})")
        _stage(1, "done", f"{doip_gateway}:{PORT_DOIP}")
        _log(f"DoIP TCP up on {doip_gateway}:{PORT_DOIP}")

        # ---- 3. Routing activation
        _stage(2, "running", "")
        sock.sendall(_doip(PT_ROUTING_ACT_REQ, _routing_activation_payload()))
        try:
            ptype, payload = _recv_frame(sock, timeout=5.0)
        except (socket.timeout, ConnectionError, OSError) as e:
            _stage(2, "failed", str(e))
            return EnableAdbResult(
                False, f"RoutingActivation: {type(e).__name__}: {e}")
        if ptype != PT_ROUTING_ACT_RESP or len(payload) < 5:
            _stage(2, "failed", f"ptype=0x{ptype:04x}")
            return EnableAdbResult(
                False, f"RoutingActivation: unexpected ptype 0x{ptype:04x}")
        # payload layout: tester:H | entity:H | code:B | reserved:I [| oem:I]
        entity = struct.unpack(">H", payload[2:4])[0]
        code = payload[4]
        if code != ROUTING_ACT_OK:
            _stage(2, "failed", f"code=0x{code:02x}")
            return EnableAdbResult(
                False,
                f"DoIP RoutingActivation отклонена (code=0x{code:02x}, "
                f"entity=0x{entity:04x})")
        _stage(2, "done", f"entity=0x{entity:04x}")
        _log(f"RoutingActivation OK, entity=0x{entity:04x}")

        # ---- 4. Diagnostic session 0x03 (extended)
        _stage(3, "running", "")
        uds, err = _diag_exchange(
            sock, target_addr, b"\x10\x03", expect_sid=0x50,
            label="DiagSession 0x03", cb=stage_cb, stage=3)
        if err:
            _stage(3, "failed", err)
            return EnableAdbResult(False, f"DiagSession: {err}")
        _stage(3, "done", "extended OK")
        _log("DiagSession 0x03 → extended OK")

        # ---- 5. Security access: request seed → derive key → send key
        _stage(4, "running", "request seed")
        uds, err = _diag_exchange(
            sock, target_addr, b"\x27\x01", expect_sid=0x67,
            label="SecurityAccess seed", cb=stage_cb, stage=4)
        if err:
            _stage(4, "failed", f"seed: {err}")
            return EnableAdbResult(False, f"Запрос seed: {err}")
        # uds = [0x67, sub=0x01, seed[0..3]]
        if uds is None or len(uds) < 6:
            _stage(4, "failed", "ответ слишком короткий")
            return EnableAdbResult(False, "Запрос seed: ответ слишком короткий")
        seed = bytes(uds[2:6])
        key = seed_to_key(seed)
        _log(f"seed={seed.hex()} key={key.hex()}")
        _stage(4, "running", f"seed={seed.hex()} → key={key.hex()}")
        uds, err = _diag_exchange(
            sock, target_addr, b"\x27\x02" + key, expect_sid=0x67,
            label="SecurityAccess key", cb=stage_cb, stage=4)
        if err:
            _stage(4, "failed", f"key: {err}")
            return EnableAdbResult(False, f"Ключ отвергнут: {err}")
        _stage(4, "done", f"key={key.hex()}")
        _log("SecurityAccess granted")

        # ---- 6. RoutineControl 0x030C
        _stage(5, "running", f"0x{ROUTINE_ID:04X} ({action})")
        uds, err = _diag_exchange(
            sock, target_addr, routine_uds,
            expect_sid=0x71,
            label=routine_label, cb=stage_cb, stage=5)
        if err:
            _stage(5, "failed", err)
            return EnableAdbResult(False, f"Рутина: {err}")
        # Routine response: [0x71, sub, routine_id:H, status:B, ...]
        if uds is None or len(uds) < 4:
            _stage(5, "failed", "ответ слишком короткий")
            return EnableAdbResult(False, "Рутина: ответ слишком короткий")
        status_byte = uds[4] if len(uds) >= 5 else None
        if strict_status:
            if status_byte is None:
                _stage(5, "failed", "no status byte")
                return EnableAdbResult(False, "Рутина: ответ без status байта")
            if status_byte != 0x00:
                _stage(5, "failed", f"status=0x{status_byte:02x}")
                return EnableAdbResult(
                    False, f"Рутина вернула status=0x{status_byte:02x}")
            _stage(5, "done", "status=0x00")
            _log(f"Routine 0x030C [{action}] → ADB enabled")
        else:
            # disable: any positive 0x71 = success; surface status if any
            if status_byte is None:
                _stage(5, "done", "0x71 (no status)")
            elif status_byte == 0x00:
                _stage(5, "done", "status=0x00")
            else:
                _stage(5, "done", f"status=0x{status_byte:02x} (nonzero)")
            _log(f"Routine 0x030C [{action}] → ECU acknowledged")

        return EnableAdbResult(True, success_msg)

    except Exception as e:  # pragma: no cover — defensive guard
        log.exception("_run_adb_routine[%s] crashed", action)
        return EnableAdbResult(False, f"Сетевая ошибка: {type(e).__name__}: {e}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if tls_sock is not None:
            try:
                tls_sock.close()
            except OSError:
                pass


def enable_adb(
    doip_gateway: str = DEFAULT_DOIP_GATEWAY,
    tls_gateway: str = DEFAULT_TLS_GATEWAY,
    target_addr: int = TARGET_ADDR,
    stage_cb: Optional[StageCb] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> EnableAdbResult:
    """Run the full enable-ADB sequence — UDS startRoutine on 0x030C.

    `stage_cb(stage_idx, state, hint)` fires for each of the six stage
    transitions (see `STAGES`). `log_cb(line)` receives free-form
    one-line status strings suitable for the log pane.
    """
    return _run_adb_routine(
        "enable",
        doip_gateway=doip_gateway,
        tls_gateway=tls_gateway,
        target_addr=target_addr,
        stage_cb=stage_cb,
        log_cb=log_cb,
    )


def disable_adb(
    doip_gateway: str = DEFAULT_DOIP_GATEWAY,
    tls_gateway: str = DEFAULT_TLS_GATEWAY,
    target_addr: int = TARGET_ADDR,
    stage_cb: Optional[StageCb] = None,
    log_cb: Optional[Callable[[str], None]] = None,
) -> EnableAdbResult:
    """Run the disable-ADB sequence — UDS stopRoutine on the same ID.

    The dealer toolchain doesn't ship a disable function, so this is
    the safest plausible reverse of `enable_adb`: ISO 14229 says
    sub-function 0x02 of RoutineControl means "stop the routine that
    was started with sub 0x01". If the ECU doesn't support it we get
    a clean NRC byte (0x12 / 0x24 / 0x31) and nothing breaks.
    """
    return _run_adb_routine(
        "disable",
        doip_gateway=doip_gateway,
        tls_gateway=tls_gateway,
        target_addr=target_addr,
        stage_cb=stage_cb,
        log_cb=log_cb,
    )
