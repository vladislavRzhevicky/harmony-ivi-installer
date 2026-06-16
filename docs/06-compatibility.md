# Compatibility & troubleshooting

The bypass gets an APK *past the installer policy*. It does **not** bypass
Android's normal compatibility rules. Pre-check on the host before you push.

## Pre-flight checklist

```bash
aapt2 dump badging app.apk | head -20
```

| Requirement | Why |
|---|---|
| `minSdkVersion ≤ 31` | The head unit is Android 12 (SDK 31). A higher minSdk is rejected. |
| ABI includes `arm64-v8a` (or no native libs) | The SoC is arm64. An `armeabi-v7a`-only or `x86` build won't run. |
| Single universal `.apk` | `.xapk` / split APKs need a workaround (see below). |
| Old `targetSdk` is fine | e.g. `targetSdk=19` just throws strict-mode warnings, installs fine. |

## `.xapk` / split APKs

The multi-APK broker path hardcodes the Avatr installer package and fails on
Deepal, so this tooling rejects non-`.apk` inputs at the boundary. Options:

1. Download a **single universal APK** (APKMirror / ApkPure often offer one
   next to the `.xapk`).
2. Install just `base.apk` - fine when the splits are non-critical (extra
   densities), may crash at launch if a split holds required native libs.
3. Patch the broker jar to use the correct installer package, run on a 2nd port.

## Failure codes you'll see

| Code | Meaning | What to do |
|---|---|---|
| `INSTALL_FAILED_ABORTED: User rejected permissions` | The policy hook bounced a plain install | This is the whole reason for the broker / pm-disable; use a strategy. |
| `INSTALL_FAILED_INTERNAL_ERROR` (broker) | Wrong/absent installer package | Pass the correct OEM installer (`com.huawei.appinstaller.car` on Deepal). |
| `INSTALL_FAILED_INTERNAL_ERROR: Session relinquished` | Used a non-HDB bridge sub-command | Use `hdb-session-install-user`, not `legacy-session-install-user`. |
| `INSTALL_FAILED_VERSION_DOWNGRADE` | Installed version is newer | Add `-d` (pm-disable already does); broker path uninstalls + retries. |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | Signature mismatch vs an existing install | Uninstall the existing copy first (terminal otherwise). |
| `INSTALL_FAILED_INSUFFICIENT_STORAGE` | Disk full | Terminal - free space; no strategy helps. |
| `INSTALL_PARSE_FAILED_*` | The APK itself is broken/not an APK | Terminal - re-download. |

The cascade treats the `INSTALL_PARSE_FAILED_*`, `INVALID_APK`,
`INSUFFICIENT_STORAGE`, and `UPDATE_INCOMPATIBLE` codes as **terminal** (no
point retrying the other strategy); everything else triggers fallback.

## App installs but doesn't show on a screen

The seed succeeded but fan-out didn't reach that screen's user. Check per-user
state and fan out:

```bash
adb shell dumpsys package <pkg> | grep -E "User [0-9]+:.*installed="
adb shell pm install-existing --user <N> <pkg>
```

See [`04-screens-and-users.md`](04-screens-and-users.md). The app does this
automatically (post-install fan-out verification) and re-runs
`pm install-existing` for any missing user.

## App installs but can't use mic/camera/location

Runtime permissions weren't granted - `pm grant` from the shell is silently
swallowed. Use the reflection helper:
[`05-runtime-permissions.md`](05-runtime-permissions.md).

## `adb` can't see the car / "unauthorized"

- The head unit must be in a **dealer-unlocked / ADB-enabled** state. On some
  Deepal models USB shares the CarPlay and ADB paths - you may need to toggle
  `usb.carplay.state`.
- If an OTA killed the USB ADB socket entirely, the only way back in is the
  DoIP/UDS diagnostic bus - that path needs manufacturer credentials that are
  **redacted** from this public release (see
  [`../ivi-installer/ivi_installer/diag.py`](../ivi-installer/ivi_installer/diag.py)).
- Don't bother with `adb root` (`adbd cannot run as root in production builds`)
  or `su` - neither exists. The whole method is non-root by design.

## Broker says "not reachable"

The daemon isn't running (it doesn't survive a reboot unless restarted) or the
port isn't forwarded. Re-deploy:

```bash
adb shell "CLASSPATH=/data/local/tmp/avatr-hdb-broker.jar \
    app_process64 /system/bin AvatrHdbBroker 38787" &
adb forward tcp:38787 tcp:38787
printf 'PING\n' | nc 127.0.0.1 38787      # expect RESULT 0 …
```

See [`../broker/README.md`](../broker/README.md).
