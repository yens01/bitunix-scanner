"""Push newly detected small-cap breakouts to Telegram; never places orders."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

COOLDOWN_SECONDS = 6 * 3600
ALERT_STAGES = {"爆發初期", "突破延續"}


def select_alerts(snapshot, state, *, now=None, max_alerts=3):
    now = now or datetime.now(timezone.utc).timestamp()
    last = dict(state.get("last_alert_at") or {})
    candidates = snapshot.get("candidates") or []
    selected = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", ""))
        if not symbol.endswith("USDT") or item.get("stage") not in ALERT_STAGES:
            continue
        if now - float(last.get(symbol, 0)) < COOLDOWN_SECONDS:
            continue
        if len(selected) >= max_alerts:
            break
        selected.append(item)
    return selected


def format_alert(item):
    symbol = item["symbol"]
    return (
        f"🚀 小幣爆發初期｜{symbol}\\n"
        f"階段：{item['stage']}｜雷達分數：{item.get('score', '—')}\\n"
        f"15m 已收盤價：{item.get('last_closed_price', '—')}\\n"
        f"突破壓力：{item.get('resistance', '—')}\\n"
        f"突破幅度：{item.get('breakout_pct', '—')}%\\n"
        f"成交量：{item.get('volume_ratio', '—')} 倍\\n"
        f"24h 漲幅：{item.get('change_24h_pct', '—')}%\\n"
        "⚠️ 僅供觀察，不是即時成交價或買入指令；請勿追高。"
    ).replace("\\\\n", "\\n")


def send_telegram(message, token, chat):
    payload = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise RuntimeError("Telegram rejected small-cap alert")


def deliver(snapshot, state, sender, *, now=None):
    now = now or datetime.now(timezone.utc).timestamp()
    state.setdefault("last_alert_at", {})
    for item in select_alerts(snapshot, state, now=now):
        sender(format_alert(item))
        # Write immediately after acknowledgement so later failures don't replay prior alerts.
        state["last_alert_at"][item["symbol"]] = now
        Path("radar_alert_state.json").write_text(json.dumps(state), encoding="utf-8")
        print(f"Small-cap alert delivered: {item['symbol']}", flush=True)
    state["last_alert_at"] = {
        sym: ts for sym, ts in state["last_alert_at"].items()
        if now - float(ts) < 2 * 86400
    }
    Path("radar_alert_state.json").write_text(json.dumps(state), encoding="utf-8")


def main():
    snapshot = json.loads(Path("small_cap_radar.json").read_text(encoding="utf-8"))
    state_path = Path("radar_alert_state.json")
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    else:
        # On first deployment, suppress existing candidates to avoid surprise alert bursts.
        prior = Path("previous_small_cap_radar.json")
        previous = json.loads(prior.read_text(encoding="utf-8")) if prior.exists() else {}
        now = datetime.now(timezone.utc).timestamp()
        state = {"last_alert_at": {
            c["symbol"]: now for c in previous.get("candidates", [])
            if isinstance(c, dict) and c.get("stage") in ALERT_STAGES and c.get("symbol")
        }}
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("Telegram credentials missing; no alerts acknowledged")
    deliver(snapshot, state, lambda msg: send_telegram(msg, token, chat))


if __name__ == "__main__":
    main()
