@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt --quiet
start "" pythonw vna_panel.py %*
