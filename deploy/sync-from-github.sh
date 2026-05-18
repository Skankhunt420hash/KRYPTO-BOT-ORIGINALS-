#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, zieht main, erhält lokalen Recovery-State, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"
runtime_backup=""

cleanup() {
  if [[ -n "$runtime_backup" && -f "$runtime_backup" ]]; then
    rm -f "$runtime_backup"
  fi
}
trap cleanup EXIT

echo "==> Working directory: $PWD"
sudo systemctl stop krypto-bot 2>/dev/null || true

if [[ -f data/runtime_recovery.json ]]; then
  runtime_backup="$(mktemp)"
  cp data/runtime_recovery.json "$runtime_backup"
fi

git fetch origin
# Getrackte Laufzeitdateien nicht committen — für den Pull bereinigen.
# runtime_recovery.json enthält operative Pause-/Risk-Off-Sperren und wird danach wiederhergestellt.
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

if [[ -n "$runtime_backup" && -f "$runtime_backup" ]]; then
  mkdir -p data
  cp "$runtime_backup" data/runtime_recovery.json
  echo "==> data/runtime_recovery.json aus lokalem Runtime-State wiederhergestellt"
fi

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
