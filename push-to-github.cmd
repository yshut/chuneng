@echo off
chcp 65001 >nul
cd /d "%~dp0"

where git >nul 2>&1
if errorlevel 1 (
  echo 未找到 git，请先安装 Git for Windows。
  pause
  exit /b 1
)

if not exist .git (
  git init
)

git add -A
git status

echo.
set /p OK=是否提交并推送到 origin main? (Y/N): 
if /i not "%OK%"=="Y" exit /b 0

git diff --cached --quiet
if errorlevel 1 (
  git commit -m "chore: initial import storage energy agent"
) else (
  echo 没有变更需要提交。
)

git remote get-url origin >nul 2>&1
if errorlevel 1 (
  git remote add origin https://github.com/yshut/chuneng.git
) else (
  git remote set-url origin https://github.com/yshut/chuneng.git
)
git branch -M main
echo.
echo 即将执行: git push -u origin main
echo 若提示登录，请使用 GitHub 账号或 Personal Access Token。
git push -u origin main

pause
