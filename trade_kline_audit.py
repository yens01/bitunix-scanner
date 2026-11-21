"""Read-only Bitunix historical candle audit for exported futures order CSV.

Input: CSV with columns date, side, futures, average price, executed, netprofits.
Export XLSX to CSV locally first. No trading API keys or order endpoints are used.
"""
import argparse
import csv
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

API = "https://fapi.bitunix.com/api/v1/futures/market/kline"
INTERVAL_MS = 5 * 60_000


def timestamp_ms(value):
    value = str(value).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Naive datetime: export timestamps must include UTC offset")
    return int(dt.timestamp() * 1000)


def read_orders(path):
    with open(path, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    required = {"date", "side", "futures", "average price", "executed"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("CSV missing required columns: " + ", ".join(sorted(required)))
    result = []
    for row in rows:
        side = row["side"].strip().lower()
        if side not in {"open long", "open short", "close long", "close short"}:
            continue
        qty = float(row["executed"] or 0)
        price = float(row["average price"] or 0)
        if qty <= 0 or price <= 0:
            continue
        result.append({"ts": timestamp_ms(row["date"]), "symbol": row["futures"].strip().upper(),
                       "side": side, "price": price, "qty": qty,
                       "net": float(row.get("netprofits") or 0)})
    return sorted(result, key=lambda r: r["ts"])


def fetch_candles(symbol, start_ms, end_ms, *, sleep=0.15, fetch=None):
    """5m closed candles; paginate backwards with exclusive, interval-aligned endTime."""
    fetch = fetch or _fetch
    cursor = ((end_ms + INTERVAL_MS - 1) // INTERVAL_MS) * INTERVAL_MS
    earliest = (start_ms // INTERVAL_MS) * INTERVAL_MS
    found = {}
    while cursor > earliest:
        params = {"symbol": symbol, "interval": "5m", "endTime": cursor, "limit": 200}
        data = fetch(params)
        if not data:
            break
        stamps = []
        for bar in data:
            ts = int(bar["time"])
            if ts >= cursor:
                raise ValueError("API returned candle at/after exclusive endTime")
            stamps.append(ts)
            if earliest <= ts < end_ms:
                found[ts] = {"time": ts, "high": float(bar["high"]), "low": float(bar["low"]),
                             "close": float(bar["close"])}
        if not stamps or min(stamps) >= cursor:
            raise ValueError("Non-progressing kline pagination")
        cursor = min(stamps)
        if sleep:
            time.sleep(sleep)
    return [found[k] for k in sorted(found)]


def _fetch(params):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                payload = json.load(response)
            if str(payload.get("code")) not in {"0", "200"}:
                raise RuntimeError("Bitunix kline API error: " + str(payload.get("msg")))
            return payload.get("data") or []
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)


def diagnose(orders, candles):
    by_symbol = defaultdict(list)
    for row in orders:
        by_symbol[row["symbol"]].append(row)
    results = []
    for symbol, trades in by_symbol.items():
        bars = {bar["time"]: bar for bar in candles.get(symbol, [])}
        exposure = {"long": 0.0, "short": 0.0}
        last_entry = {"long": None, "short": None}
        for trade in trades:
            side = trade["side"]
            direction = "long" if "long" in side else "short"
            if side.startswith("close"):
                exposure[direction] = max(0.0, exposure[direction] - trade["qty"])
                if exposure[direction] < 1e-9:
                    last_entry[direction] = None
                continue
            previous = last_entry[direction]
            add_against = None
            if previous and exposure[direction] > 0:
                add_against = (trade["price"] < previous["price"]) if direction == "long" else (trade["price"] > previous["price"])
            was_add = exposure[direction] > 0
            exposure[direction] += trade["qty"]
            last_entry[direction] = trade
            # Start with the NEXT complete 5m candle to avoid claiming intra-bar ordering.
            entry_bucket = (trade["ts"] // INTERVAL_MS) * INTERVAL_MS
            future = [bars.get(entry_bucket + i * INTERVAL_MS) for i in range(1, 13)]
            def movement(window):
                subset = future[:window]
                if any(bar is None for bar in subset):
                    return None
                end = subset[-1]["close"]
                signed = (end / trade["price"] - 1) * 100 * (1 if direction == "long" else -1)
                adverse = min(bar["low"] for bar in subset) if direction == "long" else max(bar["high"] for bar in subset)
                adverse_pct = ((adverse / trade["price"] - 1) * 100 * (1 if direction == "long" else -1))
                return round(signed, 4), round(min(0, adverse_pct), 4)
            m15, m60 = movement(3), movement(12)
            results.append({"symbol": symbol, "entry_utc": datetime.fromtimestamp(trade["ts"]/1000, timezone.utc).isoformat(),
                            "direction": direction, "entry_price": trade["price"], "qty": trade["qty"],
                            "was_add": was_add, "added_against_last_entry": add_against,
                            "return_15m_pct": m15[0] if m15 else None,
                            "adverse_15m_pct": m15[1] if m15 else None,
                            "return_60m_pct": m60[0] if m60 else None,
                            "adverse_60m_pct": m60[1] if m60 else None,
                            "complete_60m": m60 is not None})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", required=True, help="UTC timestamped Bitunix CSV export")
    parser.add_argument("--output", default="trade_kline_audit.csv")
    parser.add_argument("--max-symbols", type=int, default=10, help="Limit requests for first run")
    parser.add_argument("--cache-dir", default=".kline_audit_cache")
    args = parser.parse_args()
    orders = read_orders(args.orders)
    counts = defaultdict(int)
    for order in orders:
        if order["side"].startswith("open"):
            counts[order["symbol"]] += 1
    symbols = sorted(counts, key=lambda s: (-counts[s], s))[:args.max_symbols]
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    candles = {}
    for symbol in symbols:
        subset = [row for row in orders if row["symbol"] == symbol]
        start = min(row["ts"] for row in subset) - 60 * 60_000
        end = max(row["ts"] for row in subset) + 70 * 60_000
        cache = cache_dir / (symbol + "_5m.json")
        if cache.exists():
            bars = json.loads(cache.read_text())
            if not bars or bars[0]["time"] > start or bars[-1]["time"] < end - INTERVAL_MS:
                bars = fetch_candles(symbol, start, end)
                cache.write_text(json.dumps(bars))
        else:
            bars = fetch_candles(symbol, start, end)
            cache.write_text(json.dumps(bars))
        candles[symbol] = bars
        print(symbol, len(bars), "candles")
    selected = [o for o in orders if o["symbol"] in symbols]
    output = diagnose(selected, candles)
    if not output:
        raise RuntimeError("No matching opening trades")
    with open(args.output, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output[0]))
        writer.writeheader()
        writer.writerows(output)
    valid = [r for r in output if r["complete_60m"]]
    print(f"Audited {len(output)} entries; {len(valid)} have full 60m candles. Output: {args.output}")
    if valid:
        adverse = sum(r["return_15m_pct"] < 0 for r in valid)
        adds = [r for r in valid if r["added_against_last_entry"] is not None]
        print(f"15m negative: {adverse}/{len(valid)}; additions against previous entry: "
              f"{sum(r['added_against_last_entry'] for r in adds)}/{len(adds)}")


if __name__ == "__main__":
    main()
