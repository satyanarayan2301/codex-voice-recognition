@echo off
title Push voicecoder to GitHub
cd /d "%~dp0"
if not exist ".git" git init -b main
git config user.name >nul 2>&1 || git config user.name "satu08"
git config user.email >nul 2>&1 || git config user.email "satu08@users.noreply.github.com"
git add .gitignore README.md requirements.txt voicecoder.py test_voicecoder.py "0 - Install Claude Code.bat" "1 - Test speaker and mic.bat" "2 - Start voice Codex.bat" "3 - Start voice Claude.bat" "4 - Log in to Claude.bat" "Push to GitHub.bat"
git commit -m "Add voicecoder: hands-free voice companion for Codex CLI and Claude Code" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" -m "Claude-Session: https://claude.ai/code/session_013pCi2TfDY2wymGLRzsTV5c"
git remote remove origin >nul 2>&1
git remote add origin https://github.com/satu08/codex-voice-recognition.git
git branch -M main
git push -u origin main
echo.
if errorlevel 1 (echo PUSH FAILED - copy the message above into the chat.) else (echo Pushed! Open https://github.com/satu08/codex-voice-recognition)
pause
