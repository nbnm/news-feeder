#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "$0")/.." && pwd)"
source_image="$project_root/assets/branding/shiba-news-logo.png"
output_icon="$project_root/macos/Resources/AppIcon.icns"
if [ -f "$output_icon" ] && [ "$output_icon" -nt "$source_image" ]; then exit 0; fi
iconset="$project_root/dist/AppIcon.iconset"
mkdir -p "$iconset" "$project_root/macos/Resources"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$source_image" --out "$iconset/icon_${size}x${size}.png" >/dev/null
  retina_size=$((size * 2))
  sips -z "$retina_size" "$retina_size" "$source_image" --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil --convert icns --output "$output_icon" "$iconset"
