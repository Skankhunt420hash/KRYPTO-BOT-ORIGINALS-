#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, stellt Laufzeit-JSONs zurück, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
TMP_DIR="$(mktemp -d)"
cleanup() {
  for f in data/daily_summary.json data/runtime_recovery.json; do
    if [[ -f "$TMP_DIR/$f" ]]; then
      mkdir -p "$(dirname "$f")"
      cp -p "$TMP_DIR/$f" "$f"
    fi
  done
  sudo systemctl start krypto-bot 2>/dev/null || true
  sudo systemctl start safety-watchdog 2>/dev/null || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

for f in data/daily_summary.json data/runtime_recovery.json; do
  if [[ -f "$f" ]]; then
    mkdir -p "$TMP_DIR/$(dirname "$f")"
    cp -p "$f" "$TMP_DIR/$f"
  fi
done

sudo systemctl stop safety-watchdog 2>/dev/null || true
sudo systemctl stop krypto-bot 2>/dev/null || true

git fetch origin
# Getrackte Laufzeitdateien nicht committen — zurück auf letzten Commit
git restore data/daily_summary.json data/runtime_recovery.json 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

for f in data/daily_summary.json data/runtime_recovery.json; do
  if [[ -f "$TMP_DIR/$f" ]]; then
    mkdir -p "$(dirname "$f")"
    cp -p "$TMP_DIR/$f" "$f"
  fi
done

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl start safety-watchdog 2>/dev/null || true
trap - EXIT
rm -rf "$TMP_DIR"
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
