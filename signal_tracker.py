#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
訊號日記與事後追蹤
- YES：可進場訊號 → 先碰 SL/TP；有利達 0.8R 後移動停損至成本
- 規劃 v3：做多需日／4H 同向；盤整用 15m 收盤破箱（非影線）
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
PLAN_RULESET = "v3"
LEGACY_RULESET = "legacy"

# 同一幣種同方向去重冷卻（秒）
DEDUP_SECONDS = 4 * 3600
# 同幣同規劃時段去重（避免同槽重複寫）
PLAN_DEDUP_SECONDS = 2 * 3600
# 結算最長觀察根數（15m × 96 ≈ 24h；規劃另縮）
DEFAULT_MAX_BARS = 96
PLAN_MAX_BARS_15M = 32  # 15m × 32 ≈ 8 小時
PLAN_MAX_BARS_4H = 18  # 舊版 4H 規劃
# 同根同時碰到 SL/TP 時採保守：算停損
CONSERVATIVE_SAME_BAR = True
# YES／等回測：有利偏移達此 R 後，停損移到成本（減少先賺後吐）
TRAIL_BE_AT_R = 0.8
# 日記備註標記：已推過「請手動保本」提醒，避免重複刷屏
BE_HINT_TAG = "已提示保本"


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
    kind: str = "yes"  # yes | retest | plan | fade
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
    *,
    kind: str = "yes",
    cooldown: int = DEDUP_SECONDS,
) -> bool:
    for r in reversed(rows):
        if (r.kind or "yes") != kind:
            continue
        if r.symbol != symbol or r.side != side:
            continue
        if now_ms - r.signal_time_ms < cooldown * 1000:
            return True
        break
    return False


def _setup_trade_side(s: Any) -> str:
    side = getattr(s, "side", "none")
    if side in ("long", "short"):
        return side
    structure = str(getattr(s, "structure", "") or "")
    if structure == "偏多":
        return "long"
    if structure == "偏空":
        return "short"
    return "none"


def _blocks_long_htf(s: Any) -> bool:
    """做多需日線偏多；有 4H 時也要偏多。"""
    if _setup_trade_side(s) != "long":
        return False
    mtf = getattr(s, "mtf", None) or {}
    if mtf.get("1D") != "偏多":
        return True
    if "4H" in mtf and mtf.get("4H") != "偏多":
        return True
    blob = f"{getattr(s, 'mtf_note', '') or ''}｜{getattr(s, 'note', '') or ''}"
    return any(
        tag in blob
        for tag in ("禁逆日線做多", "做多需日線偏多", "做多需4H偏多")
    )


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
        side = _setup_trade_side(s)
        if side not in ("long", "short"):
            continue
        if _blocks_long_htf(s):
            continue
        entry = float(getattr(s, "entry", 0) or 0)
        stop = float(getattr(s, "stop", 0) or 0)
        tp = float(getattr(s, "tp", 0) or 0)
        if entry <= 0 or stop <= 0 or tp <= 0:
            continue
        if abs(entry - stop) <= 0:
            continue
        symbol = getattr(s, "symbol", "")
        if _recent_duplicate(rows + added, symbol, side, now_ms, kind="yes"):
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


def record_fade_signals(
    setups: list[Any],
    *,
    interval: str = "15m",
    path: Path = JOURNAL_PATH,
    quiet: bool = False,
) -> list[JournalEntry]:
    """日空＋4H 反抽＋15m 高位見頂空訊（獨立 kind=fade）。"""
    rows = load_journal(path)
    now_ms = _now_ms()
    added: list[JournalEntry] = []
    for s in setups:
        if not getattr(s, "fade_ready", False):
            continue
        if getattr(s, "trigger_ready", False):
            continue  # 已是 YES 就別重複記 fade
        side = getattr(s, "side", "none")
        if side != "short":
            continue
        entry = float(getattr(s, "entry", 0) or 0)
        stop = float(getattr(s, "stop", 0) or 0)
        tp = float(getattr(s, "tp", 0) or 0)
        if entry <= 0 or stop <= 0 or tp <= 0 or abs(entry - stop) <= 0:
            continue
        symbol = getattr(s, "symbol", "")
        if _recent_duplicate(rows + added, symbol, side, now_ms, kind="fade"):
            continue
        # 同幣若剛記過 YES 空也不重複
        if _recent_duplicate(rows + added, symbol, side, now_ms, kind="yes"):
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
            note="日空反抽見頂",
            ruleset=CURRENT_RULESET,
            kind="fade",
            zone_lo=float(getattr(s, "zone_lo", 0) or 0),
            zone_hi=float(getattr(s, "zone_hi", 0) or 0),
            mtf_note=str(getattr(s, "mtf_note", "") or ""),
        )
        append_entry(je, path)
        added.append(je)
        rows.append(je)
    if added and not quiet:
        print(f"已記錄 {len(added)} 筆見頂空訊 → {path.name}")
    return added


def record_base_signals(
    setups: list[Any],
    *,
    interval: str = "15m",
    path: Path = JOURNAL_PATH,
    quiet: bool = False,
) -> list[JournalEntry]:
    """日多＋4H 回檔＋15m 低位見底多訊（獨立 kind=base）。"""
    rows = load_journal(path)
    now_ms = _now_ms()
    added: list[JournalEntry] = []
    for s in setups:
        if not getattr(s, "base_ready", False):
            continue
        if getattr(s, "trigger_ready", False) or getattr(s, "fade_ready", False):
            continue
        side = getattr(s, "side", "none")
        if side != "long":
            continue
        entry = float(getattr(s, "entry", 0) or 0)
        stop = float(getattr(s, "stop", 0) or 0)
        tp = float(getattr(s, "tp", 0) or 0)
        if entry <= 0 or stop <= 0 or tp <= 0 or abs(entry - stop) <= 0:
            continue
        symbol = getattr(s, "symbol", "")
        if _recent_duplicate(rows + added, symbol, side, now_ms, kind="base"):
            continue
        if _recent_duplicate(rows + added, symbol, side, now_ms, kind="yes"):
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
            note="日多回檔見底",
            ruleset=CURRENT_RULESET,
            kind="base",
            zone_lo=float(getattr(s, "zone_lo", 0) or 0),
            zone_hi=float(getattr(s, "zone_hi", 0) or 0),
            mtf_note=str(getattr(s, "mtf_note", "") or ""),
        )
        append_entry(je, path)
        added.append(je)
        rows.append(je)
    if added and not quiet:
        print(f"已記錄 {len(added)} 筆見底多訊 → {path.name}")
    return added


def record_retest_signals(
    setups: list[Any],
    *,
    interval: str = "15m",
    path: Path = JOURNAL_PATH,
    quiet: bool = False,
) -> list[JournalEntry]:
    """
    記錄「靠近區・等回測」計畫單（尚未可扣板機）。
    有完整入場／停損／止盈才入帳，結算方式同 YES（含 0.8R 保本）。
    """
    rows = load_journal(path)
    now_ms = _now_ms()
    added: list[JournalEntry] = []
    for s in setups:
        if getattr(s, "trigger_ready", False):
            continue
        if not getattr(s, "waiting_retest", False):
            continue
        if not bool(getattr(s, "mtf_aligned", True)):
            continue
        side = _setup_trade_side(s)
        if side not in ("long", "short"):
            continue
        if _blocks_long_htf(s):
            continue
        entry = float(getattr(s, "entry", 0) or 0)
        stop = float(getattr(s, "stop", 0) or 0)
        tp = float(getattr(s, "tp", 0) or 0)
        if entry <= 0 or stop <= 0 or tp <= 0 or abs(entry - stop) <= 0:
            continue
        symbol = getattr(s, "symbol", "")
        if not symbol:
            continue
        if _recent_duplicate(rows + added, symbol, side, now_ms, kind="retest"):
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
            structure=str(getattr(s, "structure", "") or ""),
            missing_steps=str(getattr(s, "missing_steps", "") or ""),
            status="open",
            note="等回測・計畫參考",
            ruleset=CURRENT_RULESET,
            kind="retest",
            zone_lo=float(getattr(s, "zone_lo", 0) or 0),
            zone_hi=float(getattr(s, "zone_hi", 0) or 0),
            mtf_note=str(getattr(s, "mtf_note", "") or ""),
        )
        append_entry(je, path)
        added.append(je)
        rows.append(je)
    if added and not quiet:
        print(f"已記錄 {len(added)} 筆等回測 → {path.name}")
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


def _plan_directional_side(s: Any) -> str:
    """
    只有高週期同向、且已到區／等回測／可進，才記成方向單。
    做多需日／4H 同向；未到區改走箱體檢驗。
    """
    structure = str(getattr(s, "structure", "") or "")
    if structure not in ("偏多", "偏空"):
        return "none"
    mtf = getattr(s, "mtf", None) or {}
    mtf_note = str(getattr(s, "mtf_note", "") or "")
    note = str(getattr(s, "note", "") or "")
    if structure == "偏多" and (
        mtf.get("1D") != "偏多"
        or ("4H" in mtf and mtf.get("4H") != "偏多")
        or any(
            tag in mtf_note or tag in note
            for tag in ("禁逆日線做多", "做多需日線偏多", "做多需4H偏多")
        )
    ):
        return "none"
    if (not bool(getattr(s, "mtf_aligned", True))) or ("逆高週期" in mtf_note):
        return "none"
    ready = bool(
        getattr(s, "trigger_ready", False)
        or getattr(s, "at_zone", False)
        or getattr(s, "waiting_retest", False)
    )
    if not ready:
        return "none"
    side = getattr(s, "side", "none")
    if side not in ("long", "short"):
        side = "long" if structure == "偏多" else "short"
    return side if side in ("long", "short") else "none"


def record_plan_signals(
    setups: list[Any],
    *,
    interval: str = "15m",
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
        zone_lo = float(getattr(s, "zone_lo", 0) or 0)
        zone_hi = float(getattr(s, "zone_hi", 0) or 0)
        entry = float(getattr(s, "entry", 0) or 0)
        stop = float(getattr(s, "stop", 0) or 0)
        tp = float(getattr(s, "tp", 0) or 0)
        side = _plan_directional_side(s)
        planned_rr = float(getattr(s, "rr", 0) or 0)
        note = note_extra.strip()
        # 非方向單：用真實今日高低檢驗箱體
        if side == "none" or stop <= 0 or tp <= 0 or abs(entry - stop) <= 0:
            side = "none"
            planned_rr = 0.0
            day_lo = float(getattr(s, "plan_day_lo", 0) or 0)
            day_hi = float(getattr(s, "plan_day_hi", 0) or 0)
            if day_hi > day_lo > 0:
                zone_lo, zone_hi = day_lo, day_hi
            if zone_lo > 0 and zone_hi > zone_lo:
                entry = (zone_lo + zone_hi) / 2
                stop = zone_lo
                tp = zone_hi
            src_note = str(getattr(s, "note", "") or "")
            mtf_note = str(getattr(s, "mtf_note", "") or "")
            if any(
                tag in src_note or tag in mtf_note
                for tag in ("禁逆日線", "做多需日線", "做多需4H")
            ):
                extra = "做多未齊日／4H，改看箱"
            elif structure in ("偏多", "偏空"):
                extra = "未到區，改以今日高低檢驗"
            else:
                extra = ""
            if extra:
                note = f"{note}｜{extra}" if note else extra
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
            planned_rr=planned_rr,
            score=float(getattr(s, "setup_score", 0) or 0),
            structure=structure,
            missing_steps=str(getattr(s, "missing_steps", "") or ""),
            status="open",
            note=note,
            ruleset=PLAN_RULESET,
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


def _plan_box_break_pad(lo: float, hi: float) -> float:
    mid = (lo + hi) / 2.0
    width = hi - lo
    return max(width * 0.12, mid * 0.0015)


def resolve_plan_box(
    entry: JournalEntry,
    candles: list[Any],
    *,
    max_bars: int = PLAN_MAX_BARS_15M,
) -> JournalEntry:
    """
    盤整規劃結算（v3）：看收盤，不看影線。
    收盤站出箱外（含緩衝）→ loss；走完仍在箱內 → win。
    """
    if entry.status != "open":
        return entry
    _t, _h, _l, _c = _candle_accessors()
    del _h, _l
    after = [c for c in candles if _t(c) > entry.signal_time_ms]
    if not after:
        return entry

    lo = entry.zone_lo if entry.zone_lo > 0 else entry.stop
    hi = entry.zone_hi if entry.zone_hi > 0 else entry.tp
    if lo <= 0 or hi <= lo:
        entry.status = "timeout"
        entry.note = ((entry.note + "｜") if entry.note else "") + "無有效箱體，無法結算規劃"
        return entry

    pad = _plan_box_break_pad(lo, hi)

    def _mark_break(i: int, c: Any, way: str) -> JournalEntry:
        entry.status = "loss"
        entry.exit_price = hi if way == "up" else lo
        entry.exit_time_ms = _t(c)
        entry.bars_held = i
        entry.realized_r = 0.0
        tag = "收盤向上破箱｜盤整規劃偏離" if way == "up" else "收盤向下破箱｜盤整規劃偏離"
        entry.note = ((entry.note + "｜") if entry.note else "") + tag
        return entry

    def _broke(close: float) -> str:
        if close > hi + pad:
            return "up"
        if close < lo - pad:
            return "down"
        return ""

    if len(after) < max_bars:
        for i, c in enumerate(after, 1):
            way = _broke(_c(c))
            if way:
                return _mark_break(i, c, way)
        entry.bars_held = len(after)
        return entry

    window = after[:max_bars]
    for i, c in enumerate(window, 1):
        way = _broke(_c(c))
        if way:
            return _mark_break(i, c, way)

    last = window[-1]
    entry.status = "win"
    entry.exit_price = _c(last)
    entry.exit_time_ms = _t(last)
    entry.bars_held = len(window)
    entry.realized_r = 0.0
    entry.note = (
        ((entry.note + "｜") if entry.note else "")
        + f"觀察期內收盤仍在箱體｜盤整規劃成立({len(window)}根)"
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
    YES：有利達 TRAIL_BE_AT_R 後，停損移到成本（保本出場不算 −1R）。
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
        iv = (entry.interval or "").lower()
        if iv in {"4h", "1h"}:
            bars = PLAN_MAX_BARS_4H
        elif iv in {"15m", "5m"}:
            bars = PLAN_MAX_BARS_15M
        else:
            bars = max_bars
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

    # 觀察期未滿且尚未觸及 SL/TP → 保持 open
    observing = after if len(after) < max_bars else after[:max_bars]
    mfe = 0.0
    mae = 0.0
    # 等回測的 entry 是區間中軸，不是訊號當下成交價。沒碰到就不算進場。
    filled = kind != "retest"
    # 動態停損：有利達 0.8R 後移到成本；R 仍用原始風險計算
    cur_stop = entry.stop
    trailed = False
    use_trail = (kind or "yes") in ("yes", "retest", "fade", "base") and TRAIL_BE_AT_R > 0

    for i, c in enumerate(observing, 1):
        hi, lo = _h(c), _l(c)
        if not filled:
            touched = lo <= entry.entry if entry.side == "long" else hi >= entry.entry
            if not touched:
                continue
            filled = True
            if "已碰到計畫價" not in (entry.note or ""):
                tag = "已碰到計畫價"
                entry.note = f"{entry.note}｜{tag}" if entry.note else tag
        # 先用「進這根前」的停損結算，避免同根先創新高再回檔被誤殺
        if entry.side == "long":
            mfe = max(mfe, (hi - entry.entry) / risk)
            mae = max(mae, (entry.entry - lo) / risk)
            hit_sl = lo <= cur_stop
            hit_tp = hi >= entry.tp
            if hit_sl and hit_tp:
                if trailed:
                    entry.status = "win"
                    entry.exit_price = entry.tp
                    entry.realized_r = (entry.tp - entry.entry) / risk
                    entry.note = "同根觸及・已保本採止盈"
                elif CONSERVATIVE_SAME_BAR:
                    entry.status = "loss"
                    entry.exit_price = entry.stop
                    entry.realized_r = -1.0
                    entry.note = "同根觸及SL/TP，採保守停損"
                else:
                    entry.status = "win"
                    entry.exit_price = entry.tp
                    entry.realized_r = (entry.tp - entry.entry) / risk
                    entry.note = "同根觸及，採止盈"
                entry.exit_time_ms = _t(c)
                entry.bars_held = i
                entry.mfe_r = round(mfe, 3)
                entry.mae_r = round(mae, 3)
                return entry
            if hit_sl:
                if trailed:
                    entry.status = "timeout"
                    entry.exit_price = entry.entry
                    entry.realized_r = 0.0
                    entry.note = f"有利≥{TRAIL_BE_AT_R:g}R後移動停損至成本"
                else:
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
            hit_sl = hi >= cur_stop
            hit_tp = lo <= entry.tp
            if hit_sl and hit_tp:
                if trailed:
                    entry.status = "win"
                    entry.exit_price = entry.tp
                    entry.realized_r = (entry.entry - entry.tp) / risk
                    entry.note = "同根觸及・已保本採止盈"
                elif CONSERVATIVE_SAME_BAR:
                    entry.status = "loss"
                    entry.exit_price = entry.stop
                    entry.realized_r = -1.0
                    entry.note = "同根觸及SL/TP，採保守停損"
                else:
                    entry.status = "win"
                    entry.exit_price = entry.tp
                    entry.realized_r = (entry.entry - entry.tp) / risk
                    entry.note = "同根觸及，採止盈"
                entry.exit_time_ms = _t(c)
                entry.bars_held = i
                entry.mfe_r = round(mfe, 3)
                entry.mae_r = round(mae, 3)
                return entry
            if hit_sl:
                if trailed:
                    entry.status = "timeout"
                    entry.exit_price = entry.entry
                    entry.realized_r = 0.0
                    entry.note = f"有利≥{TRAIL_BE_AT_R:g}R後移動停損至成本"
                else:
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

        # 這根走完仍在場，有利已達門檻 → 下一根起保本
        if use_trail and (not trailed) and mfe >= TRAIL_BE_AT_R:
            cur_stop = entry.entry
            trailed = True

    if kind == "retest" and not filled:
        entry.mfe_r = 0.0
        entry.mae_r = 0.0
        entry.bars_held = len(observing)
        if len(after) < max_bars:
            return entry
        last = observing[-1]
        entry.status = "timeout"
        entry.exit_price = 0.0
        entry.exit_time_ms = _t(last)
        entry.realized_r = 0.0
        tag = "未回到計畫價，未進場"
        entry.note = f"{entry.note}｜{tag}" if entry.note else tag
        return entry

    entry.mfe_r = round(mfe, 3)
    entry.mae_r = round(mae, 3)
    entry.bars_held = len(observing)
    if trailed and "移動停損" not in (entry.note or "") and "停損移至成本" not in (entry.note or ""):
        tag = f"已達{TRAIL_BE_AT_R:g}R・停損移至成本"
        entry.note = f"{entry.note}｜{tag}" if entry.note else tag

    # 觀察期未滿：維持未結算
    if len(after) < max_bars:
        return entry

    # 超時：滿 max_bars 仍未碰 SL/TP，以最後收盤結算
    last = observing[-1]
    cl = _c(last)
    if entry.side == "long":
        realized = (cl - entry.entry) / risk
    else:
        realized = (entry.entry - cl) / risk
    entry.status = "timeout"
    entry.exit_price = cl
    entry.exit_time_ms = _t(last)
    entry.realized_r = round(realized, 3)
    entry.note = f"觀察期满({len(observing)}根)按收盤結算"
    return entry


def _mfe_from_candles(entry: JournalEntry, candles: list[Any]) -> float:
    """訊號後最大有利偏移（以原始風險為 1R）。"""
    risk = entry.risk()
    if risk <= 0 or entry.side not in ("long", "short"):
        return 0.0
    _t, _h, _l, _c = _candle_accessors()
    del _c
    after = [c for c in candles if _t(c) > entry.signal_time_ms]
    mfe = 0.0
    for c in after:
        if entry.side == "long":
            mfe = max(mfe, (_h(c) - entry.entry) / risk)
        else:
            mfe = max(mfe, (entry.entry - _l(c)) / risk)
    return mfe


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
            iv = (e.interval or "15m").lower()
            kind = e.kind or "yes"
            if kind == "plan" and iv in {"4h", "1h"}:
                use_bars = PLAN_MAX_BARS_4H
            elif kind == "plan" and iv in {"15m", "5m"}:
                use_bars = PLAN_MAX_BARS_15M
            else:
                use_bars = max_bars
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


def collect_breakeven_hints(
    fetch_klines_fn: Callable[[str, str, int], list],
    parse_fn: Callable[[list], list],
    *,
    path: Path = JOURNAL_PATH,
    max_bars: int = DEFAULT_MAX_BARS,
) -> list[JournalEntry]:
    """
    找出未結算 YES／等回測：有利已達 TRAIL_BE_AT_R、尚未提示過保本。
    標記 BE_HINT_TAG 後存檔，回傳本輪要推播的筆數清單。
    """
    if TRAIL_BE_AT_R <= 0:
        return []
    rows = load_journal(path)
    hints: list[JournalEntry] = []
    changed = False
    for i, e in enumerate(rows):
        if e.status != "open":
            continue
        if (e.kind or "yes") not in ("yes", "retest", "fade", "base"):
            continue
        if (e.kind or "yes") == "retest" and "已碰到計畫價" not in (e.note or ""):
            continue
        if e.side not in ("long", "short"):
            continue
        if BE_HINT_TAG in (e.note or ""):
            continue
        if e.risk() <= 0:
            continue
        try:
            iv = (e.interval or "15m").lower()
            raw = fetch_klines_fn(e.symbol, iv, min(max_bars + 50, 200))
            candles = parse_fn(raw)
            mfe = _mfe_from_candles(e, candles)
            e.mfe_r = round(mfe, 3)
            if mfe < TRAIL_BE_AT_R:
                rows[i] = e
                continue
            tag = f"{BE_HINT_TAG}（MFE {mfe:.2f}R）"
            e.note = f"{e.note}｜{tag}" if e.note else tag
            rows[i] = e
            hints.append(e)
            changed = True
        except Exception:
            continue
    if changed:
        save_journal(rows, path)
    return hints


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
    """YES 用現行規則；規劃用 v3。舊規劃僅在有資料時對照。"""
    rows = rows if rows is not None else load_journal(path)
    yes_rows = [
        r for r in rows_by_ruleset(rows, CURRENT_RULESET) if (r.kind or "yes") == "yes"
    ]
    retest_rows = [
        r for r in rows_by_ruleset(rows, CURRENT_RULESET) if (r.kind or "") == "retest"
    ]
    fade_rows = [
        r for r in rows_by_ruleset(rows, CURRENT_RULESET) if (r.kind or "") == "fade"
    ]
    base_rows = [
        r for r in rows_by_ruleset(rows, CURRENT_RULESET) if (r.kind or "") == "base"
    ]
    plan_rows = [
        r for r in rows_by_ruleset(rows, PLAN_RULESET) if (r.kind or "yes") == "plan"
    ]
    print_stats(compute_stats(yes_rows), f"YES · {CURRENT_RULESET}（0.8R後保本）")
    print_stats(compute_stats(retest_rows), f"等回測 · {CURRENT_RULESET}")
    print_stats(compute_stats(fade_rows), f"日空見頂 · {CURRENT_RULESET}")
    print_stats(compute_stats(base_rows), f"日多見底 · {CURRENT_RULESET}")
    print_stats(
        compute_stats(plan_rows),
        f"規劃 · {PLAN_RULESET}（做多需日／4H｜收盤破箱）",
    )
    if PLAN_RULESET != CURRENT_RULESET:
        old_plans = [
            r for r in rows_by_ruleset(rows, CURRENT_RULESET) if (r.kind or "yes") == "plan"
        ]
        if old_plans:
            print_stats(compute_stats(old_plans), f"規劃 · {CURRENT_RULESET}（舊，對照）")
    legacy = rows_by_ruleset(rows, LEGACY_RULESET)
    if legacy:
        print_stats(compute_stats(legacy), f"舊規則 {LEGACY_RULESET}（對照）")


def _fmt_journal_px(v: float) -> str:
    if not v or v <= 0:
        return "-"
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def plan_level_text(r: JournalEntry) -> str:
    """規劃用：方向單給一組價；盤整給低多／高空。"""
    if r.side in ("long", "short") and r.entry > 0 and r.stop > 0 and r.tp > 0:
        return (
            f"進{_fmt_journal_px(r.entry)} "
            f"損{_fmt_journal_px(r.stop)} "
            f"盈{_fmt_journal_px(r.tp)}"
        )
    lo = r.zone_lo if r.zone_lo > 0 else r.stop
    hi = r.zone_hi if r.zone_hi > lo else r.tp
    if lo <= 0 or hi <= lo:
        return ""
    mid = (lo + hi) / 2.0
    pad = max((hi - lo) * 0.08, mid * 0.0006)
    return (
        f"低多 進{_fmt_journal_px(lo)} 損{_fmt_journal_px(lo - pad)} 盈{_fmt_journal_px(hi)}"
        f"｜高空 進{_fmt_journal_px(hi)} 損{_fmt_journal_px(hi + pad)} 盈{_fmt_journal_px(lo)}"
    )


def _exit_hhmm(r: JournalEntry) -> str:
    """平倉時刻：觸發那根 K 的開盤（台灣時間）。未平為 —。"""
    if r.status == "open" or not r.exit_time_ms:
        return "—"
    return datetime.fromtimestamp(r.exit_time_ms / 1000, TZ_TW).strftime("%H:%M")


def _awaiting_retest_fill(r: JournalEntry) -> bool:
    """等回測且價格還沒碰到計畫進場。"""
    if (r.kind or "") != "retest":
        return False
    note = r.note or ""
    if "未進場" in note:
        return True
    if "已碰到計畫價" in note:
        return False
    return r.status == "open"


def _trade_result_text(r: JournalEntry) -> str:
    if _awaiting_retest_fill(r):
        return "尚未進場"
    if r.status == "open":
        return "未平"
    if r.status == "timeout":
        return "超時 0R"
    return f"{r.realized_r:+.2f}R"


def _trade_side_text(side: str) -> str:
    if side == "long":
        return "多"
    if side == "short":
        return "空"
    return "盤"


def format_trade_record_lines(r: JournalEntry) -> list[str]:
    """一筆日記：開倉→平倉、方向、結果，下一行進／損／盈。等回測標成計畫價。"""
    opened = (r.ts or "")[11:16] or "—"
    name = (r.symbol or "").replace("USDT", "")
    side = _trade_side_text(r.side)
    result = _trade_result_text(r)
    if (r.kind or "") == "retest" and r.entry > 0 and r.stop > 0 and r.tp > 0:
        if _awaiting_retest_fill(r):
            line = f"{opened} 等回測 {name} {side} {result}"
        else:
            line = f"{opened}→{_exit_hhmm(r)} 等回測 {name} {side} {result}"
        levels = (
            f"計畫進{_fmt_journal_px(r.entry)} "
            f"損{_fmt_journal_px(r.stop)} "
            f"盈{_fmt_journal_px(r.tp)}"
        )
        return [line, levels]
    line = f"{opened}→{_exit_hhmm(r)} {name} {side} {result}"
    levels = plan_level_text(r)
    return [line, levels] if levels else [line]


def _fmt_table_px(v: float) -> str:
    """表格用：位數收短，手機欄寬才放得下。"""
    if not v or v <= 0:
        return "—"
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 10:
        return f"{v:.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    text = f"{v:.6g}"
    if "e" in text or "E" in text:
        text = f"{v:.8f}".rstrip("0").rstrip(".")
    return text


def _row_tone(r: JournalEntry) -> str:
    if _awaiting_retest_fill(r):
        return "wait"
    if r.status == "win":
        return "win"
    if r.status == "loss":
        return "loss"
    if r.status == "timeout":
        return "timeout"
    return "open"


# 統計上的「一天」：台灣時間早上 8 點到隔天早上 8 點
DAY_START_HOUR = 8


def trading_day_bounds(
    day: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    """回傳 (起, 迄, 標籤)。起含、迄不含，格式 YYYY-MM-DD HH:MM:SS。"""
    if day:
        start = datetime.strptime(day, "%Y-%m-%d").replace(
            hour=DAY_START_HOUR, tzinfo=TZ_TW
        )
    else:
        current = now or datetime.now(TZ_TW)
        if current.tzinfo is None:
            current = current.replace(tzinfo=TZ_TW)
        start = current.replace(
            hour=DAY_START_HOUR, minute=0, second=0, microsecond=0
        )
        if current < start:
            start -= timedelta(days=1)
    end = start + timedelta(days=1)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
        start.strftime("%m-%d"),
    )


def _rows_in_trading_day(
    rows: list[JournalEntry],
    *,
    day: str | None = None,
    now: datetime | None = None,
) -> tuple[list[JournalEntry], str]:
    start, end, label = trading_day_bounds(day, now)
    picked = [
        r for r in rows if start <= (r.ts or "") < end
    ]
    picked.sort(key=lambda r: r.ts or "")
    return picked, label


def today_table_model(
    rows: list[JournalEntry],
    *,
    day: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """統計表格：早上 8 點到隔天 8 點的開倉、平倉、方向、結果、價位。"""
    today, title_day = _rows_in_trading_day(rows, day=day, now=now)
    title = f"今日紀錄 {title_day}"
    if not today:
        return {
            "title": title,
            "summary": "今天還沒有紀錄",
            "footnote": "",
            "headers": [],
            "records": [],
        }
    pending = [r for r in today if _awaiting_retest_fill(r)]
    pending_ids = {r.id for r in pending}
    closed = [r for r in today if r.status != "open" and r.id not in pending_ids]
    holding = [r for r in today if r.status == "open" and r.id not in pending_ids]
    total_r = sum(r.realized_r for r in closed)
    summary = (
        f"已平 {len(closed)} 筆 {total_r:+.2f}R｜持倉 {len(holding)}｜等回測未進 {len(pending)}"
    )
    records: list[dict[str, Any]] = []
    for r in today:
        planned = (r.kind or "") == "retest" and r.entry > 0
        entry = _fmt_table_px(r.entry)
        if planned:
            entry = f"計{entry}"
        elif r.side not in ("long", "short"):
            entry = plan_level_text(r) or entry
        records.append(
            {
                "cells": [
                    (r.ts or "")[11:16] or "—",
                    _exit_hhmm(r),
                    (r.symbol or "").replace("USDT", ""),
                    _trade_side_text(r.side),
                    _trade_result_text(r),
                    entry,
                    _fmt_table_px(r.stop),
                    _fmt_table_px(r.tp),
                ],
                "tone": _row_tone(r),
                "planned": planned,
            }
        )
    footnote = "進場前有「計」的是計畫價，不是當時成交價" if any(
        rec["planned"] for rec in records
    ) else ""
    return {
        "title": title,
        "summary": summary,
        "footnote": footnote,
        "headers": ["開倉", "平倉", "幣", "向", "結果", "進場", "停損", "止盈"],
        "records": records,
    }


def _html_text(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _disp_width(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad_cell(text: str, width: int, align: str) -> str:
    gap = width - _disp_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def format_today_table_messages(
    rows: list[JournalEntry],
    *,
    day: str | None = None,
    now: datetime | None = None,
    limit: int = 3500,
) -> list[str]:
    """統計按鈕用：可複製的文字表。不用程式碼區塊，字色跟一般訊息一樣。"""
    model = today_table_model(rows, day=day, now=now)
    title = f"📊 <b>{_html_text(model['title'])}</b>"
    head_lines = [title, _html_text(model["summary"])]
    if model["footnote"]:
        head_lines.append(_html_text(model["footnote"]))
    if not model["records"]:
        return ["\n".join(head_lines)]

    headers = [cell.replace("—", "-") for cell in model["headers"]]
    matrix = [headers] + [
        [cell.replace("—", "-") for cell in rec["cells"]] for rec in model["records"]
    ]
    aligns = ["left", "left", "left", "center", "right", "right", "right", "right"]
    widths = [
        max(_disp_width(row[i]) for row in matrix) for i in range(len(headers))
    ]

    def line(cells: list[str]) -> str:
        return " ".join(
            _pad_cell(cells[i], widths[i], aligns[i]) for i in range(len(headers))
        )

    body = [_html_text(line(row)) for row in matrix]
    head = "\n".join(head_lines)
    messages: list[str] = []
    chunk: list[str] = []
    for row in body:
        trial = chunk + [row]
        text = head + "\n" + "\n".join(trial) if not messages else "\n".join(trial)
        if chunk and len(text) > limit:
            messages.append(head + "\n" + "\n".join(chunk) if not messages else "\n".join(chunk))
            chunk = [row]
        else:
            chunk.append(row)
    if chunk:
        messages.append(head + "\n" + "\n".join(chunk) if not messages else "\n".join(chunk))
    return messages


def format_today_record_messages(
    rows: list[JournalEntry],
    *,
    day: str | None = None,
    now: datetime | None = None,
    limit: int = 3500,
) -> list[str]:
    """
    統計用文字版。一天從台灣時間早上 8 點到隔天早上 8 點。
    超過 Telegram 長度就拆成多則。
    """
    today, title_day = _rows_in_trading_day(rows, day=day, now=now)
    title = f"📊 <b>今日紀錄</b> {title_day}"
    if not today:
        return [f"{title}\n今天還沒有紀錄"]
    pending = [r for r in today if _awaiting_retest_fill(r)]
    pending_ids = {r.id for r in pending}
    closed = [r for r in today if r.status != "open" and r.id not in pending_ids]
    holding = [r for r in today if r.status == "open" and r.id not in pending_ids]
    total_r = sum(r.realized_r for r in closed)
    header = (
        f"{title}\n"
        f"已平 {len(closed)} 筆 {total_r:+.2f}R｜持倉 {len(holding)}｜等回測未進 {len(pending)}\n"
        f"等回測的「計畫進」不是當時成交價"
    )
    blocks = ["\n".join(format_trade_record_lines(r)) for r in today]
    messages: list[str] = []
    cur = header
    for block in blocks:
        piece = "\n" + block
        if len(cur) + len(piece) > limit and cur != header:
            messages.append(cur)
            cur = f"📊 <b>今日紀錄</b> {title_day}（續）" + piece
        else:
            cur += piece
    messages.append(cur)
    return messages


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
        k = r.kind or "yes"
        if k == "plan":
            kind = "規劃"
        elif k == "retest":
            kind = "等回"
        elif k == "fade":
            kind = "見頂"
        elif k == "base":
            kind = "見底"
        else:
            kind = "YES"
        print(
            f"{r.ts:<19} {kind:<4} {r.symbol:<12} {side:<4} {st:<8} "
            f"{rs:<6} {rr:>6} {real:>7}"
        )
        if k == "plan" and (r.plan_label or r.zone_lo or r.entry):
            z = ""
            if r.zone_lo and r.zone_hi:
                z = f" 箱{r.zone_lo:.4g}-{r.zone_hi:.4g}"
            levels = plan_level_text(r)
            print(f"{'':19}  └ {r.plan_label or '規劃'} {r.structure}{z}")
            if levels:
                print(f"{'':19}    {levels}")
        elif k == "retest" and (r.zone_lo or r.missing_steps):
            z = ""
            if r.zone_lo and r.zone_hi:
                z = f" 區{r.zone_lo:.4g}-{r.zone_hi:.4g}"
            miss = (r.missing_steps or "").replace("缺：", "").replace("缺:", "")
            print(f"{'':19}  └ 等回測 {r.structure}{z}" + (f"｜{miss}" if miss else ""))
    print()
