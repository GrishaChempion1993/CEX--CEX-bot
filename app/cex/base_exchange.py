"""Base exchange interface for futures data collection."""
import abc
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class FuturesSymbol:
    """Unified futures symbol info."""
    symbol: str           # e.g. "BTC_USDT"
    base_asset: str       # e.g. "BTC"
    exchange: str         # e.g. "bybit"
    is_active: bool = True
    raw_symbol: str = ""
    canonical_base: str = ""
    quote_asset: str = "USDT"
    unit_scale: Optional[float] = None
    contract_multiplier: Optional[float] = None
    is_perpetual: bool = True

    def __post_init__(self) -> None:
        if not self.raw_symbol:
            self.raw_symbol = self.symbol
        identity = self.identity()
        if not self.canonical_base:
            self.canonical_base = identity.canonical_base
        if not self.quote_asset:
            self.quote_asset = identity.quote_asset
        if not self.unit_scale:
            self.unit_scale = identity.unit_scale
        if not self.contract_multiplier:
            self.contract_multiplier = identity.contract_multiplier

    def identity(self):
        """Return a canonical identity for matching and grouping."""
        from app.cex.instrument_identity import build_instrument_identity

        return build_instrument_identity(
            symbol=self.symbol,
            base_asset=self.base_asset,
            exchange=self.exchange,
            raw_symbol=self.raw_symbol or self.symbol,
            canonical_base=self.canonical_base or self.base_asset,
            quote_asset=self.quote_asset,
            unit_scale=self.unit_scale,
            contract_multiplier=self.contract_multiplier,
            is_perpetual=self.is_perpetual,
        )

    @property
    def identity_key(self) -> str:
        return self.identity().identity_key


@dataclass
class FuturesTicker:
    """Unified futures ticker."""
    symbol: str           # e.g. "BTC_USDT"
    price: float
    timestamp_ms: int
    change_24h_pct: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    funding_rate: Optional[float] = None
    exchange: str = ""
    raw_symbol: str = ""
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    mark_price: Optional[float] = None
    volume_source: str = ""
    fetched_at_ms: Optional[int] = None
    health_flags: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.raw_symbol:
            self.raw_symbol = self.symbol
        if self.fetched_at_ms is None:
            self.fetched_at_ms = self.timestamp_ms

    def has_orderbook_prices(self) -> bool:
        return self.bid_price is not None and self.ask_price is not None

    def has_executable_quotes(self) -> bool:
        return (
            self.bid_price is not None
            and self.bid_price > 0
            and self.ask_price is not None
            and self.ask_price > 0
        )

    def has_orderbook_size(self) -> bool:
        return (
            self.bid_size is not None
            and self.bid_size > 0
            and self.ask_size is not None
            and self.ask_size > 0
        )

    def price_for_side(self, side: str) -> float:
        side = (side or "").lower()
        if side == "buy" and self.ask_price is not None and self.ask_price > 0:
            return self.ask_price
        if side == "sell" and self.bid_price is not None and self.bid_price > 0:
            return self.bid_price
        if self.mark_price is not None and self.mark_price > 0:
            return self.mark_price
        return self.price

    def size_for_side(self, side: str) -> Optional[float]:
        side = (side or "").lower()
        if side == "buy":
            return self.ask_size
        if side == "sell":
            return self.bid_size
        return None

    def liquidity_usd(self, side: str, contract_multiplier: float = 1.0) -> Optional[float]:
        price = self.price_for_side(side)
        size = self.size_for_side(side)
        if price <= 0 or size is None or size <= 0:
            return None
        return price * size * max(contract_multiplier or 1.0, 0.0)

    def merged_with(self, overlay: Optional["FuturesTicker"]) -> "FuturesTicker":
        if overlay is None:
            return self
        return FuturesTicker(
            symbol=overlay.symbol or self.symbol,
            price=overlay.price if overlay.price > 0 else self.price,
            timestamp_ms=overlay.timestamp_ms or self.timestamp_ms,
            change_24h_pct=overlay.change_24h_pct if overlay.change_24h_pct is not None else self.change_24h_pct,
            volume_24h_usd=overlay.volume_24h_usd if overlay.volume_24h_usd is not None else self.volume_24h_usd,
            funding_rate=overlay.funding_rate if overlay.funding_rate is not None else self.funding_rate,
            exchange=overlay.exchange or self.exchange,
            raw_symbol=overlay.raw_symbol or self.raw_symbol,
            bid_price=overlay.bid_price if overlay.bid_price is not None else self.bid_price,
            ask_price=overlay.ask_price if overlay.ask_price is not None else self.ask_price,
            bid_size=overlay.bid_size if overlay.bid_size is not None else self.bid_size,
            ask_size=overlay.ask_size if overlay.ask_size is not None else self.ask_size,
            mark_price=overlay.mark_price if overlay.mark_price is not None else self.mark_price,
            volume_source=overlay.volume_source or self.volume_source,
            fetched_at_ms=overlay.fetched_at_ms if overlay.fetched_at_ms is not None else self.fetched_at_ms,
            health_flags=tuple(sorted(set(self.health_flags) | set(overlay.health_flags))),
        )


class BaseExchangeClient(abc.ABC):
    """Abstract base class for exchange futures data clients."""

    EXCHANGE_NAME: str = "unknown"

    @abc.abstractmethod
    async def connect(self) -> None:
        """Initialize session / connections."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Close session / connections."""

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    @abc.abstractmethod
    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        """Get list of all active futures/perpetual symbols."""

    @abc.abstractmethod
    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        """Get tickers for all futures symbols. Returns {symbol: ticker}."""

    async def get_ticker(self, symbol: str) -> Optional[FuturesTicker]:
        """Get ticker for a single symbol. Default: fetch all and filter."""
        tickers = await self.get_all_tickers()
        return tickers.get(symbol)

    async def get_top_of_book(
        self,
        symbol: str,
        raw_symbol: Optional[str] = None,
    ) -> Optional[FuturesTicker]:
        """Get a single-symbol top-of-book snapshot if supported."""
        ticker = await self.get_ticker(symbol)
        if ticker and ticker.has_orderbook_prices():
            return ticker
        return None

    def normalize_symbol(self, raw: str) -> str:
        """Normalize symbol to BASE_USDT format."""
        raw = raw.upper().replace("-", "_").replace("/", "_")
        if raw.endswith("USDT") and "_" not in raw:
            raw = raw[:-4] + "_USDT"
        if not raw.endswith("_USDT"):
            raw = raw + "_USDT"
        return raw

    def base_from_symbol(self, symbol: str) -> str:
        """Extract base asset from normalized symbol."""
        return symbol.replace("_USDT", "").replace("USDT", "")
