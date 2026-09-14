"""Execution math helpers for CEX spread trader."""
from __future__ import annotations

from app.cex_spread_trader.base_trading_client import ContractInfo, TopOfBook


def round_down(value: float, step: float, precision: int) -> float:
    if value <= 0:
        return 0.0
    if step and step > 0:
        value = int(value / step) * step
    if precision < 0:
        precision = 0
    return float(f"{value:.{precision}f}")


def native_qty_to_base_qty(native_qty: float, contract: ContractInfo) -> float:
    multiplier = float(contract.contract_multiplier or 1.0)
    return max(native_qty, 0.0) * max(multiplier, 0.0)


def base_qty_to_native_qty(base_qty: float, contract: ContractInfo) -> float:
    multiplier = float(contract.contract_multiplier or 1.0)
    if base_qty <= 0 or multiplier <= 0:
        return 0.0
    raw_qty = base_qty / multiplier
    qty = round_down(
        raw_qty,
        float(contract.qty_step or 0.0),
        int(contract.qty_precision or 8),
    )
    min_qty = float(contract.min_qty or 0.0)
    if min_qty > 0 and 0 < qty < min_qty:
        qty = min_qty
    return qty


def compute_common_base_qty(
    target_usd: float,
    buy_price: float,
    sell_price: float,
    max_position_usd: float | None = None,
) -> float:
    reference_price = max(buy_price or 0.0, sell_price or 0.0)
    if target_usd <= 0 or reference_price <= 0:
        return 0.0
    capped_usd = target_usd
    if max_position_usd is not None and max_position_usd > 0:
        capped_usd = min(capped_usd, max_position_usd)
    return capped_usd / reference_price


def compute_limit_price(side: str, book: TopOfBook, slippage_pct: float) -> float:
    side = (side or "").lower()
    slippage_pct = max(slippage_pct or 0.0, 0.0)
    if side == "buy":
        ask = float(book.ask_price or 0.0)
        if ask <= 0:
            return 0.0
        return ask * (1.0 + slippage_pct)
    bid = float(book.bid_price or 0.0)
    if bid <= 0:
        return 0.0
    return bid * (1.0 - slippage_pct)


def compute_leg_stop_price(side: str, entry_price: float, stop_loss_pct: float) -> float:
    if entry_price <= 0:
        return 0.0
    stop_loss_pct = max(stop_loss_pct or 0.0, 0.0)
    side = (side or "").lower()
    if side == "long":
        return entry_price * (1.0 - stop_loss_pct)
    return entry_price * (1.0 + stop_loss_pct)


def is_leg_stop_triggered(side: str, entry_price: float, current_price: float, stop_loss_pct: float) -> bool:
    if entry_price <= 0 or current_price <= 0:
        return False
    stop_price = compute_leg_stop_price(side, entry_price, stop_loss_pct)
    side = (side or "").lower()
    if side == "long":
        return current_price <= stop_price
    return current_price >= stop_price


def compute_leg_realized_pnl_usd(
    side: str,
    base_qty: float,
    entry_price: float,
    exit_price: float,
    fees_open_usd: float = 0.0,
    fees_close_usd: float = 0.0,
    funding_usd: float = 0.0,
) -> float:
    if base_qty <= 0 or entry_price <= 0 or exit_price <= 0:
        return -(max(fees_open_usd, 0.0) + max(fees_close_usd, 0.0)) + funding_usd
    side = (side or "").lower()
    if side == "long":
        gross = (exit_price - entry_price) * base_qty
    else:
        gross = (entry_price - exit_price) * base_qty
    return gross - max(fees_open_usd, 0.0) - max(fees_close_usd, 0.0) + funding_usd
