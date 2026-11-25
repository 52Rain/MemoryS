#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
npm run build
bundle="build.noindex/MemoryS.app"
rm -rf "$bundle"
mkdir -p "$bundle/Contents/"{MacOS,Resources/ui,Resources/service}
swiftc -O native/App.swift -o "$bundle/Contents/MacOS/MemoryS" -framework AppKit -framework WebKit -framework Security
cp dist/app.js dist/app.css public/index.html "$bundle/Contents/Resources/ui/"
cp service/*.py ../runtime/config.py ../runtime/memory_reader.py ../runtime/memory_store.py ../runtime/service_library.py ../runtime/service_backend.py "$bundle/Contents/Resources/service/"
cp -R ../runtime/demo_vault "$bundle/Contents/Resources/service/"
cat > "$bundle/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.memorys.desktop</string>
<key>CFBundleName</key><string>MemoryS</string><key>CFBundleDisplayName</key><string>MemoryS</string>
<key>CFBundleExecutable</key><string>MemoryS</string><key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string><key>CFBundleVersion</key><string>1</string>
<key>NSHighResolutionCapable</key><true/><key>LSMinimumSystemVersion</key><string>13.0</string>
</dict></plist>
PLIST
mkdir -p build.noindex/MemoryS.iconset
swift native/Icon.swift build.noindex/icon-source.png
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" build.noindex/icon-source.png --out "build.noindex/MemoryS.iconset/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" build.noindex/icon-source.png --out "build.noindex/MemoryS.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns build.noindex/MemoryS.iconset -o "$bundle/Contents/Resources/MemoryS.icns"
/usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string MemoryS.icns" "$bundle/Contents/Info.plist"
codesign --force --sign - "$bundle"
codesign --verify --strict "$bundle"
echo "Built $bundle"
if [ "${INSTALL_TO_APPLICATIONS:-0}" = "1" ] && [ -d "$HOME/Applications" ]; then
  rm -rf "$HOME/Applications/MemoryS.app"
  ditto "$bundle" "$HOME/Applications/MemoryS.app"
  echo "Installed to $HOME/Applications/MemoryS.app"
fi
