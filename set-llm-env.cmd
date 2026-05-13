@echo off
REM ============================================================
REM  One-shot helper to permanently set MIMO_API_KEY in your
REM  Windows USER environment. After running this, CLOSE ALL
REM  cmd windows and reopen one - then env var will be visible.
REM ============================================================
setlocal EnableExtensions

echo.
echo ============================================================
echo   Set MIMO_API_KEY in your USER environment (permanent)
echo ============================================================
echo.
echo This will store the key in Windows user profile.
echo To revoke later: open cmd and run `setx MIMO_API_KEY ""`.
echo.
set /p "KEY=Paste your API Key here and press Enter: "
if "%KEY%"=="" (
    echo [ERROR] Empty key. Aborted.
    pause
    exit /b 1
)

setx MIMO_API_KEY "%KEY%" >nul
if errorlevel 1 (
    echo [ERROR] setx failed.
    pause
    exit /b 1
)

echo.
echo [OK] MIMO_API_KEY has been saved to your user environment.
echo.
echo IMPORTANT: close ALL existing cmd windows and the running
echo            server, then reopen cmd and run start-web.cmd.
echo            Existing windows do NOT see the new env var.
echo.
pause
endlocal
