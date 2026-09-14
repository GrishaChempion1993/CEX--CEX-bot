"""BitMart futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://api-cloud-v2.bitmart.com"


class BitMartClient(BaseExchangeClient):
    EXCHANGE_NAME = "bitmart"

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
            if data.get("code") != 1000:
                raise RuntimeError(f"BitMart API error: {data.get('message')}")
            return data.get("data", {})

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        data = await self._get("/contract/public/details")
        symbols = []
        for item in data.get("symbols", []):
            quote = item.get("quote_currency", "")
            if quote != "USDT":
                continue
            raw = item.get("symbol", "")  # e.g. "BTCUSDT"
            base = item.get("base_currency", "")
            if not base:
                base = raw.replace("USDT", "")
            sym = f"{base}_USDT"
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=base,
                exchange=self.EXCHANGE_NAME,
                raw_symbol=raw,
                contract_multiplier=float(item.get("contract_size") or 1.0),
            ))
        logger.info("BitMart: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        # BitMart doesn't have a separate tickers endpoint in V2;
        # use /contract/public/details which includes last_price
        data = await self._get("/contract/public/details")
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in data.get("symbols", []):
            raw = item.get("symbol", "")
            if not raw.endswith("USDT"):
                continue
            base = item.get("base_currency", raw.replace("USDT", ""))
            sym = f"{base}_USDT"
            try:
                price = float(item.get("last_price", 0))
                if price <= 0:
                    continue
                change = None
                chg = item.get("change_24h")
                if chg:
                    change = float(chg) * 100  # decimal to pct
                vol = None
                vq = item.get("turnover_24h")
                if vq:
                    vol = float(vq)
                fr = None
                fr_raw = item.get("funding_rate")
                if fr_raw:
                    fr = float(fr_raw)
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=now_ms,
                    change_24h_pct=change, volume_24h_usd=vol, funding_rate=fr,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=raw,
                )
            except (ValueError, TypeError):
                continue
        return tickers

    async def get_top_of_book(self, symbol: str, raw_symbol: str | None = None) -> Optional[FuturesTicker]:
        raw = (raw_symbol or symbol or "").replace("_", "")
        data = await self._get("/contract/public/depth", {"symbol": raw})
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bid_price = float(bids[0][0]) if bids else None
        bid_size = sum(float(level[1]) for level in bids[:5]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_size = sum(float(level[1]) for level in asks[:5]) if asks else None
        ts = int(data.get("timestamp") or time.time() * 1000)
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
