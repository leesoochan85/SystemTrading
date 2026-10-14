@echo off
setlocal
cd /d "%~dp0"
set "DATA_PYTHON=%USERPROFILE%\anaconda3\envs\marketdata\python.exe"
if not exist "%DATA_PYTHON%" (
    echo [ERROR] marketdata Python was not found:
    echo %DATA_PYTHON%
    exit /b 2
)
if not exist "bootstrap_market_history.py" (
    echo [ERROR] Put this BAT in the SystemTrading project root.
    exit /b 2
)
if /I "%~1"=="history" goto history
if /I "%~1"=="value" goto value
if "%~1"=="" goto history
 echo Usage: update_market_data.bat [history^|value]
 exit /b 2
:history
echo [1/2] Updating daily price history...
"%DATA_PYTHON%" bootstrap_market_history.py --lookback-days 400 --min-history-rows 180 --sleep 1
if errorlevel 1 goto failed
if /I "%~1"=="history" goto stats
:value
echo [2/2] Collecting financial snapshots for all supported stocks...
"%DATA_PYTHON%" collect_value_fundamentals.py --limit 0
if errorlevel 1 goto failed
:stats
echo [STATUS] Financial DB status:
"%DATA_PYTHON%" collect_value_fundamentals.py --stats
if errorlevel 1 goto failed
echo [DONE] Review collection summaries before starting main.py.
echo [NOTE] Successful process exit does not guarantee every stock was collected.
exit /b 0
:failed
set "DATA_UPDATE_EXIT=%ERRORLEVEL%"
echo [STOPPED] A collection process failed. Review the output above.
exit /b %DATA_UPDATE_EXIT%
