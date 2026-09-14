"""Bitget futures client (V2 API)."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bitget.com"


class BitgetClient(BaseExchangeClient):
    EXCHANGE_NAME = "bitget"

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
            if data.get("code") != "00000":
                raise RuntimeError(f"Bitget API error: {data.get('msg')}")
            return data.get("data", {})

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        data = await self._get("/api/v2/mix/market/contracts", {
            "productType": "USDT-FUTURES",
        })
        symbols = []
        for item in (data if isinstance(data, list) else []):
            status = item.get("symbolStatus", "")
            if status != "normal":
                continue
            raw = item.get("symbol", "")  # e.g. "BTCUSDT"
            sym = self.normalize_symbol(raw)
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=item.get("baseCoin", self.base_from_symbol(sym)),
                exchange=self.EXCHANGE_NAME,
                raw_symbol=raw,
                contract_multiplier=float(item.get("sizeMultiplier") or 1.0),
            ))
        logger.info("Bitget: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        data = await self._get("/api/v2/mix/market/tickers", {
            "productType": "USDT-FUTURES",
        })
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in (data if isinstance(data, list) else []):
            raw = item.get("symbol", "")
            if not raw.upper().endswith("USDT"):
                continue
            sym = self.normalize_symbol(raw)
            try:
                price = float(item.get("lastPr", 0))
                if price <= 0:
                    continue
                change = None
                chg = item.get("change24h")
                if chg:
                    change = float(chg) * 100  # decimal to pct
                vol = None
                vq = item.get("quoteVolume")
                if vq:
                    vol = float(vq)
                fr = None
                fr_raw = item.get("fundingRate")
                if fr_raw:
                    fr = float(fr_raw)
                ts = int(item.get("ts", now_ms))
                bid_price = float(item.get("bidPr") or 0) or None
                ask_price = float(item.get("askPr") or 0) or None
                bid_size = float(item.get("bidSz") or 0) or None
                ask_size = float(item.get("askSz") or 0) or None
                mark_price = float(item.get("markPrice") or 0) or None
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=ts,
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
        data = await self._get(
            "/api/v2/mix/market/merge-depth",
            {
                "symbol": raw,
                "productType": "USDT-FUTURES",
                "precision": "scale0",
                "limit": "10",
            },
        )
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bid_price = float(bids[0][0]) if bids else None
        bid_size = sum(float(level[1]) for level in bids[:5]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_size = sum(float(level[1]) for level in asks[:5]) if asks else None
        ts = int(data.get("ts") or time.time() * 1000)
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
