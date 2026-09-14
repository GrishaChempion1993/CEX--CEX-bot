"""HTX (Huobi) futures client."""
import logging
import time
from typing import Dict, List, Optional
import aiohttp
from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hbdm.com"


class HTXClient(BaseExchangeClient):
    EXCHANGE_NAME = "htx"

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
            data = await r.json(content_type=None)
            if data.get("status") != "ok" and data.get("code") not in (200, None):
                raise RuntimeError(f"HTX API error: {data}")
            return data

    async def get_futures_symbols(self) -> List[FuturesSymbol]:
        data = await self._get("/linear-swap-api/v1/swap_contract_info")
        symbols = []
        for item in data.get("data", []):
            status = item.get("contract_status", 0)
            if status != 1:  # 1 = active
                continue
            pair = item.get("pair", "")  # e.g. "BTC-USDT"
            if not pair.endswith("-USDT"):
                continue
            base = pair.split("-")[0]
            sym = f"{base}_USDT"
            if sym not in {s.symbol for s in symbols}:
                symbols.append(FuturesSymbol(
                    symbol=sym,
                    base_asset=base,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=pair,
                    contract_multiplier=float(item.get("contract_size") or 1.0),
                ))
        logger.info("HTX: %d futures symbols", len(symbols))
        return symbols

    async def get_all_tickers(self) -> Dict[str, FuturesTicker]:
        data = await self._get("/linear-swap-ex/market/detail/batch_merged")
        tickers = {}
        now_ms = int(time.time() * 1000)
        for item in data.get("ticks", []):
            contract = item.get("contract_code", "")
            if not contract.endswith("-USDT"):
                continue
            base = contract.split("-")[0]
            sym = f"{base}_USDT"
            try:
                price = float(item.get("close", 0))
                if price <= 0:
                    continue
                change = None
                open_p = item.get("open")
                if open_p and float(open_p) > 0:
                    change = ((price - float(open_p)) / float(open_p)) * 100
                vol = None
                vol_raw = item.get("trade_turnover")
                if vol_raw:
                    vol = float(vol_raw)
                bid = item.get("bid") or []
                ask = item.get("ask") or []
                bid_price = float(bid[0]) if len(bid) >= 1 else None
                bid_size = float(bid[1]) if len(bid) >= 2 else None
                ask_price = float(ask[0]) if len(ask) >= 1 else None
                ask_size = float(ask[1]) if len(ask) >= 2 else None
                tickers[sym] = FuturesTicker(
                    symbol=sym, price=price, timestamp_ms=int(item.get("ts", now_ms)),
                    change_24h_pct=change, volume_24h_usd=vol,
                    exchange=self.EXCHANGE_NAME,
                    raw_symbol=contract,
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
        data = await self._get("/linear-swap-ex/market/depth", {"contract_code": raw, "type": "step0"})
        tick = data.get("tick") or {}
        bids = tick.get("bids") or []
        asks = tick.get("asks") or []
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
