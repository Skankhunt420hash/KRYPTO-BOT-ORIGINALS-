#!/usr/bin/env bash
# Einmal auf dem Ubuntu-Server ausführen (im Projektordner: ./deploy/setup-server.sh)
set -e
cd "$(dirname "$0")/.."
echo "==> Ordner: $(pwd)"

if ! command -v python3 &>/dev/null; then
  echo "==> Installiere Python..."
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip
fi

echo "==> Virtuelle Umgebung..."
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

mkdir -p data logs
echo ""
echo "=== Fertig mit Setup ==="
echo "Als Nächstes:"
echo "  1) Datei .env hier hinlegen (kopieren mit scp oder nano .env)"
echo "  2) Test:  source .venv/bin/activate && python main.py --multi --interval 60"
echo "  3) Dauerbetrieb: siehe deploy/SO-MACHST-DU-DAS.md (systemd)"
echo ""
