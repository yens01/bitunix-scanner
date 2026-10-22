"""單股公司本業。用 Yahoo 的產業與業務摘要，收成一句中文。"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

_SECTOR = {
    "Technology": "科技",
    "Financial Services": "金融",
    "Healthcare": "醫療",
    "Consumer Cyclical": "可選消費",
    "Consumer Defensive": "民生消費",
    "Industrials": "工業",
    "Energy": "能源",
    "Basic Materials": "原物料",
    "Real Estate": "房地產",
    "Utilities": "公用事業",
    "Communication Services": "通訊",
}

_INDUSTRY = {
    "Semiconductors": "半導體",
    "Semiconductor Equipment & Materials": "半導體設備",
    "Consumer Electronics": "消費性電子",
    "Software - Infrastructure": "基礎軟體",
    "Software - Application": "應用軟體",
    "Software—Infrastructure": "基礎軟體",
    "Software—Application": "應用軟體",
    "Internet Content & Information": "網路內容",
    "Banks - Diversified": "銀行",
    "Banks - Regional": "銀行",
    "Insurance - Diversified": "保險",
    "Credit Services": "信用與支付",
    "Auto Manufacturers": "汽車製造",
    "Drug Manufacturers - General": "製藥",
    "Biotechnology": "生技",
    "Oil & Gas Integrated": "石油",
    "Telecom Services": "電信",
    "Entertainment": "影音娛樂",
    "Internet Retail": "電商",
    "Specialty Retail": "零售",
    "Restaurants": "餐飲",
    "Aerospace & Defense": "航太國防",
    "Information Technology Services": "資訊服務",
    "Electronic Components": "電子零組件",
    "Computer Hardware": "電腦硬體",
    "Scientific & Technical Instruments": "儀器",
}

# 摘要裡常見字，用來補「具體在做什麼」。
_HINTS: tuple[tuple[str, str], ...] = (
    ("smartphone", "智慧手機"),
    ("personal computer", "個人電腦"),
    ("tablet", "平板"),
    ("wearable", "穿戴裝置"),
    ("integrated circuit", "積體電路"),
    ("semiconductor", "半導體"),
    ("wafer", "晶圓"),
    ("graphics processing", "繪圖晶片"),
    ("data center", "資料中心"),
    ("artificial intelligence", "人工智慧"),
    ("cloud", "雲端"),
    ("software", "軟體"),
    ("social media", "社群"),
    ("advertising", "廣告"),
    ("streaming", "串流"),
    ("video game", "遊戲"),
    ("electric vehicle", "電動車"),
    ("automobile", "汽車"),
    ("battery", "電池"),
    ("solar", "太陽能"),
    ("oil", "石油"),
    ("natural gas", "天然氣"),
    ("bank", "銀行"),
    ("insurance", "保險"),
    ("payment", "支付"),
    ("credit card", "信用卡"),
    ("drug", "藥品"),
    ("biotech", "生技"),
    ("medical device", "醫療器材"),
    ("retail", "零售"),
    ("restaurant", "餐飲"),
    ("airline", "航空"),
    ("shipping", "航運"),
    ("steel", "鋼鐵"),
    ("chemical", "化學"),
    ("real estate", "房地產"),
    ("telecom", "電信"),
    ("cyber", "資安"),
)


def _fetch_profile(opener, crumb: str, yahoo: str, timeout: int = 8) -> dict | None:
    """成功回 profile（可能是空 dict），網路失敗回 None。"""
    url = (
        "https://query1.finance.yahoo.com/v10/finance/quoteSummary/"
        + urllib.parse.quote(yahoo)
        + "?modules=assetProfile&crumb="
        + urllib.parse.quote(crumb)
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        raw = opener.open(req, timeout=timeout).read()
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    result = (body.get("quoteSummary") or {}).get("result") or []
    if not result:
        return {}
    return result[0].get("assetProfile") or {}


def _profile(yahoo: str) -> dict:
    from us_heat import _yahoo_opener

    opener, crumb = _yahoo_opener()
    return _fetch_profile(opener, crumb, yahoo) or {}


def _bits(summary: str, industry: str) -> list[str]:
    low = summary.lower()
    skip = {
        "Semiconductors": {"智慧手機", "個人電腦", "平板", "穿戴裝置", "汽車", "電動車", "遊戲"},
        "Semiconductor Equipment & Materials": {"智慧手機", "個人電腦", "平板", "穿戴裝置"},
        "Electronic Components": {"智慧手機", "個人電腦", "平板"},
    }.get(industry, set())
    found: list[str] = []
    for key, label in _HINTS:
        if label in skip:
            continue
        if key in low and label not in found:
            found.append(label)
    ind = _INDUSTRY.get(industry) or ""
    if ind and ind not in found:
        found.append(ind)
    return found[:4]


def _verb(summary: str) -> str:
    low = summary.lower()
    makes = any(k in low for k in ("manufactur", "produc", "fabricat"))
    sells = any(k in low for k in ("sell", "market", "design", "develop"))
    if makes and sells:
        return "製造並銷售"
    if makes:
        return "製造"
    if "design" in low and sells:
        return "設計並銷售"
    if sells:
        return "銷售"
    if any(k in low for k in ("provid", "operat", "offer")):
        return "提供"
    return "從事"


def _short_text(profile: dict) -> str:
    summary = str(profile.get("longBusinessSummary") or "")
    industry = str(profile.get("industry") or "")
    bits = _bits(summary, industry)[:2]
    if bits:
        return "、".join(bits)
    return _SECTOR.get(str(profile.get("sector") or ""), "")


_SHORT_CACHE: dict[str, str] = {}
_SHORT_PATH = Path(__file__).with_name("company_brief_cache.json")
_SHORT_LOADED = False


def _load_short_cache() -> None:
    global _SHORT_LOADED
    if _SHORT_LOADED:
        return
    _SHORT_LOADED = True
    try:
        data = json.loads(_SHORT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str):
                _SHORT_CACHE[str(key)] = value


def _save_short_cache() -> None:
    try:
        _SHORT_PATH.write_text(json.dumps(_SHORT_CACHE, ensure_ascii=False), encoding="utf-8")
    except Exception:
        return


def business_shorts(symbols: list[str]) -> dict[str, str]:
    """熱度欄用的短說明，不含「主要」二字。查不到的鍵給空字串。最多等 4 秒。"""
    _load_short_cache()
    uniq: list[str] = []
    for symbol in symbols:
        if symbol and symbol not in uniq:
            uniq.append(symbol)
    pending = [symbol for symbol in uniq if symbol not in _SHORT_CACHE]
    if pending:
        from us_heat import _yahoo_opener

        try:
            opener, crumb = _yahoo_opener()
        except Exception:
            opener = crumb = None
        if opener is not None and crumb:

            def fetch(symbol: str) -> tuple[str, str | None]:
                profile = _fetch_profile(opener, crumb, symbol, timeout=4)
                if profile is None:
                    return symbol, None
                return symbol, _short_text(profile)

            ex = ThreadPoolExecutor(max_workers=min(6, len(pending)))
            futs = [ex.submit(fetch, symbol) for symbol in pending]
            try:
                done, _rest = wait(futs, timeout=4)
                for fut in done:
                    try:
                        symbol, text = fut.result()
                    except Exception:
                        continue
                    if text is not None:
                        _SHORT_CACHE[symbol] = text
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
                _save_short_cache()
    return {symbol: _SHORT_CACHE.get(symbol, "") for symbol in uniq}


def business_line(*yahoo_symbols: str) -> str:
    """一句中文。yahoo_symbols 依序試，例如 2330.TW、2330.TWO。"""
    profile: dict = {}
    for yahoo in yahoo_symbols:
        if not yahoo:
            continue
        try:
            profile = _profile(yahoo)
        except Exception:
            profile = {}
        if profile.get("longBusinessSummary") or profile.get("industry"):
            break
    if not profile:
        return ""
    summary = str(profile.get("longBusinessSummary") or "")
    industry = str(profile.get("industry") or "")
    sector = _SECTOR.get(str(profile.get("sector") or ""), "")
    bits = _bits(summary, industry)
    if bits:
        return f"主要：{_verb(summary)}{'、'.join(bits)}。"
    if sector:
        return f"主要：{sector}。"
    return ""
