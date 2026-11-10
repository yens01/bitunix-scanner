"""Telegram paper events, clearly labeled and never representing live fills."""
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

def format_event(e):
    if e.get("type") == "paper_open":
        return "🧪 模擬開倉（非真實委託）\n幣種：" + e.get("symbol","") + "\n訊號：" + e.get("signal_id","")
    if e.get("type") == "close":
        return ("🧪 模擬平倉（非真實成交）\n訊號：" + e.get("signal_id","")
                + "\n原因：" + e.get("reason","") + "\n估算淨損益："
                + str(e.get("estimated_net_pnl_usdt")) + " USDT")
    return None

def main():
    path=Path("paper_events_pending.json")
    if not path.exists():
        return
    events=json.loads(path.read_text(encoding="utf-8"))
    if not events:
        return
    token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
    if not token or not chat:
        raise RuntimeError("Telegram credentials missing; events remain in snapshot")
    for e in events:
        msg=format_event(e)
        if not msg:
            continue
        payload=urllib.parse.urlencode({"chat_id":chat,"text":msg}).encode()
        request=urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",data=payload,method="POST")
        with urllib.request.urlopen(request,timeout=20) as response:
            data=json.load(response)
            if not data.get("ok"):
                raise RuntimeError("Telegram API rejected message")
        print("Sent paper event:",e.get("type"),e.get("signal_id"))

if __name__=="__main__":
    main()
