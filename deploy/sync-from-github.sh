#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, zieht main und erhält lokale Laufzeit-JSONs, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
sudo systemctl stop krypto-bot 2>/dev/null || true

RUNTIME_BACKUP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$RUNTIME_BACKUP_DIR"
}
trap cleanup EXIT

RUNTIME_FILES=(
  "data/daily_summary.json"
  "data/runtime_recovery.json"
)

echo "==> Sichere lokale Laufzeitdateien"
for file in "${RUNTIME_FILES[@]}"; do
  if [[ -f "$file" ]]; then
    mkdir -p "$RUNTIME_BACKUP_DIR/$(dirname "$file")"
    cp -p "$file" "$RUNTIME_BACKUP_DIR/$file"
  fi
done

git fetch origin
# Getrackte Laufzeitdateien nicht committen — zurück auf letzten Commit
git restore "${RUNTIME_FILES[@]}" 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

echo "==> Stelle lokale Laufzeitdateien wieder her"
for file in "${RUNTIME_FILES[@]}"; do
  if [[ -f "$RUNTIME_BACKUP_DIR/$file" ]]; then
    mkdir -p "$(dirname "$file")"
    cp -p "$RUNTIME_BACKUP_DIR/$file" "$file"
  fi
done

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
