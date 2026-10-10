@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
REM Telegram：請編輯同資料夾的 telegram.local.env 填入 TOKEN 與 CHAT_ID
python bitunix_scanner.py
pause
