"""Separate journal signal outcomes from paper-executed trading performance."""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timedelta, timezone

from paper_status import fetch_snapshot
from paper_performance import report

TW = timezone(timedelta(hours=8))


def build_comparison_message(rows, state, *, now=None):
    now = now or datetime.now(TW)
    day = now.strftime("%Y-%m-%d")
    today = [r for r in rows if (r.ts or "")[:10] == day]
    closed = [r for r in today if r.status in ("win", "loss", "timeout")
              and r.side in ("long", "short")]
    total_r = sum(float(r.realized_r or 0) for r in closed)
    pending = sum(r.status == "open" for r in today)
    unfilled = sum(r.status == "skip" for r in today)
    paper = report(state)
    p = paper["overall"]
    counts = (state.get("entry_diagnostics") or {}).get("counts") or {}
    reasons = (state.get("entry_diagnostics") or {}).get("risk_rejected") or {}
    rejection = sum(reasons.values())
    def n(key):
        return int(counts.get(key, 0))
    lines = [
        "📊 <b>訊號與模擬交易｜分開統計</b>",
        f"台灣日期：{day}",
        "",
        "📋 <b>A. 訊號日記（非成交）</b>",
        f"今日已結算：{len(closed)} 筆｜訊號 R 合計：{total_r:+.2f}R",
        f"今日未結算：{pending}｜略過：{unfilled}",
        "說明：包含計畫價追蹤，不能視為實際開倉。",
        "",
        "🧪 <b>B. 模擬倉位（非真實委託）</b>",
        f"累積已平倉：{p['closed_trades']} 筆｜勝率：{str(p['win_rate_pct']) + '%' if p['win_rate_pct'] is not None else '—'}",
        f"估算已實現淨損益：{p['estimated_net_pnl_usdt']:+.4f} USDT",
        f"Profit Factor：{p['profit_factor'] if p['profit_factor'] is not None else '—（無虧損或無交易）'}",
        f"最大已平倉回撤：{p['max_drawdown_usdt']:.4f} USDT",
        f"目前持倉：{html.escape(str((state.get('position') or {}).get('symbol') or '無'))}",
        "",
        "🔎 <b>C. 模擬進場觀察漏斗（累積掃描次數，非獨立訊號）</b>",
        f"候選檢查：{n('retest_open')}",
        f"缺少 K 線：{n('no_candle')}｜K 線未收：{n('unclosed_candle')}",
        f"訊號前 K 線：{n('candle_before_signal')}｜未觸及進場價：{n('entry_not_touched')}",
        f"風控／品質拒絕：{rejection}｜成功開倉：{n('opened')}",
    ]
    if reasons:
        lines.append("拒絕原因：" + "；".join(
            f"{html.escape(str(k))} {v}" for k, v in sorted(reasons.items())))
    if not counts:
        lines.append("尚無累積診斷；新版本執行後開始記錄。")
    lines.append("⚠️ A 為當日訊號、B 為累積模擬成交，統計母體不同，不能直接比較勝率。")
    return "\n".join(lines)


def build_cloud_comparison_message(rows):
    try:
        return build_comparison_message(rows, fetch_snapshot())
    except Exception:
        return "⚠️ 無法讀取模擬倉位雲端快照；為避免誤導，本次不顯示混合統計。"
