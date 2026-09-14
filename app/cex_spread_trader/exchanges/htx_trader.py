"""HTX (Huobi) Linear Swap API futures trading client."""
from __future__ import annotations

import hashlib
import hmac
import json as _json
import logging
import os
import urllib.parse
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

_BASE_URL = "https://api.hbdm.com"
_HOST = "api.hbdm.com"


class HtxTrader(BaseTradingClient):
    """HTX (Huobi) USDT-M Linear Swap futures trader."""

    EXCHANGE_NAME: str = "htx"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("HTX_API_KEY", "")
        self._api_secret: str = os.getenv("HTX_API_SECRET", "")
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

    def _utc_timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def _sign(self, method: str, path: str, params: Dict[str, str]) -> str:
        sorted_params = "&".join(
            f"{k}={urllib.parse.quote(str(v), safe='')}"
            for k, v in sorted(params.items())
        )
        pre_sign = f"{method}\n{_HOST}\n{path}\n{sorted_params}"
        sig = hmac.new(
            self._api_secret.encode(), pre_sign.encode(), hashlib.sha256
        ).digest()
        import base64
        return base64.b64encode(sig).decode()

    def _build_auth_params(self) -> Dict[str, str]:
        return {
            "AccessKeyId": self._api_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": self._utc_timestamp(),
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        assert self._session, "call connect() first"
        auth_params = self._build_auth_params()

        if method == "GET" and params:
            auth_params.update(params)

        signature = self._sign(method, path, auth_params)
        auth_params["Signature"] = signature

        qs = urllib.parse.urlencode(auth_params)
        url = f"{_BASE_URL}{path}?{qs}"

        try:
            if method == "GET":
                async with self._session.get(url) as resp:
                    data = await resp.json(content_type=None)
            else:
                headers = {"Content-Type": "application/json"}
                async with self._session.post(
                    url, headers=headers, data=_json.dumps(body or {})
                ) as resp:
                    data = await resp.json(content_type=None)
        except Exception as exc:
            logger.error("HTX request error %s %s: %s", method, path, exc)
            return {"status": "error", "err_msg": str(exc)}

        if data.get("status") == "error":
            logger.warning("HTX API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BTC-USDT."""
        return symbol.replace("_", "-")

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request(
                "POST",
                "/linear-swap-api/v1/swap_cross_account_info",
                body={"margin_account": "USDT"},
            )
            if data.get("status") == "ok":
                info_list = data.get("data", [])
                if info_list:
                    return float(info_list[0].get("margin_available", 0))
        except Exception as exc:
            logger.error("HTX get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "GET",
                "/linear-swap-api/v1/swap_contract_info",
                params={"contract_code": raw},
            )
            items = data.get("data", [])
            if not items:
                return None
            info = items[0]
            price_step = float(info.get("price_tick", 0.01))
            contract_size = float(info.get("contract_size", 1))
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("symbol", ""),
                quote_asset="USDT",
                price_precision=_count_decimals(str(price_step)),
                qty_precision=0,  # HTX uses integer volume (# of contracts)
                min_qty=1.0,
                qty_step=1.0,
                price_step=price_step,
                contract_multiplier=contract_size,
                max_leverage=int(info.get("max_leverage", 100)),
                extra={"contract_type": info.get("contract_type", "")},
            )
        except Exception as exc:
            logger.error("HTX get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "POST",
                "/linear-swap-api/v1/swap_cross_switch_lever_rate",
                body={"contract_code": raw, "lever_rate": leverage},
            )
            if data.get("status") == "ok":
                return True
            # Already at this leverage
            err_code = data.get("err_code", 0)
            if err_code == 1037:
                return True
            logger.warning("HTX set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("HTX set_leverage error: %s", exc)
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
        direction = "buy" if side.lower() == "buy" else "sell"
        offset = "close" if reduce_only else "open"
        try:
            data = await self._request(
                "POST",
                "/linear-swap-api/v1/swap_cross_order",
                body={
                    "contract_code": raw,
                    "volume": int(qty) if qty == int(qty) else qty,
                    "direction": direction,
                    "offset": offset,
                    "order_price_type": "optimal_20",
                    "lever_rate": 20,
                },
            )
            if data.get("status") == "ok":
                result = data.get("data", {})
                return OrderResult(
                    success=True,
                    order_id=str(result.get("order_id", "")),
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
                error_code=data.get("err_code"),
                error_msg=data.get("err_msg", ""),
                raw=data,
            )
        except Exception as exc:
            logger.error("HTX place_market_order error: %s", exc)
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
                "POST",
                "/linear-swap-api/v1/swap_cross_position_info",
                body={"contract_code": raw},
            )
            items = data.get("data", [])
            for item in items:
                volume = float(item.get("volume", 0))
                if volume > 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("HTX get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request(
                "POST",
                "/linear-swap-api/v1/swap_cross_position_info",
                body={},
            )
            positions: List[PositionInfo] = []
            for item in data.get("data", []):
                volume = float(item.get("volume", 0))
                if volume > 0:
                    sym = self.normalize_symbol(
                        item.get("contract_code", "").replace("-", "_")
                    )
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("HTX get_all_positions error: %s", exc)
            return []

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        direction = item.get("direction", "")
        side = "long" if direction == "buy" else "short"
        volume = float(item.get("volume", 0))
        cost_open = float(item.get("cost_open", 0))
        last_price = float(item.get("last_price", 0))
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=volume,
            size_usd=volume * float(item.get("contract_size", 1)) * last_price,
            entry_price=cost_open,
            mark_price=last_price,
            leverage=float(item.get("lever_rate", 1)),
            unrealized_pnl=float(item.get("profit_unreal", 0)),
            margin=float(item.get("position_margin", 0)),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    """Count decimal places in a string like '0.001'."""
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
