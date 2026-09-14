@echo off
setlocal
cd /d "%~dp0"

echo Starting Colour-bot...
python main.py --route route2

echo.
echo Press any key to close this window...
pause >nul