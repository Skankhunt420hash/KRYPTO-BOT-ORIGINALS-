#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, stellt Laufzeit-JSONs zurück, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
RUNTIME_BACKUP_DIR="$(mktemp -d)"
restore_runtime_files() {
  [[ -d "$RUNTIME_BACKUP_DIR" ]] || return 0
  for f in data/daily_summary.json data/runtime_recovery.json; do
    if [[ -f "$RUNTIME_BACKUP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp "$RUNTIME_BACKUP_DIR/$f" "$f"
    fi
  done
  rm -rf "$RUNTIME_BACKUP_DIR"
}
cleanup_on_error() {
  restore_runtime_files
  sudo systemctl start krypto-bot 2>/dev/null || true
  sudo systemctl start safety-watchdog 2>/dev/null || true
}
mkdir -p "$RUNTIME_BACKUP_DIR/data"
for f in data/daily_summary.json data/runtime_recovery.json; do
  [[ -f "$f" ]] && cp "$f" "$RUNTIME_BACKUP_DIR/$f"
done
trap cleanup_on_error EXIT

sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin
# Getrackte Laufzeitdateien nicht committen — für den Pull kurz zurücksetzen,
# danach aber operative Runtime-/Safety-Flags wiederherstellen.
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
trap - EXIT
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
