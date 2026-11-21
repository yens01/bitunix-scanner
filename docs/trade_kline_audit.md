# Historical candle trade audit (read-only)

This script matches Bitunix futures order exports to historical **5-minute LAST_PRICE** candles. It **does not trade**, use credentials, or access private APIs.

1. On your computer, open the Bitunix XLSX export and **Save As CSV UTF-8**. Keep the `date` field in ISO-8601 format **including UTC offset** (e.g. `2026-11-13T06:00:00+00:00`). A timezone-free date is rejected deliberately.
2. Run `python trade_kline_audit.py --orders futures-history.csv --max-symbols 5 --output trade_kline_audit.csv`.
3. Inspect `return_15m_pct` / `return_60m_pct`: positive is favorable for the recorded direction. `added_against_last_entry` compares each added entry with the preceding entry price. Missing K-line windows are blank, **not assumed zero**.

Uses official Bitunix `GET /api/v1/futures/market/kline`, `endTime` exclusive and `limit <= 200`, paging backwards. See https://www.bitunix.com/api-docs/futures/market/get_kline . Requests may be substantial across 8 months; start with 1–5 symbols. Historical delisted symbols may return no candles. Cached data lives in `.kline_audit_cache` (do not commit).

**Limitations:** The CSV export lacks position IDs; additions are inferred from chronological order and same-side outstanding quantity, not verified position grouping. This is a descriptive price-path audit, not a backtest. The entry candle is excluded because high/low ordering inside it is unknown. LAST_PRICE candles are not executable bid/ask quotes and do not include funding, spread, or liquidation mechanics. This script cannot prove why a trade was placed.
