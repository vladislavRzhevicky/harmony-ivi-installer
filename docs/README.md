# Documentation

Method write-ups for side-loading APKs onto Huawei-automotive head units.
Start at the overview and follow the links.

| Doc | What it covers |
|---|---|
| [01-overview.md](01-overview.md) | The whole picture: why `adb install` fails, the two strategies, fan-out, permissions |
| [02-hdb-broker.md](02-hdb-broker.md) | HDB broker install - the HarmonyOS 5.0 path, two-stage recipe, gotchas |
| [03-pm-disable.md](03-pm-disable.md) | PM-disable install - the lighter primary strategy |
| [04-screens-and-users.md](04-screens-and-users.md) | Cabin displays ↔ Android users, and how to resolve the mapping |
| [05-runtime-permissions.md](05-runtime-permissions.md) | Granting runtime perms the shell `pm grant` can't |
| [06-compatibility.md](06-compatibility.md) | APK compatibility rules, failure codes, troubleshooting |

Related folders:

- [`../broker/`](../broker/) - the HDB broker daemon: source, wire protocol, build/deploy
- [`../ivi-installer/`](../ivi-installer/) - the desktop app that automates all of this
