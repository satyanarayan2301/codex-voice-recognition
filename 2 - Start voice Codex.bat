@echo off
title voicecoder - Codex
cd /d "%~dp0"
echo Drag your project folder into this window and press Enter
set /p PROJ=(or just press Enter to use this folder): 
if "%PROJ%"=="" set PROJ="%~dp0."
python voicecoder.py loop --agent codex --cwd %PROJ%
pause
