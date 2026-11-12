# Paper trading verification and research workflow

All features here are **paper-only**. No exchange order API is used.

## Stage 1: ledger and event safety

- Entry bars are persisted as `entry_bar_ms` separately from notification time.
- Exit replay skips the entry bar and bars already processed.
- Repeated-run regression tests check for duplicate close events.
- Existing cloud state remains authoritative; do not reset `paper_state.json`.
- **Remaining limitation:** the scanner does not yet have a full atomic, externally locked event ledger; GitHub cache and branch snapshots are not a transactional database. Audit state restore, backup and outbox delivery before treating it as production-grade accounting.

## Stage 2: historical replay

Prepare a historical JSON file mapping symbols to closed candle arrays with
`open_time_ms`, `low`, `high`, and `closed: true`.
Use the journal available for the same historical period.

```bash
python paper_backtest.py --journal yes_journal.jsonl --candles historical_candles.json --output backtest_comparison.json
```

The result compares 50 and 65 quality thresholds using identical candles and
0.22% roundtrip estimated costs. This is **not** an exchange-accurate fill model.
It does not yet model spreads, order book depth, funding, overlapping positions,
order expiry or portfolio capital constraints. It must not be treated as a
reliable profitability estimate until those are implemented.

## Stage 3: sample-out-of-time checks

`--cutoff-ms` restricts observations before a specified millisecond timestamp.
For a valid out-of-sample study, choose parameters only on earlier data,
freeze them, and evaluate on later untouched data. Do not choose the best
threshold from the later test set. Report sample sizes, net return,
drawdown and uncertainty, not just win rate.

## Stage 4: Telegram daily summary

```bash
python paper_daily_report.py --state paper_state.json
```

The command prints Telegram HTML-formatted text, but **does not send a message**.
The reported net PnL is cumulative; the UTC date is the accounting date.
A scheduled sender should be added only after deduplication and durable
notification state are verified. Never claim that the command alone creates
a daily Telegram notification.
