"""Paper-only early breakout radar using Bitunix public closed candles.

Scores are heuristic rankings, NOT calibrated probabilities or trade signals.
"""
from __future__ import annotations
import argparse
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from bitunix_scanner import MAJOR_SYMBOLS, fetch_klines, fetch_tickers, parse_klines


def trade_plan(closed, resistance, *, volume_ratio):
    """Illustrative long-only breakout plan, never an execution instruction."""
    last = closed[-1]
    previous = closed[-2]
    prior_resistance = max(b.high for b in closed[-22:-2])
    if previous.close > prior_resistance and last.close < prior_resistance:
        status, reason = "突破失敗", "前一根曾站上壓力，最新收盤跌回原壓力下方"
    elif last.close > resistance:
        status, reason = "突破確認", "最新15m收盤站上前20根高點；仍需觀察能否守穩"
    elif previous.close > prior_resistance and last.low <= prior_resistance <= last.close:
        status, reason = "等待回踩", "價格回測先前壓力；需再次確認支撐"
    else:
        status, reason = "等待突破", "尚未收盤站上前20根壓力"
    entry = resistance
    stop = min(b.low for b in closed[-5:])
    risk_pct = (entry - stop) / entry * 100 if entry > 0 else float("inf")
    valid = math.isfinite(risk_pct) and 0.3 <= risk_pct <= 4 and volume_ratio >= 1.5 and status != "突破失敗"
    return {"entry_status": status, "entry_reason": reason,
            "reference_entry": round(entry, 10) if valid else None,
            "reference_stop": round(stop, 10) if valid else None,
            "reference_target": round(entry + 2 * (entry - stop), 10) if valid else None,
            "reference_rr": 2 if valid else None,
            "risk_pct": round(risk_pct, 2) if math.isfinite(risk_pct) else None,
            "plan_note": "僅供突破後評估；非立即買入" if valid else "停損距離不合理或突破失敗，不提供進場計畫"}


def _breakout_candidate(symbol, closed, quote_vol, change_24h):
    """Detect the first closed 15m expansion out of a quiet base; no live trade."""
    if len(closed) < 42 or quote_vol < 500000 or not math.isfinite(change_24h):
        return None
    base = closed[-41:-5]
    recent = closed[-5:]
    if len(base) < 20 or any(b.close <= 0 or b.high <= 0 or b.low <= 0 for b in closed[-41:]):
        return None
    ceiling = max(b.high for b in base)
    floor = min(b.low for b in base)
    if floor <= 0 or ceiling / floor - 1 > 0.16:
        return None
    base_vol = sum(float(b.volume) for b in base[-20:]) / 20
    if base_vol <= 0:
        return None
    first = next((i for i, b in enumerate(recent)
                  if b.close > ceiling * 1.005 and
                  float(b.volume) / base_vol >= 2.0), None)
    if first is None:
        return None
    latest = recent[-1]
    expansion_pct = (latest.close / ceiling - 1) * 100
    if expansion_pct < 0 or expansion_pct > 15:
        return None  # Late chases are excluded from actionable watchlist.
    if any(not math.isfinite(float(b.volume)) or b.volume < 0 for b in recent):
        return None
    volume_ratio = max(float(b.volume) for b in recent[first:]) / base_vol
    score = min(100, round(65 + min(volume_ratio, 6) * 4 + (10 if first >= 3 else 0)))
    return {
        "symbol": symbol, "score": score, "stage": "爆發初期" if first >= 2 else "突破延續",
        "entry_status": "觀察突破回測", "entry_reason": "已收盤突破整理區；等待回踩與量價確認",
        "reference_entry": None, "reference_stop": None, "reference_target": None,
        "reference_rr": None, "risk_pct": None,
        "plan_note": "僅供觀察，不追高；確認滑價與流動性",
        "last_closed_price": latest.close, "volume_ratio": round(volume_ratio, 2),
        "breakout_pct": round(expansion_pct, 2),
        "range_width_pct": round((ceiling / floor - 1) * 100, 2),
        "change_24h_pct": round(change_24h, 2), "quote_volume_24h": quote_vol,
        "resistance": ceiling, "candle_open_ms": latest.time,
    }


def score_candidate(symbol, bars, quote_vol, change_24h, *, now_ms=None):
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    closed = sorted((b for b in bars if b.time + 900000 <= now_ms),
                    key=lambda b: b.time)
    if len(closed) < 42 or not math.isfinite(quote_vol) or quote_vol < 500000:
        return None
    if not -15 <= change_24h <= 120:
        return None  # Reject extreme moves and collapsing markets.
    breakout = _breakout_candidate(symbol, closed, quote_vol, change_24h)
    if breakout is not None:
        return breakout
    if not -8 <= change_24h <= 8:
        return None  # Legacy pre-breakout radar is intentionally conservative.
    recent = closed[-1]
    prior = closed[-41:-1]
    volumes = [float(getattr(b, "volume", 0) or 0) for b in prior]
    current_vol = float(getattr(recent, "volume", 0) or 0)
    if any(not math.isfinite(v) or v < 0 for v in volumes + [current_vol]):
        return None
    avg = sum(volumes[-20:]) / 20
    if avg <= 0 or recent.close <= 0:
        return None
    ratio = current_vol / avg
    resistance = max(b.high for b in prior[-20:])
    low = min(b.low for b in prior[-20:])
    if resistance <= low or low <= 0:
        return None
    breakout_pct = (recent.close / resistance - 1) * 100
    width_pct = (resistance / low - 1) * 100
    if width_pct > 22 or ratio < 1.5 or breakout_pct < -1.5:
        return None
    volume_points = min(30, max(0, (ratio - 1) * 12))
    structure_points = 25 if 0 <= breakout_pct <= 3 else (14 if -1.5 <= breakout_pct < 0 else 0)
    compression_points = 20 if width_pct <= 8 else (12 if width_pct <= 14 else 5)
    strength_points = 15 if 0 <= change_24h <= 5 else (7 if -3 <= change_24h <= 8 else 0)
    liquidity_points = 10 if quote_vol >= 3000000 else (6 if quote_vol >= 1000000 else 3)
    score = round(volume_points + structure_points + compression_points + strength_points + liquidity_points)
    if score < 60:
        return None
    plan = trade_plan(closed, resistance, volume_ratio=ratio)
    return {"symbol": symbol, "score": score, "stage": "啟動" if breakout_pct >= 0 else "潛伏",
            **plan,
            "last_closed_price": recent.close, "volume_ratio": round(ratio, 2),
            "breakout_pct": round(breakout_pct, 2), "range_width_pct": round(width_pct, 2),
            "change_24h_pct": round(change_24h, 2), "quote_volume_24h": quote_vol,
            "resistance": resistance, "candle_open_ms": recent.time}


def fetch_with_backoff(fetch_candles, symbol, *, attempts=4, sleep=time.sleep):
    """Retry only rate-limit failures; propagate other API failures."""
    for attempt in range(attempts):
        try:
            return fetch_candles(symbol)
        except RuntimeError as exc:
            message = str(exc).lower()
            limited = any(token in message for token in ("request too frequently", "rate limit", "http 429", "too many requests"))
            if not limited or attempt == attempts - 1:
                raise
            delay = min(2 ** attempt * 3, 24)
            print(f"Radar {symbol}: rate limited; retry in {delay}s ({attempt + 1}/{attempts})", flush=True)
            sleep(delay)


def is_untradable_contract_error(exc):
    """Only skip explicit symbol-level futures trading restrictions."""
    message = str(exc).lower()
    return any(phrase in message for phrase in (
        "this futures is not allowed to trade",
        "futures is not allowed to trade",
    ))


def rank_candidates(tickers, fetch_candles, *, now_ms=None, limit=5, pool=60):
    eligible = []
    for t in tickers:
        sym = str(t.get("symbol") or "").upper()
        if not sym.endswith("USDT") or sym in MAJOR_SYMBOLS:
            continue
        try:
            qv = float(t.get("quoteVol") or 0)
            last = float(t.get("lastPrice") or t.get("last") or 0)
            opening = float(t.get("open") or 0)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in (qv, last, opening)) or opening <= 0 or last <= 0:
            continue
        change = (last / opening - 1) * 100
        if 500000 <= qv <= 150000000 and -15 <= change <= 120:
            eligible.append((sym, qv, change))
    # Cover both liquid quiet bases and fast movers, instead of volume-only top 20.
    eligible.sort(key=lambda item: (abs(item[2]) < 8, -item[1]))
    result = []
    # Run sequentially to avoid burst requests after the main market scanner.
    # Retry exchange rate limits with bounded exponential backoff.
    for sym, qv, change in eligible[:pool]:
        try:
            bars = fetch_with_backoff(fetch_candles, sym)
            candidate = score_candidate(sym, bars, qv, change, now_ms=now_ms)
        except RuntimeError as exc:
            if is_untradable_contract_error(exc):
                print(f"Radar skipped untradable contract {sym}: {exc}", flush=True)
                continue
            print(f"Radar {sym}: API unavailable after retries: {exc}", flush=True)
            raise
        except (ValueError, TypeError, AttributeError, ZeroDivisionError) as exc:
            print(f"Radar skipped invalid {sym}: {exc}", flush=True)
            continue
        if candidate:
            result.append(candidate)
    return sorted(result, key=lambda r: (-r["score"], -r["volume_ratio"], r["symbol"]))[:limit]


def scan():
    # Allow the preceding full-market scan to cool down before a smaller radar pass.
    time.sleep(5)
    def paced_candles(symbol):
        time.sleep(1.5)
        return parse_klines(fetch_klines(symbol, "15m", 100))
    return rank_candidates(fetch_tickers(), paced_candles)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("small_cap_radar.json"))
    args = parser.parse_args()
    data = {"generated_at": datetime.now(timezone.utc).isoformat(),
            "disclaimer": "heuristic watchlist; no live orders; not a probability",
            "candidates": scan()}
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
