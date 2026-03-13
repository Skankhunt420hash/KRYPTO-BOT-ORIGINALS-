"""
OHLCV Data Loader für Backtesting.

Unterstützt CSV-Dateien mit flexiblen Spaltennamen.
Pflichtfelder: Zeitstempel + open, high, low, close, volume.

Validierungen:
- Fehlende Pflicht-Spalten
- Nicht-numerische OHLCV-Werte
- NaN-Zeilen (werden gewarnt und entfernt)
- Zeitstempel-Parsing-Fehler
- Unsortierte Zeitstempel
- Doppelte Zeitstempel
- Unplausible Kerzen (high < low, close <= 0)
"""

from pathlib import Path
from typing import Optional

import pandas as pd

from src.utils.logger import setup_logger

logger = setup_logger("backtest.loader")

# Akzeptierte Spaltennamen für den Zeitstempel (Priorität: oben = zuerst)
_TS_ALIASES = [
    "timestamp", "Timestamp", "datetime", "Datetime",
    "date", "Date", "time", "Time", "open_time",
]

# Akzeptierte Spaltennamen für OHLCV (case-insensitive)
_OHLCV_ALIASES = {
    "open":   ["open", "o"],
    "high":   ["high", "h"],
    "low":    ["low", "l"],
    "close":  ["close", "c"],
    "volume": ["volume", "vol", "v", "qty"],
}


def load_csv(path: str, symbol: str = "UNKNOWN") -> pd.DataFrame:
    """
    Lädt eine OHLCV-CSV-Datei und gibt ein validiertes DataFrame zurück.

    Rückgabe:
        pd.DataFrame mit DatetimeIndex (UTC) und Spalten:
        open, high, low, close, volume  (alle float64)

    Raises:
        FileNotFoundError: Datei existiert nicht
        ValueError: Daten fehlerhaft oder unvollständig
    """
    fpath = Path(path)
    if not fpath.exists():
        raise FileNotFoundError(f"CSV nicht gefunden: {fpath.resolve()}")

    logger.info(f"Lade CSV: {fpath.name} ({fpath.stat().st_size / 1024:.1f} KB)")

    try:
        raw = pd.read_csv(fpath)
    except Exception as e:
        raise ValueError(f"CSV konnte nicht gelesen werden: {e}")

    if raw.empty:
        raise ValueError(f"CSV-Datei ist leer: {fpath}")

    # ── Zeitstempel-Spalte finden ─────────────────────────────────────────
    ts_col = _find_ts_column(raw)

    # ── OHLCV-Spalten finden (case-insensitive) ───────────────────────────
    col_map = _find_ohlcv_columns(raw)

    # ── Ergebnis-DataFrame aufbauen ───────────────────────────────────────
    result = pd.DataFrame(index=_parse_timestamps(raw[ts_col], fpath))
    result.index.name = "timestamp"

    for target, src in col_map.items():
        try:
            result[target] = pd.to_numeric(raw[src].values, errors="raise")
        except (TypeError, ValueError):
            raise ValueError(
                f"Spalte '{src}' ({target}) enthält nicht-numerische Werte"
            )

    # ── NaN bereinigen ────────────────────────────────────────────────────
    nan_count = result.isnull().any(axis=1).sum()
    if nan_count > 0:
        logger.warning(f"{nan_count} Zeilen mit NaN-Werten entfernt")
        result = result.dropna()

    if len(result) == 0:
        raise ValueError("Nach NaN-Bereinigung keine Daten übrig")

    # ── Sortierung sicherstellen ──────────────────────────────────────────
    result = result.sort_index()

    # ── Duplikate entfernen ───────────────────────────────────────────────
    dupes = result.index.duplicated().sum()
    if dupes > 0:
        logger.warning(f"{dupes} doppelte Zeitstempel entfernt (keep=first)")
        result = result[~result.index.duplicated(keep="first")]

    # ── Plausibilitätsprüfung ─────────────────────────────────────────────
    bad = (result["high"] < result["low"]) | (result["close"] <= 0) | (result["open"] <= 0)
    if bad.any():
        n_bad = int(bad.sum())
        logger.warning(f"{n_bad} unplausible Kerzen entfernt (high<low oder close<=0)")
        result = result[~bad]

    if len(result) < 10:
        raise ValueError(
            f"Zu wenig valide Kerzen nach Bereinigung: {len(result)}"
        )

    logger.info(
        f"Datensatz bereit: {len(result):,} Kerzen | "
        f"{result.index[0].strftime('%Y-%m-%d')} → "
        f"{result.index[-1].strftime('%Y-%m-%d')} | "
        f"Symbol: {symbol}"
    )
    return result


# ── Hilfsfunktionen ────────────────────────────────────────────────────────


def _find_ts_column(df: pd.DataFrame) -> str:
    """Findet die Zeitstempel-Spalte nach Priorität."""
    for alias in _TS_ALIASES:
        if alias in df.columns:
            return alias

    # Fallback: erste Spalte, falls sie als Datum parsierbar ist
    first = df.columns[0]
    try:
        pd.to_datetime(df[first].iloc[0])
        logger.debug(f"Zeitstempel-Spalte automatisch erkannt: '{first}'")
        return first
    except Exception:
        pass

    raise ValueError(
        f"Keine Zeitstempel-Spalte gefunden.\n"
        f"Akzeptierte Namen: {_TS_ALIASES}\n"
        f"Vorhandene Spalten: {list(df.columns)}"
    )


def _find_ohlcv_columns(df: pd.DataFrame) -> dict:
    """Findet OHLCV-Spalten (case-insensitive)."""
    lower_map = {c.lower(): c for c in df.columns}
    result = {}
    for target, aliases in _OHLCV_ALIASES.items():
        found = None
        for alias in aliases:
            if alias.lower() in lower_map:
                found = lower_map[alias.lower()]
                break
        if found is None:
            raise ValueError(
                f"Pflicht-Spalte '{target}' nicht gefunden.\n"
                f"Akzeptierte Namen: {aliases}\n"
                f"Vorhandene Spalten: {list(df.columns)}"
            )
        result[target] = found
    return result


def _parse_timestamps(ts_series: pd.Series, fpath: Path) -> pd.DatetimeIndex:
    """Parst Zeitstempel-Spalte zu UTC-DatetimeIndex."""
    try:
        idx = pd.to_datetime(ts_series, utc=True)
        return idx
    except Exception:
        pass

    # Fallback: Unix-Millisekunden
    try:
        idx = pd.to_datetime(ts_series.astype(float), unit="ms", utc=True)
        logger.debug("Zeitstempel als Unix-Millisekunden geparst")
        return idx
    except Exception:
        pass

    # Fallback: Unix-Sekunden
    try:
        idx = pd.to_datetime(ts_series.astype(float), unit="s", utc=True)
        logger.debug("Zeitstempel als Unix-Sekunden geparst")
        return idx
    except Exception as e:
        raise ValueError(
            f"Zeitstempel-Spalte konnte nicht geparst werden ({fpath.name}): {e}"
        )
