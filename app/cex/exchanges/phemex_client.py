"""Phemex futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://api.phemex.com"


class PhemexClient(BaseExchangeClient):
    EXCHANGE_NAME = "phemex"

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

    async def _get(self, path: str, params: dict = None) -> dict:
        async with self.session.get(f"{BASE_URL}{path}", params=params) as r:
            r.raise_for_status()
            data = await r.json()
            if data.get("code") not in (0, None):
                raise RuntimeError(f"Phemex API error: {data.get('msg')}")
            return data

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        data = await self._get("/public/products")
        symbols = []
        for item in data.get("data", {}).get("perpProductsV2", data.get("data", {}).get("products", [])):
            status = item.get("status", "")
            settle = item.get("settleCurrency", item.get("quoteCurrency", ""))
            sym_type = item.get("type", "")
            if status != "Listed":
                continue
            if settle != "USDT" and "Perpetual" not in sym_type:
                continue
            raw = item.get("symbol", "")  # e.g. "BTCUSDT"
            base = item.get("baseCurrency", "")
            if not base:
                base = raw.replace("USDT", "")
            sym = f"{base}_USDT"
            symbols.append(FuturesSymbol(
                symbol=sym, base_asset=base, exchange=self.EXCHANGE_NAME, raw_symbol=raw,
            ))
        logger.info("Phemex: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        data = await self._get("/md/v2/ticker/24hr/all")
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in data.get("result", []):
            raw = item.get("symbol", "")
            if not raw.endswith("USDT"):
                continue
            base = raw.replace("USDT", "")
            sym = f"{base}_USDT"
            try:
                price = float(item.get("closeRp") or item.get("lastPrice") or item.get("markPriceRp") or 0)
                if price <= 0:
                    continue
                change = None
                open_p = item.get("openRp") or item.get("openPrice") or item.get("open")
                if open_p:
                    op = float(open_p)
                    if op > 0:
                        change = ((price - op) / op) * 100
                vol = None
                vq = item.get("turnoverRv") or item.get("turnoverEv")
                if vq:
                    vol = float(vq)
                mark_price = float(item.get("markPriceRp") or 0) or None
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=now_ms,
                    change_24h_pct=change, volume_24h_usd=vol,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=raw,
                    mark_price=mark_price,
                )
            except (ValueError, TypeError):
                continue
        return tickers

    async def get_top_of_book(self, symbol: str, raw_symbol: str | None = None) -> Optional[FuturesTicker]:
        raw = (raw_symbol or symbol or "").replace("_", "")
        data = await self._get("/md/v2/orderbook", {"symbol": raw})
        result = data.get("result", {})
        book = result.get("orderbook_p", {})
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bid_price = float(bids[0][0]) if bids else None
        bid_size = float(bids[0][1]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_size = float(asks[0][1]) if asks else None
        ts = int(result.get("timestamp") or time.time() * 1000)
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
