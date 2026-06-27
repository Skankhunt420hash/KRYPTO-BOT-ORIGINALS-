#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, stellt Laufzeit-JSONs zurück, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"

RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
BACKUP_DIR="$(mktemp -d)"

restore_runtime_files() {
  local file
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/$file" ]]; then
      mkdir -p "$(dirname "$file")"
      cp "$BACKUP_DIR/$file" "$file"
    fi
  done
}

restart_services() {
  sudo systemctl start krypto-bot 2>/dev/null || true
  sudo systemctl start safety-watchdog 2>/dev/null || true
}

cleanup() {
  local status=$?
  restore_runtime_files
  rm -rf "$BACKUP_DIR"
  restart_services
  exit "$status"
}
trap cleanup EXIT

mkdir -p "$BACKUP_DIR/data"
for file in "${RUNTIME_FILES[@]}"; do
  if [[ -f "$file" ]]; then
    cp "$file" "$BACKUP_DIR/$file"
  fi
done

sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin main
git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

trap - EXIT
restore_runtime_files
rm -rf "$BACKUP_DIR"
restart_services
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
