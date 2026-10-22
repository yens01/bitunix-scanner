"""小幣本業。用 CoinGecko 的分類與英文簡介，收成一句中文。"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

_UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
_CACHE: dict[str, str] = {}
_CACHE_PATH = Path(__file__).with_name("coin_brief_cache.json")
_CACHE_LOADED = False
_SAVE = threading.Lock()
_PACE = threading.Lock()
_last_call = 0.0


def _load_cache() -> None:
    global _CACHE_LOADED
    if _CACHE_LOADED:
        return
    _CACHE_LOADED = True
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and value.strip():
                _CACHE[str(key)] = value


def _save_cache() -> None:
    _load_cache()
    try:
        with _SAVE:
            try:
                disk = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                disk = {}
            if isinstance(disk, dict):
                for key, value in disk.items():
                    if key not in _CACHE and isinstance(value, str) and value.strip():
                        _CACHE[str(key)] = value
            _CACHE_PATH.write_text(
                json.dumps(_CACHE, ensure_ascii=False),
                encoding="utf-8",
            )
    except Exception:
        return


def _get_json(url: str) -> dict | list | None:
    """稍微錯開請求。被限流就等一下再試一次，不要把後面的幣全部放棄。"""
    global _last_call
    req = urllib.request.Request(url, headers=_UA)
    for attempt in range(3):
        with _PACE:
            gap = 0.35 - (time.time() - _last_call)
            if gap > 0:
                time.sleep(gap)
            _last_call = time.time()
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            if ex.code == 429 and attempt < 2:
                time.sleep(1.5)
                continue
            return None
        except Exception:
            return None
    return None

# 長的關鍵字放前面，避免短字先吃掉。
_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Decentralized Exchange", "去中心化交易所"),
    ("Liquid Staking", "流動性質押"),
    ("Liquid Restaking", "再質押"),
    ("Artificial Intelligence", "人工智慧"),
    ("Smart Contract Platform", "智能合約平台"),
    ("Real World Assets", "實物資產上鏈"),
    ("Layer 2", "二層網路"),
    ("Layer 1", "公鏈"),
    ("Centralized Exchange", "中心化交易所"),
    ("Decentralized Finance", "去中心化金融"),
    ("Meme", "迷因幣"),
    ("Gaming", "遊戲"),
    ("GameFi", "遊戲"),
    ("Oracle", "預言機"),
    ("Storage", "去中心化儲存"),
    ("Privacy", "隱私"),
    ("Stablecoins", "穩定幣"),
    ("Stablecoin", "穩定幣"),
    ("Lending", "借貸"),
    ("Perpetuals", "永續合約"),
    ("NFT", "NFT"),
    ("Infrastructure", "基礎設施"),
    ("Wallet", "錢包"),
    ("Bridge", "跨鏈橋"),
    ("Social", "社群"),
    ("DePIN", "去中心化實體基建"),
    ("DeFi", "去中心化金融"),
    ("AI", "人工智慧"),
)

_HINTS: tuple[tuple[str, str], ...] = (
    ("decentralized exchange", "去中心化交易所"),
    ("perpetual", "永續合約"),
    ("liquid staking", "流動性質押"),
    ("restaking", "再質押"),
    ("artificial intelligence", "人工智慧"),
    ("layer 2", "二層網路"),
    ("stablecoin", "穩定幣"),
    ("meme", "迷因幣"),
    ("lending", "借貸"),
    ("oracle", "預言機"),
    ("wallet", "錢包"),
    ("bridge", "跨鏈橋"),
    ("privacy", "隱私"),
    ("storage", "去中心化儲存"),
    ("real world asset", "實物資產上鏈"),
    ("game", "遊戲"),
    ("nft", "NFT"),
    ("payment", "支付"),
)


def _base(symbol: str) -> str:
    base = symbol.upper().replace("USDT", "").replace("-", "")
    if base.startswith("1000") and len(base) > 4:
        base = base[4:]
    return base


def _coin_id(base: str) -> str | None:
    """找到回 id，確定沒有回空字串，網路失敗回 None。"""
    url = "https://api.coingecko.com/api/v3/search?query=" + urllib.parse.quote(base.lower())
    data = _get_json(url)
    if not isinstance(data, dict):
        return _id_from_list(base) or None
    coins = data.get("coins") or []
    exact = [c for c in coins if str(c.get("symbol") or "").upper() == base]
    if not exact:
        return _id_from_list(base) or ""

    def rank(c: dict) -> int:
        value = c.get("market_cap_rank")
        return value if isinstance(value, int) else 10**9

    exact.sort(key=rank)
    found = str(exact[0].get("id") or "")
    if found:
        return found
    return _id_from_list(base)


_LIST_PATH = Path(__file__).with_name("coin_list_cache.json")
_LIST: list[dict] | None = None


def _coin_list() -> list[dict]:
    global _LIST
    if _LIST is not None:
        return _LIST
    try:
        data = json.loads(_LIST_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            _LIST = data
            return _LIST
    except Exception:
        pass
    url = "https://api.coingecko.com/api/v3/coins/list"
    data = _get_json(url)
    if not isinstance(data, list):
        _LIST = []
        return _LIST
    _LIST = data
    try:
        _LIST_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return _LIST


def _id_from_list(base: str) -> str:
    """搜尋前幾筆沒有這個代號時，改從全清單對代號，再用市值挑一檔。"""
    ids = [
        str(row.get("id") or "")
        for row in _coin_list()
        if str(row.get("symbol") or "").upper() == base and row.get("id")
    ]
    ids = [item for item in ids if item]
    if not ids:
        return ""
    if len(ids) == 1:
        return ids[0]
    url = (
        "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids="
        + urllib.parse.quote(",".join(ids[:15]))
    )
    rows = _get_json(url)
    if not isinstance(rows, list) or not rows:
        return ids[0]
    # 同代號很多灰塵幣。先留下有實際價格的，再比市值。
    priced = [
        row
        for row in rows
        if isinstance(row.get("current_price"), (int, float)) and row["current_price"] >= 0.01
    ]
    pool = priced or rows

    def rank_row(row: dict) -> tuple[float, float, float]:
        def num(key: str) -> float:
            value = row.get(key)
            return float(value) if isinstance(value, (int, float)) else 0.0

        return (num("market_cap"), num("total_volume"), num("current_price"))

    pool.sort(key=rank_row, reverse=True)
    return str(pool[0].get("id") or ids[0])


def _labels(categories: list, summary: str) -> list[str]:
    found: list[str] = []
    for cat in categories or []:
        text = str(cat)
        if any(word in text for word in ("Ecosystem", "Index", "Themed")):
            continue
        for key, label in _CATEGORIES:
            if key.lower() in text.lower() and label not in found:
                found.append(label)
                break
        if len(found) >= 2:
            return found
    if found:
        return found
    low = re.sub(r"<[^>]+>", " ", summary or "").lower()
    for key, label in _HINTS:
        if key in low and label not in found:
            found.append(label)
        if len(found) >= 2:
            break
    return found


def coin_brief(symbol: str) -> str:
    """一句中文，說明這個幣主要是什麼。沒有資料就回空字串。"""
    briefs = coin_briefs([symbol])
    return briefs.get(symbol.upper(), "") or briefs.get(_base(symbol), "")


def _remember(out: dict[str, str], symbol: str, text: str) -> None:
    if not text:
        return
    out[symbol] = text
    out[symbol.upper()] = text
    out[_base(symbol)] = text


def _one(symbol: str) -> tuple[str, str, bool]:
    """回傳 (代號, 說明, 是否可快取)。網路失敗不要快取。"""
    base = _base(symbol)
    cid = _coin_id(base)
    if cid is None:
        return symbol, "", False
    if not cid:
        return symbol, "", True
    url = (
        "https://api.coingecko.com/api/v3/coins/"
        + urllib.parse.quote(cid)
        + "?localization=false&tickers=false&market_data=false"
        + "&community_data=false&developer_data=false&sparkline=false"
    )
    data = _get_json(url)
    if not data:
        return symbol, "", False
    desc = data.get("description") or {}
    summary = desc.get("en") if isinstance(desc, dict) else ""
    labels = _labels(data.get("categories") or [], str(summary or ""))
    name = re.sub(r"\s+", " ", str(data.get("name") or "")).strip()
    if len(name) > 24:
        cut = name[:24].rsplit(" ", 1)[0]
        name = (cut or name[:24]).rstrip()
    purpose = "、".join(labels)
    if name and purpose and name.upper() != base:
        text = f"{name}｜{purpose}"
    else:
        text = purpose or (name if name.upper() != base else "")
    return symbol, text, bool(text)


def coin_briefs(symbols: list[str]) -> dict[str, str]:
    """回傳 {原代號: 中文說明}，說明不含「主要」二字。

    熱度榜不能等。查過的讀快取；沒查過的同時查，最多等 18 秒。
    """
    _load_cache()
    uniq: list[str] = []
    for symbol in symbols:
        if symbol and symbol not in uniq:
            uniq.append(symbol)
    out: dict[str, str] = {}
    pending: list[str] = []
    for symbol in uniq:
        base = _base(symbol)
        if base in _CACHE:
            _remember(out, symbol, _CACHE[base])
        else:
            pending.append(symbol)
    if not pending:
        return out
    workers = min(2, len(pending))
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(_one, symbol) for symbol in pending]
    try:
        done, _rest = wait(futs, timeout=25)
        for fut in done:
            try:
                symbol, text, ok = fut.result()
            except Exception:
                continue
            if ok and text:
                _CACHE[_base(symbol)] = text
            _remember(out, symbol, text)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        _save_cache()
    return out
