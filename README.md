# Installing Android apps on Huawei-automotive head units

Open research + tooling for side-loading ordinary Android **APKs** onto Chinese
EVs that run a **HarmonyOS Cockpit** head unit - Deepal, Avatr, Aito, and other
Huawei-stack IVIs - where a plain `adb install` is rejected by the OEM installer
policy.

These cars are AOSP underneath, so they *can* run Android apps (navigation,
media, keyboards). This repo documents **how**, end to end, and ships a working
desktop tool that does it.

> ⚠️ **Read [`DISCLAIMER.md`](DISCLAIMER.md) before anything else.** This is for
> research and for vehicles you own or are authorized to service. It requires
> physical USB access and a dealer-unlocked ADB - nothing here works remotely.

---

## What's in here

```
public/
├── README.md            ← you are here
├── DISCLAIMER.md        ← scope, ethics, legal - read first
├── docs/                ← the method, explained
│   ├── 01-overview.md          the whole picture
│   ├── 02-hdb-broker.md        HDB broker install (HarmonyOS 5.0 path)
│   ├── 03-pm-disable.md        PM-disable install (lighter primary)
│   ├── 04-screens-and-users.md cabin displays ↔ Android users
│   ├── 05-runtime-permissions.md  granting perms the shell can't
│   └── 06-compatibility.md     APK rules, failure codes, troubleshooting
├── broker/              ← the on-device HDB bypass daemon
│   ├── src/                    decompiled daemon source (reference)
│   ├── perm-grant/             reflection permission-grant helper (original source)
│   └── avatr-hdb-broker.jar    prebuilt, ready to push
└── ivi-installer/       ← the desktop app that automates all of it
```

## The 60-second version

A normal install fails because (1) `adbd` is `uid=shell` with no root, and
(2) Huawei's `HwInstallPolicy` hook auto-rejects the "external sources" dialog.
Two non-root strategies get around it:

1. **PM-disable** - temporarily disable the dialog component
   (`com.android.packageinstaller`), run `pm install -i <oem-installer>`,
   re-enable it. → [docs/03](docs/03-pm-disable.md)
2. **HDB broker** - a tiny `app_process` daemon uses reflection to create an
   install session flagged as HDB-initiated (`installFlags |= 262144`), which
   the policy hook trusts. → [docs/02](docs/02-hdb-broker.md) +
   [broker/](broker/)

Then the app is **fanned out** to every cabin screen
(`pm install-existing --user N`, one user per display →
[docs/04](docs/04-screens-and-users.md)) and its **runtime permissions** are
granted via a reflection helper, because `pm grant` from the shell is silently
swallowed → [docs/05](docs/05-runtime-permissions.md).

## The tool

[`ivi-installer/`](ivi-installer/) is a self-contained macOS/Windows desktop app
(PySide6) implementing the full cascade locally - **no license server, no VIN
allow-list, no telemetry**.

```bash
cd ivi-installer
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m ivi_installer            # GUI
.venv/bin/python -m ivi_installer --cli      # list devices
```

See [`ivi-installer/README.md`](ivi-installer/README.md) for build + scripting.

## Honesty about what's published

- **The broker is reverse-engineered.** The `AvatrHdbBroker` /
  `HuaweiShellBridge` sources in [`broker/src/`](broker/src/) are decompiler
  output from a third-party tool's `app_process` jar, kept as an auditable
  reference. The permission-grant helper is original source.
- **Personal secrets are removed.** No VINs, device serials, accounts, or
  servers. The DoIP "Enable ADB" recovery path keeps its full method but has the
  manufacturer seed→key mask and mTLS Tester key **redacted** - supply your own
  via `IVI_DOIP_*` env vars to re-enable that one feature.
- **Large/copyrighted binaries excluded** (a bundled keyboard APK, Google's
  `platform-tools`) - documented as optional drop-ins.

## License

No open-source license is granted. See [`DISCLAIMER.md`](DISCLAIMER.md) - this
is published for research and authorized use, with no warranty.
