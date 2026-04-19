# Bot auf dem Server – nur diese Schritte (fest: `/root/krypto-bot`)

Alles passiert in **zwei Fenstern**:  
**A)** einmal **Windows PowerShell**  
**B)** mehrfach **Server** (nach `ssh`)

---

## Vorher auf dem PC (GitHub)

Dein Code muss auf GitHub sein (privates Repo).  
URL notieren, z. B. `https://github.com/max/krypto-bot.git`

---

## SCHRITT 1 – Server öffnen (nur einmal)

Auf **Windows**, PowerShell:

```text
ssh root@DEINE_SERVER_IP
```

`DEINE_SERVER_IP` = die Zahl von DigitalOcean.  
Passwort oder Key – wie du den Droplet eingerichtet hast.

---

## SCHRITT 2 – Bot holen (genau diese 3 Zeilen, URL anpassen)

**Auf dem Server** (schwarzes Fenster nach ssh), **eine Zeile nach der anderen**:

```bash
apt update && apt install -y git python3 python3-venv python3-pip
```

```bash
cd /root && git clone https://github.com/DEIN_USER/DEIN_REPO.git krypto-bot
```

`DEIN_USER` und `DEIN_REPO` = von deiner GitHub-URL.

Falls `krypto-bot` schon existiert und Chaos macht:

```bash
cd /root && mv krypto-bot krypto-bot-alt-$(date +%s) && git clone https://github.com/DEIN_USER/DEIN_REPO.git krypto-bot
```

---

## SCHRITT 3 – Installation + Dienst (auf dem Server)

```bash
cd /root/krypto-bot && chmod +x deploy/INSTALL-SERVER-KOMPLETT.sh && sudo ./deploy/INSTALL-SERVER-KOMPLETT.sh
```

Wenn am Ende steht **`.env fehlt`** → weiter mit SCHRITT 4.  
Wenn **active (running)** → fertig.

---

## SCHRITT 4 – `.env` hochladen (Windows PowerShell, **neues** Fenster)

**Nicht** auf dem Server eingeloggt sein.  
Pfad zu **deiner** `.env` anpassen:

```powershell
scp "C:\Users\elbbu\Desktop\KRYPTO-BOT-ORIGINALS-\KRYPTO-BOT-ORIGINALS-\.env" root@DEINE_SERVER_IP:/root/krypto-bot/.env
```

---

## SCHRITT 5 – Bot starten (wieder auf dem Server)

```bash
ssh root@DEINE_SERVER_IP
```

```bash
sudo systemctl start krypto-bot
sudo systemctl status krypto-bot
```

Grün / **active (running)** = läuft.

Logs:

```bash
sudo journalctl -u krypto-bot -f
```

Beenden der Anzeige: `Strg+C` (Bot läuft weiter).

---

## Wenn wieder nichts geht

Diese **eine** Zeile auf dem Server:

```bash
ls -la /root/krypto-bot/main.py && ls -la /root/krypto-bot/.env
```

Beide müssen **existieren**. Wenn `main.py` fehlt → SCHRITT 2 nochmal mit richtiger GitHub-URL.  
Wenn `.env` fehlt → SCHRITT 4.

---

## Merksatz

| Wo | Was |
|----|-----|
| Ordner auf Server | **immer** `/root/krypto-bot` |
| `.env` | **immer** `/root/krypto-bot/.env` |
| Dienstname | **krypto-bot** |

Nach Änderungen am Code auf GitHub:

```bash
cd /root/krypto-bot && git pull && source .venv/bin/activate && pip install -r requirements.txt && sudo systemctl restart krypto-bot
```
