# Disclaimer & scope

## What this is

Research notes and tooling for installing standard Android APKs onto the head
unit of a vehicle **you own or are authorized to service**. The head units in
scope run AOSP under a HarmonyOS Cockpit shell and are perfectly capable of
running ordinary Android apps; the OEM simply locks down the installer. This
material explains and automates the install.

## What this is **not**

- **Not a remote exploit.** Every technique here needs *physical* USB access to
  the car and an ADB connection that the vehicle owner / dealer has unlocked.
  Nothing here can be done over the air or against a car you can't touch.
- **Not root.** No `su`, no `adb root`, no `/system` writes, no firmware
  modification. The methods work entirely within the unprivileged `shell` user
  and route every change through the stock `pm` / `cmd` interfaces or an
  in-process helper that calls already-present binder APIs.
- **Not a way to pirate apps.** Bring your own legally-obtained APKs.

## Use responsibly

- Only operate on a vehicle you own, or one whose owner has authorized you.
- Read-only by default: probe and understand before you change anything.
- Modifying a vehicle's software can affect its behavior and **may void
  warranty** or violate the terms you accepted with the vehicle. That's your
  call and your responsibility.
- Safety-relevant systems are out of scope. This is about user-facing apps
  (navigation, media, keyboards) on the infotainment user space - not vehicle
  control, ADAS, or any drive-by-wire function.

## Redactions

Manufacturer cryptographic material is intentionally **not** included:

- The DoIP/UDS "Enable ADB" recovery path keeps its full documented method, but
  the seed→key mask and the mTLS Tester certificate/private key are removed.
  Supply your own (e.g. issued to you, or extracted from tooling you're licensed
  to use) via the `IVI_DOIP_SEED_KEY_MASK`, `IVI_DOIP_TESTER_CHAIN`, and
  `IVI_DOIP_TESTER_KEY` environment variables.

## Provenance

The on-device HDB broker (`broker/src/`) is **reverse-engineered** - decompiled
from a third-party tool's `app_process` jar - and is published as an auditable
reference, not as original work. The permission-grant helper and the desktop app
are original.

## No warranty / no license

This repository is provided **as-is**, for research and authorized use, with
**no warranty** of any kind and **no open-source license granted**. You are
responsible for complying with the laws and contracts that apply to you and to
the vehicle you work on. The authors accept no liability for any damage,
data loss, warranty loss, or legal consequence arising from use of this
material.
