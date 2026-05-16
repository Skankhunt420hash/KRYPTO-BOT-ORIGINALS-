#!/usr/bin/env python3
"""
Separater Safety-Watchdog (eigener Prozess).

Start:
  python safety_watchdog.py

systemd-Beispiel: deploy/safety-watchdog.service

Siehe config/settings.py: SAFETY_WATCHDOG_* und .env.example.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.safety.watchdog import main  # noqa: E402

if __name__ == "__main__":
    main()
