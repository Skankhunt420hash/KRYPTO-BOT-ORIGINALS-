#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, zieht main und startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
sudo systemctl stop krypto-bot 2>/dev/null || true

RUNTIME_RECOVERY_FILE="data/runtime_recovery.json"
RUNTIME_RECOVERY_BACKUP=""

cleanup() {
  if [[ -n "$RUNTIME_RECOVERY_BACKUP" ]]; then
    rm -f "$RUNTIME_RECOVERY_BACKUP"
  fi
}
trap cleanup EXIT

restore_runtime_recovery() {
  if [[ -n "$RUNTIME_RECOVERY_BACKUP" && -f "$RUNTIME_RECOVERY_BACKUP" ]]; then
    mkdir -p "$(dirname "$RUNTIME_RECOVERY_FILE")"
    cp -p "$RUNTIME_RECOVERY_BACKUP" "$RUNTIME_RECOVERY_FILE"
  fi
}

if [[ -f "$RUNTIME_RECOVERY_FILE" ]]; then
  RUNTIME_RECOVERY_BACKUP="$(mktemp)"
  cp -p "$RUNTIME_RECOVERY_FILE" "$RUNTIME_RECOVERY_BACKUP"
fi

git fetch origin
# Getrackte Laufzeitdateien vor dem Pull kurz zurücksetzen; Recovery-State danach wiederherstellen.
git restore -- data/daily_summary.json "$RUNTIME_RECOVERY_FILE" 2>/dev/null || true

git pull origin main --no-rebase || {
  restore_runtime_recovery
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}
restore_runtime_recovery

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
