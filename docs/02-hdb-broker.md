# HDB broker install (HarmonyOS 5.0 path)

The deep dive on the broker strategy. For the daemon's source, wire protocol,
and build steps see [`../broker/`](../broker/); this doc is the *method* - how
an install actually lands.

Reference firmware for everything below: a Deepal S09 (board `ALSK-D587`,
HwSAPT, HarmonySpace 5.0, Android 12 / SDK 31). The same approach applies to
Avatr and other HarmonyOS-Cockpit head units; only the installer package name
changes.

## The one idea

Huawei's `HwInstallPolicy` hook lets an install through **without a dialog** if
the install session is flagged as **HDB-initiated**:

```
installFlags |= 262144      // LEGACY_INSTALL_FLAG_HDB
```

Everything the broker does is in service of setting that flag (plus the hidden
`hdbEncode` / `hdbArgs` companion fields and a matching `setHdbKey()` nonce) on
a real `PackageInstaller` session, then committing the APK into it. PMS still
validates the APK signature and scans it exactly as normal - the HDB flag
bypasses the *external-source confirmation*, **not** the signature check.

## Why a daemon

`adbd` is `uid=2000 (shell)` and can't reach `IHwPackageManager` directly. But
`app_process` can launch an arbitrary `main()` with the **system framework on
the classpath**, and a shell-uid process launched that way can reflect into the
hidden APIs. The broker is that process, kept alive as a loopback TCP server so
the host can drive it. See [`../broker/README.md`](../broker/README.md) for
deploy + protocol.

## The recipe - a two-stage install

The HDB-flagged broker install is **global**: it overwrites the package
globally and resets the per-user `installed` flags. So you do **not** loop the
broker across users. You seed once, then fan out with the stock command.

### Stage 1 - seed the package (one broker call)

```bash
SERIAL=<adb-serial>
adb -s "$SERIAL" forward tcp:38787 tcp:38787
adb -s "$SERIAL" push app.apk /data/local/tmp/iviinstaller_app.apk
```

Then one `RUN hdb-session-install-user …` over `127.0.0.1:38787`:

```
hdb-session-install-user
  -                                     # packageName: "-" = auto-resolve from APK
  /data/local/tmp/iviinstaller_app.apk
  0                                     # seed via user 0 (system) - see fan-out
  ivi-app-seed-<nonce>                  # arbitrary unique HDB key
  com.huawei.appinstaller.car           # Deepal installer pkg (Avatr: com.huawei.appmarket.vehicle)
  1000                                  # originatingUid = system
```

A successful run prints a trace ending in:

```
legacyTrace step=commitResult detail=status=0 message=INSTALL_SUCCEEDED
hdbSessionInstallUser ok sessionId=<id>
true
```

The installed package ends up with
`installerPackageName=com.huawei.appinstaller.car`.

### Stage 2 - fan out to every cabin-screen user

```bash
for user in $(cabin_screen_users); do
  adb -s "$SERIAL" shell pm install-existing --user "$user" <package.name>
done
```

`pm install-existing` is stock Android: it registers an already-installed APK
from `/data/app/…` for an additional user. **No broker, no HDB flag, no policy
hook** - the APK was already validated in stage 1. It's fast (no copy) and safe
(doesn't disturb other users). Don't hardcode the user list - resolve it per
car (see [`04-screens-and-users.md`](04-screens-and-users.md)).

### Stage 3 - grant runtime permissions

`pm grant` is silently intercepted; use the reflection helper instead. See
[`05-runtime-permissions.md`](05-runtime-permissions.md).

A reference Python implementation of all three stages is in
[`../broker/README.md`](../broker/README.md) (client) and
[`../ivi-installer/ivi_installer/strategies.py`](../ivi-installer/ivi_installer/strategies.py)
(full cascade with parsing + retries).

## Gotchas (each of these has cost someone a session)

### The installer package is brand-specific

The bridge's built-in default is the **Avatr** package
`com.huawei.appmarket.vehicle`. On **Deepal** the real one is
`com.huawei.appinstaller.car`. Pass the right one explicitly - a wrong/absent
installer package fails the commit with `INSTALL_FAILED_INTERNAL_ERROR`. The
app auto-detects it with a `pm path` probe (`firmware.detect()`).

### Only the HDB sub-command works on 5.0

Three bridge sub-commands look like they should install; only one does:

| Sub-command | Result | Why |
|---|---|---|
| `scan-install <apk>` | `false` | `scanInstallApk()` only allows whitelisted APKs |
| `legacy-session-install-user …` | `INSTALL_FAILED_INTERNAL_ERROR: Session relinquished` | no HDB marker → policy revokes the session at commit |
| `hdb-session-install-user … <key> …` | `INSTALL_SUCCEEDED` | sets the HDB flag + hidden fields → policy trusts it |

### The HDB key is just a nonce

`setHdbKey(key)` + `SHA-256("key=path…")` in `hdbEncode`. Any string works as
long as it's consistent within one call. It is **not** a credential.

### Broker installs are global - never loop them across users

A second broker install (even into a different user) resets the first user's
`installed=false`. Seed once via the broker, fan out with `pm install-existing`.
Verify per-user state with:

```bash
adb shell dumpsys package <pkg> | grep -E "User [0-9]+:.*installed="
# and, more precisely:
adb shell pm path --user <N> <pkg>
```

### Signatures don't need re-signing

Confirmed across real apps signed with their own developer certs (Yandex, etc.)
and even the Android test key. The HDB flag bypasses the external-source
dialog, not the signature whitelist - no platform re-signing is required.

### `.xapk` / split APKs

The `…-multi` bridge sub-command hardcodes the Avatr installer package, so it
fails on Deepal. Workarounds, easiest first:

1. Grab a **single universal APK** instead of the `.xapk` (APKMirror/ApkPure
   often have one).
2. Install only `base.apk` via the single-APK path (works when the splits are
   non-critical; may crash if they carry required native libs).
3. Patch the broker jar to use the correct installer package and run it on a
   second port.

This tooling rejects `.xapk` inputs at the boundary for exactly this reason
(`build_context_from_path` raises on non-`.apk`).

### Standard compatibility still applies

HDB does not bypass Android compatibility. Pre-check on the host:

```bash
aapt2 dump badging app.apk | head
```

- `minSdkVersion ≤ 31` (the car is Android 12)
- ABI includes `arm64-v8a` (or is ABI-agnostic)
- old `targetSdk` is fine (just strict-mode warnings)

See [`06-compatibility.md`](06-compatibility.md) for the full checklist.

## Provenance note

The broker is reverse-engineered from a third-party tool's `app_process` jar
(decompiled source in [`../broker/src/`](../broker/src/)). It is published as a
reference so the method is auditable. The bypass hinges on a single privileged
reflection call (`setHdbKey` + an HDB-flagged session); `HdbKeySetter.java` is
the minimal reproduction of just that call.
