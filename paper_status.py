"""Read-only Telegram paper positions report from the cloud-journal snapshot."""
from __future__ import annotations
import html
import json
import urllib.request
from datetime import datetime, timezone

SNAPSHOT_URL = "https://raw.githubusercontent.com/yens01/bitunix-scanner/cloud-journal/cloud/paper_state.json"

def fetch_snapshot(url=SNAPSHOT_URL):
    req = urllib.request.Request(url, headers={"User-Agent": "bitunix-paper-status/1.0", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)

def format_paper_positions(state):
    if not isinstance(state, dict) or not isinstance(state.get("events"), list):
        raise ValueError("invalid paper state")
    events = state["events"]
    opens = [e for e in events if e.get("type") == "open"]
    closes = [e for e in events if e.get("type") == "close"]
    pnl = sum(float(e.get("estimated_net_pnl_usdt", 0)) for e in closes)
    p = state.get("position")
    lines = ["🧪 <b>目前模擬倉位（非真實下單）</b>"]
    if p:
        direction = "做多" if p.get("side") == "long" else "做空"
        lines.extend([
            "🟢 <b>持倉中</b>",
            f"幣種：<b>{html.escape(str(p.get('symbol','—')))}</b>｜{direction}",
            f"進場：{float(p['entry']):,.6g} USDT",
            f"停損：{float(p['stop']):,.6g}｜止盈：{float(p['tp']):,.6g}",
            f"數量：{float(p['quantity']):.8g}",
            f"開倉時間：{html.escape(str(p.get('opened_at','—')))} (UTC)",
            "未實現損益：未計算（非即時報價）",
        ])
    else:
        lines.append("⚪ 目前沒有模擬持倉")
    lines.extend([
        "",
        f"📒 歷史模擬開倉：{len(opens)} 筆｜平倉：{len(closes)} 筆",
        f"累積估算已實現損益：{pnl:+.4f} USDT",
        f"今日開倉：{int(state.get('orders_today',0))} / 2",
        "最近 5 筆平倉：",
    ])
    for e in closes[-5:][::-1]:
        lines.append(f"• {html.escape(str(e.get('signal_id','—')))}｜{html.escape(str(e.get('reason','—')))}｜{float(e.get('estimated_net_pnl_usdt',0)):+.4f} U")
    if not closes:
        lines.append("尚無模擬平倉紀錄")
    lines.append("\n資料來源：GitHub cloud-journal 快照；可能晚於市場即時狀態。")
    return "\n".join(lines)

def build_paper_positions_message():
    try:
        return format_paper_positions(fetch_snapshot())
    except Exception:
        return "⚠️ 暫時無法讀取雲端模擬倉位快照，無法確認目前持倉；請稍後再試。"
