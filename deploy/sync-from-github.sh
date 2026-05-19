#!/usr/bin/env bash
# Auf dem Server ausführen (im Bot-Verzeichnis), um mit origin/main zu synchronisieren.
# Stoppt den Dienst, stellt Laufzeit-JSONs zurück, zieht main, startet neu.
set -euo pipefail
BOT_DIR="${1:-/root/krypto-bot}"
cd "$BOT_DIR"

echo "==> Working directory: $PWD"
sudo systemctl stop krypto-bot 2>/dev/null || true

runtime_backup_dir="$(mktemp -d)"
cleanup() {
  rm -rf "$runtime_backup_dir"
}
trap cleanup EXIT

runtime_files=(
  "data/daily_summary.json"
  "data/runtime_recovery.json"
)

# Lokale Laufzeitdateien enthalten operative Zustände wie /pause und /riskoff.
# Sie werden kurz gesichert, damit der Pull nicht durch getrackte Änderungen blockiert,
# danach aber wieder exakt auf den Server-Stand zurückgesetzt.
for file in "${runtime_files[@]}"; do
  if [[ -f "$file" ]]; then
    mkdir -p "$runtime_backup_dir/$(dirname "$file")"
    cp -p "$file" "$runtime_backup_dir/$file"
  fi
done

git fetch origin
git restore "${runtime_files[@]}" 2>/dev/null || true

git pull origin main --no-rebase || {
  echo "pull fehlgeschlagen — optional: git reset --hard origin/main (lokale Commits am Server gehen verloren)"
  exit 1
}

for file in "${runtime_files[@]}"; do
  if [[ -f "$runtime_backup_dir/$file" ]]; then
    mkdir -p "$(dirname "$file")"
    cp -p "$runtime_backup_dir/$file" "$file"
  fi
done

if [[ -f .venv/bin/pip ]]; then
  .venv/bin/pip install -q -r requirements.txt
fi

sudo systemctl start krypto-bot
sudo systemctl status krypto-bot --no-pager || true
echo "==> Logs: sudo journalctl -u krypto-bot -n 50 --no-pager"
