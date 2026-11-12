"""Separate journal signal outcomes from paper-executed trading performance."""
from __future__ import annotations

import html
from collections import Counter
from datetime import datetime, timedelta, timezone

from paper_status import fetch_snapshot
from paper_performance import report

TW = timezone(timedelta(hours=8))


def build_comparison_message(rows, state, *, now=None):
    """Paper results first; signal observations and scan diagnostics are distinct."""
    now = now or datetime.now(TW)
    day = now.strftime("%Y-%m-%d")
    today = [r for r in rows if (r.ts or "")[:10] == day]
    # De-duplicate by signal ID where available, matching journal semantics.
    by_id = {}
    for i, row in enumerate(today):
        key = getattr(row, "id", None) or f"no-id-{i}"
        by_id[key] = row
    today = list(by_id.values())
    closed = [r for r in today if r.status in ("win", "loss", "timeout")
              and r.side in ("long", "short")]
    total_r = sum(float(r.realized_r or 0) for r in closed)
    pending = sum(r.status == "open" for r in today)
    unfilled = sum(r.status == "skip" for r in today)
    paper = report(state)
    p = paper["overall"]
    diag = state.get("entry_diagnostics") or {}
    counts = diag.get("counts") or {}
    reasons = diag.get("risk_rejected") or {}
    unique = diag.get("unique_signals") or {}
    stages = Counter(item.get("stage", "unknown") for item in unique.values()
                     if isinstance(item, dict))
    def n(key):
        return int(counts.get(key, 0) or 0)
    checks = n("retest_open")
    missing = n("no_candle")
    missing_pct = f"{missing / checks * 100:.1f}%" if checks > 0 else "—"
    lines = [
        "📊 <b>市場總覽｜模擬績效優先</b>",
        f"查詢時間：{now.strftime('%Y-%m-%d %H:%M')}（台灣時間）",
        f"模擬帳本狀態日：{html.escape(str(state.get('day') or '未知'))}（UTC；不是快照更新時間）",
        "",
        "💰 <b>A. 模擬交易績效（累積、非真實成交）</b>",
        f"已平倉：{p['closed_trades']} 筆｜勝率：{str(p['win_rate_pct']) + '%' if p['win_rate_pct'] is not None else '—'}",
        f"估算已實現淨損益：{p['estimated_net_pnl_usdt']:+.4f} USDT",
        f"Profit Factor：{p['profit_factor'] if p['profit_factor'] is not None else '—（無虧損或無交易）'}",
        f"最大已平倉回撤：{p['max_drawdown_usdt']:.4f} USDT",
        f"目前持倉：{html.escape(str((state.get('position') or {}).get('symbol') or '無'))}",
    ]
    if p["closed_trades"] < 20:
        lines.append(f"⚠️ 僅 {p['closed_trades']} 筆模擬平倉，樣本不足 20 筆，暫勿推論策略穩定獲利。")
    if paper["unmatched_close_events"] or paper["duplicate_close_events"] or paper["duplicate_open_events"]:
        lines.append("⚠️ 帳本事件異常："
                     f"無法匹配平倉 {paper['unmatched_close_events']}｜"
                     f"重複開倉 {paper['duplicate_open_events']}｜"
                     f"重複平倉 {paper['duplicate_close_events']}")
    lines += [
        "",
        "📋 <b>B. 今日訊號觀察（非成交、非績效）</b>",
        f"已結算：{len(closed)} 筆｜訊號假設 R 合計：{total_r:+.2f}R",
        f"未結算：{pending}｜略過：{unfilled}",
        "⚠️ 訊號 R 僅追蹤計畫價的假設結果，不代表模擬帳本獲利。",
        "",
        "🔎 <b>C. 模擬進場診斷（累積掃描檢查，可重複）</b>",
        f"候選檢查：{checks} 次｜缺 K 線：{missing} 次（占候選檢查 {missing_pct}）",
        f"K 線未收：{n('unclosed_candle')}｜訊號前 K 線：{n('candle_before_signal')}",
        f"未觸及進場價：{n('entry_not_touched')}｜成功開倉：{n('opened')}",
        f"風控／品質拒絕：{sum(int(v) for v in reasons.values())}",
        "⚠️ 各診斷計數可能重複或跨階段，比例僅供排查，不是獨立訊號失敗率。",
        "",
        "🧭 <b>D. 獨立訊號（最後觀察階段）</b>",
        f"獨立候選：{len(unique)}｜已開倉：{stages['opened']}｜拒絕：{stages['rejected']}",
        f"未觸價：{stages['entry_not_touched']}｜等待 K 線：{stages['waiting_candle']}｜缺 K 線：{stages['no_candle']}",
    ]
    if reasons:
        lines.append("拒絕原因（掃描次數）：" + "；".join(
            f"{html.escape(str(k))} {v}" for k, v in sorted(reasons.items())))
    if not counts:
        lines.append("尚無累積診斷；無法判定掃描資料品質。")
    if checks and missing / checks >= 0.3:
        lines.append("⚠️ 缺 K 線檢查占比偏高（≥30%），建議排查 API、歷史 K 線與訊號時間。")
    lines.append("📌 訊號日記、獨立訊號、累積掃描及模擬成交為不同母體，不能直接比較。")
    lines.append("⚠️ 資料取自雲端快照；查詢時間不代表快照更新時間或即時行情。")
    return "\n".join(lines)

def build_cloud_comparison_message(rows):
    try:
        return build_comparison_message(rows, fetch_snapshot())
    except Exception:
        return "⚠️ 無法讀取模擬倉位雲端快照；為避免誤導，本次不顯示混合統計。"
