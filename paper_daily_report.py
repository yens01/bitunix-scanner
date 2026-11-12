"""Read-only Telegram-ready daily paper performance summary."""
from __future__ import annotations
import argparse
import html
import json
from pathlib import Path
from paper_performance import report


def daily_message(state):
    summary = report(state)
    overall = summary["overall"]
    counts = (state.get("entry_diagnostics") or {}).get("counts") or {}
    reasons = (state.get("entry_diagnostics") or {}).get("risk_rejected") or {}
    lines = [
        "🧪 <b>模擬交易每日檢視</b>",
        f"帳本日期（UTC）：{html.escape(str(state.get('day', '—')))}",
        f"已平倉：{overall['closed_trades']} 筆",
        f"累積淨損益（估算）：{overall['estimated_net_pnl_usdt']:+.4f} USDT",
        f"勝率：{str(overall['win_rate_pct']) + '%' if overall['win_rate_pct'] is not None else '樣本不足'}",
        f"最大回撤：{overall['max_drawdown_usdt']:.4f} USDT",
        f"持倉：{html.escape(str((state.get('position') or {}).get('symbol') or '無'))}",
        f"累積候選掃描：{counts.get('retest_open', 0)}｜累積開倉：{counts.get('opened', 0)}",
        f"未觸價：{counts.get('entry_not_touched', 0)}｜缺 K 線：{counts.get('no_candle', 0)}",
    ]
    if reasons:
        lines.append("拒絕原因：" + "；".join(
            f"{html.escape(str(k))} {v}" for k, v in sorted(reasons.items())))
    lines.append("⚠️ 累積績效非當日績效；訊號日記不等於成交，非投資建議。")
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state", type=Path, required=True)
    args = p.parse_args()
    print(daily_message(json.loads(args.state.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
