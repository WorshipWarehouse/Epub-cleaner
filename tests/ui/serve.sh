#!/usr/bin/env bash
# Playwright's webServer entry point: run the real app.
set -euo pipefail

cd "$(dirname "$0")/../.."

export EPUB_PURIFIER_HOST=127.0.0.1
export EPUB_PURIFIER_PORT="${EPUB_PURIFIER_UI_PORT:-8791}"

exec .venv/bin/epub-purifier-web
