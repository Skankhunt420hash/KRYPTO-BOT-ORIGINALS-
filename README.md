# KRYPTO-BOT ORIGINALS

Automatisierter Kryptowährungs-Trading-Bot mit mehreren Strategien, Risikomanagement und Paper-Trading-Modus.

---

## Funktionen

- **Mehrere Strategien**: RSI+EMA, MACD Crossover, Kombiniert
- **Risikomanagement**: Stop-Loss, Take-Profit, Trailing Stop, Positionsgrößen-Kontrolle
- **Paper-Trading**: Sicheres Testen ohne echtes Geld
- **Multi-Pair**: Mehrere Handelspaare gleichzeitig
- **Logging**: Farbige Konsolen-Ausgabe + Datei-Logs
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
│   ├── bot.py                 # Haupt-Bot-Logik
│   ├── exchange/
│   │   └── connector.py       # Exchange-Verbindung (ccxt)
│   ├── strategies/
│   │   ├── base_strategy.py   # Abstrakte Basis
│   │   ├── rsi_ema_strategy.py
│   │   ├── macd_strategy.py
│   │   └── combined_strategy.py
│   └── utils/
│       ├── logger.py          # Logging
│       └── risk_manager.py    # Risiko & Positionen
├── logs/                      # Log-Dateien
└── data/                      # Datenbank
```

---

## Schnellstart

### 1. Abhängigkeiten installieren

```bash
pip install -r requirements.txt
```

### 2. Konfiguration

```bash
cp .env.example .env
# .env mit deinen Einstellungen bearbeiten
```

### 3. Bot starten

```bash
# Paper-Trading (sicher zum Testen)
python main.py

# Nur einen Zyklus ausführen
python main.py --once

# Status anzeigen
python main.py --status

# Mit benutzerdefiniertem Interval (in Sekunden)
python main.py --interval 300
```

---

## Konfiguration (.env)

| Variable                  | Standard        | Beschreibung                          |
|---------------------------|-----------------|---------------------------------------|
| `EXCHANGE`                | `binance`       | Exchange-Name (ccxt-kompatibel)       |
| `API_KEY`                 | –               | API-Schlüssel der Börse               |
| `API_SECRET`              | –               | API-Secret der Börse                  |
| `TRADING_MODE`            | `paper`         | `paper` oder `live`                   |
| `TRADING_PAIRS`           | `BTC/USDT,...`  | Kommagetrennte Handelspaare           |
| `TIMEFRAME`               | `1h`            | Kerzen-Zeitrahmen                     |
| `MAX_POSITION_SIZE_PERCENT` | `2.0`         | Max. Kapitaleinsatz pro Trade (%)     |
| `MAX_OPEN_TRADES`         | `5`             | Max. gleichzeitige offene Positionen  |
| `STOP_LOSS_PERCENT`       | `2.0`           | Stop-Loss in %                        |
| `TAKE_PROFIT_PERCENT`     | `4.0`           | Take-Profit in %                      |
| `STRATEGY`                | `rsi_ema`       | `rsi_ema`, `macd_crossover`, `combined` |
| `PAPER_TRADING_BALANCE`   | `10000.0`       | Startkapital für Paper-Trading (USDT) |

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
> Teste den Bot immer zuerst im Paper-Trading-Modus (`TRADING_MODE=paper`).
> Der Autor übernimmt keine Haftung für finanzielle Verluste.
