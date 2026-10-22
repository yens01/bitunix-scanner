"""台股當沖掃描。

沿用 sr_logic 的同一套條件：
日線定方向，60 分扮演原本 4H 的中週期，15 分做出進場。
現股時段 09:05–12:45 才標成可進；13:25 前要平倉，不留倉。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sr_logic import Candle, ChecklistResult, detect_structure, evaluate_setup

TZ_TW = timezone(timedelta(hours=8))

# 成交熱、波動夠、適合當沖的上市標的。臨時查代號不限這份。
TW_UNIVERSE: list[tuple[str, str]] = [
    ("2330", "台積電"),
    ("2317", "鴻海"),
    ("2454", "聯發科"),
    ("2308", "台達電"),
    ("2382", "廣達"),
    ("3711", "日月光投控"),
    ("2303", "聯電"),
    ("3231", "緯創"),
    ("2357", "華碩"),
    ("6669", "緯穎"),
    ("2376", "技嘉"),
    ("2603", "長榮"),
    ("2609", "陽明"),
    ("3037", "欣興"),
    ("3443", "創意"),
    ("2345", "智邦"),
    ("2408", "南亞科"),
    ("3661", "世芯"),
    ("2881", "富邦金"),
    ("0050", "台灣50"),
]

# 當沖停損超過這個幅度，15 分結構再好也不當可進。
MAX_DAY_RISK_PCT = 2.0
# 距區超過這個百分比，當天不容易打到，不放進等回測。
MAX_WAIT_GAP_PCT = 0.8
_UA = {"User-Agent": "Mozilla/5.0"}


def now_tw() -> datetime:
    return datetime.now(TZ_TW)


def session_state(now: datetime | None = None) -> tuple[str, str]:
    """回傳 (entry|flat|closed, 短句)。週末與 13:25 後都是 closed。"""
    now = now or now_tw()
    if now.weekday() >= 5:
        return "closed", "週末休市"
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 5 <= minutes < 12 * 60 + 45:
        return "entry", "可進場"
    if 12 * 60 + 45 <= minutes < 13 * 60 + 25:
        return "flat", "只出不進"
    return "closed", "非交易時段"


def _get_json(url: str) -> dict | None:
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _fetch_chart(yahoo: str, interval: str, rng: str) -> dict | None:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{yahoo}?interval={interval}&range={rng}&includePrePost=false"
    )
    data = _get_json(url)
    if not data:
        return None
    chart = data.get("chart") or {}
    result = chart.get("result") or []
    if chart.get("error") or not result:
        return None
    return result[0]


def _candles_from_chart(result: dict) -> list[Candle]:
    ts = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    vols = quote.get("volume") or []
    out: list[Candle] = []
    for i, t in enumerate(ts):
        try:
            c = float(closes[i])
            h = float(highs[i])
            lo = float(lows[i])
            o = float(opens[i])
        except (TypeError, ValueError, IndexError):
            continue
        if c <= 0 or h <= 0 or lo <= 0:
            continue
        vol = 0.0
        try:
            vol = float(vols[i] or 0)
        except (TypeError, ValueError, IndexError):
            vol = 0.0
        out.append(
            Candle(time=int(t), open=o, high=h, low=lo, close=c, volume=vol)
        )
    while out and out[-1].volume <= 0:
        out.pop()
    return out


def _resample_60m(bars: list[Candle]) -> list[Candle]:
    """用 15 分合成 60 分。同一小時內的棒收成一根。"""
    buckets: dict[str, list[Candle]] = {}
    for bar in bars:
        dt = datetime.fromtimestamp(bar.time, TZ_TW)
        key = dt.strftime("%Y-%m-%d-%H")
        buckets.setdefault(key, []).append(bar)
    out: list[Candle] = []
    for key in sorted(buckets):
        xs = buckets[key]
        out.append(
            Candle(
                time=xs[0].time,
                open=xs[0].open,
                high=max(x.high for x in xs),
                low=min(x.low for x in xs),
                close=xs[-1].close,
                volume=sum(x.volume for x in xs),
            )
        )
    return out


def _prev_close(daily: list[Candle], now: datetime) -> float:
    if len(daily) < 2:
        return daily[-1].close if daily else 0.0
    last_day = datetime.fromtimestamp(daily[-1].time, TZ_TW).date()
    if last_day == now.date():
        return daily[-2].close
    return daily[-1].close


def _load_symbol(code: str) -> tuple[list[Candle], list[Candle], list[Candle], float] | None:
    """15 分、60 分、日線、現價。上市沒有資料就試上櫃。"""
    for yahoo in (f"{code}.TW", f"{code}.TWO"):
        intra = _fetch_chart(yahoo, "15m", "60d")
        daily_raw = _fetch_chart(yahoo, "1d", "1y")
        if not intra or not daily_raw:
            continue
        c15 = _candles_from_chart(intra)
        daily = _candles_from_chart(daily_raw)
        if len(c15) < 40 or len(daily) < 40:
            continue
        c60 = _resample_60m(c15)
        if len(c60) < 40:
            continue
        meta = intra.get("meta") or {}
        last = float(meta.get("regularMarketPrice") or 0)
        if last <= 0:
            last = c15[-1].close
        return c15, c60, daily, last
    return None


def _bias(candles: list[Candle]) -> str | None:
    if len(candles) < 40:
        return None
    bias, _, _, _ = detect_structure(candles)
    return bias


def _gap_pct(r: ChecklistResult, last: float) -> float:
    if r.at_zone:
        return 0.0
    mid = (r.zone_lo + r.zone_hi) / 2 if r.zone_hi > 0 else r.zone_lo
    px = last if last > 0 else r.entry
    if mid <= 0 or px <= 0:
        return 999.0
    return abs(px - mid) / px * 100.0


@dataclass
class TwHit:
    code: str
    name: str
    last: float
    chg_pct: float
    result: ChecklistResult
    tier: str  # go | fade | base | wait | watch
    flag: str


def _classify(r: ChecklistResult, last: float, chg: float, prev: float) -> tuple[str, str] | None:
    fade = bool(getattr(r, "fade_ready", False))
    base = bool(getattr(r, "base_ready", False))
    risk = abs(r.entry - r.stop) / r.entry * 100 if r.entry > 0 and r.stop > 0 else 0.0
    if abs(chg) >= 9.0:
        return None
    flag = ""
    if prev > 0 and r.tp > 0:
        if r.side == "long" and r.tp >= prev * 1.095:
            return "watch", "止盈接近漲停"
        if r.side == "short" and r.tp <= prev * 0.905:
            return "watch", "止盈接近跌停"
    wide = risk > MAX_DAY_RISK_PCT
    if wide:
        flag = f"停損{risk:.1f}%過寬"
    if r.trigger_ready and r.side in ("long", "short"):
        return ("watch", flag) if wide else ("go", "")
    if fade and r.side == "short":
        return ("watch", flag) if wide else ("fade", "")
    if base and r.side == "long":
        return ("watch", flag) if wide else ("base", "")
    if r.waiting_retest and r.mtf_aligned and r.structure in ("偏多", "偏空"):
        if _gap_pct(r, last) <= MAX_WAIT_GAP_PCT:
            return "wait", ""
        return None
    if flag:
        return "watch", flag
    if r.mtf_aligned and r.structure in ("偏多", "偏空") and r.setup_score >= 55:
        return "watch", ""
    return None


def _scan_one(code: str, name: str, *, keep_all: bool = False) -> TwHit | None:
    loaded = _load_symbol(code)
    if not loaded:
        return None
    c15, c60, daily, last = loaded
    prev = _prev_close(daily, now_tw())
    chg = (last - prev) / prev * 100 if prev else 0.0
    mtf: dict[str, str] = {}
    day_bias = _bias(daily)
    mid_bias = _bias(c60)
    if day_bias:
        mtf["1D"] = day_bias
    if mid_bias:
        # 60 分放進原本 4H 的格子，日多／日空／見頂／見底條件才對得上當沖。
        mtf["4H"] = mid_bias
    result = evaluate_setup(
        code,
        c15,
        "15m",
        mtf=mtf,
        htf_poc_candles=c60,
    )
    ranked = _classify(result, last, chg, prev)
    if not ranked:
        if not keep_all:
            return None
        return TwHit(code, name, last, chg, result, "none", "")
    tier, flag = ranked
    return TwHit(code, name, last, chg, result, tier, flag)


def _px(v: float) -> str:
    if v <= 0:
        return "-"
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 100:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    if abs(v - round(v)) < 1e-9:
        return f"{v:.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _levels(r: ChecklistResult, *, label: str) -> list[str]:
    if r.entry > 0 and r.stop > 0 and r.tp > 0:
        risk = abs(r.entry - r.stop) / r.entry * 100
        return [
            f"　{label} {_px(r.entry)}  止損 {_px(r.stop)}  止盈 {_px(r.tp)}",
            f"　RR {r.rr:.1f} ｜ 風險 {risk:.2f}%",
        ]
    if r.zone_lo > 0 and r.zone_hi >= r.zone_lo:
        return [f"　觀察區 {_px(r.zone_lo)}–{_px(r.zone_hi)}"]
    return ["　進出場未齊"]


def _line(hit: TwHit, *, tag: str) -> str:
    r = hit.result
    direction = "多" if (r.side == "long" or r.structure == "偏多") else "空"
    bits = [f"{hit.code} {hit.name}", f"偏{direction}" if tag != "可進" else f"做{direction}", tag]
    if r.rr > 0 and tag in {"可進", "見頂", "見底", "等回測"}:
        bits.append(f"RR{r.rr:.1f}")
    bits.append(f"分{r.setup_score:.0f}")
    bits.append(f"{hit.chg_pct:+.1f}%")
    day = (r.mtf or {}).get("1D", "")
    mid = (r.mtf or {}).get("4H", "")
    if day or mid:
        bits.append(f"日{day or '—'} 60分{mid or '—'}")
    if hit.flag:
        bits.append(hit.flag)
    if tag == "見頂":
        bits.append("空需可先賣後買")
    return " · ".join(bits)


def _emit(lines: list[str], emoji: str, hit: TwHit, *, tag: str, label: str) -> None:
    lines.append(f"{emoji} {_line(hit, tag=tag)}")
    lines.extend(_levels(hit.result, label=label))
    if tag == "關注":
        lines.append("　條件未齊，先不要進")


def _header(now: datetime, state: str, label: str, pool_n: int) -> list[str]:
    lines = [
        "🇹🇼 台股當沖",
        f"🕐 {now.strftime('%m-%d %H:%M')} ｜ {label} ｜ 池 {pool_n} 檔",
        "日線定方向 · 60分當中週期 · 15分進場",
    ]
    if state == "entry":
        lines.append("13:25 前平倉，不留倉。做空要先確認可先賣後買。")
    elif state == "flat":
        lines.append("已過 12:45，不再進新單。手上的單 13:25 前平倉。")
    else:
        lines.append("現在不是進場時段。下面是條件，下一個 09:05 後才當沖。")
    lines.append("")
    return lines


def _render(hits: list[TwHit], *, pool_n: int, html: bool) -> str:
    now = now_tw()
    state, label = session_state(now)
    lines = _header(now, state, label, pool_n)
    buckets = {"go": [], "fade": [], "base": [], "wait": [], "watch": []}
    for hit in hits:
        buckets[hit.tier].append(hit)
    for tier in buckets:
        buckets[tier].sort(key=lambda h: (h.result.setup_score, h.result.rr), reverse=True)
        buckets[tier] = buckets[tier][:4]

    go_title = "✅ 可進" if state == "entry" else "✅ 條件到了（現在不進）"
    if buckets["go"]:
        lines.append(go_title)
        for hit in buckets["go"]:
            emoji = "🟢" if hit.result.side == "long" else "🔴"
            _emit(lines, emoji, hit, tag="可進", label="進場")
        lines.append("")
    if buckets["fade"]:
        lines.append("🔻 日空見頂・空")
        for hit in buckets["fade"]:
            _emit(lines, "🔴", hit, tag="見頂", label="進場")
        lines.append("")
    if buckets["base"]:
        lines.append("🔺 日多見底・多")
        for hit in buckets["base"]:
            _emit(lines, "🟢", hit, tag="見底", label="進場")
        lines.append("")
    if buckets["wait"]:
        lines.append("⏳ 等回測（到區再確認）")
        for hit in buckets["wait"]:
            emoji = "🟢" if hit.result.structure == "偏多" else "🔴"
            _emit(lines, emoji, hit, tag="等回測", label="計畫進場")
        lines.append("")
    if buckets["watch"]:
        lines.append("👀 方向關注")
        for hit in buckets["watch"]:
            emoji = "🟢" if hit.result.structure == "偏多" else "🔴"
            _emit(lines, emoji, hit, tag="關注", label="參考價")
        lines.append("")
    if not any(buckets.values()):
        lines.append("這輪沒有適合當沖的檔。")
    text = "\n".join(lines).rstrip()
    if not html:
        return text
    # 標題加粗，價格不用 code，避免手機一行被撐爆。
    html_lines = []
    for i, line in enumerate(text.splitlines()):
        if i == 0:
            html_lines.append(f"<b>{_esc(line)}</b>")
        elif line.startswith(("✅", "🔻", "🔺", "⏳", "👀")):
            html_lines.append(f"<b>{_esc(line)}</b>")
        else:
            html_lines.append(_esc(line))
    return "\n".join(html_lines)


def _business_line(code: str) -> str:
    from company_brief import business_line

    return business_line(f"{code}.TW", f"{code}.TWO")


def _insert_brief(text: str, brief: str) -> str:
    if not brief:
        return text
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "現價" in line or "進場" in line or line[:1] in {"🟢", "🔴"}:
            lines.insert(i + 1, brief)
            return "\n".join(lines)
    lines.append(brief)
    return "\n".join(lines)


def build_tw_daytrade_message(*, code: str | None = None, html: bool = True) -> str:
    """全池掃描，或單一代號。html=False 給 CMD。"""
    code = (code or "").strip()
    if code:
        name = next((n for c, n in TW_UNIVERSE if c == code), code)
        hit = _scan_one(code, name, keep_all=True)
        if hit is None:
            return f"❌ 找不到 {_esc(code) if html else code} 的 K 線。"
        if hit.tier == "none":
            now = now_tw()
            state, label = session_state(now)
            r = hit.result
            day = (r.mtf or {}).get("1D", "—")
            mid = (r.mtf or {}).get("4H", "—")
            body = [
                f"{hit.code} {hit.name}  現價 {_px(hit.last)}  {hit.chg_pct:+.1f}%",
                _business_line(code),
                f"日{day} · 60分{mid}",
                "條件未齊，今天不做。",
            ]
            text = "\n".join(_header(now, state, label, 1) + [line for line in body if line])
            return text if not html else "\n".join(
                f"<b>{_esc(line)}</b>" if i == 0 else _esc(line)
                for i, line in enumerate(text.splitlines())
            )
        text = _render([hit], pool_n=1, html=False)
        text = _insert_brief(text, _business_line(code))
        if not html:
            return text
        return "\n".join(
            f"<b>{_esc(line)}</b>" if i == 0 or line.startswith(("✅", "🔻", "🔺", "⏳", "👀")) else _esc(line)
            for i, line in enumerate(text.splitlines())
        )

    hits: list[TwHit] = []
    workers = 8
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_scan_one, c, n) for c, n in TW_UNIVERSE]
        for fut in as_completed(futs):
            try:
                hit = fut.result()
            except Exception:
                continue
            if hit:
                hits.append(hit)
    return _render(hits, pool_n=len(TW_UNIVERSE), html=html)
