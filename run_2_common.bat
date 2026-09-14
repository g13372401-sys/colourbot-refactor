@echo off
setlocal
cd /d "%~dp0"

echo Starting Colour-bot...
python main.py --route route2 --start common

echo.
echo Press any key to close this window...
pause >nul