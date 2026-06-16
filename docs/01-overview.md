# Overview - installing APKs on Huawei-automotive head units

This is the conceptual map. Read it first; the other docs go deep on each
piece.

## The problem

Most Chinese EVs with a **HarmonyOS Cockpit** head unit (Deepal, Avatr, Aito,
Luxeed, and other HIMA / Huawei-stack brands) run **AOSP underneath**. So they
*can* run ordinary Android APKs - but you can't just `adb install` one. The
attempt is rejected:

```
adb install app.apk
  → INSTALL_FAILED_ABORTED: User rejected permissions
```

Two things are in the way:

1. **`adbd` runs as `uid=2000 (shell)`** - no root, no `su`, and it can't reach
   Huawei's hidden package-manager APIs directly.
2. **Huawei's `HwInstallPolicy` hook** intercepts the install and demands an
   "external sources" confirmation. On a car there's no sane UI for that dialog,
   so it auto-rejects.

`adb root` is dead on these production builds and there is no `su`. So every
approach here is **non-root**, working within shell-uid constraints.

## Two install strategies

The tooling cascades two independent strategies. Either can be primary; the
app tries the lighter one first and falls back.

### 1. PM-disable install (primary, lighter)

Temporarily disable the component that shows the rejection dialog
(`com.android.packageinstaller`) for the active user, run a normal
`pm install` that names the OEM installer package, then re-enable the
component. No daemon, no jar, no reflection.

→ [`03-pm-disable.md`](03-pm-disable.md)

### 2. HDB broker install (HarmonyOS 5.0 path)

When PM-disable isn't enough (some HarmonySpace 5.0 firmwares), route the
install through a tiny on-device daemon (`AvatrHdbBroker`) launched via
`app_process` with the system framework on its classpath. It uses reflection to
create an install session **flagged as HDB-initiated** (`installFlags |=
262144`) - a flag the policy hook trusts and lets through with no dialog.

→ [`02-hdb-broker.md`](02-hdb-broker.md) and the source in
[`../broker/`](../broker/)

> There is also a **non-Huawei path**: some head units (e.g. Deepal S07/S05 on
> Changan's own OS) have no `HwInstallPolicy` and take a plain
> `adb install -r -d -t`. That's not what this repo's broker is for - it's the
> easy case.

## After the install: two more steps

A successful install on the seed user is not enough on a multi-screen car.

### Fan-out to every cabin screen

The cabin has up to four displays (driver / passenger / rear / HUD), and on
these head units **each physical screen is bound to its own Android user**. An
app only appears on a screen if it's installed *for that screen's user*. So
after seeding the package once, you register it for the other users with the
stock, unprivileged command:

```bash
pm install-existing --user <N> <package>
```

Resolving which user maps to which screen is firmware-specific and must be
probed, not hardcoded. → [`04-screens-and-users.md`](04-screens-and-users.md)

### Grant runtime permissions

`pm grant` from the shell is silently swallowed by the policy hook - the
command returns success but nothing changes. Without this step, apps install
but can't use the mic, camera, location, or storage. The fix is a 2.5 KB helper
that calls `IPackageManager.grantRuntimePermission()` via reflection, bypassing
the shell-level hook. → [`05-runtime-permissions.md`](05-runtime-permissions.md)

## End-to-end flow

```
┌─ host (laptop) ────────────┐        ┌─ car head unit (shell uid) ─────────┐
│ 1. adb devices             │        │                                      │
│ 2. detect OS / installer   │◄──────►│ getprop / pm path / dumpsys          │
│ 3. resolve screen users    │        │                                      │
│ 4. install:                │        │                                      │
│    a. pm-disable  ──────────────────►│ disable packageinstaller → pm install│
│       (or)                  │        │                                      │
│    b. hdb broker  ──────────────────►│ app_process AvatrHdbBroker → session │
│ 5. fan out to screens ──────────────►│ pm install-existing --user N         │
│ 6. grant runtime perms ─────────────►│ app_process HwPermGrant …            │
│ 7. verify per-user state ───────────►│ dumpsys package <pkg>                │
└────────────────────────────┘        └──────────────────────────────────────┘
```

The reference implementation of all of this is
[`../ivi-installer/ivi_installer/strategies.py`](../ivi-installer/ivi_installer/strategies.py)
(the full local cascade) and `installer.py` (the public entry point).

## Ground rules

- **Read-only by default.** Probe before you mutate. Everything in step 1-3 is
  read-only.
- **No `/system` writes, ever.** Every on-device change goes through `pm` /
  `cmd` / the in-process broker - never `mount -o rw,remount`.
- **Physical access only.** None of this works remotely; it needs USB and a
  dealer-unlocked ADB on a car you own or are authorized to service. See
  [`../DISCLAIMER.md`](../DISCLAIMER.md).
