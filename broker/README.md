# The HDB broker

This folder contains the on-device daemon that makes APK installs succeed on
**HarmonyOS Cockpit / HwSAPT** head units (Deepal S09, Avatr, …), where a plain
`adb install` is rejected by Huawei's installer policy with:

```
INSTALL_FAILED_ABORTED: User rejected permissions
```

It also contains the small reflection helper that grants runtime permissions
PMS otherwise blocks.

> **Provenance & honesty.** The broker (`AvatrHdbBroker` /
> `HuaweiShellBridge`) is **reverse-engineered** - these `.java` files are
> `jadx` decompiler output from a third-party tool's `app_process` jar, not
> hand-written source. They are published here as a *reference* so the method
> is auditable and reproducible. Class/field names like `codex-session-...`
> are the original author's, preserved verbatim. `HwPermGrant.java` in
> [`perm-grant/`](perm-grant/) is original, hand-written source.

```
broker/
├── README.md                    ← this file
├── avatr-hdb-broker.jar         ← prebuilt daemon (~23 KB), ready to push
├── src/                         ← decompiled daemon source (reference)
│   ├── AvatrHdbBroker.java       - TCP listener + base64 wire protocol
│   ├── HuaweiShellBridge.java    - reflection into IPackageManager / IHwPackageManager
│   ├── HdbKeySetter.java         - standalone setHdbKey() tool
│   └── AvatrHdbBroker$$ExternalSyntheticBackport0.java
└── perm-grant/
    ├── HwPermGrant.java          - runtime-permission grant via reflection (original)
    ├── hw-perm-grant.jar         - prebuilt (~2.5 KB)
    └── build.sh                  - javac → d8 → jar
```

---

## Why a daemon at all?

On these head units `adbd` runs as `uid=2000 (shell)`. That UID can't talk to
Huawei's hidden package-manager APIs directly, and the stock `pm install` path
is intercepted by the `HwInstallPolicy` hook, which pops (and auto-rejects) an
"external sources" confirmation that has no UI on a car.

The trick: **`app_process`**. Android's `app_process` launcher can run an
arbitrary `main()` class *with the full system framework on the classpath*.
A shell-uid process started that way can use **reflection** to reach
`IPackageManager` / `IHwPackageManager` and create an install session that is
**flagged as HDB-initiated** - a flag the policy hook trusts and waves through
without any dialog.

So the broker is a tiny long-lived `app_process` server: you talk to it over a
loopback TCP socket, it does the privileged reflection on your behalf, and it
streams the result back.

```
host (adb)  ──adb forward tcp:38787──►  127.0.0.1:38787 on the car
                                         │
                                         ▼
                            app_process64 AvatrHdbBroker 38787
                            (uid=shell, system framework on classpath)
                                         │  reflection
                                         ▼
                            IPackageManager / IHwPackageManager
                            → HDB-flagged install session → PMS
```

---

## How the broker is deployed and started

The jar is pushed to the device's world-writable tmp dir and launched with the
system framework on the classpath. No root, no `su`.

```bash
SERIAL=<adb-serial>          # from `adb devices`
PORT=38787

# 1. push the daemon
adb -s "$SERIAL" push avatr-hdb-broker.jar /data/local/tmp/avatr-hdb-broker.jar

# 2. start it (backgrounded; survives the adb shell exiting via nohup/setsid
#    in practice - here shown foreground-detached)
adb -s "$SERIAL" shell "CLASSPATH=/data/local/tmp/avatr-hdb-broker.jar \
    app_process64 /system/bin AvatrHdbBroker $PORT" &

# 3. forward the loopback port to the host
adb -s "$SERIAL" forward tcp:$PORT tcp:$PORT

# 4. health check
printf 'PING\n' | nc 127.0.0.1 $PORT      # → RESULT 0 cG9uZwo=   (base64 "pong")
```

`AvatrHdbBroker.main()` binds `ServerSocket(port, 50, 127.0.0.1)` - loopback
only, so nothing off-device can reach it - prints
`AvatrHdbBroker listening tcp=127.0.0.1:<port>`, and serves one command per
connection forever.

The IVI Installer app automates exactly this (`app_process_helper` /
`redeploy_broker`): it pushes the jar, starts the daemon, forwards the port,
and pings until it answers.

---

## Wire protocol

Loopback TCP. **One command per connection**, connection closed after the
reply. All arguments are individually Base64-encoded (Android `Base64`
flag `10` = `NO_PADDING | NO_WRAP`), space-separated.

| Command | Format | Response |
|---|---|---|
| Ping | `PING\n` | `RESULT 0 <b64("pong\n")>\n` |
| Stage a file | `STAGE <b64-relpath> <bytes>\n<raw bytes>` | `STAGED <b64-abspath> <bytes>\n` |
| Run the bridge | `RUN <b64-arg1> <b64-arg2> …\n` | `RESULT <exit> <b64-output>\n` |

- `RESULT <exit> <payload>` - `exit` is `0` on success; `payload` is base64 of
  the captured stdout+stderr of the reflected call (plus any stack trace).
- `STAGE` writes into `/data/local/tmp/avatr-hdb-stage/<relpath>` with strict
  path-traversal guards (no leading `/`, no `..`, canonical-path containment).
  Useful when the host has no shell access; if you have shell, just
  `adb push` straight to `/data/local/tmp/` instead.
- `RUN` decodes its args and calls `HuaweiShellBridge.main(args)` with stdout
  and stderr redirected into the captured buffer (see `runBridge()` in
  `AvatrHdbBroker.java`). The first arg is the **sub-command**.

A minimal Python client for the `RUN` path:

```python
import socket, base64

def b64(s): return base64.b64encode(s.encode()).decode()

def run_bridge(*args, host="127.0.0.1", port=38787, timeout=180):
    s = socket.create_connection((host, port), timeout=timeout)
    s.sendall(f"RUN {' '.join(b64(a) for a in args)}\n".encode())
    buf = b""
    while chunk := s.recv(65536):
        buf += chunk
    s.close()
    tag, code, payload = buf.decode().rstrip("\n").split(" ", 2)
    return int(code), base64.b64decode(payload).decode("utf-8", "replace")
```

---

## What `HuaweiShellBridge` actually does

`HuaweiShellBridge.main(args)` dispatches on `args[0]`. The sub-command that
works on HarmonyOS 5.0 is **`hdb-session-install-user`**. Its job, in three
moves:

1. **Set an HDB key.** `setHdbKey(nonce)` via reflection on the Huawei PM
   service, and embed `SHA-256("nonce=path1 path2 …")` into the session's
   hidden `hdbEncode` field. The actual nonce is arbitrary - it just has to be
   self-consistent within one call.
2. **Create an install session with the HDB flag set.**
   `installFlags |= 262144` (`LEGACY_INSTALL_FLAG_HDB`) on the
   `SessionParams`, plus `installerPackageName` set to the OEM installer.
   That flag is what the `HwInstallPolicy` hook trusts.
3. **Write the APK into the session and commit.** PMS validates the
   signature/scan once, exactly as for a normal install - **no platform
   re-signing needed**; the HDB flag bypasses the *external-source* dialog,
   not the signature check.

Key constants in `HuaweiShellBridge.java`:

```java
LEGACY_INSTALL_FLAG_HDB           = 262144   // the magic flag
DEFAULT_HDB_SESSION_INSTALLER_PACKAGE = "com.huawei.appmarket.vehicle"  // Avatr default
```

> ⚠️ **The default installer package is the Avatr one.** On **Deepal** the
> installer package is `com.huawei.appinstaller.car`. If you let the bridge
> fall back to its `com.huawei.appmarket.vehicle` default on a Deepal, the
> commit fails with `INSTALL_FAILED_INTERNAL_ERROR` (PMS can't resolve a
> non-existent package as the installer). **Always pass the right installer
> package explicitly.** The app auto-detects it via
> `firmware.detect()` → `pm path` probe.

### The `hdb-session-install-user` argument vector

```
hdb-session-install-user
  <packageName | "-">        # "-" = auto-resolve from the APK
  <apk-path-on-device>       # e.g. /data/local/tmp/app.apk
  <userId>                   # seed user (use 0 = system; see fan-out below)
  <hdbKey-nonce>             # any unique string
  <installerPackage>         # com.huawei.appinstaller.car (Deepal) / .appmarket.vehicle (Avatr)
  <originatingUid>           # 1000 (system)
```

Other sub-commands exist but do **not** work on 5.0 and are documented as
dead-ends in [`../docs/02-hdb-broker.md`](../docs/02-hdb-broker.md)
(`scan-install`, `legacy-session-install-user`, and the `…-multi` variant that
hardcodes the Avatr installer).

---

## `HdbKeySetter`

A standalone one-shot: `app_process … HdbKeySetter <key>` just calls
`setHdbKey(key)` and exits. The daemon doesn't need it (the bridge sets the key
itself); it's kept as a minimal reproduction of the single privileged call the
whole bypass hinges on.

---

## Build the broker from source

The jar here is prebuilt and ready to push. To rebuild from the decompiled
sources (note: decompiler output may need light fix-ups to recompile cleanly -
the `$$ExternalSyntheticBackport0` shim is a d8-desugaring artifact):

```bash
# Requires a JDK, Android SDK build-tools (d8), and an android.jar (API 31+).
javac -classpath "$ANDROID_JAR" -d out src/*.java
d8 --output . out/defpackage/*.class
jar cf avatr-hdb-broker.jar classes.dex
```

## Build the permission-grant helper

`HwPermGrant.java` is clean, original source and recompiles directly:

```bash
cd perm-grant
./build.sh           # javac → d8 → jar  (edit the output path inside as needed)
```

It calls `IPackageManager.grantRuntimePermission(pkg, perm, userId)` via
reflection - see [`../docs/05-runtime-permissions.md`](../docs/05-runtime-permissions.md)
for why `pm grant` from the shell is silently swallowed and this isn't.

---

## Cleanup

Everything the broker touches lives under `/data/local/tmp/`. To remove it:

```bash
adb shell pkill -f AvatrHdbBroker
adb forward --remove tcp:38787
adb shell rm -f /data/local/tmp/avatr-hdb-broker.jar \
                /data/local/tmp/avatr-hdb-broker.log \
                '/data/local/tmp/iviinstaller_*.apk'
adb shell rm -rf /data/local/tmp/avatr-hdb-stage/
```

Kill the daemon **before** unlinking the jar - a still-mmap'd jar stays on disk
until the process exits. The app's `cleanup.cleanup_installer_footprint` does
this in the right order.
