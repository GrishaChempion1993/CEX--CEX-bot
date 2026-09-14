"""Gate.io Futures API trading client."""
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

_BASE_URL = "https://fx-api.gateio.ws"


class GateTrader(BaseTradingClient):
    """Gate.io USDT-margined futures trader."""

    EXCHANGE_NAME: str = "gate"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("GATE_API_KEY", "")
        self._api_secret: str = os.getenv("GATE_API_SECRET", "")
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

    def _sign(
        self,
        method: str,
        path: str,
        query: str,
        body: str,
        ts: str,
    ) -> str:
        """HMAC-SHA512(method + \\n + path + \\n + query + \\n + hex(sha512(body)) + \\n + timestamp)."""
        body_hash = hashlib.sha512(body.encode()).hexdigest()
        pre_sign = f"{method}\n{path}\n{query}\n{body_hash}\n{ts}"
        return hmac.new(
            self._api_secret.encode(), pre_sign.encode(), hashlib.sha512
        ).hexdigest()

    def _auth_headers(
        self, method: str, path: str, query: str, body: str, ts: str
    ) -> Dict[str, str]:
        return {
            "KEY": self._api_key,
            "SIGN": self._sign(method, path, query, body, ts),
            "Timestamp": ts,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
    ) -> Any:
        """Send authenticated request, return parsed JSON."""
        assert self._session, "call connect() first"
        ts = str(int(time.time()))

        qs = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        body_str = _json.dumps(body) if body is not None else ""
        headers = self._auth_headers(method.upper(), path, qs, body_str, ts)

        url = f"{_BASE_URL}{path}" + (f"?{qs}" if qs else "")

        if method.upper() == "GET":
            async with self._session.get(url, headers=headers) as resp:
                data = await resp.json()
        elif method.upper() == "POST":
            async with self._session.post(url, headers=headers, data=body_str) as resp:
                data = await resp.json()
        elif method.upper() == "PUT":
            async with self._session.put(url, headers=headers, data=body_str) as resp:
                data = await resp.json()
        else:
            raise ValueError(f"unsupported method {method}")

        if isinstance(data, dict) and "label" in data:
            logger.warning("Gate API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BTC_USDT (same format, Gate uses underscore)."""
        return symbol

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/api/v4/futures/usdt/accounts")
            return float(data.get("available", 0))
        except Exception as exc:
            logger.error("Gate get_balance error: %s", exc)
            return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("GET", f"/api/v4/futures/usdt/contracts/{raw}")
            if isinstance(data, dict) and "label" in data:
                return None
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=raw.split("_")[0] if "_" in raw else raw,
                quote_asset="USDT",
                price_precision=_count_decimals(str(data.get("order_price_round", "0.01"))),
                qty_precision=0,  # Gate sizes are integer contract counts
                min_qty=float(data.get("order_size_min", 1)),
                qty_step=1.0,
                price_step=float(data.get("order_price_round", 0)),
                contract_multiplier=float(data.get("quanto_multiplier", 1)),
                max_leverage=int(float(data.get("leverage_max", 100))),
            )
        except Exception as exc:
            logger.error("Gate get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "POST",
                f"/api/v4/futures/usdt/positions/{raw}/leverage",
                body={"leverage": str(leverage)},
            )
            if isinstance(data, dict) and "label" not in data:
                return True
            logger.warning("Gate set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("Gate set_leverage error: %s", exc)
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
        # Gate uses signed size: positive = long, negative = short
        size = int(qty) if side.lower() == "buy" else -int(qty)
        try:
            body: Dict[str, Any] = {
                "contract": raw,
                "size": size,
                "price": "0",  # market order
                "tif": "ioc",
            }
            if reduce_only:
                body["reduce_only"] = True
            data = await self._request("POST", "/api/v4/futures/usdt/orders", body=body)
            if isinstance(data, dict) and "label" in data:
                return OrderResult(
                    success=False,
                    exchange=self.EXCHANGE_NAME,
                    symbol=symbol,
                    side=side.lower(),
                    error_msg=data.get("message", data.get("label", "")),
                    raw=data,
                )
            order_id = str(data.get("id", ""))
            fill_price = float(data.get("fill_price", 0) or 0)
            return OrderResult(
                success=True,
                order_id=order_id,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                filled_qty=abs(float(data.get("size", 0))),
                filled_price=fill_price,
                status=data.get("status", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("Gate place_market_order error: %s", exc)
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
            data = await self._request("GET", f"/api/v4/futures/usdt/positions/{raw}")
            if isinstance(data, dict) and "label" in data:
                return None
            size = int(data.get("size", 0))
            if size == 0:
                return None
            return self._parse_position(symbol, data)
        except Exception as exc:
            logger.error("Gate get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request("GET", "/api/v4/futures/usdt/positions")
            if not isinstance(data, list):
                return []
            positions: List[PositionInfo] = []
            for item in data:
                size = int(item.get("size", 0))
                if size != 0:
                    contract = item.get("contract", "")
                    sym = self.normalize_symbol(contract)
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("Gate get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        size = int(item.get("size", 0))
        side = "long" if size > 0 else "short"
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=abs(size),
            size_usd=abs(float(item.get("value", 0))),
            entry_price=float(item.get("entry_price", 0) or 0),
            mark_price=float(item.get("mark_price", 0) or 0),
            leverage=float(item.get("leverage", 1) or 1),
            unrealized_pnl=float(item.get("unrealised_pnl", 0) or 0),
            margin=float(item.get("margin", 0) or 0),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
