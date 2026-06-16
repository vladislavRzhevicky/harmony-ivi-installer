#!/usr/bin/env bash
# Build IVI Installer for macOS end-to-end:
#
#   1. briefcase update macOS    — sync source into the bundle
#   2. briefcase build macOS     — link the .app, ad-hoc sign
#   3. scripts/trim_qt.sh        — strip unused Qt modules (~110 MB savings)
#   4. briefcase package macOS   — wrap into a .dmg
#
# Usage:
#   scripts/build.sh             # full pipeline
#   scripts/build.sh --skip-trim # for debugging the untrimmed bundle
#
# Run from the ivi-installer/ directory or anywhere — the script cd's
# into its repo root.

set -euo pipefail

SKIP_TRIM=0
for arg in "$@"; do
    case "$arg" in
        --skip-trim) SKIP_TRIM=1 ;;
        -h|--help)
            sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Anchor at the ivi-installer/ root regardless of where we were invoked.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR/.."

VERSION=$(python -c "import ivi_installer; print(ivi_installer.__version__)")
echo "▶ IVI Installer build pipeline (version $VERSION)"
echo

# Refresh icon.icns from icon.png so a swapped-out PNG flows into the
# .app without needing a separate `briefcase create` reset. Skipped
# silently when the source PNG isn't there.
if [[ -f icon.png ]]; then
    echo "── 0/4: regenerate icon.icns ──"
    bash scripts/make_icns.sh icon.png
    echo
fi

echo "── 1/4: briefcase update macOS ──"
# `--update-resources` forces briefcase to re-copy the icon (and other
# bundle resources). Without it, an icon swap silently does nothing on
# subsequent builds and you ship the previous icon.
briefcase update macOS --update-resources
echo

echo "── 2/4: briefcase build macOS ──"
briefcase build macOS
echo

if [[ $SKIP_TRIM -eq 1 ]]; then
    echo "── 3/4: SKIPPED (--skip-trim) ──"
else
    echo "── 3/4: trim Qt frameworks ──"
    bash scripts/trim_qt.sh
fi
echo

echo "── 4/4: briefcase package macOS --adhoc-sign ──"
# Remove any prior dmg of the same version so briefcase doesn't refuse
# to overwrite or end up with a stale signature.
rm -f "dist/IVI Installer-$VERSION.dmg"
briefcase package macOS --adhoc-sign
echo

DMG="dist/IVI Installer-$VERSION.dmg"
APP="build/ivi_installer/macos/app/IVI Installer.app"
echo "✓ Done."
echo "  .app: $(du -sh "$APP" | awk '{print $1}')  $APP"
echo "  .dmg: $(du -sh "$DMG" | awk '{print $1}')  $DMG"
