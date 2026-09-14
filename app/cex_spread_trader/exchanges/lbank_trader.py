"""LBank Futures API trading client."""
from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import os
import time
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv

from app.cex_spread_trader.base_trading_client import (
    BaseTradingClient,
    ContractInfo,
    OrderResult,
    PositionInfo,
)

load_dotenv()
logger = logging.getLogger(__name__)

_BASE_URL = "https://lbkperp.lbank.com"


class LbankTrader(BaseTradingClient):
    """LBank Futures trading client."""

    EXCHANGE_NAME: str = "lbank"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("LBANK_API_KEY", "")
        self._api_secret: str = os.getenv("LBANK_API_SECRET", "")
        self._session: Optional[aiohttp.ClientSession] = None

    # ── lifecycle ─────────────────────────────────────────────────

    async def connect(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── auth helpers ──────────────────────────────────────────────

    def _sign(self, timestamp: str, params: Dict[str, Any]) -> str:
        """HMAC-SHA256(timestamp + sorted_params)."""
        sorted_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        pre_sign = f"{timestamp}{sorted_str}"
        return hmac.new(
            self._api_secret.encode(), pre_sign.encode(), hashlib.sha256
        ).hexdigest().lower()

    def _auth_headers(self, timestamp: str, params: Dict[str, Any]) -> Dict[str, str]:
        return {
            "Apikey": self._api_key,
            "Signature": self._sign(timestamp, params),
            "Timestamp": timestamp,
            "Content-Type": "application/json",
        }

    async def _request_get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        assert self._session, "call connect() first"
        ts = str(int(time.time() * 1000))
        p = params or {}
        headers = self._auth_headers(ts, p)
        qs = "&".join(f"{k}={v}" for k, v in sorted(p.items()))
        url = f"{_BASE_URL}{path}" + (f"?{qs}" if qs else "")
        async with self._session.get(url, headers=headers) as resp:
            data = await resp.json()
        if not data.get("result"):
            logger.warning("LBank API error GET %s: %s", path, data)
        return data

    async def _request_post(
        self,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        assert self._session, "call connect() first"
        ts = str(int(time.time() * 1000))
        b = body or {}
        headers = self._auth_headers(ts, b)
        url = f"{_BASE_URL}{path}"
        body_str = _json.dumps(b)
        async with self._session.post(url, headers=headers, data=body_str) as resp:
            data = await resp.json()
        if not data.get("result"):
            logger.warning("LBank API error POST %s: %s", path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT stays as BTC_USDT for LBank."""
        return symbol

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request_post(
                "/cfd/openApi/v1/pub/getBalance", {"coin": "USDT"}
            )
            if data.get("result"):
                info = data.get("data", {})
                return float(info.get("available", 0))
        except Exception as exc:
            logger.error("LBank get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request_get(
                "/cfd/openApi/v1/pub/instrument", {"symbol": raw}
            )
            if not data.get("result"):
                return None
            info = data.get("data", {})
            if isinstance(info, list):
                if not info:
                    return None
                info = info[0]
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("baseCurrency", ""),
                quote_asset=info.get("quoteCurrency", "USDT"),
                price_precision=_count_decimals(str(info.get("priceStep", "0.01"))),
                qty_precision=_count_decimals(str(info.get("volStep", "0.001"))),
                min_qty=float(info.get("minVol", 0)),
                qty_step=float(info.get("volStep", 0)),
                price_step=float(info.get("priceStep", 0)),
                contract_multiplier=float(info.get("contractSize", 1)),
                max_leverage=int(float(info.get("maxLeverage", 100))),
                extra=info if isinstance(info, dict) else {},
            )
        except Exception as exc:
            logger.error("LBank get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request_post(
                "/cfd/openApi/v1/pub/setLeverage",
                {"symbol": raw, "leverage": str(leverage)},
            )
            if data.get("result"):
                return True
            logger.warning("LBank set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("LBank set_leverage error: %s", exc)
            return False

    # ── orders ────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = False,
    ) -> OrderResult:
        raw = self.to_raw_symbol(symbol)
        # LBank directions: 1=open_long, 2=open_short, 3=close_long, 4=close_short
        if side.lower() == "buy":
            direction = 3 if reduce_only else 1  # close_short or open_long
        else:
            direction = 4 if reduce_only else 2  # close_long or open_short

        try:
            data = await self._request_post(
                "/cfd/openApi/v1/pub/submitOrder",
                {
                    "symbol": raw,
                    "direction": str(direction),
                    "type": "market",
                    "volume": str(qty),
                },
            )
            result = data.get("data", {})
            if data.get("result"):
                return OrderResult(
                    success=True,
                    order_id=str(result.get("orderId", "")),
                    exchange=self.EXCHANGE_NAME,
                    symbol=symbol,
                    side=side.lower(),
                    status="filled",
                    raw=data,
                )
            return OrderResult(
                success=False,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                error_code=data.get("error_code"),
                error_msg=data.get("msg", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("LBank place_market_order error: %s", exc)
            return OrderResult(
                success=False,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                error_msg=str(exc),
            )

    # ── positions ─────────────────────────────────────────────────

    async def get_position(self, symbol: str) -> Optional[PositionInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request_post(
                "/cfd/openApi/v1/pub/getPositions", {"symbol": raw}
            )
            items = data.get("data", [])
            if isinstance(items, dict):
                items = [items]
            for item in items:
                size = float(item.get("volume", 0))
                if size > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("LBank get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request_post("/cfd/openApi/v1/pub/getPositions", {})
            positions: List[PositionInfo] = []
            items = data.get("data", [])
            if isinstance(items, dict):
                items = [items]
            for item in items:
                size = float(item.get("volume", 0))
                if size > 0:
                    sym = item.get("symbol", "")
                    if not sym:
                        continue
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("LBank get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        # direction: 1=long, 2=short
        direction = item.get("direction", 1)
        side = "long" if str(direction) == "1" else "short"
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=float(item.get("volume", 0)),
            size_usd=float(item.get("holdAmount", 0)),
            entry_price=float(item.get("openPrice", 0)),
            mark_price=float(item.get("markPrice", 0)),
            leverage=float(item.get("leverage", 1)),
            unrealized_pnl=float(item.get("unrealizedPnl", 0)),
            margin=float(item.get("margin", 0)),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places in a string like '0.001'."""
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
