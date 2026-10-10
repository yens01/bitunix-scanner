#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YES 訊號日記與事後追蹤
- 記錄每次可進場訊號（入場／停損／止盈／計畫RR）
- 用後續 K 線結算：先碰到停損或止盈
- 統計勝率、平均計畫RR、平均實現R、期望值
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

JOURNAL_PATH = Path(__file__).resolve().parent / "yes_journal.jsonl"
TZ_TW = timezone(timedelta(hours=8))

# 目前生效規則標籤（新訊號寫入此值；舊檔無欄位視為 legacy）
CURRENT_RULESET = "v2"
LEGACY_RULESET = "legacy"

# 同一幣種同方向去重冷卻（秒）
DEDUP_SECONDS = 4 * 3600
# 結算最長觀察根數（15m × 96 ≈ 24h；1h 可另傳）
DEFAULT_MAX_BARS = 96
# 同根同時碰到 SL/TP 時採保守：算停損
CONSERVATIVE_SAME_BAR = True


@dataclass
class JournalEntry:
    id: str
    ts: str  # ISO 記錄時間
    signal_time_ms: int  # 訊號當下（通常用記錄時）
    symbol: str
    side: str  # long / short
    interval: str
    entry: float
    stop: float
    tp: float
    planned_rr: float
    score: float
    structure: str
    missing_steps: str
    status: str = "open"  # open / win / loss / timeout
    exit_price: float = 0.0
    exit_time_ms: int = 0
    realized_r: float = 0.0  # 以風險為 1R 的實現倍數
    bars_held: int = 0
    note: str = ""
    mfe_r: float = 0.0  # 最大有利偏移（R）
    mae_r: float = 0.0  # 最大不利偏移（R）
    ruleset: str = LEGACY_RULESET  # legacy | v2（新規則單獨統計）

    def risk(self) -> float:
        return abs(self.entry - self.stop)


def _now_iso() -> str:
    return datetime.now(TZ_TW).strftime("%Y-%m-%d %H:%M:%S")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def load_journal(path: Path = JOURNAL_PATH) -> list[JournalEntry]:
    if not path.exists():
        return []
    rows: list[JournalEntry] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                rows.append(JournalEntry(**{k: d[k] for k in JournalEntry.__dataclass_fields__ if k in d}))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
    return rows


def save_journal(rows: list[JournalEntry], path: Path = JOURNAL_PATH) -> None:
    with path.open("w", encoding="utf-8") as fp:
        for r in rows:
            fp.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")


def append_entry(entry: JournalEntry, path: Path = JOURNAL_PATH) -> None:
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")


def _recent_duplicate(
    rows: list[JournalEntry],
    symbol: str,
    side: str,
    now_ms: int,
    cooldown: int = DEDUP_SECONDS,
) -> bool:
    for r in reversed(rows):
        if r.symbol != symbol or r.side != side:
            continue
        if now_ms - r.signal_time_ms < cooldown * 1000:
            return True
        break
    return False


def record_yes_signals(
    setups: list[Any],
    *,
    interval: str = "15m",
    path: Path = JOURNAL_PATH,
    quiet: bool = False,
) -> list[JournalEntry]:
    """從 ChecklistResult 列表記錄 trigger_ready 的訊號。"""
    rows = load_journal(path)
    now_ms = _now_ms()
    added: list[JournalEntry] = []
    for s in setups:
        if not getattr(s, "trigger_ready", False):
            continue
        side = getattr(s, "side", "none")
        if side not in ("long", "short"):
            continue
        entry = float(getattr(s, "entry", 0) or 0)
        stop = float(getattr(s, "stop", 0) or 0)
        tp = float(getattr(s, "tp", 0) or 0)
        if entry <= 0 or stop <= 0 or tp <= 0:
            continue
        if abs(entry - stop) <= 0:
            continue
        symbol = getattr(s, "symbol", "")
        if _recent_duplicate(rows + added, symbol, side, now_ms):
            continue
        je = JournalEntry(
            id=uuid.uuid4().hex[:12],
            ts=_now_iso(),
            signal_time_ms=now_ms,
            symbol=symbol,
            side=side,
            interval=interval,
            entry=entry,
            stop=stop,
            tp=tp,
            planned_rr=float(getattr(s, "rr", 0) or 0),
            score=float(getattr(s, "setup_score", 0) or 0),
            structure=str(getattr(s, "structure", "")),
            missing_steps=str(getattr(s, "missing_steps", "")),
            status="open",
            ruleset=CURRENT_RULESET,
        )
        append_entry(je, path)
        added.append(je)
        rows.append(je)
    if added and not quiet:
        print(f"已記錄 {len(added)} 筆 YES 訊號 → {path.name}")
    return added


def resolve_entry(
    entry: JournalEntry,
    candles: list[Any],
    *,
    max_bars: int = DEFAULT_MAX_BARS,
) -> JournalEntry:
    """
    candles: 舊→新，需含 time/high/low/close（Candle 或 dict）
    只看訊號時間之後的 K 線。
    """
    if entry.status != "open":
        return entry

    def _t(c: Any) -> int:
        return int(getattr(c, "time", None) or c.get("time") or 0)

    def _h(c: Any) -> float:
        return float(getattr(c, "high", None) or c.get("high") or 0)

    def _l(c: Any) -> float:
        return float(getattr(c, "low", None) or c.get("low") or 0)

    def _c(c: Any) -> float:
        return float(getattr(c, "close", None) or c.get("close") or 0)

    after = [c for c in candles if _t(c) > entry.signal_time_ms]
    if not after:
        return entry

    risk = entry.risk()
    if risk <= 0:
        entry.status = "timeout"
        entry.note = "風險距離為 0，無法結算"
        return entry

    window = after[:max_bars]
    mfe = 0.0
    mae = 0.0

    for i, c in enumerate(window, 1):
        hi, lo, cl = _h(c), _l(c), _c(c)
        if entry.side == "long":
            mfe = max(mfe, (hi - entry.entry) / risk)
            mae = max(mae, (entry.entry - lo) / risk)
            hit_sl = lo <= entry.stop
            hit_tp = hi >= entry.tp
            if hit_sl and hit_tp:
                if CONSERVATIVE_SAME_BAR:
                    entry.status = "loss"
                    entry.exit_price = entry.stop
                    entry.realized_r = -1.0
                else:
                    entry.status = "win"
                    entry.exit_price = entry.tp
                    entry.realized_r = (entry.tp - entry.entry) / risk
                entry.exit_time_ms = _t(c)
                entry.bars_held = i
                entry.mfe_r = round(mfe, 3)
                entry.mae_r = round(mae, 3)
                entry.note = "同根觸及SL/TP，採保守停損" if CONSERVATIVE_SAME_BAR else "同根觸及，採止盈"
                return entry
            if hit_sl:
                entry.status = "loss"
                entry.exit_price = entry.stop
                entry.realized_r = -1.0
                entry.exit_time_ms = _t(c)
                entry.bars_held = i
                entry.mfe_r = round(mfe, 3)
                entry.mae_r = round(mae, 3)
                return entry
            if hit_tp:
                entry.status = "win"
                entry.exit_price = entry.tp
                entry.realized_r = (entry.tp - entry.entry) / risk
                entry.exit_time_ms = _t(c)
                entry.bars_held = i
                entry.mfe_r = round(mfe, 3)
                entry.mae_r = round(mae, 3)
                return entry
        else:  # short
            mfe = max(mfe, (entry.entry - lo) / risk)
            mae = max(mae, (hi - entry.entry) / risk)
            hit_sl = hi >= entry.stop
            hit_tp = lo <= entry.tp
            if hit_sl and hit_tp:
                if CONSERVATIVE_SAME_BAR:
                    entry.status = "loss"
                    entry.exit_price = entry.stop
                    entry.realized_r = -1.0
                else:
                    entry.status = "win"
                    entry.exit_price = entry.tp
                    entry.realized_r = (entry.entry - entry.tp) / risk
                entry.exit_time_ms = _t(c)
                entry.bars_held = i
                entry.mfe_r = round(mfe, 3)
                entry.mae_r = round(mae, 3)
                entry.note = "同根觸及SL/TP，採保守停損" if CONSERVATIVE_SAME_BAR else "同根觸及，採止盈"
                return entry
            if hit_sl:
                entry.status = "loss"
                entry.exit_price = entry.stop
                entry.realized_r = -1.0
                entry.exit_time_ms = _t(c)
                entry.bars_held = i
                entry.mfe_r = round(mfe, 3)
                entry.mae_r = round(mae, 3)
                return entry
            if hit_tp:
                entry.status = "win"
                entry.exit_price = entry.tp
                entry.realized_r = (entry.entry - entry.tp) / risk
                entry.exit_time_ms = _t(c)
                entry.bars_held = i
                entry.mfe_r = round(mfe, 3)
                entry.mae_r = round(mae, 3)
                return entry

    # 超時：以最後收盤結算
    last = window[-1]
    cl = _c(last)
    if entry.side == "long":
        realized = (cl - entry.entry) / risk
    else:
        realized = (entry.entry - cl) / risk
    entry.status = "timeout"
    entry.exit_price = cl
    entry.exit_time_ms = _t(last)
    entry.bars_held = len(window)
    entry.realized_r = round(realized, 3)
    entry.mfe_r = round(mfe, 3)
    entry.mae_r = round(mae, 3)
    entry.note = f"觀察期满({len(window)}根)按收盤結算"
    return entry


def resolve_all(
    fetch_klines_fn: Callable[[str, str, int], list],
    parse_fn: Callable[[list], list],
    *,
    path: Path = JOURNAL_PATH,
    max_bars: int = DEFAULT_MAX_BARS,
    only_open: bool = True,
) -> tuple[int, int]:
    """
    fetch_klines_fn(symbol, interval, limit) -> raw list
    parse_fn(raw) -> candles
    回傳 (更新筆數, 仍未結算筆數)
    """
    rows = load_journal(path)
    updated = 0
    still_open = 0
    for i, e in enumerate(rows):
        if only_open and e.status != "open":
            continue
        if e.status != "open":
            continue
        try:
            # 多抓一些以覆蓋訊號後時間
            raw = fetch_klines_fn(e.symbol, e.interval or "15m", min(max_bars + 50, 200))
            candles = parse_fn(raw)
            before = e.status
            rows[i] = resolve_entry(e, candles, max_bars=max_bars)
            if rows[i].status != before:
                updated += 1
            if rows[i].status == "open":
                still_open += 1
        except Exception as ex:
            rows[i].note = f"結算失敗: {ex}"
            still_open += 1
    save_journal(rows, path)
    return updated, still_open


@dataclass
class JournalStats:
    total: int = 0
    open_n: int = 0
    win_n: int = 0
    loss_n: int = 0
    timeout_n: int = 0
    win_rate: float = 0.0  # 不含 open；timeout 算已結算
    avg_planned_rr: float = 0.0
    avg_realized_r: float = 0.0
    expectancy_r: float = 0.0
    sum_r: float = 0.0
    avg_mfe: float = 0.0
    avg_mae: float = 0.0
    by_side: dict[str, dict[str, float]] = field(default_factory=dict)


def compute_stats(rows: list[JournalEntry] | None = None, path: Path = JOURNAL_PATH) -> JournalStats:
    rows = rows if rows is not None else load_journal(path)
    st = JournalStats(total=len(rows))
    if not rows:
        return st

    closed = [r for r in rows if r.status in ("win", "loss", "timeout")]
    st.open_n = sum(1 for r in rows if r.status == "open")
    st.win_n = sum(1 for r in closed if r.status == "win")
    st.loss_n = sum(1 for r in closed if r.status == "loss")
    st.timeout_n = sum(1 for r in closed if r.status == "timeout")

    if closed:
        # 勝率：嚴格用 win/(win+loss)，timeout 另計；另給含 timeout 的結算勝率
        wl = st.win_n + st.loss_n
        st.win_rate = (st.win_n / wl * 100) if wl else 0.0
        st.avg_planned_rr = sum(r.planned_rr for r in closed) / len(closed)
        st.avg_realized_r = sum(r.realized_r for r in closed) / len(closed)
        st.sum_r = sum(r.realized_r for r in closed)
        st.expectancy_r = st.avg_realized_r
        st.avg_mfe = sum(r.mfe_r for r in closed) / len(closed)
        st.avg_mae = sum(r.mae_r for r in closed) / len(closed)

    for side in ("long", "short"):
        sub = [r for r in closed if r.side == side]
        if not sub:
            continue
        w = sum(1 for r in sub if r.status == "win")
        l = sum(1 for r in sub if r.status == "loss")
        st.by_side[side] = {
            "n": float(len(sub)),
            "win_rate": (w / (w + l) * 100) if (w + l) else 0.0,
            "avg_r": sum(r.realized_r for r in sub) / len(sub),
        }
    return st


def print_stats(st: JournalStats, title: str = "YES 訊號追蹤統計") -> None:
    print()
    print(f"══ {title} ══")
    print(f"總筆數 {st.total} ｜ 未結算 {st.open_n} ｜ 勝 {st.win_n} ｜ 負 {st.loss_n} ｜ 超時 {st.timeout_n}")
    if st.win_n + st.loss_n > 0:
        print(f"勝率(不含超時) {st.win_rate:.1f}%  （win/(win+loss)）")
    closed_n = st.win_n + st.loss_n + st.timeout_n
    if closed_n:
        print(f"平均計畫 RR {st.avg_planned_rr:.2f}")
        print(f"平均實現 R  {st.avg_realized_r:+.2f}R  （合計 {st.sum_r:+.2f}R）")
        print(f"期望值      {st.expectancy_r:+.2f}R / 筆")
        print(f"平均 MFE {st.avg_mfe:.2f}R ｜ 平均 MAE {st.avg_mae:.2f}R")
    for side, d in st.by_side.items():
        label = "做多" if side == "long" else "做空"
        print(f"  {label}: {int(d['n'])} 筆｜勝率 {d['win_rate']:.1f}%｜均R {d['avg_r']:+.2f}")
    if st.open_n:
        print(f"提示: 還有 {st.open_n} 筆未結算，請執行 journal resolve")
    if st.total == 0:
        print("尚無紀錄。先跑 setup／watch，有 YES 會自動寫入 yes_journal.jsonl")
    print()


def rows_by_ruleset(rows: list[JournalEntry], ruleset: str) -> list[JournalEntry]:
    return [r for r in rows if (r.ruleset or LEGACY_RULESET) == ruleset]


def print_stats_by_ruleset(rows: list[JournalEntry] | None = None, path: Path = JOURNAL_PATH) -> None:
    """全部 + 新規則(v2) + 舊規則(legacy) 分開印勝率。"""
    rows = rows if rows is not None else load_journal(path)
    print_stats(compute_stats(rows), "全部（含舊＋新）")
    v2 = rows_by_ruleset(rows, CURRENT_RULESET)
    legacy = rows_by_ruleset(rows, LEGACY_RULESET)
    print_stats(compute_stats(v2), f"新規則 {CURRENT_RULESET}（單獨勝率）")
    if legacy:
        print_stats(compute_stats(legacy), f"舊規則 {LEGACY_RULESET}（對照）")
    else:
        print(f"（尚無 {LEGACY_RULESET} 舊紀錄）\n")


def print_journal(rows: list[JournalEntry] | None = None, limit: int = 30) -> None:
    rows = rows if rows is not None else load_journal()
    rows = list(reversed(rows))[:limit]
    print()
    print(f"══ 訊號日記（最近 {len(rows)} 筆）══")
    print(
        f"{'時間':<19} {'幣種':<12} {'邊':<4} {'狀態':<8} "
        f"{'規則':<6} {'計畫RR':>6} {'實現R':>7} {'入場':>10}"
    )
    print("-" * 100)
    for r in rows:
        side = "多" if r.side == "long" else "空"
        st_map = {"open": "未結算", "win": "勝", "loss": "負", "timeout": "超時"}
        st = st_map.get(r.status, r.status)
        rr = f"{r.planned_rr:.1f}" if r.planned_rr else "-"
        real = f"{r.realized_r:+.2f}" if r.status != "open" else "-"
        rs = (r.ruleset or LEGACY_RULESET)[:6]
        print(
            f"{r.ts:<19} {r.symbol:<12} {side:<4} {st:<8} "
            f"{rs:<6} {rr:>6} {real:>7} {r.entry:>10.6g}"
        )
    print()
