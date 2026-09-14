"""MEXC web (futures) client using session token."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Dict, Optional

import aiohttp
import asyncio

from app.config.settings import Config
from app.trader.token_manager import TokenManager

logger = logging.getLogger(__name__)


DEFAULT_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9,ru;q=0.8",
    "cache-control": "no-cache",
    "content-type": "application/json",
    "dnt": "1",
    "language": "English",
    "origin": "https://www.mexc.com",
    "pragma": "no-cache",
    "referer": "https://www.mexc.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": Config.MEXC_WEB_USER_AGENT,
    "x-language": "en-US",
}


class MexcAuthError(RuntimeError):
    """Raised when MEXC private API auth cannot be recovered automatically."""


class MexcWebClient:
    """Async client for MEXC futures web API."""

    def __init__(self, token_manager: TokenManager):
        self.token_manager = token_manager
        self.base_url = Config.MEXC_WEB_BASE_URL.rstrip("/")
        total_timeout = max(10, Config.MEXC_WEB_TIMEOUT_SEC)
        self.timeout = aiohttp.ClientTimeout(
            total=total_timeout,
            connect=min(10, total_timeout),
            sock_read=total_timeout,
        )
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self.session

    @staticmethod
    def _serialize_body(body: Any) -> str:
        if body is None:
            return ""
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    def _sign_body(self, token: str, body_json: str) -> Dict[str, str]:
        now = str(int(time.time() * 1000))
        raw = f"{token}{now}"
        g = hashlib.md5(raw.encode()).hexdigest()[7:]
        sign = hashlib.md5(f"{now}{body_json}{g}".encode()).hexdigest()
        return {"x-mxc-nonce": now, "x-mxc-sign": sign}

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Any] = None,
        auth: bool = False,
        base_url: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        session = await self._get_session()
        base = (base_url or self.base_url).rstrip("/")
        url = f"{base}{path}"
        headers = dict(DEFAULT_HEADERS)
        if extra_headers:
            headers.update(extra_headers)

        body_json = self._serialize_body(body)

        if auth:
            token = await self.token_manager.get_token()
            headers["authorization"] = token
            headers.update(self._sign_body(token, body_json))

        proxy = Config.MEXC_WEB_PROXY or None
        for attempt in range(2):
            try:
                async with session.request(
                    method,
                    url,
                    params=params,
                    data=body_json if body is not None else None,
                    headers=headers,
                    proxy=proxy,
                ) as response:
                    if Config.TRADER_LOG_HTTP:
                        logger.info(
                            "HTTP %s %s status=%s proxy=%s",
                            method,
                            path,
                            response.status,
                            "on" if proxy else "off",
                        )
                    body = await response.text()
                    if not body or not body.strip():
                        raise RuntimeError(
                            f"MEXC HTTP {response.status}: empty response body for {method} {path}"
                        )
                    import json as _json
                    data = _json.loads(body)
                    auth_error = self._is_auth_error(response.status, data) if auth else False
                    if auth_error and attempt == 0:
                        if Config.MEXC_WEB_REFRESH_ENABLED:
                            logger.warning("MEXC auth expired for %s %s, refreshing browser session", method, path)
                            try:
                                token = await self.token_manager.refresh_token(force=True)
                            except Exception as exc:
                                raise MexcAuthError(f"MEXC auth refresh failed: {exc}") from exc
                            headers["authorization"] = token
                            headers.update(self._sign_body(token, body_json))
                            continue
                        raise MexcAuthError(
                            f"MEXC HTTP {response.status}: {data}. Send a fresh WEB token via Telegram reply."
                        )
                    if response.status >= 400:
                        if auth_error:
                            raise MexcAuthError(f"MEXC HTTP {response.status}: {data}")
                        raise RuntimeError(f"MEXC HTTP {response.status}: {data}")
                    return data
            except asyncio.TimeoutError:
                logger.warning("HTTP timeout method=%s path=%s proxy=%s", method, path, "on" if proxy else "off")
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise
            except aiohttp.ClientError as exc:
                logger.warning("HTTP client error method=%s path=%s err=%s", method, path, exc)
                if attempt == 0:
                    await asyncio.sleep(0.5)
                    continue
                raise
        raise MexcAuthError("MEXC request failed after automatic session refresh")

    @staticmethod
    def _is_auth_error(status: int, data: Dict[str, Any]) -> bool:
        if status in (401, 403):
            return True
        if isinstance(data, dict):
            if data.get("success") is False and data.get("code") in (10001, 401, 403):
                return True
            msg = str(data.get("message") or data.get("msg") or "").lower()
            if "auth" in msg or "token" in msg or "signature" in msg:
                return True
        return False

    async def submit_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/private/order/submit", body=payload, auth=True)

    async def cancel_order(self, order_ids: list) -> Dict[str, Any]:
        return await self._request("POST", "/private/order/cancel", body=order_ids, auth=True)

    async def place_stop_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        # Try the main base_url first (futures.mexc.com), fall back to
        # the platform URL only if mtoken is configured.
        try:
            return await self._request(
                "POST",
                "/private/stoporder/place/v2",
                body=payload,
                auth=True,
            )
        except Exception as primary_err:
            if not Config.MEXC_WEB_MTOKEN:
                raise
            logger.warning("Stop-loss primary URL failed (%s), trying platform URL", primary_err)
            extra_headers = {"platform": "H5-web", "mtoken": Config.MEXC_WEB_MTOKEN}
            params = {"mhash": Config.MEXC_WEB_MHASH} if Config.MEXC_WEB_MHASH else None
            return await self._request(
                "POST",
                "/private/stoporder/place/v2",
                params=params,
                body=payload,
                auth=True,
                base_url=Config.MEXC_WEB_STOP_BASE_URL,
                extra_headers=extra_headers,
            )

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        if symbol:
            payload["symbol"] = symbol
        return await self._request("POST", "/private/order/cancel_all", body=payload, auth=True)

    async def get_open_positions(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        params = {"symbol": symbol} if symbol else None
        return await self._request("GET", "/private/position/open_positions", params=params, auth=True)

    async def get_account_asset(self, currency: str = "USDT") -> Dict[str, Any]:
        return await self._request("GET", f"/private/account/asset/{currency}", auth=True)

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return await self._request("GET", f"/private/order/get/{order_id}", auth=True)

    async def get_contract_detail(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        params = {"symbol": symbol} if symbol else None
        return await self._request("GET", "/contract/detail", params=params, auth=False)

    async def get_ticker(self, symbol: str) -> Dict[str, Any]:
        return await self._request("GET", "/contract/ticker", params={"symbol": symbol}, auth=False)
