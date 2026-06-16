"""Tests for ivi_installer.diag — DoIP/UDS Enable-ADB sequence.

Network-talking paths (open_tls_session, enable_adb, discover_gateways)
need a real IVI on the bench, so they aren't covered here. We focus on
the pure-logic primitives: the DoIP frame builders.

NOTE (public build): the SecurityAccess seed→key mask is REDACTED from
this source tree (see diag.py). The seed→key value tests that depended
on the real mask are therefore omitted. The structural tests below run
only when a mask is supplied via ``IVI_DOIP_SEED_KEY_MASK``.
"""
from __future__ import annotations

import struct

import pytest

from ivi_installer import diag

_HAS_MASK = diag.SEED_KEY_MASK is not None
_needs_mask = pytest.mark.skipif(not _HAS_MASK, reason="seed-key mask redacted")


# ---- seed_to_key (structure only — value vectors are redacted) --------------

@_needs_mask
def test_seed_to_key_length():
    """Always returns exactly 4 bytes (requires a supplied mask)."""
    assert len(diag.seed_to_key(b"\x00\x00\x00\x00")) == 4
    assert len(diag.seed_to_key(b"\xff\xff\xff\xff")) == 4


def test_seed_to_key_rejects_wrong_length():
    """Length validation happens before the mask is touched."""
    with pytest.raises(ValueError):
        diag.seed_to_key(b"\x00\x00\x00")
    with pytest.raises(ValueError):
        diag.seed_to_key(b"\x00\x00\x00\x00\x00")


@_needs_mask
def test_seed_to_key_deterministic():
    """Same seed always returns the same key — no state between calls."""
    seed = b"\xde\xad\xbe\xef"
    assert diag.seed_to_key(seed) == diag.seed_to_key(seed)


# ---- DoIP frame builders ----------------------------------------------------

def test_doip_header_layout():
    """version(2) | inv(0xFD) | ptype:H | len:I | payload."""
    frame = diag._doip(0x8001, b"abc")
    assert frame[0] == 0x02
    assert frame[1] == 0xFD
    assert struct.unpack(">H", frame[2:4])[0] == 0x8001
    assert struct.unpack(">I", frame[4:8])[0] == 3
    assert frame[8:] == b"abc"


def test_routing_activation_payload_layout():
    """tester:H | type:B | reserved:I | oem:4s — type 0 = default."""
    p = diag._routing_activation_payload()
    assert len(p) == 11
    tester = struct.unpack(">H", p[0:2])[0]
    assert tester == diag.TESTER_ADDR
    assert p[2] == 0  # activation type
    assert struct.unpack(">I", p[3:7])[0] == 0
    assert p[7:11] == b"\x00\x00\x00\x00"


def test_diag_msg_wraps_with_addresses():
    """DiagMessage payload is source:H | target:H | uds[]."""
    msg = diag._diag_msg(b"\x10\x03", target=0x0300)
    # Skip the 8-byte DoIP header.
    payload = msg[8:]
    src = struct.unpack(">H", payload[0:2])[0]
    tgt = struct.unpack(">H", payload[2:4])[0]
    uds = payload[4:]
    assert src == diag.TESTER_ADDR
    assert tgt == 0x0300
    assert uds == b"\x10\x03"


# ---- TLS context ------------------------------------------------------------

@pytest.mark.skipif(
    diag.TESTER_CHAIN_PEM is None or diag.TESTER_PRIVATE_KEY_PEM is None,
    reason="tester credentials redacted in the public build",
)
def test_build_tls_context_loads_cert():
    """The Tester chain + private key parse without raising. Doesn't
    actually open a socket — just verifies the supplied PEMs are
    syntactically valid Ed25519 material that ssl.SSLContext accepts.

    Skipped in the public build (credentials redacted); runs when the
    user supplies IVI_DOIP_TESTER_CHAIN / IVI_DOIP_TESTER_KEY.
    """
    ctx = diag._build_tls_context()
    assert ctx.verify_mode.name == "CERT_NONE"
    assert ctx.check_hostname is False


# ---- Stage labels exposed to the UI ----------------------------------------

def test_stages_are_six_strings():
    """The Pipeline widget in the UI is built directly off STAGES, so
    the count and ordering need to stay locked to the enable_adb
    sequence."""
    assert len(diag.STAGES) == 6
    assert all(isinstance(s, str) and s for s in diag.STAGES)


def test_stages_for_action_variants():
    """Pipeline labels are identical except for the trailing stage —
    enable says "Enable ADB", disable says "Disable ADB"."""
    en = diag.stages_for("enable")
    di = diag.stages_for("disable")
    assert en[:-1] == di[:-1]
    assert "Enable" in en[-1]
    assert "Disable" in di[-1]
    # `enable` matches the legacy STAGES tuple exactly.
    assert en == diag.STAGES


def test_stages_for_rejects_unknown_action():
    with pytest.raises(ValueError):
        diag.stages_for("toggle")


def test_disable_adb_is_callable():
    """Smoke check that the public disable wrapper exists and takes
    the same kwargs as `enable_adb` (the UI wires both through the
    same worker)."""
    assert callable(diag.disable_adb)
    # Inspect signature so we catch divergence early.
    import inspect
    en_sig = inspect.signature(diag.enable_adb)
    di_sig = inspect.signature(diag.disable_adb)
    assert list(en_sig.parameters) == list(di_sig.parameters)
