"""KuCoin Futures API trading client."""
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

_BASE_URL = "https://api-futures.kucoin.com"


class KucoinTrader(BaseTradingClient):
    """KuCoin Futures (USDTM perpetual) trader."""

    EXCHANGE_NAME: str = "kucoin"

    # KuCoin uses XBTUSDTM for Bitcoin; most other coins use SYMBOLUSDT + M
    _SYMBOL_OVERRIDES = {"BTC": "XBT"}

    def __init__(self) -> None:
        self._api_key: str = os.getenv("KUCOIN_API_KEY", "")
        self._api_secret: str = os.getenv("KUCOIN_API_SECRET", "")
        self._passphrase: str = os.getenv("KUCOIN_PASSPHRASE", "")
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

    def _sign(self, timestamp: str, method: str, endpoint: str, body: str = "") -> str:
        """Base64(HMAC-SHA256(timestamp + method + endpoint + body))."""
        message = f"{timestamp}{method}{endpoint}{body}"
        mac = hmac.new(
            self._api_secret.encode(), message.encode(), hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _sign_passphrase(self) -> str:
        """KuCoin V2 requires passphrase to be HMAC-signed."""
        mac = hmac.new(
            self._api_secret.encode(), self._passphrase.encode(), hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _auth_headers(self, timestamp: str, method: str, endpoint: str, body: str = "") -> Dict[str, str]:
        return {
            "KC-API-KEY": self._api_key,
            "KC-API-SIGN": self._sign(timestamp, method, endpoint, body),
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": self._sign_passphrase(),
            "KC-API-KEY-VERSION": "2",
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
            endpoint = f"{path}?{qs}" if qs else path
            headers = self._auth_headers(ts, "GET", endpoint)
            url = f"{_BASE_URL}{endpoint}"
            async with self._session.get(url, headers=headers) as resp:
                data = await resp.json()
        else:
            body_str = _json.dumps(body or {})
            headers = self._auth_headers(ts, "POST", path, body_str)
            url = f"{_BASE_URL}{path}"
            async with self._session.post(url, headers=headers, data=body_str) as resp:
                data = await resp.json()

        code = data.get("code", "")
        if str(code) != "200000":
            logger.warning("KuCoin API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BASEUSDTM (with overrides like BTC -> XBT)."""
        parts = symbol.upper().replace("-", "_").split("_")
        base = parts[0] if parts else symbol
        base = self._SYMBOL_OVERRIDES.get(base, base)
        return f"{base}USDTM"

    def _raw_to_normalized(self, raw: str) -> str:
        """XBTUSDTM -> BTC_USDT, ETHUSDTM -> ETH_USDT."""
        s = raw.upper()
        if s.endswith("USDTM"):
            base = s[:-5]
            # reverse overrides
            reverse = {v: k for k, v in self._SYMBOL_OVERRIDES.items()}
            base = reverse.get(base, base)
            return f"{base}_USDT"
        return self.normalize_symbol(raw)

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/api/v1/account-overview", {
                "currency": "USDT",
            })
            result = data.get("data", {})
            return float(result.get("availableBalance", 0))
        except Exception as exc:
            logger.error("KuCoin get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("GET", f"/api/v1/contracts/{raw}")
            info = data.get("data", {})
            if not info:
                return None
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("baseCurrency", ""),
                quote_asset=info.get("quoteCurrency", "USDT"),
                price_precision=_count_decimals(str(info.get("tickSize", 0.01))),
                qty_precision=0,  # KuCoin uses lot size (integer contracts)
                min_qty=float(info.get("lotSize", 1)),
                qty_step=float(info.get("lotSize", 1)),
                price_step=float(info.get("tickSize", 0)),
                contract_multiplier=float(info.get("multiplier", 1)),
                max_leverage=int(float(info.get("maxLeverage", 100))),
                extra=info,
            )
        except Exception as exc:
            logger.error("KuCoin get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """KuCoin doesn't have a dedicated set-leverage endpoint for futures.
        Leverage is typically set per-order. We still attempt the position
        risk-limit endpoint and always return True as leverage is applied at
        order placement time."""
        # Attempt to use the margin endpoint if available, but KuCoin
        # primarily handles leverage per-order.
        raw = self.to_raw_symbol(symbol)
        try:
            # KuCoin does not have a standalone set-leverage endpoint.
            # Leverage is passed with each order. Log and return True.
            logger.info(
                "KuCoin: leverage=%d for %s will be applied at order time",
                leverage, raw,
            )
            return True
        except Exception as exc:
            logger.error("KuCoin set_leverage error: %s", exc)
            return False

    # ── orders ────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = False,
        leverage: int = 1,
    ) -> OrderResult:
        raw = self.to_raw_symbol(symbol)
        kucoin_side = "buy" if side.lower() == "buy" else "sell"
        import uuid
        client_oid = str(uuid.uuid4())
        body: Dict[str, Any] = {
            "clientOid": client_oid,
            "symbol": raw,
            "side": kucoin_side,
            "type": "market",
            "size": int(qty),
            "leverage": leverage,
        }
        if reduce_only:
            body["reduceOnly"] = True
        try:
            data = await self._request("POST", "/api/v1/orders", body=body)
            result = data.get("data", {})
            if str(data.get("code", "")) == "200000":
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
            logger.error("KuCoin place_market_order error: %s", exc)
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
            data = await self._request("GET", "/api/v1/position", {
                "symbol": raw,
            })
            item = data.get("data", {})
            if not item:
                return None
            size = abs(float(item.get("currentQty", 0)))
            if size > 0:
                return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("KuCoin get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request("GET", "/api/v1/positions")
            positions: List[PositionInfo] = []
            items = data.get("data", [])
            if isinstance(items, dict):
                items = [items]
            for item in items:
                size = abs(float(item.get("currentQty", 0)))
                if size > 0:
                    raw_sym = item.get("symbol", "")
                    sym = self._raw_to_normalized(raw_sym)
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("KuCoin get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        current_qty = float(item.get("currentQty", 0))
        side = "long" if current_qty > 0 else "short"
        size = abs(current_qty)
        mark_price = float(item.get("markPrice", 0))
        mark_value = float(item.get("markValue", 0))
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=size,
            size_usd=abs(mark_value) if mark_value else size * mark_price,
            entry_price=float(item.get("avgEntryPrice", 0)),
            mark_price=mark_price,
            leverage=float(item.get("realLeverage", 1)),
            unrealized_pnl=float(item.get("unrealisedPnl", 0)),
            margin=float(item.get("posMargin", 0)),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places in a string like '0.001'."""
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
