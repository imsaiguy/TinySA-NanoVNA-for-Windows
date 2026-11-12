@echo off
REM Builds a single tinySA_Panel.exe in the dist folder.
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller --quiet
pyinstaller --noconfirm --onefile --windowed --exclude-module pkg_resources --name tinySA_Panel tinysa_panel.py
echo.
echo Done: dist\tinySA_Panel.exe
pause
