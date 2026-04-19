#!/usr/bin/env bash
# Auf dem DigitalOcean-Server ausführen (als root), NACHDEM /root/krypto-bot per git clone existiert.
#   cd /root/krypto-bot && chmod +x deploy/INSTALL-SERVER-KOMPLETT.sh && sudo ./deploy/INSTALL-SERVER-KOMPLETT.sh

set -e
BOT_DIR="/root/krypto-bot"

echo "==> Prüfe Ordner $BOT_DIR ..."
if [ ! -f "$BOT_DIR/main.py" ]; then
  echo "FEHLER: $BOT_DIR/main.py nicht gefunden."
  echo "Zuerst (eine Zeile, URL von GitHub einsetzen):"
  echo "  cd /root && git clone https://github.com/DEIN_USER/DEIN_REPO.git krypto-bot"
  exit 1
fi

echo "==> System-Pakete ..."
apt-get update -qq
apt-get install -y git python3 python3-venv python3-pip

echo "==> Python & pip (Projekt) ..."
cd "$BOT_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
mkdir -p data logs
deactivate

echo "==> systemd-Dienst installieren ..."
if [ ! -f "$BOT_DIR/deploy/krypto-bot-root.service" ]; then
  echo "FEHLER: deploy/krypto-bot-root.service fehlt. git pull?"
  exit 1
fi
cp "$BOT_DIR/deploy/krypto-bot-root.service" /etc/systemd/system/krypto-bot.service
systemctl daemon-reload
systemctl enable krypto-bot

if [ ! -f "$BOT_DIR/.env" ]; then
  echo ""
  echo "=========================================="
  echo "  STOP: Datei .env fehlt in $BOT_DIR"
  echo "=========================================="
  echo "Auf deinem Windows-PC (PowerShell), eine Zeile:"
  echo "  scp \"C:\\Pfad\\zu\\.env\" root@DEINE_SERVER_IP:/root/krypto-bot/.env"
  echo ""
  echo "Dann auf dem Server nochmal:"
  echo "  sudo systemctl start krypto-bot"
  echo "  sudo systemctl status krypto-bot"
  exit 0
fi

echo "==> Starte Bot ..."
systemctl restart krypto-bot 2>/dev/null || systemctl start krypto-bot
sleep 2
systemctl status krypto-bot --no-pager || true
echo ""
echo "Fertig. Logs: sudo journalctl -u krypto-bot -f"
