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


def _closed_candles(symbol, interval, minutes, now):
    """Only completed, recent exchange candles; reject unknown schemas and gaps."""
    from bitunix_scanner import fetch_klines
    raw = fetch_klines(symbol, interval=interval, limit=120)
    if not isinstance(raw, list):
        raise ValueError("invalid candles")
    candles = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("invalid candle")
        ts = item.get("time", item.get("timestamp"))
        ts = float(ts)
        if ts > 1e12:
            ts /= 1000
        vals = [float(item[k]) for k in ("open", "high", "low", "close")]
        if not all(math.isfinite(v) and v > 0 for v in vals):
            raise ValueError("invalid OHLC")
        op, hi, lo, cl = vals
        if lo > min(op, cl) or hi < max(op, cl):
            raise ValueError("invalid OHLC bounds")
        candles.append((ts, op, hi, lo, cl))
    candles.sort(key=lambda c: c[0])
    if len({c[0] for c in candles}) != len(candles):
        raise ValueError("duplicate candles")
    current = now.timestamp()
    closed = [c for c in candles if c[0] + minutes * 60 <= current]
    if len(closed) < 55 or current - (closed[-1][0] + minutes * 60) > minutes * 60 + 90:
        raise ValueError("stale or insufficient candles")
    if any(b[0] - a[0] != minutes * 60 for a, b in zip(closed[-55:], closed[-54:])):
        raise ValueError("missing candle interval")
    return closed


def _ema(values, period):
    alpha = 2 / (period + 1)
    v = sum(values[:period]) / period
    for x in values[period:]:
        v = alpha * x + (1 - alpha) * v
    return v


def _watch_levels(symbol, now):
    hourly = _closed_candles(symbol, "1h", 60, now)
    quarter = _closed_candles(symbol, "15m", 15, now)
    closes = [c[4] for c in hourly]
    fast, slow = _ema(closes, 20), _ema(closes, 50)
    direction = "做多" if fast > slow else "做空"
    if abs(fast - slow) / slow < 0.001:
        return f"{symbol}｜⏳ 1H 趨勢不明，等待。"
    # Last 15m close must confirm direction and recover/break preceding close.
    last, prev = quarter[-1], quarter[-2]
    recent = quarter[-15:-1]
    tr = [max(c[2]-c[3], abs(c[2]-p[4]), abs(c[3]-p[4]))
          for p, c in zip(quarter[-16:-1], quarter[-15:])]
    atr = sum(tr[-14:]) / 14
    if atr <= 0:
        raise ValueError("invalid ATR")
    if direction == "做多":
        stop = min(c[3] for c in recent[-6:]) - 0.2 * atr
        confirmed = last[4] > last[1] and last[4] > prev[4] and last[3] > stop
        risk = last[4] - stop
        target = last[4] + 2 * risk
    else:
        stop = max(c[2] for c in recent[-6:]) + 0.2 * atr
        confirmed = last[4] < last[1] and last[4] < prev[4] and last[2] < stop
        risk = stop - last[4]
        target = last[4] - 2 * risk
    if not confirmed or risk <= 0 or risk / last[4] > 0.02 or target <= 0:
        return f"{symbol}｜⏳ {direction}方向觀察中；15m 確認或停損距離不合格，等待。"
    # Last closed candle is a reference, not a market order.
    price = last[4]
    return ("\n".join([
        f"{symbol}｜🔎 {direction}候選計畫（未觸發，僅模擬）",
        f"參考進場價：{price:,.2f} U（15m 已收盤價，非現價）",
        f"停損：{stop:,.2f} U｜目標 2R：{target:,.2f} U",
        f"單位風險：{risk:,.2f} U｜風報比 1:2",
        f"訊號 K 線結束：{datetime.fromtimestamp(last[0]+900, TW):%H:%M}（台灣）",
        "有效期：僅供下一根 15m K 線觀察；須重新檢查現價及風險。",
        "⛔ 未驗證回踩成交、現價與滑價，不能直接市價下單。",
    ]))


def entry_watch_message(state, *, now=None):
    state = _valid_state(state)
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(TW)
    lines = ["🕒 <b>今日進場觀察｜BTCUSDT / ETHUSDT</b>",
             f"台灣時間：{local:%Y-%m-%d %H:%M}"]
    if state.get("halted"):
        lines.append("⛔ 模擬交易已停止；今日不新增持倉。")
    elif state.get("position"):
        p = state["position"]
        lines.append("⛔ 已有模擬持倉；禁止加碼或同時開第二筆。")
        lines.append(f"目前持倉：{str(p.get('symbol', '未知'))}；請使用「📌 模擬持倉」查看原停損。")
    elif int(state.get("orders_today", 0)) >= 2:
        lines.append("⛔ 已達每日兩筆上限；今日不再進場。")
    else:
        for symbol in ("BTCUSDT", "ETHUSDT"):
            try:
                lines.append(_watch_levels(symbol, now))
            except (ValueError, TypeError, KeyError, OSError, RuntimeError, TimeoutError) as exc:
                lines.append(f"{symbol}｜⚠️ K 線資料無法驗證，禁止進場。")
    lines.extend([
        "模擬單筆風險 ≤0.5%；每日風險 ≤1%；禁止攤平與移遠停損。",
        "⚠️ 僅提供條件式模擬計畫，不自動下單，也不是即時成交報價。",
    ])
    return "\n".join(lines)


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
