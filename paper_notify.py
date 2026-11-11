"""Telegram paper events, clearly labeled and never representing live fills."""
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

def _price(value):
    return f"{float(value):.10f}".rstrip("0").rstrip(".") if value is not None else "未提供"


def format_event(e):
    kind = e.get("type")
    if kind not in ("paper_open", "close"):
        return None
    side = {"long": "做多 LONG", "short": "做空 SHORT"}.get(e.get("side"), "未提供")
    symbol = e.get("symbol") or "未提供"
    lines = [
        "🧪 模擬開倉｜非真實委託" if kind == "paper_open" else "🧪 模擬平倉｜非真實成交",
        f"幣種：{symbol}",
        f"方向：{side}",
        f"進場價：{_price(e.get('entry'))} USDT",
        f"停損價：{_price(e.get('stop'))} USDT",
        f"停利價：{_price(e.get('tp'))} USDT",
    ]
    qty = e.get("quantity")
    entry = e.get("entry")
    if qty is not None:
        lines.append(f"模擬數量：{float(qty):,.6f} {symbol.removesuffix('USDT')}")
    if qty is not None and entry is not None:
        lines.append(f"倉位名目價值：約 {float(qty) * float(entry):.4f} USDT")
    if qty is not None and entry is not None and e.get("stop") is not None:
        risk = abs(float(entry) - float(e["stop"])) * float(qty)
        lines.append(f"預估停損損失（未含費用）：約 {risk:.4f} USDT")
    if kind == "close":
        lines.extend([
            f"平倉原因：{'停損 SL' if e.get('reason') == 'stop' else '停利 TP' if e.get('reason') == 'tp' else e.get('reason', '未提供')}",
            f"平倉價：{_price(e.get('exit'))} USDT",
            f"估算淨損益：{e.get('estimated_net_pnl_usdt', '未提供')} USDT",
        ])
    if e.get("opened_at"):
        lines.append(f"開倉時間（UTC）：{e['opened_at']}")
    lines.append(f"訊號 ID：{e.get('signal_id', '未提供')}")
    lines.append("⚠️ 僅供模擬紀錄，並非交易所成交證明")
    return "\n".join(lines)


def deliver(state, sender):
    """Deliver pending events; persist each successful acknowledgement.

    Telegram cannot guarantee exactly-once delivery after an ambiguous timeout.
    """
    from paper_trading import save_state
    path = Path("paper_state.json")
    for item in state.get("notification_outbox", []):
        if item.get("sent"):
            continue
        message = format_event(item.get("event", {}))
        if not message:
            item["sent"] = True
            save_state(state, path)
            continue
        sender(message)
        item["sent"] = True
        save_state(state, path)


def send_telegram(message, token, chat):
    payload = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
        if not data.get("ok"):
            raise RuntimeError("Telegram API rejected message")


def main():
    from paper_trading import load_state
    path = Path("paper_state.json")
    if not path.exists():
        raise RuntimeError("paper state unavailable; cannot deliver notifications")
    state = load_state(path)
    if not any(not item.get("sent") for item in state.get("notification_outbox", [])):
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("Telegram credentials missing; outbox remains pending")
    deliver(state, lambda message: send_telegram(message, token, chat))


if __name__ == "__main__":
    main()
