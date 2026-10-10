#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
訊號日記與事後追蹤
- YES：可進場訊號（入場／停損／止盈）→ 先碰 SL/TP 結算
- 規劃：BTC／ETH 日內 4H 規劃 → 盤整看箱體是否成立；有方向則同 YES 結算
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
# 同幣同規劃時段去重（避免同槽重複寫）
PLAN_DEDUP_SECONDS = 2 * 3600
# 結算最長觀察根數（15m × 96 ≈ 24h；4H 規劃另縮）
DEFAULT_MAX_BARS = 96
PLAN_MAX_BARS_4H = 18  # 4H × 18 ≈ 3 天
# 同根同時碰到 SL/TP 時採保守：算停損
CONSERVATIVE_SAME_BAR = True


@dataclass
class JournalEntry:
    id: str
    ts: str  # ISO 記錄時間
    signal_time_ms: int  # 訊號當下（通常用記錄時）
    symbol: str
    side: str  # long / short / none
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
    kind: str = "yes"  # yes | plan
    zone_lo: float = 0.0
    zone_hi: float = 0.0
    plan_label: str = ""  # 下午規劃／晚盤規劃…
    mtf_note: str = ""

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
            kind="yes",
            zone_lo=float(getattr(s, "zone_lo", 0) or 0),
            zone_hi=float(getattr(s, "zone_hi", 0) or 0),
            mtf_note=str(getattr(s, "mtf_note", "") or ""),
        )
        append_entry(je, path)
        added.append(je)
        rows.append(je)
    if added and not quiet:
        print(f"已記錄 {len(added)} 筆 YES 訊號 → {path.name}")
    return added


def _recent_plan_duplicate(
    rows: list[JournalEntry],
    symbol: str,
    plan_label: str,
    now_ms: int,
    cooldown: int = PLAN_DEDUP_SECONDS,
) -> bool:
    for r in reversed(rows):
        if (r.kind or "yes") != "plan":
            continue
        if r.symbol != symbol or (r.plan_label or "") != plan_label:
            continue
        if now_ms - r.signal_time_ms < cooldown * 1000:
            return True
        break
    return False


def record_plan_signals(
    setups: list[Any],
    *,
    interval: str = "4h",
    plan_label: str = "",
    note_extra: str = "",
    path: Path = JOURNAL_PATH,
    quiet: bool = False,
) -> list[JournalEntry]:
    """記錄 BTC／ETH 日內規劃（含盤整），供事後檢驗規劃準否。"""
    rows = load_journal(path)
    now_ms = _now_ms()
    added: list[JournalEntry] = []
    for s in setups:
        symbol = getattr(s, "symbol", "")
        if not symbol:
            continue
        if _recent_plan_duplicate(rows + added, symbol, plan_label, now_ms):
            continue
        structure = str(getattr(s, "structure", "") or "")
        side = getattr(s, "side", "none")
        if side not in ("long", "short"):
            if structure == "偏多":
                side = "long"
            elif structure == "偏空":
                side = "short"
            else:
                side = "none"
        zone_lo = float(getattr(s, "zone_lo", 0) or 0)
        zone_hi = float(getattr(s, "zone_hi", 0) or 0)
        entry = float(getattr(s, "entry", 0) or 0)
        stop = float(getattr(s, "stop", 0) or 0)
        tp = float(getattr(s, "tp", 0) or 0)
        # 盤整／無完整進出場：用箱體上下緣結算「盤整是否成立」
        if side == "none" or stop <= 0 or tp <= 0 or abs(entry - stop) <= 0:
            side = "none"
            if zone_lo > 0 and zone_hi > zone_lo:
                entry = (zone_lo + zone_hi) / 2
                stop = zone_lo
                tp = zone_hi
        note = note_extra.strip()
        mtf = str(getattr(s, "mtf_note", "") or "")
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
            structure=structure,
            missing_steps=str(getattr(s, "missing_steps", "") or ""),
            status="open",
            note=note,
            ruleset=CURRENT_RULESET,
            kind="plan",
            zone_lo=zone_lo,
            zone_hi=zone_hi,
            plan_label=plan_label,
            mtf_note=mtf,
        )
        append_entry(je, path)
        added.append(je)
        rows.append(je)
    if added and not quiet:
        print(f"已記錄 {len(added)} 筆規劃 → {path.name}（{plan_label or interval}）")
    return added


def _candle_accessors() -> tuple[Callable[[Any], int], Callable[[Any], float], Callable[[Any], float], Callable[[Any], float]]:
    def _t(c: Any) -> int:
        return int(getattr(c, "time", None) or c.get("time") or 0)

    def _h(c: Any) -> float:
        return float(getattr(c, "high", None) or c.get("high") or 0)

    def _l(c: Any) -> float:
        return float(getattr(c, "low", None) or c.get("low") or 0)

    def _c(c: Any) -> float:
        return float(getattr(c, "close", None) or c.get("close") or 0)

    return _t, _h, _l, _c


def resolve_plan_box(
    entry: JournalEntry,
    candles: list[Any],
    *,
    max_bars: int = PLAN_MAX_BARS_4H,
) -> JournalEntry:
    """
    盤整規劃結算：
    - 觀察期內價格大致留在箱體 → win（盤整判斷成立）
    - 提前向上／向下破箱 → loss（並註明方向）
    """
    if entry.status != "open":
        return entry
    _t, _h, _l, _c = _candle_accessors()
    after = [c for c in candles if _t(c) > entry.signal_time_ms]
    if not after:
        return entry

    lo = entry.zone_lo if entry.zone_lo > 0 else entry.stop
    hi = entry.zone_hi if entry.zone_hi > 0 else entry.tp
    if lo <= 0 or hi <= lo:
        entry.status = "timeout"
        entry.note = ((entry.note + "｜") if entry.note else "") + "無有效箱體，無法結算規劃"
        return entry

    pad = (hi - lo) * 0.02  # 允許 2% 箱寬緩衝，避免假突破
    window = after[:max_bars]
    for i, c in enumerate(window, 1):
        if _h(c) > hi + pad:
            entry.status = "loss"
            entry.exit_price = hi
            entry.exit_time_ms = _t(c)
            entry.bars_held = i
            entry.realized_r = 0.0
            entry.note = ((entry.note + "｜") if entry.note else "") + "箱體向上突破｜盤整規劃偏離"
            return entry
        if _l(c) < lo - pad:
            entry.status = "loss"
            entry.exit_price = lo
            entry.exit_time_ms = _t(c)
            entry.bars_held = i
            entry.realized_r = 0.0
            entry.note = ((entry.note + "｜") if entry.note else "") + "箱體向下突破｜盤整規劃偏離"
            return entry

    last = window[-1]
    entry.status = "win"
    entry.exit_price = _c(last)
    entry.exit_time_ms = _t(last)
    entry.bars_held = len(window)
    entry.realized_r = 0.0
    entry.note = (
        ((entry.note + "｜") if entry.note else "")
        + f"觀察期內仍在箱體｜盤整規劃成立({len(window)}根)"
    )
    return entry


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

    kind = entry.kind or "yes"
    # 規劃＋盤整／無方向：走箱體結算
    if kind == "plan" and (
        entry.side not in ("long", "short")
        or entry.structure == "盤整"
        or entry.risk() <= 0
    ):
        bars = PLAN_MAX_BARS_4H if (entry.interval or "").lower() in {"4h", "1h"} else max_bars
        return resolve_plan_box(entry, candles, max_bars=bars)

    _t, _h, _l, _c = _candle_accessors()

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
            iv = e.interval or "15m"
            use_bars = (
                PLAN_MAX_BARS_4H
                if (e.kind or "yes") == "plan" and iv.lower() in {"4h", "1h"}
                else max_bars
            )
            # 多抓一些以覆蓋訊號後時間
            raw = fetch_klines_fn(e.symbol, iv, min(use_bars + 50, 200))
            candles = parse_fn(raw)
            before = e.status
            rows[i] = resolve_entry(e, candles, max_bars=use_bars)
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


def compute_stats(
    rows: list[JournalEntry] | None = None,
    path: Path = JOURNAL_PATH,
    *,
    kind: str | None = None,
) -> JournalStats:
    rows = rows if rows is not None else load_journal(path)
    if kind is not None:
        rows = [r for r in rows if (r.kind or "yes") == kind]
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
    """YES／規劃分開；v2 為主（legacy 僅在有資料時對照）。"""
    rows = rows if rows is not None else load_journal(path)
    v2 = rows_by_ruleset(rows, CURRENT_RULESET)
    yes_v2 = [r for r in v2 if (r.kind or "yes") == "yes"]
    plan_v2 = [r for r in v2 if (r.kind or "yes") == "plan"]
    print_stats(compute_stats(yes_v2), f"YES · {CURRENT_RULESET}")
    print_stats(compute_stats(plan_v2), f"規劃 · {CURRENT_RULESET}（盤整勝=箱體成立）")
    legacy = rows_by_ruleset(rows, LEGACY_RULESET)
    if legacy:
        print_stats(compute_stats(legacy), f"舊規則 {LEGACY_RULESET}（對照）")


def print_journal(rows: list[JournalEntry] | None = None, limit: int = 30) -> None:
    rows = rows if rows is not None else load_journal()
    rows = list(reversed(rows))[:limit]
    print()
    print(f"══ 訊號日記（最近 {len(rows)} 筆）══")
    print(
        f"{'時間':<19} {'類型':<4} {'幣種':<12} {'邊':<4} {'狀態':<8} "
        f"{'規則':<6} {'計畫RR':>6} {'實現R':>7}"
    )
    print("-" * 100)
    for r in rows:
        if r.side == "long":
            side = "多"
        elif r.side == "short":
            side = "空"
        else:
            side = "整"
        st_map = {"open": "未結算", "win": "勝", "loss": "負", "timeout": "超時"}
        st = st_map.get(r.status, r.status)
        rr = f"{r.planned_rr:.1f}" if r.planned_rr else "-"
        real = f"{r.realized_r:+.2f}" if r.status != "open" else "-"
        rs = (r.ruleset or LEGACY_RULESET)[:6]
        kind = "規劃" if (r.kind or "yes") == "plan" else "YES"
        print(
            f"{r.ts:<19} {kind:<4} {r.symbol:<12} {side:<4} {st:<8} "
            f"{rs:<6} {rr:>6} {real:>7}"
        )
        if (r.kind or "yes") == "plan" and (r.plan_label or r.zone_lo):
            z = ""
            if r.zone_lo and r.zone_hi:
                z = f" 箱{r.zone_lo:.4g}-{r.zone_hi:.4g}"
            print(f"{'':19}  └ {r.plan_label or '規劃'} {r.structure}{z}")
    print()
