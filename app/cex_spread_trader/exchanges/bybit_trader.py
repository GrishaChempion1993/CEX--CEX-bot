"""Bybit V5 Unified API futures trading client."""
from __future__ import annotations

import hashlib
import hmac
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

_BASE_URL = "https://api.bybit.com"
_RECV_WINDOW = "5000"


class BybitTrader(BaseTradingClient):
    """Bybit V5 Unified Account futures trader."""

    EXCHANGE_NAME: str = "bybit"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("BYBIT_API_KEY", "")
        self._api_secret: str = os.getenv("BYBIT_API_SECRET", "")
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

    def _sign(self, ts: str, payload: str) -> str:
        """HMAC-SHA256(timestamp + api_key + recv_window + payload)."""
        pre_sign = f"{ts}{self._api_key}{_RECV_WINDOW}{payload}"
        return hmac.new(
            self._api_secret.encode(), pre_sign.encode(), hashlib.sha256
        ).hexdigest()

    def _auth_headers(self, ts: str, payload: str) -> Dict[str, str]:
        return {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-SIGN": self._sign(ts, payload),
            "X-BAPI-RECV-WINDOW": _RECV_WINDOW,
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
        ts = str(int(time.time() * 1000))

        if method == "GET":
            qs = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
            headers = self._auth_headers(ts, qs)
            url = f"{_BASE_URL}{path}" + (f"?{qs}" if qs else "")
            async with self._session.get(url, headers=headers) as resp:
                data = await resp.json()
        else:
            import json as _json

            body_str = _json.dumps(body or {})
            headers = self._auth_headers(ts, body_str)
            url = f"{_BASE_URL}{path}"
            async with self._session.post(url, headers=headers, data=body_str) as resp:
                data = await resp.json()

        if data.get("retCode", -1) != 0:
            logger.warning("Bybit API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BASEUSDT (no underscore)."""
        return symbol.replace("_", "")

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            for coin_info in data.get("result", {}).get("list", [{}])[0].get("coin", []):
                if coin_info.get("coin") == "USDT":
                    return float(coin_info.get("availableToWithdraw", 0))
        except Exception as exc:
            logger.error("Bybit get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "GET",
                "/v5/market/instruments-info",
                {"category": "linear", "symbol": raw},
            )
            items = data.get("result", {}).get("list", [])
            if not items:
                return None
            info = items[0]
            lot_filter = info.get("lotSizeFilter", {})
            price_filter = info.get("priceFilter", {})
            lev_filter = info.get("leverageFilter", {})
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("baseCoin", ""),
                quote_asset=info.get("quoteCoin", "USDT"),
                price_precision=_count_decimals(price_filter.get("tickSize", "0.01")),
                qty_precision=_count_decimals(lot_filter.get("qtyStep", "0.001")),
                min_qty=float(lot_filter.get("minOrderQty", 0)),
                qty_step=float(lot_filter.get("qtyStep", 0)),
                price_step=float(price_filter.get("tickSize", 0)),
                max_leverage=int(float(lev_filter.get("maxLeverage", 100))),
            )
        except Exception as exc:
            logger.error("Bybit get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("POST", "/v5/position/set-leverage", body={
                "category": "linear",
                "symbol": raw,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            })
            if data.get("retCode") == 0:
                return True
            # 110043 = leverage not modified (already set)
            if data.get("retCode") == 110043:
                return True
            logger.warning("Bybit set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("Bybit set_leverage error: %s", exc)
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
        bybit_side = "Buy" if side.lower() == "buy" else "Sell"
        try:
            data = await self._request("POST", "/v5/order/create", body={
                "category": "linear",
                "symbol": raw,
                "side": bybit_side,
                "orderType": "Market",
                "qty": str(qty),
                "reduceOnly": reduce_only,
            })
            result = data.get("result", {})
            if data.get("retCode") == 0:
                return OrderResult(
                    success=True,
                    order_id=result.get("orderId", ""),
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
                error_code=data.get("retCode"),
                error_msg=data.get("retMsg", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("Bybit place_market_order error: %s", exc)
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
                "/v5/position/list",
                {"category": "linear", "symbol": raw},
            )
            items = data.get("result", {}).get("list", [])
            for item in items:
                size = float(item.get("size", 0))
                if size > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("Bybit get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request(
                "GET",
                "/v5/position/list",
                {"category": "linear", "settleCoin": "USDT"},
            )
            positions: List[PositionInfo] = []
            for item in data.get("result", {}).get("list", []):
                size = float(item.get("size", 0))
                if size > 0:
                    sym = self.normalize_symbol(item.get("symbol", ""))
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("Bybit get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        side_raw = item.get("side", "")
        side = "long" if side_raw == "Buy" else "short"
        size = float(item.get("size", 0))
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=size,
            size_usd=float(item.get("positionValue", 0)),
            entry_price=float(item.get("avgPrice", 0)),
            mark_price=float(item.get("markPrice", 0)),
            leverage=float(item.get("leverage", 1)),
            unrealized_pnl=float(item.get("unrealisedPnl", 0)),
            margin=float(item.get("positionIM", 0)),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places in a string like '0.001'."""
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
