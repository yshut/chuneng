@echo off
REM ============================================================
REM  Chuneng AGENT - Web UI launcher (ASCII-only for CMD safety)
REM  Provider: MiMo (OpenAI compatible)
REM ============================================================

setlocal

cd /d "%~dp0"

REM ---- Data directory (avoids the Linux default /var/lib/chuneng-agent) ----
set "CHUNENG_DATA_ROOT=%~dp0data"

REM ---- LLM credentials (optional at startup) ----
REM You can either:
REM   1) Set MIMO_API_KEY before running this script (recommended for daily use):
REM         set MIMO_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx
REM      or add MIMO_API_KEY in Windows System Properties > Environment Variables.
REM   2) Skip this step and configure the API Key in the browser UI later.
REM      The server will still start; the LLM features will activate after you
REM      enter the key via the gear icon in the top bar.
set "KEY_PRESENT=yes"
if "%MIMO_API_KEY%"=="" set "KEY_PRESENT=no"

REM Optional override; falls back to the official default when empty.
if "%MIMO_BASE_URL%"=="" set "MIMO_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1"

REM ---- Port (change if 7860 is taken) ----
set "PORT=7860"

REM ---- Pick Python launcher ----
set "PYTHON_CMD="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3"
if "%PYTHON_CMD%"=="" (
    python --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if "%PYTHON_CMD%"=="" (
    echo.
    echo [ERROR] Python not found. Install Python 3.10+ and add it to PATH.
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Chuneng AGENT - Web UI
echo ============================================================
echo   Data dir:   %CHUNENG_DATA_ROOT%
echo   API base:   %MIMO_BASE_URL%
echo   Model:      mimo-v2.5-pro
echo   Port:       %PORT%
echo   API Key:    %KEY_PRESENT% (env: MIMO_API_KEY)
echo ============================================================
if "%KEY_PRESENT%"=="no" (
    echo.
    echo [INFO] MIMO_API_KEY is empty.
    echo        The server will start anyway. Open the browser and click
    echo        the gear icon in the top bar to enter your API Key.
)
echo.
echo Starting... browser will open in a few seconds.
echo Close this window or press Ctrl+C to stop the server.
echo.

REM ---- Open browser after 4s so uvicorn has time to bind ----
start "" /b cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:%PORT%/"

REM ---- Launch the web server ----
%PYTHON_CMD% main.py --web --port %PORT% --llm-provider mimo --llm-model mimo-v2.5-pro

echo.
echo ============================================================
echo  Server stopped. If you saw any error above, screenshot it.
echo ============================================================
pause
endlocal
