"""Read-only 15U discipline dashboard. Historical loss is not a trading target."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from paper_status import fetch_snapshot

TW = timezone(timedelta(hours=8))
HISTORICAL_LOSS_USDT = 11493.264148
PLAN_START_USDT = 15.0


def _valid_state(state):
    if not isinstance(state, dict) or not isinstance(state.get("events"), list):
        raise ValueError("missing or invalid paper events")
    return state


def _closed_pnl(state):
    total = 0.0
    for event in state["events"]:
        if event.get("type") != "close":
            continue
        value = float(event["estimated_net_pnl_usdt"])
        if not math.isfinite(value):
            raise ValueError("invalid paper PnL")
        total += value
    return total


def goal_message(state):
    state = _valid_state(state)
    pnl = _closed_pnl(state)
    equity = PLAN_START_USDT + pnl
    return "\n".join([
        "🎯 <b>15U 紀律重建計畫（僅模擬）</b>",
        f"起始模擬本金：{PLAN_START_USDT:.2f} U",
        f"模擬已平倉淨損益：{pnl:+.4f} U",
        f"模擬已平倉後資金：{equity:.4f} U",
        f"歷史實盤淨虧損（人工基準）：{HISTORICAL_LOSS_USDT:,.2f} U",
        "⚠️ 歷史實盤虧損與模擬損益分開計算，不能視為已回本。",
        "🛡️ 今日紀律：不攤平、不移遠停損、最多兩筆、連虧兩筆停止。",
        "📌 不以回本金額設定每日獲利配額；沒有訊號就是休息。",
    ])


def entry_watch_message(state, *, now=None):
    state = _valid_state(state)
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(TW)
    if state.get("halted"):
        status = "⛔ 模擬交易已停止；今日不新增持倉。"
    elif state.get("position"):
        status = "⛔ 已有模擬持倉；禁止加碼或同時開第二筆。"
    elif int(state.get("orders_today", 0)) >= 2:
        status = "⛔ 已達每日兩筆上限；今日不再進場。"
    else:
        status = "⏳ 尚未驗證 1H EMA20/50 與 15m 回踩確認；目前不提供進場指令。"
    return "\n".join([
        "🕒 <b>今日進場觀察｜BTCUSDT / ETHUSDT</b>",
        f"台灣時間：{local:%Y-%m-%d %H:%M}",
        status,
        "候選觀察時間：09:00–10:00、21:00–23:00（歷史觀察時段，非已驗證優勢）。",
        "必要條件：1H EMA20/50 趨勢一致、15m 回踩後收盤確認、停損可執行。",
        "模擬單筆風險 ≤0.5%；每日風險 ≤1%；停損後不加碼。",
        "⚠️ 此按鈕未接即時 K 線確認，無法判定今天何時可以開倉。",
        "⚠️ 不自動下單；若資料過期、風險不符或不確定，一律等待。",
    ])


def build_recovery_message(command):
    try:
        state = _valid_state(fetch_snapshot())
        if command == "GOAL":
            return goal_message(state)
        if command == "ENTRY_WATCH":
            return entry_watch_message(state)
    except (ValueError, TypeError, KeyError, OverflowError, OSError, TimeoutError):
        return "⚠️ 無法驗證雲端模擬交易狀態；暫停提供目標進度或進場觀察。"
    raise ValueError("unsupported recovery command")
