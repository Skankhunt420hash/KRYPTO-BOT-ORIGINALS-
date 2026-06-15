#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt Dienste, zieht main, bewahrt operative Runtime-Safety-Flags und startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
TMP_DIR="$(mktemp -d)"
WATCHDOG_WAS_ACTIVE=0

restore_runtime_state() {
  if [[ -f "$TMP_DIR/runtime_recovery.json" ]]; then
    mkdir -p data
    cp -p "$TMP_DIR/runtime_recovery.json" data/runtime_recovery.json
  fi
}

cleanup_and_recover() {
  local rc=$?
  if [[ $rc -ne 0 ]]; then
    echo "sync fehlgeschlagen — Runtime-State wird wiederhergestellt und Bot wird neu gestartet"
    restore_runtime_state
    sudo systemctl start krypto-bot 2>/dev/null || true
    if [[ "$WATCHDOG_WAS_ACTIVE" == "1" ]]; then
      sudo systemctl start safety-watchdog 2>/dev/null || true
    fi
  fi
  rm -rf "$TMP_DIR"
  exit "$rc"
}
trap cleanup_and_recover EXIT

if sudo systemctl is-active --quiet safety-watchdog 2>/dev/null; then
  WATCHDOG_WAS_ACTIVE=1
  sudo systemctl stop safety-watchdog 2>/dev/null || true
fi
sudo systemctl stop krypto-bot 2>/dev/null || true

if [[ -f data/runtime_recovery.json ]]; then
  cp -p data/runtime_recovery.json "$TMP_DIR/runtime_recovery.json"
fi

git fetch origin
# Getrackte Reporting-Dateien nicht committen — operative Recovery-Safety-Flags bleiben erhalten.
git restore data/daily_summary.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

restore_runtime_state

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
if [[ "$WATCHDOG_WAS_ACTIVE" == "1" ]]; then
  sudo systemctl start safety-watchdog 2>/dev/null || true
fi
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
trap - EXIT
rm -rf "$TMP_DIR"
