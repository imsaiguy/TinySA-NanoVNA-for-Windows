@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller --quiet
pyinstaller --noconfirm --onefile --windowed --exclude-module pkg_resources --name NanoVNA_Panel vna_panel.py
echo.
echo Done: dist\NanoVNA_Panel.exe
pause
