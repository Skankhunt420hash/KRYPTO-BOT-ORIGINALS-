import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    EXCHANGE: str = os.getenv("EXCHANGE", "binance")
    API_KEY: str = os.getenv("API_KEY", "")
    API_SECRET: str = os.getenv("API_SECRET", "")

    TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")

    TRADING_PAIRS: list = os.getenv(
        "TRADING_PAIRS", "BTC/USDT,ETH/USDT"
    ).split(",")

    TIMEFRAME: str = os.getenv("TIMEFRAME", "1h")

    MAX_POSITION_SIZE_PERCENT: float = float(
        os.getenv("MAX_POSITION_SIZE_PERCENT", 2.0)
    )
    MAX_OPEN_TRADES: int = int(os.getenv("MAX_OPEN_TRADES", 5))
    STOP_LOSS_PERCENT: float = float(os.getenv("STOP_LOSS_PERCENT", 2.0))
    TAKE_PROFIT_PERCENT: float = float(os.getenv("TAKE_PROFIT_PERCENT", 4.0))
    TRAILING_STOP: bool = os.getenv("TRAILING_STOP", "false").lower() == "true"

    PAPER_TRADING_BALANCE: float = float(
        os.getenv("PAPER_TRADING_BALANCE", 10000.0)
    )

    STRATEGY: str = os.getenv("STRATEGY", "rsi_ema")

    # Telegram: automatisch aktiv wenn TOKEN + CHAT_ID gesetzt sind.
    # TELEGRAM_ENABLED=false deaktiviert explizit (z.B. für Tests).
    TELEGRAM_ENABLED: bool = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    # Mindest-Konfidenz (0-100) damit ein Signal eine Telegram-Meldung auslöst
    TELEGRAM_MIN_CONFIDENCE: float = float(os.getenv("TELEGRAM_MIN_CONFIDENCE", 50.0))

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///data/trades.db")

    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

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

    # Wartezeit nach Schließung einer Position auf demselben Coin (Minuten)
    COIN_COOLDOWN_MINUTES: int = int(os.getenv("COIN_COOLDOWN_MINUTES", 60))

    # Wartezeit nach einem Verlust-Trade für dieselbe Strategie (Minuten)
    STRATEGY_COOLDOWN_MINUTES: int = int(os.getenv("STRATEGY_COOLDOWN_MINUTES", 30))

    # Schutz vor doppelten Signalen: gleiche Strategie + Symbol in N Minuten (Minuten)
    DUPLICATE_SIGNAL_MINUTES: int = int(os.getenv("DUPLICATE_SIGNAL_MINUTES", 15))

    # ------------------------------------------------------------------
    # SHORT-Trading Einstellungen
    # ------------------------------------------------------------------

    # SHORT im Paper-Modus immer erlaubt (Simulation). Im Live-Modus nur
    # wenn FUTURES_MODE=true, sonst blockiert (Spot kann nicht shorten).
    SHORT_ENABLED: bool = os.getenv("SHORT_ENABLED", "true").lower() == "true"

    # True = Futures-/Margin-Konto (SHORT live ausführbar)
    # False = Spot-Konto (SHORT nur im Paper-Modus simulierbar)
    FUTURES_MODE: bool = os.getenv("FUTURES_MODE", "false").lower() == "true"

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
    PERF_SELECTOR_WEIGHT: float = float(os.getenv("PERF_SELECTOR_WEIGHT", 0.15))


settings = Settings()
