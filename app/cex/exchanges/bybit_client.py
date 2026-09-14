"""Bybit V5 futures client."""
import logging
import os
import time
from typing import Dict, List, Optional
import aiohttp
from dotenv import load_dotenv
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bybit.com"

class BybitClient(BaseExchangeClient):
    EXCHANGE_NAME = "bybit"

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        # Grab first proxy from PROXY_POOL env (Bybit blocked by IP in some regions)
        pool = os.getenv("PROXY_POOL", "").strip()
        self._proxy = pool.split(",")[0].strip() if pool else None

    async def connect(self):
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )

    async def close(self):
        if self.session:
            await self.session.close()

    async def _get(self, path: str, params: dict = None) -> dict:
        kwargs = {}
        if self._proxy:
            from aiohttp import BasicAuth
            # Parse optional proxy credentials from the configured URL.
            from urllib.parse import urlparse
            parsed = urlparse(self._proxy)
            proxy_url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
            kwargs["proxy"] = proxy_url
            if parsed.username:
                kwargs["proxy_auth"] = BasicAuth(parsed.username, parsed.password or "")
        async with self.session.get(f"{BASE_URL}{path}", params=params, **kwargs) as r:
            r.raise_for_status()
            data = await r.json()
            if data.get("retCode") != 0:
                raise RuntimeError(f"Bybit API error: {data.get('retMsg')}")
            return data.get("result", {})

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        result = await self._get("/v5/market/instruments-info", {
            "category": "linear",
            "limit": "1000",
        })
        symbols = []
        for item in result.get("list", []):
            status = item.get("status", "")
            quote = item.get("quoteCoin", "")
            if status != "Trading" or quote != "USDT":
                continue
            raw = item.get("symbol", "")
            sym = self.normalize_symbol(raw)
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=self.base_from_symbol(sym),
                exchange=self.EXCHANGE_NAME,
                raw_symbol=raw,
            ))
        logger.info("Bybit: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        result = await self._get("/v5/market/tickers", {"category": "linear"})
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in result.get("list", []):
            raw = item.get("symbol", "")
            if not raw.endswith("USDT"):
                continue
            sym = self.normalize_symbol(raw)
            try:
                price = float(item.get("lastPrice", 0))
                if price <= 0:
                    continue
                change = None
                p24 = item.get("prevPrice24h")
                if p24 and float(p24) > 0:
                    change = ((price - float(p24)) / float(p24)) * 100
                vol = None
                v24 = item.get("turnover24h")
                if v24:
                    vol = float(v24)
                fr = None
                fr_raw = item.get("fundingRate")
                if fr_raw:
                    fr = float(fr_raw)
                bid_price = float(item.get("bid1Price") or 0) or None
                ask_price = float(item.get("ask1Price") or 0) or None
                bid_size = float(item.get("bid1Size") or 0) or None
                ask_size = float(item.get("ask1Size") or 0) or None
                mark_price = float(item.get("markPrice") or 0) or None
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=now_ms,
                    change_24h_pct=change, volume_24h_usd=vol, funding_rate=fr,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=raw,
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
        raw = (raw_symbol or symbol or "").replace("_", "")
        result = await self._get(
            "/v5/market/orderbook",
            {"category": "linear", "symbol": raw, "limit": "10"},
        )
        bids = result.get("b") or []
        asks = result.get("a") or []
        bid_price = float(bids[0][0]) if bids else None
        bid_size = sum(float(level[1]) for level in bids[:5]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_size = sum(float(level[1]) for level in asks[:5]) if asks else None
        ts = int(result.get("ts") or time.time() * 1000)
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
