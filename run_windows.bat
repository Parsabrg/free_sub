@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python main.py --mode auto --publish
exit /b %ERRORLEVEL%
