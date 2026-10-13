@echo off
setlocal
cd /d "%~dp0"

set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"

start "SystemTrading React" /D "%CD%\web_frontend" cmd /k "npm run dev"

"%CD%\webenv\Scripts\python.exe" run_web_api.py

pause