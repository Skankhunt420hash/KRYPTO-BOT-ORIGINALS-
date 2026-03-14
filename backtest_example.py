#!/usr/bin/env python3
"""
Einfaches Startskript für einen Beispiel-Backtest.

Standard:
    - CSV:      data/BTC_USDT_1h_test.csv
    - Strategie: trend_continuation (Enhanced-Strategie)

Beispiele:
    python backtest_example.py
    python backtest_example.py --csv data/BTC_USDT_1h_test.csv --strategy momentum_pullback
    python backtest_example.py --csv data/BTC_USDT_1h_test.csv --multi
"""

import argparse

from backtest.cli import run_backtest


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Beispiel-Backtest-Starter für KRYPTO-BOT ORIGINALS",
    )
    parser.add_argument(
        "--csv",
        type=str,
        default="data/BTC_USDT_1h_test.csv",
        help="Pfad zur OHLCV-CSV-Datei (Standard: data/BTC_USDT_1h_test.csv)",
    )
    parser.add_argument(
        "--strategy",
        type=str,
        default="trend_continuation",
        help=(
            "Enhanced-Strategie für den Backtest "
            "(z.B. momentum_pullback, range_reversion, volatility_breakout, trend_continuation). "
            "Ignoriert, wenn --multi gesetzt ist."
        ),
    )
    parser.add_argument(
        "--multi",
        action="store_true",
        help="Multi-Strategie-Backtest mit Meta-Selector (ignoriert --strategy).",
    )
    parser.add_argument(
        "--export",
        type=str,
        default=None,
        help="Optionales Ausgabeverzeichnis für Ergebnisse (CSV + JSON).",
    )
    parser.add_argument(
        "--initial-balance",
        type=float,
        default=10_000.0,
        dest="initial_balance",
        help="Startkapital für Backtest (Standard: 10000 USDT).",
    )
    parser.add_argument(
        "--fee",
        type=float,
        default=0.10,
        help="Gebühr in %% pro Seite (Standard: 0.10).",
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.05,
        help="Slippage in %% (Standard: 0.05).",
    )
    parser.add_argument(
        "--position-size",
        type=float,
        default=2.0,
        dest="position_size",
        help="Positionsgröße in %% des Kapitals pro Trade (Standard: 2.0).",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=40.0,
        dest="min_confidence",
        help="Mindest-Konfidenz 0-100 für Signale im Backtest (Standard: 40).",
    )

    args = parser.parse_args()
    run_backtest(args)


if __name__ == "__main__":
    main()

