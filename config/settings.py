import os
from dotenv import load_dotenv

load_dotenv()


def _first_non_empty(*keys: str, default: str = "") -> str:
    for key in keys:
        val = os.getenv(key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
    return default


def _env_bool(primary: str, fallback: str = "", default: bool = False) -> bool:
    raw = os.getenv(primary)
    if raw is None and fallback:
        raw = os.getenv(fallback)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    EXCHANGE: str = os.getenv("EXCHANGE", "binance")
    API_KEY: str = os.getenv("API_KEY", "")
    API_SECRET: str = os.getenv("API_SECRET", "")

    # BOT_MODE ist der bevorzugte Name; TRADING_MODE bleibt voll kompatibel.
    BOT_MODE: str = _first_non_empty("BOT_MODE", "TRADING_MODE", default="paper").lower()
    TRADING_MODE: str = BOT_MODE
    # Live-Trading muss explizit freigeschaltet werden (Safety-Guard).
    LIVE_TRADING_ENABLED: bool = _env_bool(
        "LIVE_TRADING_ENABLED", "ENABLE_LIVE_TRADING", default=False
    )
    # Mini-Live Vorstufe: zusätzliche harte Begrenzungen für kontrollierte Live-Tests
    LIVE_TEST_MODE: bool = _env_bool("LIVE_TEST_MODE", default=False)
    # Maximales Notional (Quote, z.B. USDT) pro Live-Test-Order
    LIVE_MAX_POSITION_SIZE: float = float(os.getenv("LIVE_MAX_POSITION_SIZE", 25.0))
    # Optional: nur diese Strategien im Mini-Live zulassen (kommagetrennt, leer = alle)
    LIVE_ALLOWED_STRATEGIES: str = os.getenv("LIVE_ALLOWED_STRATEGIES", "")
    # Strengeres Daily-Loss-Limit nur für Mini-Live
    LIVE_TEST_DAILY_LOSS_LIMIT_PCT: float = float(
        os.getenv("LIVE_TEST_DAILY_LOSS_LIMIT_PCT", 1.0)
    )
    # Harte Live-Risk-Prüfung vor echten Orders
    LIVE_HARD_RISK_GATE_ENABLED: bool = _env_bool(
        "LIVE_HARD_RISK_GATE_ENABLED", default=True
    )
    # Mindestanforderungen für Live-Kapital
    LIVE_MIN_ACCOUNT_EQUITY_USDT: float = float(
        os.getenv("LIVE_MIN_ACCOUNT_EQUITY_USDT", 100.0)
    )
    LIVE_MIN_FREE_CAPITAL_USDT: float = float(
        os.getenv("LIVE_MIN_FREE_CAPITAL_USDT", 25.0)
    )
    # Maximal erlaubte Verlustserie (global) bevor neue Entries blockiert werden
    LIVE_MAX_LOSING_STREAK: int = int(os.getenv("LIVE_MAX_LOSING_STREAK", 3))
    # Leere Liste = keine zusätzliche Symbol-Whitelist
    LIVE_ALLOWED_SYMBOLS: str = os.getenv("LIVE_ALLOWED_SYMBOLS", "")

    TRADING_PAIRS: list = os.getenv(
        "TRADING_PAIRS", "BTC/USDT,ETH/USDT"
    ).split(",")

    # Dynamisches Universum statt fester TRADING_PAIRS (z.B. alle Kraken-Linear-Perps).
    # Leer = nur manuelle TRADING_PAIRS.
    # kraken_perps: Kraken Futures linear USD (EXCHANGE=krakenfutures)
    # binance_usdm: Binance USDT-M linear Perps (EXCHANGE=binance, FUTURES_MODE=true)
    TRADING_UNIVERSE: str = os.getenv("TRADING_UNIVERSE", "").strip()
    # Optional: Obergrenze für Scan-Umfang (0 = alle Symbole des Universums, alphabetisch sortiert)
    TRADING_UNIVERSE_MAX_SYMBOLS: int = int(
        os.getenv("TRADING_UNIVERSE_MAX_SYMBOLS", "0")
    )

    TIMEFRAME: str = os.getenv("TIMEFRAME", "1h")

    MAX_POSITION_SIZE_PERCENT: float = float(
        os.getenv("MAX_POSITION_SIZE_PERCENT", 2.0)
    )
    MAX_OPEN_TRADES: int = int(os.getenv("MAX_OPEN_TRADES", 5))
    STOP_LOSS_PERCENT: float = float(os.getenv("STOP_LOSS_PERCENT", 2.0))
    TAKE_PROFIT_PERCENT: float = float(os.getenv("TAKE_PROFIT_PERCENT", 4.0))
    TRAILING_STOP: bool = os.getenv("TRAILING_STOP", "false").lower() == "true"

    # Heuristische Mindest-„Gewinnchance“ (0–100, siehe src/utils/win_chance.py).
    # Trades mit niedrigerer Kennzahl werden nicht eröffnet. 0 = Filter deaktiviert.
    MIN_WIN_CHANCE_PCT: float = float(os.getenv("MIN_WIN_CHANCE_PCT", "80"))

    PAPER_TRADING_BALANCE: float = float(
        os.getenv("PAPER_TRADING_BALANCE", 10000.0)
    )
    # Paper: Balance = Equity; kein Abzug des vollen Notionals beim Open (nur PnL beim Close).
    PAPER_EQUITY_ACCOUNT: bool = _env_bool("PAPER_EQUITY_ACCOUNT", default=True)

    STRATEGY: str = os.getenv("STRATEGY", "rsi_ema")

    # Telegram Hauptschalter:
    # - bevorzugt: ENABLE_TELEGRAM
    # - kompatibel: TELEGRAM_ENABLED
    ENABLE_TELEGRAM: bool = _env_bool(
        "ENABLE_TELEGRAM", "TELEGRAM_ENABLED", default=False
    )
    TELEGRAM_ENABLED: bool = ENABLE_TELEGRAM

    # Benachrichtigungs-Hauptschalter:
    # - bevorzugt: ENABLE_NOTIFICATIONS
    # - wenn false: alle Telegram-Notifications aus (Panel kann weiterhin antworten)
    ENABLE_NOTIFICATIONS: bool = _env_bool("ENABLE_NOTIFICATIONS", default=True)

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    # Mindest-Konfidenz (0-100) damit ein Signal eine Telegram-Meldung auslöst
    TELEGRAM_MIN_CONFIDENCE: float = float(os.getenv("TELEGRAM_MIN_CONFIDENCE", 50.0))
    # Benachrichtigungslevel:
    # off      -> keine Telegram-Meldungen
    # critical -> nur kritische Risk-/Error-Events
    # trading  -> trade-relevante Meldungen (Default)
    # all      -> inkl. Runtime-/Control-Events (pause, strategy change, ...)
    TELEGRAM_NOTIFY_LEVEL: str = os.getenv("TELEGRAM_NOTIFY_LEVEL", "trading").lower()
    if not ENABLE_NOTIFICATIONS:
        TELEGRAM_NOTIFY_LEVEL = "off"
    # Cooldown für Error-Alerts (verhindert Spam bei wiederholten Exceptions)
    TELEGRAM_ERROR_ALERT_COOLDOWN_SEC: int = int(
        os.getenv("TELEGRAM_ERROR_ALERT_COOLDOWN_SEC", 120)
    )

    # Telegram-Control-Panel: optionales Bedien-Interface
    TELEGRAM_PANEL_ENABLED: bool = (
        os.getenv("TELEGRAM_PANEL_ENABLED", "false").lower() == "true"
    )
    TELEGRAM_PANEL_POLL_INTERVAL_SEC: int = int(
        os.getenv("TELEGRAM_PANEL_POLL_INTERVAL_SEC", 10)
    )
    TELEGRAM_PANEL_LOG_LINES: int = int(
        os.getenv("TELEGRAM_PANEL_LOG_LINES", 20)
    )
    # Kommagetrennte Liste von Chat-/User-IDs, die das Panel bedienen dürfen.
    # Leer = kein Whitelisting (nicht empfohlen in produktiven Umgebungen).
    TELEGRAM_PANEL_ALLOWED_IDS: str = os.getenv("TELEGRAM_PANEL_ALLOWED_IDS", "")

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/trades.db")

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    # App-Orchestrierung / Prozess-Schutz
    ENFORCE_SINGLE_INSTANCE: bool = _env_bool(
        "ENFORCE_SINGLE_INSTANCE", default=True
    )
    APP_INSTANCE_LOCKFILE: str = os.getenv(
        "APP_INSTANCE_LOCKFILE", "data/app.lock"
    )
    # Recovery-/Restart-Schutz: persistierter Control-State + Positions-Rekonstruktion
    STATE_RECOVERY_ENABLED: bool = _env_bool("STATE_RECOVERY_ENABLED", default=True)
    STATE_RECOVERY_FILE: str = os.getenv(
        "STATE_RECOVERY_FILE", "data/runtime_recovery.json"
    )
    RECOVERY_MAX_OPEN_TRADES_RESTORE: int = int(
        os.getenv("RECOVERY_MAX_OPEN_TRADES_RESTORE", 100)
    )
    # Exchange-Read-Retry (nur read-only Calls, keine Mehrfachorders)
    EXCHANGE_READ_RETRY_MAX: int = int(os.getenv("EXCHANGE_READ_RETRY_MAX", 2))
    EXCHANGE_READ_RETRY_BACKOFF_SEC: float = float(
        os.getenv("EXCHANGE_READ_RETRY_BACKOFF_SEC", 1.5)
    )
    # Duplicate-Order-Schutzfenster (Sekunden)
    EXCHANGE_DUPLICATE_WINDOW_SEC: int = int(
        os.getenv("EXCHANGE_DUPLICATE_WINDOW_SEC", 15)
    )
    # Supervisor/Controller: Startparameter für den separaten Bot-Prozess
    SUPERVISOR_BOT_ARGS: str = os.getenv(
        "SUPERVISOR_BOT_ARGS", "--multi --interval 60"
    )
    SUPERVISOR_PIDFILE: str = os.getenv(
        "SUPERVISOR_PIDFILE", "data/bot_process.pid"
    )
    SUPERVISOR_BOT_LOGFILE: str = os.getenv(
        "SUPERVISOR_BOT_LOGFILE", "logs/bot_process.log"
    )

    RSI_PERIOD: int = 14
    RSI_OVERSOLD: float = 30.0
    RSI_OVERBOUGHT: float = 70.0

    EMA_SHORT: int = 9
    EMA_LONG: int = 21

    MACD_FAST: int = 12
    MACD_SLOW: int = 26
    MACD_SIGNAL: int = 9

    CANDLE_LIMIT: int = 200

    # ------------------------------------------------------------------
    # Multi-Strategy / Meta-Selector Einstellungen
    # ------------------------------------------------------------------

    # STRATEGY=auto aktiviert den Multi-Strategy-Modus mit Meta-Selector
    # Einzelne Strategien: rsi_ema, macd_crossover, combined
    # Multi-Modus:         auto

    # Mindest-Konfidenz für aktionsfähige Signale (0-100)
    MIN_CONFIDENCE: float = float(os.getenv("MIN_CONFIDENCE", 40.0))

    # Mindest-RR für aktionsfähige Signale
    MIN_RR: float = float(os.getenv("MIN_RR", 1.5))

    # ------------------------------------------------------------------
    # Risk Engine Cooldowns & Limits
    # ------------------------------------------------------------------

    # Tagesverlust-Limit in % des Startkapitals (danach kein neues Trading)
    DAILY_LOSS_LIMIT_PCT: float = float(os.getenv("DAILY_LOSS_LIMIT_PCT", 5.0))

    # Optionaler Volatilitäts-Stop: blockiert neue Trades in HIGH_VOLATILITY-Regimes
    # (Regime wird von RegimeEngine erkannt und im EnhancedSignal.regime gespeichert)
    RISK_BLOCK_HIGH_VOLATILITY: bool = (
        os.getenv("RISK_BLOCK_HIGH_VOLATILITY", "false").lower() == "true"
    )

    # Wartezeit nach Schließung einer Position auf demselben Coin (Minuten)
    COIN_COOLDOWN_MINUTES: int = int(os.getenv("COIN_COOLDOWN_MINUTES", 60))

    # Wartezeit nach einem Verlust-Trade für dieselbe Strategie (Minuten)
    STRATEGY_COOLDOWN_MINUTES: int = int(os.getenv("STRATEGY_COOLDOWN_MINUTES", 30))

    # Schutz vor doppelten Signalen: gleiche Strategie + Symbol in N Minuten (Minuten)
    DUPLICATE_SIGNAL_MINUTES: int = int(os.getenv("DUPLICATE_SIGNAL_MINUTES", 15))

    # Wiederholte Verluste gleiche Strategie + Symbol: Entry sperren (Lernschicht)
    LOSS_PATTERN_MEMORY_ENABLED: bool = _env_bool("LOSS_PATTERN_MEMORY_ENABLED", default=True)
    LOSS_PATTERN_MEMORY_FILE: str = os.getenv(
        "LOSS_PATTERN_MEMORY_FILE", "data/loss_pattern_memory.json"
    )
    LOSS_PATTERN_WINDOW_HOURS: float = float(os.getenv("LOSS_PATTERN_WINDOW_HOURS", "72"))
    # Ab so vielen Verlusten im Fenster wird neu eröffnet blockiert (z. B. 2 = ab dem 3. Versuch)
    LOSS_PATTERN_MAX_LOSSES: int = int(os.getenv("LOSS_PATTERN_MAX_LOSSES", "2"))

    # ------------------------------------------------------------------
    # SHORT-Trading Einstellungen
    # ------------------------------------------------------------------

    # SHORT im Paper-Modus immer erlaubt (Simulation). Im Live-Modus nur
    # wenn FUTURES_MODE=true, sonst blockiert (Spot kann nicht shorten).
    SHORT_ENABLED: bool = os.getenv("SHORT_ENABLED", "true").lower() == "true"

    # True = Futures-/Margin-Konto (SHORT live ausführbar)
    # False = Spot-Konto (SHORT nur im Paper-Modus simulierbar)
    FUTURES_MODE: bool = os.getenv("FUTURES_MODE", "false").lower() == "true"

    # Nur SHORT-Entries (Multi-Strategie-Modus: Meta-Selector wählt nur unter SHORT-Signalen).
    # Single-Strategy-Bot (z.B. rsi_ema) unterstützt keine Short-Eröffnung → Zyklus wird übersprungen.
    SHORT_ONLY_TRADING: bool = _env_bool("SHORT_ONLY_TRADING", default=False)

    # ------------------------------------------------------------------
    # Strategy Performance Tracker & Scorer
    # ------------------------------------------------------------------

    # Anzahl der letzten Trades für Rolling-Window-Metriken
    PERF_TRACKER_ROLLING_WINDOW: int = int(os.getenv("PERF_TRACKER_ROLLING_WINDOW", 20))

    # Abklingfaktor für recency-gewichtete Win-Rate (0.90 = ältere Trades werden
    # mit 0.90^n gewichtet → neuere Trades wichtiger)
    PERF_TRACKER_RECENCY_DECAY: float = float(os.getenv("PERF_TRACKER_RECENCY_DECAY", 0.90))

    # Minimale Anzahl globaler Trades bevor ein Score angewendet wird
    # (unter diesem Schwellwert: neutraler Score 0.5)
    PERF_TRACKER_MIN_TRADES: int = int(os.getenv("PERF_TRACKER_MIN_TRADES", 10))

    # Minimale Anzahl regime-spezifischer Trades für Regime-Adjustment
    PERF_TRACKER_MIN_REGIME_TRADES: int = int(os.getenv("PERF_TRACKER_MIN_REGIME_TRADES", 5))

    # Gewicht des Performance-Scores im Meta-Selector (0.0 = deaktiviert)
    # final_score = signal_score + (perf_score - 0.5) * PERF_SELECTOR_WEIGHT
    # Bei 0.15: maximale Anpassung = ±0.075 (konservativ)
    PERF_SELECTOR_WEIGHT: float = float(os.getenv("PERF_SELECTOR_WEIGHT", 0.22))

    # Optionaler harter Performance-Gate: Strategien mit einem
    # Performance-Score unterhalb dieses Werts werden im Meta-Selector
    # komplett ignoriert (Eligibility-Layer). 0.0 = deaktiviert.
    STRATEGY_MIN_PERF_SCORE: float = float(os.getenv("STRATEGY_MIN_PERF_SCORE", 0.0))
    # Optionaler Bonus für eine zur Laufzeit gesetzte Strategie-Präferenz
    # (z.B. via Telegram /setstrategy). 0.0 = deaktiviert.
    CONTROL_STRATEGY_PRIORITY_BONUS: float = float(
        os.getenv("CONTROL_STRATEGY_PRIORITY_BONUS", 0.08)
    )
    # Brain-Gate: Mindestsignal-Score fuer Entry-Freigabe (0..1)
    BRAIN_MIN_SCORE_TO_TRADE: float = float(
        os.getenv("BRAIN_MIN_SCORE_TO_TRADE", 0.45)
    )
    # Unterhalb dieses Scores gilt die Marktphase als "riskant/unsauber"
    BRAIN_RISKY_PHASE_SCORE: float = float(
        os.getenv("BRAIN_RISKY_PHASE_SCORE", 0.35)
    )

    # ------------------------------------------------------------------
    # Portfolio Risk Engine & Position Sizing
    # ------------------------------------------------------------------

    # Sizing-Modus: fixed_notional | fixed_risk_pct | confidence_scaled
    #   fixed_notional:    fester USDT-Betrag pro Trade
    #   fixed_risk_pct:    Risiko-basiert auf SL-Distanz (empfohlen)
    #   confidence_scaled: wie fixed_risk_pct, aber skaliert mit Signal-Konfidenz
    POSITION_SIZING_MODE: str = os.getenv("POSITION_SIZING_MODE", "fixed_risk_pct")

    # Fester USDT-Betrag pro Trade (nur für fixed_notional)
    FIXED_NOTIONAL_USD: float = float(os.getenv("FIXED_NOTIONAL_USD", 200.0))

    # Risiko pro Trade als % des Kontos (Basis für fixed_risk_pct + confidence_scaled)
    # Beispiel: 1.0% von 10.000 USDT = 100 USDT Risiko pro Trade
    RISK_PER_TRADE_PCT: float = float(os.getenv("RISK_PER_TRADE_PCT", 1.0))

    # Mindest- / Maximal-Positionswert in USDT
    MIN_POSITION_NOTIONAL: float = float(os.getenv("MIN_POSITION_NOTIONAL", 10.0))
    MAX_POSITION_NOTIONAL: float = float(os.getenv("MAX_POSITION_NOTIONAL", 5000.0))

    # Skalierungs-Faktor-Grenzen für confidence_scaled
    # Bei conf=40 (Minimum): CONFIDENCE_MIN_SCALE × Basisbetrag
    # Bei conf=100 (Maximum): CONFIDENCE_MAX_SCALE × Basisbetrag
    CONFIDENCE_MIN_SCALE: float = float(os.getenv("CONFIDENCE_MIN_SCALE", 0.5))
    CONFIDENCE_MAX_SCALE: float = float(os.getenv("CONFIDENCE_MAX_SCALE", 1.5))

    # Maximales Gesamt-Portfolio-Risiko (Summe aller offenen SL-Risiken, % des Kapitals)
    MAX_TOTAL_OPEN_RISK_PCT: float = float(os.getenv("MAX_TOTAL_OPEN_RISK_PCT", 10.0))

    # Max. gleichzeitige Positionen gesamt / pro Symbol / pro Strategie
    MAX_POSITIONS_TOTAL: int = int(os.getenv("MAX_POSITIONS_TOTAL", 5))
    MAX_POSITIONS_PER_SYMBOL: int = int(os.getenv("MAX_POSITIONS_PER_SYMBOL", 1))
    MAX_STRATEGY_POSITIONS: int = int(os.getenv("MAX_STRATEGY_POSITIONS", 2))

    # Max. % der Positionen in gleicher Richtung (LONG oder SHORT)
    MAX_SAME_DIRECTION_EXPOSURE_PCT: float = float(
        os.getenv("MAX_SAME_DIRECTION_EXPOSURE_PCT", 80.0)
    )

    # Max. Risiko (% des Kapitals) innerhalb eines Symbol-Clusters
    # Cluster: BTC/ETH = "majors", alles andere = "alts" (heuristisch)
    MAX_CLUSTER_RISK_PCT: float = float(os.getenv("MAX_CLUSTER_RISK_PCT", 6.0))

    # ------------------------------------------------------------------
    # Execution Quality Layer & Fail-Safes
    # ------------------------------------------------------------------

    # Anzahl der Retries bei temporären Fehlern (Timeout, Netzwerk, ...)
    EXECUTION_MAX_RETRIES: int = int(os.getenv("EXECUTION_MAX_RETRIES", 3))

    # Initiale Wartezeit (Sekunden) zwischen Retries – verdoppelt sich exponentiell
    EXECUTION_RETRY_BACKOFF_SEC: float = float(os.getenv("EXECUTION_RETRY_BACKOFF_SEC", 2.0))

    # Maximale erlaubte Preisabweichung zwischen Signal-Entry und aktuellem Ticker (%)
    # 0.0 = Prüfung deaktiviert
    MAX_ENTRY_DEVIATION_PCT: float = float(os.getenv("MAX_ENTRY_DEVIATION_PCT", 0.5))

    # Maximale Slippage-Events in einem Fenster bevor Emergency Pause
    MAX_SLIPPAGE_EVENTS_WINDOW: int = int(os.getenv("MAX_SLIPPAGE_EVENTS_WINDOW", 5))

    # Anzahl aufeinanderfolgender Execution-Fehler bis Circuit Breaker auslöst
    MAX_CONSECUTIVE_EXEC_ERRORS: int = int(os.getenv("MAX_CONSECUTIVE_EXEC_ERRORS", 5))

    # Anzahl aufeinanderfolgender Rejections bevor Emergency Pause ausgelöst wird
    MAX_CONSECUTIVE_REJECTIONS: int = int(os.getenv("MAX_CONSECUTIVE_REJECTIONS", 10))

    # Bei Execution-Fehlern Bot automatisch pausieren (Emergency Pause)
    EMERGENCY_PAUSE_ON_EXEC_ERRORS: bool = (
        os.getenv("EMERGENCY_PAUSE_ON_EXEC_ERRORS", "true").lower() == "true"
    )

    # Cooldown-Zeit des Circuit Breakers in Sekunden
    CIRCUIT_BREAKER_COOLDOWN_SEC: int = int(os.getenv("CIRCUIT_BREAKER_COOLDOWN_SEC", 300))

    # Pfad zur Kill-Switch-Datei: Bot pausiert wenn diese Datei existiert
    # Erstellen: touch ./KILL_SWITCH | Entfernen: rm ./KILL_SWITCH
    KILL_SWITCH_FILE: str = os.getenv("KILL_SWITCH_FILE", "./KILL_SWITCH")

    # ------------------------------------------------------------------
    # Health Monitor & Watchdog
    # ------------------------------------------------------------------

    # Health-Monitoring global aktivieren/deaktivieren
    HEALTH_MONITOR_ENABLED: bool = (
        os.getenv("HEALTH_MONITOR_ENABLED", "true").lower() == "true"
    )

    # Zeit (Sekunden) ohne Heartbeat bevor Warnung / Pause
    # Ein Heartbeat wird zu Beginn jedes run_cycle() gesetzt
    HEALTH_HEARTBEAT_TIMEOUT_SEC: int = int(
        os.getenv("HEALTH_HEARTBEAT_TIMEOUT_SEC", 300)
    )

    # Wie alt dürfen Marktdaten (OHLCV) maximal sein (Sekunden)
    # Abhängig vom TIMEFRAME: 1h → 4000s sinnvoll, 5m → 600s
    DATA_STALE_TIMEOUT_SEC: int = int(os.getenv("DATA_STALE_TIMEOUT_SEC", 600))

    # Wie oft Health-Snapshots geloggt werden (Sekunden)
    HEALTH_CHECK_INTERVAL_SEC: int = int(os.getenv("HEALTH_CHECK_INTERVAL_SEC", 300))

    # Zeitfenster für Error-Rate-Monitoring (Minuten)
    ERROR_WINDOW_MINUTES: int = int(os.getenv("ERROR_WINDOW_MINUTES", 30))

    # Maximale Fehler im Zeitfenster bis Status DEGRADED
    MAX_ERRORS_PER_WINDOW: int = int(os.getenv("MAX_ERRORS_PER_WINDOW", 20))

    # Maximale kritische Fehler im Zeitfenster bis Status ERROR
    MAX_CRITICAL_ERRORS_PER_WINDOW: int = int(
        os.getenv("MAX_CRITICAL_ERRORS_PER_WINDOW", 5)
    )

    # Mindestabstand zwischen gleichen Telegram-Health-Alerts (Sekunden)
    TELEGRAM_ALERT_COOLDOWN_SEC: int = int(
        os.getenv("TELEGRAM_ALERT_COOLDOWN_SEC", 300)
    )

    # Trading pausieren wenn Marktdaten stale sind (konservativ: False)
    HEALTH_PAUSE_ON_STALE_DATA: bool = (
        os.getenv("HEALTH_PAUSE_ON_STALE_DATA", "false").lower() == "true"
    )

    # Trading pausieren wenn Heartbeat-Timeout überschritten (konservativ: False)
    HEALTH_PAUSE_ON_HEARTBEAT_MISS: bool = (
        os.getenv("HEALTH_PAUSE_ON_HEARTBEAT_MISS", "false").lower() == "true"
    )

    # Ressourcenüberwachung via psutil (True = aktiviert wenn psutil installiert)
    RESOURCE_MONITOR_ENABLED: bool = (
        os.getenv("RESOURCE_MONITOR_ENABLED", "true").lower() == "true"
    )

    # Grenzwerte für Ressourcen-Warnungen
    MAX_MEMORY_PCT: float = float(os.getenv("MAX_MEMORY_PCT", 80.0))
    MAX_CPU_PCT: float = float(os.getenv("MAX_CPU_PCT", 90.0))


settings = Settings()
