#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt die Dienste, zieht main und stellt lokale Laufzeit-JSONs wieder her.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"

TMP_DIR="$(mktemp -d)"
RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)

backup_runtime_files() {
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$file" ]]; then
      mkdir -p "$TMP_DIR/$(dirname "$file")"
      cp -a "$file" "$TMP_DIR/$file"
    fi
  done
}

restore_runtime_files() {
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$TMP_DIR/$file" ]]; then
      mkdir -p "$(dirname "$file")"
      cp -a "$TMP_DIR/$file" "$file"
    fi
  done
}

finish() {
  status=$?
  restore_runtime_files
  sudo systemctl start krypto-bot 2>/dev/null || true
  sudo systemctl start safety-watchdog 2>/dev/null || true
  rm -rf "$TMP_DIR"
  exit "$status"
}
trap finish EXIT

backup_runtime_files

sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin main
# Getrackte Laufzeitdateien kurz zurücksetzen, damit der Pull nicht an lokalen Runtime-Änderungen scheitert.
git restore "${RUNTIME_FILES[@]}" 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — Bot/Watchdog werden mit lokalem Runtime-State wieder gestartet."
  exit 1
}

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

restore_runtime_files
sudo systemctl start krypto-bot
sudo systemctl start safety-watchdog 2>/dev/null || true
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
trap - EXIT
rm -rf "$TMP_DIR"
