"""
Separater Safety-Watchdog: überwacht den Handels-Bot und führt nur definierte,
sichere Reparatur-Schritte aus.

Was er tut:
- Prüft, ob der Bot-Prozess (main.py …) läuft; optional Neustart per Shell-Befehl.
- Liest Bot-Log-Tail: viele ERROR/Traceback-Zeilen → optional Neustart (Cooldown).
- python -m compileall auf src/ + config/ (Syntax-Fehler erkennen, kein Blind-Fix).
- Paper: festgefahrenes runtime_recovery.json (paused/risk_off) optional zurücksetzen.
- Optional: ruff --fix nur wenn SAFETY_WATCHDOG_RUFF_AUTOFIX=true und ruff im PATH.

Was er bewusst NICHT tut:
- Keine beliebigen LLM-Code-Edits, kein „alles umschreiben“.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from config.settings import settings
from src.utils.logger import setup_logger
from src.utils.telegram_notifier import TelegramNotifier

logger = setup_logger("safety_watchdog")

_ERROR_RE = re.compile(
    r"(ERROR|CRITICAL|Traceback|Exception:|Fatal Python error)",
    re.IGNORECASE,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _find_bot_pids() -> List[int]:
    """PIDs von Prozessen, deren cmdline main.py enthält (Handels-Bot)."""
    try:
        import psutil
    except ImportError:
        return []
    out: List[int] = []
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = p.info.get("cmdline") or []
            flat = " ".join(str(x) for x in cmd).lower()
            if "main.py" in flat and "safety_watchdog" not in flat:
                pid = int(p.info["pid"])
                if pid != os.getpid():
                    out.append(pid)
        except (psutil.Error, TypeError, ValueError):
            continue
    return out


def _tail_log(path: Path, max_lines: int) -> List[str]:
    if not path.is_file():
        return []
    max_lines = max(0, int(max_lines))
    if max_lines == 0:
        return []
    try:
        chunks = bytearray()
        lines_seen = 0
        block_size = 8192
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            while pos > 0 and lines_seen <= max_lines:
                read_size = min(block_size, pos)
                pos -= read_size
                fh.seek(pos)
                chunk = fh.read(read_size)
                chunks[:0] = chunk
                lines_seen += chunk.count(b"\n")
        raw = chunks.decode("utf-8", errors="replace").splitlines()
        return raw[-max_lines:]
    except OSError as e:
        logger.warning("Log lesen fehlgeschlagen %s: %s", path, e)
        return []


def _count_error_lines(lines: List[str]) -> int:
    return sum(1 for ln in lines if _ERROR_RE.search(ln))


def _run_compileall(root: Path) -> Tuple[bool, str]:
    if not bool(getattr(settings, "SAFETY_WATCHDOG_RUN_COMPILEALL", True)):
        return True, "compileall übersprungen"
    cmd = [sys.executable, "-m", "compileall", "src", "config", "-q"]
    try:
        r = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
        )
        if r.returncode == 0:
            return True, "compileall OK"
        err = (r.stderr or r.stdout or "")[:800]
        return False, f"compileall fehlgeschlagen rc={r.returncode}: {err}"
    except subprocess.TimeoutExpired:
        return False, "compileall timeout"
    except Exception as e:
        return False, f"compileall: {e}"


def _maybe_ruff_autofix(root: Path) -> Tuple[bool, str]:
    if not bool(getattr(settings, "SAFETY_WATCHDOG_RUFF_AUTOFIX", False)):
        return True, "ruff aus"
    ruff = shutil.which("ruff")
    if not ruff:
        return True, "ruff nicht im PATH"
    cmd = [ruff, "check", "src", "config", "--fix"]
    try:
        r = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=180,
        )
        msg = (r.stdout or r.stderr or "")[:600]
        return True, f"ruff rc={r.returncode} {msg}"
    except Exception as e:
        return False, f"ruff: {e}"


def _clear_stuck_recovery(root: Path) -> Tuple[bool, str]:
    if str(getattr(settings, "TRADING_MODE", "paper")).lower() != "paper":
        return False, "nur paper"
    if not bool(getattr(settings, "SAFETY_WATCHDOG_CLEAR_STUCK_RECOVERY", True)):
        return False, "clear recovery aus"
    rel = getattr(settings, "STATE_RECOVERY_FILE", "data/runtime_recovery.json")
    path = Path(rel)
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        return False, "keine recovery-datei"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"recovery parse: {e}"
    changed = False
    if data.get("paused"):
        data["paused"] = False
        changed = True
    if data.get("risk_off"):
        data["risk_off"] = False
        changed = True
    if not changed:
        return False, "recovery schon frei"
    try:
        path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
        return True, "recovery paused/risk_off zurückgesetzt"
    except OSError as e:
        return False, f"recovery schreiben: {e}"


def _restart_bot(reason: str, tg: Optional[TelegramNotifier], last_mono: float) -> float:
    cmd = (getattr(settings, "SAFETY_WATCHDOG_RESTART_CMD", "") or "").strip()
    now = time.monotonic()
    cooldown = float(getattr(settings, "SAFETY_WATCHDOG_RESTART_COOLDOWN_SEC", 1800))
    if now - last_mono < cooldown:
        logger.info("Neustart übersprungen (Cooldown): %s", reason)
        return last_mono
    if not cmd:
        logger.warning("Neustart gewünscht, aber SAFETY_WATCHDOG_RESTART_CMD leer: %s", reason)
        if tg and tg.enabled:
            tg.notify_error("SAFETY_WATCHDOG", f"Neustart nötig, kein RESTART_CMD: {reason[:200]}")
        return last_mono
    logger.warning("Safety-Watchdog Neustart: %s | cmd=%s", reason, cmd)
    if tg and tg.enabled:
        tg.notify_error("SAFETY_WATCHDOG_RESTART", f"{reason[:180]}\ncmd={cmd[:120]}")
    try:
        subprocess.run(cmd, shell=True, timeout=120, check=False)
    except Exception as e:
        logger.error("Neustart-Befehl fehlgeschlagen: %s", e)
    return now


def run_forever() -> None:
    root = _project_root()
    tg = TelegramNotifier()
    poll = max(30, int(getattr(settings, "SAFETY_WATCHDOG_POLL_SEC", 120)))
    tail_n = max(50, int(getattr(settings, "SAFETY_WATCHDOG_LOG_TAIL_LINES", 500)))
    err_thr = max(1, int(getattr(settings, "SAFETY_WATCHDOG_ERROR_LINE_THRESHOLD", 20)))
    log_rel = (getattr(settings, "SAFETY_WATCHDOG_LOG_FILE", "") or "").strip() or getattr(
        settings, "SUPERVISOR_BOT_LOGFILE", "logs/bot_process.log"
    )
    log_path = Path(log_rel)
    if not log_path.is_absolute():
        log_path = root / log_path

    last_restart_mono = 0.0
    logger.info(
        "Safety-Watchdog start | poll=%ss | log=%s | restart_cmd=%s",
        poll,
        log_path,
        "set" if (getattr(settings, "SAFETY_WATCHDOG_RESTART_CMD", "") or "").strip() else "empty",
    )

    while True:
        try:
            # 1) Recovery-Datei (Paper)
            cleared, msg = _clear_stuck_recovery(root)
            if cleared:
                logger.warning("Recovery-Anpassung: %s", msg)
                if tg.enabled:
                    tg.notify_error("SAFETY_WATCHDOG", msg)

            # 2) Syntax
            ok_c, cmsg = _run_compileall(root)
            if not ok_c:
                logger.error("compileall: %s", cmsg)
                if tg.enabled:
                    tg.notify_error("SAFETY_WATCHDOG_COMPILE", cmsg[:400])
                # Kein Neustart: Syntaxfehler behebt restart nicht — manuell fixen.

            # 3) Optional ruff
            _maybe_ruff_autofix(root)

            # 4) Prozess lebendig?
            pids = _find_bot_pids()
            if not pids:
                logger.error("Kein Bot-Prozess (main.py) gefunden")
                if tg.enabled:
                    tg.notify_error("SAFETY_WATCHDOG", "Kein main.py-Prozess — Neustart?")
                last_restart_mono = _restart_bot("bot_process_missing", tg, last_restart_mono)

            # 5) Log-Burst
            lines = _tail_log(log_path, tail_n)
            n_err = _count_error_lines(lines)
            if n_err >= err_thr:
                logger.error("Viele Fehlerzeilen im Log (%d/%d): Neustart erwägen", n_err, tail_n)
                if tg.enabled:
                    tg.notify_error(
                        "SAFETY_WATCHDOG_LOG",
                        f"ERROR-Zeilen im Tail: {n_err} (Schwelle {err_thr})",
                    )
                last_restart_mono = _restart_bot(f"log_error_burst n={n_err}", tg, last_restart_mono)

        except Exception as e:
            logger.exception("Safety-Watchdog Schleifenfehler: %s", e)
            if tg.enabled:
                tg.notify_error("SAFETY_WATCHDOG_LOOP", str(e)[:300])

        time.sleep(poll)


def main() -> None:
    os.chdir(_project_root())
    run_forever()


if __name__ == "__main__":
    main()
