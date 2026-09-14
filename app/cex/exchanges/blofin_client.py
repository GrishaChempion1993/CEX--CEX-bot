"""BloFin futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://openapi.blofin.com"


class BloFinClient(BaseExchangeClient):
    EXCHANGE_NAME = "blofin"

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
            data = await r.json()
            if data.get("code") != "0":
                raise RuntimeError(f"BloFin API error: {data.get('msg')}")
            return data.get("data", [])

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        items = await self._get("/api/v1/market/instruments", {
            "instType": "SWAP",
        })
        symbols = []
        for item in items:
            state = item.get("state", "")
            quote = item.get("quoteCurrency", item.get("settleCcy", ""))
            if state != "live" or quote != "USDT":
                continue
            inst_id = item.get("instId", "")  # e.g. "BTC-USDT"
            base = inst_id.split("-")[0] if "-" in inst_id else inst_id
            sym = f"{base}_USDT"
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=base,
                exchange=self.EXCHANGE_NAME,
                raw_symbol=inst_id,
                contract_multiplier=float(item.get("contractValue") or 1.0),
            ))
        logger.info("BloFin: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        items = await self._get("/api/v1/market/tickers", {"instType": "SWAP"})
        tickers = {}
        for item in items:
            inst_id = item.get("instId", "")
            if not inst_id.endswith("-USDT"):
                continue
            base = inst_id.split("-")[0]
            sym = f"{base}_USDT"
            try:
                price = float(item.get("last", 0))
                if price <= 0:
                    continue
                ts = int(item.get("ts", time.time() * 1000))
                change = None
                open24 = item.get("open24h")
                if open24 and float(open24) > 0:
                    change = ((price - float(open24)) / float(open24)) * 100
                vol = None
                # BloFin uses volCurrency24h (base) * price for USD volume
                vc = item.get("volCurrency24h") or item.get("volCcy24h")
                if vc:
                    vol = float(vc) * price  # convert base volume to USD
                bid_price = float(item.get("bidPrice") or 0) or None
                ask_price = float(item.get("askPrice") or 0) or None
                bid_size = float(item.get("bidSize") or 0) or None
                ask_size = float(item.get("askSize") or 0) or None
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=ts,
                    change_24h_pct=change, volume_24h_usd=vol,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=inst_id,
                    bid_price=bid_price,
                    ask_price=ask_price,
                    bid_size=bid_size,
                    ask_size=ask_size,
                )
            except (ValueError, TypeError):
                continue
        return tickers

    async def get_top_of_book(self, symbol: str, raw_symbol: str | None = None) -> Optional[FuturesTicker]:
        raw = raw_symbol or symbol.replace("_", "-")
        items = await self._get("/api/v1/market/books", {"instId": raw, "sz": "10"})
        if not items:
            return None
        book = items[0]
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bid_price = float(bids[0][0]) if bids else None
        bid_size = sum(float(level[1]) for level in bids[:5]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_size = sum(float(level[1]) for level in asks[:5]) if asks else None
        ts = int(book.get("ts") or time.time() * 1000)
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
