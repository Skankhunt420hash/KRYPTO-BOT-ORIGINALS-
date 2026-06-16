#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt die Dienste, schützt Laufzeit-JSONs, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
BACKUP_DIR="$(mktemp -d)"
restore_runtime_files() {
  for file in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/$file" ]]; then
      mkdir -p "$(dirname "$file")"
      cp "$BACKUP_DIR/$file" "$file"
    fi
  done
  rm -rf "$BACKUP_DIR"
}
trap restore_runtime_files EXIT

for file in "${RUNTIME_FILES[@]}"; do
  if [[ -f "$file" ]]; then
    mkdir -p "$BACKUP_DIR/$(dirname "$file")"
    cp "$file" "$BACKUP_DIR/$file"
  fi
done

git fetch origin
# Getrackte Laufzeitdateien blockieren sonst Pulls; lokale Werte werden danach restauriert.
git restore "${RUNTIME_FILES[@]}" 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

restore_runtime_files
trap - EXIT

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl start safety-watchdog 2>/dev/null || true
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
