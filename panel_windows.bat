@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python app.py %*
if errorlevel 1 (
    echo.
    echo The panel could not be started. See the message above.
    pause
)
