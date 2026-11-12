@echo off
REM First run installs dependencies, then launches the panel.
cd /d "%~dp0"
python -m pip install -r requirements.txt --quiet
start "" pythonw tinysa_panel.py %*
