#!/usr/bin/env bash
# Compile HwPermGrant.java → classes.dex → hw-perm-grant.jar.
#
# Output is dropped at:
#   ivi_installer/resources/hw-perm-grant.jar
#
# Run from the repo root or from tools/hw-perm-grant/ — either works.
#
# Requirements:
#   - JDK (Android Studio's bundled JBR works fine)
#   - Android SDK build-tools 34+ (for d8)
#   - Android SDK platforms/android-31+ (for android.jar)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC_DIR="$REPO_ROOT/ivi_installer/resources"
SRC_JAVA="$SRC_DIR/HwPermGrant.java"
OUT_JAR="$SRC_DIR/hw-perm-grant.jar"

if [[ -z "${JAVA_HOME:-}" ]]; then
    if [[ -d "/Applications/Android Studio.app/Contents/jbr/Contents/Home" ]]; then
        export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
    fi
fi
PATH="${JAVA_HOME:+$JAVA_HOME/bin:}$PATH"

ANDROID_JAR="$(ls -1 ~/Library/Android/sdk/platforms/android-3*/android.jar 2>/dev/null | sort -V | tail -1)"
if [[ -z "$ANDROID_JAR" ]]; then
    echo "no android.jar found under ~/Library/Android/sdk/platforms/" >&2
    exit 1
fi
D8="$(ls -1 ~/Library/Android/sdk/build-tools/3*.0.0/d8 2>/dev/null | sort -V | tail -1)"
if [[ -z "$D8" ]]; then
    echo "d8 not found under ~/Library/Android/sdk/build-tools/" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cp "$SRC_JAVA" "$WORK/"
cd "$WORK"
javac -source 11 -target 11 -classpath "$ANDROID_JAR" HwPermGrant.java
"$D8" --output . HwPermGrant.class
jar cf "$OUT_JAR" classes.dex
echo "wrote $OUT_JAR ($(wc -c < "$OUT_JAR") bytes)"
