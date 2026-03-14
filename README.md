# KRYPTO-BOT ORIGINALS

Multi-Strategy-Krypto-Bot mit Paper-Trading, Backtesting und Telegram-Control-Panel.

## Projektüberblick

- **Fokus heute:** stabiler `paper`-Modus mit Entscheidungslogik vor jeder Ausführung
- **Entscheidungsschicht:** Regime-Erkennung -> Meta-Selector -> Risk-Gate -> Execution
- **Steuerung & Monitoring:** Telegram-Panel (`/status`, `/summary`, `/risk`, `/strategy`, ...)
- **Persistenz:** SQLite (`data/trades.db`) für Trades/Statistiken
- **Live-Modus:** strukturell vorbereitet, aber bewusst sicher begrenzt

---

## Funktionen

- **Mehrere Strategien**: RSI+EMA, MACD Crossover, Kombiniert sowie erweiterte Strategien (Momentum Pullback, Range Reversion, Volatility Breakout, Trend Continuation)
- **Risikomanagement**: Stop-Loss, Take-Profit, Trailing Stop, Positionsgrößen-Kontrolle
- **Portfolio-Risk-Engine**: Zentrales Exposure-Management mit unterschiedlichen Position-Sizing-Modi
- **Paper-Trading**: Sicheres Testen ohne echtes Geld
- **Backtesting & Walk-Forward**: Candle-by-candle Backtests und Walk-Forward-Evaluation mit Overfitting-Indikatoren
- **Multi-Pair**: Mehrere Handelspaare gleichzeitig
- **Logging & Monitoring**: Farbige Konsolen-Ausgabe + Datei-Logs, Health-Monitor, Telegram-Benachrichtigungen
- **Exchange-Kompatibilität**: Binance, Kraken, Bybit, Coinbase und viele mehr (via ccxt)

---

## Projektstruktur

```
krypto-bot/
├── main.py                    # Einstiegspunkt
├── requirements.txt           # Python-Abhängigkeiten
├── .env.example               # Konfigurationsvorlage
├── config/
│   └── settings.py            # Alle Einstellungen
├── src/
│   ├── bot.py                 # Haupt-Bot-Logik (Single & Multi-Strategy)
│   ├── exchange/              # Exchange-Verbindung (ccxt)
│   ├── engine/                # Risk-Engine, Meta-Selector, Execution, Health
│   ├── strategies/            # Legacy- und Enhanced-Strategien
│   ├── storage/               # SQLite-Storage & Repository
│   └── utils/                 # Logger, Basis-RiskManager, Telegram
├── backtest/                  # Backtest-Engine, CLI & Walk-Forward
├── logs/                      # Log-Dateien
└── data/                      # Datenbank & CSV-Daten
```

---

## Lokaler Start (Windows, Paper-Modus)

### 1) Installation und virtuelle Umgebung

```powershell
cd "C:\Users\elbbu\Desktop\KRYPTO-BOT-ORIGINALS-\KRYPTO-BOT-ORIGINALS-"
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

### 2) `.env` anlegen

```powershell
copy .env.example .env
notepad .env
```

### 3) Pflichtvariablen für Paper-Start

```env
BOT_MODE=paper
TRADING_PAIRS=BTC/USDT,ETH/USDT,SOL/USDT
TIMEFRAME=1h
STRATEGY=auto
LIVE_TRADING_ENABLED=false
ENFORCE_SINGLE_INSTANCE=true
```

API-Schlüssel sind im Paper-Modus nicht notwendig.

### 4) Bot starten

```powershell
python main.py --multi --interval 60
```

Nützliche Zusatzbefehle:

```powershell
python main.py --status --multi
python main.py --once --multi
python main.py --strategy-stats
python main.py --health
```

### 5. Backtesting & Walk-Forward

```bash
# Einzelstrategie-Backtest (z.B. trend_continuation) auf CSV
python main.py --backtest --csv data/BTC_USDT_1h_test.csv --strategy trend_continuation

# Multi-Strategie-Backtest inkl. Meta-Selector
python main.py --backtest --csv data/BTC_USDT_1h_test.csv --multi

# Walk-Forward-Evaluation
python main.py --walk-forward --csv data/BTC_USDT_1h_test.csv --strategy trend_continuation
```

### 6. Telegram-Control-Panel (optional)

#### 6.1 Telegram-Bot mit BotFather erstellen

1. In Telegram `@BotFather` öffnen  
2. `/newbot` senden  
3. Namen und Username vergeben  
4. BotFather liefert den Token → als `TELEGRAM_BOT_TOKEN` in `.env` eintragen

#### 6.2 Eigene Chat-ID ermitteln

1. Dem neuen Bot einmal `/start` senden  
2. Im Browser aufrufen (Token einsetzen):  
   `https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates`  
3. In der JSON-Antwort die `chat.id` auslesen  
4. Diese ID als `TELEGRAM_CHAT_ID` (und optional in `TELEGRAM_PANEL_ALLOWED_IDS`) setzen

#### 6.3 Benötigte `.env`-Einträge (Telegram)

```env
ENABLE_TELEGRAM=true
# optional kompatibel:
# TELEGRAM_ENABLED=true

TELEGRAM_BOT_TOKEN=<botfather_token>
TELEGRAM_CHAT_ID=<deine_chat_id>

# Benachrichtigungen
ENABLE_NOTIFICATIONS=true
TELEGRAM_NOTIFY_LEVEL=trading
TELEGRAM_MIN_CONFIDENCE=50
TELEGRAM_ERROR_ALERT_COOLDOWN_SEC=120

# Control-Panel (Polling)
TELEGRAM_PANEL_ENABLED=true
TELEGRAM_PANEL_POLL_INTERVAL_SEC=10
TELEGRAM_PANEL_LOG_LINES=20
# optional:
TELEGRAM_PANEL_ALLOWED_IDS=<chat_id_1,chat_id_2>
```

#### 6.4 Lokal testen

1. Bot lokal im Paper-Modus starten:

```powershell
python main.py --multi --interval 60
```

2. In Telegram nacheinander testen:
   - `/start`
   - `/status`
   - `/summary`
   - `/risk`
   - `/positions`

Wenn keine Antwort kommt: `ENABLE_TELEGRAM`, Token, Chat-ID und `TELEGRAM_PANEL_ENABLED` prüfen.

##### Technischer E2E-Startablauf (erwartete Logs)

Beim Start mit aktiviertem Panel sollten in der Konsole folgende Punkte erscheinen:

1. `Telegram-Startup: enabled=True | panel=True | token=erkannt | chat_id=erkannt`
2. `Telegram-Notifier Init | enabled=True ...`
3. `Telegram-Panel Init | telegram_enabled=True | panel_enabled=True ...`
4. `Telegram-Control-Panel Polling-Thread gestartet.`
5. `Telegram Polling gestartet.`
6. `Multi-Bot bereit.`

Wenn stattdessen `deaktiviert` oder `token/chat_id fehlt` erscheint, ist die `.env` noch nicht korrekt gesetzt.

Wenn `HTTP 401 Unauthorized` erscheint, ist der Token ungültig (BotFather-Token erneuern).
Wenn `HTTP 409 Conflict` erscheint, läuft bereits ein anderer Poller/Prozess mit demselben Bot-Token.
Dann nur eine Instanz laufen lassen (oder den alten Prozess beenden).

#### 6.5 Verfügbare Commands

- **Info / Monitoring**  
  `/start`, `/help`, `/status`, `/summary`, `/mode`, `/strategy`, `/risk`, `/balance`, `/positions`, `/trades`, `/logs`
- **Steuerung (sicherheitsbegrenzt)**  
  `/pause`, `/resume`, `/riskoff`, `/riskon`, `/setmode paper`, `/setstrategy <name>`, `/stop_bot`, `/start_bot` *(nur wenn Start-Callback angebunden ist)*

#### 6.6 Sicherheitsgrenzen

- Live-Modus kann **nicht** per Telegram aktiviert werden (`/setmode` erlaubt nur `paper`)
- Telegram muss explizit aktiviert werden (`ENABLE_TELEGRAM=true`)
- Panel muss explizit aktiviert werden (`TELEGRAM_PANEL_ENABLED=true`)
- Optionales Whitelisting über `TELEGRAM_PANEL_ALLOWED_IDS` (empfohlen)
- Risk- und Pause-Flags blockieren nur **neue Entries**; offene Positionen bleiben verwaltet
- Telegram-Versand ist fehlertolerant und darf den Trading-Loop nicht crashen

#### 6.7 Zusammenspiel mit Paper-Trading

- Telegram steuert primär den lokalen Paper-Betrieb:
  - Status/Trades/Positionen kommen aus Runtime-Status + SQLite-DB
  - Steuerbefehle (`/pause`, `/riskoff`) greifen auf Runtime-Control und Risk-Gates
- Ergebnis: Bedienung über Telegram ohne direkte Vermischung mit Order-Engine-Logik

---

## Konfiguration (.env)

| Variable                  | Standard        | Beschreibung                          |
|---------------------------|-----------------|---------------------------------------|
| `EXCHANGE`                | `binance`       | Exchange-Name (ccxt-kompatibel)       |
| `API_KEY`                 | –               | API-Schlüssel der Börse               |
| `API_SECRET`              | –               | API-Secret der Börse                  |
| `BOT_MODE`                | `paper`         | Bevorzugter Betriebsmodus: `paper` oder `live` |
| `TRADING_MODE`            | `paper`         | Kompatibilitätsalias zu `BOT_MODE`    |
| `LIVE_TRADING_ENABLED`    | `false`         | Zusätzlicher Safety-Guard: Live nur bei expliziter Freigabe |
| `ENFORCE_SINGLE_INSTANCE` | `true`          | Verhindert parallele Bot-Prozesse (Telegram-Polling-Konflikte) |
| `APP_INSTANCE_LOCKFILE`   | `data/app.lock` | Lockfile-Pfad für Single-Instance-Schutz |
| `TRADING_PAIRS`           | `BTC/USDT,...`  | Kommagetrennte Handelspaare           |
| `TIMEFRAME`               | `1h`            | Kerzen-Zeitrahmen                     |
| `MAX_POSITION_SIZE_PERCENT` | `2.0`         | Max. Kapitaleinsatz pro Trade (%)     |
| `MAX_OPEN_TRADES`         | `5`             | Max. gleichzeitige offene Positionen  |
| `STOP_LOSS_PERCENT`       | `2.0`           | Stop-Loss in %                        |
| `TAKE_PROFIT_PERCENT`     | `4.0`           | Take-Profit in %                      |
| `STRATEGY`                | `rsi_ema`       | `rsi_ema`, `macd_crossover`, `combined`, `auto` (Multi-Mode) |
| `PAPER_TRADING_BALANCE`   | `10000.0`       | Startkapital für Paper-Trading (USDT) |
| `ENABLE_TELEGRAM`         | `false`         | Telegram global aktivieren/deaktivieren |
| `TELEGRAM_ENABLED`        | `false`         | Kompatibilitätsalias für `ENABLE_TELEGRAM` |
| `ENABLE_NOTIFICATIONS`    | `true`          | Telegram-Benachrichtigungen global an/aus |
| `TELEGRAM_NOTIFY_LEVEL`   | `trading`       | `off`, `critical`, `trading`, `all` |
| `TELEGRAM_ERROR_ALERT_COOLDOWN_SEC` | `120` | Cooldown für wiederholte Error-Alerts |
| `RISK_PER_TRADE_PCT`      | `1.0`           | Max. Risiko je Trade (% vom Konto, Backtest/Multi-Mode) |
| `MAX_TOTAL_OPEN_RISK_PCT` | `10.0`          | Max. Summe aller offenen SL-Risiken (% vom Konto) |
| `MAX_POSITIONS_TOTAL`     | `5`             | Max. Gesamtzahl gleichzeitiger Positionen (Multi-Mode) |
| `DAILY_LOSS_LIMIT_PCT`    | `5.0`           | Tagesverlust-Limit in % des Startkapitals |
| `COIN_COOLDOWN_MINUTES`   | `60`            | Cooldown nach Trade auf demselben Symbol (Minuten) |
| `STRATEGY_COOLDOWN_MINUTES` | `30`          | Cooldown nach Verlust-Trade pro Strategie (Minuten) |
| `RISK_BLOCK_HIGH_VOLATILITY` | `false`      | `true` = neue Trades im HIGH_VOLATILITY-Regime blockieren |
| `STRATEGY_MIN_PERF_SCORE` | `0.0`           | Optionaler Performance-Gate \[0.0–1.0], z.B. 0.4 = Strategien mit Score < 0.4 werden im Meta-Selector ignoriert |
| `BRAIN_MIN_SCORE_TO_TRADE` | `0.45`         | Brain-Gate: Mindestscore für Entry-Freigabe |
| `BRAIN_RISKY_PHASE_SCORE` | `0.35`          | Unterhalb dieses Scores gilt die Phase als riskant |
| `TELEGRAM_PANEL_ENABLED`  | `false`         | `true` = Telegram-Control-Panel aktiviert (Polling) |
| `TELEGRAM_PANEL_POLL_INTERVAL_SEC` | `10`   | Poll-Intervall des Panels (Sekunden) |
| `TELEGRAM_PANEL_LOG_LINES` | `20`          | Anzahl Log-Zeilen für `/logs` |
| `TELEGRAM_PANEL_ALLOWED_IDS` | –           | Kommagetrennte Chat-/User-IDs, die Befehle senden dürfen |

---

## Paper-Modus: aktueller Funktionsstand

- Lädt OHLCV-Daten über `ccxt` (öffentliche Endpunkte im Paper-Modus)
- Regime-Erkennung + Multi-Strategie-Signale
- Intelligence-Brain + Meta-Selector mit Eligibility/Scoring und Performance-Gate
- Risk-Engine vor jeder Ausführung (Cooldowns, Daily-Loss, Duplikat-Block, Max-Positions)
- Portfolio-Sizing und Exposure-Limits
- Virtuelle Trades (open/close), PnL und Equity-Update
- Runtime-State + Telegram-Panel mit echten Statusdaten
- App-Orchestrierung mit Single-Instance-Lock für stabile lokale Runs

## Live-Modus: bewusst eingeschränkt

- Live wird **nicht** über Telegram aktiviert (`/setmode` nur `paper`)
- Live benötigt explizite Konfiguration (`BOT_MODE=live`, API-Key/Secret)
- `SHORT` im echten Futures-Live ist weiterhin nicht vollständig implementiert
- Projekt ist aktuell auf stabilen Paper-Betrieb ausgelegt, nicht auf produktiven Echtgeldbetrieb

---

## Strategien

### RSI + EMA (`rsi_ema`)
- **Kauf**: RSI unter 30 (oversold) + EMA9 kreuzt über EMA21
- **Verkauf**: RSI über 70 (overbought) ODER EMA bearisches Kreuz

### MACD Crossover (`macd_crossover`)
- **Kauf**: MACD-Linie kreuzt über Signal-Linie
- **Verkauf**: MACD-Linie kreuzt unter Signal-Linie

### Kombiniert (`combined`)
- Beide Strategien müssen übereinstimmen → höhere Zuverlässigkeit

---

## Hinweis

> **WARNUNG**: Trading mit echtem Geld birgt erhebliche finanzielle Risiken.
> Teste den Bot immer zuerst im Paper-Trading-Modus (`BOT_MODE=paper`).
> Der Autor übernimmt keine Haftung für finanzielle Verluste.
