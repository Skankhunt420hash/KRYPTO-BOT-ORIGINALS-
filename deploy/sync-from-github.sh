#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt Watchdog/Bot, erhält Laufzeit-JSONs, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
RUNTIME_FILES=(data/daily_summary.json data/runtime_recovery.json)
TMP_DIR="$(mktemp -d)"

backup_runtime_files() {
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$f" ]]; then
      mkdir -p "$TMP_DIR/$(dirname "$f")"
      cp "$f" "$TMP_DIR/$f"
    fi
  done
}

restore_runtime_files() {
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$TMP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp "$TMP_DIR/$f" "$f"
    fi
  done
}

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

restart_services() {
  sudo systemctl start krypto-bot
  sudo systemctl start safety-watchdog 2>/dev/null || true
}

backup_runtime_files

# Watchdog zuerst stoppen, damit er krypto-bot während Pull/Install nicht neu startet.
sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin
# Getrackte Laufzeitdateien blockieren sonst git pull; lokale Werte werden danach restauriert.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  restore_runtime_files
  restart_services
  exit 1
}

restore_runtime_files

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

restart_services
sudo systemctl status krypto-bot --no-pager || true
sudo systemctl status safety-watchdog --no-pager 2>/dev/null || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
