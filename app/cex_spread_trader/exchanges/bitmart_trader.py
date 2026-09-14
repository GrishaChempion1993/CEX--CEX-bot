"""BitMart Futures API trading client."""
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

_BASE_URL = "https://api-cloud-v2.bitmart.com"


class BitmartTrader(BaseTradingClient):
    """BitMart Futures trading client."""

    EXCHANGE_NAME: str = "bitmart"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("BITMART_API_KEY", "")
        self._api_secret: str = os.getenv("BITMART_API_SECRET", "")
        self._memo: str = os.getenv("BITMART_MEMO", "")
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

    def _sign(self, timestamp: str, body: str) -> str:
        """HMAC-SHA256(timestamp + '#' + memo + '#' + body)."""
        pre_sign = f"{timestamp}#{self._memo}#{body}"
        return hmac.new(
            self._api_secret.encode(), pre_sign.encode(), hashlib.sha256
        ).hexdigest()

    def _auth_headers(self, timestamp: str, body: str) -> Dict[str, str]:
        return {
            "X-BM-KEY": self._api_key,
            "X-BM-SIGN": self._sign(timestamp, body),
            "X-BM-TIMESTAMP": timestamp,
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
            url = f"{_BASE_URL}{path}" + (f"?{qs}" if qs else "")
            headers = self._auth_headers(ts, "")
            async with self._session.get(url, headers=headers) as resp:
                data = await resp.json()
        else:
            body_str = _json.dumps(body or {})
            url = f"{_BASE_URL}{path}"
            headers = self._auth_headers(ts, body_str)
            async with self._session.post(url, headers=headers, data=body_str) as resp:
                data = await resp.json()

        code = data.get("code", -1)
        if code != 1000:
            logger.warning("BitMart API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BASEUSDT (BitMart futures use no underscore)."""
        return symbol.replace("_", "")

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/contract/private/assets-detail")
            for asset in data.get("data", []):
                if asset.get("currency", "").upper() == "USDT":
                    return float(asset.get("available_balance", 0))
        except Exception as exc:
            logger.error("BitMart get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "GET", "/contract/public/details", {"symbol": raw}
            )
            symbols_list = data.get("data", {}).get("symbols", [])
            if not symbols_list:
                return None
            info = symbols_list[0]
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("base_currency", ""),
                quote_asset=info.get("quote_currency", "USDT"),
                price_precision=_count_decimals(str(info.get("price_precision", "0.01"))),
                qty_precision=_count_decimals(str(info.get("vol_precision", "0.001"))),
                min_qty=float(info.get("min_volume", 0)),
                qty_step=float(info.get("vol_precision", 0)),
                price_step=float(info.get("price_precision", 0)),
                contract_multiplier=float(info.get("contract_size", 1)),
                max_leverage=int(float(info.get("max_leverage", 100))),
                extra=info,
            )
        except Exception as exc:
            logger.error("BitMart get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("POST", "/contract/private/submit-leverage", body={
                "symbol": raw,
                "leverage": str(leverage),
                "open_type": "cross",
            })
            if data.get("code") == 1000:
                return True
            logger.warning("BitMart set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("BitMart set_leverage error: %s", exc)
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
        # BitMart sides: 1=buy_open, 2=buy_close, 3=sell_open, 4=sell_close
        if side.lower() == "buy":
            bm_side = 2 if reduce_only else 1
        else:
            bm_side = 4 if reduce_only else 3

        try:
            data = await self._request("POST", "/contract/private/submit-order", body={
                "symbol": raw,
                "side": bm_side,
                "type": "market",
                "leverage": "20",
                "open_type": "cross",
                "size": int(qty),
            })
            result = data.get("data", {})
            if data.get("code") == 1000:
                return OrderResult(
                    success=True,
                    order_id=str(result.get("order_id", "")),
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
                error_code=data.get("code"),
                error_msg=data.get("message", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("BitMart place_market_order error: %s", exc)
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
                "GET", "/contract/private/position", {"symbol": raw}
            )
            items = data.get("data", [])
            for item in items:
                size = float(item.get("current_amount", 0))
                if size > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("BitMart get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request("GET", "/contract/private/position")
            positions: List[PositionInfo] = []
            for item in data.get("data", []):
                size = float(item.get("current_amount", 0))
                if size > 0:
                    sym = self.normalize_symbol(item.get("symbol", ""))
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("BitMart get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        # BitMart position_type: 1=long, 2=short
        pos_type = item.get("position_type", 1)
        side = "long" if pos_type == 1 else "short"
        size = float(item.get("current_amount", 0))
        entry = float(item.get("open_avg_price", 0))
        mark = float(item.get("current_value", 0))
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=size,
            size_usd=float(item.get("current_value", 0)),
            entry_price=entry,
            mark_price=mark,
            leverage=float(item.get("leverage", 1)),
            unrealized_pnl=float(item.get("unrealized_value", 0)),
            margin=float(item.get("im", 0)),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places in a string like '0.001'."""
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
