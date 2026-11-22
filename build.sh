#!/usr/bin/env sh
# Build dist/Kanade/Kanade with PyInstaller on Linux or macOS (no Python needed to run the result).
# FFmpeg is not bundled: install it with your package manager (Kanade finds it on PATH).
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python -m PyInstaller --noconfirm --clean kanade.spec
echo
echo "Built: dist/Kanade/Kanade"
