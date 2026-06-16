# PM-disable install (primary strategy)

The lighter of the two strategies. No daemon, no jar push, no reflection - just
three `pm` commands. It's the primary because it's simpler and works on most
builds; the [HDB broker](02-hdb-broker.md) is the fallback for the
HarmonySpace 5.0 firmwares where this isn't enough.

Reference implementation:
[`../ivi-installer/ivi_installer/strategies.py`](../ivi-installer/ivi_installer/strategies.py)
(`_strategy_pm_disable_install`).

## The idea

The thing that blocks a normal `adb install` is the component that renders the
"external sources" rejection dialog: **`com.android.packageinstaller`**. If you
temporarily disable that component for the active user, a normal `pm install`
that names the OEM installer package goes through. Then you re-enable the
component so the system is exactly as you found it.

## The sequence

```bash
SERIAL=<adb-serial>
PI=com.android.packageinstaller
INSTALLER=com.huawei.appinstaller.car      # Deepal (Avatr: com.huawei.appmarket.vehicle)
PI_USER=<active driver user>               # resolved, not hardcoded - see below

# 1. push the apk
adb -s "$SERIAL" push app.apk /data/local/tmp/iviinstaller_app.apk

# 2. disable the dialog component for the active user
adb -s "$SERIAL" shell pm disable-user --user "$PI_USER" "$PI"

# 3. install, naming the OEM installer, WITHOUT --user / -t
adb -s "$SERIAL" shell pm install -r -d -g -i "$INSTALLER" \
    /data/local/tmp/iviinstaller_app.apk

# 4. ALWAYS re-enable, even if step 3 failed (do this in a finally)
adb -s "$SERIAL" shell pm enable --user "$PI_USER" "$PI"
```

`pm install` flags used:

| Flag | Meaning |
|---|---|
| `-r` | replace an existing install |
| `-d` | allow version downgrade (closes the dominant `INSTALL_FAILED_VERSION_DOWNGRADE`) |
| `-g` | grant all runtime permissions at install time |
| `-i <pkg>` | set the installer package (must be the OEM installer) |

After a successful seed, **fan out** to the other cabin screens with
`pm install-existing --user N` exactly as in the broker path
([`02-hdb-broker.md`](02-hdb-broker.md) stage 2).

## Why these exact choices (hard-won)

- **Per-user disable on the active driver user, not user 0.** On Huawei IVIs
  user 0 is a headless system user; disabling its PackageInstaller and
  installing with `--user 0 -t` left the dialog hook live on the *real* user
  and still bounced. The working form is: disable on `max(running non-zero
  user id)`, `pm install` **without** `--user` / `-t`, symmetric re-enable.
- **No global `pm disable` fallback.** The global form is strictly more
  dangerous (it disables the component for *every* user, including ones you
  didn't resolve) and on builds where per-user disable fails, the global form
  fails too - so there's no upside. If disable fails, abort cleanly and let the
  cascade fall through to the broker.
- **Always re-enable in a `finally`.** If the process dies mid-install with
  PackageInstaller still disabled, the user can't install anything through the
  normal UI until it's re-enabled. The re-enable must run even on crash/abort.

## Resolving the user to disable

`_resolve_packageinstaller_user` picks the active driver user - in practice the
`max` of the running non-zero user ids. **Don't hardcode it.** Different cars
expose the driver on user 10, 12, 13, or higher. See
[`04-screens-and-users.md`](04-screens-and-users.md) for the full resolution
chain.

## When it fails → broker

If `pm disable-user` is rejected, or `pm install` still returns
`INSTALL_FAILED_ABORTED` after the disable (some HarmonySpace 5.0 builds keep a
second policy gate), the strategy aborts cleanly and the cascade moves on to the
[HDB broker](02-hdb-broker.md). Terminal codes (a genuinely broken APK -
`INSTALL_FAILED_INVALID_APK`, `INSTALL_PARSE_FAILED_*`, insufficient storage)
stop the cascade entirely; there's no point trying the broker for those.
