"""每日重要財金新聞。只留標題，不收內文。"""

from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

TZ_TW = timezone(timedelta(hours=8))
_UA = {"User-Agent": "Mozilla/5.0"}
_QUERY = "台股 OR 美股 OR 聯準會 OR 台積電 OR 比特幣 OR 原油 OR 匯率 when:1d"
_FEED = (
    "https://news.google.com/rss/search?q="
    + urllib.parse.quote(_QUERY)
    + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
)

_CALENDAR = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_CAL_CACHE = Path(__file__).with_name("ff_calendar_cache.json")
_COUNTRY = {
    "USD": "美國",
    "JPY": "日本",
    "CNY": "中國",
    "EUR": "歐元",
    "GBP": "英國",
}
_EVENT_ZH = {
    "Unemployment Claims": "初請失業金",
    "Non-Farm Employment Change": "非農就業",
    "Unemployment Rate": "失業率",
    "CPI y/y": "消費者物價年增率",
    "CPI m/m": "消費者物價月增率",
    "Core CPI m/m": "核心消費者物價",
    "Core CPI y/y": "核心消費者物價年增率",
    "National Core CPI y/y": "核心CPI年增率",
    "FOMC Statement": "聯準會聲明",
    "Federal Funds Rate": "聯邦基金利率",
    "FOMC Press Conference": "聯準會記者會",
    "FOMC Meeting Minutes": "聯準會會議紀要",
    "GDP q/q": "GDP季增率",
    "Advance GDP q/q": "GDP初值",
    "Retail Sales m/m": "零售銷售",
    "Core Retail Sales m/m": "核心零售銷售",
    "Flash Manufacturing PMI": "製造業PMI初值",
    "Flash Services PMI": "服務業PMI初值",
    "Revised UoM Consumer Sentiment": "密大消費者信心",
    "Revised UoM Inflation Expectations": "密大通膨預期",
    "Crude Oil Inventories": "原油庫存",
    "ISM Manufacturing PMI": "ISM製造業",
    "ISM Services PMI": "ISM服務業",
    "ADP Non-Farm Employment Change": "ADP就業",
    "PPI m/m": "生產者物價",
    "Core PPI m/m": "核心生產者物價",
}
# 熱度榜上的美股代號，中文新聞通常不寫 ticker。
_US_ALIAS = {
    "NVDA": "輝達",
    "AAPL": "蘋果",
    "TSLA": "特斯拉",
    "AMD": "超微",
    "META": "Meta",
    "MSFT": "微軟",
    "AMZN": "亞馬遜",
    "GOOGL": "谷歌",
    "GOOG": "谷歌",
    "AVGO": "博通",
    "TSM": "台積電",
    "MU": "美光",
    "NFLX": "網飛",
    "INTC": "英特爾",
    "QCOM": "高通",
    "ARM": "安謀",
    "PLTR": "Palantir",
    "SMCI": "超微電腦",
    "DELL": "戴爾",
    "IBM": "IBM",
}

# 愈前面愈重要。命中才留下，避免社會、娛樂新聞混進來。
_TOPICS: list[tuple[str, tuple[str, ...]]] = [
    ("台股", ("台股", "加權", "櫃買", "台積電", "台積", "聯發科", "鴻海", "外資", "投信", "法人", "半導體")),
    ("國際", ("美股", "道瓊", "那斯達克", "標普", "聯準會", "聯準", "升息", "降息", "CPI", "非農", "財報", "輝達", "關稅", "央行")),
    ("匯率原物料", ("原油", "油價", "黃金", "新台幣", "匯率", "美元指數", "美債")),
    ("加密", ("比特幣", "以太幣", "以太坊", "加密")),
]


def now_tw() -> datetime:
    return datetime.now(TZ_TW)


@dataclass
class Headline:
    title: str
    source: str
    when: datetime
    topic: str
    score: int


def _child_text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    if el is not None and (el.text or "").strip():
        return el.text.strip()
    for child in item:
        if child.tag.endswith(tag) and (child.text or "").strip():
            return child.text.strip()
    return ""


def _fetch_items(query: str | None = None) -> list[ET.Element]:
    if query:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote(query)
            + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        )
    else:
        url = _FEED
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    return list(root.findall(".//item"))


def _event_zh(title: str) -> str | None:
    if title in _EVENT_ZH:
        return _EVENT_ZH[title]
    low = title.lower()
    if "fomc" in low or "federal funds" in low:
        return "聯準會"
    if "non-farm" in low or "nonfarm" in low:
        return "非農就業"
    if "unemployment claims" in low:
        return "初請失業金"
    if "cpi" in low:
        return "消費者物價"
    if "manufacturing pmi" in low:
        return "製造業PMI"
    if "services pmi" in low:
        return "服務業PMI"
    if low.startswith("gdp"):
        return "GDP"
    return None


def _load_calendar() -> list | None:
    """日曆一週才換，抓過就先用檔案。被限流時沿用舊檔。"""
    cached: list | None = None
    cached_at: datetime | None = None
    try:
        blob = json.loads(_CAL_CACHE.read_text(encoding="utf-8"))
        if isinstance(blob.get("data"), list):
            cached = blob["data"]
            cached_at = datetime.fromisoformat(str(blob.get("at") or ""))
            if cached_at.tzinfo is None:
                cached_at = cached_at.replace(tzinfo=TZ_TW)
    except Exception:
        cached = None
    if (
        cached is not None
        and cached_at is not None
        and datetime.now(TZ_TW) - cached_at < timedelta(minutes=45)
    ):
        return cached
    req = urllib.request.Request(_CALENDAR, headers={**_UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, list):
            _CAL_CACHE.write_text(
                json.dumps(
                    {"at": datetime.now(TZ_TW).isoformat(), "data": data},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return data
    except Exception:
        pass
    return cached


def _watch_lines(now: datetime) -> list[str] | None:
    """從現在到明早台股開盤前，留下會動到盤的數據。抓失敗回 None。"""
    data = _load_calendar()
    if not isinstance(data, list):
        return None
    start = now - timedelta(hours=2)
    end = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    picked: list[tuple[datetime, str]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            when = datetime.fromisoformat(str(row.get("date") or "")).astimezone(TZ_TW)
        except ValueError:
            continue
        if not (start <= when <= end):
            continue
        impact = str(row.get("impact") or "")
        country = str(row.get("country") or "")
        title = str(row.get("title") or "")
        zh = _event_zh(title)
        low = title.lower()
        major = any(k in low for k in ("cpi", "gdp", "fomc", "non-farm", "nonfarm", "unemployment claims"))
        if country == "USD" and impact in {"High", "Medium"}:
            pass
        elif country in {"JPY", "CNY"} and (impact == "High" or major):
            pass
        elif country in {"EUR", "GBP"} and impact == "High":
            pass
        elif major and country in {"USD", "JPY", "CNY"}:
            pass
        else:
            continue
        if zh is None and impact != "High":
            continue
        label = zh or title
        clock = when.strftime("%H:%M")
        if when.date() > now.date():
            clock = "明早 " + clock
        extra = []
        if str(row.get("forecast") or "").strip():
            extra.append(f"預期 {row['forecast']}")
        if str(row.get("previous") or "").strip():
            extra.append(f"前值 {row['previous']}")
        tail = ("  " + " · ".join(extra)) if extra else ""
        place = _COUNTRY.get(country, country)
        picked.append((when, f"{clock}  {place}{label}{tail}"))
    picked.sort(key=lambda item: item[0])
    return [text for _, text in picked[:4]]


def _num(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _px(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 100:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def _session_quotes() -> tuple[str, str]:
    from us_heat import _quote_batch, _yahoo_opener

    opener, crumb = _yahoo_opener()
    rows = _quote_batch(
        opener,
        crumb,
        ["^N225", "^KS11", "000001.SS", "BTC-USD", "ETH-USD"],
    )
    by_symbol = {str(row.get("symbol") or ""): row for row in rows}

    def bit(symbol: str, label: str) -> str:
        row = by_symbol.get(symbol) or {}
        price = _num(row.get("regularMarketPrice"))
        chg = _num(row.get("regularMarketChangePercent"))
        if price <= 0:
            return ""
        return f"{label} {_px(price)} {chg:+.2f}%"

    asia = " · ".join(
        part
        for part in (
            bit("^N225", "日經"),
            bit("^KS11", "韓股"),
            bit("000001.SS", "滬指"),
        )
        if part
    )
    crypto = " · ".join(
        part
        for part in (bit("BTC-USD", "比特幣"), bit("ETH-USD", "以太坊"))
        if part
    )
    return asia, crypto


def _tw_heat_labels() -> list[tuple[str, str]]:
    from tw_heat import load_tw_heat

    labels = []
    for row in load_tw_heat()[:8]:
        name = (row.name or "").strip()
        if len(name) < 2:
            continue
        labels.append((name, name))
    return labels


def _us_heat_labels() -> list[tuple[str, str]]:
    from us_heat import load_us_heat

    labels = []
    for row in load_us_heat().rows[:8]:
        code = (row.code or "").strip().upper()
        if len(code) < 2:
            continue
        keyword = _US_ALIAS.get(code) or code
        labels.append((code, keyword))
    return labels


def _heat_lines(labels: list[tuple[str, str]]) -> list[str]:
    words: list[str] = []
    for display, keyword in labels:
        for word in (keyword, display):
            if word and word not in words and len(word) >= 2:
                words.append(word)
    if not words:
        return []
    items = _fetch_items(" OR ".join(words[:12]) + " when:1d")
    now = now_tw()
    cutoff = now - timedelta(hours=30)
    parsed: list[tuple[datetime, str]] = []
    for item in items:
        source = _child_text(item, "source")
        title = _clean_title(_child_text(item, "title"), source)
        when = _parse_when(_child_text(item, "pubDate"))
        if not title or when is None or when < cutoff:
            continue
        if any(bad in title for bad in ("個股概覽", "個股行情", "個股走勢", "技術分析")):
            continue
        parsed.append((when, title))
    parsed.sort(key=lambda item: item[0], reverse=True)
    lines = []
    seen: set[str] = set()
    rank = {display: i for i, (display, _) in enumerate(labels)}
    found: list[tuple[int, str]] = []
    for _when, title in parsed:
        low = title.lower()
        for display, keyword in labels:
            if display in seen:
                continue
            keys = [keyword, display, _US_ALIAS.get(display, "")]
            if any(key and key.lower() in low for key in keys):
                seen.add(display)
                found.append((rank.get(display, 99), f"· {display}  {title}"))
                break
    found.sort(key=lambda item: item[0])
    return [text for _, text in found[:4]]


def _topic_and_score(title: str) -> tuple[str, int] | None:
    best_name = ""
    best_hits = 0
    score = 0
    low = title.lower()
    for name, words in _TOPICS:
        hits = sum(1 for w in words if w.lower() in low)
        score += hits * 2
        if hits > best_hits:
            best_hits = hits
            best_name = name
    if any(w in title for w in ("比特幣", "以太幣", "以太坊", "加密")):
        best_name = "加密"
        score += 2
    if score <= 0 or not best_name:
        return None
    return best_name, score


_DUP_MARKS = (
    "台積電adr",
    "spacex",
    "meta",
    "聯準會",
    "比特幣",
    "道瓊",
    "標普",
    "費半",
    "原油",
    "黃金",
    "加權",
)


def _dup_mark(title: str) -> str:
    low = title.lower().replace(" ", "")
    if "spacex" in low:
        return "spacex"
    if "adr" in low:
        return "adr"
    if "台股" in title and "台積" in title:
        return "台股盤"
    for mark in _DUP_MARKS:
        if mark in low:
            return mark
    return low[:12]


def _clean_title(title: str, source: str) -> str:
    title = html.unescape(title).strip()
    if source and title.endswith(source):
        title = title[: -len(source)].rstrip(" -–—|")
    if " - " in title:
        head, tail = title.rsplit(" - ", 1)
        if len(tail) <= 18:
            title = head.strip()
    if len(title) > 42:
        title = title[:42].rstrip("，,、｜| ") + "…"
    return title


def _parse_when(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_TW)


def collect_headlines(*, limit: int = 8) -> list[Headline]:
    now = now_tw()
    cutoff = now - timedelta(hours=30)
    rows: list[Headline] = []
    for item in _fetch_items():
        raw_title = _child_text(item, "title")
        source = _child_text(item, "source")
        title = _clean_title(raw_title, source)
        when = _parse_when(_child_text(item, "pubDate"))
        if not title or when is None or when < cutoff:
            continue
        ranked = _topic_and_score(title)
        if not ranked:
            continue
        topic, score = ranked
        rows.append(Headline(title, source, when, topic, score))
    rows.sort(key=lambda h: (h.score, h.when), reverse=True)
    best: dict[str, Headline] = {}
    for row in rows:
        mark = _dup_mark(row.title)
        prev = best.get(mark)
        if prev is None:
            best[mark] = row
            continue
        # 台股盤中以最新的一則為準，其餘同題留分數較高的。
        if mark == "台股盤":
            if row.when > prev.when:
                best[mark] = row
        elif (row.score, row.when) > (prev.score, prev.when):
            best[mark] = row
    unique = sorted(best.values(), key=lambda h: (h.score, h.when), reverse=True)
    # 每個主題最多 3 則，總數再砍到 limit。
    kept: list[Headline] = []
    counts: dict[str, int] = {}
    for row in unique:
        if counts.get(row.topic, 0) >= 3:
            continue
        counts[row.topic] = counts.get(row.topic, 0) + 1
        kept.append(row)
        if len(kept) >= limit:
            break
    order = {name: i for i, (name, _) in enumerate(_TOPICS)}
    kept.sort(key=lambda h: (order.get(h.topic, 9), -h.when.timestamp()))
    return kept


def _ansi(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m"


def _paint_pct(line: str) -> str:
    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            num = float(raw[:-1])
        except ValueError:
            return raw
        if num > 0:
            return _ansi(raw, "32")
        if num < 0:
            return _ansi(raw, "31")
        return raw

    return re.sub(r"[+-]\d+\.\d+%", repl, line)


def _paint_cmd(text: str) -> str:
    """終端機用。區段標題青色，今天要注意黃色，漲紅跌綠。"""
    heads = {name for name, _ in _TOPICS} | {"今天要注意", "亞洲盤", "熱度有新聞"}
    section = ""
    out: list[str] = []
    for i, line in enumerate(text.splitlines()):
        if i == 0 or line in heads:
            if line in heads:
                section = line
            out.append(_ansi(line, "1;36"))
            continue
        if line.startswith("　"):
            out.append(_ansi(line, "2"))
        elif section == "今天要注意" and line.startswith("·"):
            out.append(_ansi(line, "33"))
        elif section == "熱度有新聞" and line.startswith("·"):
            mark, sep, rest = line.partition("  ")
            out.append(_ansi(mark, "1;36") + sep + rest)
        elif section in {"亞洲盤", "加密"}:
            out.append(_paint_pct(line))
        else:
            out.append(line)
    return "\n".join(out)


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_fin_news_message(*, html_mode: bool = True, limit: int = 8) -> str:
    now = now_tw()
    watch: list[str] = []
    asia = ""
    crypto_px = ""
    labels: list[tuple[str, str]] = []
    rows: list[Headline] = []
    news_error = ""
    pool = ThreadPoolExecutor(max_workers=5)
    try:
        fut_news = pool.submit(collect_headlines, limit=limit)
        fut_watch = pool.submit(_watch_lines, now)
        fut_px = pool.submit(_session_quotes)
        fut_tw = pool.submit(_tw_heat_labels)
        fut_us = pool.submit(_us_heat_labels)
        try:
            rows = fut_news.result(timeout=12)
        except Exception as ex:
            news_error = str(ex)
        try:
            watch = fut_watch.result(timeout=8)
        except Exception:
            watch = None
        try:
            asia, crypto_px = fut_px.result(timeout=8)
        except Exception:
            asia, crypto_px = "", ""
        try:
            labels.extend(fut_tw.result(timeout=3))
        except Exception:
            pass
        try:
            labels.extend(fut_us.result(timeout=3))
        except Exception:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    heat: list[str] = []
    if labels:
        try:
            heat = _heat_lines(labels)
        except Exception:
            heat = []
    if news_error and not rows and watch is None and not asia:
        err = f"財金新聞抓取失敗：{news_error}"
        return f"❌ {_esc(err)}" if html_mode else f"❌ {err}"
    lines = [
        "📰 財金新聞",
        f"🕐 {now.strftime('%m-%d %H:%M')} ｜ 近 30 小時重點",
        "",
        "今天要注意",
    ]
    if watch:
        lines.extend(f"· {line}" for line in watch)
    elif watch is None:
        lines.append("· 數據日曆暫時沒抓到。")
    else:
        lines.append("· 今天到明早開盤，日曆上沒有要緊的數據。")
    if asia:
        lines.extend(["", "亞洲盤", asia])
    if heat:
        lines.extend(["", "熱度有新聞", *heat])
    if not rows:
        lines.extend(["", "這段時間沒有對到台股、美股、匯率或加密的要聞。"])
    else:
        current = ""
        for row in rows:
            if row.topic != current:
                lines.append("")
                current = row.topic
                lines.append(current)
                if current == "加密" and crypto_px:
                    lines.append(crypto_px)
            clock = row.when.strftime("%m-%d %H:%M")
            src = f" · {row.source}" if row.source else ""
            lines.append(f"· {row.title}")
            lines.append(f"　{clock}{src}")
        if crypto_px and not any(row.topic == "加密" for row in rows):
            lines.extend(["", "加密", crypto_px])
    text = "\n".join(lines).rstrip()
    heads = {name for name, _ in _TOPICS} | {"今天要注意", "亞洲盤", "熱度有新聞"}
    if not html_mode:
        return _paint_cmd(text)
    out = []
    for i, line in enumerate(text.splitlines()):
        if i == 0 or line in heads:
            out.append(f"<b>{_esc(line)}</b>")
        else:
            out.append(_esc(line))
    return "\n".join(out)
