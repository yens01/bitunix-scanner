#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitunix 幣種偵測 / 下單輔助分析
- 掃描合約市場 ticker、資金費率
- 依漲跌幅、成交量、波動、資金費率評分
- 套用支撐壓力反應、第四章六問、Fibo 力道返還
- 總流程：實戰做單 11 步（高週期→結構→BOS→區→Fibo→流動性→指標→回測→SL→RR→進場）
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# 固定 UTC+8（不依賴系統 tzdata；Windows／Ubuntu Actions 都可用）
TZ_TW = timezone(timedelta(hours=8))


def now_tw() -> datetime:
    """顯示用：一律台灣時間（UTC+8），避免雲端 runner 用 UTC。"""
    return datetime.now(TZ_TW)

from sr_logic import ChecklistResult, evaluate_setup, parse_klines
from risk_manager import (
    count_open_by_side,
    filter_yes_by_risk,
    format_risk_line,
    load_risk_config,
)
from signal_tracker import (
    print_journal,
    print_stats_by_ruleset,
    record_plan_signals,
    record_yes_signals,
    resolve_all,
)

BASE = "https://fapi.bitunix.com"
TIMEOUT = 20
AUTO_RECORD_YES = True  # Setup／監看出現 YES 時自動寫入日記
LOCAL_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram.local.env")


def load_local_env(path: str = LOCAL_ENV_PATH) -> None:
    """從 telegram.local.env 讀取 TOKEN / CHAT_ID（不覆蓋已存在的環境變數）。"""
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as fp:
            for raw in fp:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and val and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


# 一啟動就載入本機 Telegram 設定
load_local_env()

# ── Setup 過濾預設（去重／降噪）────────────────────────────────────────────
DEFAULT_MIN_SETUP_VOL = 500_000      # 最低 24h 成交額 USDT
DEFAULT_MAX_RANGE_PCT = 50.0         # 排除過度瘋狂振幅（略放寬）
DEFAULT_MIN_LIST_DAYS = 7.0          # 上市未滿 N 天排除
DEFAULT_MAX_RR = 3.0                 # RR 鎖上緣（過高多半停損太近／目標過遠）
DEFAULT_MIN_RR = 1.5                 # RR 鎖下緣
DEFAULT_IDEAL_RR_LO = 1.5            # 品質排序：理想 RR 下緣
DEFAULT_IDEAL_RR_HI = 2.5            # 品質排序：理想 RR 上緣
DEFAULT_TOP_MAJOR_PER_SIDE = 2       # 大幣同向名額
DEFAULT_TOP_ALT_PER_SIDE = 3         # 山寨同向名額（略放寬）
DEFAULT_MIN_RISK_PCT = 0.4           # 風險幅度下限（停損太近易掃損）
DEFAULT_MAX_RISK_PCT = 1.0           # 風險幅度上限（過寬風險失控）
DEFAULT_NEAR_ZONE_PCT = 0.8          # 等回測：距區 ≤0.8% 才推卡片
DEFAULT_ALERT_COOLDOWN_MIN = 45      # 同一幣冷卻分鐘
DEFAULT_MAX_ALERTS_PER_WAVE = 4      # 同波最多推播檔數（YES+靠近合計）
# BTC/ETH 日內規劃時段（台灣時間）：下午四點／晚上八點／美股開盤
# 美股開盤：夏令約 21:30、冬令約 22:30；預設用 21:30（夏令），冬令可改 22:30
SESSION_PLAN_SLOTS: tuple[tuple[int, int, str], ...] = (
    (16, 0, "下午規劃"),
    (20, 0, "晚盤規劃"),
    (21, 30, "美股開盤規劃"),
)
SESSION_PLAN_INTERVAL = "4h"         # 日內規劃用 4H K 線
SESSION_PLAN_MTF = ("1d", "1w", "1M")
SESSION_PLAN_SYMBOLS = ("BTCUSDT", "ETHUSDT")
SESSION_PLAN_COOLDOWN_PREFIX = "__session_btc_eth_plan__"
SESSION_PLAN_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "session_plan_state.json"
)
# 相容舊常數名稱
EVENING_PLAN_HOUR = 16
EVENING_PLAN_INTERVAL = SESSION_PLAN_INTERVAL
COOLDOWN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alert_cooldown.json")
TELEGRAM_OFFSET_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "telegram_offset.json"
)

# 大幣組（同向名額獨立，避免被山寨擠掉）
MAJOR_SYMBOLS = {
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
    "NEARUSDT", "SUIUSDT", "TONUSDT", "TRXUSDT", "ATOMUSDT", "APTUSDT",
    "OPUSDT", "ARBUSDT", "INJUSDT", "FILUSDT", "AAVEUSDT", "UNIUSDT",
}


@dataclass
class SetupFilterConfig:
    min_quote_vol: float = DEFAULT_MIN_SETUP_VOL
    max_range_pct: float = DEFAULT_MAX_RANGE_PCT
    min_list_days: float = DEFAULT_MIN_LIST_DAYS
    max_rr: float = DEFAULT_MAX_RR
    min_rr: float = DEFAULT_MIN_RR
    ideal_rr_lo: float = DEFAULT_IDEAL_RR_LO
    ideal_rr_hi: float = DEFAULT_IDEAL_RR_HI
    top_major_per_side: int = DEFAULT_TOP_MAJOR_PER_SIDE
    top_alt_per_side: int = DEFAULT_TOP_ALT_PER_SIDE
    min_risk_pct: float = DEFAULT_MIN_RISK_PCT
    max_risk_pct: float = DEFAULT_MAX_RISK_PCT
    near_zone_pct: float = DEFAULT_NEAR_ZONE_PCT
    max_alerts_per_wave: int = DEFAULT_MAX_ALERTS_PER_WAVE
    require_mtf_aligned: bool = True
    lock_rr_band: bool = True  # YES／可推單鎖在 min_rr～max_rr


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{BASE}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"User-Agent": "BitunixScanner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"連線失敗: {e.reason}") from e

    if body.get("code") != 0:
        raise RuntimeError(f"API 錯誤: {body.get('msg', body)}")
    return body.get("data")


def fetch_tickers() -> list[dict]:
    return _get("/api/v1/futures/market/tickers") or []


def fetch_pairs() -> list[dict]:
    return _get("/api/v1/futures/market/trading_pairs") or []


def fetch_funding_batch() -> list[dict]:
    return _get("/api/v1/futures/market/funding_rate/batch") or []


def fetch_klines(symbol: str, interval: str = "15m", limit: int = 100) -> list[dict]:
    data = _get(
        "/api/v1/futures/market/kline",
        {"symbol": symbol, "interval": interval, "limit": limit},
    )
    return data or []


def fetch_ticker_last(symbol: str) -> float:
    """交易所最新成交價（比 K 線收盤更接近盤面現價）。"""
    sym = symbol.upper()
    try:
        for t in fetch_tickers():
            if (t.get("symbol") or "").upper() == sym:
                return f(t.get("lastPrice") or t.get("last"))
    except RuntimeError:
        pass
    return 0.0


# ── 數值工具 ─────────────────────────────────────────────────────────────────

def f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def pct_change(last: float, open_: float) -> float:
    if open_ == 0:
        return 0.0
    return (last - open_) / open_ * 100.0


def fmt_num(n: float, digits: int = 2) -> str:
    abs_n = abs(n)
    if abs_n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.{digits}f}B"
    if abs_n >= 1_000_000:
        return f"{n / 1_000_000:.{digits}f}M"
    if abs_n >= 1_000:
        return f"{n / 1_000:.{digits}f}K"
    if abs_n >= 1:
        return f"{n:.{digits}f}"
    return f"{n:.6g}"


def fmt_price(p: float) -> str:
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


# ── 資料模型 ─────────────────────────────────────────────────────────────────

@dataclass
class CoinSignal:
    symbol: str
    last: float
    open_24h: float
    high: float
    low: float
    quote_vol: float
    base_vol: float
    change_pct: float
    range_pct: float
    funding: float | None
    max_leverage: int | None
    status: str
    score: float
    tags: list[str]


# ── 分析 ─────────────────────────────────────────────────────────────────────

def build_signals(
    tickers: list[dict],
    pairs: list[dict],
    funding: list[dict],
    min_quote_vol: float = 50_000,
) -> list[CoinSignal]:
    pair_map = {p.get("symbol"): p for p in pairs}
    fund_map = {f_.get("symbol"): f(f_.get("fundingRate")) for f_ in funding}

    # 成交量分位用
    vols = [f(t.get("quoteVol")) for t in tickers if f(t.get("quoteVol")) > 0]
    vols_sorted = sorted(vols)
    def vol_rank(v: float) -> float:
        if not vols_sorted:
            return 0.0
        # 粗略百分位
        lo, hi = 0, len(vols_sorted)
        while lo < hi:
            mid = (lo + hi) // 2
            if vols_sorted[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(vols_sorted) * 100

    signals: list[CoinSignal] = []
    for t in tickers:
        symbol = t.get("symbol") or ""
        if not symbol.endswith("USDT"):
            continue
        pair = pair_map.get(symbol, {})
        status = pair.get("symbolStatus", "UNKNOWN")
        if status and status != "OPEN" and pair:
            continue

        last = f(t.get("lastPrice") or t.get("last"))
        open_ = f(t.get("open"))
        high = f(t.get("high"))
        low = f(t.get("low"))
        qv = f(t.get("quoteVol"))
        bv = f(t.get("baseVol"))
        if qv < min_quote_vol or last <= 0 or open_ <= 0:
            continue

        chg = pct_change(last, open_)
        rng = ((high - low) / open_ * 100) if open_ else 0.0
        fr = fund_map.get(symbol)

        tags: list[str] = []
        score = 0.0

        # 成交量（流動性）
        vr = vol_rank(qv)
        score += vr * 0.35
        if vr >= 90:
            tags.append("高流動")
        elif vr >= 70:
            tags.append("流動佳")

        # 漲跌動能（取絕對值偏高 + 方向標籤）
        abs_chg = abs(chg)
        mom = min(abs_chg / 15.0, 1.0) * 100  # 15% 封頂
        score += mom * 0.30
        if chg >= 8:
            tags.append("強勢漲")
        elif chg >= 3:
            tags.append("偏多")
        elif chg <= -8:
            tags.append("急跌")
        elif chg <= -3:
            tags.append("偏空")

        # 波動空間（適合短線）
        vola = min(rng / 20.0, 1.0) * 100
        score += vola * 0.20
        if rng >= 12:
            tags.append("高波動")

        # 資金費率極端（可能回歸 / 擠壓）
        if fr is not None:
            abs_fr = abs(fr)
            fr_score = min(abs_fr / 0.01, 1.0) * 100  # 1% 封頂
            score += fr_score * 0.15
            if fr >= 0.005:
                tags.append("費率偏多")
            elif fr <= -0.005:
                tags.append("費率偏空")

        if not tags:
            tags.append("觀望")

        signals.append(
            CoinSignal(
                symbol=symbol,
                last=last,
                open_24h=open_,
                high=high,
                low=low,
                quote_vol=qv,
                base_vol=bv,
                change_pct=chg,
                range_pct=rng,
                funding=fr,
                max_leverage=int(pair["maxLeverage"]) if pair.get("maxLeverage") is not None else None,
                status=status or "OPEN",
                score=round(score, 2),
                tags=tags,
            )
        )

    signals.sort(key=lambda s: s.score, reverse=True)
    return signals


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def fetch_mtf_bias(symbol: str, tfs: list[str] | None = None) -> dict[str, str]:
    """高週期方向：1H / 4H / 日 / 週 / 月。"""
    from sr_logic import detect_structure

    tfs = tfs or ["1h", "4h", "1d", "1w"]
    out: dict[str, str] = {}
    for tf in tfs:
        try:
            raw = fetch_klines(symbol, interval=tf, limit=80)
            candles = parse_klines(raw)
            need = 40 if tf not in {"1w", "1M"} else 20
            if len(candles) < need:
                continue
            bias, _, _, _ = detect_structure(candles)
            out[tf.upper()] = bias
        except RuntimeError:
            continue
    # 月線可選
    try:
        raw = fetch_klines(symbol, interval="1M", limit=40)
        candles = parse_klines(raw)
        if len(candles) >= 12:
            from sr_logic import detect_structure
            bias, _, _, _ = detect_structure(candles)
            out["1M"] = bias
    except RuntimeError:
        pass
    return out


def fetch_session_opens(symbol: str) -> dict[str, float]:
    """
    日開盤 / 晚盤20:00(台北=UTC+8 → 12:00 UTC) / 週開 / 月開。
    """
    from datetime import datetime, timezone, timedelta

    opens: dict[str, float] = {}
    try:
        d1 = parse_klines(fetch_klines(symbol, "1d", 5))
        if d1:
            opens["DAY"] = d1[-1].open
    except RuntimeError:
        pass
    try:
        w1 = parse_klines(fetch_klines(symbol, "1w", 5))
        if w1:
            opens["WEEK"] = w1[-1].open
    except RuntimeError:
        pass
    try:
        m1 = parse_klines(fetch_klines(symbol, "1M", 5))
        if m1:
            opens["MONTH"] = m1[-1].open
    except RuntimeError:
        pass
    # 晚盤 20:00 台北：找最近一根 1h 開盤對應 12:00 UTC
    try:
        h1 = parse_klines(fetch_klines(symbol, "1h", 48))
        tw = timezone(timedelta(hours=8))
        for c in reversed(h1):
            dt = datetime.fromtimestamp(c.time / 1000, tz=timezone.utc).astimezone(tw)
            if dt.hour == 20:
                opens["EVE20"] = c.open
                break
    except (RuntimeError, OSError, ValueError):
        pass
    return opens


def analyze_klines(symbol: str, interval: str = "15m", with_mtf: bool = True) -> dict[str, Any]:
    raw = fetch_klines(symbol, interval=interval, limit=120)
    candles = parse_klines(raw)
    if len(candles) < 40:
        return {"error": "K 線不足"}

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]

    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    # 第四章 MA5/10/30
    def _sma_last(period: int) -> float:
        if len(closes) < period:
            return closes[-1]
        return sum(closes[-period:]) / period

    ma5, ma10, ma30 = _sma_last(5), _sma_last(10), _sma_last(30)
    r = rsi(closes, 14)
    k_close = closes[-1]
    prev = closes[-2]
    ticker_last = fetch_ticker_last(symbol)
    # 分析／進場參考用最新成交；沒 ticker 才退回 K 收
    last = ticker_last if ticker_last > 0 else k_close
    support = min(lows[-20:])
    resist = max(highs[-20:])
    avg_vol = sum(vols[-20:]) / 20 if vols else 0
    last_vol = vols[-1] if vols else 0
    vol_surge = (last_vol / avg_vol) if avg_vol > 0 else 0

    mtf = fetch_mtf_bias(symbol) if with_mtf else {}
    opens = fetch_session_opens(symbol) if with_mtf else {}
    setup = evaluate_setup(symbol, candles, interval, mtf=mtf, opens=opens)

    return {
        "symbol": symbol,
        "interval": interval,
        "last": last,
        "k_close": k_close,
        "ticker_last": ticker_last,
        "change_bar": pct_change(k_close, prev),
        "ema9": e9[-1],
        "ema21": e21[-1],
        "ma5": ma5,
        "ma10": ma10,
        "ma30": ma30,
        "rsi": r,
        "support": support,
        "resist": resist,
        "vol_surge": vol_surge,
        "bias": setup.structure,
        "setup": setup,
        "mtf": mtf,
        "long_entry": setup.entry if setup.side == "long" else last,
        "long_stop": setup.stop if setup.side == "long" else last * 0.985,
        "long_tp": setup.tp if setup.side == "long" else last * 1.02,
        "short_entry": setup.entry if setup.side == "short" else last,
        "short_stop": setup.stop if setup.side == "short" else last * 1.015,
        "short_tp": setup.tp if setup.side == "short" else last * 0.98,
        "bars": len(closes),
    }


def evaluate_symbol(
    symbol: str,
    interval: str = "15m",
    with_mtf: bool = True,
    mtf_tfs: list[str] | None = None,
) -> ChecklistResult | None:
    try:
        raw = fetch_klines(symbol, interval=interval, limit=120)
        candles = parse_klines(raw)
        if len(candles) < 40:
            return None
        tfs = mtf_tfs if mtf_tfs is not None else ["1h", "4h", "1d"]
        mtf = fetch_mtf_bias(symbol, tfs) if with_mtf else {}
        opens = fetch_session_opens(symbol) if with_mtf else {}
        return evaluate_setup(symbol, candles, interval, mtf=mtf, opens=opens)
    except RuntimeError:
        return None


def _pair_launch_map() -> dict[str, float]:
    """symbol -> launchTime(ms)。"""
    out: dict[str, float] = {}
    try:
        for p in fetch_pairs():
            sym = p.get("symbol") or ""
            lt = f(p.get("launchTime"))
            if sym and lt > 0:
                out[sym] = lt
    except RuntimeError:
        pass
    return out


def _side_key(r: ChecklistResult) -> str:
    if r.side == "long" or (r.side == "none" and r.structure == "偏多"):
        return "long"
    if r.side == "short" or (r.side == "none" and r.structure == "偏空"):
        return "short"
    return "other"


def _is_major(symbol: str) -> bool:
    return symbol.upper() in MAJOR_SYMBOLS


def quality_rank(
    r: ChecklistResult,
    vol: float,
    cfg: SetupFilterConfig,
) -> float:
    """
    品質分：RR 落在 1.5~3 最優、流程步數高、成交量大、可進優先。
    用來取代「先到先贏」的同向名額。
    """
    q = 0.0
    if r.trigger_ready:
        q += 50
    # RR 甜區
    if r.rr > 0:
        if cfg.ideal_rr_lo <= r.rr <= cfg.ideal_rr_hi:
            q += 40
        elif cfg.ideal_rr_hi < r.rr <= 5:
            q += 22
        elif r.rr > 5:
            q += max(5, 20 - (r.rr - 5) * 2)  # 過高扣
        elif r.rr >= cfg.min_rr:
            q += 15
    q += min(r.workflow_passed, 11) * 3.5  # ≤38.5
    q += min(r.setup_score, 100) * 0.15
    # 成交量（log）
    q += min(math.log10(max(vol, 1)) / 9 * 25, 25)
    if _is_major(r.symbol):
        q += 8
    if r.mtf_aligned:
        q += 5
    return q


def zone_distance_pct(r: ChecklistResult, last_price: float) -> float:
    """現價距區中軸的百分比；在區內為 0。"""
    if r.at_zone:
        return 0.0
    mid = (r.zone_lo + r.zone_hi) / 2 if r.zone_hi > 0 else r.zone_lo
    px = last_price if last_price > 0 else r.entry
    if mid <= 0 or px <= 0:
        return 999.0
    return abs(px - mid) / px * 100.0


def is_near_retest_zone(
    r: ChecklistResult,
    last_price: float,
    near_pct: float = DEFAULT_NEAR_ZONE_PCT,
) -> bool:
    return r.waiting_retest and zone_distance_pct(r, last_price) <= near_pct


def filter_setups(
    rows: list[ChecklistResult],
    signals: list[CoinSignal],
    cfg: SetupFilterConfig | None = None,
    launch_map: dict[str, float] | None = None,
) -> tuple[list[ChecklistResult], list[str]]:
    """去重／過濾噪音 + 品質排序 + 大幣/山寨分組名額。"""
    cfg = cfg or SetupFilterConfig()
    launch_map = launch_map if launch_map is not None else _pair_launch_map()
    sig_map = {s.symbol: s for s in signals}
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    rejected: list[str] = []
    kept: list[ChecklistResult] = []

    for r in rows:
        sig = sig_map.get(r.symbol)
        reasons: list[str] = []

        vol = sig.quote_vol if sig else 0.0
        if vol < cfg.min_quote_vol:
            reasons.append(f"量<{cfg.min_quote_vol/1e3:.0f}K")

        rng = sig.range_pct if sig else 0.0
        if rng >= cfg.max_range_pct:
            reasons.append(f"振幅{rng:.0f}%過大")

        lt = launch_map.get(r.symbol, 0.0)
        if lt > 0:
            age_days = (now_ms - lt) / 86400000
            if age_days < cfg.min_list_days:
                reasons.append(f"上市{age_days:.1f}天")

        # RR 鎖 1.5～2.5：YES 硬擋；計畫單超過上限也擋
        if r.rr > 0 and r.rr > cfg.max_rr:
            reasons.append(f"RR{r.rr:.1f}>{cfg.max_rr:.1f}")
        if r.trigger_ready and r.rr > 0 and r.rr < cfg.min_rr:
            reasons.append(f"RR{r.rr:.1f}<{cfg.min_rr}")
        if cfg.lock_rr_band and r.trigger_ready and r.rr > 0:
            if not (cfg.min_rr <= r.rr <= cfg.max_rr):
                reasons.append(f"RR不在{cfg.min_rr:.1f}~{cfg.max_rr:.1f}")

        # 風險幅度鎖 0.5%～0.8%
        if r.entry > 0 and r.stop > 0:
            risk_pct = abs(r.entry - r.stop) / r.entry * 100
            if risk_pct < cfg.min_risk_pct:
                reasons.append(f"停損過近{risk_pct:.2f}%")
            elif risk_pct > cfg.max_risk_pct and (r.trigger_ready or r.side != "none"):
                reasons.append(f"停損過寬{risk_pct:.2f}%")

        if cfg.require_mtf_aligned and not r.mtf_aligned and r.trigger_ready:
            reasons.append("逆高週期")

        # 硬規則：結構與方向不得相反（防逆勢單混進列表）
        if r.structure == "偏多" and r.side == "short":
            reasons.append("逆結構禁空")
        if r.structure == "偏空" and r.side == "long":
            reasons.append("逆結構禁多")
        if r.structure == "盤整" and r.side in ("long", "short"):
            reasons.append("盤整禁方向單")
        if r.trigger_ready and r.structure == "偏多" and r.side != "long":
            reasons.append("YES非順勢多")
        if r.trigger_ready and r.structure == "偏空" and r.side != "short":
            reasons.append("YES非順勢空")

        if reasons:
            rejected.append(f"{r.symbol}({','.join(reasons[:2])})")
            continue

        kept.append(r)

    # 品質排序
    kept.sort(
        key=lambda x: quality_rank(x, sig_map[x.symbol].quote_vol if x.symbol in sig_map else 0, cfg),
        reverse=True,
    )

    # 大幣 / 山寨 × 多 / 空 分組名額
    slots = {
        ("long", "major"): 0,
        ("long", "alt"): 0,
        ("short", "major"): 0,
        ("short", "alt"): 0,
    }
    limits = {
        "major": cfg.top_major_per_side,
        "alt": cfg.top_alt_per_side,
    }
    limited: list[ChecklistResult] = []
    for r in kept:
        sk = _side_key(r)
        if sk == "other":
            limited.append(r)
            continue
        bucket = "major" if _is_major(r.symbol) else "alt"
        key = (sk, bucket)
        if slots[key] >= limits[bucket]:
            rejected.append(f"{r.symbol}({'大幣' if bucket == 'major' else '山寨'}同向超量)")
            continue
        slots[key] += 1
        limited.append(r)

    # 最終仍以品質分排序輸出
    limited.sort(
        key=lambda x: (
            x.trigger_ready,
            quality_rank(x, sig_map[x.symbol].quote_vol if x.symbol in sig_map else 0, cfg),
        ),
        reverse=True,
    )
    return limited, rejected


def pick_wave_alerts(
    yes: list[ChecklistResult],
    near: list[ChecklistResult],
    *,
    max_n: int = DEFAULT_MAX_ALERTS_PER_WAVE,
    signals: list[CoinSignal] | None = None,
    cfg: SetupFilterConfig | None = None,
) -> tuple[list[ChecklistResult], list[ChecklistResult]]:
    """同波最多推 max_n 檔：先 YES 再靠近區，皆依品質分排序。"""
    cfg = cfg or SetupFilterConfig()
    sig_map = {s.symbol: s.quote_vol for s in (signals or [])}

    def _q(r: ChecklistResult) -> float:
        return quality_rank(r, sig_map.get(r.symbol, 0.0), cfg)

    yes_s = sorted(yes, key=_q, reverse=True)
    near_s = sorted(near, key=_q, reverse=True)
    budget = max(0, max_n)
    take_yes = yes_s[:budget]
    budget -= len(take_yes)
    take_near = near_s[:budget]
    return take_yes, take_near


def load_alert_cooldown(path: str = COOLDOWN_PATH) -> dict[str, float]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        return {str(k): float(v) for k, v in data.items()}
    except Exception:
        return {}


def save_alert_cooldown(data: dict[str, float], path: str = COOLDOWN_PATH) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=0)
    except OSError:
        pass


def filter_cooldown(
    setups: list[ChecklistResult],
    cooldown: dict[str, float],
    *,
    minutes: float = DEFAULT_ALERT_COOLDOWN_MIN,
) -> tuple[list[ChecklistResult], dict[str, float]]:
    """同一幣冷卻內不重複推。回傳 (可推清單, 更新後 cooldown)。"""
    now = time.time()
    window = minutes * 60
    out: list[ChecklistResult] = []
    for r in setups:
        last = cooldown.get(r.symbol, 0.0)
        if now - last < window:
            continue
        out.append(r)
        cooldown[r.symbol] = now
    # 清超過 24h 的舊紀錄
    cooldown = {k: v for k, v in cooldown.items() if now - v < 86400}
    return out, cooldown


def scan_setups(
    symbols: list[str],
    interval: str = "15m",
    workers: int = 6,
    only_trigger: bool = False,
    with_mtf: bool = True,
) -> list[ChecklistResult]:
    results: list[ChecklistResult] = []
    total = len(symbols)
    print(f"正在掃描 Setup {total} 個幣（{interval}，MTF={'開' if with_mtf else '關'}）…")

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(evaluate_symbol, sym, interval, with_mtf): sym for sym in symbols}
        for fut in as_completed(futs):
            done += 1
            if done % 10 == 0 or done == total:
                print(f"  進度 {done}/{total}", end="\r")
            r = fut.result()
            if not r:
                continue
            if only_trigger and not r.trigger_ready:
                continue
            results.append(r)
    print()
    results.sort(key=lambda x: (x.trigger_ready, x.setup_score, x.rr), reverse=True)
    return results


# ── 警報 ─────────────────────────────────────────────────────────────────────

def _beep() -> None:
    try:
        if sys.platform == "win32":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            winsound.Beep(880, 180)
            winsound.Beep(1175, 220)
        else:
            print("\a", end="", flush=True)
    except Exception:
        print("\a", end="", flush=True)


def _desktop_toast(title: str, body: str) -> None:
    """Windows 非阻塞氣泡通知。"""
    if sys.platform != "win32":
        return
    try:
        import subprocess
        # 轉義單引號
        t = title.replace("'", "")[:40]
        b = body.replace("'", " ").replace("\n", " | ")[:180]
        ps = (
            "[reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null; "
            "[reflection.assembly]::loadwithpartialname('System.Drawing') | Out-Null; "
            "$n=New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
            f"$n.ShowBalloonTip(5000,'{t}','{b}',"
            "[System.Windows.Forms.ToolTipIcon]::Info); "
            "Start-Sleep -Seconds 6; $n.Dispose()"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _telegram_token_chat() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token, chat


def _telegram_api(method: str, payload: dict[str, Any] | None = None, *, timeout: int = 35) -> Any:
    token, _ = _telegram_token_chat()
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            return None
        return body.get("result")
    except Exception:
        return None


def _telegram_send(text: str, *, html: bool = True, chat_id: str | None = None) -> bool:
    token, default_chat = _telegram_token_chat()
    chat = (chat_id or default_chat).strip()
    if not token or not chat:
        return False
    payload: dict[str, Any] = {
        "chat_id": chat,
        "text": text[:3900],
        "disable_web_page_preview": "true",
    }
    if html:
        payload["parse_mode"] = "HTML"
    result = _telegram_api("sendMessage", payload, timeout=20)
    if result is not None:
        return True
    # HTML 失敗時改純文字再試一次
    if html:
        return _telegram_send(
            text.replace("<", "").replace(">", ""), html=False, chat_id=chat
        )
    return False


def load_telegram_offset(path: str = TELEGRAM_OFFSET_PATH) -> int:
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        return int(data.get("offset", 0))
    except Exception:
        return 0


def save_telegram_offset(offset: int, path: str = TELEGRAM_OFFSET_PATH) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump({"offset": int(offset)}, fp)
    except OSError:
        pass


def normalize_query_symbol(text: str) -> str | None:
    """把手機輸入轉成 XXXUSDT；無法辨識則 None。"""
    raw = (text or "").strip().upper()
    if not raw or raw.startswith("/"):
        # 允許 /btc /detail btc
        raw = raw.lstrip("/").strip().upper()
        if raw.startswith("DETAIL "):
            raw = raw[7:].strip()
        elif raw.startswith("D "):
            raw = raw[2:].strip()
        elif raw in {"START", "HELP", "STATS", "JOURNAL", "JS"}:
            return raw
    raw = raw.replace("-", "").replace("/", "").replace(" ", "")
    if not raw or not raw.replace("USDT", "").isalnum():
        return None
    if raw in {"START", "HELP", "STATS", "JOURNAL", "JS"}:
        return raw
    if not raw.endswith("USDT"):
        raw += "USDT"
    if len(raw) < 6 or len(raw) > 20:
        return None
    return raw


def format_telegram_detail(info: dict[str, Any], meta: CoinSignal | None = None) -> str:
    """手機回覆用：濃縮版 detail（對齊 CMD 重點）。"""
    if info.get("error"):
        return f"❌ {_esc(str(info['error']))}"
    setup: ChecklistResult | None = info.get("setup")
    live = info.get("ticker_last") or (meta.last if meta else 0) or info.get("last") or 0
    k_close = info.get("k_close") or info.get("last") or 0
    lines = [
        f"🔎 <b>{_esc(info['symbol'])}</b> · {_esc(info['interval'])}",
        f"現價 <code>{_esc(_px(live))}</code>",
    ]
    if k_close > 0 and live > 0 and abs(live - k_close) / live >= 0.001:
        lines.append(f"{_esc(info['interval'])} K收 <code>{_esc(_px(k_close))}</code>")
    if meta:
        chg = f"{meta.change_pct:+.2f}%"
        lines.append(
            f"24h {chg} · 振幅 {meta.range_pct:.1f}% · 額 {fmt_num(meta.quote_vol)}"
        )
    mtf = info.get("mtf") or {}
    if mtf:
        lines.append("多週期 " + " ".join(f"{k}:{v}" for k, v in mtf.items()))

    if setup:
        side = _side_short_label(setup)
        if setup.trigger_ready:
            status = "✅ 可扣板機"
        elif setup.waiting_retest:
            status = "⏳ 等回測"
        elif setup.structure == "盤整":
            status = "📋 盤整觀望"
        else:
            status = "📋 條件未齊"
        lines.append(f"{side} ｜ {status} ｜ 流程 {setup.workflow_passed}/11 ｜ 分 {setup.setup_score:.0f}")
        if setup.zone_lo and setup.zone_hi:
            zlab = "箱體" if setup.structure == "盤整" else "優先區"
            lines.append(
                f"{zlab} <code>{_esc(_px(setup.zone_lo))}</code>–"
                f"<code>{_esc(_px(setup.zone_hi))}</code>"
            )
        if setup.trigger_ready or (
            setup.side in ("long", "short")
            and setup.entry
            and setup.stop
            and setup.tp
            and setup.rr > 0
            and (setup.waiting_retest or setup.at_zone)
        ):
            risk_pct = abs(setup.entry - setup.stop) / setup.entry * 100 if setup.entry else 0
            lines.append(f"入場 <code>{_esc(_px(setup.entry))}</code>")
            lines.append(
                f"停損 <code>{_esc(_px(setup.stop))}</code>（{risk_pct:.2f}%）"
            )
            lines.append(
                f"止盈 <code>{_esc(_px(setup.tp))}</code> · RR <b>{setup.rr:.1f}</b>"
            )
            if setup.trigger_ready and setup.entry and setup.stop:
                rl = format_risk_line(setup.entry, setup.stop)
                if rl:
                    lines.append(_esc(rl))
        if setup.structure == "偏多":
            lines.append("🚫 偏多・禁空")
        elif setup.structure == "偏空":
            lines.append("🚫 偏空・禁多")
        elif setup.structure == "盤整":
            lines.append("🚫 盤整・禁方向單")
        if setup.mtf_note:
            lines.append(f"MTF {_esc(setup.mtf_note.split('｜')[0][:48])}")
        if setup.missing_steps:
            lines.append(f"缺 {_esc(setup.missing_steps.replace('缺：', ''))}")
        if setup.note:
            lines.append(f"備註 {_esc(setup.note[:120])}")
    else:
        lines.append(f"偏向 {_esc(str(info.get('bias', '-')))}")

    lines.append("⚠️ 僅供參考，非投資建議")
    return "\n".join(lines)


def answer_telegram_symbol(symbol: str, *, interval: str = "15m") -> str:
    try:
        info = analyze_klines(symbol, interval, with_mtf=True)
    except RuntimeError as e:
        return f"❌ {_esc(symbol)} 查詢失敗：{_esc(str(e))}"
    meta = None
    try:
        # 輕量：只從 ticker 補 24h，避免整市 scan
        for t in fetch_tickers():
            if (t.get("symbol") or "") == symbol:
                last = f(t.get("lastPrice") or t.get("last"))
                open_ = f(t.get("open"))
                high = f(t.get("high"))
                low = f(t.get("low"))
                qv = f(t.get("quoteVol"))
                chg = pct_change(last, open_) if open_ else 0.0
                rng = ((high - low) / open_ * 100) if open_ > 0 else 0.0
                meta = CoinSignal(
                    symbol=symbol,
                    last=last,
                    open_24h=open_,
                    high=high,
                    low=low,
                    quote_vol=qv,
                    base_vol=0.0,
                    change_pct=chg,
                    range_pct=rng,
                    funding=None,
                    max_leverage=None,
                    status="",
                    score=0.0,
                    tags=[],
                )
                break
    except RuntimeError:
        pass
    return format_telegram_detail(info, meta)


def poll_telegram_queries(
    *,
    timeout: int = 0,
    interval: str = "15m",
    max_msgs: int = 8,
) -> int:
    """
    處理手機傳給 Bot 的幣種查詢（只回覆 TELEGRAM_CHAT_ID）。
    timeout=0：立刻取佇列（給 watch-once）；>0：長輪詢秒數（給 bot 模式）。
    """
    token, chat = _telegram_token_chat()
    if not token or not chat:
        return 0
    offset = load_telegram_offset()
    payload: dict[str, Any] = {"timeout": str(max(0, int(timeout)))}
    if offset > 0:
        payload["offset"] = str(offset)
    updates = _telegram_api("getUpdates", payload, timeout=max(20, timeout + 10))
    if not isinstance(updates, list):
        return 0

    handled = 0
    next_offset = offset
    for upd in updates:
        uid = int(upd.get("update_id", 0) or 0)
        next_offset = max(next_offset, uid + 1)
        msg = upd.get("message") or {}
        from_chat = str((msg.get("chat") or {}).get("id", ""))
        if from_chat != str(chat):
            continue
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        key = normalize_query_symbol(text)
        if key is None:
            _telegram_send(
                "用法：直接傳幣種，例如 <code>BTC</code> / <code>ETHUSDT</code>\n"
                "或 <code>/help</code>",
                chat_id=from_chat,
            )
            handled += 1
            continue
        if key in {"START", "HELP"}:
            _telegram_send(
                "📱 <b>幣種查詢</b>\n"
                "傳 <code>BTC</code>、<code>MAGIC</code>、<code>AVAXUSDT</code>\n"
                "會回傳類似 CMD <code>detail</code> 的摘要\n"
                "統計：傳 <code>stats</code>",
                chat_id=from_chat,
            )
            handled += 1
            continue
        if key in {"STATS", "JOURNAL", "JS"}:
            # 簡短統計文字
            from signal_tracker import compute_stats, load_journal, CURRENT_RULESET

            rows = load_journal()
            yes = [r for r in rows if (r.ruleset or "") == CURRENT_RULESET and (r.kind or "yes") == "yes"]
            plan = [r for r in rows if (r.ruleset or "") == CURRENT_RULESET and (r.kind or "yes") == "plan"]
            sy, sp = compute_stats(yes), compute_stats(plan)
            _telegram_send(
                f"📊 <b>日記統計</b>\n"
                f"YES {sy.total}｜勝{sy.win_n} 負{sy.loss_n} 未結{sy.open_n}"
                + (f"｜勝率{sy.win_rate:.1f}%" if sy.win_n + sy.loss_n else "")
                + f"\n規劃 {sp.total}｜勝{sp.win_n} 負{sp.loss_n} 未結{sp.open_n}"
                + (f"｜勝率{sp.win_rate:.1f}%" if sp.win_n + sp.loss_n else ""),
                chat_id=from_chat,
            )
            handled += 1
            continue

        print(f"Telegram 查詢: {key}")
        reply = answer_telegram_symbol(key, interval=interval)
        ok = _telegram_send(reply, chat_id=from_chat)
        print(f"  → {'已回覆' if ok else '回覆失敗'} {key}")
        handled += 1
        if handled >= max_msgs:
            break

    if next_offset > offset:
        save_telegram_offset(next_offset)
    return handled


def telegram_bot_loop(*, interval: str = "15m") -> None:
    """本機長駐：手機傳幣種立即回 detail 摘要。"""
    enable_ansi()
    token, chat = _telegram_token_chat()
    if not token or not chat:
        print("請先設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（telegram.local.env）")
        return
    print(color("Telegram 查詢 Bot 已啟動（Ctrl+C 停止）", "1;36"))
    print("手機對 Bot 傳幣種，例如 BTC / ETHUSDT；回覆僅限你的 CHAT_ID")
    try:
        while True:
            n = poll_telegram_queries(timeout=25, interval=interval, max_msgs=10)
            if n == 0:
                time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n已停止 Bot")


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _px(v: float) -> str:
    if not v or v <= 0:
        return "-"
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.8f}".rstrip("0").rstrip(".")


def format_alert_line(r: ChecklistResult) -> str:
    """終端機用一行摘要。"""
    side = {"long": "多", "short": "空"}.get(r.side, r.structure)
    return (
        f"{r.symbol} {side} 入場{_px(r.entry)} "
        f"停損{_px(r.stop)} 止盈{_px(r.tp)} "
        f"RR={r.rr:.1f}｜{r.workflow_passed}/11"
    )


def _plan_htf_bias_line(r: ChecklistResult) -> str:
    """規劃用：即使 4H 盤整也顯示日／週／月偏向。"""
    mtf = r.mtf or {}
    parts: list[str] = []
    for key, lab in (("1D", "日"), ("1W", "週"), ("1M", "月")):
        if key in mtf:
            parts.append(f"{lab}:{mtf[key]}")
    if not parts:
        for key, lab in (("4H", "4H"), ("1H", "1H")):
            if key in mtf:
                parts.append(f"{lab}:{mtf[key]}")
    return "｜".join(parts) if parts else "高週期無資料"


def _plan_show_full_levels(r: ChecklistResult) -> bool:
    """有明確方向且靠近／等回測／可進，才展開完整進出場。"""
    if r.structure == "盤整":
        return False
    directed = r.structure in ("偏多", "偏空") or r.side in ("long", "short")
    if not directed:
        return False
    if not (r.entry > 0 and r.stop > 0 and r.tp > 0 and abs(r.entry - r.stop) > 0):
        return False
    return bool(r.trigger_ready or r.waiting_retest or r.at_zone)


def _plan_zone_line(r: ChecklistResult, *, html: bool = True) -> str:
    if not (r.zone_lo and r.zone_hi):
        return ""
    lo, hi = _px(r.zone_lo), _px(r.zone_hi)
    if r.structure == "盤整":
        label = "箱體"
    elif r.structure == "偏多" or r.side == "long":
        label = "下方支撐區"
    elif r.structure == "偏空" or r.side == "short":
        label = "上方壓力區"
    else:
        label = "優先區"
    if html:
        return f"{label} <code>{_esc(lo)}</code>–<code>{_esc(hi)}</code>"
    return f"{label} {lo}–{hi}"


def format_telegram_card(r: ChecklistResult, *, kind: str) -> str:
    """
    Telegram HTML 卡片。
    kind: yes | retest | retest_summary | plan
    """
    if kind == "plan":
        return format_telegram_plan_card(r)

    if r.side == "long":
        side_emoji, side_txt = "🟢", "做多"
    elif r.side == "short":
        side_emoji, side_txt = "🔴", "做空"
    elif r.structure == "偏多":
        side_emoji, side_txt = "🟢", "偏多(等)"
    elif r.structure == "偏空":
        side_emoji, side_txt = "🔴", "偏空(等)"
    else:
        side_emoji, side_txt = "⚪", r.structure

    risk_pct = abs(r.entry - r.stop) / r.entry * 100 if r.entry and r.stop else 0
    reward_pct = abs(r.tp - r.entry) / r.entry * 100 if r.entry and r.tp else 0
    group = "大幣" if _is_major(r.symbol) else "山寨"

    if kind == "yes":
        status = "✅ 可扣板機"
        note = "可扣板機（仍非投資建議）"
    else:
        status = "⏳ 靠近區・等回測"
        note = _esc(r.missing_steps or "等價格確認反應後再進")

    ban = ""
    if r.structure == "偏多":
        ban = "🚫 結構偏多・禁止做空"
    elif r.structure == "偏空":
        ban = "🚫 結構偏空・禁止做多"
    elif r.structure == "盤整":
        ban = "🚫 盤整・禁止方向單"

    zone = _plan_zone_line(r, html=True)
    mtf = _esc(r.mtf_note) if r.mtf_note else ("高週期對齊" if r.mtf_aligned else "高週期未對齊")
    lines = [
        f"{side_emoji} <b>{_esc(r.symbol)}</b>  {side_txt} ｜ {status} ｜ {group}",
    ]
    if zone:
        lines.append(zone)
    lines.append(f"入場 <code>{_esc(_px(r.entry))}</code>")
    lines.append(f"停損 <code>{_esc(_px(r.stop))}</code>  （約 {risk_pct:.2f}%）")
    lines.append(f"止盈 <code>{_esc(_px(r.tp))}</code>  （約 {reward_pct:.2f}%）")
    lines.extend(
        [
            f"RR <b>{r.rr:.1f}</b> ｜ 流程 {r.workflow_passed}/11 ｜ 分 {r.setup_score:.0f}",
            f"MTF: {mtf}",
        ]
    )
    if ban:
        lines.append(ban)
    if kind == "yes" and r.entry and r.stop:
        rl = format_risk_line(r.entry, r.stop)
        if rl:
            lines.append(_esc(rl))
    lines.append(f"備註: {note}")
    return "\n".join(lines)


def format_telegram_plan_card(r: ChecklistResult) -> str:
    """4H 日內規劃卡：盤整短卡；有方向＋回測區才展開進出場。"""
    group = "大幣" if _is_major(r.symbol) else "山寨"
    htf = _esc(_plan_htf_bias_line(r))
    zone = _plan_zone_line(r, html=True)
    full = _plan_show_full_levels(r)

    if r.structure == "盤整":
        lines = [
            f"⚪ <b>{_esc(r.symbol)}</b>  盤整 ｜ 📋 規劃・等選邊 ｜ {group}",
        ]
        if zone:
            lines.append(zone)
        lines.append(f"高週期 {htf}")
        lines.append("🚫 盤整・禁止方向單")
        lines.append("備註: 等市場選邊後再給進出場")
        return "\n".join(lines)

    if r.side == "long" or r.structure == "偏多":
        side_emoji, side_txt = "🟢", "偏多" if r.side != "long" else "做多"
        ban = "🚫 結構偏多・禁止做空"
    elif r.side == "short" or r.structure == "偏空":
        side_emoji, side_txt = "🔴", "偏空" if r.side != "short" else "做空"
        ban = "🚫 結構偏空・禁止做多"
    else:
        side_emoji, side_txt = "⚪", r.structure
        ban = ""

    if r.trigger_ready:
        status = "✅ 可扣板機"
    elif r.waiting_retest or r.at_zone:
        status = "📋 規劃・等回測"
    else:
        status = "📋 規劃・有方向"

    lines = [
        f"{side_emoji} <b>{_esc(r.symbol)}</b>  {side_txt} ｜ {status} ｜ {group}",
    ]
    if zone:
        lines.append(zone)
    lines.append(f"高週期 {htf}")

    if full:
        risk_pct = abs(r.entry - r.stop) / r.entry * 100
        reward_pct = abs(r.tp - r.entry) / r.entry * 100 if r.tp else 0
        lines.append(f"計畫入場 <code>{_esc(_px(r.entry))}</code>")
        lines.append(f"計畫停損 <code>{_esc(_px(r.stop))}</code>  （約 {risk_pct:.2f}%）")
        lines.append(f"計畫止盈 <code>{_esc(_px(r.tp))}</code>  （約 {reward_pct:.2f}%）")
        lines.append(f"RR <b>{r.rr:.1f}</b> ｜ 流程 {r.workflow_passed}/11 ｜ 分 {r.setup_score:.0f}")
        note = _esc(r.missing_steps or r.note or "條件齊再進")
    else:
        lines.append("進出場: 等回測到區後再給完整計畫")
        note = "有方向但尚未到回測區，先看上下沿"

    if ban:
        lines.append(ban)
    lines.append(f"備註: {note}")
    return "\n".join(lines)


def format_console_card(r: ChecklistResult, *, kind: str) -> str:
    """終端機卡片（與 Telegram 對齊）。"""
    if kind == "plan":
        return format_console_plan_card(r)

    if r.side == "long":
        side_txt = "做多"
    elif r.side == "short":
        side_txt = "做空"
    else:
        side_txt = r.structure
    risk_pct = abs(r.entry - r.stop) / r.entry * 100 if r.entry and r.stop else 0
    reward_pct = abs(r.tp - r.entry) / r.entry * 100 if r.entry and r.tp else 0
    if kind == "yes":
        status = "可扣板機"
    else:
        status = "靠近區・等回測"
    group = "大幣" if _is_major(r.symbol) else "山寨"
    mtf = (r.mtf_note or ("對齊" if r.mtf_aligned else "未對齊"))[:60]
    if r.structure == "偏多":
        ban = "禁空"
    elif r.structure == "偏空":
        ban = "禁多"
    else:
        ban = "禁方向"
    lines = [
        f"  ┌ {r.symbol}  {side_txt} ｜ {status} ｜ {group} ｜ {ban}",
        f"  │ 入場 {_px(r.entry)}",
        f"  │ 停損 {_px(r.stop)}  （約 {risk_pct:.2f}%）",
        f"  │ 止盈 {_px(r.tp)}  （約 {reward_pct:.2f}%）",
        f"  │ RR {r.rr:.1f} ｜ 流程 {r.workflow_passed}/11 ｜ 分 {r.setup_score:.0f}",
        f"  │ MTF: {mtf}",
    ]
    if kind == "yes" and r.entry and r.stop:
        rl = format_risk_line(r.entry, r.stop)
        if rl:
            lines.append(f"  │ {rl}")
    lines[-1] = "  └" + lines[-1][3:] if lines[-1].startswith("  │") else lines[-1]
    return "\n".join(lines)


def format_console_plan_card(r: ChecklistResult) -> str:
    htf = _plan_htf_bias_line(r)
    zone = _plan_zone_line(r, html=False)
    full = _plan_show_full_levels(r)
    group = "大幣" if _is_major(r.symbol) else "山寨"
    if r.structure == "盤整":
        lines = [
            f"  ┌ {r.symbol}  盤整 ｜ 規劃・等選邊 ｜ {group}",
        ]
        if zone:
            lines.append(f"  │ {zone}")
        lines.append(f"  │ 高週期 {htf}")
        lines.append("  └ 禁方向・等選邊")
        return "\n".join(lines)

    side_txt = (
        "做多" if r.side == "long" else
        "做空" if r.side == "short" else
        r.structure
    )
    status = (
        "可扣板機" if r.trigger_ready else
        "等回測" if (r.waiting_retest or r.at_zone) else
        "有方向"
    )
    lines = [
        f"  ┌ {r.symbol}  {side_txt} ｜ 規劃・{status} ｜ {group}",
    ]
    if zone:
        lines.append(f"  │ {zone}")
    lines.append(f"  │ 高週期 {htf}")
    if full:
        risk_pct = abs(r.entry - r.stop) / r.entry * 100
        lines.append(f"  │ 入場 {_px(r.entry)}")
        lines.append(f"  │ 停損 {_px(r.stop)}  （約 {risk_pct:.2f}%）")
        lines.append(f"  │ 止盈 {_px(r.tp)} ｜ RR {r.rr:.1f}")
        lines.append(f"  └ 流程 {r.workflow_passed}/11")
    else:
        lines.append("  └ 等回測到區再給進出場")
    return "\n".join(lines)


def _side_short_label(r: ChecklistResult) -> str:
    """摘要用方向：多／空／多*／空*／整。"""
    if r.side == "long":
        return "多"
    if r.side == "short":
        return "空"
    if r.structure == "偏多":
        return "多*"
    if r.structure == "偏空":
        return "空*"
    return "整"


def build_telegram_message(title: str, setups: list[ChecklistResult], *, kind: str) -> str:
    now = now_tw().strftime("%m-%d %H:%M")
    if kind == "retest_summary":
        names = ", ".join(
            f"{r.symbol.replace('USDT', '')}{_side_short_label(r)}" for r in setups[:12]
        )
        return (
            f"👀 <b>{_esc(title)}</b>\n🕐 {now}\n"
            f"遠處等回測 <b>{len(setups)}</b> 檔（距區 &gt; {DEFAULT_NEAR_ZONE_PCT}% 不逐檔推播）\n"
            f"<code>{_esc(names)}</code>"
            + (f" …+{len(setups)-12}" if len(setups) > 12 else "")
            + "\n多*/空*=計畫方向（尚未可進）｜靠近區時會再推完整卡片"
        )

    head = {
        "yes": f"🚨 <b>{_esc(title)}</b>\n🕐 {now} ｜ 共 {len(setups)} 檔｜皆為可扣板機",
        "retest": (
            f"👀 <b>{_esc(title)}</b>\n🕐 {now} ｜ 共 {len(setups)} 檔\n"
            f"僅推距區 ≤{DEFAULT_NEAR_ZONE_PCT}% 的靠近訊號"
        ),
        "plan": (
            f"📘 <b>{_esc(title)}</b>\n🕐 {now}\n"
            f"BTC／ETH・<b>{SESSION_PLAN_INTERVAL.upper()}</b> 日內規劃（參考，非必進）"
        ),
    }.get(kind, f"<b>{_esc(title)}</b>\n🕐 {now}")

    card_kind = "retest" if kind == "retest_summary" else kind
    cards = [format_telegram_card(r, kind=card_kind) for r in setups[:6]]
    body = "\n\n————\n\n".join(cards)
    if len(setups) > 6:
        body += f"\n\n…另有 {len(setups) - 6} 檔未列出"
    foot = "\n\n⚠️ 僅供規劃，請自行確認槓桿與風險"
    return f"{head}\n\n{body}{foot}"


def notify_alert(
    title: str,
    lines: list[str] | None = None,
    *,
    setups: list[ChecklistResult] | None = None,
    kind: str = "yes",
    sound: bool = False,
    desktop: bool = False,
    telegram: bool = True,
) -> None:
    print()
    print(color(f"══ {title} ══", "1;33"))
    if setups and kind != "retest_summary":
        card_kind = "yes" if kind == "yes" else ("plan" if kind == "plan" else "retest")
        for r in setups:
            print(format_console_card(r, kind=card_kind))
            print()
    elif setups and kind == "retest_summary":
        names = ", ".join(
            f"{r.symbol.replace('USDT', '')}{_side_short_label(r)}" for r in setups[:15]
        )
        print(f"  遠處等回測 {len(setups)} 檔: {names}")
        print(f"  （距區 >{DEFAULT_NEAR_ZONE_PCT}% 僅摘要｜多*/空*=計畫向）")
        print()
    else:
        body = "\n".join(lines or [])
        print(body)

    if sound:
        _beep()
    if desktop and setups:
        _desktop_toast(title, " / ".join(r.symbol for r in setups[:5]))
    if telegram:
        if setups:
            msg = build_telegram_message(title, setups, kind=kind)
        else:
            msg = f"<b>{_esc(title)}</b>\n{_esc(chr(10).join(lines or []))}"
        ok = _telegram_send(msg)
        if ok:
            print("  → 已推送 Telegram")
        elif os.environ.get("TELEGRAM_BOT_TOKEN"):
            print("  → Telegram 推送失敗，請檢查 TOKEN/CHAT_ID")


# ── 顯示 ─────────────────────────────────────────────────────────────────────

def enable_ansi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass
        # 避免 Windows 主控台中文亂碼
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
        try:
            import os
            os.system("chcp 65001 >nul")
        except Exception:
            pass


def color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _disp_width(text: str) -> int:
    """終端顯示寬度（中文 2、ANSI 不计）。"""
    n = 0
    for ch in _ANSI_RE.sub("", text):
        n += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return n


def pad_l(text: str, width: int) -> str:
    """左對齊（右側補空白）。"""
    return text + " " * max(0, width - _disp_width(text))


def pad_r(text: str, width: int) -> str:
    """右對齊（左側補空白）。"""
    return " " * max(0, width - _disp_width(text)) + text


def c_chg(pct: float) -> str:
    s = f"{pct:+.2f}%"
    if pct > 0:
        return color(s, "32")
    if pct < 0:
        return color(s, "31")
    return s


def c_chg_cell(pct: float, width: int) -> str:
    """先對齊再上色，避免 ANSI 把欄位撐歪。"""
    plain = pad_r(f"{pct:+.2f}%", width)
    if pct > 0:
        return color(plain, "32")
    if pct < 0:
        return color(plain, "31")
    return plain


def print_table(signals: list[CoinSignal], top: int = 30, mode: str = "hot") -> None:
    rows = signals
    if mode == "gainers":
        rows = sorted(signals, key=lambda s: s.change_pct, reverse=True)
    elif mode == "losers":
        rows = sorted(signals, key=lambda s: s.change_pct)
    elif mode == "volume":
        rows = sorted(signals, key=lambda s: s.quote_vol, reverse=True)
    elif mode == "funding":
        rows = sorted(
            [s for s in signals if s.funding is not None],
            key=lambda s: abs(s.funding or 0),
            reverse=True,
        )

    rows = rows[:top]
    now = now_tw().strftime("%m-%d %H:%M")
    title = {
        "hot": "綜合熱度排行",
        "gainers": "24h 漲幅榜",
        "losers": "24h 跌幅榜",
        "volume": "成交量榜",
        "funding": "資金費率極端",
    }.get(mode, mode)

    # 欄寬以「顯示寬度」計（中文佔 2）
    w_i, w_sym, w_px, w_chg = 3, 14, 12, 9
    w_rng, w_vol, w_fr, w_lev = 8, 9, 10, 4
    gap = "  "
    total_w = (
        w_i + w_sym + w_px + w_chg + w_rng + w_vol + w_fr + w_lev + 7 * 2 + 16
    )

    print()
    print(color(f"══ Bitunix · {title} ══", "1;36") + f"  {now}  · Top {len(rows)}/{len(signals)}")
    print("-" * total_w)
    header = (
        pad_r("#", w_i)
        + gap
        + pad_l("幣種", w_sym)
        + gap
        + pad_r("價格", w_px)
        + gap
        + pad_r("24h", w_chg)
        + gap
        + pad_r("振幅", w_rng)
        + gap
        + pad_r("成交額", w_vol)
        + gap
        + pad_r("費率", w_fr)
        + gap
        + pad_r("槓桿", w_lev)
        + gap
        + "標籤"
    )
    print(color(header, "1"))
    print("-" * total_w)

    for i, s in enumerate(rows, 1):
        fr = f"{s.funding * 100:+.4f}%" if s.funding is not None else "-"
        lev = str(s.max_leverage) if s.max_leverage else "-"
        tags = ",".join(s.tags[:3]) if s.tags else "-"
        line = (
            pad_r(str(i), w_i)
            + gap
            + pad_l(s.symbol, w_sym)
            + gap
            + pad_r(fmt_price(s.last), w_px)
            + gap
            + c_chg_cell(s.change_pct, w_chg)
            + gap
            + pad_r(f"{s.range_pct:.2f}%", w_rng)
            + gap
            + pad_r(fmt_num(s.quote_vol), w_vol)
            + gap
            + pad_r(fr, w_fr)
            + gap
            + pad_r(lev, w_lev)
            + gap
            + f"{tags} ({s.score:.0f})"
        )
        print(line)
    print("-" * total_w)
    print(color("分數＝流動性+動能+波動+費率；非買賣建議。detail 幣種 看詳情。", "2"))
    print()


def _ok(flag: bool) -> str:
    return color("✓", "32") if flag else color("✗", "31")


def _px_or_dash(v: float) -> str:
    return fmt_price(v) if v and v > 0 else "-"


def print_checklist(setup: ChecklistResult) -> None:
    print()
    print(color("── 實戰做單流程（11步）──", "1;36"))
    print("每個工具都在問：這個位置值不值得冒險？不是找藉口進場。")
    if setup.workflow:
        for title, passed, detail in setup.workflow:
            print(f"  {_ok(passed)} {title}")
            print(f"      {detail}")
        if setup.trigger_ready:
            progress_note = "可扣板機"
        else:
            progress_note = setup.missing_steps or "條件未齊，觀望"
        print(
            color(
                f"  → 流程進度 {setup.workflow_passed}/11 ｜ {progress_note}",
                "1;33",
            )
        )
        if setup.mtf_note:
            print(f"  MTF: {setup.mtf_note}")
    else:
        print("  （尚未產生流程）")

    print()
    print(color("── 第四章六問｜支撐壓力｜Fibo（細節）──", "1;35"))
    for title, passed, detail in setup.chapter4:
        print(f"  {_ok(passed)} {title}: {detail}")
    for title, passed, detail in setup.checklist:
        print(f"  {_ok(passed)} {title}: {detail}")
    fib = setup.fibo
    if fib and fib.ready and fib.levels:
        print(
            f"  Fibo水位 0.382={fmt_price(fib.levels.get('0.382', 0))}  "
            f"0.5={fmt_price(fib.levels.get('0.5', 0))}  "
            f"0.618={fmt_price(fib.levels.get('0.618', 0))}"
            + (
                f"  力道返還{fib.force_return_tag}≈{fmt_price(fib.force_return_price)}"
                if fib.force_return_price
                else ""
            )
        )
    if setup.eq_levels:
        print(f"  {setup.eq_levels}")
    if setup.liquidity:
        print(f"  流動性: {setup.liquidity}")
    if setup.indicators:
        print(f"  指標: {setup.indicators}")

    print()
    print(color("── 入場／停損／止盈位置 ──", "1;32"))
    side_txt = {"long": "做多 LONG", "short": "做空 SHORT", "none": "暫不下手"}.get(setup.side, setup.side)
    plan = ""
    if setup.side == "none" and setup.structure == "偏多":
        plan = "（傾向做多，條件未齊）"
    elif setup.side == "none" and setup.structure == "偏空":
        plan = "（傾向做空，條件未齊）"
    print(f"  方向: {side_txt}{plan}")

    # 僅 YES 顯示完整進出場；等回測只給計畫摘要；其餘不顯示價位避免誤以為可進
    if setup.trigger_ready:
        print(f"  入場: {_px_or_dash(setup.entry)}")
        print(f"  停損: {_px_or_dash(setup.stop)}" + (f"  ← {setup.invalidation_reason}" if setup.invalidation_reason else ""))
        print(f"  止盈: {_px_or_dash(setup.tp)}" + (
            f"  ← 力道返還 {fib.force_return_tag}" if fib and fib.force_return_price and setup.tp == fib.force_return_price else ""
        ))
        if setup.entry and setup.stop and setup.entry > 0:
            risk_pct = abs(setup.entry - setup.stop) / setup.entry * 100
            print(f"  風險幅度: {risk_pct:.2f}%（入場→停損）")
        if setup.entry and setup.tp and setup.entry > 0:
            reward_pct = abs(setup.tp - setup.entry) / setup.entry * 100
            print(f"  目標幅度: {reward_pct:.2f}%（入場→止盈）")
        if setup.rr:
            print(f"  盈虧比 RR: {setup.rr:.2f}" + (" ✓" if setup.rr >= 1.5 else f" （建議≥1.5）"))
    elif setup.waiting_retest:
        print(color("  （等回測・僅計畫參考，非可進場）", "33"))
        print(f"  計畫區中軸: {_px_or_dash(setup.entry)}")
        print(f"  計畫停損: {_px_or_dash(setup.stop)}")
        print(f"  計畫止盈: {_px_or_dash(setup.tp)}" + (f" ｜計畫RR {setup.rr:.1f}" if setup.rr else ""))
    else:
        print(color("  條件未齊 → 暫不顯示進場／停損／止盈（避免誤當可下單）", "33"))
        print(f"  原因: {setup.missing_steps or setup.note or '觀望'}")

    print()
    if setup.structure == "偏多":
        print(color("【硬規則】結構偏多 → 只做多，禁止做空", "1;31"))
    elif setup.structure == "偏空":
        print(color("【硬規則】結構偏空 → 只做空，禁止做多", "1;31"))
    elif setup.structure == "盤整":
        print(color("【硬規則】盤整 → 不給方向單，等選邊", "1;31"))
    trig = color("可扣板機", "1;32") if setup.trigger_ready else color("尚未扣板機", "1;33")
    wait = color("等回測", "33") if setup.waiting_retest else ""
    conclude_side = side_txt if setup.trigger_ready else (
        "暫不下手" if setup.side == "none" else f"{side_txt}（未齊）"
    )
    print(
        f"結論: {color(conclude_side, '1;33')} ｜ {trig} ｜ 邏輯分 {setup.setup_score:.0f}/100"
        + (f" ｜ {wait}" if wait else "")
    )
    print(f"提醒: {setup.note}")
    print(color("好單不是衝動；是方向、位置、止損、目標都說得通。", "1;36"))

    if setup.supports:
        s_txt = " ｜ ".join(f"{fmt_price(z.lo)}-{fmt_price(z.hi)}(×{z.strength})" for z in setup.supports)
        print(f"近端支撐區: {s_txt}")
    if setup.resists:
        r_txt = " ｜ ".join(f"{fmt_price(z.lo)}-{fmt_price(z.hi)}(×{z.strength})" for z in setup.resists)
        print(f"近端壓力區: {r_txt}")
    print()


def print_detail(info: dict[str, Any], meta: CoinSignal | None = None) -> None:
    if info.get("error"):
        print(info["error"])
        return
    print()
    print(color(f"══ {info['symbol']} 細節分析 ({info['interval']}) ══", "1;36"))
    if meta:
        print(
            f"24h: {c_chg(meta.change_pct)}  振幅 {meta.range_pct:.2f}%  "
            f"成交額 {fmt_num(meta.quote_vol)}  "
            f"費率 {(meta.funding * 100 if meta.funding is not None else float('nan')):+.4f}%"
            if meta.funding is not None
            else f"24h: {c_chg(meta.change_pct)}  振幅 {meta.range_pct:.2f}%  成交額 {fmt_num(meta.quote_vol)}"
        )
        if meta.max_leverage:
            print(f"最大槓桿: {meta.max_leverage}x  標籤: {', '.join(meta.tags)}")
    live = info.get("ticker_last") or (meta.last if meta else 0) or info["last"]
    k_close = info.get("k_close") or info["last"]
    print(f"現價: {fmt_price(live)}  （交易所最新成交，非限價單）")
    # 急漲急跌時 K 收常落後 ticker，分開標示避免誤會
    if k_close > 0 and abs(live - k_close) / max(live, 1e-12) >= 0.001:
        print(
            f"{info['interval']} K收: {fmt_price(k_close)}  "
            f"（上一根 {c_chg(info['change_bar'])}｜與現價不同屬正常）"
        )
    else:
        print(f"上一根K: {c_chg(info['change_bar'])}")
    print(
        f"MA5: {fmt_price(info.get('ma5', 0))}  MA10: {fmt_price(info.get('ma10', 0))}  "
        f"MA30: {fmt_price(info.get('ma30', 0))}"
    )
    print(f"EMA9: {fmt_price(info['ema9'])}  EMA21: {fmt_price(info['ema21'])}")
    rsi_s = f"{info['rsi']:.1f}" if info["rsi"] is not None else "-"
    print(f"RSI(14): {rsi_s}  量能比(近/均): {info['vol_surge']:.2f}x")
    print(f"近20根高低: 支撐 {fmt_price(info['support'])}  壓力 {fmt_price(info['resist'])}")
    mtf = info.get("mtf") or {}
    if mtf:
        print("多週期方向: " + "  ".join(f"{k}={v}" for k, v in mtf.items()))

    setup: ChecklistResult | None = info.get("setup")
    if setup:
        print_checklist(setup)
    else:
        print(color(f"偏向: {info['bias']}", "1;33"))
        print()


def _split_reject(item: str) -> tuple[str, str]:
    if "(" in item and item.endswith(")"):
        i = item.index("(")
        return item[:i], item[i + 1 : -1]
    return item, ""


def _print_reject_block(rejected: list[str], *, limit: int = 10) -> None:
    if not rejected:
        return
    print(color(f"剔除 {len(rejected)}", "33"))
    for item in rejected[:limit]:
        sym, reason = _split_reject(item)
        short = sym.replace("USDT", "")
        print(f"  {short:<10} {reason}")
    if len(rejected) > limit:
        print(f"  …另 {len(rejected) - limit} 檔")


def print_setup_table(
    rows: list[ChecklistResult],
    top: int = 20,
    rejected: list[str] | None = None,
) -> None:
    rows = rows[:top]
    now = now_tw().strftime("%m-%d %H:%M")
    yes_n = sum(1 for r in rows if r.trigger_ready)
    wait_n = sum(1 for r in rows if r.waiting_retest and not r.trigger_ready)
    print()
    print(color("══ Setup ══", "1;35") + f"  {now}  · 顯示 {len(rows)}  · YES {yes_n}  · 等回測 {wait_n}")

    if not rows:
        print(color("（目前沒有通過過濾的 Setup）", "2"))
        _print_reject_block(rejected or [])
        print(
            color(
                f"規則 RR{DEFAULT_MIN_RR:g}~{DEFAULT_MAX_RR:g} · "
                f"風險{DEFAULT_MIN_RISK_PCT:g}~{DEFAULT_MAX_RISK_PCT:g}% · "
                f"同向大幣{DEFAULT_TOP_MAJOR_PER_SIDE}/山寨{DEFAULT_TOP_ALT_PER_SIDE}",
                "2",
            )
        )
        print(color("詳情  detail 幣種", "2"))
        print()
        return

    w_i, w_sym, w_dir, w_side, w_st = 2, 12, 4, 3, 4
    w_e, w_s, w_t, w_rr, w_wf, w_sc = 11, 11, 11, 4, 5, 3
    gap = " "
    total_w = w_i + w_sym + w_dir + w_side + w_st + w_e + w_s + w_t + w_rr + w_wf + w_sc + 10
    print("-" * total_w)
    header = (
        pad_r("#", w_i)
        + gap
        + pad_l("幣種", w_sym)
        + gap
        + pad_l("方向", w_dir)
        + gap
        + pad_l("邊", w_side)
        + gap
        + pad_l("狀態", w_st)
        + gap
        + pad_r("入場", w_e)
        + gap
        + pad_r("停損", w_s)
        + gap
        + pad_r("止盈", w_t)
        + gap
        + pad_r("RR", w_rr)
        + gap
        + pad_r("步", w_wf)
        + gap
        + pad_r("分", w_sc)
    )
    print(color(header, "1"))
    print("-" * total_w)
    for i, r in enumerate(rows, 1):
        # 狀態欄不用 ANSI（避免對齊跑掉）；YES 整列再上色
        if r.trigger_ready:
            status = "YES"
        elif r.waiting_retest:
            status = "等"
        else:
            status = "·"
        side = {"long": "多", "short": "空", "none": "-"}.get(r.side, "-")
        if side == "-" and r.structure == "偏多":
            side = "多*"
        elif side == "-" and r.structure == "偏空":
            side = "空*"
        if r.trigger_ready:
            e_s, s_s, t_s = _px_or_dash(r.entry), _px_or_dash(r.stop), _px_or_dash(r.tp)
            rr = f"{r.rr:.1f}" if r.rr else "-"
        elif r.waiting_retest:
            e_s, s_s, t_s = _px_or_dash(r.entry), "-", "-"
            rr = f"{r.rr:.1f}" if r.rr else "-"
        else:
            e_s, s_s, t_s, rr = "-", "-", "-", "-"
        wf = f"{r.workflow_passed}/11"
        if not r.mtf_aligned:
            wf += "!"
        row = (
            pad_r(str(i), w_i)
            + gap
            + pad_l(r.symbol, w_sym)
            + gap
            + pad_l(r.structure, w_dir)
            + gap
            + pad_l(side, w_side)
            + gap
            + pad_l(status, w_st)
            + gap
            + pad_r(e_s, w_e)
            + gap
            + pad_r(s_s, w_s)
            + gap
            + pad_r(t_s, w_t)
            + gap
            + pad_r(rr, w_rr)
            + gap
            + pad_r(wf, w_wf)
            + gap
            + pad_r(f"{r.setup_score:.0f}", w_sc)
        )
        print(color(row, "32") if r.trigger_ready else row)
        # 一行備註就好，避免每檔三行刷屏
        bits: list[str] = []
        if r.trigger_ready:
            bits.append(color("可扣板機", "32"))
            if r.entry and r.stop:
                bits.append(f"風險{abs(r.entry - r.stop) / r.entry * 100:.2f}%")
        elif r.waiting_retest:
            bits.append("等回測（價僅參考）")
        elif r.missing_steps:
            bits.append(r.missing_steps.replace("缺：", "缺 ").replace("、", " "))
        else:
            bits.append("觀望")
        bits.append("大幣" if _is_major(r.symbol) else "山寨")
        if r.mtf_note:
            bits.append(r.mtf_note.split("｜")[0][:28])
        print("   " + " · ".join(bits))
    print("-" * total_w)
    _print_reject_block(rejected or [], limit=8)
    print(
        color(
            f"規則 RR{DEFAULT_MIN_RR:g}~{DEFAULT_MAX_RR:g} · "
            f"風險{DEFAULT_MIN_RISK_PCT:g}~{DEFAULT_MAX_RISK_PCT:g}% · "
            f"多*/空*=計畫向 · detail 幣種",
            "2",
        )
    )
    print()


def run_filtered_setup(
    signals: list[CoinSignal],
    *,
    n: int = 60,
    interval: str = "15m",
    top: int = 20,
    only_trigger: bool = False,
    cfg: SetupFilterConfig | None = None,
    show: bool = True,
) -> list[ChecklistResult]:
    cfg = cfg or SetupFilterConfig()
    # 先用更高成交額挑掃描池
    pool = [s for s in signals if s.quote_vol >= cfg.min_quote_vol]
    if len(pool) < 15:
        pool = sorted(signals, key=lambda s: s.quote_vol, reverse=True)[:n]
    else:
        pool = sorted(pool, key=lambda s: s.quote_vol, reverse=True)[:n]
    raw = scan_setups([s.symbol for s in pool], interval=interval, only_trigger=False, with_mtf=True)
    filtered, rejected = filter_setups(raw, signals, cfg=cfg)
    if only_trigger:
        filtered = [x for x in filtered if x.trigger_ready]
    if show:
        print_setup_table(filtered, top=top, rejected=rejected)
    if AUTO_RECORD_YES:
        yes = [x for x in filtered if x.trigger_ready]
        if yes:
            # 日記也只記同波品質前 N，避免一次灌爆拉低統計
            yes_take, _ = pick_wave_alerts(
                yes, [], max_n=cfg.max_alerts_per_wave, signals=signals, cfg=cfg
            )
            yes_take = _apply_yes_risk_gate(yes_take)
            record_yes_signals(yes_take, interval=interval)
    return filtered


def export_setups_csv(rows: list[ChecklistResult], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(
            [
                "symbol", "structure", "at_zone", "waiting_retest", "zone_kind",
                "zone_lo", "zone_hi", "reaction", "candle_signals", "trigger_ready",
                "side", "score", "rr", "invalidation", "confluence",
                "entry", "stop", "tp", "ma_align", "bos_choch",
                "fib_depth", "fib_tag", "fib_price", "force_return_tag",
                "force_return_price", "missing_steps", "mtf_aligned", "mtf_note", "note",
            ]
        )
        for r in rows:
            fib = r.fibo
            w.writerow(
                [
                    r.symbol, r.structure, r.at_zone, r.waiting_retest, r.zone_kind,
                    r.zone_lo, r.zone_hi, r.reaction, "|".join(r.candle_signals),
                    r.trigger_ready, r.side, r.setup_score, r.rr, r.invalidation,
                    "|".join(r.confluence), r.entry, r.stop, r.tp,
                    r.ma_align, r.bos_choch,
                    fib.depth if fib else "",
                    fib.fib_tag if fib else "",
                    fib.fib_price if fib else "",
                    fib.force_return_tag if fib else "",
                    fib.force_return_price if fib else "",
                    r.missing_steps, r.mtf_aligned, r.mtf_note, r.note,
                ]
            )
    print(f"已匯出 {len(rows)} 筆 setup → {path}")


def export_csv(signals: list[CoinSignal], path: str, top: int = 100) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(
            [
                "symbol", "last", "change_pct", "range_pct", "quote_vol",
                "funding", "max_leverage", "score", "tags",
            ]
        )
        for s in signals[:top]:
            w.writerow(
                [
                    s.symbol,
                    s.last,
                    round(s.change_pct, 4),
                    round(s.range_pct, 4),
                    s.quote_vol,
                    s.funding if s.funding is not None else "",
                    s.max_leverage or "",
                    s.score,
                    "|".join(s.tags),
                ]
            )
    print(f"已匯出 {min(top, len(signals))} 筆 → {path}")


# ── CLI ──────────────────────────────────────────────────────────────────────

def scan(min_vol: float) -> list[CoinSignal]:
    print("正在抓取 Bitunix 市場資料…")
    tickers = fetch_tickers()
    pairs = fetch_pairs()
    try:
        funding = fetch_funding_batch()
    except RuntimeError:
        funding = []
    print(f"ticker {len(tickers)} / 交易對 {len(pairs)} / 費率 {len(funding)}")
    return build_signals(tickers, pairs, funding, min_quote_vol=min_vol)


def _pick_symbols(signals: list[CoinSignal], n: int = 60) -> list[str]:
    """優先掃熱度高、成交量大的幣。"""
    by_vol = sorted(signals, key=lambda s: s.quote_vol, reverse=True)
    return [s.symbol for s in by_vol[:n]]


def _session_plan_slot_key(day: str, hour: int, minute: int) -> str:
    return f"{SESSION_PLAN_COOLDOWN_PREFIX}:{day}:{hour:02d}{minute:02d}"


def _due_session_plan_slots(cooldown: dict[str, float]) -> list[tuple[int, int, str]]:
    """回傳目前已到點、且今日該時段尚未推過的規劃槽。"""
    tw = now_tw()
    mins_now = tw.hour * 60 + tw.minute
    day = tw.strftime("%Y-%m-%d")
    due: list[tuple[int, int, str]] = []
    for hour, minute, label in SESSION_PLAN_SLOTS:
        if mins_now < hour * 60 + minute:
            continue
        key = _session_plan_slot_key(day, hour, minute)
        if key not in cooldown:
            due.append((hour, minute, label))
    return due


def _build_btc_eth_plans() -> list[ChecklistResult]:
    plans: list[ChecklistResult] = []
    for sym in SESSION_PLAN_SYMBOLS:
        r = evaluate_symbol(
            sym,
            SESSION_PLAN_INTERVAL,
            with_mtf=True,
            mtf_tfs=list(SESSION_PLAN_MTF),
        )
        if r:
            plans.append(r)
    return plans


def _plan_fingerprint(plans: list[ChecklistResult]) -> str:
    """結構／區間／方向指紋，用於同日無變化判斷。"""
    bits: list[str] = []
    for r in sorted(plans, key=lambda x: x.symbol):
        bits.append(
            "|".join(
                [
                    r.symbol,
                    r.structure,
                    r.side,
                    "1" if r.waiting_retest else "0",
                    "1" if r.trigger_ready else "0",
                    "1" if r.at_zone else "0",
                    f"{r.zone_lo:.4f}",
                    f"{r.zone_hi:.4f}",
                    f"{r.entry:.4f}",
                    f"{r.stop:.4f}",
                    f"{r.tp:.4f}",
                    _plan_htf_bias_line(r),
                ]
            )
        )
    return ";".join(bits)


def load_session_plan_state(path: str = SESSION_PLAN_STATE_PATH) -> dict[str, str]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_session_plan_state(state: dict[str, str], path: str = SESSION_PLAN_STATE_PATH) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(state, fp, ensure_ascii=False, indent=0)
    except OSError:
        pass


def _plan_brief_summary(plans: list[ChecklistResult]) -> str:
    parts: list[str] = []
    for r in plans:
        z = ""
        if r.zone_lo and r.zone_hi:
            z = f" {_px(r.zone_lo)}–{_px(r.zone_hi)}"
        parts.append(f"{r.symbol.replace('USDT', '')} {r.structure}{z}")
    return "｜".join(parts)


def send_session_major_plans(
    cooldown: dict[str, float],
    *,
    telegram: bool = True,
) -> tuple[int, dict[str, float]]:
    """
    到點推播 BTC／ETH 日內規劃：
    16:00 下午／20:00 晚盤／21:30 美股開盤（各一次／天）。
    同日內容指紋相同 → 只推「無變化」短訊，不重貼完整卡。
    """
    due = _due_session_plan_slots(cooldown)
    if not due:
        return 0, cooldown

    plans = _build_btc_eth_plans()
    if not plans:
        print("日內規劃：BTC/ETH 無法取得 4H Setup，略過")
        return 0, cooldown

    tw = now_tw()
    day = tw.strftime("%Y-%m-%d")
    # 一次只推「最新已到點」時段；更早未推的時段標記略過，避免補發刷屏
    for hour, minute, _label in due[:-1]:
        cooldown[_session_plan_slot_key(day, hour, minute)] = time.time()
    hour, minute, label = due[-1]
    title = f"BTC／ETH {label}（{SESSION_PLAN_INTERVAL.upper()}）"
    fp = _plan_fingerprint(plans)
    state = load_session_plan_state()
    unchanged = state.get("date") == day and state.get("fingerprint") == fp

    if unchanged:
        brief = _plan_brief_summary(plans)
        notify_alert(
            title,
            lines=[f"與上一時段相同，無變化｜{brief}"],
            kind="plan",
            sound=False,
            desktop=False,
            telegram=telegram,
        )
        print(f"日內規劃：「{label}」無變化（短訊）｜{brief}")
        note_extra = "無變化"
    else:
        notify_alert(
            title,
            setups=plans,
            kind="plan",
            sound=False,
            desktop=False,
            telegram=telegram,
        )
        print(f"日內規劃：已推「{label}」{', '.join(r.symbol for r in plans)}")
        note_extra = ""

    # 規劃一律入日記，事後可檢驗箱體／方向準否（與 YES 分開統計）
    record_plan_signals(
        plans,
        interval=SESSION_PLAN_INTERVAL,
        plan_label=label,
        note_extra=note_extra,
    )

    cooldown[_session_plan_slot_key(day, hour, minute)] = time.time()
    save_session_plan_state(
        {
            "date": day,
            "fingerprint": fp,
            "last_label": label,
            "summary": _plan_brief_summary(plans),
        }
    )
    sent = 1

    cutoff = time.time() - 3 * 86400
    cleaned = {
        k: v
        for k, v in cooldown.items()
        if not (k.startswith(SESSION_PLAN_COOLDOWN_PREFIX + ":") and v < cutoff)
    }
    return sent, cleaned


def _apply_yes_risk_gate(
    yes_list: list[ChecklistResult],
) -> list[ChecklistResult]:
    """固定 1R 資訊僅顯示；此處擋「同向／總倉」超量。"""
    if not yes_list:
        return yes_list
    rc = load_risk_config()
    open_c = count_open_by_side()
    allowed, blocked = filter_yes_by_risk(yes_list, cfg=rc)
    if blocked:
        print(
            color(
                f"風控擋下 {len(blocked)} 檔｜"
                f"未結多{open_c['long']}/空{open_c['short']}｜"
                f"同向≤{rc.max_same_side} 總≤{rc.max_total_open}｜"
                + "、".join(blocked[:8]),
                "1;33",
            )
        )
    return allowed


def watch_once(
    *,
    n: int = 100,
    desktop: bool = False,
    alert_retest: bool = True,
    alert_far_summary: bool = False,
    cooldown_min: float = DEFAULT_ALERT_COOLDOWN_MIN,
) -> dict[str, int]:
    """
    掃一轮就結束（給 GitHub Actions／Cloud Function 用）。
    靠 alert_cooldown.json 做跨次冷卻；雲端可用 Actions cache 保存該檔。
    """
    enable_ansi()
    cooldown = load_alert_cooldown()
    cfg = SetupFilterConfig()
    rc = load_risk_config()
    print(
        color(
            f"單次掃描｜冷卻 {cooldown_min:.0f} 分｜"
            f"風險{DEFAULT_MIN_RISK_PCT}~{DEFAULT_MAX_RISK_PCT}%｜"
            f"RR{DEFAULT_MIN_RR}~{DEFAULT_MAX_RR}｜同波≤{DEFAULT_MAX_ALERTS_PER_WAVE}｜"
            f"1R={rc.risk_pct_per_r:g}%×{rc.account_usdt:g}U｜同向≤{rc.max_same_side}",
            "1;36",
        )
    )
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        print("警告: 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，不會推播")

    # 先結算日記，釋放已平倉的同向名額
    try:
        n_upd, n_open = resolve_all(fetch_klines, parse_klines, max_bars=96)
        if n_upd:
            print(f"日記結算：更新 {n_upd} 筆，未結 {n_open}")
    except Exception as ex:
        print(f"日記結算略過: {ex}")

    signals = scan(min_vol=cfg.min_quote_vol)
    price_map = {s.symbol: s.last for s in signals}
    setups = run_filtered_setup(signals, n=n, top=15, cfg=cfg, show=False)
    yes_now = [r for r in setups if r.trigger_ready]
    wait_list = [r for r in setups if r.waiting_retest]
    near_now = [
        r
        for r in wait_list
        if is_near_retest_zone(r, price_map.get(r.symbol, r.entry), cfg.near_zone_pct)
    ]
    far_now = [r for r in wait_list if r not in near_now]

    cand_yes, cooldown = filter_cooldown(yes_now, cooldown, minutes=cooldown_min)
    cand_near: list[ChecklistResult] = []
    if alert_retest:
        cand_near, cooldown = filter_cooldown(near_now, cooldown, minutes=cooldown_min)

    # 同波最多 N 檔（先 YES 再靠近）
    new_yes, new_near = pick_wave_alerts(
        cand_yes,
        cand_near,
        max_n=cfg.max_alerts_per_wave,
        signals=signals,
        cfg=cfg,
    )
    new_yes = _apply_yes_risk_gate(new_yes)
    # 沒被選上的不寫進冷卻時間戳（允許下一波再競爭）— 需把未選的從 cooldown 撤回
    sent_syms = {r.symbol for r in new_yes} | {r.symbol for r in new_near}
    for r in cand_yes + cand_near:
        if r.symbol not in sent_syms and r.symbol in cooldown:
            # 剛寫入的冷卻撤回，避免「沒推卻冷卻」
            del cooldown[r.symbol]

    if new_yes:
        notify_alert(
            "可扣板機 YES",
            setups=new_yes,
            kind="yes",
            sound=False,
            desktop=desktop,
            telegram=True,
        )
        if AUTO_RECORD_YES:
            record_yes_signals(new_yes, interval="15m")
    if new_near:
        notify_alert(
            "靠近區・等回測",
            setups=new_near,
            kind="retest",
            sound=False,
            desktop=desktop,
            telegram=True,
        )

    # 雲端預設不推遠處摘要，避免每次排程刷屏；本機 watch 迴圈仍可推
    if alert_far_summary and far_now:
        far_key = "__far_summary__"
        now = time.time()
        if now - cooldown.get(far_key, 0.0) >= cooldown_min * 60:
            notify_alert(
                "遠處等回測（摘要）",
                setups=far_now,
                kind="retest_summary",
                sound=False,
                desktop=False,
                telegram=True,
            )
            cooldown[far_key] = now

    # 16:00／20:00／美股開盤：BTC／ETH 日內規劃（各時段每天一次）
    _, cooldown = send_session_major_plans(cooldown, telegram=True)

    # 處理手機傳給 Bot 的幣種查詢（對齊 CMD detail）
    try:
        n_q = poll_telegram_queries(timeout=0, interval="15m")
        if n_q:
            print(f"Telegram 查詢已處理 {n_q} 則")
    except Exception as ex:
        print(f"Telegram 查詢略過: {ex}")

    save_alert_cooldown(cooldown)
    print(
        f"[{now_tw().strftime('%H:%M:%S')}] 完成｜"
        f"YES={len(yes_now)}(推{len(new_yes)}) "
        f"靠近={len(near_now)}(推{len(new_near)}) "
        f"同波上限{cfg.max_alerts_per_wave} 遠處等={len(far_now)}"
    )
    return {
        "yes": len(yes_now),
        "yes_sent": len(new_yes),
        "near": len(near_now),
        "near_sent": len(new_near),
        "far": len(far_now),
    }


def watch_loop(
    *,
    seconds: int = 120,
    n: int = 100,
    desktop: bool = False,
    alert_retest: bool = True,
    cooldown_min: float = DEFAULT_ALERT_COOLDOWN_MIN,
) -> None:
    """本機定時掃描：可扣板機卡片 + 靠近區回測；遠處等回測只摘要。"""
    enable_ansi()
    seen_yes: set[str] = set()
    seen_near: set[str] = set()
    seen_far: set[str] = set()
    cooldown = load_alert_cooldown()
    print(color(f"警報監看中：每 {seconds}s 掃一次（Ctrl+C 停止）", "1;36"))
    print(
        f"冷卻 {cooldown_min:.0f} 分｜風險 {DEFAULT_MIN_RISK_PCT}~{DEFAULT_MAX_RISK_PCT}%｜"
        f"RR {DEFAULT_MIN_RR}~{DEFAULT_MAX_RR}｜同波≤{DEFAULT_MAX_ALERTS_PER_WAVE}"
    )
    print("Telegram: telegram.local.env 或環境變數 TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
    cfg = SetupFilterConfig()
    while True:
        try:
            signals = scan(min_vol=cfg.min_quote_vol)
            price_map = {s.symbol: s.last for s in signals}
            setups = run_filtered_setup(signals, n=n, top=15, cfg=cfg, show=False)
            yes_now = {r.symbol: r for r in setups if r.trigger_ready}
            wait_list = [r for r in setups if r.waiting_retest]
            near_now = {
                r.symbol: r
                for r in wait_list
                if is_near_retest_zone(r, price_map.get(r.symbol, r.entry), cfg.near_zone_pct)
            }
            far_now = {r.symbol: r for r in wait_list if r.symbol not in near_now}

            cand_yes = [yes_now[s] for s in yes_now if s not in seen_yes]
            cand_near = (
                [near_now[s] for s in near_now if s not in seen_near] if alert_retest else []
            )
            new_far = (
                [far_now[s] for s in far_now if s not in seen_far] if alert_retest else []
            )

            cand_yes, cooldown = filter_cooldown(cand_yes, cooldown, minutes=cooldown_min)
            cand_near, cooldown = filter_cooldown(cand_near, cooldown, minutes=cooldown_min)
            new_yes, new_near = pick_wave_alerts(
                cand_yes,
                cand_near,
                max_n=cfg.max_alerts_per_wave,
                signals=signals,
                cfg=cfg,
            )
            new_yes = _apply_yes_risk_gate(new_yes)
            sent_syms = {r.symbol for r in new_yes} | {r.symbol for r in new_near}
            for r in cand_yes + cand_near:
                if r.symbol not in sent_syms and r.symbol in cooldown:
                    del cooldown[r.symbol]

            if new_yes:
                notify_alert(
                    "可扣板機 YES",
                    setups=new_yes,
                    kind="yes",
                    sound=False,
                    desktop=desktop,
                    telegram=True,
                )
                if AUTO_RECORD_YES:
                    record_yes_signals(new_yes, interval="15m")
            if new_near:
                notify_alert(
                    "靠近區・等回測",
                    setups=new_near,
                    kind="retest",
                    sound=False,
                    desktop=desktop,
                    telegram=True,
                )
            if new_far:
                notify_alert(
                    "遠處等回測（摘要）",
                    setups=new_far,
                    kind="retest_summary",
                    sound=False,
                    desktop=False,
                    telegram=True,
                )

            if not new_yes and not new_near and not new_far:
                print(
                    f"[{now_tw().strftime('%H:%M:%S')}] 無新警報｜"
                    f"YES={len(yes_now)} 靠近={len(near_now)} 遠處等={len(far_now)}"
                )

            seen_yes = (seen_yes | set(yes_now)) & set(yes_now)
            seen_near = (seen_near | set(near_now)) & set(near_now)
            seen_far = (seen_far | set(far_now)) & set(far_now)
            try:
                poll_telegram_queries(timeout=0, interval="15m")
            except Exception:
                pass
            save_alert_cooldown(cooldown)

            time.sleep(max(seconds, 30))
        except KeyboardInterrupt:
            print("\n已停止監看")
            break


def interactive() -> None:
    enable_ansi()
    signals = scan(min_vol=50_000)
    print_table(signals, top=25, mode="hot")
    setups: list[ChecklistResult] = []

    while True:
        print(color("── 指令 ──", "2"))
        print("  h 熱度   s Setup   t 可進   w 等回測   a 警報")
        print("  j 日記   jr 結算   js 統計  d 幣種   r 重掃   c 匯出   q 離開")
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not cmd:
            continue
        low = cmd.lower()
        if low in {"q", "quit", "exit"}:
            break
        if low in {"r", "refresh"}:
            signals = scan(min_vol=50_000)
            setups = []
            print_table(signals, top=25, mode="hot")
        elif low in {"h", "hot"}:
            print_table(signals, top=25, mode="hot")
        elif low in {"g", "gainers"}:
            print_table(signals, top=25, mode="gainers")
        elif low in {"l", "losers"}:
            print_table(signals, top=25, mode="losers")
        elif low in {"v", "volume"}:
            print_table(signals, top=25, mode="volume")
        elif low in {"f", "funding"}:
            print_table(signals, top=25, mode="funding")
        elif low in {"s", "setup", "sr"}:
            setups = run_filtered_setup(signals, n=100, top=20)
        elif low in {"t", "trigger"}:
            setups = run_filtered_setup(signals, n=100, top=20, only_trigger=True)
        elif low in {"w", "wait", "retest"}:
            all_s = run_filtered_setup(signals, n=100, top=40, show=False)
            setups = all_s
            price_map = {s.symbol: s.last for s in signals}
            waiting = [x for x in all_s if x.waiting_retest]
            if not waiting:
                print("目前沒有「等待回測」的幣。")
            else:
                near = [
                    x
                    for x in waiting
                    if is_near_retest_zone(x, price_map.get(x.symbol, x.entry))
                ]
                far = [x for x in waiting if x not in near]
                if near:
                    print(color(f"── 靠近區（≤{DEFAULT_NEAR_ZONE_PCT}%）{len(near)} 檔 ──", "1;32"))
                    print_setup_table(near, top=20)
                if far:
                    names = ", ".join(x.symbol for x in far[:20])
                    print(
                        color(
                            f"── 遠處等回測 {len(far)} 檔（僅摘要，警報不逐檔推）──",
                            "1;33",
                        )
                    )
                    print(f"  {names}" + ("…" if len(far) > 20 else ""))
                    print()
        elif low in {"a", "alert", "watch"}:
            print("進入警報監看（預設 120 秒）。可用: python bitunix_scanner.py watch")
            watch_loop(seconds=120, n=100, desktop=False)
        elif low.startswith("d ") or low.startswith("detail "):
            parts = cmd.split(maxsplit=1)
            if len(parts) < 2:
                print("用法: d BTCUSDT")
                continue
            sym = parts[1].strip().upper().replace("-", "").replace("/", "")
            if not sym.endswith("USDT"):
                sym += "USDT"
            meta = next((s for s in signals if s.symbol == sym), None)
            try:
                info = analyze_klines(sym, "15m")
            except RuntimeError as e:
                print(e)
                continue
            print_detail(info, meta)
        elif low in {"c", "csv"}:
            export_csv(signals, "bitunix_scan.csv", top=100)
            if setups:
                export_setups_csv(setups, "bitunix_setups.csv")
        elif low in {"j", "journal"}:
            print_journal(limit=40)
        elif low in {"jr", "resolve"}:
            print("正在用後續 K 線結算未平倉訊號…")
            n_upd, n_open = resolve_all(fetch_klines, parse_klines, max_bars=96)
            print(f"本輪更新 {n_upd} 筆，仍未結算 {n_open} 筆")
            print_stats_by_ruleset()
        elif low in {"js", "stats"}:
            # 先嘗試結算再開統計（全部／新規則 v2／舊規則分開）
            resolve_all(fetch_klines, parse_klines, max_bars=96)
            print_stats_by_ruleset()
        else:
            print("未知指令")


def main() -> None:
    enable_ansi()
    p = argparse.ArgumentParser(
        description="Bitunix 幣種偵測 + 第四章六問 + 支撐壓力反應邏輯",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例:
  python bitunix_scanner.py setup --top 20
  python bitunix_scanner.py watch --seconds 120
  python bitunix_scanner.py journal list
  python bitunix_scanner.py journal resolve
  python bitunix_scanner.py journal stats
  python bitunix_scanner.py detail ETHUSDT
        """,
    )
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("scan", help="掃描市場排行")
    s.add_argument("--mode", choices=["hot", "gainers", "losers", "volume", "funding"], default="hot")
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--min-vol", type=float, default=50_000, help="最低 24h 成交額 (USDT)")
    s.add_argument("--export", type=str, default="", help="匯出 CSV 路徑")
    s.add_argument("--loop", type=int, default=0, help="每隔 N 秒重新掃描，0=只跑一次")

    u = sub.add_parser("setup", help="Setup 掃描（含過濾／缺步／MTF）")
    u.add_argument("--top", type=int, default=20)
    u.add_argument("--n", type=int, default=100, help="掃描池大小")
    u.add_argument("--interval", default="15m")
    u.add_argument("--min-vol", type=float, default=DEFAULT_MIN_SETUP_VOL)
    u.add_argument("--max-rr", type=float, default=DEFAULT_MAX_RR, help="RR 上限（預設 2.5）")
    u.add_argument("--min-rr", type=float, default=DEFAULT_MIN_RR, help="RR 下限（預設 1.5）")
    u.add_argument("--top-major", type=int, default=DEFAULT_TOP_MAJOR_PER_SIDE, help="大幣同向名額")
    u.add_argument("--top-alt", type=int, default=DEFAULT_TOP_ALT_PER_SIDE, help="山寨同向名額")
    u.add_argument("--min-risk", type=float, default=DEFAULT_MIN_RISK_PCT, help="最低風險幅度(%%)")
    u.add_argument("--max-risk", type=float, default=DEFAULT_MAX_RISK_PCT, help="最高風險幅度(%%)")
    u.add_argument(
        "--max-wave",
        type=int,
        default=DEFAULT_MAX_ALERTS_PER_WAVE,
        help="同波最多推播檔數",
    )
    u.add_argument("--trigger-only", action="store_true", help="只顯示可扣板機")
    u.add_argument("--export", type=str, default="", help="匯出 setup CSV")

    w = sub.add_parser("watch", help="本機定時掃描並警報（可扣板機／靠近區回測）")
    w.add_argument("--seconds", type=int, default=120, help="掃描間隔秒數")
    w.add_argument("--n", type=int, default=100, help="掃描池大小")
    w.add_argument("--desktop", action="store_true", help="同時彈出桌面氣泡")
    w.add_argument("--no-retest", action="store_true", help="不報等回測，只報可扣板機")
    w.add_argument(
        "--cooldown",
        type=float,
        default=DEFAULT_ALERT_COOLDOWN_MIN,
        help="同一幣冷卻分鐘（預設 45）",
    )

    wo = sub.add_parser("watch-once", help="掃一轮就結束（GitHub Actions／雲端用）")
    wo.add_argument("--n", type=int, default=100, help="掃描池大小")
    wo.add_argument("--no-retest", action="store_true", help="不報等回測，只報可扣板機")
    wo.add_argument("--far-summary", action="store_true", help="一併推遠處等回測摘要")
    wo.add_argument(
        "--cooldown",
        type=float,
        default=DEFAULT_ALERT_COOLDOWN_MIN,
        help="同一幣冷卻分鐘（預設 45）",
    )

    d = sub.add_parser("detail", help="單幣細節 + 第四章六問 + 支撐壓力清單")
    d.add_argument("symbol", type=str)
    d.add_argument("--interval", default="15m", help="K線週期 如 5m 15m 1h")
    d.add_argument("--no-mtf", action="store_true", help="不快取 1H/4H/D 多週期")

    b = sub.add_parser("bot", help="Telegram 手機查幣（傳 BTC／MAGIC 回 detail 摘要）")
    b.add_argument("--interval", default="15m", help="分析週期")

    j = sub.add_parser("journal", help="YES 訊號日記／結算／統計")
    j.add_argument(
        "action",
        choices=["list", "resolve", "stats"],
        help="list=列表 resolve=結算 stats=勝率統計",
    )
    j.add_argument("--max-bars", type=int, default=96, help="最多觀察幾根K（15m×96≈24h）")
    j.add_argument("--limit", type=int, default=40, help="list 顯示筆數")

    args = p.parse_args()

    if args.cmd is None:
        interactive()
        return

    if args.cmd == "scan":
        while True:
            signals = scan(min_vol=args.min_vol)
            print_table(signals, top=args.top, mode=args.mode)
            if args.export:
                export_csv(signals, args.export, top=max(args.top, 100))
            if not args.loop:
                break
            print(f"{args.loop}s 後重新掃描… (Ctrl+C 停止)")
            try:
                time.sleep(args.loop)
            except KeyboardInterrupt:
                break
        return

    if args.cmd == "setup":
        cfg = SetupFilterConfig(
            min_quote_vol=args.min_vol,
            max_rr=args.max_rr,
            min_rr=args.min_rr,
            ideal_rr_lo=args.min_rr,
            ideal_rr_hi=args.max_rr,
            top_major_per_side=args.top_major,
            top_alt_per_side=args.top_alt,
            min_risk_pct=args.min_risk,
            max_risk_pct=args.max_risk,
            max_alerts_per_wave=args.max_wave,
        )
        signals = scan(min_vol=min(args.min_vol, 50_000))
        setups = run_filtered_setup(
            signals,
            n=args.n,
            interval=args.interval,
            top=args.top,
            only_trigger=args.trigger_only,
            cfg=cfg,
        )
        if args.export:
            export_setups_csv(setups[: max(args.top, 50)], args.export)
        return

    if args.cmd == "watch":
        watch_loop(
            seconds=args.seconds,
            n=args.n,
            desktop=args.desktop,
            alert_retest=not args.no_retest,
            cooldown_min=args.cooldown,
        )
        return

    if args.cmd == "watch-once":
        watch_once(
            n=args.n,
            alert_retest=not args.no_retest,
            alert_far_summary=args.far_summary,
            cooldown_min=args.cooldown,
        )
        return

    if args.cmd == "detail":
        sym = args.symbol.upper().replace("-", "").replace("/", "")
        if not sym.endswith("USDT"):
            sym += "USDT"
        signals = scan(min_vol=0)
        meta = next((x for x in signals if x.symbol == sym), None)
        info = analyze_klines(sym, args.interval, with_mtf=not args.no_mtf)
        print_detail(info, meta)
        return

    if args.cmd == "bot":
        telegram_bot_loop(interval=args.interval)
        return

    if args.cmd == "journal":
        if args.action == "list":
            print_journal(limit=args.limit)
        elif args.action == "resolve":
            print("正在結算未平倉 YES 訊號…")
            n_upd, n_open = resolve_all(
                fetch_klines, parse_klines, max_bars=args.max_bars
            )
            print(f"本輪更新 {n_upd} 筆，仍未結算 {n_open} 筆")
            print_stats_by_ruleset()
        elif args.action == "stats":
            resolve_all(fetch_klines, parse_klines, max_bars=args.max_bars)
            print_stats_by_ruleset()
            print_journal(limit=min(args.limit, 15))
        return


if __name__ == "__main__":
    main()
