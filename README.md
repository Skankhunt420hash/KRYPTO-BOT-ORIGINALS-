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

#### Kraken Perpetuals (alle Linear-Perps scannen)

Kraken-**Spot** (`EXCHANGE=kraken`) und Kraken-**Futures/Perps** (`EXCHANGE=krakenfutures`) sind in ccxt getrennt. Für alle USD-linearen Perpetuals:

```env
EXCHANGE=krakenfutures
TRADING_UNIVERSE=kraken_perps
TRADING_PAIRS=
FUTURES_MODE=true
```

Der Bot lädt beim Start die aktiven **linearen** Swap-Märkte (keine inversen Kontrakte wie `BTC/USD:BTC`). Hinweis: Viele Symbole pro Zyklus erhöhen API-Last und Laufzeit – optional `TRADING_UNIVERSE_MAX_SYMBOLS` setzen.

#### Binance USDT-M Perpetuals (alle linear scannen)

```env
EXCHANGE=binance
FUTURES_MODE=true
TRADING_UNIVERSE=binance_usdm
TRADING_PAIRS=
```

#### Lernschicht / Verluste

- **Performance-Tracker** und **PERF_SELECTOR_WEIGHT** beeinflussen die Strategiewahl mit jeder geschlossenen Trade-Historie.
- **Loss-Pattern-Memory** (`LOSS_PATTERN_MEMORY_*`): mehrere Verluste mit derselben Strategie + Symbol innerhalb des Zeitfensters blockieren neue Entries (Datei `data/loss_pattern_memory.json`).

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

#### 6.4a Empfohlener Betrieb als Controller + Bot-Prozess (Windows)

Damit Telegram auch bei gestopptem Bot weiter Befehle empfangen kann:

1. Controller starten (dauerhaft laufend):

```powershell
python controller.py
```

2. In Telegram den Hauptbot als separaten Prozess steuern:
   - `/botstart`
   - `/botstop`
   - `/botrestart`
   - `/botstatus`

Hinweis: Der vom Controller gestartete Bot-Prozess läuft mit `TELEGRAM_PANEL_ENABLED=false`,
damit es keinen `getUpdates`-Konflikt durch zwei Poller gibt. Notifications des Bots bleiben aktiv.

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
  `/pause`, `/resume`, `/riskoff`, `/riskon`, `/killswitch`, `/killswitchoff`, `/setmode paper`, `/setstrategy <name>`, `/stop_bot`, `/start_bot` *(nur wenn Start-Callback angebunden ist)*
- **Supervisor (separater Bot-Prozess)**  
  `/botstart`, `/botstop`, `/botrestart`, `/botstatus`

#### 6.6 Sicherheitsgrenzen

- Live-Modus kann **nicht** per Telegram aktiviert werden (`/setmode` erlaubt nur `paper`)
- Telegram muss explizit aktiviert werden (`ENABLE_TELEGRAM=true`)
- Panel muss explizit aktiviert werden (`TELEGRAM_PANEL_ENABLED=true`)
- Optionales Whitelisting über `TELEGRAM_PANEL_ALLOWED_IDS` (empfohlen)
- Risk- und Pause-Flags blockieren nur **neue Entries**; offene Positionen bleiben verwaltet
- Harte Live-Risk-Prüfung blockiert echte Orders bei Regelverstoß (inkl. Min-Equity/Free-Capital, Loss-Streak, Symbol-Freigabe)
- Kill-Switch stoppt neue Orders sofort über Datei-Flag (`KILL_SWITCH_FILE`)
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
| `LIVE_TEST_MODE`          | `false`         | Aktiviert Mini-Live-Vorstufe (strikter als normaler Live-Modus) |
| `LIVE_MAX_POSITION_SIZE`  | `25.0`          | Max. Notional pro echter Order im Mini-Live (USDT) |
| `LIVE_ALLOWED_STRATEGIES` | leer            | Optionale Strategie-Whitelist für Mini-Live (kommagetrennt) |
| `LIVE_TEST_DAILY_LOSS_LIMIT_PCT` | `1.0`    | Strengeres Daily-Loss-Limit für Mini-Live |
| `LIVE_HARD_RISK_GATE_ENABLED` | `true`      | Erzwingt harte Live-Risk-Prüfung vor jeder echten Entry-Order |
| `LIVE_MIN_ACCOUNT_EQUITY_USDT` | `100.0`    | Mindest-Kontoequity für Live-Entries |
| `LIVE_MIN_FREE_CAPITAL_USDT` | `25.0`      | Mindest-freies Kapital (Quote, z. B. USDT) für Live-Entries |
| `LIVE_MAX_LOSING_STREAK` | `3`            | Blockiert neue Live-Entries ab dieser globalen Verlustserie |
| `LIVE_ALLOWED_SYMBOLS`   | leer           | Optionale Symbol-Whitelist für Live (kommagetrennt) |
| `ENFORCE_SINGLE_INSTANCE` | `true`          | Verhindert parallele Bot-Prozesse (Telegram-Polling-Konflikte) |
| `APP_INSTANCE_LOCKFILE`   | `data/app.lock` | Lockfile-Pfad für Single-Instance-Schutz |
| `STATE_RECOVERY_ENABLED`  | `true`          | Persistiert Runtime-Control und aktiviert Restart-Recovery |
| `STATE_RECOVERY_FILE`     | `data/runtime_recovery.json` | JSON-Datei für persistierten Recovery-Zustand |
| `RECOVERY_MAX_OPEN_TRADES_RESTORE` | `100` | Maximale Anzahl offener DB-Trades, die beim Restart rekonstruiert werden |
| `SUPERVISOR_BOT_ARGS`     | `--multi --interval 60` | Startargumente für den Bot-Prozess aus `controller.py` |
| `SUPERVISOR_PIDFILE`      | `data/bot_process.pid` | PID-Datei des vom Controller gestarteten Bot-Prozesses |
| `SUPERVISOR_BOT_LOGFILE`  | `logs/bot_process.log` | Logdatei des separaten Bot-Prozesses |
| `EXCHANGE_READ_RETRY_MAX` | `2`             | Retry-Anzahl für read-only Exchange-Calls |
| `EXCHANGE_READ_RETRY_BACKOFF_SEC` | `1.5`    | Backoff in Sekunden für read-only Retries |
| `EXCHANGE_DUPLICATE_WINDOW_SEC` | `15`      | Duplicate-Order-Schutzfenster (Sekunden) |
| `TRADING_PAIRS`           | `BTC/USDT,...`  | Kommagetrennte Handelspaare           |
| `TRADING_UNIVERSE`        | _(leer)_        | `kraken_perps` (Kraken Futures) oder `binance_usdm` (Binance USDT-M) |
| `MIN_WIN_CHANCE_PCT`     | `80`            | Heuristische Mindest-„Gewinnchance“ für Entries (0 = aus) |
| `LOSS_PATTERN_MEMORY_ENABLED` | `true`     | Wiederholte Verluste gleiches Setup → Entry-Sperre |
| `LOSS_PATTERN_WINDOW_HOURS` | `72`         | Zeitfenster für Verlust-Zählung |
| `LOSS_PATTERN_MAX_LOSSES` | `2`             | Ab so vielen Verlusten im Fenster Sperre |
| `TRADING_UNIVERSE_MAX_SYMBOLS` | `0`        | Optional: max. Anzahl Symbole (0 = alle) |
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

## Exchange-Schicht (Read-only vs. orderfähig)

- **Read-only (mit Retry):** `fetch_ohlcv`, `fetch_ticker`, `fetch_market_price`, `fetch_balance`, `fetch_open_orders`, `fetch_open_positions`, `fetch_symbol_info`
- **Orderfähig (kontrolliert):** `create_market_buy_order`, `create_market_sell_order`, `cancel_order`
- **Checks vor Live-Order:** Precision-Rundung, Min-Amount, Min-Notional, verfügbare Balance, Duplicate-Order-Schutz, harte Live-Risk-Gate-Prüfung
- **Mini-Live (falls `LIVE_TEST_MODE=true`):** max 1 offene Position, strengeres Daily-Loss-Limit, optional Symbol-/Strategie-Whitelist, hartes Max-Notional (`LIVE_MAX_POSITION_SIZE`)
- **Safety:** Live-Order nur wenn `TRADING_MODE=live` und `LIVE_TRADING_ENABLED=true` plus API-Key/Secret
- **Restart-Recovery:** Beim Start werden persistierter Control-State, offene DB-Trades und offene Exchange-Orders/Positionen abgeglichen; bei Inkonsistenz werden Entries konservativ blockiert

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

### Paper-Trade Recording (persistente Datengrundlage)

- Speicherung erfolgt in SQLite: `data/trades.db`, Tabelle `trades`
- Offene Positionen: `status='open'`, abgeschlossene Trades: `status='closed'`
- Erfasste Kernfelder pro Paper-Trade:
  - `timestamp_open`, `timestamp_close`
  - `symbol`, `strategy_name`, `side`
  - `entry_price`, `exit_price`, `stop_loss`, `take_profit`, `position_size`
  - `pnl_abs`, `pnl_pct`, `exit_reason` (Alias zu `reason_close`)
  - `regime` (Regime bei Entry), `signal_score`, `risk_state_at_entry` (JSON)
- Telegram `/positions` und `/trades` lesen primär diese DB-Basis (Runtime nur als Fallback).

### Decision-Logging (Brain/Selector/Risk nachvollziehbar)

- Speicherung erfolgt ebenfalls in `data/trades.db`, Tabelle `decisions`.
- Pro Entscheidungszyklus (pro Symbol) werden strukturiert gespeichert:
  - `timestamp`, `mode`, `symbol`, `timeframe`
  - `detected_regime`
  - `eligible_strategies` (JSON), `strategy_ranking` (JSON)
  - `chosen_strategy`, `signal_score`
  - `risk_decision`, `allow_trade`
  - `reject_reason`, `last_decision_reason`
  - `market_context` (JSON; z. B. Volatilität/Trend/Momentum)
- Damit sind nicht nur ausgeführte Trades, sondern auch Ablehnungen und Skip-Entscheidungen auswertbar.

### Performance-Tracking (Zeitreihe + Tagesreport)

- SQLite-Zeitreihe: Tabelle `performance_snapshots` in `data/trades.db`
  - Felder u. a.: `timestamp`, `current_balance`, `current_equity`, `open_positions_count`,
    `realized_pnl_total`, `unrealized_pnl_total`, `day_pnl`, `total_trades`, `win_rate`, `max_drawdown_pct`
- Tagesaggregation: Tabelle `daily_performance` in `data/trades.db`
  - Enthält u. a. Trades/PNL des Tages, Winrate, beste/schlechteste Strategie, häufigste Ablehnungsgründe
- JSON-Export für schnellen Blick: `data/daily_summary.json`
  - Enthält die letzten Tageszusammenfassungen kompakt als `days`-Liste.

Auswertung:
- SQL: `SELECT * FROM performance_snapshots ORDER BY id DESC LIMIT 100;`
- SQL: `SELECT * FROM daily_performance ORDER BY day DESC;`
- JSON: `data/daily_summary.json` direkt im Editor öffnen.

## Live-Modus: bewusst eingeschränkt

- Live wird **nicht** über Telegram aktiviert (`/setmode` nur `paper`)
- Live benötigt explizite Konfiguration (`BOT_MODE=live`, API-Key/Secret, `LIVE_TRADING_ENABLED=true`)
- Live ist nur im Multi-Flow erlaubt (`--multi` oder `STRATEGY=auto`)
- Für sichere Vorstufe empfohlen: `LIVE_TEST_MODE=true` (Mini-Live statt normalem Live)
- `SHORT` im echten Futures-Live ist weiterhin nicht vollständig implementiert
- Projekt ist aktuell auf stabilen Paper-Betrieb ausgelegt, nicht auf produktiven Echtgeldbetrieb

### Modi im Vergleich (sicherer Übergang)

- **Paper (`BOT_MODE=paper`)**: keine echten Orders, volle Strategie-/Risk-Tests.
- **Mini-Live (`BOT_MODE=live` + `LIVE_TRADING_ENABLED=true` + `LIVE_TEST_MODE=true`)**: echte Orders mit harten Zusatzgrenzen (kleines Notional, optional Symbol-/Strategie-Whitelist, max 1 offene Position, strengeres Daily-Loss).
- **Normal Live (`BOT_MODE=live` + `LIVE_TRADING_ENABLED=true` + `LIVE_TEST_MODE=false`)**: weiterhin durch Live-Hard-Gate und Exchange-Safety geschützt, aber ohne Mini-Live-Extraklammern.

### Empfohlene Aktivierungsreihenfolge

1. **Paper stabil** (`/status`, `/risk`, `/summary` plausibel, keine Fehler-Spikes).
2. **Mini-Live sehr klein** (1 Symbol, 1 Strategie, kleines `LIVE_MAX_POSITION_SIZE`).
3. **Normal Live** erst nach mehreren sauberen Mini-Live-Tagen ohne Recovery-/Gate-Probleme.

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
