"""BingX Perpetual Swap API futures trading client."""
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

_BASE_URL = "https://open-api.bingx.com"


class BingxTrader(BaseTradingClient):
    """BingX USDT-M Perpetual Swap futures trader."""

    EXCHANGE_NAME: str = "bingx"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("BINGX_API_KEY", "")
        self._api_secret: str = os.getenv("BINGX_API_SECRET", "")
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

    def _sign(self, params_str: str) -> str:
        """HMAC-SHA256 of the sorted query params string."""
        return hmac.new(
            self._api_secret.encode(), params_str.encode(), hashlib.sha256
        ).hexdigest()

    def _build_signed_qs(self, params: Dict[str, Any]) -> str:
        """Build sorted query string with timestamp and appended signature."""
        params["timestamp"] = str(int(time.time() * 1000))
        sorted_str = "&".join(
            f"{k}={v}" for k, v in sorted(params.items())
        )
        signature = self._sign(sorted_str)
        return f"{sorted_str}&signature={signature}"

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        assert self._session, "call connect() first"
        qs = self._build_signed_qs(params or {})
        url = f"{_BASE_URL}{path}?{qs}"
        headers = {"X-BX-APIKEY": self._api_key}

        try:
            if method == "GET":
                async with self._session.get(url, headers=headers) as resp:
                    data = await resp.json(content_type=None)
            else:
                headers["Content-Type"] = "application/json"
                async with self._session.post(url, headers=headers) as resp:
                    data = await resp.json(content_type=None)
        except Exception as exc:
            logger.error("BingX request error %s %s: %s", method, path, exc)
            return {"code": -1, "msg": str(exc)}

        code = data.get("code", -1)
        if code != 0:
            logger.warning("BingX API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BTC-USDT."""
        return symbol.replace("_", "-")

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request(
                "GET", "/openApi/swap/v2/user/balance"
            )
            if data.get("code") == 0:
                balance = data.get("data", {}).get("balance", {})
                return float(balance.get("availableMargin", 0))
        except Exception as exc:
            logger.error("BingX get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "GET", "/openApi/swap/v2/quote/contracts"
            )
            if data.get("code") != 0:
                return None
            contracts = data.get("data", [])
            info = None
            for c in contracts:
                if c.get("symbol") == raw:
                    info = c
                    break
            if not info:
                return None
            qty_step = float(info.get("tradeMinQuantity", 0.001))
            price_step = float(info.get("pricePrecision", 0.01))
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("asset", ""),
                quote_asset=info.get("currency", "USDT"),
                price_precision=int(info.get("pricePrecision", 8))
                    if isinstance(info.get("pricePrecision"), int)
                    else _count_decimals(str(price_step)),
                qty_precision=_count_decimals(str(qty_step)),
                min_qty=float(info.get("tradeMinQuantity", 0)),
                qty_step=qty_step,
                price_step=price_step,
                contract_multiplier=1.0,
                max_leverage=int(info.get("maxLongLeverage", 100)),
                extra={"status": info.get("status", "")},
            )
        except Exception as exc:
            logger.error("BingX get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "POST",
                "/openApi/swap/v2/trade/leverage",
                params={
                    "symbol": raw,
                    "side": "BOTH",
                    "leverage": str(leverage),
                },
            )
            if data.get("code") == 0:
                return True
            logger.warning("BingX set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("BingX set_leverage error: %s", exc)
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
        bingx_side = "BUY" if side.lower() == "buy" else "SELL"
        # For position side: BUY+LONG=open long, SELL+SHORT=open short,
        # SELL+LONG=close long, BUY+SHORT=close short
        if reduce_only:
            position_side = "SHORT" if bingx_side == "BUY" else "LONG"
        else:
            position_side = "LONG" if bingx_side == "BUY" else "SHORT"
        try:
            data = await self._request(
                "POST",
                "/openApi/swap/v2/trade/order",
                params={
                    "symbol": raw,
                    "side": bingx_side,
                    "type": "MARKET",
                    "quantity": str(qty),
                    "positionSide": position_side,
                },
            )
            if data.get("code") == 0:
                result = data.get("data", {}).get("order", data.get("data", {}))
                return OrderResult(
                    success=True,
                    order_id=str(result.get("orderId", "")),
                    exchange=self.EXCHANGE_NAME,
                    symbol=symbol,
                    side=side.lower(),
                    filled_qty=float(result.get("executedQty", 0)),
                    filled_price=float(result.get("avgPrice", 0)),
                    status=result.get("status", "filled"),
                    raw=data,
                )
            return OrderResult(
                success=False,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                error_code=data.get("code"),
                error_msg=data.get("msg", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("BingX place_market_order error: %s", exc)
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
            data = await self._request(
                "GET",
                "/openApi/swap/v2/user/positions",
                params={"symbol": raw},
            )
            if data.get("code") != 0:
                return None
            items = data.get("data", [])
            for item in items:
                size = abs(float(item.get("positionAmt", 0)))
                if size > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("BingX get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request(
                "GET", "/openApi/swap/v2/user/positions"
            )
            if data.get("code") != 0:
                return []
            positions: List[PositionInfo] = []
            for item in data.get("data", []):
                size = abs(float(item.get("positionAmt", 0)))
                if size > 0:
                    sym = self.normalize_symbol(
                        item.get("symbol", "").replace("-", "_")
                    )
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("BingX get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        pos_side = item.get("positionSide", "").upper()
        side = "long" if pos_side == "LONG" else "short"
        size = abs(float(item.get("positionAmt", 0)))
        entry = float(item.get("avgPrice", 0))
        mark = float(item.get("markPrice", 0))
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=size,
            size_usd=size * mark,
            entry_price=entry,
            mark_price=mark,
            leverage=float(item.get("leverage", 1)),
            unrealized_pnl=float(item.get("unrealizedProfit", 0)),
            margin=float(item.get("initialMargin", 0)),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places in a string like '0.001'."""
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
