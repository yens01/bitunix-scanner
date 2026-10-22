"""美股綜合熱度。

對齊台股熱度：流動性、漲跌動能、振幅，沒有費率。
美股沒有漲跌停，動能以 12%、振幅以 15% 封頂。
股價低於 1 美元或成交額低於 5,000 萬美元的不進榜。
"""

from __future__ import annotations

import http.cookiejar
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TZ_TW = timezone(timedelta(hours=8))
_UA = {"User-Agent": "Mozilla/5.0"}
_UA_JSON = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_SCREEN = "https://api.nasdaq.com/api/screener/stocks"
_MIN_VALUE = 50_000_000
_MIN_PRICE = 1.0
_PAGE = 250
_QUOTE_BATCH = 80


def now_tw() -> datetime:
    return datetime.now(TZ_TW)


def _f(value: object) -> float:
    try:
        if value is None or value == "" or value == "-":
            return 0.0
        text = str(value).replace(",", "").replace("%", "").replace("$", "").strip()
        if not text or text in {"N/A", "nan"}:
            return 0.0
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=_UA_JSON)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _screener_candidates() -> list[tuple[str, str, float, float]]:
    """回傳 (代號, 名稱, 漲跌%, 市值)。先用這包縮小，再向 Yahoo 要振幅和成交額。"""
    first = _http_json(f"{_SCREEN}?tableonly=true&limit={_PAGE}&offset=0")
    data = first.get("data") or {}
    total = int(data.get("totalrecords") or 0)
    pages = [data.get("table") or {}]
    offsets = list(range(_PAGE, max(total, _PAGE), _PAGE))

    def _page(offset: int) -> dict:
        body = _http_json(f"{_SCREEN}?tableonly=true&limit={_PAGE}&offset={offset}")
        return (body.get("data") or {}).get("table") or {}

    if offsets:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(_page, off) for off in offsets]
            for fut in as_completed(futs):
                try:
                    pages.append(fut.result())
                except Exception:
                    continue

    rough: list[tuple[str, str, float, float, float]] = []
    for table in pages:
        for row in table.get("rows") or []:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol.isalpha() or not (1 <= len(symbol) <= 5):
                continue
            name = str(row.get("name") or symbol).strip()
            pct = _f(row.get("pctchange"))
            cap = _f(row.get("marketCap"))
            if cap <= 0:
                continue
            score = abs(pct) * max(1.0, math.log10(cap))
            rough.append((symbol, name, pct, cap, score))

    mega = [row for row in rough if row[3] >= 200_000_000_000]
    movers = [
        row
        for row in rough
        if row[3] >= 300_000_000 and (abs(row[2]) >= 1.5 or row[3] >= 50_000_000_000)
    ]
    movers.sort(key=lambda r: r[4], reverse=True)
    picked: dict[str, tuple[str, str, float, float]] = {}
    for symbol, name, pct, cap, _score in mega + movers[:160]:
        picked.setdefault(symbol, (symbol, name, pct, cap))
    return list(picked.values())


def _yahoo_opener() -> tuple[urllib.request.OpenerDirector, str]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        opener.open(urllib.request.Request("https://fc.yahoo.com", headers=_UA), timeout=15)
    except urllib.error.HTTPError:
        pass
    raw = opener.open(
        urllib.request.Request(
            "https://query1.finance.yahoo.com/v1/test/getcrumb",
            headers=_UA,
        ),
        timeout=15,
    ).read()
    return opener, raw.decode().strip()


def _quote_batch(
    opener: urllib.request.OpenerDirector,
    crumb: str,
    symbols: list[str],
) -> list[dict]:
    joined = ",".join(symbols)
    url = (
        "https://query1.finance.yahoo.com/v7/finance/quote?symbols="
        + urllib.parse.quote(joined)
        + "&crumb="
        + urllib.parse.quote(crumb)
    )
    raw = opener.open(urllib.request.Request(url, headers=_UA), timeout=20).read()
    body = json.loads(raw.decode("utf-8"))
    return list((body.get("quoteResponse") or {}).get("result") or [])


@dataclass
class UsHeat:
    code: str
    name: str
    last: float
    chg: float
    range_pct: float
    value: float
    score: float
    tags: list[str]


@dataclass
class UsHeatBoard:
    rows: list[UsHeat]
    session: str
    asof: str


def _session_label(state: str) -> str:
    return {
        "REGULAR": "盤中",
        "PRE": "盤前",
        "PREPRE": "盤前",
        "POST": "盤後",
        "POSTPOST": "盤後",
        "CLOSED": "已收盤",
    }.get(state, "報價")


def _asof_text(ts: float) -> str:
    if ts > 10_000_000_000:
        ts = ts / 1000
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, TZ_TW).strftime("%m-%d %H:%M")


def _short_name(name: str, code: str) -> str:
    text = name or code
    for suffix in (
        " Common Stock",
        " Ordinary Shares",
        " Class A",
        " Class B",
        " Class C",
        " Corporation",
        " Corp.",
        " Incorporated",
        " Inc.",
        " Inc",
        " Holdings",
        " Group",
        ",",
    ):
        text = text.replace(suffix, "")
    text = " ".join(text.split())
    if len(text) > 18:
        cut = text[:18].rsplit(" ", 1)[0]
        text = (cut or text[:18]).rstrip()
    return text or code


def _build(quotes: list[dict]) -> tuple[list[UsHeat], str, str]:
    parsed: list[tuple[str, str, float, float, float, float, str, float]] = []
    session = ""
    asof = ""
    for row in quotes:
        if str(row.get("quoteType") or "EQUITY") not in {"EQUITY", ""}:
            continue
        code = str(row.get("symbol") or "").strip().upper()
        last = _f(row.get("regularMarketPrice"))
        prev = _f(row.get("regularMarketPreviousClose"))
        high = _f(row.get("regularMarketDayHigh"))
        low = _f(row.get("regularMarketDayLow"))
        open_ = _f(row.get("regularMarketOpen"))
        volume = _f(row.get("regularMarketVolume"))
        if not code or last < _MIN_PRICE or prev <= 0 or volume <= 0:
            continue
        value = volume * last
        if value < _MIN_VALUE:
            continue
        chg = _f(row.get("regularMarketChangePercent"))
        if chg == 0 and prev:
            chg = (last - prev) / prev * 100
        base = open_ if open_ > 0 else prev
        rng = ((high - low) / base * 100) if base > 0 and high >= low > 0 else abs(chg)
        name = _short_name(str(row.get("shortName") or row.get("longName") or code), code)
        state = str(row.get("marketState") or "")
        if state and not session:
            session = _session_label(state)
        if not asof:
            asof = _asof_text(_f(row.get("regularMarketTime")))
        parsed.append((code, name, last, chg, rng, value, state, volume))

    values = sorted(item[5] for item in parsed)

    def vol_rank(v: float) -> float:
        if not values:
            return 0.0
        lo, hi = 0, len(values)
        while lo < hi:
            mid = (lo + hi) // 2
            if values[mid] < v:
                lo = mid + 1
            else:
                hi = mid
        return lo / len(values) * 100

    heats: list[UsHeat] = []
    for code, name, last, chg, rng, value, _state, _volume in parsed:
        tags: list[str] = []
        score = 0.0
        vr = vol_rank(value)
        score += vr * 0.41
        if vr >= 90:
            tags.append("高流動")
        elif vr >= 70:
            tags.append("流動佳")

        mom = min(abs(chg) / 12.0, 1.0) * 100
        score += mom * 0.35
        if chg >= 5:
            tags.append("強勢漲")
        elif chg >= 2:
            tags.append("偏多")
        elif chg <= -5:
            tags.append("急跌")
        elif chg <= -2:
            tags.append("偏空")

        vola = min(max(rng, 0.0) / 15.0, 1.0) * 100
        score += vola * 0.24
        if rng >= 6:
            tags.append("高波動")
        if not tags:
            tags.append("觀望")
        heats.append(
            UsHeat(
                code=code,
                name=name,
                last=last,
                chg=chg,
                range_pct=rng,
                value=value,
                score=round(score, 2),
                tags=tags,
            )
        )
    heats.sort(key=lambda s: s.score, reverse=True)
    return heats, session or "報價", asof


def load_us_heat() -> UsHeatBoard:
    candidates = _screener_candidates()
    symbols = [row[0] for row in candidates]
    opener, crumb = _yahoo_opener()
    quotes: list[dict] = []
    batches = [symbols[i : i + _QUOTE_BATCH] for i in range(0, len(symbols), _QUOTE_BATCH)]
    for batch in batches:
        try:
            quotes.extend(_quote_batch(opener, crumb, batch))
        except Exception:
            continue
    rows, session, asof = _build(quotes)
    return UsHeatBoard(rows=rows, session=session, asof=asof)


def _px(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.4f}"


def _usd(v: float) -> str:
    if v >= 100_000_000:
        return f"{v / 100_000_000:.2f}億美元"
    if v >= 10_000:
        return f"{v / 10_000:.0f}萬美元"
    return f"{v:.0f}"


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _what_map(codes: list[str]) -> dict[str, str]:
    try:
        from company_brief import business_shorts
    except Exception:
        return {}
    return business_shorts(codes)


def build_us_heat_message(*, top: int = 12, html: bool = True) -> str:
    now = now_tw()
    try:
        board = load_us_heat()
    except Exception as ex:
        err = f"美股熱度抓取失敗：{ex}"
        return f"❌ {_esc(err)}" if html else f"❌ {err}"
    show = board.rows[:top]
    what = _what_map([row.code for row in show])
    when = f"{board.session}"
    if board.asof:
        when += f" · 資料 {board.asof}"
    lines = [
        "🔥 美股熱度",
        f"🕐 {now.strftime('%m-%d %H:%M')} ｜ {when} ｜ Top {len(show)}/{len(board.rows)}",
        "分數＝流動性＋動能＋振幅。沒有費率。",
        "",
    ]
    if not show:
        lines.append("目前沒有通過成交額門檻的股票。")
    for i, row in enumerate(show, 1):
        tags = "、".join(row.tags[:3])
        lines.append(f"{i}. {row.code} {row.name}  {_px(row.last)}  {row.chg:+.2f}%")
        if what.get(row.code):
            lines.append(f"　主要：{what[row.code]}")
        lines.append(
            f"　振幅 {row.range_pct:.2f}% ｜ 額 {_usd(row.value)} ｜ {tags} ({row.score:.0f})"
        )
    text = "\n".join(lines).rstrip()
    if not html:
        return text
    out = []
    for i, line in enumerate(text.splitlines()):
        if i == 0:
            out.append(f"<b>{_esc(line)}</b>")
        else:
            out.append(_esc(line))
    return "\n".join(out)


def _dw(text: str) -> int:
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    gap = max(0, width - _dw(text))
    return (" " * gap + text) if right else (text + " " * gap)


def _color(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def print_us_heat(*, top: int = 25) -> None:
    now = now_tw().strftime("%m-%d %H:%M")
    print("正在抓美股熱度…")
    board = load_us_heat()
    show = board.rows[:top]
    print("正在補主要內容…")
    what = _what_map([row.code for row in show])
    w_tag = 26
    when = board.session + (f" · 資料 {board.asof}" if board.asof else "")
    print()
    print(
        _color("══ 美股 · 綜合熱度排行 ══", "1;36")
        + f"  {now}  · {when}  · Top {len(show)}/{len(board.rows)}"
    )
    print("-" * 118)
    print(
        _pad("#", 3, right=True)
        + "  "
        + _pad("代號", 6)
        + "  "
        + _pad("名稱", 16)
        + "  "
        + _pad("價格", 10, right=True)
        + "  "
        + _pad("今日", 8, right=True)
        + "  "
        + _pad("振幅", 8, right=True)
        + "  "
        + _pad("成交額", 12, right=True)
        + "  "
        + _pad("標籤", w_tag)
        + "  主要"
    )
    print("-" * 118)
    for i, row in enumerate(show, 1):
        chg = f"{row.chg:+.2f}%"
        chg_c = _color(_pad(chg, 8, right=True), "32" if row.chg >= 0 else "31")
        tags = ",".join(row.tags[:3])
        print(
            _pad(str(i), 3, right=True)
            + "  "
            + _pad(row.code, 6)
            + "  "
            + _pad(row.name, 16)
            + "  "
            + _pad(_px(row.last), 10, right=True)
            + "  "
            + chg_c
            + "  "
            + _pad(f"{row.range_pct:.2f}%", 8, right=True)
            + "  "
            + _pad(_usd(row.value), 12, right=True)
            + "  "
            + _pad(f"{tags} ({row.score:.0f})", w_tag)
            + "  "
            + (what.get(row.code) or "-")
        )
    print("-" * 118)
    print("分數＝流動性+動能+振幅；美股無費率。非買賣建議。")
    print()


def _nth_sunday(year: int, month: int, n: int):
    from datetime import date

    first = date(year, month, 1)
    day = 1 + (6 - first.weekday()) % 7
    return date(year, month, day + 7 * (n - 1))


def _us_dst(day) -> bool:
    return _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1)


def _et_now(now: datetime | None = None) -> datetime:
    now = now or now_tw()
    utc = now.astimezone(timezone.utc)
    et = utc.astimezone(timezone(timedelta(hours=-4)))
    if not _us_dst(et.date()):
        et = utc.astimezone(timezone(timedelta(hours=-5)))
    return et


def us_session_state(now: datetime | None = None) -> tuple[str, str]:
    """美股正規盤。回傳 (entry|flat|closed, 短句)。"""
    et = _et_now(now)
    if et.weekday() >= 5:
        return "closed", "已收盤"
    minutes = et.hour * 60 + et.minute
    if 9 * 60 + 35 <= minutes < 15 * 60 + 15:
        return "entry", "可進場"
    if 15 * 60 + 15 <= minutes < 16 * 60:
        return "flat", "只出不進"
    return "closed", "已收盤"


def build_us_stock_message(symbol: str, *, html: bool = True) -> str:
    """查一支美股：本業、報價，以及同一套日線／60分／15分條件。"""
    from company_brief import business_line
    from sr_logic import evaluate_setup
    from tw_daytrade import (
        _bias,
        _candles_from_chart,
        _classify,
        _fetch_chart,
        _prev_close,
        _px,
        _resample_60m,
    )

    code = "".join(ch for ch in symbol.upper() if ch.isalnum())
    if not code or not code.isalpha() or not (1 <= len(code) <= 5):
        text = f"❌ 無法辨識美股代號 {symbol}"
        return f"❌ {_esc(code or symbol)}" if html else text
    try:
        opener, crumb = _yahoo_opener()
        quotes = _quote_batch(opener, crumb, [code])
    except Exception as ex:
        err = f"美股 {code} 抓取失敗：{ex}"
        return f"❌ {_esc(err)}" if html else f"❌ {err}"
    if not quotes:
        text = f"❌ 找不到美股 {code}"
        return text if not html else f"❌ {_esc(text[2:])}"
    q = quotes[0]
    name = _short_name(str(q.get("shortName") or q.get("longName") or code), code)
    last = _f(q.get("regularMarketPrice"))
    prev = _f(q.get("regularMarketPreviousClose"))
    chg = _f(q.get("regularMarketChangePercent"))
    state, label = us_session_state()
    brief = business_line(code)
    now = now_tw()
    lines = [
        f"🇺🇸 {code} {name}",
        f"🕐 {now.strftime('%m-%d %H:%M')} ｜ {label}",
        f"現價 {_px(last)}  {chg:+.2f}%" if last else "現價 -",
    ]
    if brief:
        lines.append(brief)
    lines.append("日線定方向 · 60分當中週期 · 15分進場")
    if state == "entry":
        lines.append("收盤前平倉，不留倉。做空要有券。")
    elif state == "flat":
        lines.append("接近收盤，不再進新單。手上的單收盤前平倉。")
    else:
        lines.append("現在不是美股進場時段。下面是條件，下一盤再看。")

    intra = _fetch_chart(code, "15m", "60d")
    daily_raw = _fetch_chart(code, "1d", "1y")
    c15 = _candles_from_chart(intra) if intra else []
    daily = _candles_from_chart(daily_raw) if daily_raw else []
    c60 = _resample_60m(c15) if c15 else []
    if len(c15) >= 40 and len(c60) >= 40 and len(daily) >= 40 and last > 0:
        mtf: dict[str, str] = {}
        day_bias = _bias(daily)
        mid_bias = _bias(c60)
        if day_bias:
            mtf["1D"] = day_bias
        if mid_bias:
            mtf["4H"] = mid_bias
        result = evaluate_setup(code, c15, "15m", mtf=mtf, htf_poc_candles=c60)
        if prev <= 0:
            prev = _prev_close(daily, now)
        ranked = _classify(result, last, chg, prev)
        day = mtf.get("1D", "—")
        mid = mtf.get("4H", "—")
        lines.append(f"日{day} · 60分{mid}")
        if ranked and ranked[0] in {"go", "fade", "base", "wait"} and state == "entry":
            kind = {"go": "可進", "fade": "見頂", "base": "見底", "wait": "等回測"}[ranked[0]]
            side = "多" if result.side == "long" or result.structure == "偏多" else "空"
            lines.append(f"{kind} · 做{side} · RR {result.rr:.1f}")
            if result.entry > 0 and result.stop > 0 and result.tp > 0:
                lab = "計畫進場" if ranked[0] == "wait" else "進場"
                lines.append(f"{lab} {_px(result.entry)}  止損 {_px(result.stop)}  止盈 {_px(result.tp)}")
        else:
            lines.append("條件未齊，這盤不做。")
    else:
        lines.append("K 線不足，只先看本業和報價。")
    text = "\n".join(lines).rstrip()
    if not html:
        return text
    out = []
    for i, line in enumerate(text.splitlines()):
        if i == 0:
            out.append(f"<b>{_esc(line)}</b>")
        else:
            out.append(_esc(line))
    return "\n".join(out)
