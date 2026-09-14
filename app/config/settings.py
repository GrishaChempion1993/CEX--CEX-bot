"""Configuration settings loaded from environment variables"""
import json
import os
from pathlib import Path
from typing import List
from dotenv import load_dotenv

# Load .env file
load_dotenv()


def _parse_float_map(raw: str) -> dict[str, float]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return {str(k).strip().lower(): float(v) for k, v in loaded.items()}
    except Exception:
        pass
    result: dict[str, float] = {}
    for chunk in text.split(","):
        piece = chunk.strip()
        if not piece or ":" not in piece:
            continue
        key, value = piece.split(":", 1)
        try:
            result[key.strip().lower()] = float(value.strip())
        except ValueError:
            continue
    return result


class Config:
    """Application configuration"""
    
    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_MID_SPREAD_CHAT_ID: str = os.getenv("TELEGRAM_MID_SPREAD_CHAT_ID", "").strip()
    TELEGRAM_MID_SPREAD_MIN_PCT: float = float(os.getenv("TELEGRAM_MID_SPREAD_MIN_PCT", "3.0"))
    TELEGRAM_MID_SPREAD_MAX_PCT: float = float(os.getenv("TELEGRAM_MID_SPREAD_MAX_PCT", "10.0"))

    # Telegram (trader service)
    TRADER_TELEGRAM_BOT_TOKEN: str = os.getenv("TRADER_TELEGRAM_BOT_TOKEN", "").strip() or TELEGRAM_BOT_TOKEN
    TRADER_TELEGRAM_CHAT_ID: str = os.getenv("TRADER_TELEGRAM_CHAT_ID", "").strip() or TELEGRAM_CHAT_ID

    # Telegram (CEX spread scanner)
    CEX_SPREAD_TELEGRAM_BOT_TOKEN: str = (
        os.getenv("CEX_SPREAD_TELEGRAM_BOT_TOKEN", "").strip() or TELEGRAM_BOT_TOKEN
    )
    CEX_SPREAD_TELEGRAM_CHAT_ID: str = (
        os.getenv("CEX_SPREAD_TELEGRAM_CHAT_ID", "").strip() or TELEGRAM_CHAT_ID
    )

    # CEX-CEX spread scanner
    CEX_SPREAD_ENABLED: bool = os.getenv("CEX_SPREAD_ENABLED", "false").lower() == "true"
    CEX_SPREAD_EXCHANGES: List[str] = [
        x.strip().lower()
        for x in os.getenv(
            "CEX_SPREAD_EXCHANGES",
            "bybit,okx,gate,bitget,blofin,kucoin,htx,bingx,phemex,bitmart,coinex,lbank",
        ).split(",")
        if x.strip()
    ]
    CEX_SPREAD_POLL_INTERVAL_SEC: float = float(os.getenv("CEX_SPREAD_POLL_INTERVAL_SEC", "5"))
    CEX_SPREAD_REGISTRY_REFRESH_SEC: int = int(os.getenv("CEX_SPREAD_REGISTRY_REFRESH_SEC", "3600"))
    CEX_SPREAD_MIN_SPREAD_OPEN_PCT: float = float(os.getenv("CEX_SPREAD_MIN_SPREAD_OPEN_PCT", "3.0"))
    CEX_SPREAD_MIN_SPREAD_CLOSE_PCT: float = float(os.getenv("CEX_SPREAD_MIN_SPREAD_CLOSE_PCT", "0.3"))
    CEX_SPREAD_CLOSE_RATIO: float = float(os.getenv("CEX_SPREAD_CLOSE_RATIO", "0.80"))
    CEX_SPREAD_MAX_DATA_AGE_SEC: float = float(os.getenv("CEX_SPREAD_MAX_DATA_AGE_SEC", "10"))
    CEX_SPREAD_MIN_VOLUME_USD: float = float(os.getenv("CEX_SPREAD_MIN_VOLUME_USD", "400000"))
    CEX_SPREAD_MIN_BOOK_LIQUIDITY_USD: float = float(os.getenv("CEX_SPREAD_MIN_BOOK_LIQUIDITY_USD", "2000"))
    CEX_SPREAD_CONFIRM_ROUNDS: int = int(os.getenv("CEX_SPREAD_CONFIRM_ROUNDS", "2"))
    CEX_SPREAD_CONFIRM_DELAY_MS: int = int(os.getenv("CEX_SPREAD_CONFIRM_DELAY_MS", "1500"))
    CEX_SPREAD_CONFIRM_CONCURRENCY: int = int(os.getenv("CEX_SPREAD_CONFIRM_CONCURRENCY", "8"))
    CEX_SPREAD_MAX_MEDIAN_DEVIATION_PCT: float = float(os.getenv("CEX_SPREAD_MAX_MEDIAN_DEVIATION_PCT", "20"))
    CEX_SPREAD_VENUE_ERROR_LIMIT: int = int(os.getenv("CEX_SPREAD_VENUE_ERROR_LIMIT", "3"))
    CEX_SPREAD_VENUE_COOLDOWN_SEC: int = int(os.getenv("CEX_SPREAD_VENUE_COOLDOWN_SEC", "300"))
    CEX_SPREAD_MAX_SIGNAL_LIFETIME_SEC: int = int(os.getenv("CEX_SPREAD_MAX_SIGNAL_LIFETIME_SEC", "1800"))
    CEX_SPREAD_REDIS_ENABLED: bool = os.getenv("CEX_SPREAD_REDIS_ENABLED", "true").lower() == "true"
    CEX_SPREAD_STREAM_KEY: str = os.getenv("CEX_SPREAD_STREAM_KEY", "parser.signals.cex_spread")
    CEX_SPREAD_STREAM_MAXLEN: int = int(
        os.getenv("CEX_SPREAD_STREAM_MAXLEN", os.getenv("SIGNAL_STREAM_MAXLEN", "10000"))
    )
    CEX_SPREAD_DRY_RUN: bool = os.getenv("CEX_SPREAD_DRY_RUN", "false").lower() == "true"
    CEX_SPREAD_LOG_LEVEL: str = os.getenv("CEX_SPREAD_LOG_LEVEL", "INFO").upper()
    CEX_SPREAD_LOG_FILE: str = os.getenv("CEX_SPREAD_LOG_FILE", "").strip()
    CEX_SPREAD_CACHE_ENABLED: bool = os.getenv("CEX_SPREAD_CACHE_ENABLED", "true").lower() == "true"
    CEX_SPREAD_CACHE_FILE: str = os.getenv("CEX_SPREAD_CACHE_FILE", "").strip()
    CEX_SPREAD_CLOSE_DURATION_STATS_FILE: Path = Path(
        os.getenv(
            "CEX_SPREAD_CLOSE_DURATION_STATS_FILE",
            str(Path(__file__).parent.parent / "data" / "cex_spread_close_duration_stats.json"),
        )
    )
    
    # Spread thresholds
    MIN_SPREAD_PCT: float = float(os.getenv("MIN_SPREAD_PCT", "6.0"))
    SPREAD_DIRECTION_MODE: str = os.getenv("SPREAD_DIRECTION_MODE", "short_only").strip().lower()

    # Signal stream (Redis)
    SIGNAL_STREAM_ENABLED: bool = os.getenv("SIGNAL_STREAM_ENABLED", "false").lower() == "true"
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    SIGNAL_STREAM_KEY: str = os.getenv("SIGNAL_STREAM_KEY", "parser.signals")
    SIGNAL_STREAM_GROUP: str = os.getenv("SIGNAL_STREAM_GROUP", "trader")
    SIGNAL_STREAM_MAXLEN: int = int(os.getenv("SIGNAL_STREAM_MAXLEN", "10000"))
    SIGNAL_LEVERAGE: float = float(os.getenv("SIGNAL_LEVERAGE", "1.0"))
    
    # DEX filters
    DEX_MIN_LIQ_USD: float = float(os.getenv("DEX_MIN_LIQ_USD", "50000"))
    DEX_MIN_VOL_USD: float = float(os.getenv("DEX_MIN_VOL_USD", "30000"))
    DEX_PROXY_URL: str = os.getenv("DEX_PROXY_URL", "").strip()
    DEX_PROXY_RATIO: float = float(os.getenv("DEX_PROXY_RATIO", "1.0"))
    DEX_PROXY_POOL: str = os.getenv("DEX_PROXY_POOL", "").strip()

    # Proxy pool (shared across services; DexScreener uses DEX_PROXY_POOL if set)
    PROXY_POOL: str = os.getenv("PROXY_POOL", "").strip()
    PROXY_POOL_MODE: str = os.getenv("PROXY_POOL_MODE", "round_robin").strip().lower()
    PROXY_POOL_COOLDOWN_SEC: float = float(os.getenv("PROXY_POOL_COOLDOWN_SEC", "120"))
    _MEXC_USE_PROXY_DEFAULT: bool = os.getenv("MEXC_USE_PROXY", "false").lower() == "true"
    MEXC_USE_PROXY: bool = _MEXC_USE_PROXY_DEFAULT
    MEXC_REST_USE_PROXY: bool = os.getenv(
        "MEXC_REST_USE_PROXY",
        "true" if _MEXC_USE_PROXY_DEFAULT else "false",
    ).lower() == "true"
    MEXC_WS_USE_PROXY: bool = os.getenv(
        "MEXC_WS_USE_PROXY",
        "true" if _MEXC_USE_PROXY_DEFAULT else "false",
    ).lower() == "true"
    
    # Data freshness
    MAX_DATA_AGE_SEC: int = int(os.getenv("MAX_DATA_AGE_SEC", "3"))
    
    # Deduplication
    RESEND_COOLDOWN_SEC: int = int(os.getenv("RESEND_COOLDOWN_SEC", "60"))
    RESEND_SPREAD_DELTA_PCT: float = float(os.getenv("RESEND_SPREAD_DELTA_PCT", "2.0"))
    
    # Timing
    REGISTRY_REFRESH_SEC: int = int(os.getenv("REGISTRY_REFRESH_SEC", "86400"))  # 24 hours (1 day)
    SCAN_INTERVAL_SEC: float = float(os.getenv("SCAN_INTERVAL_SEC", "1.5"))
    HTTP_TIMEOUT_SEC: int = int(os.getenv("HTTP_TIMEOUT_SEC", "8"))
    
    # Concurrency
    MAX_CONCURRENCY: int = int(os.getenv("MAX_CONCURRENCY", "8"))
    DEX_CACHE_TTL_SEC: float = float(os.getenv("DEX_CACHE_TTL_SEC", "4"))
    DEX_MIN_INTERVAL_SEC: float = float(os.getenv("DEX_MIN_INTERVAL_SEC", "1.0"))
    DEX_MIN_INTERVAL_MAX_SEC: float = float(os.getenv("DEX_MIN_INTERVAL_MAX_SEC", "5.0"))
    DEX_BACKOFF_MULT: float = float(os.getenv("DEX_BACKOFF_MULT", "2.5"))
    DEX_COOLDOWN_SEC: float = float(os.getenv("DEX_COOLDOWN_SEC", "45"))
    DEX_JITTER_PCT: float = float(os.getenv("DEX_JITTER_PCT", "0.2"))
    DEX_BATCH_SIZE: int = int(os.getenv("DEX_BATCH_SIZE", "30"))  # max 30 per DexScreener API
    DEX_BATCH_POLL_INTERVAL_SEC: float = float(os.getenv("DEX_BATCH_POLL_INTERVAL_SEC", "1.0"))  # delay between full scans
    DEX_BATCH_COLLECT_SEC: float = float(os.getenv("DEX_BATCH_COLLECT_SEC", "0.5"))  # batch collection window for confirm
    DEX_BATCH_COLLECT_MAX: int = int(os.getenv("DEX_BATCH_COLLECT_MAX", "30"))  # max candidates per confirm batch
    MEXC_TICKER_CACHE_TTL_SEC: float = float(os.getenv("MEXC_TICKER_CACHE_TTL_SEC", "0.25"))
    MEXC_WS_ENABLED: bool = os.getenv("MEXC_WS_ENABLED", "true").lower() == "true"
    MEXC_WS_URL: str = os.getenv("MEXC_WS_URL", "wss://contract.mexc.com/edge")
    MEXC_WS_PING_SEC: float = float(os.getenv("MEXC_WS_PING_SEC", "15"))
    MEXC_WS_RECONNECT_SEC: float = float(os.getenv("MEXC_WS_RECONNECT_SEC", "5"))
    MEXC_FORCE_IPV4: bool = os.getenv("MEXC_FORCE_IPV4", "false").lower() == "true"
    MEXC_WS_BACKEND: str = os.getenv("MEXC_WS_BACKEND", "aiohttp").lower()
    MEXC_WS_HEADLESS: bool = os.getenv("MEXC_WS_HEADLESS", "true").lower() == "true"
    WATCHLIST_BATCH_SIZE: int = int(os.getenv("WATCHLIST_BATCH_SIZE", "25"))
    BACKGROUND_BATCH_SIZE: int = int(os.getenv("BACKGROUND_BATCH_SIZE", "10"))
    WATCHLIST_INTERVAL_SEC: float = float(os.getenv("WATCHLIST_INTERVAL_SEC", "6"))
    BACKGROUND_INTERVAL_SEC: float = float(os.getenv("BACKGROUND_INTERVAL_SEC", "20"))
    MEXC_MIN_VOL_USD: float = float(os.getenv("MEXC_MIN_VOL_USD", "100000"))

    # Streaming (DexPaprika)
    STREAM_ENABLED: bool = os.getenv("STREAM_ENABLED", "false").lower() == "true"
    STREAM_URL: str = os.getenv("STREAM_URL", "https://streaming.dexpaprika.com/stream")
    STREAM_ASSETS_BATCH: int = int(os.getenv("STREAM_ASSETS_BATCH", "50"))
    STREAM_PRICE_TTL_SEC: float = float(os.getenv("STREAM_PRICE_TTL_SEC", "4"))
    STREAM_SPREAD_TRIGGER_PCT: float = float(os.getenv("STREAM_SPREAD_TRIGGER_PCT", "0.3"))
    STREAM_HEARTBEAT_SEC: float = float(os.getenv("STREAM_HEARTBEAT_SEC", "15"))
    STREAM_RECONNECT_DELAY_SEC: float = float(os.getenv("STREAM_RECONNECT_DELAY_SEC", "5"))
    STREAM_HISTORY_LIMIT: int = int(os.getenv("STREAM_HISTORY_LIMIT", "1"))
    STREAM_RESTART_SEC: float = float(os.getenv("STREAM_RESTART_SEC", "300"))
    PAPRIKA_VALIDATE_DELAY_SEC: float = float(os.getenv("PAPRIKA_VALIDATE_DELAY_SEC", "0.25"))
    PAPRIKA_VALIDATE_TTL_SEC: int = int(os.getenv("PAPRIKA_VALIDATE_TTL_SEC", "86400"))
    STREAM_ALLOWED_CHAINS: List[str] = [
        c.strip().lower() for c in os.getenv(
            "STREAM_ALLOWED_CHAINS",
            "unichain,plasma,bsc,arbitrum,fantom,sui,blast,sei,hyperevm,ethereum,optimism,base,scroll,ton,botanix,avalanche,aptos,solana,tron,sonic,linea,cronos,ronin,monad,polygon,zksync,celo,berachain,mantle,bob_network"
        ).split(",") if c.strip()
    ]
    CANDIDATE_QUEUE_MAX: int = int(os.getenv("CANDIDATE_QUEUE_MAX", "200"))
    CANDIDATE_DEDUP_TTL_SEC: float = float(os.getenv("CANDIDATE_DEDUP_TTL_SEC", "45"))
    CANDIDATE_WORKERS: int = int(os.getenv("CANDIDATE_WORKERS", "2"))
    CONFIRM_CONCURRENCY: int = int(os.getenv("CONFIRM_CONCURRENCY", "6"))

    # Scoring / watchlist
    SCORE_DECAY: float = float(os.getenv("SCORE_DECAY", "0.9"))
    SCORE_DECAY_INTERVAL_SEC: int = int(os.getenv("SCORE_DECAY_INTERVAL_SEC", "1800"))  # 30 min
    SCORE_EVENT_TTL_SEC: int = int(os.getenv("SCORE_EVENT_TTL_SEC", "3600"))  # 60 min
    SCORE_VOL_SPIKE_RATIO: float = float(os.getenv("SCORE_VOL_SPIKE_RATIO", "0.02"))  # 2% of 24h vol in window
    SCORE_VOL_SPIKE_MIN_USD: float = float(os.getenv("SCORE_VOL_SPIKE_MIN_USD", "50000"))
    SCORE_PRICE_SPIKE_PCT: float = float(os.getenv("SCORE_PRICE_SPIKE_PCT", "2.0"))  # % change threshold
    SCORE_HIT_INCREMENT: float = float(os.getenv("SCORE_HIT_INCREMENT", "5.0"))
    SCORE_CANDIDATE_INCREMENT: float = float(os.getenv("SCORE_CANDIDATE_INCREMENT", "1.0"))
    
    # Chains
    PRIORITY_CHAINS: List[str] = [
        chain.strip() for chain in os.getenv("PRIORITY_CHAINS", "solana,bsc").split(",")
    ]
    # Priority pairs file (MEXC symbols) - optional
    PRIORITY_PAIRS_FILE: Path = Path(os.getenv("PRIORITY_PAIRS_FILE", "pairs.txt"))
    
    # Paths
    DATA_DIR: Path = Path(__file__).parent.parent / "data"
    CACHE_FILE: Path = DATA_DIR / "official_tokens.json"
    PAPRIKA_CACHE_FILE: Path = DATA_DIR / "paprika_tokens.json"
    PAPRIKA_CACHE_TTL_SEC: int = int(os.getenv("PAPRIKA_CACHE_TTL_SEC", "86400"))
    PAPRIKA_VALID_CACHE_FILE: Path = DATA_DIR / "paprika_valid_tokens.json"
    PAPRIKA_INVALID_CACHE_FILE: Path = DATA_DIR / "paprika_invalid_tokens.json"
    CLOSE_DURATION_STATS_FILE: Path = DATA_DIR / "close_duration_stats.json"
    
    # MEXC symbol overrides (can be extended)
    MEXC_SYMBOL_OVERRIDES: dict[str, str] = {}
    
    # MEXC API Keys (REQUIRED for token registry)
    MEXC_API_KEY: str = os.getenv("MEXC_API_KEY", "")
    MEXC_API_SECRET: str = os.getenv("MEXC_API_SECRET", "")

    # MEXC web (futures private) auth
    MEXC_WEB_TOKEN: str = os.getenv("MEXC_WEB_TOKEN", "")
    MEXC_WEB_TOKEN_FILE: str = os.getenv("MEXC_WEB_TOKEN_FILE", "")
    MEXC_WEB_BASE_URL: str = os.getenv("MEXC_WEB_BASE_URL", "https://futures.mexc.com/api/v1")
    MEXC_WEB_TIMEOUT_SEC: int = int(os.getenv("MEXC_WEB_TIMEOUT_SEC", "30"))
    MEXC_WEB_USER_AGENT: str = os.getenv(
        "MEXC_WEB_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    MEXC_WEB_PROXY: str = os.getenv("MEXC_WEB_PROXY", "")

    # MEXC web stop/plan order endpoint (from browser)
    MEXC_WEB_STOP_BASE_URL: str = os.getenv(
        "MEXC_WEB_STOP_BASE_URL",
        "https://www.mexc.com/api/platform/futures/api/v1",
    )
    MEXC_WEB_MTOKEN: str = os.getenv("MEXC_WEB_MTOKEN", "")
    MEXC_WEB_P0: str = os.getenv("MEXC_WEB_P0", "")
    MEXC_WEB_K0: str = os.getenv("MEXC_WEB_K0", "")
    MEXC_WEB_CHASH: str = os.getenv("MEXC_WEB_CHASH", "")
    MEXC_WEB_MHASH: str = os.getenv("MEXC_WEB_MHASH", "")

    # MEXC web auto-refresh (Playwright + TOTP)
    MEXC_WEB_REFRESH_ENABLED: bool = os.getenv("MEXC_WEB_REFRESH_ENABLED", "false").lower() == "true"
    MEXC_LOGIN_URL: str = os.getenv("MEXC_LOGIN_URL", "https://www.mexc.com/login")
    MEXC_LOGIN_EMAIL: str = os.getenv("MEXC_LOGIN_EMAIL", "")
    MEXC_LOGIN_PASSWORD: str = os.getenv("MEXC_LOGIN_PASSWORD", "")
    MEXC_TOTP_SECRET: str = os.getenv("MEXC_TOTP_SECRET", "")
    MEXC_WEB_HEADLESS: bool = os.getenv("MEXC_WEB_HEADLESS", "true").lower() == "true"
    MEXC_TOKEN_STORAGE_KEYS: List[str] = [
        k.strip() for k in os.getenv(
            "MEXC_TOKEN_STORAGE_KEYS",
            "Authorization,authorization,token,access_token",
        ).split(",") if k.strip()
    ]

    # Trader settings
    TRADER_POSITION_PCT: float = float(os.getenv("TRADER_POSITION_PCT", "0.1"))
    TRADER_MIN_USD: float = float(os.getenv("TRADER_MIN_USD", "1.0"))
    TRADER_MIN_ONLY: bool = os.getenv("TRADER_MIN_ONLY", "false").lower() == "true"
    TRADER_ENTRY_ORDER_TYPE: str = os.getenv("TRADER_ENTRY_ORDER_TYPE", "market").strip().lower()
    TRADER_IOC_SLIPPAGE_PCT: float = float(os.getenv("TRADER_IOC_SLIPPAGE_PCT", "0.0"))
    TRADER_LIMIT_TTL_SEC: float = float(os.getenv("TRADER_LIMIT_TTL_SEC", "10"))
    TRADER_LEVERAGE: int = int(os.getenv("TRADER_LEVERAGE", "1"))
    TRADER_SCALE_IN_PCT: float = float(os.getenv("TRADER_SCALE_IN_PCT", "0.02"))
    TRADER_STOP_LOSS_PCT: float = float(os.getenv("TRADER_STOP_LOSS_PCT", "0.03"))
    TRADER_STOP_LOSS_ENABLED: bool = os.getenv("TRADER_STOP_LOSS_ENABLED", "true").lower() == "true"
    TRADER_MIN_AVG_CLOSE_SEC: float = float(os.getenv("TRADER_MIN_AVG_CLOSE_SEC", "30"))
    TRADER_MAX_POSITION_SEC: int = int(os.getenv("TRADER_MAX_POSITION_SEC", "60000"))
    TRADER_STATUS_POLL_SEC: float = float(os.getenv("TRADER_STATUS_POLL_SEC", "5"))
    TRADER_LOG_LEVEL: str = os.getenv("TRADER_LOG_LEVEL", "INFO").upper()
    TRADER_LOG_FILE: str = os.getenv("TRADER_LOG_FILE", "").strip()
    TRADER_LOG_HTTP: bool = os.getenv("TRADER_LOG_HTTP", "false").lower() == "true"

    # Dex trader settings
    DEX_TRADER_ENABLED: bool = os.getenv("DEX_TRADER_ENABLED", "false").lower() == "true"
    DEX_TRADER_STREAM_GROUP: str = os.getenv("DEX_TRADER_STREAM_GROUP", "dex_trader")
    DEX_TRADER_FILL_TIMEOUT_SEC: int = int(os.getenv("DEX_TRADER_FILL_TIMEOUT_SEC", "40"))
    DEX_TRADER_POLL_SEC: float = float(os.getenv("DEX_TRADER_POLL_SEC", "2"))
    DEX_TRADER_BUY_ATTEMPTS: int = int(os.getenv("DEX_TRADER_BUY_ATTEMPTS", "10"))
    DEX_TRADER_SLIPPAGE_PCT: float = float(os.getenv("DEX_TRADER_SLIPPAGE_PCT", "0.005"))
    DEX_TRADER_SLIPPAGE_MAX_PCT: float = float(os.getenv("DEX_TRADER_SLIPPAGE_MAX_PCT", "0.01"))
    DEX_TRADER_SELL_RETRIES: int = int(os.getenv("DEX_TRADER_SELL_RETRIES", "4"))
    DEX_TRADER_SELL_SLIPPAGE_STEP_PCT: float = float(os.getenv("DEX_TRADER_SELL_SLIPPAGE_STEP_PCT", "0.005"))
    DEX_TRADER_SELL_SLIPPAGE_MAX_PCT: float = float(os.getenv("DEX_TRADER_SELL_SLIPPAGE_MAX_PCT", "0.05"))
    DEX_TRADER_STOP_LOSS_PCT: float = float(os.getenv("DEX_TRADER_STOP_LOSS_PCT", "0.015"))
    DEX_TRADER_MIN_HOLD_SEC: float = float(os.getenv("DEX_TRADER_MIN_HOLD_SEC", "30"))
    DEX_TRADER_STOP_POLL_SEC: float = float(os.getenv("DEX_TRADER_STOP_POLL_SEC", "2.5"))
    DEX_TRADER_STOP_PRICE_STALE_SEC: float = float(os.getenv("DEX_TRADER_STOP_PRICE_STALE_SEC", "20"))
    DEX_TRADER_PRIORITY_FEE_PCT: float = float(os.getenv("DEX_TRADER_PRIORITY_FEE_PCT", "0.001"))
    DEX_TRADER_DRY_RUN: bool = os.getenv("DEX_TRADER_DRY_RUN", "true").lower() == "true"
    DEX_TRADER_NOTIFY_SKIPS: bool = os.getenv("DEX_TRADER_NOTIFY_SKIPS", "true").lower() == "true"
    DEX_TRADER_HEARTBEAT_SEC: int = int(os.getenv("DEX_TRADER_HEARTBEAT_SEC", "900"))
    DEX_TRADER_STRICT_CONFIG: bool = os.getenv("DEX_TRADER_STRICT_CONFIG", "true").lower() == "true"
    DEX_TRADER_ERROR_THROTTLE_SEC: float = float(os.getenv("DEX_TRADER_ERROR_THROTTLE_SEC", "60"))
    DEX_TRADER_MIN_FILL_RATIO: float = float(os.getenv("DEX_TRADER_MIN_FILL_RATIO", "0.98"))
    DEX_TRADER_CLEAR_BALANCE_EPS: float = float(os.getenv("DEX_TRADER_CLEAR_BALANCE_EPS", "0"))
    DEX_TRADER_BUY_CHUNK_FACTORS: List[float] = [
        float(x.strip()) for x in os.getenv("DEX_TRADER_BUY_CHUNK_FACTORS", "0.4,0.3,0.2,0.1").split(",") if x.strip()
    ]
    DEX_TRADER_SELL_CHUNK_FACTORS: List[float] = [
        float(x.strip()) for x in os.getenv("DEX_TRADER_SELL_CHUNK_FACTORS", "0.5,0.3,0.2").split(",") if x.strip()
    ]

    # Dex trader wallets / RPCs
    DEX_SOLANA_RPC_URL: str = os.getenv("DEX_SOLANA_RPC_URL", "").strip()
    DEX_SOLANA_USDT_MINT: str = os.getenv(
        "DEX_SOLANA_USDT_MINT",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    )
    DEX_SOLANA_MIN_SOL: float = float(os.getenv("DEX_SOLANA_MIN_SOL", "0.003"))
    DEX_SOLANA_COMMITMENT: str = os.getenv("DEX_SOLANA_COMMITMENT", "confirmed").strip().lower()
    DEX_SOLANA_CONFIRM_TIMEOUT_SEC: float = float(os.getenv("DEX_SOLANA_CONFIRM_TIMEOUT_SEC", "25"))
    DEX_JUPITER_BASE_URL: str = os.getenv("DEX_JUPITER_BASE_URL", "https://api.jup.ag/swap/v1")
    DEX_JUPITER_API_KEY: str = os.getenv("DEX_JUPITER_API_KEY", "")
    DEX_BSC_RPC_URL: str = (
        os.getenv("DEX_BSC_RPC_URL")
        or os.getenv("BSC_RPC_URL")
        or "https://bsc-dataseed.binance.org"
    ).strip()
    DEX_BASE_RPC_URL: str = (
        os.getenv("DEX_BASE_RPC_URL")
        or os.getenv("BASE_RPC_URL")
        or "https://mainnet.base.org"
    ).strip()
    DEX_SOLANA_PRIVATE_KEY: str = os.getenv("DEX_SOLANA_PRIVATE_KEY", "").strip()
    DEX_EVM_PRIVATE_KEY: str = (os.getenv("DEX_EVM_PRIVATE_KEY") or os.getenv("METAMASK_PRIVATE_KEY", "")).strip()
    DEX_0X_API_KEY: str = os.getenv("DEX_0X_API_KEY", "").strip()
    DEX_0X_TIMEOUT_SEC: float = float(os.getenv("DEX_0X_TIMEOUT_SEC", "15"))
    DEX_0X_RETRIES: int = int(os.getenv("DEX_0X_RETRIES", "3"))
    DEX_0X_BSC_BASE_URL: str = (os.getenv("DEX_0X_BSC_BASE_URL", "https://api.0x.org").strip() or "https://api.0x.org")
    DEX_0X_BASE_BASE_URL: str = (os.getenv("DEX_0X_BASE_BASE_URL", "https://api.0x.org").strip() or "https://api.0x.org")
    DEX_0X_BUY_RETRIES: int = int(os.getenv("DEX_0X_BUY_RETRIES", "4"))
    DEX_0X_BUY_SLIPPAGE_STEP_PCT: float = float(os.getenv("DEX_0X_BUY_SLIPPAGE_STEP_PCT", "0.002"))
    DEX_0X_MAX_SLIPPAGE_PCT: float = float(os.getenv("DEX_0X_MAX_SLIPPAGE_PCT", "0.03"))
    DEX_0X_EXCLUDED_SOURCES: str = os.getenv("DEX_0X_EXCLUDED_SOURCES", "").strip()
    DEX_BSC_ROUTER: str = os.getenv("DEX_BSC_ROUTER", "")
    DEX_BSC_FACTORY: str = os.getenv("DEX_BSC_FACTORY", "")
    DEX_BSC_V3_FEES: List[int] = [
        int(x.strip())
        for x in os.getenv("DEX_BSC_V3_FEES", "100,500,2500,10000").split(",")
        if x.strip().isdigit()
    ]
    DEX_BASE_ROUTER: str = os.getenv("DEX_BASE_ROUTER", "")
    DEX_BASE_FACTORY: str = os.getenv("DEX_BASE_FACTORY", "0x420DD381b31aEf6683db6B902084cB0FFECe40Da")
    DEX_JUPITER_HTTP_TIMEOUT_SEC: float = float(os.getenv("DEX_JUPITER_HTTP_TIMEOUT_SEC", "15"))
    DEX_JUPITER_QUOTE_RETRIES: int = int(os.getenv("DEX_JUPITER_QUOTE_RETRIES", "3"))
    DEX_JUPITER_SWAP_RETRIES: int = int(os.getenv("DEX_JUPITER_SWAP_RETRIES", "3"))
    DEX_JUPITER_RPC_RETRIES: int = int(os.getenv("DEX_JUPITER_RPC_RETRIES", "3"))
    DEX_JUPITER_MAX_ACCOUNTS: int = int(os.getenv("DEX_JUPITER_MAX_ACCOUNTS", "64"))
    DEX_JUPITER_MAX_PRIORITY_LAMPORTS: int = int(os.getenv("DEX_JUPITER_MAX_PRIORITY_LAMPORTS", "200000"))
    DEX_JUPITER_PRIORITY_LEVEL: str = os.getenv("DEX_JUPITER_PRIORITY_LEVEL", "high").strip().lower()
    DEX_JUPITER_USE_DYNAMIC_SLIPPAGE: bool = os.getenv("DEX_JUPITER_USE_DYNAMIC_SLIPPAGE", "true").lower() == "true"
    DEX_JUPITER_TARGET_FILL_RATIO: float = float(os.getenv("DEX_JUPITER_TARGET_FILL_RATIO", "0.98"))
    DEX_JUPITER_BALANCE_POLL_SEC: float = float(os.getenv("DEX_JUPITER_BALANCE_POLL_SEC", "0.35"))
    DEX_JUPITER_BALANCE_POLL_ATTEMPTS: int = int(os.getenv("DEX_JUPITER_BALANCE_POLL_ATTEMPTS", "12"))

    # Spot <-> DEX arbitrage service
    SPOT_DEX_ARB_ENABLED: bool = os.getenv("SPOT_DEX_ARB_ENABLED", "false").lower() == "true"
    SPOT_DEX_ARB_DRY_RUN: bool = os.getenv("SPOT_DEX_ARB_DRY_RUN", "true").lower() == "true"
    SPOT_DEX_ARB_SCAN_INTERVAL_SEC: float = float(os.getenv("SPOT_DEX_ARB_SCAN_INTERVAL_SEC", "2.0"))
    SPOT_DEX_ARB_CONFIRM_SEC: int = int(os.getenv("SPOT_DEX_ARB_CONFIRM_SEC", "30"))
    SPOT_DEX_ARB_ROUND_RECHECK_SEC: float = float(os.getenv("SPOT_DEX_ARB_ROUND_RECHECK_SEC", "2.0"))
    SPOT_DEX_ARB_MAX_ROUNDS: int = int(os.getenv("SPOT_DEX_ARB_MAX_ROUNDS", "5"))
    SPOT_DEX_ARB_MAX_PARALLEL_CYCLES: int = int(os.getenv("SPOT_DEX_ARB_MAX_PARALLEL_CYCLES", "1"))
    SPOT_DEX_ARB_MAX_TOKENS_PER_SCAN: int = int(os.getenv("SPOT_DEX_ARB_MAX_TOKENS_PER_SCAN", "120"))
    SPOT_DEX_ARB_MIN_SPREAD_PCT: float = float(os.getenv("SPOT_DEX_ARB_MIN_SPREAD_PCT", "1.0"))
    SPOT_DEX_ARB_MIN_DEX_LIQ_USD: float = float(os.getenv("SPOT_DEX_ARB_MIN_DEX_LIQ_USD", "50000"))
    SPOT_DEX_ARB_MIN_DEX_VOL_USD: float = float(os.getenv("SPOT_DEX_ARB_MIN_DEX_VOL_USD", "30000"))
    SPOT_DEX_ARB_MIN_MEXC_VOL_USD: float = float(os.getenv("SPOT_DEX_ARB_MIN_MEXC_VOL_USD", "50000"))
    SPOT_DEX_ARB_CYCLE_USDT: float = float(os.getenv("SPOT_DEX_ARB_CYCLE_USDT", "100"))
    SPOT_DEX_ARB_MIN_CYCLE_USDT: float = float(os.getenv("SPOT_DEX_ARB_MIN_CYCLE_USDT", "10"))
    SPOT_DEX_ARB_MAX_LIQ_PCT: float = float(os.getenv("SPOT_DEX_ARB_MAX_LIQ_PCT", "0.001"))
    SPOT_DEX_ARB_MAX_VOL_PCT: float = float(os.getenv("SPOT_DEX_ARB_MAX_VOL_PCT", "0.01"))
    SPOT_DEX_ARB_SLIPPAGE_PCT: float = float(os.getenv("SPOT_DEX_ARB_SLIPPAGE_PCT", "0.005"))
    SPOT_DEX_ARB_SLIPPAGE_MAX_PCT: float = float(os.getenv("SPOT_DEX_ARB_SLIPPAGE_MAX_PCT", "0.015"))
    SPOT_DEX_ARB_PRIORITY_FEE_PCT: float = float(os.getenv("SPOT_DEX_ARB_PRIORITY_FEE_PCT", "0.001"))
    SPOT_DEX_ARB_TRANSFER_TIMEOUT_SEC: int = int(os.getenv("SPOT_DEX_ARB_TRANSFER_TIMEOUT_SEC", "900"))
    SPOT_DEX_ARB_TRANSFER_POLL_SEC: float = float(os.getenv("SPOT_DEX_ARB_TRANSFER_POLL_SEC", "5"))
    SPOT_DEX_ARB_ALLOWED_CHAINS: List[str] = [
        c.strip().lower() for c in os.getenv("SPOT_DEX_ARB_ALLOWED_CHAINS", "bsc,base,solana").split(",") if c.strip()
    ]
    SPOT_DEX_ARB_REQUIRED_QUOTE_SYMBOLS: List[str] = [
        c.strip().upper() for c in os.getenv("SPOT_DEX_ARB_REQUIRED_QUOTE_SYMBOLS", "USDT").split(",") if c.strip()
    ]
    SPOT_DEX_ARB_BSC_WALLET_ADDRESS: str = os.getenv("SPOT_DEX_ARB_BSC_WALLET_ADDRESS", "").strip()
    SPOT_DEX_ARB_BASE_WALLET_ADDRESS: str = os.getenv("SPOT_DEX_ARB_BASE_WALLET_ADDRESS", "").strip()
    SPOT_DEX_ARB_SOLANA_WALLET_ADDRESS: str = os.getenv("SPOT_DEX_ARB_SOLANA_WALLET_ADDRESS", "").strip()
    SPOT_DEX_ARB_MEXC_NETWORK_BSC: str = os.getenv("SPOT_DEX_ARB_MEXC_NETWORK_BSC", "BEP20")
    SPOT_DEX_ARB_MEXC_NETWORK_BASE: str = os.getenv("SPOT_DEX_ARB_MEXC_NETWORK_BASE", "BASE")
    SPOT_DEX_ARB_MEXC_NETWORK_SOLANA: str = os.getenv("SPOT_DEX_ARB_MEXC_NETWORK_SOLANA", "SOL")
    SPOT_DEX_ARB_TELEGRAM_BOT_TOKEN: str = os.getenv(
        "SPOT_DEX_ARB_TELEGRAM_BOT_TOKEN",
        os.getenv("TELEGRAM_BOT_TOKEN", ""),
    )
    SPOT_DEX_ARB_TELEGRAM_CHAT_ID: str = os.getenv(
        "SPOT_DEX_ARB_TELEGRAM_CHAT_ID",
        os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    # CEX spread trader settings
    CEX_SPREAD_TRADER_ENABLED: bool = os.getenv("CEX_SPREAD_TRADER_ENABLED", "false").lower() == "true"
    CEX_SPREAD_TRADER_DRY_RUN: bool = os.getenv("CEX_SPREAD_TRADER_DRY_RUN", "true").lower() == "true"
    CEX_SPREAD_TRADER_GROUP: str = os.getenv("CEX_SPREAD_TRADER_GROUP", "cex_spread_trader")
    CEX_SPREAD_TRADER_EXCHANGES: List[str] = [
        x.strip().lower()
        for x in os.getenv(
            "CEX_SPREAD_TRADER_EXCHANGES",
            "bybit,okx,gate,bitget,blofin,kucoin,htx,bingx,phemex,bitmart,coinex,lbank",
        ).split(",")
        if x.strip()
    ]
    CEX_SPREAD_TRADER_POSITION_PCT: float = float(os.getenv("CEX_SPREAD_TRADER_POSITION_PCT", "0.1"))
    CEX_SPREAD_TRADER_MIN_USD: float = float(os.getenv("CEX_SPREAD_TRADER_MIN_USD", "5.0"))
    CEX_SPREAD_TRADER_LEVERAGE: int = int(os.getenv("CEX_SPREAD_TRADER_LEVERAGE", "1"))
    CEX_SPREAD_TRADER_STOP_LOSS_PCT: float = float(os.getenv("CEX_SPREAD_TRADER_STOP_LOSS_PCT", "0.03"))
    CEX_SPREAD_TRADER_STOP_LOSS_ENABLED: bool = os.getenv("CEX_SPREAD_TRADER_STOP_LOSS_ENABLED", "true").lower() == "true"
    CEX_SPREAD_TRADER_MIN_AVG_CLOSE_SEC: float = float(os.getenv("CEX_SPREAD_TRADER_MIN_AVG_CLOSE_SEC", "30"))
    CEX_SPREAD_TRADER_MONITOR_SEC: float = float(os.getenv("CEX_SPREAD_TRADER_MONITOR_SEC", "10"))
    CEX_SPREAD_TRADER_SLIPPAGE_PCT: float = float(os.getenv("CEX_SPREAD_TRADER_SLIPPAGE_PCT", "0.002"))
    CEX_SPREAD_TRADER_ORDER_TIMEOUT_SEC: float = float(os.getenv("CEX_SPREAD_TRADER_ORDER_TIMEOUT_SEC", "300"))
    CEX_SPREAD_TRADER_DRY_RUN_BALANCE_USD: float = float(os.getenv("CEX_SPREAD_TRADER_DRY_RUN_BALANCE_USD", "1000"))
    CEX_SPREAD_TRADER_DEFAULT_TAKER_FEE_PCT: float = float(
        os.getenv("CEX_SPREAD_TRADER_DEFAULT_TAKER_FEE_PCT", "0.0006")
    )
    CEX_SPREAD_TRADER_FEE_OVERRIDES: dict[str, float] = _parse_float_map(
        os.getenv("CEX_SPREAD_TRADER_FEE_OVERRIDES", "")
    )
    CEX_SPREAD_TRADER_LOG_LEVEL: str = os.getenv("CEX_SPREAD_TRADER_LOG_LEVEL", "INFO").upper()
    CEX_SPREAD_TRADER_LOG_FILE: str = os.getenv("CEX_SPREAD_TRADER_LOG_FILE", "").strip()
    CEX_SPREAD_TRADER_TELEGRAM_BOT_TOKEN: str = (
        os.getenv("CEX_SPREAD_TRADER_TELEGRAM_BOT_TOKEN", "").strip()
        or TELEGRAM_BOT_TOKEN
    )
    CEX_SPREAD_TRADER_TELEGRAM_CHAT_ID: str = os.getenv("CEX_SPREAD_TRADER_TELEGRAM_CHAT_ID", "").strip()

    # Signal closing / monitoring
    CLOSE_SPREAD_EXIT_PCT: float = float(os.getenv("CLOSE_SPREAD_EXIT_PCT", "0.5"))
    CLOSE_SPREAD_REDUCTION_PCT: float = float(os.getenv("CLOSE_SPREAD_REDUCTION_PCT", "0.80"))
    CLOSE_TIMEOUT_SEC: int = int(os.getenv("CLOSE_TIMEOUT_SEC", "600"))
    CLOSE_POLL_INTERVAL_SEC: float = float(os.getenv("CLOSE_POLL_INTERVAL_SEC", "5"))
    CLOSE_PING_INTERVAL_SEC: int = int(os.getenv("CLOSE_PING_INTERVAL_SEC", "600"))

    # Near-spread monitoring
    NEAR_SPREAD_MARGIN_PCT: float = float(os.getenv("NEAR_SPREAD_MARGIN_PCT", "0.2"))
    NEAR_SPREAD_WATCH_SEC: int = int(os.getenv("NEAR_SPREAD_WATCH_SEC", "120"))

    # Max size heuristics
    MAX_SIZE_LIQ_PCT: float = float(os.getenv("MAX_SIZE_LIQ_PCT", "0.0001"))  # 0.01% of liquidity
    MAX_SIZE_VOL_PCT: float = float(os.getenv("MAX_SIZE_VOL_PCT", "0.01"))    # 1% of 24h volume
    MAX_SIZE_USD_CAP: float = float(os.getenv("MAX_SIZE_USD_CAP", "1000"))
    
    @classmethod
    def validate_config(cls):
        """Validate that required configuration is present"""
        if not cls.MEXC_API_KEY or not cls.MEXC_API_SECRET:
            raise ValueError(
                "MEXC_API_KEY and MEXC_API_SECRET are REQUIRED. "
                "Get them from https://www.mexc.com/user/api (need SPOT_WITHDRAW_READ permission)"
            )
        if not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are REQUIRED. "
                "Get bot token from @BotFather and chat ID from @userinfobot"
            )

    @classmethod
    def validate_cex_spread_config(cls) -> None:
        """Validate config required by the standalone CEX spread scanner."""
        if not cls.CEX_SPREAD_TELEGRAM_BOT_TOKEN:
            raise ValueError("CEX_SPREAD_TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN is required")
        if not cls.CEX_SPREAD_TELEGRAM_CHAT_ID:
            raise ValueError("CEX_SPREAD_TELEGRAM_CHAT_ID or TELEGRAM_CHAT_ID is required")
        if not cls.CEX_SPREAD_EXCHANGES:
            raise ValueError("CEX_SPREAD_EXCHANGES must contain at least one exchange")
        if cls.CEX_SPREAD_REDIS_ENABLED and not cls.REDIS_URL:
            raise ValueError("REDIS_URL is required when CEX_SPREAD_REDIS_ENABLED=true")

    @classmethod
    def validate_cex_spread_trader_config(cls) -> None:
        """Validate config for the CEX spread trader service."""
        if not cls.CEX_SPREAD_TRADER_TELEGRAM_BOT_TOKEN:
            raise ValueError("CEX_SPREAD_TRADER_TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN is required")
        if not cls.CEX_SPREAD_TRADER_TELEGRAM_CHAT_ID:
            raise ValueError("CEX_SPREAD_TRADER_TELEGRAM_CHAT_ID is required")
        if not cls.CEX_SPREAD_TRADER_EXCHANGES:
            raise ValueError("CEX_SPREAD_TRADER_EXCHANGES must contain at least one exchange")
        if not cls.REDIS_URL:
            raise ValueError("REDIS_URL is required for the CEX spread trader")
        if cls.CEX_SPREAD_TRADER_LEVERAGE != 1:
            raise ValueError("CEX_SPREAD_TRADER_LEVERAGE must be 1 for the current trader implementation")
        if cls.CEX_SPREAD_TRADER_ORDER_TIMEOUT_SEC <= 0:
            raise ValueError("CEX_SPREAD_TRADER_ORDER_TIMEOUT_SEC must be > 0")
        if cls.CEX_SPREAD_TRADER_DRY_RUN:
            if cls.CEX_SPREAD_TRADER_DRY_RUN_BALANCE_USD <= 0:
                raise ValueError("CEX_SPREAD_TRADER_DRY_RUN_BALANCE_USD must be > 0 in dry-run mode")
            return

        required_envs = {
            "bybit": ("BYBIT_API_KEY", "BYBIT_API_SECRET"),
            "okx": ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"),
            "gate": ("GATE_API_KEY", "GATE_API_SECRET"),
            "bitget": ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE"),
            "blofin": ("BLOFIN_API_KEY", "BLOFIN_API_SECRET", "BLOFIN_PASSPHRASE"),
            "kucoin": ("KUCOIN_API_KEY", "KUCOIN_API_SECRET", "KUCOIN_PASSPHRASE"),
            "htx": ("HTX_API_KEY", "HTX_API_SECRET"),
            "bingx": ("BINGX_API_KEY", "BINGX_API_SECRET"),
            "phemex": ("PHEMEX_API_KEY", "PHEMEX_API_SECRET"),
            "bitmart": ("BITMART_API_KEY", "BITMART_API_SECRET", "BITMART_MEMO"),
            "coinex": ("COINEX_API_KEY", "COINEX_API_SECRET"),
            "lbank": ("LBANK_API_KEY", "LBANK_API_SECRET"),
        }
        missing: list[str] = []
        for exchange in cls.CEX_SPREAD_TRADER_EXCHANGES:
            for env_name in required_envs.get(exchange, ()):
                if not os.getenv(env_name, "").strip():
                    missing.append(env_name)
        if missing:
            raise ValueError(
                "Missing CEX spread trader credentials for live mode: " + ", ".join(sorted(set(missing)))
            )

    @staticmethod
    def _normalized_ascii(name: str, value: str) -> str:
        import unicodedata

        text = unicodedata.normalize("NFKC", value or "")
        for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060"):
            text = text.replace(ch, "")
        text = text.strip()
        try:
            text.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(f"{name} contains non-ASCII characters: {repr(text)}") from exc
        return text

    @classmethod
    def validate_dex_trader_config(cls) -> None:
        """Strict DEX trader config validation (fail-fast)."""
        if not cls.DEX_TRADER_ENABLED or cls.DEX_TRADER_DRY_RUN:
            return

        errors: list[str] = []

        def need(name: str, value: str):
            if not (value or "").strip():
                errors.append(f"{name} is required")
                return
            try:
                cls._normalized_ascii(name, value)
            except Exception as exc:
                errors.append(str(exc))

        need("DEX_EVM_PRIVATE_KEY", cls.DEX_EVM_PRIVATE_KEY)
        need("DEX_0X_API_KEY", cls.DEX_0X_API_KEY)
        need("DEX_0X_BSC_BASE_URL", cls.DEX_0X_BSC_BASE_URL)
        need("DEX_0X_BASE_BASE_URL", cls.DEX_0X_BASE_BASE_URL)
        need("DEX_BSC_RPC_URL", cls.DEX_BSC_RPC_URL)
        need("DEX_BASE_RPC_URL", cls.DEX_BASE_RPC_URL)

        if cls.DEX_SOLANA_PRIVATE_KEY:
            # Solana is optional at runtime; validate only if key provided.
            need("DEX_SOLANA_RPC_URL", cls.DEX_SOLANA_RPC_URL or "https://api.mainnet-beta.solana.com")
            need("DEX_JUPITER_BASE_URL", cls.DEX_JUPITER_BASE_URL)

        if errors and cls.DEX_TRADER_STRICT_CONFIG:
            raise ValueError("Invalid DEX trader config: " + "; ".join(errors))
