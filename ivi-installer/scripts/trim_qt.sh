#!/usr/bin/env bash
# Strip unused Qt modules from the briefcase-built .app to shrink the
# bundle. Run between `briefcase build macOS` and `briefcase package macOS`.
#
# Pure-QtWidgets app — we keep QtCore, QtGui, QtWidgets, QtDBus, and the
# cocoa platform plugin. Everything else goes.
#
# Idempotent — re-running on a trimmed bundle is a no-op.

set -eu

APP="${1:-build/ivi_installer/macos/app/IVI Installer.app}"
QT_DIR="$APP/Contents/Resources/app_packages/PySide6/Qt"

if [[ ! -d "$QT_DIR" ]]; then
    echo "Qt dir not found: $QT_DIR" >&2
    echo "Did you run 'briefcase build macOS' first?" >&2
    exit 1
fi

echo "Trimming Qt frameworks in $APP..."
before=$(du -sm "$APP" | awk '{print $1}')

# Whole subtrees we never need (QML/Quick world, translations, build-time tools).
rm -rf "$QT_DIR/qml"
rm -rf "$QT_DIR/translations"
rm -rf "$QT_DIR/metatypes"
rm -rf "$QT_DIR/libexec"

# Frameworks. Keep: QtCore, QtGui, QtWidgets, QtDBus.
KEEP="QtCore.framework QtGui.framework QtWidgets.framework QtDBus.framework"
for fw in "$QT_DIR/lib/"*.framework; do
    name=$(basename "$fw")
    case " $KEEP " in
        *" $name "*) ;;            # keep
        *) rm -rf "$fw" ;;
    esac
done

# Plugins. Keep only platforms (cocoa/offscreen) and styles (macOS look).
PLUGINS_KEEP="platforms styles"
for plugin in "$QT_DIR/plugins/"*; do
    name=$(basename "$plugin")
    case " $PLUGINS_KEEP " in
        *" $name "*) ;;
        *) rm -rf "$plugin" ;;
    esac
done

# PySide6 Python bindings — drop wrappers for stripped modules so import
# attempts fail loudly rather than load a broken DLL.
PYSIDE_DIR="$APP/Contents/Resources/app_packages/PySide6"
for f in "$PYSIDE_DIR/"Qt*.so "$PYSIDE_DIR/"Qt*.abi3.so; do
    [[ -e "$f" ]] || continue
    base=$(basename "$f")
    case "$base" in
        QtCore.*|QtGui.*|QtWidgets.*|QtDBus.*) ;;     # keep
        *) rm -f "$f" ;;
    esac
done

after=$(du -sm "$APP" | awk '{print $1}')
echo "Trim complete: ${before}M → ${after}M"
