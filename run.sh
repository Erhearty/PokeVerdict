#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── dependency checks ──────────────────────────────────────────────────────────

missing=()

if ! python3 -c "import PIL" 2>/dev/null; then
    missing+=("pillow")
fi
if ! python3 -c "import numpy" 2>/dev/null; then
    missing+=("numpy")
fi
if ! python3 -c "import pytesseract" 2>/dev/null; then
    missing+=("pytesseract")
fi

if [[ ${#missing[@]} -gt 0 ]]; then
    echo "Missing Python packages: ${missing[*]}"
    echo "Install with:  pip install ${missing[*]}"
    exit 1
fi

if ! command -v tesseract &>/dev/null; then
    echo "tesseract-ocr binary not found."
    echo "Install with:  sudo dnf install tesseract   (Fedora)"
    echo "           or: sudo apt install tesseract-ocr   (Debian/Ubuntu)"
    exit 1
fi

if [[ ! -f gamedata.sqlite ]]; then
    echo "gamedata.sqlite not found — building it now..."
    python3 tools/build_gamedata.py --fetch
fi

# ── launch ─────────────────────────────────────────────────────────────────────

exec python3 tools/shortcut_server.py --collection collection.sqlite --rules rules.json "$@"
