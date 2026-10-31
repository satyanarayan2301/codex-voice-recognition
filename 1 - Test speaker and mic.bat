@echo off
title voicecoder - test
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python is not installed.
  echo Install it from https://www.python.org/downloads/  and tick "Add python.exe to PATH".
  echo Then double-click this file again.
  pause
  exit /b
)
echo Installing voice libraries (first time only)...
python -m pip install --quiet --disable-pip-version-check SpeechRecognition sounddevice numpy
python voicecoder.py test
echo.
pause
