#!/usr/bin/env bash
# Build a macOS .icns bundle from a single high-res PNG.
#
# Briefcase 0.4.x for the macOS-app target expects a pre-built .icns
# file next to the project root (e.g. icon.icns when pyproject has
# `icon = "icon"`). It does NOT auto-convert from PNG. This script
# fills that gap using macOS's built-in `sips` and `iconutil`.
#
# Idempotent: regenerates icon.icns from icon.png every run.
#
# Usage:
#   scripts/make_icns.sh           # uses icon.png → icon.icns
#   scripts/make_icns.sh path.png  # source PNG override

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR/.."

SRC="${1:-icon.png}"
DST="${SRC%.png}.icns"
ICONSET="${SRC%.png}.iconset"

if [[ ! -f "$SRC" ]]; then
    echo "Source PNG not found: $SRC" >&2
    exit 1
fi

# Sanity-check sources: square + at least 512px (Apple recommends 1024+
# for crisp Retina rendering). Don't error on smaller — just warn.
read -r W H < <(sips -g pixelWidth -g pixelHeight "$SRC" \
                | awk '/pixelWidth/{w=$2} /pixelHeight/{h=$2} END{print w, h}')
if [[ "$W" != "$H" ]]; then
    echo "WARNING: $SRC is $Wx$H — macOS expects a square icon" >&2
fi
if (( W < 512 )); then
    echo "WARNING: $SRC is only ${W}px wide — Retina sizes will look soft" >&2
fi

rm -rf "$ICONSET" "$DST"
mkdir -p "$ICONSET"

# Standard Apple .icns sizes — base + @2x for each.
sips -z   16   16 "$SRC" --out "$ICONSET/icon_16x16.png"        > /dev/null
sips -z   32   32 "$SRC" --out "$ICONSET/icon_16x16@2x.png"     > /dev/null
sips -z   32   32 "$SRC" --out "$ICONSET/icon_32x32.png"        > /dev/null
sips -z   64   64 "$SRC" --out "$ICONSET/icon_32x32@2x.png"     > /dev/null
sips -z  128  128 "$SRC" --out "$ICONSET/icon_128x128.png"      > /dev/null
sips -z  256  256 "$SRC" --out "$ICONSET/icon_128x128@2x.png"   > /dev/null
sips -z  256  256 "$SRC" --out "$ICONSET/icon_256x256.png"      > /dev/null
sips -z  512  512 "$SRC" --out "$ICONSET/icon_256x256@2x.png"   > /dev/null
sips -z  512  512 "$SRC" --out "$ICONSET/icon_512x512.png"      > /dev/null
sips -z 1024 1024 "$SRC" --out "$ICONSET/icon_512x512@2x.png"   > /dev/null

iconutil -c icns "$ICONSET" -o "$DST"
rm -rf "$ICONSET"

echo "✓ $DST  ($(du -sh "$DST" | awk '{print $1}'))"
