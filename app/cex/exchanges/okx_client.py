"""OKX V5 futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://www.okx.com"


class OKXClient(BaseExchangeClient):
    EXCHANGE_NAME = "okx"

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
                raise RuntimeError(f"OKX API error: {data.get('msg')}")
            return data.get("data", [])

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        items = await self._get("/api/v5/public/instruments", {
            "instType": "SWAP",
        })
        symbols = []
        for item in items:
            state = item.get("state", "")
            ct_type = item.get("ctType", "")
            settle = item.get("settleCcy", "")
            if state != "live" or ct_type != "linear" or settle != "USDT":
                continue
            # instId like "BTC-USDT-SWAP"
            inst_id = item.get("instId", "")
            base = inst_id.split("-")[0] if "-" in inst_id else inst_id
            sym = f"{base}_USDT"
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=base,
                exchange=self.EXCHANGE_NAME,
                raw_symbol=inst_id,
                contract_multiplier=float(item.get("ctVal") or 1.0),
            ))
        logger.info("OKX: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        items = await self._get("/api/v5/market/tickers", {"instType": "SWAP"})
        tickers = {}
        for item in items:
            inst_id = item.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
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
                vol_ccy = item.get("volCcy24h")
                if vol_ccy:
                    vol = float(vol_ccy)
                bid_price = float(item.get("bidPx") or 0) or None
                ask_price = float(item.get("askPx") or 0) or None
                bid_size = float(item.get("bidSz") or 0) or None
                ask_size = float(item.get("askSz") or 0) or None
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
        raw = raw_symbol or symbol.replace("_USDT", "-USDT-SWAP")
        items = await self._get("/api/v5/market/books", {"instId": raw, "sz": "10"})
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
