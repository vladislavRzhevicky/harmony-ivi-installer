"""Collect a comprehensive read-only snapshot of a connected device.

Every command here is a pure read — `getprop`, `dumpsys`, `cat /proc/*`,
`df`, `pm list`, `uptime`. No state is mutated. This is the diagnostic
companion to `devices.py` which only gathers the small subset the rest
of the UI cares about.

Returned shape:
    [
        ("Section name", [
            ("Field label", "value"),
            ...
        ]),
        ...
    ]

Failed reads are reported as "—"; this function never raises (apart
from the wrapping adb transport errors, which the caller handles).
"""
from __future__ import annotations

import logging
import re
from typing import Callable

from . import adb

log = logging.getLogger(__name__)

PLACEHOLDER = "—"


def collect(serial: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Run all read-only probes against `serial` and return sections."""
    props = _read_getprop(serial)
    sections: list[tuple[str, list[tuple[str, str]]]] = []

    sections.append(("Identity", _identity(serial, props)))
    sections.append(("Build & OS", _build(props)))
    sections.append(("HarmonyOS", _harmony(props)))
    sections.append(("Hardware", _hardware(serial, props)))
    sections.append(("Display", _display(serial)))
    sections.append(("Memory & Storage", _memory_storage(serial)))
    sections.append(("Battery", _battery(serial)))
    sections.append(("Network", _network(serial, props)))
    sections.append(("Locale & Time", _locale_time(serial, props)))
    sections.append(("Users & Packages", _packages(serial)))
    sections.append(("ADB / Shell", _shell(serial, props)))

    return sections


# ---- raw probes ----

def _read_getprop(serial: str) -> dict[str, str]:
    """One bulk `getprop` and parse: lines look like '[key]: [value]'."""
    try:
        r = adb.run("shell", "getprop", serial=serial, check=False, timeout=20)
    except Exception as e:
        log.warning("getprop failed: %s", e)
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        m = re.match(r"\[(.+?)\]:\s*\[(.*)\]", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _safe_run(serial: str, *args: str, timeout: int = 20) -> str:
    """Run a read-only command, never raise. Return stdout (or "")."""
    try:
        r = adb.run(*args, serial=serial, check=False, timeout=timeout)
        return r.stdout
    except Exception as e:
        log.warning("read %s failed: %s", args, e)
        return ""


def _first(props: dict[str, str], *keys: str) -> str:
    """Return the first non-empty getprop value among `keys`, else PLACEHOLDER."""
    for k in keys:
        v = props.get(k, "").strip()
        if v:
            return v
    return PLACEHOLDER


# ---- section builders ----

def _identity(serial: str, props: dict[str, str]) -> list[tuple[str, str]]:
    return [
        ("Serial", serial),
        ("Brand", _first(props, "ro.product.brand")),
        ("Manufacturer", _first(props, "ro.product.manufacturer")),
        ("Model", _first(props, "ro.product.model")),
        ("Marketing name", _first(props, "ro.product.marketname",
                                    "ro.config.marketing_name")),
        ("Device", _first(props, "ro.product.device")),
        ("Product", _first(props, "ro.product.name")),
        ("Hardware", _first(props, "ro.hardware")),
        ("Board", _first(props, "ro.product.board")),
        ("Fingerprint", _first(props, "ro.build.fingerprint")),
    ]


def _build(props: dict[str, str]) -> list[tuple[str, str]]:
    return [
        ("Android version", _first(props, "ro.build.version.release")),
        ("API level", _first(props, "ro.build.version.sdk")),
        ("Codename", _first(props, "ro.build.version.codename")),
        ("Security patch", _first(props, "ro.build.version.security_patch")),
        ("Build ID", _first(props, "ro.build.id")),
        ("Build type", _first(props, "ro.build.type")),
        ("Build tags", _first(props, "ro.build.tags")),
        ("Build user", _first(props, "ro.build.user")),
        ("Build host", _first(props, "ro.build.host")),
        ("Build date", _first(props, "ro.build.date")),
    ]


def _harmony(props: dict[str, str]) -> list[tuple[str, str]]:
    return [
        ("HarmonyOS version", _first(props, "hw_sc.build.platform.version",
                                       "ro.huawei.build.display.id")),
        ("EMUI version", _first(props, "ro.build.version.emui")),
        ("Hi-suite version", _first(props, "ro.huawei.build.version")),
        ("HarmonyOS country", _first(props, "ro.huawei.region",
                                       "persist.sys.country")),
        ("Carrier", _first(props, "ro.config.carrier", "gsm.sim.operator.alpha")),
    ]


def _hardware(serial: str, props: dict[str, str]) -> list[tuple[str, str]]:
    cpuinfo = _safe_run(serial, "shell", "cat", "/proc/cpuinfo", timeout=10)
    cores = sum(1 for line in cpuinfo.splitlines() if line.startswith("processor"))
    cpu_model = ""
    for line in cpuinfo.splitlines():
        if line.startswith("Hardware") or line.startswith("model name"):
            cpu_model = line.split(":", 1)[1].strip()
            break
    return [
        ("Primary ABI", _first(props, "ro.product.cpu.abi")),
        ("All ABIs", _first(props, "ro.product.cpu.abilist")),
        ("CPU model", cpu_model or PLACEHOLDER),
        ("CPU cores", str(cores) if cores else PLACEHOLDER),
        ("Bootloader", _first(props, "ro.bootloader")),
        ("Bootmode", _first(props, "ro.bootmode")),
    ]


def _display(serial: str) -> list[tuple[str, str]]:
    size = _safe_run(serial, "shell", "wm", "size", timeout=10).strip()
    density = _safe_run(serial, "shell", "wm", "density", timeout=10).strip()
    # Strip noisy prefixes ("Physical size: 2400x1080" → "2400x1080").
    size_v = size.split(":", 1)[-1].strip() if size else PLACEHOLDER
    density_v = density.split(":", 1)[-1].strip() if density else PLACEHOLDER
    return [
        ("Size (px)", size_v or PLACEHOLDER),
        ("Density (dpi)", density_v or PLACEHOLDER),
    ]


def _memory_storage(serial: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    meminfo = _safe_run(serial, "shell", "cat", "/proc/meminfo", timeout=10)
    for line in meminfo.splitlines()[:3]:  # MemTotal/MemFree/MemAvailable
        parts = line.split()
        if len(parts) >= 2:
            kb = int(parts[1]) if parts[1].isdigit() else 0
            label = parts[0].rstrip(":")
            out.append((label, f"{kb / 1024:.0f} MB" if kb else PLACEHOLDER))

    df_data = _safe_run(serial, "shell", "df", "-h", "/data", timeout=10)
    for line in df_data.splitlines():
        cols = line.split()
        if len(cols) >= 5 and cols[-1] == "/data":
            out.append(("/data total", cols[1]))
            out.append(("/data used", f"{cols[2]} ({cols[4]})"))
            break
    return out


def _battery(serial: str) -> list[tuple[str, str]]:
    text = _safe_run(serial, "shell", "dumpsys", "battery", timeout=10)
    fields = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    pick = lambda key: fields.get(key, "") or PLACEHOLDER
    level = fields.get("level", "")
    scale = fields.get("scale", "100")
    pct = ""
    if level.isdigit() and scale.isdigit() and int(scale):
        pct = f" ({int(level) * 100 // int(scale)}%)"
    return [
        ("Level", f"{level}/{scale}{pct}" if level else PLACEHOLDER),
        ("Status", pick("status")),
        ("Health", pick("health")),
        ("Plugged", pick("plugged")),
        ("Technology", pick("technology")),
        ("Voltage (mV)", pick("voltage")),
        ("Temperature (0.1°C)", pick("temperature")),
    ]


def _network(serial: str, props: dict[str, str]) -> list[tuple[str, str]]:
    addrs = _safe_run(serial, "shell", "ip", "-o", "addr", timeout=10)
    ipv4 = []
    for line in addrs.splitlines():
        # "2: wlan0    inet 192.168.1.42/24 ..."
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "inet":
            iface = parts[1]
            if iface == "lo":
                continue
            ipv4.append(f"{iface}={parts[3]}")
    interfaces = _first(props, "wifi.interface")
    return [
        ("IPv4 addresses", ", ".join(ipv4) or PLACEHOLDER),
        ("Wi-Fi interface name", interfaces),
        # MAC addresses and IMEI are restricted on Android 10+ — even
        # `getprop` returns redacted values for the shell user, so we
        # don't bother probing.
    ]


def _locale_time(serial: str, props: dict[str, str]) -> list[tuple[str, str]]:
    date = _safe_run(serial, "shell", "date", "+%Y-%m-%d %H:%M:%S %Z",
                      timeout=10).strip()
    uptime = _safe_run(serial, "shell", "uptime", timeout=10).strip()
    return [
        ("Locale", _first(props, "persist.sys.locale", "ro.product.locale")),
        ("Country", _first(props, "persist.sys.country", "ro.product.locale.region")),
        ("Timezone", _first(props, "persist.sys.timezone")),
        ("Device clock", date or PLACEHOLDER),
        ("Uptime", uptime or PLACEHOLDER),
    ]


def _packages(serial: str) -> list[tuple[str, str]]:
    def count(*flags: str) -> str:
        out = _safe_run(serial, "shell", "pm", "list", "packages", *flags,
                          timeout=20)
        n = sum(1 for line in out.splitlines() if line.startswith("package:"))
        return str(n) if n else PLACEHOLDER

    users = _safe_run(serial, "shell", "pm", "list", "users", timeout=10)
    user_count = sum(1 for line in users.splitlines() if "UserInfo{" in line)

    return [
        ("Total packages", count()),
        ("System packages", count("-s")),
        ("3rd-party packages", count("-3")),
        ("Disabled packages", count("-d")),
        ("Users on device", str(user_count) if user_count else PLACEHOLDER),
    ]


def _shell(serial: str, props: dict[str, str]) -> list[tuple[str, str]]:
    user = _safe_run(serial, "shell", "whoami", timeout=10).strip()
    sel = _safe_run(serial, "shell", "getenforce", timeout=10).strip()
    return [
        ("adbd UID", user or PLACEHOLDER),
        ("SELinux mode", sel or PLACEHOLDER),
        ("Debuggable", _first(props, "ro.debuggable")),
        ("Secure", _first(props, "ro.secure")),
        ("Boot completed", _first(props, "sys.boot_completed")),
    ]


# ---- pretty-printer ----

def format_sections(
    sections: list[tuple[str, list[tuple[str, str]]]],
) -> str:
    """Render a sections list as a fixed-width plain-text block."""
    lines: list[str] = []
    for title, rows in sections:
        lines.append(f"━━ {title} ━━")
        if not rows:
            lines.append("  (no data)")
        else:
            width = max((len(k) for k, _ in rows), default=0)
            for k, v in rows:
                lines.append(f"  {k:<{width}}  {v}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
