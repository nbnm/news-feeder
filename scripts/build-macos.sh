#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
configuration="${1:-debug}"
bash "$project_root/scripts/generate-app-icon.sh"
cd "$project_root/macos"
export CLANG_MODULE_CACHE_PATH="$PWD/.cache/clang"
export SWIFT_MODULECACHE_PATH="$PWD/.cache/swift"
swift build -c "$configuration" --cache-path .cache --config-path .swiftpm/config --security-path .swiftpm/security
binary_path="$(swift build -c "$configuration" --show-bin-path --cache-path .cache --config-path .swiftpm/config --security-path .swiftpm/security)"
app_path="$project_root/dist/News Feeder.app"
mkdir -p "$app_path/Contents/MacOS"
mkdir -p "$app_path/Contents/Resources"
chmod -R u+w "$app_path"
cp "$binary_path/NewsFeeder" "$app_path/Contents/MacOS/NewsFeeder"
cp "$project_root/macos/Info.plist" "$app_path/Contents/Info.plist"
cp "$project_root/macos/Resources/AppIcon.icns" "$app_path/Contents/Resources/AppIcon.icns"
for resource in "$binary_path"/*.bundle; do
  if [ -d "$resource" ]; then cp -R "$resource" "$app_path/Contents/Resources/"; fi
done
chmod -R u+w "$app_path"
codesign --force --sign - "$app_path"
printf 'Built %s\n' "$app_path"
