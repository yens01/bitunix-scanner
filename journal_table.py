#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把今日日記畫成手機可讀的表格 PNG。"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

_FONT = Path(__file__).resolve().parent / "fonts" / "NotoSansSC-subset.otf"
_SCALE = 2
_PAGE = 32

_BG = (255, 255, 255)
_HEADER_BG = (27, 94, 69)
_HEADER_FG = (255, 255, 255)
_TITLE = (20, 56, 44)
_ZEBRA = (244, 248, 245)
_GRID = (210, 220, 214)
_TEXT = (28, 32, 30)
_FOOT = (90, 98, 94)
_TONE = {
    "win": (22, 122, 58),
    "loss": (183, 40, 40),
    "timeout": (96, 104, 100),
    "wait": (176, 86, 20),
    "open": (21, 88, 150),
}
_PLAN = (29, 78, 137)

# 邏輯寬 1080，欄寬加總 1040，左右各留 20
_COLS = (100, 100, 128, 52, 148, 176, 176, 160)


def render_today_table_pngs(model: dict[str, Any]) -> list[bytes]:
    """有資料才畫；缺字型或 Pillow 時回空清單，呼叫端改送文字。"""
    records = list(model.get("records") or [])
    if not records or not _FONT.exists():
        return []
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return []
    pages: list[bytes] = []
    total = (len(records) + _PAGE - 1) // _PAGE
    for page, start in enumerate(range(0, len(records), _PAGE)):
        chunk = records[start : start + _PAGE]
        title = str(model.get("title") or "今日紀錄")
        if total > 1:
            title = f"{title}  {page + 1}/{total}"
        png = _draw_page(
            Image,
            ImageDraw,
            ImageFont,
            title=title,
            summary=str(model.get("summary") or ""),
            footnote=str(model.get("footnote") or "") if page == total - 1 else "",
            headers=list(model.get("headers") or []),
            records=chunk,
        )
        if png:
            pages.append(png)
    return pages


def _draw_page(Image, ImageDraw, ImageFont, *, title, summary, footnote, headers, records) -> bytes | None:
    s = _SCALE
    font_title = ImageFont.truetype(str(_FONT), 34 * s)
    font_sub = ImageFont.truetype(str(_FONT), 22 * s)
    font_head = ImageFont.truetype(str(_FONT), 24 * s)
    font_cell = ImageFont.truetype(str(_FONT), 22 * s)
    font_foot = ImageFont.truetype(str(_FONT), 18 * s)

    pad = 20 * s
    width = 1080 * s
    title_h = 54 * s
    sub_h = 40 * s if summary else 0
    head_h = 48 * s
    row_h = 46 * s
    foot_h = 40 * s if footnote else 16 * s
    height = pad + title_h + sub_h + head_h + row_h * len(records) + foot_h + pad

    img = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(img)
    y = pad
    draw.text((pad, y), title, font=font_title, fill=_TITLE)
    y += title_h
    if summary:
        draw.text((pad, y), summary, font=font_sub, fill=_TEXT)
        y += sub_h

    x0 = pad
    table_w = sum(c * s for c in _COLS)
    draw.rectangle((x0, y, x0 + table_w, y + head_h), fill=_HEADER_BG)
    x = x0
    for i, label in enumerate(headers):
        col_w = _COLS[i] * s
        _cell(draw, label, x, y, col_w, head_h, font_head, _HEADER_FG, "center")
        x += col_w
    y += head_h

    for n, rec in enumerate(records):
        bg = _ZEBRA if n % 2 == 0 else _BG
        draw.rectangle((x0, y, x0 + table_w, y + row_h), fill=bg)
        tone = _TONE.get(rec.get("tone") or "", _TEXT)
        cells = list(rec.get("cells") or [])
        x = x0
        for i, col_w_logic in enumerate(_COLS):
            col_w = col_w_logic * s
            text = cells[i] if i < len(cells) else ""
            if i == 4:
                fill = tone
                align = "center"
            elif i == 5 and rec.get("planned"):
                fill = _PLAN
                align = "right"
            elif i >= 5:
                fill = _TEXT
                align = "right"
            elif i in (0, 1, 3):
                fill = _TEXT
                align = "center"
            else:
                fill = _TEXT
                align = "left"
            _cell(draw, text, x, y, col_w, row_h, font_cell, fill, align)
            x += col_w
        draw.line((x0, y + row_h, x0 + table_w, y + row_h), fill=_GRID, width=max(1, s // 2))
        y += row_h

    draw.rectangle((x0, y - row_h * len(records) - head_h, x0 + table_w, y), outline=_GRID, width=s)
    if footnote:
        y += 12 * s
        draw.text((pad, y), footnote, font=font_foot, fill=_FOOT)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _cell(draw, text: str, x: int, y: int, w: int, h: int, font, fill, align: str) -> None:
    text = str(text or "")
    inset = 8 * _SCALE
    max_w = w - inset * 2
    size = font.size
    while text and font.getlength(text) > max_w and size > 14 * _SCALE:
        size -= 2
        font = font.font_variant(size=size)
    tw = font.getlength(text) if text else 0
    if align == "right":
        tx = x + w - inset - tw
    elif align == "center":
        tx = x + (w - tw) / 2
    else:
        tx = x + inset
    # vertical center
    bbox = font.getbbox(text or "測")
    th = bbox[3] - bbox[1]
    ty = y + (h - th) / 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=fill)
