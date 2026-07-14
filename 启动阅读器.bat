@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
title Novel Reader - Ctrl+C to stop
cd /d "%~dp0"
python scripts/maintenance/reader_server.py --open-browser
echo.
echo Server stopped. Press any key to close.
pause >nul
