"""OKX API futures trading client."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json as _json
import logging
import os
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

_BASE_URL = "https://www.okx.com"


class OkxTrader(BaseTradingClient):
    """OKX USDT-margined perpetual swap trader."""

    EXCHANGE_NAME: str = "okx"

    def __init__(self) -> None:
        self._api_key: str = os.getenv("OKX_API_KEY", "")
        self._api_secret: str = os.getenv("OKX_API_SECRET", "")
        self._passphrase: str = os.getenv("OKX_PASSPHRASE", "")
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

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        pre_sign = f"{ts}{method.upper()}{path}{body}"
        mac = hmac.new(
            self._api_secret.encode(), pre_sign.encode(), hashlib.sha256
        ).digest()
        return base64.b64encode(mac).decode()

    def _auth_headers(self, ts: str, method: str, path: str, body: str = "") -> Dict[str, str]:
        return {
            "OK-ACCESS-KEY": self._api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._passphrase,
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
        ts = self._timestamp()

        if method == "GET":
            qs = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
            full_path = f"{path}?{qs}" if qs else path
            headers = self._auth_headers(ts, "GET", full_path)
            url = f"{_BASE_URL}{full_path}"
            async with self._session.get(url, headers=headers) as resp:
                data = await resp.json()
        else:
            body_str = _json.dumps(body or {})
            headers = self._auth_headers(ts, "POST", path, body_str)
            url = f"{_BASE_URL}{path}"
            async with self._session.post(url, headers=headers, data=body_str) as resp:
                data = await resp.json()

        code = data.get("code", "-1")
        if code != "0":
            logger.warning("OKX API error %s %s: %s", method, path, data)
        return data

    # ── symbol conversion ─────────────────────────────────────────

    def to_raw_symbol(self, symbol: str) -> str:
        """BASE_USDT -> BTC-USDT-SWAP."""
        base, quote = symbol.split("_", 1) if "_" in symbol else (symbol.replace("USDT", ""), "USDT")
        return f"{base}-{quote}-SWAP"

    # ── account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        try:
            data = await self._request("GET", "/api/v5/account/balance")
            for detail in data.get("data", [{}])[0].get("details", []):
                if detail.get("ccy") == "USDT":
                    return float(detail.get("availBal", 0))
        except Exception as exc:
            logger.error("OKX get_balance error: %s", exc)
        return 0.0

    # ── contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request(
                "GET",
                "/api/v5/public/instruments",
                {"instType": "SWAP", "instId": raw},
            )
            items = data.get("data", [])
            if not items:
                return None
            info = items[0]
            return ContractInfo(
                symbol=symbol,
                raw_symbol=raw,
                base_asset=info.get("ctValCcy", ""),
                quote_asset=info.get("settleCcy", "USDT"),
                price_precision=_count_decimals(info.get("tickSz", "0.01")),
                qty_precision=_count_decimals(info.get("lotSz", "0.001")),
                min_qty=float(info.get("minSz", 0)),
                qty_step=float(info.get("lotSz", 0)),
                price_step=float(info.get("tickSz", 0)),
                contract_multiplier=float(info.get("ctVal", 1)),
                max_leverage=int(float(info.get("lever", 100))),
            )
        except Exception as exc:
            logger.error("OKX get_contract_info error: %s", exc)
            return None

    # ── leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        raw = self.to_raw_symbol(symbol)
        try:
            data = await self._request("POST", "/api/v5/account/set-leverage", body={
                "instId": raw,
                "lever": str(leverage),
                "mgnMode": "cross",
            })
            if data.get("code") == "0":
                return True
            logger.warning("OKX set_leverage failed: %s", data)
            return False
        except Exception as exc:
            logger.error("OKX set_leverage error: %s", exc)
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
        try:
            body: Dict[str, Any] = {
                "instId": raw,
                "tdMode": "cross",
                "side": side.lower(),
                "ordType": "market",
                "sz": str(qty),
            }
            if reduce_only:
                body["reduceOnly"] = True
            data = await self._request("POST", "/api/v5/trade/order", body=body)
            entries = data.get("data", [{}])
            entry = entries[0] if entries else {}
            if data.get("code") == "0" and entry.get("sCode") == "0":
                return OrderResult(
                    success=True,
                    order_id=entry.get("ordId", ""),
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
                error_code=int(entry.get("sCode", -1)) if entry.get("sCode") else None,
                error_msg=entry.get("sMsg", data.get("msg", "")),
                raw=data,
            )
        except Exception as exc:
            logger.error("OKX place_market_order error: %s", exc)
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
                "/api/v5/account/positions",
                {"instType": "SWAP", "instId": raw},
            )
            for item in data.get("data", []):
                pos_amt = float(item.get("pos", 0))
                if pos_amt != 0:
                    return self._parse_position(symbol, item)
            return None
        except Exception as exc:
            logger.error("OKX get_position error: %s", exc)
            return None

    async def get_all_positions(self) -> List[PositionInfo]:
        try:
            data = await self._request(
                "GET",
                "/api/v5/account/positions",
                {"instType": "SWAP"},
            )
            positions: List[PositionInfo] = []
            for item in data.get("data", []):
                pos_amt = float(item.get("pos", 0))
                if pos_amt != 0:
                    inst_id = item.get("instId", "")
                    sym = self._inst_id_to_symbol(inst_id)
                    positions.append(self._parse_position(sym, item))
            return positions
        except Exception as exc:
            logger.error("OKX get_all_positions error: %s", exc)
            return []

    def _inst_id_to_symbol(self, inst_id: str) -> str:
        """BTC-USDT-SWAP -> BTC_USDT."""
        parts = inst_id.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}_{parts[1]}"
        return inst_id

    def _parse_position(self, symbol: str, item: Dict[str, Any]) -> PositionInfo:
        pos_amt = float(item.get("pos", 0))
        side = "long" if item.get("posSide") == "long" or pos_amt > 0 else "short"
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=side,
            size=abs(pos_amt),
            size_usd=abs(float(item.get("notionalUsd", 0))),
            entry_price=float(item.get("avgPx", 0) or 0),
            mark_price=float(item.get("markPx", 0) or 0),
            leverage=float(item.get("lever", 1) or 1),
            unrealized_pnl=float(item.get("upl", 0) or 0),
            margin=float(item.get("margin", 0) or 0),
            raw=item,
        )


def _count_decimals(value: str) -> int:
    if "." in value:
        return len(value.rstrip("0").split(".")[1])
    return 0
