@echo off
setlocal
cd /d "%~dp0"
set "SYSTEMTRADING_PAPER_DB=%CD%\paper_breakout.db"
set "SYSTEMTRADING_PAPER_TICK_CSV=%CD%\ticks\breakout.csv"
py -3.9-32 main.py
pause