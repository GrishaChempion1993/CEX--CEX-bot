"""LBank futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://lbkperp.lbank.com"


class LBankClient(BaseExchangeClient):
    EXCHANGE_NAME = "lbank"

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
            error_code = data.get("error_code")
            success = data.get("success")
            if error_code not in (None, 0, "0"):
                raise RuntimeError(f"LBank API error: error_code={error_code} msg={data.get('msg')}")
            if success not in (None, True, "true", "True"):
                raise RuntimeError(f"LBank API error: success={success} msg={data.get('msg')}")
            return data.get("data", {})

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        data = await self._get("/cfd/openApi/v1/pub/instrument", {"productGroup": "SwapU"})
        symbols = []
        for item in (data if isinstance(data, list) else []):
            raw = item.get("symbol", "")
            if not raw:
                continue
            raw = raw.upper()
            quote = (item.get("clearCurrency") or item.get("priceCurrency") or "").upper()
            if quote != "USDT" or "USDT" not in raw:
                continue
            base = (item.get("baseCurrency") or raw.replace("_USDT", "").replace("USDT", "")).upper()
            sym = f"{base}_USDT"
            symbols.append(FuturesSymbol(
                symbol=sym,
                base_asset=base,
                exchange=self.EXCHANGE_NAME,
                raw_symbol=raw,
                is_active=item.get("needSuspend") in (None, 0, "0", False),
                quote_asset=quote,
                unit_scale=float(item.get("volumeMultiple") or 1.0),
                contract_multiplier=float(item.get("volumeMultiple") or 1.0),
            ))
        logger.info("LBank: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        try:
            data = await self._get("/cfd/openApi/v1/pub/marketData", {"productGroup": "SwapU"})
        except Exception as e:
            logger.warning("LBank tickers fetch failed: %s", e)
            return {}

        tickers = {}
        for item in (data if isinstance(data, list) else []):
            raw = item.get("symbol", "")
            if not raw:
                continue
            raw = raw.upper()
            if "USDT" not in raw:
                continue
            base = raw.replace("_USDT", "").replace("USDT", "").replace("-", "")
            sym = f"{base}_USDT"
            try:
                price = float(item.get("lastPrice", item.get("last", 0)))
                if price <= 0:
                    continue
                open_price = float(item.get("openPrice") or 0)
                change = ((price - open_price) / open_price) * 100 if open_price > 0 else None
                vol = float(item.get("turnover") or 0) or None
                funding_rate = float(item.get("fundingRate") or item.get("positionFeeRate") or 0) or None
                mark_price = float(item.get("markedPrice") or item.get("underlyingPrice") or 0) or None
                ts_raw = int(item.get("lastTime") or 0)
                timestamp_ms = ts_raw * 1000 if 0 < ts_raw < 10**12 else ts_raw
                if timestamp_ms <= 0:
                    timestamp_ms = int(time.time() * 1000)
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=timestamp_ms,
                    change_24h_pct=change, volume_24h_usd=vol,
                    funding_rate=funding_rate,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=raw,
                    mark_price=mark_price,
                    volume_source="turnover",
                )
            except (ValueError, TypeError):
                continue
        return tickers

    async def get_top_of_book(self, symbol: str, raw_symbol: str | None = None) -> Optional[FuturesTicker]:
        raw = (raw_symbol or symbol or "").replace("_", "")
        data = await self._get(
            "/cfd/openApi/v1/pub/marketOrder",
            {"symbol": raw, "depth": 5},
        )
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        try:
            bid_price = float(bids[0]["price"]) if bids else None
            bid_size = sum(float(level["volume"]) for level in bids[:5]) if bids else None
            ask_price = float(asks[0]["price"]) if asks else None
            ask_size = sum(float(level["volume"]) for level in asks[:5]) if asks else None
        except (KeyError, TypeError, ValueError):
            return None

        price = ask_price or bid_price or 0.0
        if price <= 0:
            return None

        return FuturesTicker(
            symbol=symbol,
            price=price,
            timestamp_ms=int(time.time() * 1000),
            exchange=self.EXCHANGE_NAME,
            raw_symbol=raw,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
        )
