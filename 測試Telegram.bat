@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo 讀取 telegram.local.env 並測試推播...
python -c "import os; from bitunix_scanner import load_local_env, _telegram_send; load_local_env(); t=os.environ.get('TELEGRAM_BOT_TOKEN',''); c=os.environ.get('TELEGRAM_CHAT_ID',''); print('TOKEN:', '已設定' if t else '尚未填'); print('CHAT_ID:', '已設定' if c else '尚未填'); ok=_telegram_send('\U0001f680 <b>Bitunix \u6383\u63cf\u5668</b>\nTelegram \u683c\u5f0f\u6e96\u5099\u5c31\u7dd2\n\u6709 YES / \u7b49\u56de\u6e2c\u6703\u63a8\u5361\u7247\u8a0a\u606f'); print('\u63a8\u64ad\u6210\u529f' if ok else '\u63a8\u64ad\u5931\u6557\uff1a\u8acb\u78ba\u8a8d\u5df2\u5c0d Bot \u6309 Start\uff0c\u4e14 TOKEN/CHAT_ID \u6b63\u78ba')"
pause
