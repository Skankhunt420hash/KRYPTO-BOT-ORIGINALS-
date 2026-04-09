#!/bin/bash
# ============================================================
# KRYPTO-BOT SERVER SETUP SCRIPT
# Führt alles automatisch aus - einmal aufrufen und fertig
# ============================================================

set -e  # Stoppt bei Fehler

echo ""
echo "============================================"
echo "   KRYPTO-BOT ORIGINALS - SERVER SETUP"
echo "============================================"
echo ""

# ── 1. Neueste Version holen ──────────────────────────────
echo "[1/6] Neueste Code-Version holen..."
git fetch origin
git checkout cursor/trading-bot-einrichtung-e1b6 2>/dev/null || true
git pull origin cursor/trading-bot-einrichtung-e1b6
echo "✓ Code aktuell"

# ── 2. Python-Abhängigkeiten installieren ─────────────────
echo ""
echo "[2/6] Abhängigkeiten installieren (kann 1-2 Minuten dauern)..."
pip install -r requirements.txt -q
echo "✓ Alle Pakete installiert"

# ── 3. data/ Verzeichnis erstellen ────────────────────────
echo ""
echo "[3/6] Verzeichnisse erstellen..."
mkdir -p data logs
echo "✓ Verzeichnisse bereit"

# ── 4. .env erstellen (falls noch nicht vorhanden) ────────
echo ""
echo "[4/6] Konfiguration (.env) erstellen..."

if [ -f ".env" ]; then
    echo "   .env existiert bereits – erstelle Backup..."
    cp .env .env.backup.$(date +%Y%m%d_%H%M%S)
    echo "   Backup erstellt: .env.backup.*"
fi

cat > .env << 'ENVEOF'
# ============================================================
# KRYPTO-BOT ORIGINALS - KONFIGURATION
# ============================================================
# WICHTIG: API_KEY und API_SECRET eintragen!
# Kraken API-Key erstellen: https://www.kraken.com/u/security/api
# ============================================================

# ── Exchange ─────────────────────────────────────────────
EXCHANGE=krakenfutures
API_KEY=
API_SECRET=

# ── Trading-Modus ────────────────────────────────────────
# paper = kein echtes Geld | live = echtes Geld
TRADING_MODE=paper
FUTURES_MODE=true

# ── Pairs & Zeitrahmen ───────────────────────────────────
# Werden automatisch von Kraken gescannt (DYNAMIC_PAIRS_ENABLED=true)
TRADING_PAIRS=BTC/USD:USD,ETH/USD:USD,SOL/USD:USD,XRP/USD:USD
TIMEFRAME=5m

# ── Kapital ──────────────────────────────────────────────
PAPER_TRADING_BALANCE=10000.0
STRATEGY=auto

# ── Telegram (optional, aber empfohlen) ──────────────────
# Bot erstellen: @BotFather auf Telegram
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_MIN_CONFIDENCE=60

# ── Datenbank ────────────────────────────────────────────
DATABASE_URL=sqlite:///data/trades.db
LOG_LEVEL=INFO

# ============================================================
# RISIKO-EINSTELLUNGEN
# ============================================================

MAX_POSITION_SIZE_PERCENT=2.0
MAX_OPEN_TRADES=8
STOP_LOSS_PERCENT=1.5
TAKE_PROFIT_PERCENT=3.0
TRAILING_STOP=true

DAILY_LOSS_LIMIT_PCT=5.0
COIN_COOLDOWN_MINUTES=30
STRATEGY_COOLDOWN_MINUTES=20
DUPLICATE_SIGNAL_MINUTES=10

# ── Signal-Qualität (WICHTIG für Win-Rate) ───────────────
MIN_CONFIDENCE=40.0
MIN_RR=1.5
MIN_SIGNAL_CONFIDENCE=60.0
MIN_SIGNAL_RR=2.0

# ============================================================
# PORTFOLIO RISK ENGINE
# ============================================================

POSITION_SIZING_MODE=fixed_risk_pct
FIXED_NOTIONAL_USD=200.0
RISK_PER_TRADE_PCT=1.0
MIN_POSITION_NOTIONAL=10.0
MAX_POSITION_NOTIONAL=3000.0
CONFIDENCE_MIN_SCALE=0.5
CONFIDENCE_MAX_SCALE=1.5

MAX_TOTAL_OPEN_RISK_PCT=8.0
MAX_POSITIONS_TOTAL=8
MAX_POSITIONS_PER_SYMBOL=1
MAX_STRATEGY_POSITIONS=2
MAX_SAME_DIRECTION_EXPOSURE_PCT=75.0
MAX_CLUSTER_RISK_PCT=5.0

# Mindest-SL-Distanz (verhindert VELO-artigen Spread-SL)
MIN_SL_DISTANCE_PCT=0.3

# ============================================================
# DYNAMIC PAIR SCANNER (Kraken Perpetuals)
# ============================================================

DYNAMIC_PAIRS_ENABLED=true
DYNAMIC_PAIRS_MAX=20
DYNAMIC_PAIRS_MIN_VOLUME_USD=500000

# ============================================================
# SMART EXIT ENGINE
# ============================================================

SMART_EXIT_ENABLED=true
SMART_EXIT_ATR_MULT=1.5
SMART_EXIT_LOCK_IN_PCT=0.3
SMART_EXIT_MAX_DURATION_MIN=180

# ============================================================
# REINFORCEMENT LEARNING
# ============================================================

RL_ENABLED=true

# ============================================================
# MARKET INTELLIGENCE
# ============================================================

MARKET_INTEL_ENABLED=true

# ============================================================
# SIGNAL VALIDATOR (ADX + Volumen + Candle + Momentum)
# ============================================================

SIGNAL_VALIDATOR_ENABLED=true
SIGNAL_VALIDATOR_MIN_CHECKS=3

# ============================================================
# META-SELECTOR QUALITÄTSSCHWELLE
# ============================================================

MIN_FINAL_SCORE=0.52

# ============================================================
# WIN-RATE-TRACKER
# ============================================================

WINRATE_WINDOW=20
WINRATE_PAUSE_THRESHOLD=0.40
WINRATE_RESUME_THRESHOLD=0.50
MAX_CONSECUTIVE_LOSSES=5
LOSS_STREAK_COOLDOWN_SEC=1800

# ============================================================
# EXECUTION QUALITY & FAIL-SAFES
# ============================================================

EXECUTION_MAX_RETRIES=3
EXECUTION_RETRY_BACKOFF_SEC=2.0
MAX_ENTRY_DEVIATION_PCT=0.5
MAX_SLIPPAGE_EVENTS_WINDOW=5
MAX_CONSECUTIVE_EXEC_ERRORS=5
MAX_CONSECUTIVE_REJECTIONS=10
EMERGENCY_PAUSE_ON_EXEC_ERRORS=true
CIRCUIT_BREAKER_COOLDOWN_SEC=300
KILL_SWITCH_FILE=./KILL_SWITCH

# ============================================================
# HEALTH MONITOR & WATCHDOG
# ============================================================

HEALTH_MONITOR_ENABLED=true
HEALTH_HEARTBEAT_TIMEOUT_SEC=300
DATA_STALE_TIMEOUT_SEC=600
HEALTH_CHECK_INTERVAL_SEC=300
ERROR_WINDOW_MINUTES=30
MAX_ERRORS_PER_WINDOW=20
MAX_CRITICAL_ERRORS_PER_WINDOW=5
TELEGRAM_ALERT_COOLDOWN_SEC=300
HEALTH_PAUSE_ON_STALE_DATA=false
HEALTH_PAUSE_ON_HEARTBEAT_MISS=false
RESOURCE_MONITOR_ENABLED=true
MAX_MEMORY_PCT=80.0
MAX_CPU_PCT=90.0
ENVEOF

echo "✓ .env erstellt"

# ── 5. Kurz-Test ob der Bot startet ──────────────────────
echo ""
echo "[5/6] Teste ob Bot-Importe funktionieren..."
python3 -c "
import sys
sys.path.insert(0, '.')
from config.settings import settings
from src.engine.smart_exit import SmartExitEngine
from src.engine.rl_signal_weighter import RLSignalWeighter
from src.engine.signal_validator import SignalValidator
from src.engine.winrate_tracker import WinRateTracker
print('✓ Alle Module geladen')
print(f'  Exchange: {settings.EXCHANGE}')
print(f'  Modus: {settings.TRADING_MODE}')
print(f'  Zeitrahmen: {settings.TIMEFRAME}')
print(f'  Min-Konfidenz: {settings.MIN_SIGNAL_CONFIDENCE}')
print(f'  Smart Exit: {settings.SMART_EXIT_ENABLED}')
print(f'  RL: {settings.RL_ENABLED}')
" 2>&1 | grep -v "^$"

# ── 6. Fertig ─────────────────────────────────────────────
echo ""
echo "============================================"
echo "   SETUP ABGESCHLOSSEN!"
echo "============================================"
echo ""
echo "NÄCHSTE SCHRITTE:"
echo ""
echo "1. Kraken API-Key eintragen:"
echo "   nano .env"
echo "   → API_KEY=dein_key"
echo "   → API_SECRET=dein_secret"
echo ""
echo "2. Telegram einrichten (optional):"
echo "   → TELEGRAM_BOT_TOKEN=..."
echo "   → TELEGRAM_CHAT_ID=..."
echo ""
echo "3. Bot starten:"
echo "   python3 main.py --multi"
echo ""
echo "4. Bot im Hintergrund starten (24/7):"
echo "   screen -S krypto-bot"
echo "   python3 main.py --multi"
echo "   [Ctrl+A dann D] zum Trennen"
echo "   screen -r krypto-bot  # wieder verbinden"
echo ""
echo "5. Status prüfen:"
echo "   python3 main.py --status"
echo "   python3 main.py --strategy-stats"
echo ""
ENVEOF
