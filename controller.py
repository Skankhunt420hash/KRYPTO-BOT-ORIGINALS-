#!/usr/bin/env python3
"""
Telegram-Controller/Supervisor für KRYPTO-BOT.

Dieser Prozess läuft dauerhaft und nimmt Telegram-Befehle entgegen:
  /botstart /botstop /botrestart /botstatus

Er startet/stoppt den eigentlichen Trading-Bot als separaten Prozess.
"""

from __future__ import annotations

import time

from config.settings import settings
from src.controller.supervisor import BotProcessSupervisor
from src.telegram.control_panel import PanelCallbacks, TelegramControlPanel
from src.utils.logger import setup_logger

logger = setup_logger("controller.main")


def _validate_controller_config() -> bool:
    if not settings.ENABLE_TELEGRAM:
        logger.error("Controller-Start abgebrochen: ENABLE_TELEGRAM=false")
        return False
    if not settings.TELEGRAM_PANEL_ENABLED:
        logger.error("Controller-Start abgebrochen: TELEGRAM_PANEL_ENABLED=false")
        return False
    if not settings.TELEGRAM_BOT_TOKEN:
        logger.error("Controller-Start abgebrochen: TELEGRAM_BOT_TOKEN fehlt")
        return False
    if not settings.TELEGRAM_CHAT_ID:
        logger.error("Controller-Start abgebrochen: TELEGRAM_CHAT_ID fehlt")
        return False
    return True


def main() -> None:
    if not _validate_controller_config():
        raise SystemExit(1)

    supervisor = BotProcessSupervisor()

    def _runtime_status() -> dict:
        s = supervisor.status()
        return {
            "running": s.get("running", False),
            "engine": "supervisor",
            "mode": settings.TRADING_MODE,
            "active_strategy": "external-process",
            "balance": 0.0,
            "equity": 0.0,
            "available_capital": 0.0,
            "total_trades": 0,
            "open_positions": 0,
            "open_positions_detail": [],
            "recent_trades": [],
            "recent_logs": [],
            "health_status": "n/a",
            "selector": {},
            "risk_gate": {},
            "brain": {},
            "app_context": {
                "bot_kind": "supervisor",
                "running": s.get("running", False),
                "interval_seconds": None,
                "lock_acquired": None,
            },
            "bot_process": s,
        }

    panel = TelegramControlPanel(
        callbacks=PanelCallbacks(
            get_runtime_status=_runtime_status,
            request_bot_start=supervisor.start,
            request_bot_stop=supervisor.stop,
            request_bot_restart=supervisor.restart,
            get_bot_status=supervisor.status,
        )
    )

    panel.start_in_background()
    logger.info(
        "Controller gestartet. Telegram-Befehle: /botstart /botstop /botrestart /botstatus"
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Controller beendet (Bot-Prozess bleibt unverändert).")
    finally:
        panel.stop()


if __name__ == "__main__":
    main()
