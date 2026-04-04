# Bot auf DigitalOcean – mit **GitHub** (empfohlen)

Dein **`.env`** ist im Projekt **nicht** in Git (steht in `.gitignore`) – **API-Keys landen nie auf GitHub**. Gut so.

---

## A) Einmal: Code auf GitHub legen (auf deinem PC)

### 1. Repo auf github.com anlegen

1. **github.com** → einloggen → **New repository**.
2. Name z. B. `krypto-bot` – **Private** wählen (wichtig).
3. **Create repository** – **ohne** README (du hast schon einen Ordner).

### 2. Projekt hochladen (PowerShell im Bot-Ordner)

```powershell
cd "C:\Users\elbbu\Desktop\KRYPTO-BOT-ORIGINALS-\KRYPTO-BOT-ORIGINALS-"

git status
```

Wenn **„not a git repository“** kommt:

```powershell
git init
git add .
git commit -m "Initial commit"
```

Dann Remote eintragen (**URL** von GitHub kopieren – grüner Button **Code** → HTTPS):

```powershell
git branch -M main
git remote add origin https://github.com/DEIN_USERNAME/DEIN_REPO.git
git push -u origin main
```

Beim ersten Mal fragt GitHub nach **Login** (Browser oder Token).  
Anleitung: [GitHub: HTTPS mit Token](https://docs.github.com/en/get-started/getting-started-with-git/about-remote-repositories)

**Ab jetzt:** Nach Änderungen nur noch:

```powershell
git add .
git commit -m "Kurze Beschreibung"
git push
```

Der Server holt sich Updates später mit `git pull`.

---

## B) DigitalOcean Droplet (einmal)

1. **Create** → **Droplets** → **Ubuntu 22.04** → **2 GB RAM** → SSH-Key → **Create**.
2. **IP-Adresse** notieren.

---

## C) Auf dem Server: Repo klonen & installieren

### 1. Einloggen

```powershell
ssh root@DEINE_IP
```

### 2. Repository klonen

**HTTPS** (einfach – GitHub fragt nach User/Token beim ersten Mal):

```bash
cd /root
git clone https://github.com/DEIN_USERNAME/DEIN_REPO.git krypto-bot
cd krypto-bot
```

**Privates Repo:** Derselbe Befehl – GitHub verlangt dann **Username + Personal Access Token** (als Passwort).  
Oder du legst auf dem Server einen **Deploy Key** an (fortgeschritten, optional).

### 3. Installation (automatisch)

```bash
chmod +x deploy/setup-server.sh
./deploy/setup-server.sh
```

### 4. `.env` auf den Server (ohne Git!)

Auf **deinem Windows-PC** (Pfad anpassen):

```powershell
scp "C:\Users\elbbu\Desktop\KRYPTO-BOT-ORIGINALS-\KRYPTO-BOT-ORIGINALS-\.env" root@DEINE_IP:/root/krypto-bot/.env
```

### 5. Dauerhaft starten (systemd)

```bash
cd /root/krypto-bot
sudo cp deploy/krypto-bot.service.example /etc/systemd/system/krypto-bot.service
sudo nano /etc/systemd/system/krypto-bot.service
```

- **`DEIN_LINUX_USER`** → **`root`**
- **`WorkingDirectory`** und **`ExecStart`** → Pfade **`/root/krypto-bot`** …

Speichern, dann:

```bash
sudo systemctl daemon-reload
sudo systemctl enable krypto-bot
sudo systemctl start krypto-bot
sudo systemctl status krypto-bot
```

Logs:

```bash
sudo journalctl -u krypto-bot -f
```

---

## D) Später: Code aktualisieren

Auf dem Server:

```bash
cd /root/krypto-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart krypto-bot
```

---

## Checkliste

- [ ] Repo auf GitHub (**private**), `git push` vom PC  
- [ ] Droplet + IP  
- [ ] `ssh` + `git clone` + `setup-server.sh`  
- [ ] `.env` per `scp` (nie committen!)  
- [ ] `systemctl start krypto-bot`  

**Wichtig:** Nur **eine** laufende Bot-Instanz mit demselben Telegram-Token (PC aus, wenn der Server läuft).

---

## Ohne GitHub (nur falls nötig)

Ordner per `scp` kopieren – siehe alte Variante oder fragen.
