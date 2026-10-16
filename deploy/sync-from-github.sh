#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, stellt Laufzeit-JSONs zurück, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"

BACKUP_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$BACKUP_DIR"
}
restore_runtime_files() {
  for f in data/daily_summary.json data/runtime_recovery.json; do
    if [[ -f "$BACKUP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp "$BACKUP_DIR/$f" "$f"
    fi
  done
}
restart_services() {
  sudo systemctl start krypto-bot 2>/dev/null || true
  sudo systemctl start safety-watchdog 2>/dev/null || true
}
on_exit() {
  status=$?
  restore_runtime_files
  restart_services
  cleanup
  exit "$status"
}
trap on_exit EXIT

for f in data/daily_summary.json data/runtime_recovery.json; do
  if [[ -f "$f" ]]; then
    mkdir -p "$BACKUP_DIR/$(dirname "$f")"
    cp "$f" "$BACKUP_DIR/$f"
  fi
done

sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin
# Getrackte Laufzeitdateien vor dem Pull nur im Arbeitsbaum bereinigen; danach werden lokale Runtime-Werte restauriert.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

restore_runtime_files
trap cleanup EXIT
sudo systemctl start krypto-bot
sudo systemctl start safety-watchdog 2>/dev/null || true
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
