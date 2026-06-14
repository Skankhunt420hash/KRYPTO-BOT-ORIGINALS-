#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt die Dienste, bewahrt operative Runtime-Safety-Flags, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
WATCHDOG_WAS_ACTIVE=0
RECOVERY_BACKUP=""
MAINTENANCE_FILE=".deploy-sync-in-progress"

restore_runtime_recovery() {
  if [[ -n "${RECOVERY_BACKUP:-}" && -f "$RECOVERY_BACKUP" ]]; then
    mkdir -p data
    cp "$RECOVERY_BACKUP" data/runtime_recovery.json
  fi
}

restart_services() {
  sudo systemctl start krypto-bot 2>/dev/null || true
  if [[ "$WATCHDOG_WAS_ACTIVE" == "1" ]]; then
    sudo systemctl start safety-watchdog 2>/dev/null || true
  fi
}

cleanup() {
  rc=$?
  restore_runtime_recovery
  rm -f "$MAINTENANCE_FILE"
  if [[ -n "${RECOVERY_BACKUP:-}" ]]; then
    rm -f "$RECOVERY_BACKUP"
  fi
  if [[ $rc -ne 0 ]]; then
    echo "Sync fehlgeschlagen — starte Dienste mit vorhandenem Stand wieder."
    restart_services
  fi
  exit $rc
}
trap cleanup EXIT

touch "$MAINTENANCE_FILE"
if sudo systemctl is-active --quiet safety-watchdog 2>/dev/null; then
  WATCHDOG_WAS_ACTIVE=1
  sudo systemctl stop safety-watchdog 2>/dev/null || true
fi
sudo systemctl stop krypto-bot 2>/dev/null || true

if [[ -f data/runtime_recovery.json ]]; then
  RECOVERY_BACKUP="$(mktemp)"
  cp data/runtime_recovery.json "$RECOVERY_BACKUP"
fi

git fetch origin
# Getrackte Summary-Artefakte nicht committen — Runtime-Recovery bleibt lokal erhalten.
git restore data/daily_summary.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}
restore_runtime_recovery

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

restart_services
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
