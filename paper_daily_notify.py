"""Send one daily paper-trade digest; simulated results only."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paper_performance import report


def daily_message(state, day):
    # Trades are attributed to the UTC day they were OPENED.
    all_report = report(state)
    trades = [t for t in all_report["trades"]
              if (t.get("opened_at") or "").startswith(day)]
    wins = sum(t["estimated_net_pnl_usdt"] > 0 for t in trades)
    pnl = sum(t["estimated_net_pnl_usdt"] for t in trades)
    win_rate = f"{wins / len(trades) * 100:.1f}%" if trades else "—"
    return ("📊 模擬交易每日摘要（非真實成交）\n"
            f"UTC 日期：{day}\n"
            f"當日開倉且已平倉：{len(trades)} 筆\n"
            f"獲利筆數：{wins}\n"
            f"勝率：{win_rate}\n"
            f"估算淨損益：{pnl:+.6f} USDT\n"
            "⚠️ 依開倉日歸屬，跨日未平倉不計入；模擬成本與成交價非交易所驗證。")


def main():
    state = json.loads(Path("paper_state.json").read_text(encoding="utf-8"))
    day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    msg = daily_message(state, day)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode(),
        method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        if not json.load(response).get("ok"):
            raise RuntimeError("Telegram rejected daily digest")
    print("Sent paper daily digest for", day)


if __name__ == "__main__":
    main()
