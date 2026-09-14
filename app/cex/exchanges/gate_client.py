"""Gate.io futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://api.gateio.ws/api/v4"


class GateClient(BaseExchangeClient):
    EXCHANGE_NAME = "gate"

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def connect(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"Accept": "application/json"},
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def _get(self, path: str, params: dict = None) -> list:
        async with self.session.get(f"{BASE_URL}{path}", params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        items = await self._get("/futures/usdt/contracts")
        symbols = []
        for item in items:
            if not item.get("in_delisting", False) is False:
                continue
            name = item.get("name", "")  # e.g. "BTC_USDT"
            if not name.endswith("_USDT"):
                continue
            sym = name.upper()
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=self.base_from_symbol(sym),
                exchange=self.EXCHANGE_NAME,
                raw_symbol=name,
                contract_multiplier=float(item.get("quanto_multiplier") or 1.0),
            ))
        logger.info("Gate: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        items = await self._get("/futures/usdt/tickers")
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in items:
            contract = item.get("contract", "")
            if not contract.endswith("_USDT"):
                continue
            sym = contract.upper()
            try:
                price = float(item.get("last", 0))
                if price <= 0:
                    continue
                change = None
                chg = item.get("change_percentage")
                if chg:
                    change = float(chg)  # already in pct (e.g. "2.5" = 2.5%)
                vol = None
                vol24 = item.get("volume_24h_quote")
                if vol24:
                    vol = float(vol24)
                fr = None
                fr_raw = item.get("funding_rate")
                if fr_raw:
                    fr = float(fr_raw)
                bid_price = float(item.get("highest_bid") or 0) or None
                ask_price = float(item.get("lowest_ask") or 0) or None
                bid_size = float(item.get("highest_size") or 0) or None
                ask_size = float(item.get("lowest_size") or 0) or None
                mark_price = float(item.get("mark_price") or 0) or None
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=now_ms,
                    change_24h_pct=change, volume_24h_usd=vol, funding_rate=fr,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=contract,
                    bid_price=bid_price,
                    ask_price=ask_price,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    mark_price=mark_price,
                )
            except (ValueError, TypeError):
                continue
        return tickers

    async def get_top_of_book(self, symbol: str, raw_symbol: str | None = None) -> Optional[FuturesTicker]:
        raw = raw_symbol or symbol
        data = await self._get("/futures/usdt/order_book", {"contract": raw, "limit": "20"})
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bid = bids[0] if bids else None
        ask = asks[0] if asks else None
        bid_price = float(bid.get("p")) if isinstance(bid, dict) and bid.get("p") is not None else None
        bid_size = (
            sum(float(level.get("s") or 0) for level in bids[:5] if isinstance(level, dict))
            if bids
            else None
        )
        ask_price = float(ask.get("p")) if isinstance(ask, dict) and ask.get("p") is not None else None
        ask_size = (
            sum(float(level.get("s") or 0) for level in asks[:5] if isinstance(level, dict))
            if asks
            else None
        )
        ts = int(float(data.get("update") or data.get("current") or time.time()) * 1000)
        price = ask_price or bid_price or 0.0
        if price <= 0:
            return None
        return FuturesTicker(
            symbol=symbol,
            price=price,
            timestamp_ms=ts,
            exchange=self.EXCHANGE_NAME,
            raw_symbol=raw,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
        )
