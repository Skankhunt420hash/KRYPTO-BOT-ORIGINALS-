#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, stellt Laufzeit-JSONs zurück, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
BOT_SERVICE="${BOT_SERVICE:-krypto-bot}"
WATCHDOG_SERVICE="${WATCHDOG_SERVICE:-safety-watchdog}"
RUNTIME_JSONS=(data/daily_summary.json data/runtime_recovery.json)
BACKUP_DIR="$(mktemp -d)"
FAILED=0

cleanup() {
  rm -rf "$BACKUP_DIR"
}
trap cleanup EXIT

backup_runtime_jsons() {
  local file target_dir
  for file in "${RUNTIME_JSONS[@]}"; do
    if [[ -f "$file" ]]; then
      target_dir="$BACKUP_DIR/$(dirname "$file")"
      mkdir -p "$target_dir"
      cp -p "$file" "$target_dir/"
    fi
  done
}

restore_runtime_jsons() {
  local file backup
  for file in "${RUNTIME_JSONS[@]}"; do
    backup="$BACKUP_DIR/$file"
    if [[ -f "$backup" ]]; then
      mkdir -p "$(dirname "$file")"
      cp -p "$backup" "$file"
    fi
  done
}

stop_services() {
  # Watchdog zuerst stoppen, damit er den bewusst gestoppten Bot nicht waehrend
  # des Pulls neu startet.
  sudo systemctl stop "$WATCHDOG_SERVICE" 2>/dev/null || true
  sudo systemctl stop "$BOT_SERVICE" 2>/dev/null || true
}

start_services() {
  sudo systemctl start "$BOT_SERVICE" 2>/dev/null || true
  sudo systemctl start "$WATCHDOG_SERVICE" 2>/dev/null || true
}

cd "$BOT_DIR"

echo "==> Working directory: $PWD"
stop_services
backup_runtime_jsons

git fetch origin main
# Getrackte Laufzeitdateien koennen git pull blockieren. Vorher sichern und
# nach dem Pull wiederherstellen, damit Pause/Risk-Off/Runtime-Stats erhalten bleiben.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

if ! git pull origin main --no-rebase; then
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  FAILED=1
fi

if [[ "$FAILED" -eq 0 && -f .venv/bin/pip ]]; then
  if ! .venv/bin/pip install -q -r requirements.txt; then
    echo "pip install fehlgeschlagen"
    FAILED=1
  fi
fi

restore_runtime_jsons
start_services
sudo systemctl status "$BOT_SERVICE" --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"

exit "$FAILED"
