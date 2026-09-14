"""CoinEx futures client (V2 API)."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coinex.com/v2"


class CoinExClient(BaseExchangeClient):
    EXCHANGE_NAME = "coinex"

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
                raise RuntimeError(f"CoinEx API error: {data.get('message')}")
            return data.get("data", {})

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        data = await self._get("/futures/market")
        symbols = []
        for item in (data if isinstance(data, list) else []):
            raw = item.get("market", "")  # e.g. "BTCUSDT"
            if not raw.endswith("USDT"):
                continue
            base = raw.replace("USDT", "")
            sym = f"{base}_USDT"
            symbols.append(FuturesSymbol(
                symbol=sym, base_asset=base, exchange=self.EXCHANGE_NAME, raw_symbol=raw,
            ))
        logger.info("CoinEx: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        data = await self._get("/futures/ticker")
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in (data if isinstance(data, list) else []):
            raw = item.get("market", "")
            if not raw.endswith("USDT"):
                continue
            base = raw.replace("USDT", "")
            sym = f"{base}_USDT"
            try:
                price = float(item.get("last", 0))
                if price <= 0:
                    continue
                change = None
                open_p = item.get("open")
                if open_p and float(open_p) > 0:
                    change = ((price - float(open_p)) / float(open_p)) * 100
                vol = None
                vq = item.get("value")
                if vq:
                    vol = float(vq)
                mark_price = float(item.get("mark_price") or 0) or None
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
        data = await self._get("/futures/depth", {"market": raw, "limit": "10", "interval": "0"})
        depth = data.get("depth") or {}
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        bid_price = float(bids[0][0]) if bids else None
        bid_size = sum(float(level[1]) for level in bids[:5]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_size = sum(float(level[1]) for level in asks[:5]) if asks else None
        ts = int(data.get("updated_at") or time.time() * 1000)
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
