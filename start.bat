@echo off
chcp 65001 >nul
title Remote Assistant Bot
cd /d "%~dp0"
echo ============================================
echo   کنترل سیستم شخصی با بات تلگرام
echo   برای توقف، این پنجره را ببندید
echo ============================================
python -X utf8 bot.py
pause
