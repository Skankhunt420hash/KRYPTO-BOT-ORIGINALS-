"""
Reinforcement Learning Signal Weighter

Verwendet Q-Learning um zu lernen welche Kombinationen aus
(Strategie, Regime, Side, Confidence-Bucket) profitabel sind.

Wie es funktioniert:
  - State:  (strategy_name, regime, side, confidence_bucket, market_bias)
  - Action: "execute" oder "skip" (wird als Gewichtungs-Multiplikator genutzt)
  - Reward: normalisierter PnL nach Trade-Abschluss

Nach jedem Trade-Abschluss wird die Q-Table aktualisiert.
Beim nächsten gleichartigen Signal entscheidet der RL-Agent
ob und mit welchem Gewicht das Signal ausgeführt wird.

Q-Learning Update:
  Q(s, a) ← Q(s, a) + α × [R + γ × max Q(s', a') − Q(s, a)]

  α = Learning Rate   (wie schnell neue Erfahrungen einfließen)
  γ = Discount Factor (wie wichtig sind zukünftige Belohnungen)
  R = Reward          (normalisierter Trade-PnL)

Persistenz: Q-Table wird in SQLite gespeichert (bestehendes DB-System).
Bei Neustart: Vorwissen wird geladen, kein Reset.

Conservative Design:
  - Bei zu wenig Erfahrung (< MIN_SAMPLES): neutraler Score 1.0
  - Score-Range: 0.5 bis 1.5 (moderate Auf-/Abwertung, kein Kill-Switch)
  - Exploration Rate sinkt mit Erfahrung (Epsilon-Greedy)
"""

import json
import math
import sqlite3
import time
from collections import defaultdict
from typing import Any, Dict, Optional, Tuple

from config.settings import settings
from src.storage.database import get_db_path
from src.utils.logger import setup_logger

logger = setup_logger("rl_weighter")

# ─────────────────────────────────────────────────────────────────────────────
# Hyper-Parameter
# ─────────────────────────────────────────────────────────────────────────────
_ALPHA: float = 0.25        # Learning Rate (erhöht: lernt schneller aus Fehlern)
_GAMMA: float = 0.85        # Discount Factor
_EPSILON_START: float = 0.2 # Exploration (geringer Start: direkt selektiver)
_EPSILON_MIN: float = 0.03
_MIN_SAMPLES: int = 5        # Mindest-Trades bevor Score aktiv wird (früher aktiv)
_SCORE_MIN: float = 0.4      # Stärkere Strafe für Verlust-Combos
_SCORE_MAX: float = 1.6      # Stärkerer Bonus für Gewinn-Combos
_NEUTRAL_SCORE: float = 1.0
_HARD_BLOCK_THRESHOLD: float = 0.45   # Score unter diesem Wert → Trade blockiert


def _confidence_bucket(confidence: float) -> str:
    """Teilt Konfidenz in 3 grobe Klassen ein."""
    if confidence < 55:
        return "low"
    elif confidence < 72:
        return "mid"
    else:
        return "high"


def _normalize_reward(pnl_pct: float) -> float:
    """
    Normalisiert PnL in Reward [-1, +1].
    +1 bei >= 2% Gewinn, -1 bei >= 2% Verlust.
    """
    return max(-1.0, min(1.0, pnl_pct / 2.0))


class RLSignalWeighter:
    """
    Q-Learning Agent der Signal-Gewichte adaptiv anpasst.

    Verwendung:
        weighter = RLSignalWeighter()

        # Vor Trade-Ausführung:
        score = weighter.get_score(state)       # 0.5 – 1.5
        trade_id = weighter.begin_trade(state)  # Startet Episode

        # Nach Trade-Abschluss:
        weighter.record_outcome(trade_id, pnl_pct)
    """

    def __init__(self) -> None:
        # Q-Table: state_key → {"execute": float, "skip": float}
        self._q: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"execute": 0.0, "skip": 0.0}
        )
        # Anzahl Erfahrungen pro State
        self._counts: Dict[str, int] = defaultdict(int)
        # Laufende Episoden: trade_id → (state_key, start_time)
        self._active: Dict[str, Tuple[str, float]] = {}

        self._total_trades: int = 0

        self._init_db()
        self._load()

        logger.info(
            f"[cyan]RL-Weighter geladen[/cyan] | "
            f"States: {len(self._q)} | "
            f"Trades gelernt: {self._total_trades} | "
            f"α={_ALPHA} γ={_GAMMA} ε_min={_EPSILON_MIN}"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def get_score(
        self,
        strategy: str,
        regime: str,
        side: str,
        confidence: float,
        market_bias: str = "neutral",
    ) -> float:
        """
        Gibt Score-Multiplikator zurück [0.5, 1.5].
        1.0 = neutral (unbekannt oder zu wenig Daten).
        > 1.0 = gute historische Performance → Signal aufwerten.
        < 1.0 = schlechte Performance → Signal abwerten.
        """
        if not settings.RL_ENABLED:
            return _NEUTRAL_SCORE

        key = self._make_key(strategy, regime, side, confidence, market_bias)
        n = self._counts[key]

        if n < _MIN_SAMPLES:
            return _NEUTRAL_SCORE

        q_exec = self._q[key]["execute"]
        q_skip = self._q[key]["skip"]

        # Q-Wert in Score umrechnen
        # Positiver Q(execute) → Score > 1, negativer → Score < 1
        q_diff = q_exec - q_skip
        # Stärkere Normierung (war 0.3, jetzt 0.4 für schärfere Reaktion)
        score = _NEUTRAL_SCORE + q_diff * 0.4
        score = max(_SCORE_MIN, min(_SCORE_MAX, score))

        logger.debug(
            f"[RL] {strategy}/{regime}/{side} | "
            f"n={n} Q_exec={q_exec:.3f} Q_skip={q_skip:.3f} → score={score:.3f}"
        )
        return score

    def begin_trade(
        self,
        trade_id: str,
        strategy: str,
        regime: str,
        side: str,
        confidence: float,
        market_bias: str = "neutral",
    ) -> None:
        """Startet eine neue Episode (Trade eröffnet)."""
        key = self._make_key(strategy, regime, side, confidence, market_bias)
        self._active[trade_id] = (key, time.time())

    def record_outcome(self, trade_id: str, pnl_pct: float) -> None:
        """
        Aktualisiert Q-Table nach Trade-Abschluss.
        pnl_pct: tatsächlicher PnL in Prozent.
        """
        if trade_id not in self._active:
            return

        key, _ = self._active.pop(trade_id)
        reward = _normalize_reward(pnl_pct)

        # Q-Learning Update: Q(s, "execute") ← Q + α × [R + γ × max_Q − Q]
        q_old = self._q[key]["execute"]
        max_next_q = max(self._q[key].values())
        q_new = q_old + _ALPHA * (reward + _GAMMA * max_next_q - q_old)
        self._q[key]["execute"] = round(q_new, 6)

        # Q(skip) leicht in Richtung -reward ziehen
        q_skip_old = self._q[key]["skip"]
        self._q[key]["skip"] = round(
            q_skip_old + _ALPHA * (-reward - q_skip_old), 6
        )

        self._counts[key] += 1
        self._total_trades += 1

        # Alle 20 Trades in DB speichern
        if self._total_trades % 20 == 0:
            self._save()

        logger.debug(
            f"[RL] Outcome: {key} | "
            f"PnL={pnl_pct:+.2f}% → R={reward:+.2f} | "
            f"Q: {q_old:.3f} → {q_new:.3f}"
        )

    def is_blocked(
        self,
        strategy: str,
        regime: str,
        side: str,
        confidence: float,
        market_bias: str = "neutral",
    ) -> tuple:
        """
        Gibt (blocked, reason) zurück.
        Blockiert wenn Q_execute stark negativ (zu oft verloren) + genug Daten.
        """
        key = self._make_key(strategy, regime, side, confidence, market_bias)
        n = self._counts[key]
        if n < _MIN_SAMPLES * 2:
            return False, ""

        q_exec = self._q[key]["execute"]
        # Q_execute < -0.35 bedeutet: im Schnitt ~35% schlechter als neutral
        # entspricht ungefähr einer Win-Rate unter 35%
        if q_exec < -0.35:
            estimated_wr = max(0.0, 0.5 + q_exec * 0.5)
            return True, (
                f"RL-BLOCK: {strategy}/{regime}/{side} "
                f"Q={q_exec:.3f} (est. WR≈{estimated_wr:.0%}, {n} Trades)"
            )

        score = self.get_score(strategy, regime, side, confidence, market_bias)
        if score < _HARD_BLOCK_THRESHOLD:
            return True, (
                f"RL-BLOCK: {strategy}/{regime}/{side} Score={score:.2f} "
                f"< {_HARD_BLOCK_THRESHOLD} ({n} Trades)"
            )
        return False, ""

    def get_top_states(self, n: int = 10) -> list:
        """Gibt die n besten gelernten States zurück (für Logging/Status)."""
        ranked = sorted(
            [(k, self._q[k]["execute"], self._counts[k]) for k in self._q],
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked[:n]

    def get_stats(self) -> dict:
        return {
            "total_states": len(self._q),
            "total_trades_learned": self._total_trades,
            "active_episodes": len(self._active),
            "top_states": self.get_top_states(5),
        }

    # ── Interne Methoden ──────────────────────────────────────────────────────

    @staticmethod
    def _make_key(
        strategy: str,
        regime: str,
        side: str,
        confidence: float,
        market_bias: str,
    ) -> str:
        cb = _confidence_bucket(confidence)
        return f"{strategy}|{regime}|{side}|{cb}|{market_bias}"

    def _init_db(self) -> None:
        """Erstellt RL-Tabelle in der bestehenden SQLite-DB."""
        try:
            conn = sqlite3.connect(get_db_path())
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rl_qtable (
                    state_key  TEXT PRIMARY KEY,
                    q_execute  REAL NOT NULL DEFAULT 0.0,
                    q_skip     REAL NOT NULL DEFAULT 0.0,
                    n_samples  INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"RL DB init fehlgeschlagen: {e}")

    def _save(self) -> None:
        """Persistiert Q-Table in SQLite."""
        try:
            conn = sqlite3.connect(get_db_path())
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            for key, vals in self._q.items():
                n = self._counts[key]
                conn.execute(
                    """INSERT OR REPLACE INTO rl_qtable
                       (state_key, q_execute, q_skip, n_samples, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (key, vals["execute"], vals["skip"], n, now),
                )
            conn.commit()
            conn.close()
            logger.debug(f"RL Q-Table gespeichert: {len(self._q)} States")
        except Exception as e:
            logger.warning(f"RL speichern fehlgeschlagen: {e}")

    def _load(self) -> None:
        """Lädt Q-Table aus SQLite."""
        try:
            conn = sqlite3.connect(get_db_path())
            rows = conn.execute(
                "SELECT state_key, q_execute, q_skip, n_samples FROM rl_qtable"
            ).fetchall()
            conn.close()
            for key, q_exec, q_skip, n in rows:
                self._q[key] = {"execute": q_exec, "skip": q_skip}
                self._counts[key] = n
                self._total_trades += n
            if rows:
                logger.info(f"RL Q-Table geladen: {len(rows)} States")
        except Exception as e:
            logger.debug(f"RL laden (erwartet bei erster Ausführung): {e}")
