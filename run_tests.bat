@echo off
setlocal
cd /d "%~dp0"

if not exist "webenv\Scripts\python.exe" (
    echo [ERROR] webenv Python was not found. Create the web environment first.
    pause
    exit /b 2
)

"webenv\Scripts\python.exe" -c "import fastapi, httpx, pandas, requests"
if errorlevel 1 (
    echo [ERROR] Test dependencies are missing. Run:
    echo webenv\Scripts\python.exe -m pip install -r requirements-test.txt
    pause
    exit /b 2
)

"webenv\Scripts\python.exe" -m unittest discover -s tests -v
set "SYSTEMTRADING_TEST_EXIT=%ERRORLEVEL%"
pause
exit /b %SYSTEMTRADING_TEST_EXIT%
