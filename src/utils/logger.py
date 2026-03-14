import logging
import os
import sys
from datetime import datetime
from rich.logging import RichHandler
from rich.console import Console

# Windows: CP1252 stdout can crash on Unicode log lines (box drawing, symbols).
# Best effort: reconfigure std streams to UTF-8 with replacement fallback.
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # Logging must never fail because stream reconfiguration is unavailable.
    pass

# Force modern terminal handling to keep rich output stable.
console = Console(file=sys.stdout, legacy_windows=False)


def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
    log_level = getattr(logging, level.upper(), logging.INFO)

    os.makedirs("logs", exist_ok=True)
    log_filename = f"logs/bot_{datetime.now().strftime('%Y%m%d')}.log"

    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    logger.propagate = False

    if not logger.handlers:
        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            markup=True,
            rich_tracebacks=True,
        )
        rich_handler.setLevel(log_level)

        file_handler = logging.FileHandler(log_filename, encoding="utf-8")
        file_handler.setLevel(log_level)
        file_formatter = logging.Formatter(
            "%(asctime)s | %(name)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)

        logger.addHandler(rich_handler)
        logger.addHandler(file_handler)

    return logger
