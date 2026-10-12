#!/bin/bash
# 字幕ととのえの完了通知アプリ（SubtitlePolishNotifier.app）を、このMacで組み立てる。
# 起動ファイルが初回に自動で呼ぶ。Xcode のコマンドラインツール（clang）が必要。
# 作り方は ChronoFlow の build_notifier_app.command と同じ（ad-hoc 署名、Dockに出さない）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$SCRIPT_DIR/SubtitlePolishNotifier.app"
ICON="$SCRIPT_DIR/../icon/icon.icns"
# 通知の許可はこのIDに付く。変えると、利用者がもう一度許可する必要がある
BUNDLE_ID="com.luluapps.subtitlepolish.notifier"
ARCH="$(uname -m)"

command -v clang >/dev/null 2>&1 || { echo "clang がないため、通知アプリを作れませんでした（通知は使わずに動きます）。" >&2; exit 1; }
/bin/rm -rf "$APP"
/bin/mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
/usr/bin/clang -arch "$ARCH" -fobjc-arc -mmacosx-version-min=11.0 \
  -framework Foundation -framework AppKit -framework UserNotifications \
  "$SCRIPT_DIR/SubtitlePolishNotifier.m" -o "$APP/Contents/MacOS/SubtitlePolishNotifier"
[ -f "$ICON" ] && /bin/cp "$ICON" "$APP/Contents/Resources/AppIcon.icns"
/bin/cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>字幕ととのえ</string>
  <key>CFBundleDisplayName</key><string>字幕ととのえ</string>
  <key>CFBundleExecutable</key><string>SubtitlePolishNotifier</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict>
</plist>
PLIST
/usr/bin/codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f -R -trusted "$APP" >/dev/null 2>&1 || true
echo "通知アプリを作りました: $APP"
