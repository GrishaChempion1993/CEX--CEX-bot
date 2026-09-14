"""BingX futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://open-api.bingx.com"


class BingXClient(BaseExchangeClient):
    EXCHANGE_NAME = "bingx"

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
            if data.get("code") != 0:
                raise RuntimeError(f"BingX API error: {data.get('msg')}")
            return data.get("data", {})

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        data = await self._get("/openApi/swap/v2/quote/contracts")
        symbols = []
        for item in (data if isinstance(data, list) else []):
            status = item.get("apiStateSell")  # 1 = tradeable
            currency = item.get("currency", "")
            if currency != "USDT":
                continue
            raw = item.get("symbol", "")  # e.g. "BTC-USDT"
            base = raw.split("-")[0] if "-" in raw else raw.replace("USDT", "")
            sym = f"{base}_USDT"
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=base,
                exchange=self.EXCHANGE_NAME,
                raw_symbol=raw,
                contract_multiplier=float(item.get("size") or 1.0),
            ))
        logger.info("BingX: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        data = await self._get("/openApi/swap/v2/quote/ticker")
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in (data if isinstance(data, list) else []):
            raw = item.get("symbol", "")
            if "USDT" not in raw.upper():
                continue
            base = raw.split("-")[0] if "-" in raw else raw.replace("-USDT", "").replace("USDT", "")
            sym = f"{base.upper()}_USDT"
            try:
                price = float(item.get("lastPrice", 0))
                if price <= 0:
                    continue
                change = None
                chg = item.get("priceChangePercent")
                if chg:
                    change = float(chg)
                vol = None
                vq = item.get("quoteVolume")
                if vq:
                    vol = float(vq)
                bid_price = float(item.get("bidPrice") or 0) or None
                ask_price = float(item.get("askPrice") or 0) or None
                bid_size = float(item.get("bidQty") or 0) or None
                ask_size = float(item.get("askQty") or 0) or None
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=now_ms,
                    change_24h_pct=change, volume_24h_usd=vol,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=raw,
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
        data = await self._get("/openApi/swap/v2/quote/depth", {"symbol": raw, "limit": "5"})
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bid_price = float(bids[0][0]) if bids else None
        bid_size = sum(float(level[1]) for level in bids[:5]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_size = sum(float(level[1]) for level in asks[:5]) if asks else None
        ts = int(data.get("T") or time.time() * 1000)
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
