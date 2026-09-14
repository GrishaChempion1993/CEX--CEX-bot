"""Core data models"""
from datetime import datetime
from uuid import uuid4
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class TokenNetwork(BaseModel):
    """Network configuration for a token from MEXC"""
    chain_id: str  # ethereum, bsc, solana, etc.
    contract: str  # Contract address (lowercase normalized)
    deposit_enabled: bool
    withdraw_enabled: bool
    confirmations: Optional[int] = None


EVM_CHAINS = {
    "ethereum",
    "bsc",
    "polygon",
    "arbitrum",
    "base",
    "optimism",
    "avalanche",
    "fantom",
    "linea",
    "scroll",
    "zksync",
    "celo",
    "mantle",
    "cronos",
    "unichain",
    "plasma",
    "blast",
    "sei",
    "hyperevm",
    "sonic",
    "ronin",
    "monad",
    "berachain",
    "bob_network",
}


def normalize_contract(chain_id: str, contract: str) -> str:
    if not contract:
        return contract
    if chain_id and chain_id.lower() in EVM_CHAINS:
        return contract.lower()
    return contract


class MEXCToken(BaseModel):
    """Token from MEXC with networks and contracts"""
    symbol: str  # BTC, ETH, etc.
    mexc_symbol: str  # BTC_USDT
    networks: List[TokenNetwork] = Field(default_factory=list)
    
    def get_contracts_dict(self) -> Dict[str, str]:
        """Convert to old format for compatibility"""
        return {net.chain_id: net.contract for net in self.networks}


# Legacy model for backward compatibility (deprecated)
class OfficialToken(BaseModel):
    """Legacy token model (deprecated, use MEXCToken instead)"""
    id: str
    symbol: str
    name: str
    contracts: Dict[str, str] = Field(default_factory=dict)  # chain_id -> contract_address


class DexQuote(BaseModel):
    """DEX quote from DexScreener"""
    chain_id: str
    base_symbol: str
    base_address: str
    quote_symbol: str
    quote_address: Optional[str] = None
    pair_address: str
    dex_id: Optional[str] = None
    pair_type: Optional[str] = None  # e.g. stable/volatile
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    tx_count_24h: int
    dex_url: str
    ts: int  # Unix timestamp in milliseconds
    change_24h_pct: Optional[float] = None
    ts_missing: bool = False


class SpreadSignal(BaseModel):
    """Spread signal for arbitrage opportunity"""
    signal_id: str = Field(default_factory=lambda: uuid4().hex)
    direction: str = "short"
    spread_pct: float
    dex_price: float
    dex_url: str
    mexc_price: float
    mexc_url: str
    chain: str
    contract: str
    token_symbol: str
    dex_pair_address: Optional[str] = None
    dex_base_address: Optional[str] = None
    dex_quote_address: Optional[str] = None
    dex_quote_symbol: Optional[str] = None
    dex_id: Optional[str] = None
    dex_pair_type: Optional[str] = None
    dex_liquidity_usd: float
    dex_volume_24h_usd: float
    mexc_volume_24h_usd: Optional[float] = None
    confirmations: Optional[int] = None
    timestamp_utc: datetime = Field(default_factory=datetime.utcnow)
    dex_change_24h_pct: Optional[float] = None
    mexc_change_24h_pct: Optional[float] = None
    origin_label: Optional[str] = None  # e.g., DEX (DUMP) [M: x% VS D: y%]
    max_size_tokens: Optional[float] = None
    max_size_usd: Optional[float] = None
    message_id: Optional[int] = None
    mexc_symbol: Optional[str] = None
    dex_ts_missing: Optional[bool] = None
    dex_ts_stale: Optional[bool] = None
    funding_rate: Optional[float] = None
    funding_time_utc: Optional[datetime] = None
    avg_close_sec: Optional[float] = None
    avg_close_count: Optional[int] = None
    telegram_chat_id: Optional[str] = None


class CexSpreadLeg(BaseModel):
    """One venue leg of a CEX-CEX futures spread."""

    exchange: str
    symbol: str
    raw_symbol: Optional[str] = None
    url: Optional[str] = None
    price: float
    price_source: str = "last"
    volume_24h_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None
    funding_rate: Optional[float] = None
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    timestamp_ms: Optional[int] = None
    fetched_at_ms: Optional[int] = None


class CexSpreadSignal(BaseModel):
    """Signal model for CEX-CEX futures spread discovery."""

    signal_id: str = Field(default_factory=lambda: uuid4().hex)
    signal_family: str = "cex_cex_futures"
    event_type: str = "open"
    canonical_base: str
    quote_asset: str = "USDT"
    unit_scale: float = 1.0
    spread_open_pct: float
    spread_current_pct: float
    confidence: float = 1.0
    max_position_usd: Optional[float] = None
    avg_close_sec: Optional[float] = None
    avg_close_count: Optional[int] = None
    buy_leg: CexSpreadLeg
    sell_leg: CexSpreadLeg
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    closed_at: Optional[datetime] = None
    close_reason: Optional[str] = None
    message_id: Optional[int] = None
    telegram_chat_id: Optional[str] = None
    metadata: Dict[str, str] = Field(default_factory=dict)

    @property
    def buy_exchange(self) -> str:
        return self.buy_leg.exchange

    @property
    def sell_exchange(self) -> str:
        return self.sell_leg.exchange

    @property
    def buy_symbol(self) -> str:
        return self.buy_leg.symbol

    @property
    def sell_symbol(self) -> str:
        return self.sell_leg.symbol
