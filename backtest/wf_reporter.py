"""
Walk-Forward Reporter

Konsolenausgabe (Rich) und optionaler CSV/JSON-Export für
Walk-Forward-Evaluationsergebnisse.
"""

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from backtest.walk_forward import WalkForwardResult, SplitResult, WalkForwardSummary

console = Console()

_TS_FMT = "%Y-%m-%d"   # Datumsformat in Tabellen


def print_wf_report(result: WalkForwardResult) -> None:
    """Gibt den vollständigen Walk-Forward-Report auf der Konsole aus."""

    cfg = result.wf_config
    bt = result.bt_config
    s = result.summary

    # ── Header ────────────────────────────────────────────────────────────
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]WALK-FORWARD EVALUATION[/bold cyan]\n"
        f"[dim]{result.mode_label} | {bt.symbol} | {bt.timeframe} | "
        f"IS={cfg.is_candles} OOS={cfg.oos_candles} Kerzen | "
        f"Modus={cfg.mode} | Schritt={cfg.step_candles}[/dim]",
        border_style="cyan",
    ))

    # ── Split-Übersicht ───────────────────────────────────────────────────
    split_tbl = Table(
        title=f"📅 IS/OOS Split-Übersicht ({s.n_splits} Splits)",
        box=box.ROUNDED,
        border_style="blue",
    )
    split_tbl.add_column("#", justify="right", style="dim")
    split_tbl.add_column("IS Zeitraum", min_width=22)
    split_tbl.add_column("IS Trades", justify="right")
    split_tbl.add_column("IS Win-%", justify="right")
    split_tbl.add_column("IS PnL%", justify="right")
    split_tbl.add_column("OOS Zeitraum", min_width=22)
    split_tbl.add_column("OOS Trades", justify="right")
    split_tbl.add_column("OOS Win-%", justify="right")
    split_tbl.add_column("OOS PnL%", justify="right")
    split_tbl.add_column("OOS ✓?", justify="center")

    for sp in result.splits:
        is_s = sp.is_stats
        oos_s = sp.oos_stats
        is_period = f"{_fmt_ts(sp.is_start)} → {_fmt_ts(sp.is_end)}"
        oos_period = f"{_fmt_ts(sp.oos_start)} → {_fmt_ts(sp.oos_end)}"
        oos_ok = "[green]✓[/green]" if sp.oos_profitable else "[red]✗[/red]"
        is_pnl_c = "green" if is_s.total_pnl_pct >= 0 else "red"
        oos_pnl_c = "green" if oos_s.total_pnl_pct >= 0 else "red"

        split_tbl.add_row(
            str(sp.split_idx),
            is_period,
            str(is_s.n_trades),
            f"{is_s.winrate_pct:.1f}%",
            f"[{is_pnl_c}]{is_s.total_pnl_pct:+.2f}%[/{is_pnl_c}]",
            oos_period,
            str(oos_s.n_trades),
            f"{oos_s.winrate_pct:.1f}%",
            f"[{oos_pnl_c}]{oos_s.total_pnl_pct:+.2f}%[/{oos_pnl_c}]",
            oos_ok,
        )

    console.print(split_tbl)

    # ── OOS-Kennzahlen pro Split (detailliert) ────────────────────────────
    detail_tbl = Table(
        title="📊 OOS-Kennzahlen pro Split",
        box=box.ROUNDED,
        border_style="cyan",
    )
    detail_tbl.add_column("#", justify="right", style="dim")
    detail_tbl.add_column("OOS Trades", justify="right")
    detail_tbl.add_column("Win-%", justify="right")
    detail_tbl.add_column("PnL (USDT)", justify="right", min_width=12)
    detail_tbl.add_column("PF", justify="right")
    detail_tbl.add_column("Max DD%", justify="right")
    detail_tbl.add_column("LONG/SHORT", justify="right")
    detail_tbl.add_column("IS→OOS PnL", justify="right")

    for sp in result.splits:
        oos_s = sp.oos_stats
        pnl_c = "green" if oos_s.total_pnl_abs >= 0 else "red"
        dd_c = "red" if oos_s.max_drawdown_pct > 20 else "yellow" if oos_s.max_drawdown_pct > 10 else "green"
        pf_str = _fmt_pf(oos_s.profit_factor)
        deg = sp.pnl_degradation
        deg_str = f"{deg:+.2f}x" if deg != 0.0 else "—"
        deg_c = "green" if deg >= 0.5 else "yellow" if deg >= 0 else "red"

        long_n = oos_s.long_stats.n_trades if oos_s.long_stats else 0
        short_n = oos_s.short_stats.n_trades if oos_s.short_stats else 0
        ls_str = f"{long_n}L / {short_n}S"

        detail_tbl.add_row(
            str(sp.split_idx),
            str(oos_s.n_trades),
            f"{oos_s.winrate_pct:.1f}%",
            f"[{pnl_c}]{oos_s.total_pnl_abs:+,.4f}[/{pnl_c}]",
            pf_str,
            f"[{dd_c}]{oos_s.max_drawdown_pct:.1f}%[/{dd_c}]",
            ls_str,
            f"[{deg_c}]{deg_str}[/{deg_c}]",
        )

    console.print(detail_tbl)

    # ── Gesamtzusammenfassung ─────────────────────────────────────────────
    pnl_c = "green" if s.oos_avg_pnl_pct >= 0 else "red"
    cons_c = "green" if s.consistency_score >= 0.60 else "yellow" if s.consistency_score >= 0.40 else "red"
    deg_c = "green" if s.pnl_degradation_ratio >= 0.50 else "yellow" if s.pnl_degradation_ratio >= 0 else "red"
    wr_deg_c = "green" if s.winrate_degradation_ratio >= 0.70 else "yellow" if s.winrate_degradation_ratio >= 0.50 else "red"

    sum_tbl = Table(
        title="🏁 Gesamtzusammenfassung (über alle validen Splits)",
        box=box.ROUNDED,
        border_style="cyan",
    )
    sum_tbl.add_column("Kennzahl", style="bold", min_width=30)
    sum_tbl.add_column("Wert", justify="right", min_width=18)

    sum_tbl.add_row("Splits gesamt", str(s.n_splits))
    sum_tbl.add_row(
        f"Valide Splits (≥{result.wf_config.min_trades_per_split} OOS-Trades)",
        str(s.n_valid_splits),
    )
    sum_tbl.add_row(
        "Profitable OOS-Splits",
        f"[{cons_c}]{s.n_profitable_oos}/{s.n_valid_splits}[/{cons_c}]",
    )
    sum_tbl.add_row("", "")
    sum_tbl.add_row(
        "Ø OOS-PnL %",
        f"[{pnl_c}]{s.oos_avg_pnl_pct:+.3f}%[/{pnl_c}]",
    )
    sum_tbl.add_row(
        "Median OOS-PnL %",
        f"[{pnl_c}]{s.oos_median_pnl_pct:+.3f}%[/{pnl_c}]",
    )
    sum_tbl.add_row("Ø OOS-Win-Rate", f"{s.oos_avg_winrate:.1f}%")
    sum_tbl.add_row("Ø OOS-Profit-Factor", _fmt_pf(s.oos_avg_profit_factor))
    sum_tbl.add_row("Ø OOS-Max-Drawdown", f"{s.oos_avg_max_drawdown:.1f}%")
    sum_tbl.add_row("OOS-Trades gesamt", str(s.oos_total_trades))
    sum_tbl.add_row("", "")
    sum_tbl.add_row("Ø IS-PnL % (Referenz)", f"{s.is_avg_pnl_pct:+.3f}%")
    sum_tbl.add_row("Ø IS-Win-Rate (Referenz)", f"{s.is_avg_winrate:.1f}%")
    sum_tbl.add_row("", "")
    sum_tbl.add_row(
        "Konsistenz-Score (OOS profitabel)",
        f"[{cons_c}]{s.consistency_score:.0%}[/{cons_c}]",
    )
    sum_tbl.add_row(
        "PnL-Degradation IS→OOS",
        f"[{deg_c}]{s.pnl_degradation_ratio:.2f}x[/{deg_c}]  "
        f"[dim](1.0 = keine Degradation)[/dim]",
    )
    sum_tbl.add_row(
        "Win-Rate-Degradation IS→OOS",
        f"[{wr_deg_c}]{s.winrate_degradation_ratio:.2f}x[/{wr_deg_c}]",
    )
    console.print(sum_tbl)

    # ── Overfitting-Fazit ─────────────────────────────────────────────────
    level_color = {
        "gering": "green", "mäßig": "yellow", "hoch": "red"
    }.get(s.overfitting_level, "white")

    console.print(Panel(
        f"[bold {level_color}]Overfitting-Risiko: {s.overfitting_level.upper()}[/bold {level_color}]\n"
        f"{s.overfitting_explanation}\n\n"
        f"[dim]Hinweis: Diese Heuristiken sind Indikatoren, keine Garantien. "
        f"Wenige Splits oder Trades pro Split reduzieren die statistische Aussagekraft.[/dim]",
        title="🔍 Overfitting-Assessment",
        border_style=level_color,
    ))
    console.print()


# ── Export-Funktionen ──────────────────────────────────────────────────────


def export_wf_splits_csv(result: WalkForwardResult, output_path: str) -> None:
    """Exportiert alle Split-Ergebnisse als CSV."""
    _ensure_dir(output_path)

    rows = []
    for sp in result.splits:
        for kind, st in [("IS", sp.is_stats), ("OOS", sp.oos_stats)]:
            rows.append({
                "split": sp.split_idx,
                "type": kind,
                "start": _fmt_ts(sp.is_start if kind == "IS" else sp.oos_start),
                "end": _fmt_ts(sp.is_end if kind == "IS" else sp.oos_end),
                "n_trades": st.n_trades,
                "winrate_pct": st.winrate_pct,
                "pnl_abs": st.total_pnl_abs,
                "pnl_pct": st.total_pnl_pct,
                "profit_factor": st.profit_factor if st.profit_factor != float("inf") else 999.0,
                "max_drawdown_pct": st.max_drawdown_pct,
                "sharpe_ratio": st.sharpe_ratio,
                "pnl_degradation": sp.pnl_degradation if kind == "OOS" else "",
            })

    fields = list(rows[0].keys()) if rows else []
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    console.print(f"[green]✓ WFO-Splits CSV:[/green] {output_path}  ({len(result.splits)} Splits)")


def export_wf_summary_json(result: WalkForwardResult, output_path: str) -> None:
    """Exportiert die Zusammenfassung als JSON."""
    _ensure_dir(output_path)

    import dataclasses

    def _ser(obj):
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {k: _ser(v) for k, v in dataclasses.asdict(obj).items()}
        if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
            return None
        return obj

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": result.mode_label,
        "wf_config": _ser(result.wf_config),
        "bt_config": _ser(result.bt_config),
        "summary": _ser(result.summary),
        "splits": [
            {
                "split": sp.split_idx,
                "is_profitable": sp.is_profitable,
                "oos_profitable": sp.oos_profitable,
                "pnl_degradation": sp.pnl_degradation,
                "winrate_degradation": sp.winrate_degradation,
                "is_n_trades": sp.is_stats.n_trades,
                "oos_n_trades": sp.oos_stats.n_trades,
                "is_pnl_pct": sp.is_stats.total_pnl_pct,
                "oos_pnl_pct": sp.oos_stats.total_pnl_pct,
                "is_winrate": sp.is_stats.winrate_pct,
                "oos_winrate": sp.oos_stats.winrate_pct,
            }
            for sp in result.splits
        ],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    console.print(f"[green]✓ WFO-Summary JSON:[/green] {output_path}")


# ── Hilfsfunktionen ────────────────────────────────────────────────────────


def _fmt_ts(ts) -> str:
    try:
        return ts.strftime(_TS_FMT)
    except Exception:
        return str(ts)[:10]


def _fmt_pf(pf: float) -> str:
    if pf == float("inf") or pf > 99:
        return "∞"
    return f"{pf:.2f}"


def _ensure_dir(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
