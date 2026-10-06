#!/bin/sh
# Packages ccwidget as a self-contained macOS app, with ccwatch.py bundled inside,
# and wraps it in a .zip and a .dmg for sharing.
#
# Usage: ./package.sh [version]          (version defaults to the VERSION file, e.g. 1.0.0)
# Output:
#   dist/ccwidget.app                    universal (Apple Silicon + Intel), macOS 13+
#   dist/ccwidget-<version>.zip
#   dist/ccwidget-<version>.dmg          drag-to-Applications disk image
#
# Signing is ad-hoc unless you set SIGN_ID to a "Developer ID Application: …"
# identity from your keychain; then setting NOTARY_PROFILE to a profile saved with
# `xcrun notarytool store-credentials` also notarizes and staples the .dmg.
set -e
cd "$(dirname "$0")"

version=${1:-$(cat VERSION)}
dist=dist
app=$dist/ccwidget.app
res=$app/Contents/Resources
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

python3 -B -c 'import ast, sys; ast.parse(open(sys.argv[1]).read())' ccwatch.py  # fail early on a broken script

rm -rf "$app" "$dist/ccwidget-$version.zip" "$dist/ccwidget-$version.dmg"
mkdir -p "$dist"
APP="$app" VERSION="$version" ARCHS="arm64 x86_64" ./build-widget.sh

# The app runs the bundled copy (see Feed.locateScript in ccwidget.swift).
mkdir -p "$res"
cp ccwatch.py ccwatch-hook.sh README.md "$res/"
chmod +x "$res/ccwatch.py" "$res/ccwatch-hook.sh"

"$app/Contents/MacOS/ccwidget" --iconset "$tmp/AppIcon.iconset"
iconutil -c icns "$tmp/AppIcon.iconset" -o "$res/AppIcon.icns"

if [ -n "$SIGN_ID" ]; then
  # Hardened runtime needs the Apple Events entitlement for the focus/dashboard AppleScript.
  cat >"$tmp/entitlements.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.automation.apple-events</key><true/>
</dict>
</plist>
PLIST
  codesign --force --options runtime --timestamp --entitlements "$tmp/entitlements.plist" --sign "$SIGN_ID" "$app"
else
  codesign --force --sign - "$app"
fi
codesign --verify --strict "$app"

# Instructions for whoever receives the app. Unless it's signed with a Developer ID
# and notarized, macOS blocks the first launch of a downloaded copy.
mkdir "$tmp/share"
cp -R "$app" "$tmp/share/"
if [ -z "$NOTARY_PROFILE" ]; then
  cat >"$tmp/share/Open Me First.txt" <<'TXT'
ccwidget: a menu bar monitor for Claude Code sessions
=====================================================

1. Drag ccwidget.app into your Applications folder.

2. Open it. The first time, macOS says "Apple could not verify ccwidget is
   free of malware". This happens because the app isn't notarized by Apple.
   It is not damaged. Click "Done" (NOT "Move to Trash").

3. Allow it once, using either A or B:

   A. Open System Settings > Privacy & Security, scroll down to the
      "ccwidget was blocked..." message and click "Open Anyway". Enter
      your password, then click "Open Anyway" again.

   B. In Terminal, run:
          xattr -dr com.apple.quarantine /Applications/ccwidget.app
      then open the app normally.

   After that it opens like any other app. Look for the robot icon in the menu bar.

Requirements: macOS 13 or later, Python 3 (if macOS offers to install
the "command line developer tools" the first time, accept, then reopen
ccwidget), and Claude Code.

When you click a session to jump to its window, macOS asks once for
permission to control your terminal or editor. Click OK.
TXT
fi
mv "$tmp/share" "$tmp/ccwidget-$version"  # the zip unpacks into a ccwidget-<version> folder
ditto -c -k --keepParent "$tmp/ccwidget-$version" "$dist/ccwidget-$version.zip"
mv "$tmp/ccwidget-$version" "$tmp/share"

mkdir "$tmp/dmg"
cp -R "$tmp/share/." "$tmp/dmg/"
ln -s /Applications "$tmp/dmg/Applications"
hdiutil create -quiet -volname "ccwidget $version" -srcfolder "$tmp/dmg" -fs HFS+ -format UDZO "$dist/ccwidget-$version.dmg"

if [ -n "$SIGN_ID" ] && [ -n "$NOTARY_PROFILE" ]; then
  codesign --force --timestamp --sign "$SIGN_ID" "$dist/ccwidget-$version.dmg"
  xcrun notarytool submit "$dist/ccwidget-$version.dmg" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$dist/ccwidget-$version.dmg"
fi

echo "packaged ccwidget $version:"
ls -lh "$dist" | sed 1d
