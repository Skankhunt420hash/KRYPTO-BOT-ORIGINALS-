#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt die Dienste, zieht main und erhält lokale Laufzeit-JSONs.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
BACKUP_DIR="$(mktemp -d)"
RESTORED_RUNTIME=0

backup_runtime_files() {
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$f" ]]; then
      mkdir -p "$BACKUP_DIR/$(dirname "$f")"
      cp "$f" "$BACKUP_DIR/$f"
    fi
  done
}

restore_runtime_files() {
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp "$BACKUP_DIR/$f" "$f"
    fi
  done
  RESTORED_RUNTIME=1
}

cleanup() {
  status=$?
  if [[ "$RESTORED_RUNTIME" -eq 0 ]]; then
    restore_runtime_files || true
  fi
  rm -rf "$BACKUP_DIR"
  if [[ "$status" -ne 0 ]]; then
    sudo systemctl start krypto-bot 2>/dev/null || true
    sudo systemctl start safety-watchdog 2>/dev/null || true
  fi
}
trap cleanup EXIT

backup_runtime_files
sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin
# Getrackte Laufzeitdateien kurzfristig zurücksetzen, damit pull nicht an lokalen Writes scheitert.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

restore_runtime_files

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl start safety-watchdog 2>/dev/null || true
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
