#!/bin/sh
# First run creates ../.venv and installs dependencies, then launches the panel.
set -e
cd "$(dirname "$0")"
../setup_venv.sh
exec ../.venv/bin/python tinysa_panel.py "$@"
