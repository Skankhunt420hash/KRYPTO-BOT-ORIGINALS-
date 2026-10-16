#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt Dienste, sichert Laufzeit-JSONs, zieht main und startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
WATCHDOG_WAS_ACTIVE=0
if sudo systemctl is-active --quiet safety-watchdog 2>/dev/null; then
  WATCHDOG_WAS_ACTIVE=1
  sudo systemctl stop safety-watchdog 2>/dev/null || true
fi
sudo systemctl stop krypto-bot 2>/dev/null || true
RUNTIME_BACKUP_DIR="$(mktemp -d)"

backup_runtime_files() {
  for f in data/daily_summary.json data/runtime_recovery.json; do
    if [[ -f "$f" ]]; then
      mkdir -p "$RUNTIME_BACKUP_DIR/$(dirname "$f")"
      cp -a "$f" "$RUNTIME_BACKUP_DIR/$f"
    fi
  done
}

restore_runtime_files() {
  for f in data/daily_summary.json data/runtime_recovery.json; do
    if [[ -f "$RUNTIME_BACKUP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp -a "$RUNTIME_BACKUP_DIR/$f" "$f"
    fi
  done
}

cleanup() {
  rc=$?
  restore_runtime_files
  sudo systemctl start krypto-bot 2>/dev/null || true
  if [[ "$WATCHDOG_WAS_ACTIVE" == "1" ]]; then
    sudo systemctl start safety-watchdog 2>/dev/null || true
  fi
  rm -rf "$RUNTIME_BACKUP_DIR"
  exit "$rc"
}
trap cleanup EXIT

backup_runtime_files

git fetch origin

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

trap - EXIT
restore_runtime_files
sudo systemctl start krypto-bot
if [[ "$WATCHDOG_WAS_ACTIVE" == "1" ]]; then
  sudo systemctl start safety-watchdog 2>/dev/null || true
fi
rm -rf "$RUNTIME_BACKUP_DIR"
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
