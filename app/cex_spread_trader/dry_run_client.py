"""Dry-run trading client backed by public market data."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4

from app.cex_spread_trader.base_trading_client import (
    BaseTradingClient,
    ContractInfo,
    OrderFill,
    OrderResult,
    PositionInfo,
)
from app.cex_spread_trader.execution import compute_leg_realized_pnl_usd, native_qty_to_base_qty
from app.config.settings import Config


@dataclass
class _VirtualPosition:
    symbol: str
    side: str
    size: float
    entry_price: float
    reserved_margin_usd: float
    contract: ContractInfo


@dataclass
class _PendingOrder:
    order_id: str
    symbol: str
    side: str
    price: float
    requested_qty: float
    filled_qty: float
    remaining_qty: float
    reduce_only: bool
    status: str
    created_at: float
    fees_usd: float
    fills: List[OrderFill] = field(default_factory=list)


class DryRunTradingClient(BaseTradingClient):
    """Execution simulator using live public quotes and virtual balances."""

    def __init__(self, exchange_name: str):
        self.EXCHANGE_NAME = exchange_name
        self._available_balance = max(Config.CEX_SPREAD_TRADER_DRY_RUN_BALANCE_USD, 0.0)
        self._positions: Dict[str, _VirtualPosition] = {}
        self._pending_orders: Dict[str, _PendingOrder] = {}
        self._contract_cache: Dict[str, ContractInfo] = {}
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        await self.close_market_data_client()

    async def get_balance(self) -> float:
        return self._available_balance

    async def get_contract_info(self, symbol: str) -> Optional[ContractInfo]:
        if symbol in self._contract_cache:
            return self._contract_cache[symbol]
        info = await self.get_public_contract_info(symbol)
        if info is None:
            return None
        if not info.contract_multiplier:
            info.contract_multiplier = 1.0
        if not info.min_qty:
            info.min_qty = 0.0
        self._contract_cache[symbol] = info
        return info

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        return leverage >= 1

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        reduce_only: bool = False,
    ) -> OrderResult:
        contract = await self.get_contract_info(symbol)
        book = await self.get_top_of_book(symbol)
        if contract is None or book is None:
            return OrderResult(
                success=False,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                requested_qty=qty,
                reduce_only=reduce_only,
                order_type="market",
                error_msg="no_market_data",
            )
        fill_price = book.price_for_side(side)
        if fill_price <= 0:
            return OrderResult(
                success=False,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                requested_qty=qty,
                reduce_only=reduce_only,
                order_type="market",
                error_msg="invalid_market_price",
            )
        order_id = f"dry-{self.EXCHANGE_NAME}-{uuid4().hex[:12]}"
        fill = self._make_fill(order_id, qty, fill_price, contract)
        self._apply_fill(symbol, side, qty, fill_price, fill.fee_usd, reduce_only, contract)
        return OrderResult(
            success=True,
            order_id=order_id,
            exchange=self.EXCHANGE_NAME,
            symbol=symbol,
            side=side.lower(),
            requested_qty=qty,
            filled_qty=qty,
            remaining_qty=0.0,
            filled_price=fill_price,
            fees_usd=fill.fee_usd,
            status="filled",
            order_type="market",
            reduce_only=reduce_only,
            is_final=True,
            fill_source="dry_run_market",
            fills=[fill],
        )

    async def place_limit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        reduce_only: bool = False,
    ) -> OrderResult:
        contract = await self.get_contract_info(symbol)
        book = await self.get_top_of_book(symbol)
        if contract is None or book is None:
            return OrderResult(
                success=False,
                exchange=self.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                requested_qty=qty,
                reduce_only=reduce_only,
                order_type="limit",
                error_msg="no_market_data",
            )

        available_qty = max(book.size_for_side(side), 0.0)
        executable_price = book.price_for_side(side)
        crosses = False
        side = side.lower()
        if side == "buy":
            crosses = executable_price > 0 and price >= executable_price
        else:
            crosses = executable_price > 0 and price <= executable_price

        filled_qty = 0.0
        fills: List[OrderFill] = []
        fees_usd = 0.0
        if crosses and executable_price > 0:
            if available_qty > 0:
                filled_qty = min(qty, available_qty)
            else:
                filled_qty = qty
            if filled_qty > 0:
                fill = self._make_fill("", filled_qty, executable_price, contract)
                fills.append(fill)
                fees_usd = fill.fee_usd
                self._apply_fill(symbol, side, filled_qty, executable_price, fill.fee_usd, reduce_only, contract)

        remaining_qty = max(qty - filled_qty, 0.0)
        order_id = f"dry-{self.EXCHANGE_NAME}-{uuid4().hex[:12]}"
        for fill in fills:
            fill.order_id = order_id
        status = "filled" if remaining_qty <= 0 else ("partially_filled" if filled_qty > 0 else "open")
        pending = _PendingOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            price=price,
            requested_qty=qty,
            filled_qty=filled_qty,
            remaining_qty=remaining_qty,
            reduce_only=reduce_only,
            status=status,
            created_at=time.time(),
            fees_usd=fees_usd,
            fills=fills,
        )
        if remaining_qty > 0:
            self._pending_orders[order_id] = pending
        return OrderResult(
            success=True,
            order_id=order_id,
            exchange=self.EXCHANGE_NAME,
            symbol=symbol,
            side=side,
            requested_qty=qty,
            filled_qty=filled_qty,
            remaining_qty=remaining_qty,
            filled_price=executable_price if filled_qty > 0 else 0.0,
            fees_usd=fees_usd,
            status=status,
            order_type="limit",
            reduce_only=reduce_only,
            is_final=remaining_qty <= 0,
            fill_source="dry_run_limit",
            fills=fills,
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        pending = self._pending_orders.pop(order_id, None)
        if pending is None:
            return False
        pending.status = "cancelled"
        return True

    async def get_order_status(self, symbol: str, order_id: str) -> Optional[OrderResult]:
        pending = self._pending_orders.get(order_id)
        if pending is None:
            return None
        filled_price = 0.0
        if pending.fills:
            total_qty = sum(fill.qty for fill in pending.fills)
            if total_qty > 0:
                filled_price = sum(fill.qty * fill.price for fill in pending.fills) / total_qty
        return OrderResult(
            success=True,
            order_id=order_id,
            exchange=self.EXCHANGE_NAME,
            symbol=symbol,
            side=pending.side,
            requested_qty=pending.requested_qty,
            filled_qty=pending.filled_qty,
            remaining_qty=pending.remaining_qty,
            filled_price=filled_price,
            fees_usd=pending.fees_usd,
            status=pending.status,
            order_type="limit",
            reduce_only=pending.reduce_only,
            is_final=pending.remaining_qty <= 0,
            fill_source="dry_run_limit",
            fills=list(pending.fills),
        )

    async def get_order_fills(self, symbol: str, order_id: str) -> List[OrderFill]:
        pending = self._pending_orders.get(order_id)
        if pending is None:
            return []
        return list(pending.fills)

    async def get_position(self, symbol: str) -> Optional[PositionInfo]:
        position = self._positions.get(symbol)
        if position is None or position.size <= 0:
            return None
        book = await self.get_top_of_book(symbol)
        mark_price = book.price_for_side("sell" if position.side == "long" else "buy") if book else position.entry_price
        return PositionInfo(
            symbol=symbol,
            exchange=self.EXCHANGE_NAME,
            side=position.side,
            size=position.size,
            size_usd=native_qty_to_base_qty(position.size, position.contract) * max(mark_price, 0.0),
            entry_price=position.entry_price,
            mark_price=mark_price,
            leverage=float(Config.CEX_SPREAD_TRADER_LEVERAGE),
            unrealized_pnl=0.0,
            margin=position.reserved_margin_usd,
        )

    async def get_all_positions(self) -> List[PositionInfo]:
        result: List[PositionInfo] = []
        for symbol in list(self._positions):
            position = await self.get_position(symbol)
            if position is not None:
                result.append(position)
        return result

    def supports_limit_orders(self) -> bool:
        return True

    def supports_order_tracking(self) -> bool:
        return True

    def supports_fee_tracking(self) -> bool:
        return True

    def get_taker_fee_rate(self, symbol: str = "") -> float:
        return super().get_taker_fee_rate(symbol)

    def _make_fill(self, order_id: str, qty: float, price: float, contract: ContractInfo) -> OrderFill:
        base_qty = native_qty_to_base_qty(qty, contract)
        fee_usd = self.estimate_fee_usd(base_qty * price, contract.symbol)
        return OrderFill(
            qty=qty,
            price=price,
            fee_usd=fee_usd,
            fee_currency=contract.quote_asset or "USDT",
            timestamp_ms=int(time.time() * 1000),
            liquidity="taker",
            order_id=order_id,
        )

    def _apply_fill(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        fee_usd: float,
        reduce_only: bool,
        contract: ContractInfo,
    ) -> None:
        side = side.lower()
        leverage = max(Config.CEX_SPREAD_TRADER_LEVERAGE, 1)
        position = self._positions.get(symbol)
        base_qty = native_qty_to_base_qty(qty, contract)
        notional_usd = base_qty * max(price, 0.0)

        if reduce_only:
            if position is None or position.size <= 0:
                self._available_balance = max(self._available_balance - fee_usd, 0.0)
                return
            close_qty = min(qty, position.size)
            close_base_qty = native_qty_to_base_qty(close_qty, contract)
            released_margin = 0.0
            if position.size > 0 and position.reserved_margin_usd > 0:
                released_margin = position.reserved_margin_usd * (close_qty / position.size)
            realized_pnl = compute_leg_realized_pnl_usd(
                side=position.side,
                base_qty=close_base_qty,
                entry_price=position.entry_price,
                exit_price=price,
                fees_open_usd=0.0,
                fees_close_usd=0.0,
                funding_usd=0.0,
            )
            position.size = max(position.size - close_qty, 0.0)
            position.reserved_margin_usd = max(position.reserved_margin_usd - released_margin, 0.0)
            self._available_balance += released_margin + realized_pnl - fee_usd
            if position.size <= 0:
                self._positions.pop(symbol, None)
            return

        margin_used = notional_usd / leverage if leverage > 0 else notional_usd
        self._available_balance = max(self._available_balance - margin_used - fee_usd, 0.0)
        if position is None or position.size <= 0:
            self._positions[symbol] = _VirtualPosition(
                symbol=symbol,
                side="long" if side == "buy" else "short",
                size=qty,
                entry_price=price,
                reserved_margin_usd=margin_used,
                contract=contract,
            )
            return

        if position.side == ("long" if side == "buy" else "short"):
            new_size = position.size + qty
            if new_size > 0:
                position.entry_price = ((position.entry_price * position.size) + (price * qty)) / new_size
            position.size = new_size
            position.reserved_margin_usd += margin_used
            return

        close_qty = min(qty, position.size)
        close_base_qty = native_qty_to_base_qty(close_qty, contract)
        released_margin = 0.0
        if position.size > 0 and position.reserved_margin_usd > 0:
            released_margin = position.reserved_margin_usd * (close_qty / position.size)
        realized_pnl = compute_leg_realized_pnl_usd(
            side=position.side,
            base_qty=close_base_qty,
            entry_price=position.entry_price,
            exit_price=price,
        )
        position.size = max(position.size - close_qty, 0.0)
        position.reserved_margin_usd = max(position.reserved_margin_usd - released_margin, 0.0)
        self._available_balance += released_margin + realized_pnl
        if position.size <= 0:
            self._positions.pop(symbol, None)
