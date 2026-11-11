"""Read-only Telegram dashboard panels for the paper-only scanner."""
from __future__ import annotations

import html
import json
import urllib.request
from collections import Counter
from datetime import datetime, timezone

from paper_performance import report
from paper_status import fetch_snapshot
from quality_calibration import calibrate

COMMIT_API = "https://api.github.com/repos/yens01/bitunix-scanner/commits/cloud-journal"
SPARKS = "▁▂▃▄▅▆▇█"


def _esc(value):
    return html.escape(str(value))


def _number(value, digits=4):
    return f"{float(value):+.{digits}f}"


def _date(value):
    if not value:
        return "未知"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return "未知"


def latest_signals(rows, limit=5):
    """Show distinct journal observations; never describe them as fills."""
    unique = {}
    for row in rows:
        if row.id:
            unique[row.id] = row
    recent = sorted(unique.values(), key=lambda r: r.signal_time_ms, reverse=True)[:limit]
    lines = ["🎯 <b>最新訊號（非成交）</b>"]
    if not recent:
        return "\n".join(lines + ["目前沒有訊號日記資料。"])
    for row in recent:
        direction = {"long": "做多", "short": "做空"}.get(row.side, "觀察")
        lines.append(
            f"• <b>{_esc(row.symbol)}</b>｜{direction}｜{_esc(row.status)}"
            f"\n  進場 {_esc(row.entry)}｜停損 {_esc(row.stop)}｜停利 {_esc(row.tp)}"
            f"\n  RR {_esc(row.planned_rr)}｜{_esc(row.ts)}"
        )
    lines.append("⚠️ 訊號日記不是實際交易；價格可能已過期。")
    return "\n".join(lines)


def performance(state):
    r = report(state)
    s = r["overall"]
    win = f"{s['win_rate_pct']:.2f}%" if s["win_rate_pct"] is not None else "—"
    pf = str(s["profit_factor"]) if s["profit_factor"] is not None else "—"
    unreal = r["estimated_unrealized_pnl_usdt"]
    return "\n".join([
        "💰 <b>模擬交易績效（非真實成交）</b>",
        f"已平倉：{s['closed_trades']} 筆｜勝率：{win}",
        f"累積已實現淨損益：{_number(s['estimated_net_pnl_usdt'])} USDT",
        f"獲利因子：{pf}",
        f"最大已平倉回撤：{s['max_drawdown_usdt']:.4f} USDT ({s['max_drawdown_pct']:.2f}%)",
        f"估算浮動損益：{_number(unreal) + ' USDT' if unreal is not None else '無有效收盤標記價'}",
        "⚠️ 模擬估算；不含盤中最大浮動回撤。"
    ])


def equity_curve(state):
    r = report(state)
    s = r["overall"]
    capital = float(r["assumptions"]["starting_capital_usdt"])
    values = [capital] + [float(item["equity_usdt"]) for item in s["equity_curve"]]
    lo, hi = min(values), max(values)
    graph = "".join(SPARKS[min(7, int((x - lo) / (hi - lo) * 7))]
                    if hi > lo else SPARKS[0] for x in values[-30:])
    marked = r["estimated_marked_equity_usdt"]
    return "\n".join([
        "📈 <b>模擬資金曲線（已平倉）</b>",
        f"<code>{graph}</code>",
        f"初始：{capital:.4f} USDT｜已平倉後：{s['ending_equity_usdt']:.4f} USDT",
        f"最新估算含浮動：{f'{marked:.4f} USDT' if marked is not None else '無有效收盤標記價'}",
        f"已平倉最大回撤：{s['max_drawdown_usdt']:.4f} USDT",
        f"資料點：{len(values) - 1} 筆已平倉交易（最多顯示最近 30 段）",
        "⚠️ 文字圖不是盤中資金曲線；標記價非可成交報價。"
    ])


def signal_funnel(state):
    diag = state.get("entry_diagnostics") or {}
    unique = diag.get("unique_signals") or {}
    stages = Counter(item.get("stage", "unknown") for item in unique.values())
    counts = diag.get("counts") or {}
    reasons = diag.get("risk_rejected") or {}
    lines = [
        "🔎 <b>訊號漏斗（獨立訊號 ID）</b>",
        f"獨立候選：{len(unique)}｜已開倉：{stages['opened']}｜拒絕：{stages['rejected']}",
        f"未觸價：{stages['entry_not_touched']}｜等待 K 線：{stages['waiting_candle']}",
        f"缺少 K 線：{stages['no_candle']}",
        f"歷次掃描候選檢查：{counts.get('retest_open', 0)}（可重複）",
    ]
    if reasons:
        lines.append("累積拒絕原因（掃描次數）：")
        lines.extend(f"• {_esc(k)}：{v}" for k, v in sorted(reasons.items()))
    if not unique:
        lines.append("尚無獨立訊號診斷紀錄；不代表市場沒有訊號。")
    lines.append("⚠️ 各訊號僅記錄最後觀察階段；不等於歷史完整轉換漏斗。")
    return "\n".join(lines)


def strategy_quality(rows, state):
    data = calibrate(rows, state, min_samples=20)
    labels = {"high": "高", "medium": "中", "low": "低"}
    lines = ["🧠 <b>策略品質（模擬樣本校準）</b>"]
    for tier in ("high", "medium", "low"):
        g = data["groups"][tier]
        if g["eligible_for_interpretation"]:
            detail = (f"觀察勝率 {g['observed_win_rate_pct']:.2f}%"
                      f"｜平均淨損益 {_number(g['observed_mean_net_pnl_usdt'])} U")
        else:
            detail = "樣本不足 20 筆，暫不顯示勝率"
        lines.append(f"• {labels[tier]}品質：{g['samples']} 筆｜{detail}")
    lines.append(f"未匹配訊號的模擬平倉：{data['unmatched_fills']} 筆")
    lines.append("⚠️ 僅歷史觀察，非預測勝率；不自動調整進場門檻。")
    return "\n".join(lines)


def system_status(state, *, cloud_commit=None, now=None):
    now = now or datetime.now(timezone.utc)
    lines = ["⚙️ <b>系統狀態（唯讀）</b>", "執行模式：僅模擬交易（無真實下單）"]
    if cloud_commit:
        stamp = cloud_commit.get("commit", {}).get("committer", {}).get("date")
        lines.append(f"雲端快照提交：{_date(stamp)}")
        try:
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            age = (now - dt).total_seconds() / 60
            lines.append(f"快照延遲：約 {max(0, int(age))} 分鐘" if age >= 0 else "快照時間異常")
            if age > 45:
                lines.append("⚠️ 雲端快照超過 45 分鐘，請檢查排程。")
        except (AttributeError, ValueError, TypeError):
            lines.append("⚠️ 無法驗證快照更新時間。")
    else:
        lines.append("雲端快照更新時間：無法取得")
    lines.extend([
        f"模擬狀態：{'已停止' if state.get('halted') else '未標記停止'}（不代表排程已啟用）",
        f"模擬持倉：{_esc((state.get('position') or {}).get('symbol') or '無')}",
        f"狀態日：{_esc(state.get('day', '未知'))} UTC",
        "⚠️ 無法從快照直接驗證即時 API、排程成功或 Telegram 發送健康度。"
    ])
    return "\n".join(lines)


def fetch_cloud_commit():
    req = urllib.request.Request(COMMIT_API, headers={
        "User-Agent": "bitunix-telegram-dashboard/1.0",
        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=12) as response:
        return json.load(response)


def build_dashboard_message(command, *, rows=None, state=None):
    """Fail closed on cloud errors; never silently show an empty state as healthy."""
    if command == "LATEST":
        if rows is None:
            from signal_tracker import load_journal
            rows = load_journal()
        return latest_signals(rows)
    if state is None:
        try:
            state = fetch_snapshot()
        except Exception:
            return "⚠️ 無法取得雲端模擬交易快照，請稍後再試；本次不顯示可能過期的績效。"
    if command == "PERFORMANCE":
        return performance(state)
    if command == "EQUITY":
        return equity_curve(state)
    if command == "FUNNEL":
        return signal_funnel(state)
    if command == "QUALITY":
        if rows is None:
            from signal_tracker import load_journal
            rows = load_journal()
        return strategy_quality(rows, state)
    if command == "SYSTEM":
        try:
            commit = fetch_cloud_commit()
        except Exception:
            commit = None
        return system_status(state, cloud_commit=commit)
    raise ValueError("unsupported dashboard command")
