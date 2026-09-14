"""CoinEx Futures V2 API trading client."""
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

_BASE_URL = "https://api.coinex.com"


class CoinexTrader(BaseTradingClient):
    """CoinEx Futures V2 trading client."""

    EXCHANGE_NAME: str = "coinex"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("COINEX_API_KEY", "")
        self._api_secret: str = os.getenv("COINEX_API_SECRET", "")
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

    def _sign(self, method: str, path: str, body: str, timestamp: str) -> str:
        """HMAC-SHA256(method + path + body + timestamp)."""
        pre_sign = f"{method}{path}{body}{timestamp}"
        return hmac.new(
            self._api_secret.encode(), pre_sign.encode(), hashlib.sha256
        ).hexdigest().lower()

    def _auth_headers(self, method: str, path: str, body: str, timestamp: str) -> Dict[str, str]:
        return {
            "X-COINEX-KEY": self._api_key,
            "X-COINEX-SIGN": self._sign(method, path, body, timestamp),
            "X-COINEX-TIMESTAMP": timestamp,
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
            full_path = path + (f"?{qs}" if qs else "")
            url = f"{_BASE_URL}{full_path}"
            headers = self._auth_headers("GET", full_path, "", ts)
            async with self._session.get(url, headers=headers) as resp:
                data = await resp.json()
        else:
            body_str = _json.dumps(body or {})
            url = f"{_BASE_URL}{path}"
            headers = self._auth_headers("POST", path, body_str, ts)
            async with self._session.post(url, headers=headers, data=body_str) as resp:
                data = await resp.json()

        code = data.get("code", -1)
        if code != 0:
            logger.warning("CoinEx API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BASEUSDT (CoinEx futures use no underscore)."""
        return symbol.replace("_", "")

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/v2/assets/futures/balance")
            for asset in data.get("data", []):
                if asset.get("ccy", "").upper() == "USDT":
                    return float(asset.get("available", 0))
            return 0.0
        except Exception as exc:
            logger.error("CoinEx get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("GET", "/v2/futures/market", {"market": raw})
            items = data.get("data", [])
            if not items:
                return None
            info = items[0] if isinstance(items, list) else items
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("base_ccy", ""),
                quote_asset=info.get("quote_ccy", "USDT"),
                price_precision=_count_decimals(str(info.get("tick_size", "0.01"))),
                qty_precision=_count_decimals(str(info.get("amount_tick", "0.001"))),
                min_qty=float(info.get("min_amount", 0)),
                qty_step=float(info.get("amount_tick", 0)),
                price_step=float(info.get("tick_size", 0)),
                contract_multiplier=float(info.get("contract_val", 1)),
                max_leverage=int(float(info.get("max_leverage", 100))),
                extra=info if isinstance(info, dict) else {},
            )
        except Exception as exc:
            logger.error("CoinEx get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("POST", "/v2/futures/adjust-position-leverage", body={
                "market": raw,
                "leverage": str(leverage),
                "margin_mode": "cross_margin",
            })
            if data.get("code") == 0:
                return True
            logger.warning("CoinEx set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("CoinEx set_leverage error: %s", exc)
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
        cx_side = "buy" if side.lower() == "buy" else "sell"
        body: Dict[str, Any] = {
            "market": raw,
            "side": cx_side,
            "type": "market",
            "amount": str(qty),
            "margin_mode": "cross_margin",
        }
        if reduce_only:
            body["is_close"] = True

        try:
            data = await self._request("POST", "/v2/futures/order", body=body)
            result = data.get("data", {})
            if data.get("code") == 0:
                return OrderResult(
                    success=True,
                    order_id=str(result.get("order_id", "")),
                    exchange=self.EXCHANGE_NAME,
                    symbol=symbol,
                    side=side.lower(),
                    filled_qty=float(result.get("deal_amount", 0)),
                    filled_price=float(result.get("deal_price", 0)),
                    status="filled",
                    raw=data,
                )
            return OrderResult(
                success=False,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                error_code=data.get("code"),
                error_msg=data.get("message", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("CoinEx place_market_order error: %s", exc)
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
                "GET", "/v2/futures/pending-position", {"market": raw}
            )
            items = data.get("data", [])
            for item in items:
                size = float(item.get("amount", 0))
                if size > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("CoinEx get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request("GET", "/v2/futures/pending-position")
            positions: List[PositionInfo] = []
            for item in data.get("data", []):
                size = float(item.get("amount", 0))
                if size > 0:
                    sym = self.normalize_symbol(item.get("market", ""))
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("CoinEx get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        side_raw = item.get("side", "long")
        side = "long" if side_raw == "long" else "short"
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=float(item.get("amount", 0)),
            size_usd=float(item.get("market_value", 0)),
            entry_price=float(item.get("open_price", 0)),
            mark_price=float(item.get("close_price", 0)),
            leverage=float(item.get("leverage", 1)),
            unrealized_pnl=float(item.get("unrealized_pnl", 0)),
            margin=float(item.get("margin_amount", 0)),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places in a string like '0.001'."""
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
