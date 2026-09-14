"""Abstract base for exchange futures trading clients."""
from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.config.settings import Config

logger = logging.getLogger(__name__)


@dataclass
class ContractInfo:
    """Futures contract specification."""

    symbol: str
    raw_symbol: str = ""
    base_asset: str = ""
    quote_asset: str = "USDT"
    price_precision: int = 8
    qty_precision: int = 8
    min_qty: float = 0.0
    qty_step: float = 0.0
    price_step: float = 0.0
    contract_multiplier: float = 1.0
    max_leverage: int = 100
    taker_fee_rate: Optional[float] = None
    maker_fee_rate: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderFill:
    """One order fill in exchange-native order quantity."""

    qty: float = 0.0
    price: float = 0.0
    fee_usd: float = 0.0
    fee_currency: str = "USDT"
    timestamp_ms: int = 0
    liquidity: str = ""
    order_id: str = ""

    @property
    def notional_usd(self) -> float:
        return max(self.qty, 0.0) * max(self.price, 0.0)


@dataclass
class OrderResult:
    """Unified order execution result."""

    success: bool
    order_id: str = ""
    exchange: str = ""
    symbol: str = ""
    side: str = ""
    requested_qty: float = 0.0
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    filled_price: float = 0.0
    fees_usd: float = 0.0
    fee_currency: str = "USDT"
    status: str = ""
    order_type: str = ""
    reduce_only: bool = False
    is_final: bool = True
    fill_source: str = ""
    error_code: Optional[int] = None
    error_msg: str = ""
    fills: List[OrderFill] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def notional_usd(self) -> float:
        return max(self.filled_qty, 0.0) * max(self.filled_price, 0.0)


@dataclass
class TopOfBook:
    """Best bid/ask snapshot in exchange-native size units."""

    symbol: str
    raw_symbol: str = ""
    exchange: str = ""
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    timestamp_ms: int = 0

    def price_for_side(self, side: str) -> float:
        side = (side or "").lower()
        if side == "buy":
            return float(self.ask_price or 0.0)
        if side == "sell":
            return float(self.bid_price or 0.0)
        return 0.0

    def size_for_side(self, side: str) -> float:
        side = (side or "").lower()
        if side == "buy":
            return float(self.ask_size or 0.0)
        if side == "sell":
            return float(self.bid_size or 0.0)
        return 0.0


@dataclass
class PositionInfo:
    """Exchange position snapshot."""

    symbol: str
    exchange: str = ""
    side: str = ""  # "long" / "short"
    size: float = 0.0  # native exchange order qty / contracts
    size_usd: float = 0.0
    entry_price: float = 0.0
    mark_price: float = 0.0
    leverage: float = 1.0
    unrealized_pnl: float = 0.0
    margin: float = 0.0
    raw: Dict[str, Any] = field(default_factory=dict)


class BaseTradingClient(abc.ABC):
    """Abstract base class for exchange futures trading."""

    EXCHANGE_NAME: str = "unknown"

    @abc.abstractmethod
    async def connect(self) -> None:
        """Initialize HTTP session."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Close HTTP session."""

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()
        await self.close_market_data_client()

    async def close_market_data_client(self) -> None:
        client = getattr(self, "_public_market_client", None)
        self._public_market_client = None
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass

    # Account

    @abc.abstractmethod
    async def get_balance(self) -> float:
        """Return available USDT balance for futures trading."""

    # Contract info

    @abc.abstractmethod
    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        """Fetch contract spec for a symbol (normalized BASE_USDT)."""

    # Leverage

    @abc.abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol. Returns True on success."""

    # Orders

    @abc.abstractmethod
    async def place_market_order(
        self,
        symbol: str,
        side: str,  # "buy" / "sell"
        qty: float,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Place a market order. qty is exchange-native order size."""

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        reduce_only: bool = False,
    ) -> OrderResult:
        """Place a limit order if the venue supports it."""

        return OrderResult(
            success=False,
            exchange=self.EXCHANGE_NAME,
            symbol=symbol,
            side=side.lower(),
            requested_qty=qty,
            order_type="limit",
            reduce_only=reduce_only,
            is_final=True,
            error_msg="limit_not_supported",
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel order if the venue supports it."""

        return False

    async def get_order_status(self, symbol: str, order_id: str) -> Optional[OrderResult]:
        """Fetch order status if the venue supports it."""

        return None

    async def get_order_fills(self, symbol: str, order_id: str) -> List[OrderFill]:
        """Fetch order fills if the venue supports it."""

        return []

    # Positions

    @abc.abstractmethod
    async def get_position(self, symbol: str) -> Optional[PositionInfo]:
        """Get current open position for symbol."""

    @abc.abstractmethod
    async def get_all_positions(self) -> List[PositionInfo]:
        """Get all open positions."""

    async def close_position(self, symbol: str) -> OrderResult:
        """Close position by placing an opposing market order."""

        pos = await self.get_position(symbol)
        if not pos or pos.size <= 0:
            return OrderResult(
                success=True,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                status="no_position",
                order_type="market",
                is_final=True,
            )
        close_side = "sell" if pos.side == "long" else "buy"
        return await self.place_market_order(symbol, close_side, pos.size, reduce_only=True)

    # Capabilities / metadata

    def supports_limit_orders(self) -> bool:
        return False

    def supports_order_tracking(self) -> bool:
        return False

    def supports_fee_tracking(self) -> bool:
        return False

    def supports_funding_tracking(self) -> bool:
        return False

    async def get_top_of_book(
        self,
        symbol: str,
        raw_symbol: Optional[str] = None,
    ) -> Optional[TopOfBook]:
        """Default public BBO bridge via market-data clients."""

        market_client = await self._ensure_public_market_client()
        if market_client is None:
            return None
        try:
            ticker = await market_client.get_top_of_book(symbol, raw_symbol=raw_symbol)
        except Exception as exc:
            logger.debug("Public top-of-book failed exchange=%s symbol=%s: %s", self.EXCHANGE_NAME, symbol, exc)
            return None
        if ticker is None:
            return None
        return TopOfBook(
            symbol=symbol,
            raw_symbol=ticker.raw_symbol or raw_symbol or symbol,
            exchange=self.EXCHANGE_NAME,
            bid_price=ticker.bid_price,
            ask_price=ticker.ask_price,
            bid_size=ticker.bid_size,
            ask_size=ticker.ask_size,
            timestamp_ms=int(ticker.timestamp_ms or ticker.fetched_at_ms or time.time() * 1000),
        )

    async def get_public_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        """Best-effort contract info derived from the public market-data layer."""

        cache = getattr(self, "_public_contract_cache", None)
        if cache is None:
            cache = {}
            self._public_contract_cache = cache
        if symbol in cache:
            return cache[symbol]
        market_client = await self._ensure_public_market_client()
        if market_client is None:
            return None
        try:
            symbols = await market_client.get_futures_symbols()
        except Exception as exc:
            logger.debug("Public contract info failed exchange=%s symbol=%s: %s", self.EXCHANGE_NAME, symbol, exc)
            return None
        for item in symbols:
            if item.symbol != symbol:
                continue
            info = ContractInfo(
                symbol=item.symbol,
                raw_symbol=item.raw_symbol or item.symbol,
                base_asset=item.base_asset,
                quote_asset=item.quote_asset or "USDT",
                contract_multiplier=float(item.contract_multiplier or 1.0),
                price_precision=8,
                qty_precision=8,
            )
            cache[symbol] = info
            return info
        return None

    async def get_funding_payment(self, symbol: str, opened_at: float, closed_at: float) -> Optional[float]:
        """Best-effort realized funding for the holding interval."""

        return None

    def get_taker_fee_rate(self, symbol: str = "") -> float:
        overrides = Config.CEX_SPREAD_TRADER_FEE_OVERRIDES
        if self.EXCHANGE_NAME in overrides:
            return overrides[self.EXCHANGE_NAME]
        return Config.CEX_SPREAD_TRADER_DEFAULT_TAKER_FEE_PCT

    def estimate_fee_usd(self, notional_usd: float, symbol: str = "") -> float:
        return max(notional_usd, 0.0) * max(self.get_taker_fee_rate(symbol), 0.0)

    # Helpers

    async def _ensure_public_market_client(self):
        if getattr(self, "_public_market_client", None) is not None:
            return self._public_market_client
        try:
            from app.cex.exchanges import ALL_EXCHANGES
        except Exception:
            return None
        cls = ALL_EXCHANGES.get(self.EXCHANGE_NAME)
        if cls is None:
            return None
        try:
            client = cls()
            await client.connect()
        except Exception as exc:
            logger.debug("Public market client init failed exchange=%s: %s", self.EXCHANGE_NAME, exc)
            return None
        self._public_market_client = client
        return client

    def normalize_symbol(self, raw: str) -> str:
        raw = raw.upper().replace("-", "_").replace("/", "_")
        if raw.endswith("USDT") and "_" not in raw:
            raw = raw[:-4] + "_USDT"
        if not raw.endswith("_USDT"):
            raw = raw + "_USDT"
        return raw

    def to_raw_symbol(self, symbol: str) -> str:
        """Convert normalized BASE_USDT to exchange-native format."""

        return symbol.replace("_", "")
