"""KuCoin Futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://api-futures.kucoin.com"


class KuCoinClient(BaseExchangeClient):
    EXCHANGE_NAME = "kucoin"

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
            if data.get("code") != "200000":
                raise RuntimeError(f"KuCoin API error: {data.get('msg')}")
            return data.get("data", {})

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        items = await self._get("/api/v1/contracts/active")
        symbols = []
        for item in (items if isinstance(items, list) else []):
            status = item.get("status", "")
            settle = item.get("settleCurrency", "")
            if status != "Open" or settle != "USDT":
                continue
            raw = item.get("symbol", "")  # e.g. "XBTUSDTM"
            base = item.get("baseCurrency", "")
            if not base:
                base = raw.replace("USDTM", "").replace("USDT", "")
            if base == "XBT":
                base = "BTC"
            sym = f"{base}_USDT"
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=base,
                exchange=self.EXCHANGE_NAME,
                raw_symbol=raw,
                contract_multiplier=float(item.get("multiplier") or 1.0),
            ))
        logger.info("KuCoin: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        # KuCoin doesn't have a batch ticker endpoint for futures,
        # so we use the contracts list which includes mark/last price.
        # For tickers we'll use the /api/v1/allTickers endpoint (if available)
        # or fall back to individual ticker calls.
        # Actually KuCoin futures has no all-tickers. We'll fetch contracts + use snapshot price.
        items = await self._get("/api/v1/contracts/active")
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in (items if isinstance(items, list) else []):
            settle = item.get("settleCurrency", "")
            if settle != "USDT":
                continue
            base = item.get("baseCurrency", "")
            raw = item.get("symbol", "")
            if not base:
                base = raw.replace("USDTM", "").replace("USDT", "")
            if base == "XBT":
                base = "BTC"
            sym = f"{base}_USDT"
            try:
                price = float(item.get("markPrice") or item.get("lastTradePrice") or 0)
                if price <= 0:
                    continue
                vol = None
                v24 = item.get("turnoverOf24h")
                if v24:
                    vol = float(v24)
                fr = None
                fr_raw = item.get("fundingFeeRate")
                if fr_raw:
                    fr = float(fr_raw)
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=now_ms,
                    volume_24h_usd=vol, funding_rate=fr,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=raw,
                    mark_price=price,
                )
            except (ValueError, TypeError):
                continue
        return tickers

    async def get_top_of_book(self, symbol: str, raw_symbol: str | None = None) -> Optional[FuturesTicker]:
        raw = raw_symbol or symbol.replace("BTC_USDT", "XBTUSDTM").replace("_USDT", "USDTM")
        data = await self._get("/api/v1/level2/snapshot", {"symbol": raw})
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        bid_price = float(bids[0][0]) if bids else None
        bid_size = sum(float(level[1]) for level in bids[:5]) if bids else None
        ask_price = float(asks[0][0]) if asks else None
        ask_size = sum(float(level[1]) for level in asks[:5]) if asks else None
        ts_raw = int(data.get("ts") or 0)
        if ts_raw > 10**14:
            ts = ts_raw // 1_000_000
        else:
            ts = ts_raw or int(time.time() * 1000)
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
