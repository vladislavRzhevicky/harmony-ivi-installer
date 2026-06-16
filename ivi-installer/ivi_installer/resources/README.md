# Bundled resources

The app loads these via `importlib.resources`. Two of them are **shipped**
in this repo because they are small and central to the install method; the
rest are **optional drop-ins** you add yourself.

## Shipped

| File | Size | Used by | Notes |
|---|---:|---|---|
| `avatr-hdb-broker.jar` | ~23 KB | `strategies.hdb_broker_install` | The on-device HDB bypass daemon. Reverse-engineered reference artifact - source in [`../../../broker/`](../../../broker/). |
| `hw-perm-grant.jar` | ~2.5 KB | `runtime_perms` | Reflection helper that grants runtime permissions PMS otherwise blocks. Built from `HwPermGrant.java`. |
| `HwPermGrant.java` | ~2.5 KB | (source) | Source for `hw-perm-grant.jar`; rebuild steps in [`../../../broker/README.md`](../../../broker/README.md). |
| `extras.json` | ~29 KB | `catalog.load_extras` | Curated Store-tab seed catalog (no binaries, just metadata). |

## Optional drop-ins (NOT in this repo)

These are excluded for size / copyright reasons. The app runs without them -
the relevant tab simply degrades. Drop them in if you want the full feature
set or a packaged (`briefcase`) build.

| Path | Where to get it | Needed for |
|---|---|---|
| `celia-keyboard-*.apk` | `adb pull /system/app/Celia*/<apk>` from a Huawei IVI head unit | Keyboards tab (bundled Celia IME) |
| `platform-tools/darwin/adb` | Extract `platform-tools-latest-darwin.zip` from Google | Bundled adb on macOS (otherwise auto-downloaded on first launch) |
| `platform-tools/windows/{adb.exe, AdbWinApi.dll, AdbWinUsbApi.dll}` | Extract `platform-tools-latest-windows.zip` from Google | Bundled adb on Windows |

At runtime, `adb.ensure_adb()` downloads platform-tools into
`~/.ivi-installer/platform-tools/` on first launch, so a missing bundled
`adb` is not fatal when running from source.
