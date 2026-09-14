"""MEXC futures trading client — adapter over existing MexcWebClient."""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

from app.cex_spread_trader.base_trading_client import (
    BaseTradingClient,
    ContractInfo,
    OrderResult,
    PositionInfo,
)
from app.config.settings import Config
from app.trader.mexc_web_client import MexcWebClient
from app.trader.token_manager import TokenManager

logger = logging.getLogger(__name__)


class MexcTrader(BaseTradingClient):
    """Adapter wrapping the existing MexcWebClient for CEX spread trading."""

    EXCHANGE_NAME = "mexc"

    def __init__(self):
        self._token_manager = TokenManager()
        self._client: Optional[MexcWebClient] = None
        self._contract_cache: Dict[str, Dict[str, Any]] = {}

    async def connect(self) -> None:
        self._client = MexcWebClient(self._token_manager)
        await self._client.__aenter__()

    async def close(self) -> None:
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    # ── Account ───────────────────────────────────────────────────

    async def get_balance(self) -> float:
        result = await self._client.get_account_asset("USDT")
        data = result.get("data") or {}
        return float(data.get("availableBalance") or data.get("available") or 0)

    # ── Contract info ─────────────────────────────────────────────

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        raw_symbol = symbol.replace("_", "")
        if raw_symbol in self._contract_cache:
            return self._build_contract_info(raw_symbol, self._contract_cache[raw_symbol])

        result = await self._client.get_contract_detail(raw_symbol)
        data = result.get("data")
        if not data:
            return None

        # data can be a list or a single dict
        if isinstance(data, list):
            for item in data:
                if item.get("symbol") == raw_symbol:
                    data = item
                    break
            else:
                return None

        self._contract_cache[raw_symbol] = data
        return self._build_contract_info(raw_symbol, data)

    def _build_contract_info(self, raw_symbol: str, data: Dict) -> ContractInfo:
        return ContractInfo(
            symbol=self.normalize_symbol(raw_symbol),
            raw_symbol=raw_symbol,
            base_asset=data.get("baseCoin", ""),
            quote_asset=data.get("quoteCoin", "USDT"),
            price_precision=int(data.get("priceScale") or 8),
            qty_precision=int(data.get("volScale") or 0),
            min_qty=float(data.get("minVol") or 1),
            qty_step=float(data.get("volUnit") or 1),
            price_step=float(data.get("priceUnit") or 0),
            contract_multiplier=float(data.get("contractSize") or 1),
            max_leverage=int(data.get("maxLeverage") or 100),
        )

    # ── Leverage ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        # MEXC sets leverage at order time; no separate endpoint needed
        return True

    # ── Orders ────────────────────────────────────────────────────

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = False,
    ) -> OrderResult:
        raw_symbol = symbol.replace("_", "")
        contract = await self.get_contract_info(symbol)
        if not contract:
            return OrderResult(
                success=False, exchange=self.EXCHANGE_NAME,
                symbol=symbol, error_msg="no_contract_info",
            )

        # Convert qty (base asset) to contracts
        vol = qty / max(contract.contract_multiplier, 1e-12)
        vol = max(math.floor(vol / max(contract.qty_step, 1)) * max(contract.qty_step, 1), contract.min_qty)
        vol_str = str(int(vol)) if contract.qty_step >= 1 else f"{vol:.{contract.qty_precision}f}"

        # Determine MEXC side codes
        # 1=open_long, 2=close_short, 3=open_short, 4=close_long
        side_lower = side.lower()
        if reduce_only:
            mexc_side = 4 if side_lower == "sell" else 2  # sell to close long, buy to close short
        else:
            mexc_side = 1 if side_lower == "buy" else 3

        payload: Dict[str, Any] = {
            "symbol": raw_symbol,
            "price": "0",
            "vol": vol_str,
            "side": mexc_side,
            "type": 5,  # market
            "openType": 1,  # cross
            "leverage": Config.CEX_SPREAD_TRADER_LEVERAGE,
        }

        result = await self._client.submit_order(payload)
        if isinstance(result, dict) and result.get("success") is False:
            msg = str(result.get("message") or result.get("msg") or result)
            return OrderResult(
                success=False, exchange=self.EXCHANGE_NAME, symbol=symbol,
                side=side_lower, error_code=result.get("code"),
                error_msg=msg, raw=result,
            )

        order_id = str(result.get("data") or "")
        return OrderResult(
            success=True, order_id=order_id, exchange=self.EXCHANGE_NAME,
            symbol=symbol, side=side_lower, filled_qty=qty,
            status="filled", raw=result,
        )

    # ── Positions ─────────────────────────────────────────────────

    async def get_position(self, symbol: str) -> Optional[PositionInfo]:
        raw_symbol = symbol.replace("_", "")
        result = await self._client.get_open_positions(raw_symbol)
        data = result.get("data") or []
        if not isinstance(data, list):
            data = [data]
        for pos in data:
            if pos.get("symbol") == raw_symbol:
                return self._parse_position(pos)
        return None

    async def get_all_positions(self) -> List[PositionInfo]:
        result = await self._client.get_open_positions()
        data = result.get("data") or []
        if not isinstance(data, list):
            data = [data]
        return [self._parse_position(p) for p in data if p.get("holdVol")]

    def _parse_position(self, data: Dict) -> PositionInfo:
        pos_type = data.get("positionType", 1)  # 1=long, 2=short
        return PositionInfo(
            symbol=self.normalize_symbol(data.get("symbol", "")),
            exchange=self.EXCHANGE_NAME,
            side="long" if pos_type == 1 else "short",
            size=float(data.get("holdVol") or 0),
            entry_price=float(data.get("openAvgPrice") or 0),
            mark_price=float(data.get("markPrice") or 0),
            leverage=float(data.get("leverage") or 1),
            unrealized_pnl=float(data.get("unrealisedPnl") or 0),
            raw=data,
        )

    async def close_position(self, symbol: str) -> OrderResult:
        pos = await self.get_position(symbol)
        if not pos or pos.size <= 0:
            return OrderResult(
                success=True, exchange=self.EXCHANGE_NAME,
                symbol=symbol, status="no_position",
            )
        close_side = "sell" if pos.side == "long" else "buy"
        return await self.place_market_order(
            symbol, close_side, pos.size, reduce_only=True,
        )

    def to_raw_symbol(self, symbol: str) -> str:
        return symbol.replace("_", "")
