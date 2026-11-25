#!/bin/sh
# Creates .venv in the repo root and installs dependencies for both panels.
# Re-running only installs what is missing. Pass --build to add pyinstaller.
set -e
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    "$PY" -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
fi
.venv/bin/python -m pip install --quiet \
    -r nanovna_panel/requirements.txt \
    -r tinysa_panel/requirements.txt
if [ "$1" = "--build" ]; then
    .venv/bin/python -m pip install --quiet pyinstaller
fi
