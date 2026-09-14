"""BloFin API futures trading client."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json as _json
import logging
import os
import time
from datetime import datetime, timezone
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

_BASE_URL = "https://openapi.blofin.com"


class BlofinTrader(BaseTradingClient):
    """BloFin SWAP (perpetual futures) trader."""

    EXCHANGE_NAME: str = "blofin"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("BLOFIN_API_KEY", "")
        self._api_secret: str = os.getenv("BLOFIN_API_SECRET", "")
        self._passphrase: str = os.getenv("BLOFIN_PASSPHRASE", "")
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

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        """Base64(HMAC-SHA256(timestamp + method + path + body))."""
        message = f"{timestamp}{method}{path}{body}"
        mac = hmac.new(
            self._api_secret.encode(), message.encode(), hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _auth_headers(self, timestamp: str, method: str, path: str, body: str = "") -> Dict[str, str]:
        return {
            "ACCESS-KEY": self._api_key,
            "ACCESS-SIGN": self._sign(timestamp, method, path, body),
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
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

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
        if str(code) != "0":
            logger.warning("BloFin API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BASE-USDT."""
        return symbol.replace("_", "-")

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/api/v1/account/balance")
            details = data.get("data", {}).get("details", [])
            for detail in details:
                if detail.get("currency", "").upper() == "USDT":
                    return float(detail.get("availBal", 0))
        except Exception as exc:
            logger.error("BloFin get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("GET", "/api/v1/market/instruments", {
                "instType": "SWAP",
            })
            items = data.get("data", [])
            for info in items:
                if info.get("instId", "") == raw:
                    return ContractInfo(
                        symbol=symbol,
                        raw_symbol=raw,
                        base_asset=info.get("baseCurrency", ""),
                        quote_asset=info.get("quoteCurrency", "USDT"),
                        price_precision=_count_decimals(info.get("tickSize", "0.01")),
                        qty_precision=_count_decimals(info.get("lotSize", "0.001")),
                        min_qty=float(info.get("minSize", 0)),
                        qty_step=float(info.get("lotSize", 0)),
                        price_step=float(info.get("tickSize", 0)),
                        contract_multiplier=float(info.get("contractValue", 1)),
                        max_leverage=int(float(info.get("maxLeverage", 100))),
                        extra=info,
                    )
            return None
        except Exception as exc:
            logger.error("BloFin get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("POST", "/api/v1/account/set-leverage", body={
                "instId": raw,
                "lever": str(leverage),
                "mgnMode": "cross",
            })
            if str(data.get("code", "")) == "0":
                return True
            logger.warning("BloFin set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("BloFin set_leverage error: %s", exc)
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
        blofin_side = "buy" if side.lower() == "buy" else "sell"
        body: Dict[str, Any] = {
            "instId": raw,
            "tdMode": "cross",
            "side": blofin_side,
            "ordType": "market",
            "sz": str(qty),
        }
        if reduce_only:
            body["reduceOnly"] = True
        try:
            data = await self._request("POST", "/api/v1/trade/order", body=body)
            result = data.get("data", {})
            if str(data.get("code", "")) == "0":
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
                error_code=int(data.get("code", 0)) if str(data.get("code", "")).lstrip("-").isdigit() else None,
                error_msg=data.get("msg", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("BloFin place_market_order error: %s", exc)
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
            data = await self._request("GET", "/api/v1/account/positions", {
                "instId": raw,
            })
            items = data.get("data", [])
            for item in items:
                size = float(item.get("positions", 0))
                if size > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("BloFin get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request("GET", "/api/v1/account/positions")
            positions: List[PositionInfo] = []
            for item in data.get("data", []):
                size = float(item.get("positions", 0))
                if size > 0:
                    inst_id = item.get("instId", "")
                    sym = self.normalize_symbol(inst_id)
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("BloFin get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        pos_side = item.get("posSide", "")
        if pos_side == "long":
            side = "long"
        elif pos_side == "short":
            side = "short"
        else:
            # net mode: infer from positions sign or direction
            side = "long" if float(item.get("positions", 0)) > 0 else "short"
        size = abs(float(item.get("positions", 0)))
        mark_price = float(item.get("markPrice", 0))
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=size,
            size_usd=float(item.get("notionalUsd", 0)) if item.get("notionalUsd") else size * mark_price,
            entry_price=float(item.get("averagePrice", 0)),
            mark_price=mark_price,
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
