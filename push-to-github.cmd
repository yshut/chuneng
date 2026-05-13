@echo off
REM ============================================================
REM  Chuneng AGENT - One-click push to GitHub (ASCII-only)
REM  Usage: double-click, or run from cmd at project root.
REM  This script does:
REM    1) Sanity check: no secrets / runtime data will be staged
REM    2) git add . / git status preview
REM    3) Ask for confirmation
REM    4) git commit + git push origin main
REM ============================================================

setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================================
echo  Step 1/5  Checking repository state
echo ============================================================
git rev-parse --is-inside-work-tree >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Not inside a git repository.
    pause
    exit /b 1
)

for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%B"
echo Current branch: !BRANCH!
git remote -v
echo.

echo ============================================================
echo  Step 2/5  Safety scan - looking for files that must NOT be uploaded
echo ============================================================
set "FOUND_BAD="
git ls-files --others --cached --exclude-standard | findstr /R /I "llm_config\.json data[/\\] \.env \.env\..* secrets" > "%TEMP%\chuneng_bad.txt"
if exist "%TEMP%\chuneng_bad.txt" (
    for %%S in ("%TEMP%\chuneng_bad.txt") do if %%~zS GTR 0 set "FOUND_BAD=1"
)

if defined FOUND_BAD (
    echo.
    echo [ERROR] The following files look sensitive and would be uploaded:
    type "%TEMP%\chuneng_bad.txt"
    echo.
    echo Add them to .gitignore first, then re-run this script.
    del "%TEMP%\chuneng_bad.txt" >nul 2>nul
    pause
    exit /b 1
)
del "%TEMP%\chuneng_bad.txt" >nul 2>nul
echo OK - no sensitive files detected.
echo.

echo ============================================================
echo  Step 3/5  Staging changes (git add .)
echo ============================================================
git add .
echo.
echo --- git status preview (will be committed) ---
git status --short
echo --- end of preview ---
echo.

echo ============================================================
echo  Step 4/5  Ready to commit
echo ============================================================
set /p "MSG=Commit message (press Enter for default): "
if "!MSG!"=="" set "MSG=feat: AI capacity review + LLM config UX fixes + launcher improvements"

echo.
echo Commit message: "!MSG!"
echo Target: origin/!BRANCH!
echo.
set /p "GO=Proceed with commit + push? [y/N]: "
if /I not "!GO!"=="y" (
    echo Aborted by user. Nothing was committed.
    pause
    exit /b 0
)

echo.
echo ============================================================
echo  Step 5/5  Commit and push
echo ============================================================
git commit -m "!MSG!"
if errorlevel 1 (
    echo.
    echo [ERROR] Commit failed. Possible reasons:
    echo    - Nothing to commit ^(everything already up to date^)
    echo    - Git hook rejected the commit
    pause
    exit /b 1
)

git push origin !BRANCH!
if errorlevel 1 (
    echo.
    echo [ERROR] Push failed. Possible reasons:
    echo    - Authentication: provide a Personal Access Token as the password.
    echo      Generate one at https://github.com/settings/tokens with 'repo' scope.
    echo    - Network: check your internet connection.
    echo    - Remote diverged: run 'git pull --rebase' first, then re-run this script.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Done! Your changes are on GitHub.
echo  Repo: https://github.com/yshut/chuneng
echo ============================================================
pause
endlocal
