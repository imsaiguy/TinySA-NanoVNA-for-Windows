#!/bin/sh
# Builds tinySA_Panel.app in the dist folder (unsigned).
set -e
cd "$(dirname "$0")"
../setup_venv.sh --build
../.venv/bin/pyinstaller --noconfirm --windowed --exclude-module pkg_resources \
    --name tinySA_Panel tinysa_panel.py
echo
echo "Done: dist/tinySA_Panel.app"
