#!/bin/sh
# Builds NanoVNA_Panel.app in the dist folder (unsigned).
set -e
cd "$(dirname "$0")"
../setup_venv.sh --build
../.venv/bin/pyinstaller --noconfirm --windowed --exclude-module pkg_resources \
    --name NanoVNA_Panel vna_panel.py
echo
echo "Done: dist/NanoVNA_Panel.app"
