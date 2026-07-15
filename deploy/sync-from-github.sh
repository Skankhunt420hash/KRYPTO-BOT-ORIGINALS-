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
BOT_WAS_ACTIVE=0
WATCHDOG_WAS_ACTIVE=0
BOT_STOPPED=0
WATCHDOG_STOPPED=0

backup_runtime_files() {
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$f" ]]; then
      mkdir -p "$BACKUP_DIR/$(dirname "$f")"
      cp "$f" "$BACKUP_DIR/$f"
    fi
  done
}

restore_runtime_files() {
  local failed=0
  for f in "${RUNTIME_FILES[@]}"; do
    if [[ -f "$BACKUP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp "$BACKUP_DIR/$f" "$f" || failed=1
    fi
  done
  if [[ "$failed" -ne 0 ]]; then
    return 1
  fi
  RESTORED_RUNTIME=1
}

cleanup() {
  status=$?
  trap - EXIT
  restore_ok=1
  if [[ "$RESTORED_RUNTIME" -eq 0 ]]; then
    if ! restore_runtime_files; then
      restore_ok=0
      status=1
      echo "KRITISCH: Laufzeitdateien konnten nicht restauriert werden; Dienste bleiben gestoppt." >&2
    fi
  fi
  rm -rf "$BACKUP_DIR"
  if [[ "$status" -ne 0 && "$restore_ok" -eq 1 ]]; then
    if [[ "$BOT_WAS_ACTIVE" -eq 1 && "$BOT_STOPPED" -eq 1 ]]; then
      sudo systemctl start krypto-bot 2>/dev/null || true
    fi
    if [[ "$WATCHDOG_WAS_ACTIVE" -eq 1 && "$WATCHDOG_STOPPED" -eq 1 ]]; then
      sudo systemctl start safety-watchdog 2>/dev/null || true
    fi
  fi
  exit "$status"
}
trap cleanup EXIT

if sudo systemctl is-active --quiet safety-watchdog 2>/dev/null; then
  WATCHDOG_WAS_ACTIVE=1
fi
if sudo systemctl is-active --quiet krypto-bot 2>/dev/null; then
  BOT_WAS_ACTIVE=1
fi
if [[ "$WATCHDOG_WAS_ACTIVE" -eq 1 ]]; then
  sudo systemctl stop safety-watchdog
  WATCHDOG_STOPPED=1
fi
if [[ "$BOT_WAS_ACTIVE" -eq 1 ]]; then
  sudo systemctl stop krypto-bot
  BOT_STOPPED=1
fi
backup_runtime_files

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

if [[ "$BOT_WAS_ACTIVE" -eq 1 ]]; then
  sudo systemctl start krypto-bot
  sudo systemctl status krypto-bot --no-pager || true
fi
if [[ "$WATCHDOG_WAS_ACTIVE" -eq 1 ]]; then
  sudo systemctl start safety-watchdog 2>/dev/null || true
fi
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
