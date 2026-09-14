"""Bitget V2 Mix API futures trading client."""
from __future__ import annotations

import base64
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

_BASE_URL = "https://api.bitget.com"


class BitgetTrader(BaseTradingClient):
    """Bitget V2 USDT-FUTURES trader."""

    EXCHANGE_NAME: str = "bitget"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("BITGET_API_KEY", "")
        self._api_secret: str = os.getenv("BITGET_API_SECRET", "")
        self._passphrase: str = os.getenv("BITGET_PASSPHRASE", "")
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

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Base64(HMAC-SHA256(timestamp + method + requestPath + body))."""
        message = f"{timestamp}{method}{request_path}{body}"
        mac = hmac.new(
            self._api_secret.encode(), message.encode(), hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _auth_headers(self, timestamp: str, method: str, request_path: str, body: str = "") -> Dict[str, str]:
        return {
            "ACCESS-KEY": self._api_key,
            "ACCESS-SIGN": self._sign(timestamp, method, request_path, body),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self._passphrase,
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
            request_path = f"{path}?{qs}" if qs else path
            headers = self._auth_headers(ts, "GET", request_path)
            url = f"{_BASE_URL}{request_path}"
            async with self._session.get(url, headers=headers) as resp:
                data = await resp.json()
        else:
            body_str = _json.dumps(body or {})
            headers = self._auth_headers(ts, "POST", path, body_str)
            url = f"{_BASE_URL}{path}"
            async with self._session.post(url, headers=headers, data=body_str) as resp:
                data = await resp.json()

        code = data.get("code", "")
        if str(code) != "00000":
            logger.warning("Bitget API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BASEUSDT."""
        return symbol.replace("_", "")

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/api/v2/mix/account/account", {
                "productType": "USDT-FUTURES",
                "symbol": "BTCUSDT",
            })
            result = data.get("data", {})
            return float(result.get("available", 0))
        except Exception as exc:
            logger.error("Bitget get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("GET", "/api/v2/mix/market/contracts", {
                "productType": "USDT-FUTURES",
            })
            items = data.get("data", [])
            if not items:
                return None
            for info in items:
                if info.get("symbol", "") == raw:
                    return ContractInfo(
                        symbol=symbol,
                        raw_symbol=raw,
                        base_asset=info.get("baseCoin", ""),
                        quote_asset=info.get("quoteCoin", "USDT"),
                        price_precision=_count_decimals(info.get("pricePlace", "2")),
                        qty_precision=_count_decimals(info.get("volumePlace", "2")),
                        min_qty=float(info.get("minTradeNum", 0)),
                        qty_step=float(info.get("sizeMultiplier", 0)),
                        price_step=float(info.get("priceEndStep", 0)),
                        contract_multiplier=float(info.get("sizeMultiplier", 1)),
                        max_leverage=int(float(info.get("maxLever", 100))),
                        extra=info,
                    )
            return None
        except Exception as exc:
            logger.error("Bitget get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("POST", "/api/v2/mix/account/set-leverage", body={
                "symbol": raw,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "leverage": str(leverage),
            })
            if str(data.get("code", "")) == "00000":
                return True
            logger.warning("Bitget set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("Bitget set_leverage error: %s", exc)
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
        # Bitget uses side=buy/sell and tradeSide=open/close
        bitget_side = "buy" if side.lower() == "buy" else "sell"
        trade_side = "close" if reduce_only else "open"
        try:
            data = await self._request("POST", "/api/v2/mix/order/place-order", body={
                "symbol": raw,
                "productType": "USDT-FUTURES",
                "marginMode": "crossed",
                "marginCoin": "USDT",
                "side": bitget_side,
                "tradeSide": trade_side,
                "orderType": "market",
                "size": str(qty),
            })
            result = data.get("data", {})
            if str(data.get("code", "")) == "00000":
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
                error_code=int(data.get("code", 0)) if str(data.get("code", "")).isdigit() else None,
                error_msg=data.get("msg", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("Bitget place_market_order error: %s", exc)
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
            data = await self._request("GET", "/api/v2/mix/position/single-position", {
                "productType": "USDT-FUTURES",
                "symbol": raw,
            })
            items = data.get("data", [])
            for item in items:
                size = float(item.get("total", 0))
                if size > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("Bitget get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request("GET", "/api/v2/mix/position/all-position", {
                "productType": "USDT-FUTURES",
            })
            positions: List[PositionInfo] = []
            for item in data.get("data", []):
                size = float(item.get("total", 0))
                if size > 0:
                    sym = self.normalize_symbol(item.get("symbol", ""))
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("Bitget get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        hold_side = item.get("holdSide", "")
        side = "long" if hold_side == "long" else "short"
        size = float(item.get("total", 0))
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=size,
            size_usd=float(item.get("notionalUsd", 0)) if item.get("notionalUsd") else size * float(item.get("markPrice", 0)),
            entry_price=float(item.get("openPriceAvg", 0)),
            mark_price=float(item.get("markPrice", 0)),
            leverage=float(item.get("leverage", 1)),
            unrealized_pnl=float(item.get("unrealizedPL", 0)),
            margin=float(item.get("margin", 0)),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places or parse integer precision value."""
    try:
        v = int(value)
        return v
    except ValueError:
        pass
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
