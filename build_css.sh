#!/usr/bin/env bash
# Build Tailwind CSS from templates. Run on the host before docker compose up.
#
# Usage:
#   ./build_css.sh           # one-shot minified build
#   ./build_css.sh --watch   # dev mode — rebuild on template changes

set -euo pipefail

INPUT="static/css/input.css"
OUTPUT="static/css/tailwind.css"

if [[ "${1:-}" == "--watch" ]]; then
    echo "Watching for changes..."
    tailwindcss -i "$INPUT" -o "$OUTPUT" --watch
else
    tailwindcss -i "$INPUT" -o "$OUTPUT" --minify
    echo "Built $OUTPUT ($(wc -c < "$OUTPUT" | tr -d ' ') bytes)"
fi
