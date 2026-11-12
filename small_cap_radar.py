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


def score_candidate(symbol, bars, quote_vol, change_24h, *, now_ms=None):
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    closed = sorted((b for b in bars if b.time + 900000 <= now_ms),
                    key=lambda b: b.time)
    if len(closed) < 42 or not math.isfinite(quote_vol) or quote_vol < 500000:
        return None
    if not -8 <= change_24h <= 8:
        return None  # Avoid already-pumped coins and collapsing markets.
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
    return {"symbol": symbol, "score": score, "stage": "啟動" if breakout_pct >= 0 else "潛伏",
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


def rank_candidates(tickers, fetch_candles, *, now_ms=None, limit=5, pool=20):
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
        if 500000 <= qv <= 150000000 and -8 <= change <= 8:
            eligible.append((sym, qv, change))
    eligible.sort(key=lambda item: item[1], reverse=True)
    result = []
    # Run sequentially to avoid burst requests after the main market scanner.
    # Retry exchange rate limits with bounded exponential backoff.
    for sym, qv, change in eligible[:pool]:
        try:
            bars = fetch_with_backoff(fetch_candles, sym)
            candidate = score_candidate(sym, bars, qv, change, now_ms=now_ms)
        except RuntimeError as exc:
            print(f"Radar {sym}: API unavailable after retries: {exc}", flush=True)
            raise
        except (ValueError, TypeError, AttributeError, ZeroDivisionError) as exc:
            print(f"Radar skipped invalid {sym}: {exc}", flush=True)
            continue
        if candidate:
            result.append(candidate)
    return sorted(result, key=lambda r: (-r["score"], -r["volume_ratio"], r["symbol"]))[:limit]


def scan():
    return rank_candidates(fetch_tickers(), lambda symbol: parse_klines(fetch_klines(symbol, "15m", 100)))


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
