# IVI Installer

A small cross-platform (macOS + Windows) desktop app that side-loads Android
APKs onto **Huawei-automotive head units** - Deepal, Avatr, and other
HarmonyOS-Cockpit / HwSAPT class IVIs - where the locked-down OEM installer
policy refuses a plain `adb install`.

This is the **public, self-contained release** (`0.25.0`). It runs entirely on
your machine: there is no license server, no VIN allow-list, no telemetry. The
full install cascade is local Python you can read in
[`ivi_installer/strategies.py`](ivi_installer/strategies.py).

> ⚠️ **Read [`../DISCLAIMER.md`](../DISCLAIMER.md) first.** This tool is for
> research and for vehicles you own or are authorized to service. It needs
> physical USB access and a dealer-unlocked ADB. It does not exploit anything
> remotely.

---

## What it does

- Detects connected IVI head units (`adb devices`) and reads their
  capabilities, screens, and Android users.
- Pushes an `.apk` through one of two install strategies (see
  [`../docs/01-overview.md`](../docs/01-overview.md)):
  - **PM-disable install** (primary, lighter)
  - **HDB broker install** (HarmonyOS 5.0 path, via the on-device daemon)
- Fans the app out to every cabin screen (driver / passenger / rear) by
  resolving the per-screen Android user.
- Grants runtime permissions that PMS otherwise silently blocks
  (mic / camera / location / storage), via an on-device reflection helper.
- Extras: device-info inspector, timezone fix, keyboard (IME) enrolment,
  installer-footprint cleanup, and a DoIP/UDS "Enable ADB" recovery tab
  (**credentials redacted** - see below).

## Quick start (run from source)

Requires Python ≥ 3.11 and `adb` on your `PATH` (or let the app download it).

```bash
cd ivi-installer
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# Sanity check: list connected devices, no GUI
.venv/bin/python -m ivi_installer --cli

# Launch the GUI
.venv/bin/python -m ivi_installer
```

Drop an `.apk` onto the drop zone, pick the target screens, click **Install**.

## Build a desktop bundle

```bash
pip install briefcase
briefcase create  macOS         # or: windows
briefcase build   macOS
briefcase package macOS --adhoc-sign     # personal use; → dist/*.dmg
```

`scripts/build.sh` chains `update → build → trim_qt → package` on macOS.
For a packaged build you may want the optional drop-in binaries - see
[`ivi_installer/resources/README.md`](ivi_installer/resources/README.md).

## Programmatic use

The install pipeline is a plain function - no GUI required:

```python
from ivi_installer import installer

result = installer.install_cascade(
    "yandex-navi.apk",
    serial="<adb-serial>",        # from `adb devices`
    grant_runtime=True,
)
print(result.success, result.message)
```

## What was changed for the public release

- **No protection layer.** This release predates the server-side VIN gate
  entirely - there is nothing to remove and nothing to phone home to.
- **Trial doormat removed** from `__main__.py` (the original handed-out test
  builds expired after 14 days; that check is gone).
- **DoIP credentials redacted.** The "Enable ADB" recovery tab
  ([`ivi_installer/diag.py`](ivi_installer/diag.py)) needs a manufacturer
  seed→key mask and an mTLS Tester certificate/key. Those are **not** shipped.
  The full DoIP/UDS method is published; supply your own credentials via
  `IVI_DOIP_SEED_KEY_MASK`, `IVI_DOIP_TESTER_CHAIN`, `IVI_DOIP_TESTER_KEY` to
  make that one tab functional. Everything else works out of the box.
- **Large/copyrighted binaries excluded** (Celia keyboard APK,
  Google `platform-tools`) - documented as optional drop-ins.

## Layout

```
ivi_installer/        the Python package
  strategies.py       the full local install cascade (read this)
  installer.py        public install API
  devices.py          screen/user resolution
  firmware.py         Huawei installer-pkg detection
  runtime_perms.py    reflection perm-grant
  diag.py             DoIP/UDS Enable-ADB (credentials redacted)
  ui/                 PySide6 GUI
  resources/          bundled jars + catalog seed
tests/                ~330 pytest cases
changelog/            per-release notes
CODER_NOTES.md        design pitfalls (QThread, cascade, screen resolution)
```

See the top-level [`../docs/`](../docs/) for the method write-ups and
[`../broker/`](../broker/) for the HDB broker source.
