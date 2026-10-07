@echo off
setlocal
cd /d "%~dp0"
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
python run_web_api.py
pause