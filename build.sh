#!/bin/bash
# Builds Memwatch.app and installs it to /Applications.
set -euo pipefail
cd "$(dirname "$0")"
APP=build/Memwatch.app

/usr/bin/python3 memwatch.py --check >/dev/null
rm -rf build && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" build/icon.iconset
swiftc -O -o "$APP/Contents/MacOS/Memwatch" Memwatch.swift
cp memwatch.py "$APP/Contents/Resources/"
cp Info.plist "$APP/Contents/"

swift make_icon.swift build/icon.png
for s in 16 32 128 256 512; do
  sips -z $s $s build/icon.png --out "build/icon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) build/icon.png --out "build/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns build/icon.iconset -o "$APP/Contents/Resources/Memwatch.icns"

codesign --force --sign - "$APP"
pkill -x Memwatch || true
rm -rf /Applications/Memwatch.app
cp -R "$APP" /Applications/
echo "installed /Applications/Memwatch.app"
