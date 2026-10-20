#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交易邏輯引擎（合併兩章）

第四章流程（做單六問）:
  站哪邊 → 看哪裡 → 等回測 → 看反應 → 找止損 → 算盈虧比

支撐壓力反應順序:
  大結構 → 支撐壓力區 → K棒訊號 → 扣板機
  （影線0.5 / 重疊K0.5 / 真空區 + Fibo / SMC / 型態）

05 Fibo：
  有明確推力後才畫；多方低→高看支撐，空方高→低看壓力
  0.382淺 / 0.5中 / 0.618深；不能只靠Fibo下單，要等反應
  力道返還止盈：守住0.382→看-0.382；0.5→-0.5；0.618→-0.618

方向規則:
  偏多(HH+HL): 回檔優先做多，不猜頂
  偏空(LH+LL): 反彈優先做空，不猜底
  盤整: 不追突破，等方向出來
  方向用來決定優先做多或做空，不是預測未來

實戰做單流程 11 步:
  高週期方向 → 結構 → BOS/CHOCH → 支撐壓力/EQH/EQL → Fibo
  → 失衡/OB/BPR/真空 → MA/RSI/POC → 回測反應 → 止損 → 目標RR → 才決定進場
  好單不是衝動，是方向、位置、止損、目標都說得通
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 盈虧比門檻：低於此不建議扣板機
MIN_RR = 1.5


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


@dataclass
class Candle:
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return max(self.high - self.low, 1e-12)

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def bullish(self) -> bool:
        return self.close >= self.open

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2


@dataclass
class Zone:
    kind: str  # support / resistance
    lo: float
    hi: float
    strength: int
    touches: int

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2

    def contains(self, price: float, pad: float = 0.0) -> bool:
        return (self.lo - pad) <= price <= (self.hi + pad)

    def near(self, price: float, atr: float) -> bool:
        pad = max(atr * 0.35, (self.hi - self.lo) * 0.5)
        return self.contains(price, pad) or abs(price - self.mid) <= pad


@dataclass
class ChecklistResult:
    symbol: str
    interval: str
    # 方向 / 結構
    structure: str  # 偏多 / 偏空 / 盤整
    structure_detail: str
    # 支撐壓力
    at_zone: bool
    zone_kind: str  # support / resistance / none
    zone_lo: float
    zone_hi: float
    waiting_retest: bool = False  # 方向對但尚未回測到區
    # K棒
    reaction: bool = False
    candle_signals: list[str] = field(default_factory=list)
    # 扣板機 / 匯合
    trigger_ready: bool = False
    confluence: list[str] = field(default_factory=list)
    # 總結
    side: str = "none"  # long / short / none
    setup_score: float = 0.0
    checklist: list[tuple[str, bool, str]] = field(default_factory=list)
    chapter4: list[tuple[str, bool, str]] = field(default_factory=list)
    entry: float = 0.0
    stop: float = 0.0
    tp: float = 0.0
    rr: float = 0.0
    invalidation: float = 0.0  # 理由失效價 = 止損依據
    invalidation_reason: str = ""
    ma_align: str = ""  # MA5/10/30
    bos_choch: str = ""
    mtf: dict[str, str] = field(default_factory=dict)
    note: str = ""
    supports: list[Zone] = field(default_factory=list)
    resists: list[Zone] = field(default_factory=list)
    fibo: "FibAnalysis | None" = None
    fibo_lines: list[tuple[str, bool, str]] = field(default_factory=list)
    workflow: list[tuple[str, bool, str]] = field(default_factory=list)
    workflow_passed: int = 0
    missing_steps: str = ""  # 例: 缺：03 BOS、07 指標
    opens: dict[str, float] = field(default_factory=dict)
    rsi_div: str = ""
    eq_levels: str = ""
    liquidity: str = ""
    indicators: str = ""
    mtf_aligned: bool = True
    mtf_note: str = ""
    # 日內規劃：各交易時段高低 [(名稱, lo, hi, 是否進行中), ...]
    plan_session_hl: list[tuple[str, float, float, bool]] = field(default_factory=list)
    # 台灣日真實今日高低；操作箱過寬裁近端時仍保留供對照
    plan_day_lo: float = 0.0
    plan_day_hi: float = 0.0
    plan_box_clipped: bool = False
    # Trader Willy 背離戰法：4H POC ＋ RSI 頂/底背離
    poc_4h: float = 0.0
    div_poc_note: str = ""
    # 日空＋4H 反抽未破＋15m 高位見頂 → 獨立空訊（非一般 YES）
    fade_ready: bool = False


def parse_klines(raw: list[dict]) -> list[Candle]:
    rows = sorted(raw, key=lambda x: int(x.get("time") or 0))
    out: list[Candle] = []
    for r in rows:
        o, h, l, c = _f(r.get("open")), _f(r.get("high")), _f(r.get("low")), _f(r.get("close"))
        if c <= 0 or h <= 0:
            continue
        out.append(
            Candle(
                time=int(r.get("time") or 0),
                open=o,
                high=h,
                low=l,
                close=c,
                volume=_f(r.get("quoteVol") or r.get("baseVol")),
            )
        )
    return out


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    window = trs[-period:] if len(trs) >= period else trs
    return sum(window) / len(window) if window else 0.0


def find_swings(candles: list[Candle], left: int = 3, right: int = 3) -> tuple[list[int], list[int]]:
    """回傳 swing high / swing low 的 index。"""
    highs, lows = [], []
    n = len(candles)
    for i in range(left, n - right):
        h = candles[i].high
        l = candles[i].low
        if all(h >= candles[i - j].high for j in range(1, left + 1)) and all(
            h >= candles[i + j].high for j in range(1, right + 1)
        ):
            highs.append(i)
        if all(l <= candles[i - j].low for j in range(1, left + 1)) and all(
            l <= candles[i + j].low for j in range(1, right + 1)
        ):
            lows.append(i)
    return highs, lows


def _sma(values: list[float], period: int) -> list[float]:
    out: list[float] = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(sum(values[: i + 1]) / (i + 1))
        else:
            out.append(sum(values[i + 1 - period : i + 1]) / period)
    return out


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ma_alignment(closes: list[float]) -> tuple[str, str]:
    """第四章：MA5 / MA10 / MA30 輔助判斷方向。"""
    if len(closes) < 30:
        return "不明", "MA資料不足"
    m5, m10, m30 = _sma(closes, 5), _sma(closes, 10), _sma(closes, 30)
    a, b, c = m5[-1], m10[-1], m30[-1]
    px = closes[-1]
    if a > b > c and px >= b:
        return "多頭排列", f"MA5>{fmt_short(a)}>MA10>{fmt_short(b)}>MA30"
    if a < b < c and px <= b:
        return "空頭排列", f"MA5<{fmt_short(a)}<MA10<{fmt_short(b)}<MA30"
    if a > b and b < c:
        return "糾結偏多", f"MA5/10/30交錯｜價{fmt_short(px)}"
    if a < b and b > c:
        return "糾結偏空", f"MA5/10/30交錯｜價{fmt_short(px)}"
    return "糾結", f"MA5={fmt_short(a)} MA10={fmt_short(b)} MA30={fmt_short(c)}"


def fmt_short(p: float) -> str:
    if p >= 1000:
        return f"{p:.1f}"
    if p >= 1:
        return f"{p:.4g}"
    return f"{p:.6g}"


def detect_bos_choch(candles: list[Candle]) -> tuple[str, int]:
    """
    BOS = 順勢突破結構；CHOCH = 結構性質改變（可能反轉）。
    回傳 (標籤, 對多空的分數加減)
    """
    highs_i, lows_i = find_swings(candles, 2, 2)
    if len(highs_i) < 2 or len(lows_i) < 2:
        return "", 0
    px = candles[-1].close
    last_hh = candles[highs_i[-1]].high
    prev_hh = candles[highs_i[-2]].high
    last_ll = candles[lows_i[-1]].low
    prev_ll = candles[lows_i[-2]].low

    # 先前偏多結構（HL）被跌破 → CHOCH 轉空
    if len(lows_i) >= 2 and candles[lows_i[-2]].low < candles[lows_i[-1]].low:
        # 曾是 HL，若收盤跌破最近 HL
        if px < candles[lows_i[-1]].low:
            return "CHOCH轉空", -2
    # 先前偏空結構（LH）被突破 → CHOCH 轉多
    if len(highs_i) >= 2 and candles[highs_i[-2]].high > candles[highs_i[-1]].high:
        if px > candles[highs_i[-1]].high:
            return "CHOCH轉多", 2

    if px > last_hh and last_hh >= prev_hh * 0.999:
        return "BOS向上", 1
    if px < last_ll and last_ll <= prev_ll * 1.001:
        return "BOS向下", -1
    return "", 0


def is_ranging(candles: list[Candle], atr_v: float) -> bool:
    """盤整：近段高低落在窄幅箱體。"""
    if len(candles) < 30 or atr_v <= 0:
        return False
    window = candles[-30:]
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    mid = (hi + lo) / 2
    width_pct = (hi - lo) / mid * 100 if mid else 0
    # 箱體相對 ATR 不大，或振幅百分比偏小
    return (hi - lo) < atr_v * 6.5 or width_pct < 3.5


def detect_structure(candles: list[Candle]) -> tuple[str, str, str, str]:
    """
    第四章方向判斷。
    回傳: (偏多|偏空|盤整, 細節, MA排列文字, BOS/CHOCH)
    """
    if len(candles) < 30:
        return "盤整", "K線不足，結構不明", "不明", ""

    highs_i, lows_i = find_swings(candles, 2, 2)
    closes = [c.close for c in candles]
    atr_v = atr(candles, 14)

    sh = [candles[i].high for i in highs_i[-4:]]
    sl = [candles[i].low for i in lows_i[-4:]]

    score = 0
    detail_parts: list[str] = []

    if len(sh) >= 2:
        if sh[-1] > sh[-2]:
            score += 2
            detail_parts.append("HH")
        elif sh[-1] < sh[-2]:
            score -= 2
            detail_parts.append("LH")
    if len(sl) >= 2:
        if sl[-1] > sl[-2]:
            score += 2
            detail_parts.append("HL")
        elif sl[-1] < sl[-2]:
            score -= 2
            detail_parts.append("LL")

    ma_tag, ma_detail = ma_alignment(closes)
    if "多頭排列" in ma_tag:
        score += 2
        detail_parts.append("MA多頭")
    elif "空頭排列" in ma_tag:
        score -= 2
        detail_parts.append("MA空頭")
    elif "偏多" in ma_tag:
        score += 1
    elif "偏空" in ma_tag:
        score -= 1

    bos_tag, bos_score = detect_bos_choch(candles)
    score += bos_score
    if bos_tag:
        detail_parts.append(bos_tag)

    ranging = is_ranging(candles, atr_v)
    # HH+HL 明確多；LH+LL 明確空
    clear_bull = "HH" in detail_parts and "HL" in detail_parts
    clear_bear = "LH" in detail_parts and "LL" in detail_parts
    detail = " / ".join(detail_parts) if detail_parts else ""

    # LH/LL 與 CHOCH轉多／MA多頭同時出現 → 結構打架，標盤整（避免硬做空）
    if clear_bear and (
        "CHOCH轉多" in detail_parts or "MA多頭" in detail_parts or score > 0
    ):
        return "盤整", (detail or "結構衝突") + "｜多空訊號打架", ma_detail, bos_tag
    if clear_bull and (
        "CHOCH轉空" in detail_parts or "MA空頭" in detail_parts or score < 0
    ):
        return "盤整", (detail or "結構衝突") + "｜多空訊號打架", ma_detail, bos_tag

    if ranging and not clear_bull and not clear_bear and abs(score) < 3:
        return "盤整", detail or "箱體盤整", ma_detail, bos_tag

    if clear_bull or score >= 3:
        return "偏多", detail or "多頭結構", ma_detail, bos_tag
    if clear_bear or score <= -3:
        return "偏空", detail or "空頭結構", ma_detail, bos_tag
    if ranging:
        return "盤整", detail or "箱體盤整", ma_detail, bos_tag
    return "盤整", detail or "方向未明", ma_detail, bos_tag


def build_zones(candles: list[Candle], atr_v: float) -> tuple[list[Zone], list[Zone]]:
    """用 swing 聚合成支撐/壓力區。"""
    highs_i, lows_i = find_swings(candles, 2, 2)
    band = max(atr_v * 0.4, candles[-1].close * 0.002)

    def cluster(indices: list[int], use_high: bool) -> list[Zone]:
        levels = [(candles[i].high if use_high else candles[i].low) for i in indices]
        if not levels:
            return []
        levels = sorted(levels)
        groups: list[list[float]] = [[levels[0]]]
        for lv in levels[1:]:
            if abs(lv - groups[-1][-1]) <= band:
                groups[-1].append(lv)
            else:
                groups.append([lv])
        zones = []
        kind = "resistance" if use_high else "support"
        for g in groups:
            lo, hi = min(g) - band * 0.3, max(g) + band * 0.3
            zones.append(Zone(kind=kind, lo=lo, hi=hi, strength=len(g), touches=len(g)))
        zones.sort(key=lambda z: z.strength, reverse=True)
        return zones

    supports = cluster(lows_i[-12:], False)
    resists = cluster(highs_i[-12:], True)

    # 只保留相對現價有意義的區（附近）
    px = candles[-1].close
    supports = [z for z in supports if z.hi <= px * 1.02][:5]
    resists = [z for z in resists if z.lo >= px * 0.98][:5]
    # 若被濾光，退回近20根高低
    if not supports:
        lo = min(c.low for c in candles[-20:])
        supports = [Zone("support", lo - band * 0.2, lo + band * 0.5, 1, 1)]
    if not resists:
        hi = max(c.high for c in candles[-20:])
        resists = [Zone("resistance", hi - band * 0.5, hi + band * 0.2, 1, 1)]
    return supports, resists


def _at_local_high_for_fade(
    candles: list[Candle],
    last: Candle,
    atr_v: float,
    resists: list[Zone],
    active_zone: Zone | None,
    priority_zone: Zone | None,
) -> tuple[bool, str]:
    """
    高位見頂位置：近壓力區，或靠近近段高點，且尚未從高點崩很遠。
    """
    window = candles[-24:] if len(candles) >= 24 else candles
    if not window or atr_v <= 0:
        return False, "無波動基準"
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    # 已從高點跌超約 2×ATR → 追空，不算見頂
    if hi - last.close > atr_v * 2.0:
        return False, "已離高點過遠"
    z = active_zone or priority_zone
    if z and z.kind == "resistance" and (
        z.near(last.high, atr_v) or z.near(last.close, atr_v) or z.contains(last.close, atr_v * 0.15)
    ):
        return True, "近壓力區"
    if any(
        r.near(last.high, atr_v) or r.near(last.close, atr_v) for r in resists[:4]
    ):
        return True, "近壓力位"
    if last.high >= hi - atr_v * 0.45:
        return True, "近段高點"
    if hi > lo and (last.close - lo) / (hi - lo) >= 0.68:
        return True, "近段上緣"
    return False, "未在高位"


def detect_candle_signals(c: Candle, prev: Candle, atr_v: float) -> list[str]:
    sigs: list[str] = []
    body = c.body
    rng = c.range
    if rng <= 0:
        return sigs

    # 十字 K / Doji：實體很小
    if body / rng <= 0.12:
        sigs.append("十字K")

    # 收針 Pin：影線長、實體相對小
    if c.lower_wick >= max(body * 2.0, rng * 0.55) and c.close > c.mid:
        sigs.append("下影收針")
    if c.upper_wick >= max(body * 2.0, rng * 0.55) and c.close < c.mid:
        sigs.append("上影收針")

    # 吞噬 Engulfing
    if c.bullish and not prev.bullish and c.close >= prev.open and c.open <= prev.close:
        if c.body > prev.body * 0.9:
            sigs.append("多頭吞噬")
    if (not c.bullish) and prev.bullish and c.open >= prev.close and c.close <= prev.open:
        if c.body > prev.body * 0.9:
            sigs.append("空頭吞噬")

    # 假突破收回（相對前高/前低）
    if c.high > prev.high and c.close < prev.high and c.close < c.open:
        sigs.append("假突破收回(上)")
    if c.low < prev.low and c.close > prev.low and c.close > c.open:
        sigs.append("假突破收回(下)")

    return sigs


def wick_half_zone(c: Candle, side: str) -> tuple[float, float] | None:
    """反應後的影線 0.5 觀察區。"""
    if side == "long":
        # 下影中段
        tip, base = c.low, min(c.open, c.close)
        if base <= tip:
            return None
        mid = (tip + base) / 2
        return (tip, mid) if tip < mid else (mid, tip)
    tip, base = c.high, max(c.open, c.close)
    if tip <= base:
        return None
    mid = (tip + base) / 2
    return (mid, tip) if mid < tip else (tip, mid)


def overlap_half(c: Candle, prev: Candle) -> tuple[float, float] | None:
    """重疊 K 0.5：兩根重疊區間的中段。"""
    lo = max(c.low, prev.low)
    hi = min(c.high, prev.high)
    if hi <= lo:
        return None
    mid = (lo + hi) / 2
    # 取重疊區下半 / 上半皆可，回傳整段中點帶
    band = (hi - lo) * 0.25
    return (mid - band, mid + band)


def vacuum_zone(candles: list[Candle]) -> tuple[float, float] | None:
    """真空區：相鄰 K 實體之間出現缺口 / 失衡。"""
    if len(candles) < 3:
        return None
    a, b = candles[-3], candles[-2]
    # bullish FVG: a.high < b.low
    if b.low > a.high:
        return (a.high, b.low)
    # bearish FVG: a.low > b.high
    if b.high < a.low:
        return (b.high, a.low)
    # 也可看 -2 與 -1
    a, b = candles[-2], candles[-1]
    if b.low > a.high:
        return (a.high, b.low)
    if b.high < a.low:
        return (b.high, a.low)
    return None


@dataclass
class FibAnalysis:
    """05 Fibo：回測區與力道返還。"""
    ready: bool  # 有明確方向 + 明確推力才可畫
    side: str  # long / short / none
    swing_a: float  # 起點（多方=低，空方=高）
    swing_b: float  # 終點（多方=高，空方=低）
    impulse: float  # |B-A|
    levels: dict[str, float]  # 0.382/0.5/0.618 與 extension
    depth: str  # 淺回測|中段回測|深回測|未回測|過深|無
    at_fib: bool
    fib_tag: str  # 當前最近的回測位名稱
    fib_price: float
    force_return_tag: str  # -0.382 / -0.5 / -0.618
    force_return_price: float
    detail: str
    note: str


def find_impulse_swing(
    candles: list[Candle],
    structure: str,
) -> tuple[float, float, int, int] | None:
    """
    找最近一段明確推力的 swing。
    多方：低→高；空方：高→低。回傳 (A, B, idx_a, idx_b)
    """
    highs_i, lows_i = find_swings(candles, 2, 2)
    if not highs_i or not lows_i:
        return None

    if structure == "偏多":
        # 最近高點 B，其前最近低點 A
        b_i = highs_i[-1]
        a_candidates = [i for i in lows_i if i < b_i]
        if not a_candidates:
            return None
        a_i = a_candidates[-1]
        a, b = candles[a_i].low, candles[b_i].high
        if b <= a:
            return None
        return a, b, a_i, b_i

    if structure == "偏空":
        b_i = lows_i[-1]
        a_candidates = [i for i in highs_i if i < b_i]
        if not a_candidates:
            return None
        a_i = a_candidates[-1]
        a, b = candles[a_i].high, candles[b_i].low
        if a <= b:
            return None
        return a, b, a_i, b_i

    return None


def fib_levels_from_impulse(side: str, swing_a: float, swing_b: float) -> dict[str, float]:
    """
    標準畫法（圖中 1→0）：
      多方：A=低(1) B=高(0)；回測價 = B - (B-A)*r
      空方：A=高(1) B=低(0)；回測價 = B + (A-B)*r
    延伸（力道返還 -r）：超越 0 的延續目標。
    """
    impulse = abs(swing_b - swing_a)
    if impulse <= 0:
        return {}
    out: dict[str, float] = {"0": swing_b, "1": swing_a}
    for r, key in ((0.382, "0.382"), (0.5, "0.5"), (0.618, "0.618")):
        if side == "long":
            out[key] = swing_b - impulse * r
            out[f"-{key}"] = swing_b + impulse * r
        else:
            out[key] = swing_b + impulse * r
            out[f"-{key}"] = swing_b - impulse * r
    return out


def classify_fib_depth(
    side: str,
    px: float,
    levels: dict[str, float],
    atr_v: float,
) -> tuple[str, str, float, bool]:
    """
    判斷回測深度。回傳 (depth標籤, 最近fib名, 價, 是否在位上)
    """
    retr = [("0.382", levels.get("0.382", 0)), ("0.5", levels.get("0.5", 0)), ("0.618", levels.get("0.618", 0))]
    retr = [(n, p) for n, p in retr if p]
    if not retr:
        return "無", "", 0.0, False

    # 最近的回測位
    nearest = min(retr, key=lambda x: abs(px - x[1]))
    at = price_near_level(px, nearest[1], atr_v)

    zero = levels.get("0", 0)
    one = levels.get("1", 0)

    # 相對推力，價格回撤比例
    impulse = abs(zero - one)
    if impulse <= 0:
        return "無", nearest[0], nearest[1], at

    if side == "long":
        # 0=高, 1=低；回撤比例 = (高-現價)/impulse
        ratio = (zero - px) / impulse
    else:
        ratio = (px - zero) / impulse

    if ratio < 0.15:
        depth = "未回測"  # 仍靠近推力末端
    elif ratio < 0.45:
        depth = "淺回測"
    elif ratio < 0.56:
        depth = "中段回測"
    elif ratio <= 0.72:
        depth = "深回測"
    else:
        depth = "過深"  # 可能結構破壞

    # 若貼著某一檔，以該檔命名為主
    if at:
        mapping = {"0.382": "淺回測", "0.5": "中段回測", "0.618": "深回測"}
        depth = mapping.get(nearest[0], depth)

    return depth, nearest[0], nearest[1], at


def force_return_target(depth: str, fib_tag: str, levels: dict[str, float]) -> tuple[str, float]:
    """
    力道返還止盈觀察點：
      回測到 0.382 並守住 → 觀察 -0.382
      回測到 0.5   並守住 → 觀察 -0.5
      回測到 0.618 並守住 → 觀察 -0.618（對稱力道返還）
    """
    key = fib_tag
    if depth == "淺回測" or fib_tag == "0.382":
        key = "0.382"
    elif depth == "中段回測" or fib_tag == "0.5":
        key = "0.5"
    elif depth == "深回測" or fib_tag == "0.618":
        key = "0.618"
    else:
        return "", 0.0
    ext = f"-{key}"
    return ext, levels.get(ext, 0.0)


def analyze_fibo(
    candles: list[Candle],
    structure: str,
    atr_v: float,
    has_reaction: bool,
) -> FibAnalysis:
    """
    05 Fibo：把回測區看清楚。
    Fibo 不是進場訊號，是估計回測可能停哪裡；要等價格在位上有反應。
    """
    empty = FibAnalysis(
        ready=False,
        side="none",
        swing_a=0,
        swing_b=0,
        impulse=0,
        levels={},
        depth="無",
        at_fib=False,
        fib_tag="",
        fib_price=0,
        force_return_tag="",
        force_return_price=0,
        detail="方向或推力不明，先不畫 Fibo",
        note="Fibo 要先有明確方向與推力；不能只靠 Fibo 下單。",
    )
    if structure not in ("偏多", "偏空") or len(candles) < 40:
        return empty

    side = "long" if structure == "偏多" else "short"
    impulse_sw = find_impulse_swing(candles, structure)
    if not impulse_sw:
        return empty

    swing_a, swing_b, a_i, b_i = impulse_sw
    impulse = abs(swing_b - swing_a)
    # 推力要夠明顯（相對 ATR）
    if impulse < atr_v * 1.2:
        empty.detail = f"推力偏弱（{fmt_short(impulse)} < 1.2×ATR），不畫 Fibo"
        return empty

    levels = fib_levels_from_impulse(side, swing_a, swing_b)
    px = candles[-1].close
    depth, fib_tag, fib_price, at_fib = classify_fib_depth(side, px, levels, atr_v)
    fr_tag, fr_price = force_return_target(depth, fib_tag, levels)

    draw = "低→高觀察支撐" if side == "long" else "高→低觀察壓力"
    detail = (
        f"{'多方' if side == 'long' else '空方'}延續｜{draw}｜"
        f"A={fmt_short(swing_a)} → B={fmt_short(swing_b)}｜"
        f"0.382={fmt_short(levels.get('0.382', 0))} "
        f"0.5={fmt_short(levels.get('0.5', 0))} "
        f"0.618={fmt_short(levels.get('0.618', 0))}"
    )

    note = "Fibo 只說明回測深淺，不是進場訊號；要等位上反應。"
    if depth == "淺回測":
        note += " 淺回測=力道強、很快被接回。"
    elif depth == "中段回測":
        note += " 中段回測（0.5 常用實務位）。"
    elif depth == "深回測":
        note += " 深回測若守住，原推力仍可能延續。"
    elif depth == "過深":
        note += " 回測過深，原結構可能失效，勿盲接。"
    elif depth == "未回測":
        note += " 尚未回到 Fibo 區 → 等回測。"

    if at_fib and has_reaction and fr_price:
        note += f" 位上有反應 → 力道返還觀察 {fr_tag}≈{fmt_short(fr_price)}。"
    elif at_fib and not has_reaction:
        note += " 已到 Fibo 位，尚無反應 → 先觀察。"

    return FibAnalysis(
        ready=True,
        side=side,
        swing_a=swing_a,
        swing_b=swing_b,
        impulse=impulse,
        levels=levels,
        depth=depth,
        at_fib=at_fib,
        fib_tag=fib_tag,
        fib_price=fib_price,
        force_return_tag=fr_tag,
        force_return_price=fr_price,
        detail=detail,
        note=note,
    )


def fib_checklist_lines(fib: FibAnalysis) -> list[tuple[str, bool, str]]:
    """詳情頁用的 Fibo 檢查列。"""
    if not fib.ready:
        return [
            ("Fibo 前提", False, fib.detail),
            ("提醒", False, fib.note),
        ]
    lines = [
        ("Fibo 前提", True, fib.detail),
        (
            "回測深度",
            fib.depth in ("淺回測", "中段回測", "深回測"),
            f"{fib.depth}"
            + (f"｜貼近 {fib.fib_tag}≈{fmt_short(fib.fib_price)}" if fib.fib_tag else ""),
        ),
        (
            "位上反應",
            fib.at_fib,
            "已到 Fibo 觀察區" if fib.at_fib else "尚未到 0.382/0.5/0.618，等回測",
        ),
        (
            "力道返還止盈",
            bool(fib.force_return_price and fib.at_fib),
            (
                f"守住 {fib.fib_tag} → 觀察 {fib.force_return_tag}≈{fmt_short(fib.force_return_price)}"
                if fib.force_return_price
                else "需先確認回測深度"
            ),
        ),
        ("提醒", True, fib.note),
    ]
    return lines


def smc_hints(candles: list[Candle], structure: str) -> list[str]:
    hints: list[str] = []
    if len(candles) < 25:
        return hints
    look = candles[-25:]
    # Order Block 近似：結構方向上最後一根反向實體
    if structure == "偏多":
        for c in reversed(look[:-1]):
            if not c.bullish and c.body / c.range > 0.4:
                hints.append(f"需求OB≈{c.low:.6g}-{c.high:.6g}")
                break
        # CHOCH/BOS 已在結構
        highs_i, lows_i = find_swings(candles, 2, 2)
        if lows_i and candles[-1].close > candles[highs_i[-1]].high if highs_i else False:
            hints.append("SMC:BOS多")
    elif structure == "偏空":
        for c in reversed(look[:-1]):
            if c.bullish and c.body / c.range > 0.4:
                hints.append(f"供給OB≈{c.low:.6g}-{c.high:.6g}")
                break
        highs_i, _ = find_swings(candles, 2, 2)
        if highs_i and candles[-1].close < candles[highs_i[-1]].low:
            hints.append("SMC:BOS空")
    return hints[:3]


def chart_pattern(candles: list[Candle]) -> list[str]:
    pats: list[str] = []
    if len(candles) < 20:
        return pats
    highs_i, lows_i = find_swings(candles, 2, 2)
    if len(highs_i) >= 2:
        h1, h2 = candles[highs_i[-2]].high, candles[highs_i[-1]].high
        if abs(h1 - h2) / max(h1, 1e-12) < 0.004:
            pats.append("雙頂雛形")
    if len(lows_i) >= 2:
        l1, l2 = candles[lows_i[-2]].low, candles[lows_i[-1]].low
        if abs(l1 - l2) / max(l1, 1e-12) < 0.004:
            pats.append("雙底雛形")
    return pats


def price_near_level(price: float, level: float, atr_v: float) -> bool:
    return abs(price - level) <= max(atr_v * 0.35, price * 0.0015)


def detect_equal_levels(candles: list[Candle], atr_v: float) -> tuple[str, list[float], list[float]]:
    """EQH / EQL：相近的等高、等低。"""
    highs_i, lows_i = find_swings(candles, 2, 2)
    band = max(atr_v * 0.25, candles[-1].close * 0.001)
    eqh: list[float] = []
    eql: list[float] = []
    hs = [candles[i].high for i in highs_i[-8:]]
    ls = [candles[i].low for i in lows_i[-8:]]
    for i in range(len(hs)):
        for j in range(i + 1, len(hs)):
            if abs(hs[i] - hs[j]) <= band:
                eqh.append((hs[i] + hs[j]) / 2)
    for i in range(len(ls)):
        for j in range(i + 1, len(ls)):
            if abs(ls[i] - ls[j]) <= band:
                eql.append((ls[i] + ls[j]) / 2)
    # 去重
    def uniq(vals: list[float]) -> list[float]:
        out: list[float] = []
        for v in sorted(vals):
            if not out or abs(v - out[-1]) > band:
                out.append(v)
        return out[-3:]

    eqh, eql = uniq(eqh), uniq(eql)
    parts = []
    if eqh:
        parts.append("EQH:" + "/".join(fmt_short(x) for x in eqh))
    if eql:
        parts.append("EQL:" + "/".join(fmt_short(x) for x in eql))
    return ("｜".join(parts) if parts else "無明顯EQH/EQL"), eqh, eql


def rsi_series(closes: list[float], period: int = 14) -> list[float]:
    if len(closes) < period + 1:
        return []
    out: list[float] = [50.0] * period
    gains = [0.0]
    losses = [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[1 : period + 1]) / period
    avg_l = sum(losses[1 : period + 1]) / period
    rs = avg_g / avg_l if avg_l else 100
    out.append(100 - 100 / (1 + rs))
    for i in range(period + 1, len(closes)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l else 100
        out.append(100 - 100 / (1 + rs))
    # pad to len(closes)
    while len(out) < len(closes):
        out.insert(0, 50.0)
    return out[-len(closes) :]


def detect_rsi_divergence(candles: list[Candle]) -> str:
    closes = [c.close for c in candles]
    rsis = rsi_series(closes, 14)
    if len(closes) < 30 or len(rsis) < 30:
        return "RSI資料不足"
    highs_i, lows_i = find_swings(candles, 2, 2)
    # 看最近兩個 swing
    if len(highs_i) >= 2:
        i1, i2 = highs_i[-2], highs_i[-1]
        if closes[i2] > closes[i1] and rsis[i2] < rsis[i1] - 1:
            return "RSI頂背離(價高新低)"
        if closes[i2] < closes[i1] and rsis[i2] > rsis[i1] + 1:
            return "RSI隱性頂背離"
    if len(lows_i) >= 2:
        i1, i2 = lows_i[-2], lows_i[-1]
        if closes[i2] < closes[i1] and rsis[i2] > rsis[i1] + 1:
            return "RSI底背離(價低新高)"
        if closes[i2] > closes[i1] and rsis[i2] < rsis[i1] - 1:
            return "RSI隱性底背離"
    last_r = rsis[-1]
    if last_r >= 70:
        return f"RSI={last_r:.0f}超買區"
    if last_r <= 30:
        return f"RSI={last_r:.0f}超賣區"
    return f"RSI={last_r:.0f}無明顯背離"


def regular_rsi_divergence_side(candles: list[Candle]) -> str:
    """只回傳常規背離方向：top / bottom / ''（不含隱性）。"""
    tag = detect_rsi_divergence(candles)
    if "頂背離" in tag and "隱性" not in tag:
        return "top"
    if "底背離" in tag and "隱性" not in tag:
        return "bottom"
    return ""


def approx_poc(candles: list[Candle], *, buckets: int = 24) -> tuple[float, str]:
    """
    近似 Volume Profile POC：近段依價格桶累積成交量，取最大量桶中價。
    （Trader Willy 背離戰法用的 4H POC 錨點）
    """
    window = candles[-48:] if len(candles) >= 48 else candles
    if not window:
        return 0.0, "無POC"
    lo = min(c.low for c in window)
    hi = max(c.high for c in window)
    if hi <= lo:
        best = max(window, key=lambda c: c.volume)
        return best.mid, f"POC≈{fmt_short(best.mid)}(量能峰)"
    n = max(8, min(buckets, len(window)))
    width = (hi - lo) / n
    vol = [0.0] * n
    for c in window:
        # 成交量依 K 高低落在桶內均分
        i0 = int((c.low - lo) / width)
        i1 = int((c.high - lo) / width)
        i0 = max(0, min(n - 1, i0))
        i1 = max(0, min(n - 1, i1))
        if i1 < i0:
            i0, i1 = i1, i0
        share = c.volume / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vol[i] += share
    best_i = max(range(n), key=lambda i: vol[i])
    poc = lo + (best_i + 0.5) * width
    return poc, f"POC≈{fmt_short(poc)}(VP量能峰)"


def analyze_divergence_poc(
    candles: list[Candle],
    *,
    htf_candles: list[Candle] | None,
    px: float,
    atr_v: float,
    structure: str,
) -> tuple[float, str, list[str], float]:
    """
    Trader Willy 背離戰法：
    - 4H（或備用本地）RSI 常規頂/底背離
    - 價格靠近 4H POC（量能控制點）作匯合／止盈錨
    回傳 (poc_4h, 說明, 額外匯合標籤, 建議止盈錨或 0)
    """
    poc_src = htf_candles if htf_candles and len(htf_candles) >= 30 else candles
    poc, poc_txt = approx_poc(poc_src)
    near_poc = poc > 0 and price_near_level(px, poc, atr_v * 1.2)
    div_htf = regular_rsi_divergence_side(htf_candles) if htf_candles and len(htf_candles) >= 30 else ""
    div_loc = regular_rsi_divergence_side(candles)
    div = div_htf or div_loc
    src = "4H" if div_htf else ("本地" if div_loc else "")

    tags: list[str] = []
    note = ""
    tp_anchor = 0.0

    if div == "top":
        note = f"背離戰法:頂背離({src})｜{poc_txt}"
        if near_poc:
            tags.append("頂背離+近4H POC")
            note += "｜近POC匯合"
        elif structure == "偏空":
            tags.append("頂背離偏空")
        # 空單止盈常看回 POC（POC 在現價下方時）
        if poc > 0 and poc < px:
            tp_anchor = poc
    elif div == "bottom":
        note = f"背離戰法:底背離({src})｜{poc_txt}"
        if near_poc:
            tags.append("底背離+近4H POC")
            note += "｜近POC匯合"
        elif structure == "偏多":
            tags.append("底背離偏多")
        if poc > 0 and poc > px:
            tp_anchor = poc
    elif poc > 0:
        note = f"背離戰法:{poc_txt}"
        if near_poc:
            tags.append("近4H POC")

    if near_poc and "近POC" not in tags:
        # 與舊標籤相容
        pass
    return poc, note, tags, tp_anchor


def detect_liquidity_zones(candles: list[Candle], structure: str, atr_v: float) -> tuple[str, bool]:
    """
    失衡(FVG) / OB / BPR / 真空區。
    回傳 (說明, 是否與現價匯合)
    """
    bits: list[str] = []
    px = candles[-1].close
    near = False

    vac = vacuum_zone(candles)
    if vac:
        bits.append(f"真空/失衡{fmt_short(vac[0])}-{fmt_short(vac[1])}")
        if vac[0] - atr_v * 0.2 <= px <= vac[1] + atr_v * 0.2:
            near = True

    # Order Block：結構方向上最後一根反向實體
    look = candles[-30:]
    ob: tuple[float, float] | None = None
    if structure == "偏多":
        for c in reversed(look[:-1]):
            if not c.bullish and c.body / c.range > 0.35:
                ob = (c.low, c.high)
                break
    elif structure == "偏空":
        for c in reversed(look[:-1]):
            if c.bullish and c.body / c.range > 0.35:
                ob = (c.low, c.high)
                break
    if ob:
        bits.append(f"OB{fmt_short(ob[0])}-{fmt_short(ob[1])}")
        if ob[0] - atr_v * 0.3 <= px <= ob[1] + atr_v * 0.3:
            near = True

    # BPR：近期多空實體重疊帶
    if len(candles) >= 6:
        up = next((c for c in reversed(candles[-8:]) if c.bullish and c.body / c.range > 0.5), None)
        dn = next((c for c in reversed(candles[-8:]) if (not c.bullish) and c.body / c.range > 0.5), None)
        if up and dn:
            lo, hi = max(min(up.open, up.close), min(dn.open, dn.close)), min(
                max(up.open, up.close), max(dn.open, dn.close)
            )
            if hi > lo:
                bits.append(f"BPR{fmt_short(lo)}-{fmt_short(hi)}")
                if lo <= px <= hi:
                    near = True

    if not bits:
        return "無明顯失衡/OB/BPR/真空", False
    return "｜".join(bits[:4]), near


def check_mtf_alignment(structure: str, mtf: dict[str, str]) -> tuple[bool, str]:
    """
    多週期一致性：操作週期方向不可明顯逆 4H/日。
    小幣週線常滯後、參考價值低 → 對齊不計入 1W（畫面仍可顯示）。
    回傳 (是否對齊, 說明)
    """
    if structure not in ("偏多", "偏空"):
        return True, "盤整不檢查高週期對齊"

    # 高週期權重：4H/1D/1M（不含 1W）
    htf_keys = [k for k in ("4H", "1D", "1M") if k in mtf]
    if not htf_keys:
        # 只有 1H 也參考；完全抓不到高週期 → 不可當對齊（避免 KORU 類無 MTF 仍 YES）
        if "1H" in mtf:
            htf_keys = ["1H"]
        else:
            return False, "無高週期資料"

    opposite = "偏空" if structure == "偏多" else "偏多"
    same = structure
    opp_list = [k for k in htf_keys if mtf.get(k) == opposite]
    same_list = [k for k in htf_keys if mtf.get(k) == same]
    range_list = [k for k in htf_keys if mtf.get(k) == "盤整"]

    detail = ",".join(f"{k}:{mtf[k]}" for k in htf_keys)

    # 硬規則：做多必須日線偏多；有 4H 資料時 4H 也要偏多
    if structure == "偏多":
        if mtf.get("1D") != "偏多":
            tag = "禁逆日線做多" if mtf.get("1D") == "偏空" else "做多需日線偏多"
            return False, f"{tag}｜{detail}"
        if "4H" in mtf and mtf.get("4H") != "偏多":
            return False, f"做多需4H偏多｜{detail}"

    # 任一關鍵高週期（4H/1D）逆向 → 不對齊
    critical = [k for k in ("4H", "1D") if k in mtf and mtf[k] == opposite]
    if critical:
        return False, f"逆高週期({','.join(critical)})｜{detail}"

    # 多數高週期逆向
    if len(opp_list) >= 2 and len(same_list) == 0:
        return False, f"高週期多數逆向｜{detail}"

    # YES 必須至少一個高週期同向（純盤整不算對齊，避免亂扣板機）
    if same_list:
        return True, f"高週期同向({','.join(same_list)})｜{detail}"
    if range_list and not opp_list:
        return False, f"高週期無同向確認（多為盤整）｜{detail}"
    return False, f"高週期未確認同向｜{detail}"


def summarize_missing_steps(workflow: list[tuple[str, bool, str]]) -> str:
    """把未通過的步驟收成短字串。"""
    miss: list[str] = []
    for title, ok, _ in workflow:
        if ok:
            continue
        # title 如 "03 找 BOS / CHOCH" → "03 BOS"
        num = title[:2] if len(title) >= 2 else title
        short = title
        for key, label in (
            ("高週期", "高週期"),
            ("結構", "結構"),
            ("BOS", "BOS"),
            ("支撐壓力", "支撐壓力"),
            ("Fibo", "Fibo"),
            ("失衡", "流動性"),
            ("MA", "指標"),
            ("回測", "回測反應"),
            ("止損", "止損"),
            ("盈虧比", "盈虧比"),
            ("進場", "進場"),
        ):
            if key in title:
                short = f"{num} {label}"
                break
        miss.append(short)
    if not miss:
        return "齊全"
    return "缺：" + "、".join(miss[:5])


def session_open_bias(px: float, opens: dict[str, float]) -> str:
    """相對日/週/月/晚盤開盤的位置。"""
    if not opens:
        return "無開盤參考"
    parts = []
    for k, label in (("DAY", "日開"), ("EVE20", "晚8開"), ("WEEK", "週開"), ("MONTH", "月開")):
        if k not in opens:
            continue
        o = opens[k]
        rel = "上" if px >= o else "下"
        parts.append(f"{label}{fmt_short(o)}(價在{rel})")
    return "｜".join(parts) if parts else "無開盤參考"


def build_workflow11(
    *,
    mtf: dict[str, str],
    opens: dict[str, float],
    structure: str,
    struct_detail: str,
    bos_tag: str,
    supports: list[Zone],
    resists: list[Zone],
    eq_txt: str,
    eqh: list[float],
    eql: list[float],
    fib: FibAnalysis,
    liq_txt: str,
    liq_near: bool,
    ind_txt: str,
    ind_ok: bool,
    at_zone: bool,
    waiting_retest: bool,
    chasing: bool,
    reaction_ok: bool,
    candle_signals: list[str],
    side: str,
    stop: float,
    inv_reason: str,
    tp: float,
    rr: float,
    rr_ok: bool,
    trigger_ready: bool,
    px: float,
) -> list[tuple[str, bool, str]]:
    """實戰做單流程 11 步。"""
    # 01 高週期
    mtf_txt = ",".join(f"{k}:{v}" for k, v in mtf.items()) if mtf else "未抓高週期"
    mtf_ok = bool(mtf) and any(v in ("偏多", "偏空") for v in mtf.values())
    open_txt = session_open_bias(px, opens)
    step01 = (
        "01 定高週期方向",
        mtf_ok or structure in ("偏多", "偏空"),
        f"{mtf_txt or structure}｜{open_txt}",
    )

    # 02 結構
    has_hh_hl = "HH" in struct_detail and "HL" in struct_detail
    has_lh_ll = "LH" in struct_detail and "LL" in struct_detail
    step02 = (
        "02 看結構 HH/HL 或 LH/LL",
        structure in ("偏多", "偏空") and (has_hh_hl or has_lh_ll or "MA" in struct_detail),
        f"{structure}｜{struct_detail}",
    )

    # 03 BOS/CHOCH
    step03 = (
        "03 找 BOS / CHOCH",
        bool(bos_tag),
        bos_tag or "尚未出現明確 BOS/CHOCH",
    )

    # 04 支撐壓力 EQH/EQL
    has_sr = bool(supports or resists)
    near_eq = any(price_near_level(px, lv, abs(px) * 0.002 + 1e-9) for lv in eqh + eql)
    step04 = (
        "04 標支撐壓力 / EQH / EQL",
        has_sr,
        (
            f"支撐×{len(supports)} 壓力×{len(resists)}｜{eq_txt}"
            + ("｜現價近等高/等低" if near_eq else "")
        ),
    )

    # 05 Fibo
    fib_ok = fib.ready and fib.depth in ("淺回測", "中段回測", "深回測", "未回測")
    step05 = (
        "05 畫 Fibo 回測區與力道返還",
        fib.ready,
        fib.detail if fib.ready else fib.detail,
    )
    if fib.ready and fib.force_return_price:
        step05 = (
            step05[0],
            fib_ok,
            step05[2]
            + f"｜{fib.depth}"
            + (f"｜力道返還{fib.force_return_tag}≈{fmt_short(fib.force_return_price)}" if fib.at_fib else ""),
        )

    # 06 失衡 OB BPR 真空
    step06 = (
        "06 看失衡 / OB / BPR / 真空區",
        "無明顯" not in liq_txt,
        liq_txt + ("｜與現價匯合" if liq_near else ""),
    )

    # 07 指標
    step07 = (
        "07 搭配 MA / RSI / POC",
        ind_ok,
        ind_txt,
    )

    # 08 回測 + K反應
    step08 = (
        "08 等回測並確認 K 棒反應",
        at_zone and reaction_ok and not chasing,
        (
            f"{'已回測' if at_zone else ('等回測' if waiting_retest else '未回測')}｜"
            f"{'有反應:' + '、'.join(candle_signals[:3]) if reaction_ok else '尚無反應'}"
            + ("｜追價略過" if chasing else "")
        ),
    )

    # 09 止損
    step09 = (
        "09 先找止損（理由失效處）",
        stop > 0 and side != "none",
        f"SL≈{fmt_short(stop)}｜{inv_reason}" if stop else "尚無方向單，未定失效價",
    )

    # 10 目標 RR
    step10 = (
        "10 找目標並算盈虧比",
        rr_ok and side != "none",
        (
            f"TP≈{fmt_short(tp)}｜RR={rr:.2f}{'✓' if rr_ok else f'(需≥{MIN_RR})'}"
            if side != "none" and tp
            else f"尚未形成可算 RR 的單｜門檻≥{MIN_RR}"
        ),
    )

    # 11 才決定進場
    steps_core = [step01, step02, step04, step08, step09, step10]
    core_pass = sum(1 for s in steps_core if s[1])
    decide_ok = trigger_ready and core_pass >= 5
    step11 = (
        "11 以上都過了，才決定進場",
        decide_ok,
        (
            f"{'可進場觀察' if decide_ok else '暫不進場'}｜"
            f"方向{'✓' if structure in ('偏多','偏空') else '✗'} "
            f"位置{'✓' if at_zone else '✗'} "
            f"止損{'✓' if stop > 0 and side != 'none' else '✗'} "
            f"目標{'✓' if rr_ok else '✗'}｜"
            "好單不是衝動，是四者都說得通"
        ),
    )

    return [step01, step02, step03, step04, step05, step06, step07, step08, step09, step10, step11]


def build_range_box(candles: list[Candle], lookback: int = 30) -> Zone | None:
    """
    盤整用的真實箱體：近 N 根高低，不是最近一條微支撐/壓力。
    （舊邏輯會把 ETH 收成 8U 寬的假箱體）
    """
    n = min(lookback, len(candles))
    if n < 10:
        return None
    window = candles[-n:]
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    if hi <= lo:
        return None
    return Zone(kind="range", lo=lo, hi=hi, strength=n, touches=0)


def build_day_range_box(
    candles: list[Candle],
    *,
    day: str | None = None,
    tz_offset_hours: int = 8,
    min_bars: int = 4,
) -> Zone | None:
    """
    取指定曆日（預設：最後一根 K 所在日）的最高／最低，供日內高拋低吸。
    day 格式 YYYY-MM-DD；時區預設 UTC+8（台灣）。
    """
    if len(candles) < min_bars:
        return None
    from datetime import datetime, timedelta, timezone

    tz = timezone(timedelta(hours=tz_offset_hours))
    if day is None:
        day = datetime.fromtimestamp(candles[-1].time / 1000, tz=timezone.utc).astimezone(tz).strftime(
            "%Y-%m-%d"
        )

    day_cs = [
        c
        for c in candles
        if datetime.fromtimestamp(c.time / 1000, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
        == day
    ]
    if len(day_cs) < min_bars:
        # 今日 K 太少：退回最近 24h
        last_t = candles[-1].time
        day_cs = [c for c in candles if c.time >= last_t - 24 * 3600 * 1000]
    if len(day_cs) < min_bars:
        return None
    hi = max(c.high for c in day_cs)
    lo = min(c.low for c in day_cs)
    if hi <= lo:
        return None
    return Zone(kind="range", lo=lo, hi=hi, strength=len(day_cs), touches=0)


def build_trading_session_high_low(
    candles: list[Candle],
    *,
    start_hour_utc: int,
    end_hour_utc: int,
    min_bars: int = 2,
    now_ms: int | None = None,
) -> tuple[float, float, bool] | None:
    """
    取最近一個交易時段（進行中或剛結束）的高低。
    時段以 UTC 小時 [start, end) 定義（需 start < end，不跨日）。
    回傳 (lo, hi, 是否進行中)；K 不足則 None。
    """
    if not candles or not (0 <= start_hour_utc < end_hour_utc <= 24):
        return None
    from datetime import datetime, timedelta, timezone

    now = datetime.fromtimestamp(
        (now_ms if now_ms is not None else candles[-1].time) / 1000,
        tz=timezone.utc,
    )
    d = now.date()
    start = datetime(d.year, d.month, d.day, start_hour_utc, 0, tzinfo=timezone.utc)
    end = datetime(d.year, d.month, d.day, end_hour_utc, 0, tzinfo=timezone.utc)
    active = False
    if now < start:
        start -= timedelta(days=1)
        end -= timedelta(days=1)
        win_end = end
    elif now >= end:
        win_end = end
    else:
        active = True
        win_end = now

    t0 = int(start.timestamp() * 1000)
    t1 = int(win_end.timestamp() * 1000)
    cs = [c for c in candles if t0 <= c.time < t1]
    if len(cs) < min_bars:
        return None
    hi = max(c.high for c in cs)
    lo = min(c.low for c in cs)
    if hi <= lo:
        return None
    return lo, hi, active


def build_adaptive_range_box(
    candles: list[Candle],
    *,
    target_width: float,
    min_lookback: int = 6,
    max_lookback: int = 48,
) -> Zone | None:
    """
    日內可用箱體：在近段 lookback 中找寬度最接近 target_width 的高低區間。
    若最近波動仍遠大於目標，則以現價為中心裁成約目標寬（仍夾在近段高低內）。
    """
    if target_width <= 0 or len(candles) < min_lookback:
        return None
    px = candles[-1].close
    max_n = min(max_lookback, len(candles))
    best: Zone | None = None
    best_score = float("inf")

    for n in range(min_lookback, max_n + 1):
        window = candles[-n:]
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        width = hi - lo
        if width <= 0:
            continue
        # 寬度貼近目標；價格最好在箱內；過寬加重罰
        score = abs(width - target_width) / target_width
        if width > target_width * 2.2:
            score += (width / target_width - 2.2) * 0.45
        if not (lo <= px <= hi):
            score += 0.35
        # 略偏好較短 lookback（更像「這一盤」）
        score += n * 0.004
        if score < best_score:
            best_score = score
            best = Zone(kind="range", lo=lo, hi=hi, strength=n, touches=0)

    if best is None:
        return None

    width = best.hi - best.lo
    if width > target_width * 1.85:
        # 過寬：裁成現價中心的目標帶，並限制在近段真實高低內
        n = max(min_lookback, min(best.strength, 12))
        window = candles[-n:]
        hi = max(c.high for c in window)
        lo = min(c.low for c in window)
        half = target_width / 2.0
        lo2 = max(lo, px - half)
        hi2 = min(hi, px + half)
        if hi2 - lo2 < target_width * 0.45:
            lo2, hi2 = px - half, px + half
        if hi2 > lo2:
            best = Zone(kind="range", lo=lo2, hi=hi2, strength=n, touches=0)
    return best


def nearest_priority_zone(
    structure: str,
    supports: list[Zone],
    resists: list[Zone],
    px: float,
    *,
    range_box: Zone | None = None,
) -> Zone | None:
    """看哪裡：偏多看支撐、偏空看壓力、盤整用近段箱體上下緣。"""
    if structure == "偏多" and supports:
        below = [z for z in supports if z.mid <= px * 1.005]
        pool = below or supports
        return min(pool, key=lambda z: abs(z.mid - px))
    if structure == "偏空" and resists:
        above = [z for z in resists if z.mid >= px * 0.995]
        pool = above or resists
        return min(pool, key=lambda z: abs(z.mid - px))
    # 盤整：用近段高低箱體（避免微區當成箱體）
    if range_box is not None:
        return range_box
    cands = list(supports) + list(resists)
    if not cands:
        return None
    return min(cands, key=lambda z: abs(z.mid - px))


def compute_invalidation(
    side: str,
    active_zone: Zone | None,
    signal_candle: Candle,
    atr_v: float,
    structure: str,
) -> tuple[float, str]:
    """
    找止損 = 做單理由失效處。
    多：跌破回測支撐/反應低點；空：突破回測壓力/反應高點。
    """
    if side == "long":
        base = signal_candle.low
        if active_zone:
            base = min(base, active_zone.lo)
        stop = base - atr_v * 0.15
        return stop, "跌破支撐/反應低點 → 多單理由失效"
    if side == "short":
        base = signal_candle.high
        if active_zone:
            base = max(base, active_zone.hi)
        stop = base + atr_v * 0.15
        return stop, "突破壓力/反應高點 → 空單理由失效"
    if structure == "偏多" and active_zone:
        return active_zone.lo - atr_v * 0.15, "若跌破此支撐，偏多回測邏輯失效"
    if structure == "偏空" and active_zone:
        return active_zone.hi + atr_v * 0.15, "若突破此壓力，偏空回測邏輯失效"
    return 0.0, "尚無明確失效價"


def compute_targets(
    side: str,
    entry: float,
    stop: float,
    supports: list[Zone],
    resists: list[Zone],
    atr_v: float,
) -> tuple[float, float]:
    """目標取對向結構位；並回傳 RR。"""
    risk = abs(entry - stop)
    if risk <= 0:
        return 0.0, 0.0
    if side == "long":
        # 目標：最近壓力，至少 1.5R
        tps = [z.lo for z in resists if z.lo > entry]
        raw_tp = min(tps) if tps else entry + max(risk * 2.0, atr_v * 2)
        min_tp = entry + risk * MIN_RR
        tp = max(raw_tp, min_tp)
        return tp, (tp - entry) / risk
    if side == "short":
        tps = [z.hi for z in supports if z.hi < entry]
        raw_tp = max(tps) if tps else entry - max(risk * 2.0, atr_v * 2)
        min_tp = entry - risk * MIN_RR
        tp = min(raw_tp, min_tp)
        return tp, (entry - tp) / risk
    return 0.0, 0.0


def evaluate_setup(
    symbol: str,
    candles: list[Candle],
    interval: str = "15m",
    mtf: dict[str, str] | None = None,
    opens: dict[str, float] | None = None,
    htf_poc_candles: list[Candle] | None = None,
) -> ChecklistResult:
    empty = ChecklistResult(
        symbol=symbol,
        interval=interval,
        structure="盤整",
        structure_detail="無資料",
        at_zone=False,
        zone_kind="none",
        zone_lo=0,
        zone_hi=0,
        note="K線不足",
    )
    if len(candles) < 40:
        return empty

    atr_v = atr(candles, 14)
    structure, struct_detail, ma_detail, bos_tag = detect_structure(candles)
    supports, resists = build_zones(candles, atr_v)
    last, prev = candles[-1], candles[-2]
    px = last.close
    mtf = mtf or {}
    opens = opens or {}

    # 多週期共識（若有提供）
    mtf_bull = sum(1 for v in mtf.values() if v == "偏多")
    mtf_bear = sum(1 for v in mtf.values() if v == "偏空")
    if mtf:
        if mtf_bull >= 2 and structure != "偏空":
            if structure == "盤整":
                structure = "偏多"
                struct_detail += "｜高週期偏多"
        elif mtf_bear >= 2 and structure != "偏多":
            if structure == "盤整":
                structure = "偏空"
                struct_detail += "｜高週期偏空"

    # ② 看哪裡：優先結構區；盤整用近段高低箱體
    range_box = build_range_box(candles, 30) if structure == "盤整" else None
    priority_zone = nearest_priority_zone(
        structure, supports, resists, px, range_box=range_box
    )
    active_zone: Zone | None = None
    if priority_zone and (
        priority_zone.near(px, atr_v)
        or priority_zone.near(last.low, atr_v)
        or priority_zone.near(last.high, atr_v)
        or (
            structure == "盤整"
            and priority_zone.contains(px, pad=atr_v * 0.1)
        )
    ):
        active_zone = priority_zone

    at_zone = active_zone is not None
    zone_kind = active_zone.kind if active_zone else (
        priority_zone.kind if priority_zone else "none"
    )
    zone_lo = (active_zone or priority_zone).lo if (active_zone or priority_zone) else 0.0
    zone_hi = (active_zone or priority_zone).hi if (active_zone or priority_zone) else 0.0

    # ③ 等回測：方向清楚但價格還沒回到區 → 等待，不追價
    waiting_retest = bool(
        structure in ("偏多", "偏空") and priority_zone and not at_zone
    )
    chasing = False
    if structure == "偏多" and not at_zone and priority_zone and px > priority_zone.hi + atr_v:
        chasing = True  # 遠離支撐追多
    if structure == "偏空" and not at_zone and priority_zone and px < priority_zone.lo - atr_v:
        chasing = True
    if structure == "盤整" and priority_zone:
        # 突破近段箱體上下緣才算追價
        if px > priority_zone.hi or px < priority_zone.lo:
            chasing = True

    # ④ 看反應
    sigs = detect_candle_signals(last, prev, atr_v)
    sigs_prev = detect_candle_signals(prev, candles[-3], atr_v) if len(candles) >= 3 else []
    candle_signals = list(dict.fromkeys(sigs + [f"{s}(前一根)" for s in sigs_prev if s not in sigs]))

    # 硬反應：當根收針／吞噬；軟反應：假突破／十字（含前一根）
    hard_bull = any(x in s for s in sigs for x in ("下影收針", "多頭吞噬"))
    hard_bear = any(x in s for s in sigs for x in ("上影收針", "空頭吞噬"))
    soft_bull = any(
        x in s for s in candle_signals for x in ("假突破收回(下)",)
    ) or ("十字K" in candle_signals and at_zone and zone_kind == "support")
    soft_bear = any(
        x in s for s in candle_signals for x in ("假突破收回(上)",)
    ) or ("十字K" in candle_signals and at_zone and zone_kind == "resistance")
    bull_react = hard_bull or soft_bull
    bear_react = hard_bear or soft_bear

    # 多空反應同時出現 = 方向衝突，不算有效反應
    react_conflict = (hard_bull and hard_bear) or (bull_react and bear_react)

    reaction_ok = False
    if at_zone and active_zone and candle_signals and not react_conflict:
        touched = (
            active_zone.near(last.low, atr_v)
            or active_zone.near(last.high, atr_v)
            or active_zone.near(prev.low, atr_v)
            or active_zone.near(prev.high, atr_v)
            or active_zone.near(last.close, atr_v)
        )
        if touched:
            # 扣板機等級：必須當根硬反應（避免「前一根假突破＋當根多頭吞噬」誤開空）
            if structure == "偏多" and hard_bull and zone_kind == "support":
                reaction_ok = True
            elif structure == "偏空" and hard_bear and zone_kind == "resistance":
                reaction_ok = True
            # 盤整：有反應也不鼓勵扣板機（等方向）

    # 結構細節與標籤衝突時（如標偏空卻 CHOCH轉多／MA多頭）→ 不給方向單
    struct_conflict = False
    if structure == "偏空" and (
        "CHOCH轉多" in struct_detail or "MA多頭" in struct_detail or "多頭排列" in struct_detail
    ):
        struct_conflict = True
    if structure == "偏多" and (
        "CHOCH轉空" in struct_detail or "MA空頭" in struct_detail or "空頭排列" in struct_detail
    ):
        struct_conflict = True

    # 方向 → side（第四章：用來決定優先做多/做空，不是猜未來）
    # 硬規則：偏多只允許多、偏空只允許空；衝突則不下單
    side = "none"
    if (
        structure == "偏多"
        and zone_kind == "support"
        and reaction_ok
        and not chasing
        and not struct_conflict
    ):
        side = "long"
    elif (
        structure == "偏空"
        and zone_kind == "resistance"
        and reaction_ok
        and not chasing
        and not struct_conflict
    ):
        side = "short"
    if structure == "偏多" and side == "short":
        side = "none"
    if structure == "偏空" and side == "long":
        side = "none"
    if structure == "盤整" or struct_conflict or react_conflict:
        side = "none"

    # 扣板機匯合
    confluence: list[str] = []
    signal_candle = last if sigs else prev

    if side == "long":
        wh = wick_half_zone(signal_candle, "long")
        if wh and (wh[0] <= px <= wh[1] or price_near_level(px, (wh[0] + wh[1]) / 2, atr_v)):
            confluence.append("影線0.5")
        elif wh:
            confluence.append(f"影線0.5觀察區{wh[0]:.6g}-{wh[1]:.6g}")
    elif side == "short":
        wh = wick_half_zone(signal_candle, "short")
        if wh and (wh[0] <= px <= wh[1] or price_near_level(px, (wh[0] + wh[1]) / 2, atr_v)):
            confluence.append("影線0.5")
        elif wh:
            confluence.append(f"影線0.5觀察區{wh[0]:.6g}-{wh[1]:.6g}")

    ov = overlap_half(last, prev)
    if ov:
        if ov[0] <= px <= ov[1]:
            confluence.append("重疊K0.5")
        else:
            confluence.append(f"重疊K0.5區{ov[0]:.6g}-{ov[1]:.6g}")

    vac = vacuum_zone(candles)
    if vac:
        if vac[0] <= px <= vac[1] or (
            active_zone and abs(active_zone.mid - (vac[0] + vac[1]) / 2) <= atr_v
        ):
            confluence.append("真空區匯合")
        else:
            confluence.append(f"真空區{vac[0]:.6g}-{vac[1]:.6g}")

    # 05 Fibo：明確推力後才畫；等位上反應，不可只靠 Fibo 下單
    fib = analyze_fibo(candles, structure, atr_v, has_reaction=reaction_ok)
    if fib.ready:
        if fib.at_fib:
            confluence.append(f"Fibo{fib.fib_tag}({fib.depth})")
            if reaction_ok:
                confluence.append("Fibo位上有反應")
        elif fib.depth == "未回測":
            confluence.append("Fibo等回測")
        if fib.force_return_price and fib.at_fib:
            confluence.append(f"力道返還{fib.force_return_tag}")

    confluence.extend(smc_hints(candles, structure))
    confluence.extend(chart_pattern(candles))
    ma_tag, _ = ma_alignment([c.close for c in candles])
    if ma_tag in ("多頭排列", "空頭排列"):
        confluence.append(ma_tag)

    # 11步用：EQH/EQL、流動性、RSI、POC（提前以便計入匯合）
    eq_txt, eqh, eql = detect_equal_levels(candles, atr_v)
    liq_txt, liq_near = detect_liquidity_zones(candles, structure, atr_v)
    rsi_div = detect_rsi_divergence(candles)
    # Trader Willy：4H POC ＋ 常規頂/底背離（小幣也適用）
    poc_4h, div_poc_note, div_poc_tags, poc_tp = analyze_divergence_poc(
        candles,
        htf_candles=htf_poc_candles,
        px=px,
        atr_v=atr_v,
        structure=structure,
    )
    poc, poc_txt = approx_poc(htf_poc_candles or candles)
    if poc_4h <= 0:
        poc_4h = poc
    if liq_near:
        confluence.append("流動性區匯合")
    if price_near_level(px, poc_4h if poc_4h > 0 else poc, atr_v * 1.2):
        confluence.append("近POC")
    if any(price_near_level(px, lv, atr_v) for lv in eqh + eql):
        confluence.append("近EQH/EQL")
    if ("頂背離" in rsi_div or "底背離" in rsi_div) and "無明顯" not in rsi_div:
        confluence.append(rsi_div.split("(")[0])
    for t in div_poc_tags:
        if t not in confluence:
            confluence.append(t)

    # 硬匯合：要反應／結構證據；背離+POC 算硬匯合（Willy）
    hard_conf = [
        c
        for c in confluence
        if c
        in (
            "影線0.5",
            "重疊K0.5",
            "真空區匯合",
            "多頭排列",
            "空頭排列",
            "Fibo位上有反應",
            "流動性區匯合",
            "近EQH/EQL",
            "頂背離+近4H POC",
            "底背離+近4H POC",
        )
        or c.startswith("力道返還")
        or c.startswith("SMC")
        or ("背離" in c)
        or ("雙頂" in c or "雙底" in c)
    ]
    # 區上已有硬收針／吞噬時，本身就算硬匯合（避免影線0.5區沒對上就整筆作廢）
    if reaction_ok and (
        (side == "short" and hard_bear) or (side == "long" and hard_bull)
    ):
        hard_conf.append("區上硬反應")
    soft_conf = [
        c
        for c in confluence
        if "觀察區" in c or c.startswith("真空區") or "重疊K0.5區" in c or c == "Fibo等回測"
    ]

    # ⑤⑥ 止損 / 止盈位置（有方向就先算計畫價；真正進場仍看 side）
    # 計畫方向也鎖死：偏多只計畫多、偏空只計畫空；衝突則不算計畫單
    plan_side = side
    if plan_side == "none" and not struct_conflict and not react_conflict:
        if structure == "偏多":
            plan_side = "long"
        elif structure == "偏空":
            plan_side = "short"
    if structure == "偏多" and plan_side == "short":
        plan_side = "long"
    if structure == "偏空" and plan_side == "long":
        plan_side = "short"
    if structure == "盤整" or struct_conflict or react_conflict:
        plan_side = "none"

    # 入場：已回測用現價；等回測用區中軸作計畫入場
    if waiting_retest and priority_zone:
        entry = priority_zone.mid
    else:
        entry = px

    invalidation, inv_reason = compute_invalidation(
        plan_side if plan_side != "none" else side,
        active_zone or priority_zone,
        signal_candle,
        atr_v,
        structure,
    )
    stop = invalidation
    tp, rr = (0.0, 0.0)
    if plan_side != "none" and stop > 0:
        tp, rr = compute_targets(plan_side, entry, stop, supports, resists, atr_v)
        # 力道返還：有反應且 Fibo 到位時優先作止盈觀察
        if (
            fib.ready
            and fib.at_fib
            and fib.force_return_price
            and (reaction_ok or side != "none")
        ):
            fr = fib.force_return_price
            if (plan_side == "long" and fr > entry) or (plan_side == "short" and fr < entry):
                tp = fr
                risk = abs(entry - stop)
                rr = (abs(tp - entry) / risk) if risk > 0 else 0.0
        # Willy：背離單優先把 4H POC 當第一止盈錨（方向正確且 RR 不更差）
        if poc_tp > 0:
            risk = abs(entry - stop)
            if risk > 0:
                rr_poc = abs(poc_tp - entry) / risk
                poc_dir_ok = (plan_side == "short" and poc_tp < entry) or (
                    plan_side == "long" and poc_tp > entry
                )
                if poc_dir_ok and rr_poc >= MIN_RR and (tp <= 0 or rr_poc >= rr * 0.85):
                    tp = poc_tp
                    rr = rr_poc
                    if div_poc_note:
                        div_poc_note += "｜止盈錨4H POC"
    rr_ok = rr >= MIN_RR and side != "none"  # RR 達標只在真正有方向單時算通過

    # 多週期一致性：逆 4H/日 不可進
    mtf_aligned, mtf_note = check_mtf_alignment(structure, mtf)

    # ── YES 品質閘（QNT 類假壓力／急漲摸空）────────────────────────────
    # 1) 當根必須收在區內正確一側（空：收在壓力下緣內；多：收在支撐上緣內）
    # 2) 近端不可已收盤突破該區，也不可已掃過計畫停損
    # 3) 急漲／急跌後立刻摸邊不做（等節奏喘完）
    level_ok = True
    level_block_reason = ""
    z_chk = active_zone or priority_zone
    prior = candles[-13:-1] if len(candles) >= 14 else candles[:-1]
    if side in ("long", "short") and z_chk is not None:
        pad = atr_v * 0.2
        if side == "short":
            if last.close > z_chk.hi:
                level_ok = False
                level_block_reason = "空單當根收在壓力之上（未收回）"
            elif any(c.close > z_chk.hi + pad for c in prior):
                level_ok = False
                level_block_reason = "近端已收盤突破壓力（假區）"
            elif stop > 0 and any(c.high >= stop for c in prior):
                level_ok = False
                level_block_reason = "近端已掃過停損位"
        else:  # long
            if last.close < z_chk.lo:
                level_ok = False
                level_block_reason = "多單當根收在支撐之下（未收回）"
            elif any(c.close < z_chk.lo - pad for c in prior):
                level_ok = False
                level_block_reason = "近端已收盤跌破支撐（假區）"
            elif stop > 0 and any(c.low <= stop for c in prior):
                level_ok = False
                level_block_reason = "近端已掃過停損位"

    # 急漲／急跌後立刻摸邊：門檻略放寬（2.2×ATR），避免正常回測也被擋
    impulse_fade = False
    if side in ("long", "short") and len(candles) >= 6 and atr_v > 0:
        move5 = last.close - candles[-6].close
        if side == "short" and move5 > atr_v * 2.2:
            impulse_fade = True
            if not level_block_reason:
                level_block_reason = "急漲後摸壓，略過"
        if side == "long" and move5 < -atr_v * 2.2:
            impulse_fade = True
            if not level_block_reason:
                level_block_reason = "急跌後摸撐，略過"

    # 1H 明確逆向才擋；1H 盤整不擋
    one_h_conflict = bool(
        side != "none"
        and "1H" in mtf
        and (
            (side == "short" and mtf["1H"] == "偏多")
            or (side == "long" and mtf["1H"] == "偏空")
        )
    )
    if one_h_conflict and not level_block_reason:
        level_block_reason = "1H 逆向"

    # 做多：日線必須偏多；有 4H 時 4H 也必須偏多
    long_htf_block = False
    if side == "long":
        if mtf.get("1D") != "偏多":
            long_htf_block = True
            if not level_block_reason:
                level_block_reason = (
                    "禁逆日線做多" if mtf.get("1D") == "偏空" else "做多需日線偏多"
                )
        elif "4H" in mtf and mtf.get("4H") != "偏多":
            long_htf_block = True
            if not level_block_reason:
                level_block_reason = "做多需4H偏多"

    # 結構：清晰 HH/HL／LH/LL，或（BOS/CHOCH + 同向 MA）二擇一
    # 避免「必須同時有完整擺動＋BOS」疊到幾乎沒單
    bos_ok = bool(
        (side == "long" and bos_tag in ("BOS向上", "CHOCH轉多"))
        or (side == "short" and bos_tag in ("BOS向下", "CHOCH轉空"))
    )
    clear_swings = bool(
        (side == "long" and "HH" in struct_detail and "HL" in struct_detail)
        or (side == "short" and "LH" in struct_detail and "LL" in struct_detail)
    )
    ma_same = bool(
        (side == "long" and ("MA多頭" in struct_detail or "多頭排列" in confluence))
        or (side == "short" and ("MA空頭" in struct_detail or "空頭排列" in confluence))
    )
    swing_struct_ok = clear_swings or (bos_ok and ma_same)
    if side in ("long", "short") and not swing_struct_ok and not level_block_reason:
        level_block_reason = "結構不足（需 HH/HL 或 LH/LL，或 BOS+同向MA）"

    # 停損至少約 0.25×ATR（兼顧可成交與抗掃）
    stop_vs_atr_ok = True
    if side in ("long", "short") and stop > 0 and atr_v > 0:
        if abs(entry - stop) < atr_v * 0.25:
            stop_vs_atr_ok = False
            if not level_block_reason:
                level_block_reason = "停損小於波動（易掃損）"

    # 收盤方向：已有硬收針／吞噬時不另擋（收針本身已含方向）
    close_dir_ok = True
    if side == "short" and last.bullish and not hard_bear:
        close_dir_ok = False
        if not level_block_reason:
            level_block_reason = "空單當根收陽且無空方硬反應"
    if side == "long" and (not last.bullish) and not hard_bull:
        close_dir_ok = False
        if not level_block_reason:
            level_block_reason = "多單當根收陰且無多方硬反應"

    # 可扣板機：核心條件 + 防假區閘（已平衡，避免條件疊加到零成交）
    struct_side_ok = (
        (structure == "偏多" and side == "long")
        or (structure == "偏空" and side == "short")
    )
    trigger_ready = bool(
        side != "none"
        and struct_side_ok
        and structure in ("偏多", "偏空")
        and at_zone
        and reaction_ok
        and not chasing
        and not waiting_retest
        and not struct_conflict
        and not react_conflict
        and bos_ok
        and bool(hard_conf)
        and rr_ok
        and mtf_aligned
        and level_ok
        and not impulse_fade
        and not one_h_conflict
        and not long_htf_block
        and swing_struct_ok
        and stop_vs_atr_ok
        and close_dir_ok
    )

    # ── 日空反抽見頂（fade）：與一般 YES 分開 ────────────────────────────
    # 1D 偏空 + 4H 仍偏多（反抽未破）+ 15m 高位空方硬反應 → 可推空，不要求 MTF 對齊
    fade_ready = False
    fade_loc = ""
    if (
        not trigger_ready
        and mtf.get("1D") == "偏空"
        and mtf.get("4H") == "偏多"
        and hard_bear
        and not react_conflict
        and close_dir_ok
    ):
        at_high, fade_loc = _at_local_high_for_fade(
            candles, last, atr_v, resists, active_zone, priority_zone
        )
        if at_high:
            fade_entry = px
            fade_zone = None
            if active_zone and active_zone.kind == "resistance":
                fade_zone = active_zone
            elif priority_zone and priority_zone.kind == "resistance":
                fade_zone = priority_zone
            fade_stop, fade_inv = compute_invalidation(
                "short", fade_zone, last, atr_v, structure
            )
            if fade_stop <= fade_entry:
                fade_stop = last.high + atr_v * 0.15
                fade_inv = "突破見頂高點 → 空單理由失效"
            fade_tp, fade_rr = compute_targets(
                "short", fade_entry, fade_stop, supports, resists, atr_v
            )
            fade_stop_ok = (
                fade_stop > fade_entry
                and atr_v > 0
                and abs(fade_entry - fade_stop) >= atr_v * 0.25
            )
            fade_level_ok = True
            if fade_zone is not None and last.close > fade_zone.hi:
                fade_level_ok = False
            if fade_tp > 0 and fade_rr >= MIN_RR and fade_stop_ok and fade_level_ok:
                fade_ready = True
                side = "short"
                entry = fade_entry
                stop = fade_stop
                tp = fade_tp
                rr = fade_rr
                invalidation = fade_stop
                inv_reason = fade_inv
                rr_ok = True
                if "日空反抽見頂" not in confluence:
                    confluence.append("日空反抽見頂")
                if hard_bear and "高位硬反應" not in confluence:
                    confluence.append("高位硬反應")

    # 評分
    score = 0.0
    if structure in ("偏多", "偏空"):
        score += 30
    else:
        score += 5  # 盤整大幅降權
    if at_zone and (
        (structure == "偏多" and zone_kind == "support")
        or (structure == "偏空" and zone_kind == "resistance")
    ):
        score += 25
    elif waiting_retest:
        score += 12  # 方向對、等回測也有價值
    if reaction_ok:
        score += 18
    elif candle_signals:
        score += 4
    score += min(len(hard_conf) * 7 + len(soft_conf) * 2, 20)
    if rr_ok and side != "none":
        score += min(rr * 5, 12)
    if trigger_ready:
        score += 10
    if fade_ready:
        score += 12  # 獨立見頂空訊
    if chasing:
        score = max(score - 20, 0)
    if mtf_aligned and mtf and (
        (structure == "偏多" and mtf_bull >= 2) or (structure == "偏空" and mtf_bear >= 2)
    ):
        score += 5
    if not mtf_aligned and not fade_ready:
        score = max(score - 25, 0)  # 逆高週期大幅降權（fade 例外）

    # 支撐壓力四步清單
    loc_ok = at_zone and (
        (structure == "偏多" and zone_kind == "support")
        or (structure == "偏空" and zone_kind == "resistance")
    )
    checklist = [
        ("① 大結構（方向）", structure in ("偏多", "偏空"), f"{structure}｜{struct_detail}"),
        (
            "② 支撐壓力區（位置）",
            loc_ok,
            (
                f"{'已回測' if at_zone else '等待回測'} {zone_kind} {fmt_short(zone_lo)}-{fmt_short(zone_hi)}"
                if priority_zone
                else "尚無優先區"
            ),
        ),
        (
            "③ K棒訊號（反應）",
            reaction_ok,
            "、".join(candle_signals) if candle_signals else "無明確收針/吞噬/十字/假突破",
        ),
        (
            "④ 扣板機（匯合+RR）",
            trigger_ready or fade_ready,
            (
                f"{'、'.join(confluence[:4]) if confluence else '無匯合'}｜RR={rr:.2f}"
                + ("｜日空反抽見頂" if fade_ready else "")
                if side != "none"
                else "尚未形成方向單"
            ),
        ),
    ]

    # 第四章六問
    stand_ok = structure in ("偏多", "偏空")
    look_ok = priority_zone is not None
    retest_ok = at_zone and not chasing
    react_step_ok = reaction_ok
    stop_ok = stop > 0 and side != "none"
    rr_step_ok = rr_ok and side != "none"

    stand_detail = {
        "偏多": "偏多(HH/HL) → 回檔優先做多，不猜頂",
        "偏空": "偏空(LH/LL) → 反彈優先做空，不猜底",
        "盤整": "盤整 → 不追突破，等方向選擇",
    }.get(structure, structure)
    if ma_detail:
        stand_detail += f"｜{ma_detail}"
    if bos_tag:
        stand_detail += f"｜{bos_tag}"
    if mtf:
        stand_detail += "｜MTF:" + ",".join(f"{k}:{v}" for k, v in mtf.items())

    chapter4 = [
        ("01 站哪邊", stand_ok, stand_detail),
        (
            "02 看哪裡",
            look_ok,
            (
                f"優先{'支撐' if (priority_zone and priority_zone.kind == 'support') else '壓力'} "
                f"{fmt_short(zone_lo)}-{fmt_short(zone_hi)}"
                if priority_zone
                else "未標到關鍵區"
            ),
        ),
        (
            "03 等回測",
            retest_ok if stand_ok else False,
            (
                "已回到關鍵區"
                if at_zone
                else ("等待回測，勿追價" if waiting_retest or chasing else "尚未形成回測")
            ),
        ),
        (
            "04 看反應",
            react_step_ok,
            "、".join(candle_signals[:4]) if candle_signals else "區上尚無拒絕/收回/轉強弱訊號",
        ),
        (
            "05 找止損",
            stop_ok,
            f"失效價≈{fmt_short(invalidation)}｜{inv_reason}" if invalidation else inv_reason,
        ),
        (
            "06 算盈虧比",
            rr_step_ok,
            (
                f"入場{fmt_short(entry)} 止損{fmt_short(stop)} 目標{fmt_short(tp)}｜RR={rr:.2f} "
                f"{'(達標)' if rr_ok else f'(需≥{MIN_RR})'}"
                if side != "none" and stop
                else f"需先有方向單；門檻 RR≥{MIN_RR}"
            ),
        ),
    ]

    note = ""
    if struct_conflict:
        note = "結構訊號衝突（如 LH/LL 與 MA多頭／CHOCH轉多同時出現）→ 暫不下手，等方向一致。"
    elif react_conflict:
        note = "K棒多空反應衝突（同時有多頭與空頭訊號）→ 不扣板機。"
    elif structure == "偏多":
        note = "偏多：回檔到支撐再找多單；【硬規則】禁止做空。"
        if waiting_retest:
            note += " 目前尚未回測到區 → 等待。"
    elif structure == "偏空":
        note = "偏空：反彈到壓力再找空單；【硬規則】禁止做多。"
        if waiting_retest:
            note += " 目前尚未回測到區 → 等待。"
    else:
        note = "盤整：不追假突破，等市場選邊；【硬規則】不給方向單。"

    if chasing:
        note += " 偵測到追價/追突破，略過。"
    if level_block_reason and not trigger_ready:
        note += f" ｜品質閘:{level_block_reason} → 不扣板機。"
    if side != "none" and not bos_ok:
        note += f" 缺同向BOS/CHOCH（目前:{bos_tag or '無'}）→ 不扣板機。"
    if side != "none" and not rr_ok:
        note += f" 盈虧比 {rr:.2f} < {MIN_RR}，不建議扣板機。"
    if not mtf_aligned:
        note += f" ｜{mtf_note} → 不可進場。"
    if at_zone and candle_signals and not loc_ok and structure in ("偏多", "偏空"):
        note += " K訊號與優先區不一致，不扣板機。"
    if fib.ready:
        note += f" ｜Fibo:{fib.depth}"
        if fib.at_fib and not reaction_ok:
            note += "（到位但無反應，不下單）"
        if fib.force_return_price and fib.at_fib and reaction_ok:
            note += f"｜力道返還{fib.force_return_tag}≈{fmt_short(fib.force_return_price)}"

    # 把 Fibo 摘要併入 checklist 第四步說明
    if fib.ready and fib.at_fib:
        checklist[3] = (
            checklist[3][0],
            checklist[3][1],
            checklist[3][2] + f"｜Fibo{fib.fib_tag}{fib.depth}",
        )

    if fib.ready and fib.depth in ("淺回測", "中段回測", "深回測") and fib.at_fib:
        score = min(score + 6, 100)

    # ── 實戰做單流程 11 步 ──
    ind_bits = [ma_tag, rsi_div, poc_txt]
    if "近POC" in confluence:
        ind_bits.append("近POC")
    ind_txt = "｜".join(ind_bits)
    ind_ok = (
        ma_tag in ("多頭排列", "空頭排列", "糾結偏多", "糾結偏空")
        or ("頂背離" in rsi_div or "底背離" in rsi_div)
        or "近POC" in confluence
        or any("背離" in t for t in div_poc_tags)
    )
    if liq_near:
        score = min(score + 4, 100)
    # Willy 背離＋4H POC：與結構同向加分
    if "頂背離+近4H POC" in confluence and structure == "偏空":
        score = min(score + 10, 100)
    elif "底背離+近4H POC" in confluence and structure == "偏多":
        score = min(score + 10, 100)
    elif any("背離" in t for t in div_poc_tags) and (
        (structure == "偏空" and any("頂" in t for t in div_poc_tags))
        or (structure == "偏多" and any("底" in t for t in div_poc_tags))
    ):
        score = min(score + 5, 100)
    if div_poc_note:
        note += f" ｜{div_poc_note}"

    workflow = build_workflow11(
        mtf=mtf,
        opens=opens,
        structure=structure,
        struct_detail=struct_detail + (f"｜{mtf_note}" if mtf_note else ""),
        bos_tag=bos_tag,
        supports=supports,
        resists=resists,
        eq_txt=eq_txt,
        eqh=eqh,
        eql=eql,
        fib=fib,
        liq_txt=liq_txt,
        liq_near=liq_near,
        ind_txt=ind_txt,
        ind_ok=ind_ok,
        at_zone=at_zone,
        waiting_retest=waiting_retest,
        chasing=chasing,
        reaction_ok=reaction_ok,
        candle_signals=candle_signals,
        side=side,
        stop=stop,
        inv_reason=inv_reason,
        tp=tp,
        rr=rr,
        rr_ok=rr_ok,
        trigger_ready=trigger_ready,
        px=px,
    )
    if workflow:
        t0, _, d0 = workflow[0]
        workflow[0] = (
            t0,
            mtf_aligned and structure in ("偏多", "偏空"),
            f"{d0}｜對齊:{mtf_note}",
        )

    wf_passed = sum(1 for _, ok, _ in workflow if ok)
    missing = summarize_missing_steps(workflow)
    if trigger_ready and wf_passed >= 8:
        score = min(score + 5, 100)
    if fade_ready:
        score = min(score + 3, 100)
    if workflow:
        t11, _, _ = workflow[-1]
        enter_ok = trigger_ready or fade_ready
        workflow[-1] = (
            t11,
            enter_ok,
            (
                (
                    "日空反抽見頂・可空"
                    if fade_ready
                    else ("可進場觀察" if trigger_ready else "暫不進場")
                )
                + f"｜MTF{'✓' if mtf_aligned else '✗'} "
                f"位置{'✓' if (at_zone or fade_ready) else '✗'} "
                f"止損{'✓' if stop > 0 and side != 'none' else '✗'} "
                f"目標{'✓' if rr_ok else '✗'}｜{missing}"
            ),
        )
        wf_passed = sum(1 for _, ok, _ in workflow if ok)
        missing = summarize_missing_steps(workflow)

    note += f" ｜實戰流程 {wf_passed}/11｜{missing}"
    if fade_ready:
        note += f"｜日空反抽見頂可空（{fade_loc or '高位'}｜不要求4H已轉空）"
    elif not trigger_ready:
        note += "｜未齊全不進場"

    return ChecklistResult(
        symbol=symbol,
        interval=interval,
        structure=structure,
        structure_detail=struct_detail,
        at_zone=at_zone,
        zone_kind=zone_kind,
        zone_lo=zone_lo,
        zone_hi=zone_hi,
        waiting_retest=waiting_retest,
        reaction=reaction_ok,
        candle_signals=candle_signals,
        trigger_ready=trigger_ready,
        confluence=confluence,
        side=side,
        setup_score=round(min(score, 100), 1),
        checklist=checklist,
        chapter4=chapter4,
        entry=entry,
        stop=stop,
        tp=tp,
        rr=round(rr, 2),
        invalidation=invalidation,
        invalidation_reason=inv_reason,
        ma_align=ma_detail,
        bos_choch=bos_tag,
        mtf=mtf,
        note=note,
        supports=supports[:3],
        resists=resists[:3],
        fibo=fib,
        fibo_lines=fib_checklist_lines(fib),
        workflow=workflow,
        workflow_passed=wf_passed,
        missing_steps=missing,
        opens=opens,
        rsi_div=rsi_div,
        eq_levels=eq_txt,
        liquidity=liq_txt,
        indicators=ind_txt,
        mtf_aligned=mtf_aligned,
        mtf_note=mtf_note,
        poc_4h=poc_4h,
        div_poc_note=div_poc_note,
        fade_ready=fade_ready,
    )
