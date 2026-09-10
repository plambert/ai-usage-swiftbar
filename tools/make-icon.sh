#!/bin/bash
# Rasterize assets/<name>.svg to a 16 pt @2x template image and print the
# base64 to paste into ICON_B64 in the matching plugin.
#
# Usage: tools/make-icon.sh openrouter-glyph | tools/make-icon.sh claude-glyph
#
# The output is a TIFF, not a PNG: SwiftBar decodes the base64 with
# NSImage(data:), which ignores a PNG's DPI and would draw the pixels as
# points. TIFF resolution tags are honored, so 144 DPI yields 16 pt tall on
# a Retina display.
#
# Requires: rsvg-convert (librsvg) and magick (imagemagick) from Homebrew.
set -euo pipefail
name=${1:?usage: make-icon.sh <asset-name>}
root=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp)
rsvg-convert -h 32 "$root/assets/$name.svg" -o "$tmp"
magick "$tmp" -strip -density 144 -units PixelsPerInch -compress zip \
    "$root/assets/$name@2x.tiff"
rm -f "$tmp"
base64 -i "$root/assets/$name@2x.tiff" | tr -d '\n'
echo
