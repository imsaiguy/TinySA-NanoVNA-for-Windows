#!/bin/sh
set -e
cd "$(dirname "$0")"
../setup_venv.sh
exec ../.venv/bin/python vna_panel.py --sim
