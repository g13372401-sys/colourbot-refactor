@echo off
setlocal
cd /d "%~dp0"

echo Starting debug overlay ONLY (no automation)...
python main.py --debug

echo.
echo Press any key to close this window...
pause >nul
