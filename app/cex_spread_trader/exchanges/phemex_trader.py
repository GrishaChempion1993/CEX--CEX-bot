"""Phemex API futures trading client."""
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

_BASE_URL = "https://api.phemex.com"


class PhemexTrader(BaseTradingClient):
    """Phemex USDT-M perpetual futures trader."""

    EXCHANGE_NAME: str = "phemex"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("PHEMEX_API_KEY", "")
        self._api_secret: str = os.getenv("PHEMEX_API_SECRET", "")
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

    def _sign(self, path: str, query: str, expiry: str, body: str = "") -> str:
        """HMAC-SHA256(path + query + expiry + body)."""
        message = path + query + expiry + body
        return hmac.new(
            self._api_secret.encode(), message.encode(), hashlib.sha256
        ).hexdigest()

    def _auth_headers(self, path: str, query: str, body: str = "") -> Dict[str, str]:
        expiry = str(int(time.time()) + 60)
        return {
            "x-phemex-access-token": self._api_key,
            "x-phemex-request-expiry": expiry,
            "x-phemex-request-signature": self._sign(path, query, expiry, body),
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        assert self._session, "call connect() first"

        qs = ""
        if params:
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

        body_str = ""
        if body is not None:
            body_str = _json.dumps(body, separators=(",", ":"))

        headers = self._auth_headers(path, qs, body_str)
        url = f"{_BASE_URL}{path}" + (f"?{qs}" if qs else "")

        try:
            if method == "GET":
                async with self._session.get(url, headers=headers) as resp:
                    data = await resp.json(content_type=None)
            elif method == "PUT":
                async with self._session.put(
                    url, headers=headers, data=body_str
                ) as resp:
                    data = await resp.json(content_type=None)
            else:
                async with self._session.post(
                    url, headers=headers, data=body_str
                ) as resp:
                    data = await resp.json(content_type=None)
        except Exception as exc:
            logger.error("Phemex request error %s %s: %s", method, path, exc)
            return {"code": -1, "msg": str(exc)}

        code = data.get("code", 0)
        if code != 0:
            logger.warning("Phemex API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BTCUSDT (no separator)."""
        return symbol.replace("_", "")

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request(
                "GET",
                "/api-data/g-accounts/accountMargin",
                params={"currency": "USDT"},
            )
            if data.get("code") == 0:
                account = data.get("data", {})
                # Phemex returns Rv (risk value) strings for USDT margined
                avail = account.get("availableBalanceRv", 0)
                return float(avail)
        except Exception as exc:
            logger.error("Phemex get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("GET", "/public/products")
            if data.get("code") != 0:
                return None
            products = data.get("data", {}).get("products", data.get("data", []))
            if isinstance(products, dict):
                products = products.get("perpProductsV2", products.get("products", []))
            info = None
            for p in products:
                if p.get("symbol") == raw:
                    info = p
                    break
            if not info:
                return None
            price_step = float(info.get("tickSizeRv", info.get("tickSize", 0.01)))
            qty_step = float(info.get("qtyStepSizeRv", info.get("lotSize", 0.001)))
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("baseCurrency", ""),
                quote_asset=info.get("quoteCurrency", "USDT"),
                price_precision=_count_decimals(str(price_step)),
                qty_precision=_count_decimals(str(qty_step)),
                min_qty=float(info.get("minOrderQtyRv", info.get("minOrderQty", 0))),
                qty_step=qty_step,
                price_step=price_step,
                contract_multiplier=float(info.get("contractSize", 1)),
                max_leverage=int(info.get("maxLeverage", 100)),
                extra={"type": info.get("type", ""), "status": info.get("status", "")},
            )
        except Exception as exc:
            logger.error("Phemex get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "PUT",
                "/g-positions/leverage",
                body={
                    "symbol": raw,
                    "leverageRr": str(leverage),
                },
            )
            if data.get("code") == 0:
                return True
            logger.warning("Phemex set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("Phemex set_leverage error: %s", exc)
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
        phemex_side = "Buy" if side.lower() == "buy" else "Sell"
        # posSide for hedge mode
        if reduce_only:
            pos_side = "Short" if phemex_side == "Buy" else "Long"
        else:
            pos_side = "Long" if phemex_side == "Buy" else "Short"

        body: Dict[str, Any] = {
            "symbol": raw,
            "side": phemex_side,
            "orderQtyRq": str(qty),
            "ordType": "Market",
            "posSide": pos_side,
        }
        if reduce_only:
            body["reduceOnly"] = True

        try:
            data = await self._request("POST", "/g-orders/create", body=body)
            if data.get("code") == 0:
                result = data.get("data", {})
                return OrderResult(
                    success=True,
                    order_id=str(result.get("orderID", result.get("orderId", ""))),
                    exchange=self.EXCHANGE_NAME,
                    symbol=symbol,
                    side=side.lower(),
                    status="submitted",
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
            logger.error("Phemex place_market_order error: %s", exc)
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
                "/g-positions/list",
                params={"symbol": raw},
            )
            if data.get("code") != 0:
                return None
            positions = data.get("data", {}).get("positions", data.get("data", []))
            if isinstance(positions, dict):
                positions = [positions]
            for item in positions:
                size = abs(float(item.get("sizeRv", item.get("size", 0))))
                if size > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("Phemex get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request("GET", "/g-positions/list")
            if data.get("code") != 0:
                return []
            positions_data = data.get("data", {}).get(
                "positions", data.get("data", [])
            )
            if isinstance(positions_data, dict):
                positions_data = [positions_data]
            positions: List[PositionInfo] = []
            for item in positions_data:
                size = abs(float(item.get("sizeRv", item.get("size", 0))))
                if size > 0:
                    sym = self.normalize_symbol(item.get("symbol", ""))
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("Phemex get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        pos_side = item.get("posSide", item.get("side", "")).lower()
        side = "long" if pos_side in ("long", "buy") else "short"
        size = abs(float(item.get("sizeRv", item.get("size", 0))))
        entry = float(item.get("avgEntryPriceRp", item.get("avgEntryPrice", 0)))
        mark = float(item.get("markPriceRp", item.get("markPrice", 0)))
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=size,
            size_usd=size * mark,
            entry_price=entry,
            mark_price=mark,
            leverage=float(
                item.get("leverageRr", item.get("leverage", 1))
            ),
            unrealized_pnl=float(
                item.get("unrealisedPnlRv", item.get("unrealisedPnl", 0))
            ),
            margin=float(
                item.get("positionMarginRv", item.get("positionMargin", 0))
            ),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places in a string like '0.001'."""
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
