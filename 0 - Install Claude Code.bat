@echo off
title Install Claude Code
echo Installing Claude Code with the official installer...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
echo.
echo ---- Checking the install ----
if exist "%USERPROFILE%\.local\bin\claude.exe" (
  "%USERPROFILE%\.local\bin\claude.exe" --version
  echo.
  echo Installed. Next: double-click "4 - Log in to Claude.bat" once to sign in.
) else (
  echo Could not find claude.exe yet. Close this window, open a NEW one, and run:  claude --version
)
echo.
pause
