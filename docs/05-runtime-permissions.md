# Runtime permissions

After an app installs, it still can't use the mic, camera, location, or
storage until its runtime permissions are granted. On these head units the
obvious command **silently does nothing**:

```bash
adb shell pm grant <pkg> android.permission.RECORD_AUDIO
# exit code 0, but dumpsys still shows granted=false
```

Huawei's policy hook intercepts the `pm` / `cmd` **shell commands** and drops
the grant on the floor while returning success. The binder API underneath is
*not* hooked - so the fix is to call it directly.

Reference: [`../broker/perm-grant/HwPermGrant.java`](../broker/perm-grant/HwPermGrant.java)
and [`../ivi-installer/ivi_installer/runtime_perms.py`](../ivi-installer/ivi_installer/runtime_perms.py).

## The fix: a reflection helper

A ~2.5 KB Java helper runs under `app_process` (same trick as the broker:
shell-uid process, system framework on the classpath) and calls:

```java
IPackageManager.grantRuntimePermission(pkg, permission, userId)
```

directly, via reflection. Because it hits the binder method rather than the
`pm` shell front-end, the hook never sees it.

```bash
adb push hw-perm-grant.jar /data/local/tmp/ivi-perm-grant.jar

adb shell "CLASSPATH=/data/local/tmp/ivi-perm-grant.jar \
    app_process /system/bin HwPermGrant <pkg> <userId> <perm1> [perm2 …]"
```

It supports both the 3-arg and 4-arg `grantRuntimePermission` signatures
(they differ across Android versions), grants each requested permission, and
prints one `granted: <perm>` / `failed <perm>: <error>` line per permission plus
a final `summary: ok=N fail=N`.

## Which permissions, for which users

You need the grant **per user** - the same fan-out logic as installs (each
cabin screen is its own Android user; see
[`04-screens-and-users.md`](04-screens-and-users.md)).

The app auto-discovers what to grant by parsing the per-user
`runtime permissions:` section of `dumpsys package <pkg>` and selecting the ones
still `granted=false`, then runs one `HwPermGrant` invocation per user.
Confirmed-working permissions include:

```
POST_NOTIFICATIONS   ACCESS_FINE_LOCATION   ACCESS_COARSE_LOCATION
RECORD_AUDIO         CAMERA                 READ_PHONE_STATE
READ_EXTERNAL_STORAGE  WRITE_EXTERNAL_STORAGE
READ_CONTACTS        GET_ACCOUNTS           BLUETOOTH_CONNECT
```

## Build the helper

`HwPermGrant.java` is clean, original source (unlike the reverse-engineered
broker) and compiles directly:

```bash
cd ../broker/perm-grant
./build.sh        # javac → d8 → jar
# or, by hand:
javac -classpath "$ANDROID_JAR" HwPermGrant.java
d8 --output . HwPermGrant.class
jar cf hw-perm-grant.jar classes.dex
```

`$ANDROID_JAR` is any `platforms/android-31+/android.jar` from the Android SDK.

## Note

`pm install -g` (used by the [pm-disable strategy](03-pm-disable.md)) grants all
runtime permissions *at install time*, which covers many apps without this
helper. The helper is for the cases where `-g` wasn't used (e.g. the broker
path) or where specific per-user grants are still missing after install.
