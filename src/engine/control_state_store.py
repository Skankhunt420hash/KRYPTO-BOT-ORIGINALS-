"""
Cross-process Control-State über STATE_RECOVERY_FILE.

Der Telegram-Controller und der Trading-Bot laufen in getrennten Prozessen
(runtime_control ist nur prozesslokal). Pause/Risk-Off müssen deshalb über die
Recovery-Datei kommunizieren — analog zum Kill-Switch-File.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config.settings import settings
from src.engine.runtime_control import runtime_control
from src.utils.logger import setup_logger

logger = setup_logger("control_state_store")

_lock = threading.Lock()


def recovery_state_path() -> Path:
    return Path(settings.STATE_RECOVERY_FILE)


def _read_payload(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as e:
        logger.warning("Recovery-State lesen fehlgeschlagen (%s): %s", path, e)
        return {}


def _atomic_write(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def persist_runtime_control_to_recovery(
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Schreibt paused/risk_off/preferred_strategy/mode_request aus runtime_control
    in STATE_RECOVERY_FILE (Merge, atomar). Funktioniert auch ohne Bot-Prozess.
    """
    if not bool(getattr(settings, "STATE_RECOVERY_ENABLED", True)):
        return False
    path = recovery_state_path()
    try:
        with _lock:
            payload = _read_payload(path)
            ctrl = runtime_control.get_snapshot()
            payload["mode"] = str(
                payload.get("mode") or getattr(settings, "TRADING_MODE", "paper")
            )
            payload["paused"] = bool(ctrl.get("paused"))
            payload["risk_off"] = bool(ctrl.get("risk_off"))
            payload["preferred_strategy"] = str(ctrl.get("preferred_strategy") or "")
            payload["mode_request"] = str(ctrl.get("mode_request") or "")
            payload["updated_at"] = datetime.now(timezone.utc).isoformat()
            if extra:
                for key, value in extra.items():
                    payload[key] = value
            _atomic_write(path, payload)
        return True
    except Exception as e:
        logger.error("Control-State Persistenz fehlgeschlagen: %s", e)
        return False


def apply_recovery_control_to_runtime() -> bool:
    """
    Liest paused/risk_off aus STATE_RECOVERY_FILE und setzt runtime_control.
    Für den Bot-Zyklus (Cross-Process-Sync mit dem Controller).
    """
    if not bool(getattr(settings, "STATE_RECOVERY_ENABLED", True)):
        return False
    path = recovery_state_path()
    if not path.is_file():
        return False
    try:
        raw = _read_payload(path)
        if not raw:
            return False
        if bool(getattr(settings, "STATE_RECOVERY_RESTORE_PAUSED", True)):
            if raw.get("paused"):
                runtime_control.pause_entries()
            else:
                runtime_control.resume_entries()
        if bool(getattr(settings, "STATE_RECOVERY_RESTORE_RISK_OFF", True)):
            if raw.get("risk_off"):
                runtime_control.enable_risk_off()
            else:
                runtime_control.disable_risk_off()
        preferred = str(raw.get("preferred_strategy") or "").strip()
        if preferred:
            runtime_control.set_preferred_strategy(preferred)
        mode_request = str(raw.get("mode_request") or "").strip()
        if mode_request:
            runtime_control.request_mode(mode_request)
        return True
    except Exception as e:
        logger.warning("Control-State aus Recovery anwenden fehlgeschlagen: %s", e)
        return False
