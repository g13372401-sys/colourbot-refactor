@echo off
cd /d "%~dp0"
python flag_checker.py --verbose %*
pause