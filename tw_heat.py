"""台股綜合熱度。

對齊 Bitunix 熱度：流動性、漲跌動能、振幅。
台股沒有資金費率與槓桿，這兩項的權重攤進前三項，分數仍是 0–100。
漲跌停約 ±10%，所以動能以 7%、振幅以 8% 封頂，避免整榜擠在低分。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

TZ_TW = timezone(timedelta(hours=8))
_UA = {"User-Agent": "Mozilla/5.0"}
_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
_MIS = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
# 成交額低於這個值的雜訊不進榜（新台幣）。
_MIN_VALUE = 30_000_000


def now_tw() -> datetime:
    return datetime.now(TZ_TW)


def _http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except urllib.error.URLError as ex:
        msg = str(ex).upper()
        if "CERTIFICATE" not in msg and "SSL" not in msg:
            raise
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return resp.read()


def _f(value: object) -> float:
    try:
        if value is None or value == "" or value == "-":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _listed_codes() -> list[tuple[str, str]]:
    raw = _http_bytes(_DAY_ALL)
    rows = json.loads(raw.decode("utf-8"))
    out: list[tuple[str, str]] = []
    for row in rows:
        code = str(row.get("Code") or "").strip()
        name = str(row.get("Name") or "").strip()
        if not code.isdigit():
            continue
        if len(code) == 4 or code.startswith("00"):
            out.append((code, name))
    return out


def _fetch_batch(codes: list[str]) -> list[dict]:
    ex = "|".join(f"tse_{code}.tw" for code in codes)
    url = f"{_MIS}?ex_ch={ex}&json=1&delay=0"
    raw = _http_bytes(url)
    text = raw.decode("utf-8", errors="replace")
    start = text.find("{")
    if start < 0:
        return []
    data = json.loads(text[start:])
    return list(data.get("msgArray") or [])


def _last_price(row: dict) -> float:
    px = _f(row.get("z"))
    if px > 0:
        return px
    trade = row.get("trade") or {}
    if isinstance(trade, dict):
        px = _f(trade.get("z"))
        if px > 0:
            return px
    return 0.0


@dataclass
class TwHeat:
    code: str
    name: str
    last: float
    chg: float
    range_pct: float
    value: float
    score: float
    tags: list[str]


def _build(rows: list[dict], names: dict[str, str]) -> list[TwHeat]:
    parsed: list[tuple[dict, float, float, float, float, float]] = []
    for row in rows:
        code = str(row.get("c") or "").strip()
        last = _last_price(row)
        prev = _f(row.get("y"))
        high = _f(row.get("h"))
        low = _f(row.get("l"))
        open_ = _f(row.get("o"))
        lots = _f(row.get("v"))
        if not code or last <= 0 or prev <= 0 or lots <= 0:
            continue
        value = lots * 1000 * last
        if value < _MIN_VALUE:
            continue
        base = open_ if open_ > 0 else prev
        chg = (last - prev) / prev * 100
        rng = ((high - low) / base * 100) if base > 0 and high > 0 and low > 0 else 0.0
        parsed.append((row, last, chg, rng, value, lots))

    values = sorted(item[4] for item in parsed)

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

    heats: list[TwHeat] = []
    for row, last, chg, rng, value, _lots in parsed:
        code = str(row.get("c") or "").strip()
        tags: list[str] = []
        score = 0.0
        vr = vol_rank(value)
        score += vr * 0.41
        if vr >= 90:
            tags.append("高流動")
        elif vr >= 70:
            tags.append("流動佳")

        mom = min(abs(chg) / 7.0, 1.0) * 100
        score += mom * 0.35
        if chg >= 4:
            tags.append("強勢漲")
        elif chg >= 2:
            tags.append("偏多")
        elif chg <= -4:
            tags.append("急跌")
        elif chg <= -2:
            tags.append("偏空")

        vola = min(rng / 8.0, 1.0) * 100
        score += vola * 0.24
        if rng >= 5:
            tags.append("高波動")
        if not tags:
            tags.append("觀望")

        name = str(row.get("n") or "").strip() or names.get(code, code)
        heats.append(
            TwHeat(
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
    return heats


def load_tw_heat() -> list[TwHeat]:
    listed = _listed_codes()
    names = {code: name for code, name in listed}
    codes = [code for code, _ in listed]
    rows: list[dict] = []
    batches = [codes[i : i + 80] for i in range(0, len(codes), 80)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_fetch_batch, batch) for batch in batches]
        for fut in as_completed(futs):
            try:
                rows.extend(fut.result())
            except Exception:
                continue
    return _build(rows, names)


def _px(v: float) -> str:
    if v >= 1000:
        return f"{v:,.2f}"
    if v >= 100:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _yi(v: float) -> str:
    if v >= 100_000_000:
        return f"{v / 100_000_000:.2f}億"
    if v >= 10_000:
        return f"{v / 10_000:.0f}萬"
    return f"{v:.0f}"


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _what_map(codes: list[str]) -> dict[str, str]:
    try:
        from company_brief import business_shorts
    except Exception:
        return {}
    primary = business_shorts([f"{code}.TW" for code in codes])
    missing = [code for code in codes if not primary.get(f"{code}.TW")]
    extra = business_shorts([f"{code}.TWO" for code in missing]) if missing else {}
    return {
        code: primary.get(f"{code}.TW") or extra.get(f"{code}.TWO") or ""
        for code in codes
    }


def build_tw_heat_message(*, top: int = 12, html: bool = True) -> str:
    now = now_tw()
    try:
        rows = load_tw_heat()
    except Exception as ex:
        err = f"台股熱度抓取失敗：{ex}"
        return f"❌ {_esc(err)}" if html else f"❌ {err}"
    show = rows[:top]
    what = _what_map([row.code for row in show])
    lines = [
        "🔥 台股熱度",
        f"🕐 {now.strftime('%m-%d %H:%M')} ｜ 上市 ｜ Top {len(show)}/{len(rows)}",
        "分數＝流動性＋動能＋振幅。沒有費率。",
        "",
    ]
    if not show:
        lines.append("目前沒有通過成交額門檻的股票。")
    for i, row in enumerate(show, 1):
        tags = "、".join(row.tags[:3])
        lines.append(
            f"{i}. {row.code} {row.name}  {_px(row.last)}  {row.chg:+.2f}%"
        )
        if what.get(row.code):
            lines.append(f"　主要：{what[row.code]}")
        lines.append(
            f"　振幅 {row.range_pct:.2f}% ｜ 額 {_yi(row.value)} ｜ {tags} ({row.score:.0f})"
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


def print_tw_heat(*, top: int = 25) -> None:
    now = now_tw().strftime("%m-%d %H:%M")
    print("正在抓上市熱度…")
    rows = load_tw_heat()
    show = rows[:top]
    print("正在補主要內容…")
    what = _what_map([row.code for row in show])
    w_tag = 26
    print()
    print(_color(f"══ 台股 · 綜合熱度排行 ══", "1;36") + f"  {now}  · Top {len(show)}/{len(rows)}")
    print("-" * 108)
    print(
        _pad("#", 3, right=True)
        + "  "
        + _pad("代號", 6)
        + "  "
        + _pad("名稱", 12)
        + "  "
        + _pad("價格", 10, right=True)
        + "  "
        + _pad("今日", 8, right=True)
        + "  "
        + _pad("振幅", 8, right=True)
        + "  "
        + _pad("成交額", 10, right=True)
        + "  "
        + _pad("標籤", w_tag)
        + "  主要"
    )
    print("-" * 108)
    for i, row in enumerate(show, 1):
        chg = f"{row.chg:+.2f}%"
        chg_c = _color(_pad(chg, 8, right=True), "32" if row.chg >= 0 else "31")
        tags = ",".join(row.tags[:3])
        print(
            _pad(str(i), 3, right=True)
            + "  "
            + _pad(row.code, 6)
            + "  "
            + _pad(row.name, 12)
            + "  "
            + _pad(_px(row.last), 10, right=True)
            + "  "
            + chg_c
            + "  "
            + _pad(f"{row.range_pct:.2f}%", 8, right=True)
            + "  "
            + _pad(_yi(row.value), 10, right=True)
            + "  "
            + _pad(f"{tags} ({row.score:.0f})", w_tag)
            + "  "
            + (what.get(row.code) or "-")
        )
    print("-" * 108)
    print("分數＝流動性+動能+振幅；台股無費率。非買賣建議。")
    print()
