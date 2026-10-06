#!/bin/sh
# Builds ccwidget.app (the floating desktop widget) next to ccwatch.py.
# Usage: ./build-widget.sh            then: open ccwidget.app
#
# This dev build runs the ccwatch.py beside it, so edits to ccwatch.py only need
# the widget restarted. For a self-contained app with ccwatch.py inside, use
# ./package.sh, which calls this script with:
#   APP=<path>        where to build the app (default: ccwidget.app)
#   VERSION=<x.y.z>   CFBundleShortVersionString / CFBundleVersion (default: the VERSION file)
#   ARCHS="arm64 x86_64"   build a universal binary (default: this Mac's architecture)
#
# The About window shows the author and the avatar:
#   AUTHOR_NAME, AUTHOR_EMAIL   from the environment, else author.conf, else git config
#   avatar.png                  in this folder, copied into the app if present
set -e
cd "$(dirname "$0")"

app=${APP:-ccwidget.app}
version=${VERSION:-$(cat VERSION 2>/dev/null || echo 1.0.0)}
env_name=$AUTHOR_NAME env_email=$AUTHOR_EMAIL
[ -f author.conf ] && . ./author.conf
author=${env_name:-${AUTHOR_NAME:-$(git config user.name 2>/dev/null || id -F)}}
email=${env_email:-${AUTHOR_EMAIL:-$(git config user.email 2>/dev/null || true)}}
xml() { printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }
bin="$app/Contents/MacOS/ccwidget"
mkdir -p "$app/Contents/MacOS"
if [ -n "$ARCHS" ]; then
  tmp=$(mktemp -d)
  for arch in $ARCHS; do
    swiftc -O -swift-version 5 -parse-as-library -target "$arch-apple-macos13.0" ccwidget.swift -o "$tmp/ccwidget-$arch"
  done
  lipo -create "$tmp"/ccwidget-* -output "$bin"
  rm -rf "$tmp"
else
  swiftc -O -swift-version 5 -parse-as-library ccwidget.swift -o "$bin"
fi

cat >"$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>ccwidget</string>
  <key>CFBundleIdentifier</key><string>local.ccwatch.widget</string>
  <key>CFBundleExecutable</key><string>ccwidget</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>CCAuthorName</key><string>$(xml "$author")</string>
  <key>CCAuthorEmail</key><string>$(xml "$email")</string>
  <key>NSHumanReadableCopyright</key><string>© $(date +%Y) $(xml "$author")</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Opens the full ccwatch dashboard and brings the terminal or editor running a Claude session to the front.</string>
</dict>
</plist>
PLIST

if [ -f avatar.png ]; then
  mkdir -p "$app/Contents/Resources"
  cp avatar.png "$app/Contents/Resources/"
fi

codesign --force --sign - "$app" 2>/dev/null || true
echo "built $(pwd)/$app"
