from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config.settings import settings
from src.utils.logger import setup_logger

logger = setup_logger("self_reflection_memory")


class SelfReflectionMemory:
    """
    Speichert wiederkehrende Runtime-Probleme (z. B. Ampel-/Pause-Loops)
    und liefert kompakte Selbstreparatur-Vorschläge.
    """

    def __init__(self) -> None:
        self.enabled: bool = bool(getattr(settings, "SELF_REFLECTION_ENABLED", True))
        self._window_minutes: int = int(
            getattr(settings, "SELF_REFLECTION_WINDOW_MINUTES", 240)
        )
        self._max_events: int = int(getattr(settings, "SELF_REFLECTION_MAX_EVENTS", 500))
        self._issue_threshold: int = int(
            getattr(settings, "SELF_REFLECTION_ISSUE_THRESHOLD", 6)
        )
        self._repair_trigger_score: float = float(
            getattr(settings, "SELF_REFLECTION_REPAIR_TRIGGER_SCORE", 0.60)
        )
        self._memory_file = Path(
            str(
                getattr(
                    settings,
                    "SELF_REFLECTION_MEMORY_FILE",
                    "data/self_reflection_memory.json",
                )
            )
        )
        self._events: List[Dict] = []
        self._last_loaded_at: float = 0.0
        self._latest_insight: Dict = {
            "pattern": "init",
            "severity_score": 0.0,
            "repair_actions": [],
            "reason": "init",
            "updated_at": int(self._now_ts()),
        }
        self._load()

    @staticmethod
    def _now_ts() -> float:
        return time.time()

    def _load(self) -> None:
        if not self.enabled:
            return
        try:
            if not self._memory_file.exists():
                return
            raw = json.loads(self._memory_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                events = raw.get("events") or []
            elif isinstance(raw, list):
                events = raw
            else:
                events = []
            self._events = [e for e in events if isinstance(e, dict)]
            self._prune()
            self._last_loaded_at = self._now_ts()
        except Exception as e:
            logger.warning(f"SelfReflection Memory load fehlgeschlagen: {e}")
            self._events = []

    def _save(self) -> None:
        if not self.enabled:
            return
        try:
            self._memory_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": int(self._now_ts()),
                "window_minutes": int(self._window_minutes),
                "events": self._events[-self._max_events :],
            }
            self._memory_file.write_text(
                json.dumps(payload, ensure_ascii=True, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"SelfReflection Memory save fehlgeschlagen: {e}")

    def _prune(self) -> None:
        if not self._events:
            return
        cutoff = self._now_ts() - float(self._window_minutes * 60)
        self._events = [
            e for e in self._events if float(e.get("ts", 0.0) or 0.0) >= cutoff
        ][-self._max_events :]

    def remember(
        self,
        *,
        event_type: str,
        severity: str,
        details: Optional[Dict] = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            event = {
                "ts": self._now_ts(),
                "event_type": str(event_type or "unknown"),
                "severity": str(severity or "info"),
                "details": dict(details or {}),
            }
            self._events.append(event)
            self._prune()
            self._save()
        except Exception as e:
            logger.warning(f"SelfReflection remember fehlgeschlagen: {e}")

    def count_recent(self, event_type: str, within_minutes: int = 240) -> int:
        if not self.enabled:
            return 0
        cutoff = self._now_ts() - float(max(1, within_minutes) * 60)
        key = str(event_type or "").strip()
        return sum(
            1
            for e in self._events
            if str(e.get("event_type", "")).strip() == key
            and float(e.get("ts", 0.0) or 0.0) >= cutoff
        )

    def evaluate_ampel_problem_pattern(
        self,
        *,
        paused: bool,
        risk_off: bool,
        stale_count: int,
        last_gate_reason: str,
    ) -> Tuple[bool, str, Dict]:
        """
        Ermittelt, ob ein wiederkehrendes Ampel-Problem vorliegt.
        """
        if not self.enabled:
            return False, "disabled", {}
        gate = str(last_gate_reason or "").upper()
        ampel_problem = bool(paused or risk_off or stale_count > 0 or "CONTROL PAUSE" in gate)
        if not ampel_problem:
            return False, "no_ampel_problem", {}

        self.remember(
            event_type="ampel_problem",
            severity="warning",
            details={
                "paused": bool(paused),
                "risk_off": bool(risk_off),
                "stale_count": int(stale_count),
                "last_gate_reason": str(last_gate_reason or ""),
            },
        )
        recent = self.count_recent("ampel_problem", within_minutes=360)
        min_events = int(getattr(settings, "SELF_REFLECTION_AMPEL_MIN_EVENTS", 4))
        should_repair = recent >= min_events
        reason = (
            f"ampel_problem_pattern:{recent}/{min_events}"
            if should_repair
            else f"ampel_problem_seen:{recent}/{min_events}"
        )
        return should_repair, reason, {"recent_ampel_problem_events": recent}

    @staticmethod
    def _severity_weight(label: str) -> float:
        lv = str(label or "").strip().lower()
        if lv == "critical":
            return 1.0
        if lv == "error":
            return 0.75
        if lv == "warning":
            return 0.5
        return 0.25

    def observe(self, context: Dict) -> Dict:
        """
        Beobachtet Runtime-Kontext und erzeugt Reflexions-Insights.
        """
        if not self.enabled:
            self._latest_insight = {
                "pattern": "disabled",
                "severity_score": 0.0,
                "repair_actions": [],
                "reason": "disabled",
                "updated_at": int(self._now_ts()),
            }
            return dict(self._latest_insight)

        paused = bool(context.get("paused", False))
        risk_off = bool(context.get("risk_off", False))
        stale = int(context.get("stale_symbols", 0) or 0)
        gate_last = str(context.get("gate_last_reason", "n/a"))
        master_reason = str(context.get("master_reason", "n/a"))
        open_pos = int(context.get("open_positions", 0) or 0)
        max_pos = int(context.get("max_open_positions", 0) or 0)
        entries_today = int(context.get("entries_today", 0) or 0)
        target_per_day = int(context.get("target_trades_per_day", 0) or 0)
        master_enabled = bool(context.get("master_enabled", True))
        master_auto_paused = bool(context.get("master_auto_paused", False))

        if paused:
            self.remember(
                event_type="paused_entries",
                severity="warning",
                details={"gate_last_reason": gate_last},
            )
        if risk_off:
            self.remember(
                event_type="risk_off_active",
                severity="warning",
                details={"gate_last_reason": gate_last},
            )
        if stale > 0:
            self.remember(
                event_type="stale_market_data",
                severity="error",
                details={"stale_symbols": stale},
            )
        if "CONTROL PAUSE" in gate_last.upper():
            self.remember(
                event_type="control_pause_gate",
                severity="warning",
                details={"gate_last_reason": gate_last},
            )

        should_repair_ampel, ampel_reason, ampel_meta = self.evaluate_ampel_problem_pattern(
            paused=paused,
            risk_off=risk_off,
            stale_count=stale,
            last_gate_reason=gate_last,
        )

        recent_ampel = int(ampel_meta.get("recent_ampel_problem_events", 0) or 0)
        recent_paused = self.count_recent("paused_entries", 360)
        recent_risk_off = self.count_recent("risk_off_active", 360)
        recent_stale = self.count_recent("stale_market_data", 360)
        recent_control_pause = self.count_recent("control_pause_gate", 360)

        weighted = (
            recent_ampel * self._severity_weight("warning")
            + recent_paused * self._severity_weight("warning")
            + recent_risk_off * self._severity_weight("warning")
            + recent_control_pause * self._severity_weight("warning")
            + recent_stale * self._severity_weight("error")
        )
        severity_score = max(
            0.0,
            min(1.0, weighted / max(1.0, float(self._issue_threshold))),
        )

        repair_actions: List[str] = []
        if (should_repair_ampel or severity_score >= self._repair_trigger_score) and (
            "DAILY LOSS" not in gate_last.upper()
        ):
            if paused or risk_off:
                repair_actions.append("unlock_entries")
            if risk_off:
                repair_actions.append("clear_noncritical_risk_off")
        if (
            master_enabled
            and (master_auto_paused or "UNDER_TARGET_WINRATE" in master_reason.upper())
            and severity_score >= (self._repair_trigger_score * 0.75)
        ):
            repair_actions.append("reduce_master_strictness")
        if max_pos > 0 and open_pos >= max_pos and target_per_day > 0 and entries_today < target_per_day:
            repair_actions.append("increase_max_positions")

        if recent_stale >= 3 and stale > 0:
            pattern = "ampel_red_loop_stale_data"
        elif recent_ampel >= 4:
            pattern = "ampel_red_loop_control_pause"
        elif max_pos > 0 and open_pos >= max_pos and entries_today < target_per_day:
            pattern = "entry_capacity_lock"
        else:
            pattern = "normal_or_transient"

        self._latest_insight = {
            "pattern": pattern,
            "severity_score": round(severity_score, 3),
            "repair_actions": sorted(set(repair_actions)),
            "reason": ampel_reason,
            "recent_counts": {
                "ampel_problem": recent_ampel,
                "paused_entries": recent_paused,
                "risk_off_active": recent_risk_off,
                "stale_market_data": recent_stale,
                "control_pause_gate": recent_control_pause,
            },
            "updated_at": int(self._now_ts()),
        }
        return dict(self._latest_insight)

    def latest_insight(self) -> Dict:
        return dict(self._latest_insight)

    def snapshot(self) -> Dict:
        if not self.enabled:
            return {"enabled": False}
        self._prune()
        return {
            "enabled": True,
            "window_minutes": int(self._window_minutes),
            "event_count": len(self._events),
            "recent_ampel_problem_events_6h": self.count_recent("ampel_problem", 360),
            "recent_self_repairs_24h": self.count_recent("self_repair", 1440),
            "memory_file": str(self._memory_file),
            "latest_insight": self.latest_insight(),
        }
