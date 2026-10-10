@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 手機對 Bot 傳幣種（如 BTC）會立刻回覆；Ctrl+C 停止
python bitunix_scanner.py bot
pause
