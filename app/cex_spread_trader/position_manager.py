"""Redis-backed paired position state for CEX spread trades."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


@dataclass
class LegState:
    """One leg of a spread trade with execution accounting."""

    exchange: str = ""
    symbol: str = ""
    raw_symbol: str = ""
    url: str = ""
    side: str = ""  # "long" / "short"
    target_base_qty: float = 0.0
    submitted_qty: float = 0.0  # exchange-native order qty submitted on open
    executed_qty: float = 0.0  # exchange-native qty opened
    remaining_qty: float = 0.0  # exchange-native qty still open
    closed_qty: float = 0.0  # exchange-native qty already closed
    executed_base_qty: float = 0.0
    remaining_base_qty: float = 0.0
    closed_base_qty: float = 0.0
    avg_entry_price: float = 0.0
    avg_exit_price: float = 0.0
    fees_open_usd: float = 0.0
    fees_close_usd: float = 0.0
    funding_usd: float = 0.0
    contract_multiplier: float = 1.0
    price_source: str = ""
    status: str = "pending"  # pending / open / closing / closed / error
    stop_loss_price: float = 0.0
    last_fill_ts: float = 0.0
    close_reason: str = ""
    error: str = ""
    order_ids: list[str] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in {"open", "closing"} and self.remaining_base_qty > 0

    @property
    def is_closed(self) -> bool:
        return self.status == "closed" or self.remaining_base_qty <= 0

    def register_order_id(self, order_id: str) -> None:
        if order_id and order_id not in self.order_ids:
            self.order_ids.append(order_id)


@dataclass
class PairedPosition:
    """A two-legged CEX spread position."""

    signal_id: str = ""
    canonical_base: str = ""
    quote_asset: str = "USDT"
    spread_open_pct: float = 0.0
    spread_current_pct: float = 0.0
    confidence: float = 0.0
    buy_leg: LegState = field(default_factory=LegState)
    sell_leg: LegState = field(default_factory=LegState)
    opened_at: float = 0.0
    updated_at: float = 0.0
    closed_at: float = 0.0
    close_reason: str = ""
    gross_pnl_usd: float = 0.0
    net_pnl_usd: float = 0.0
    fees_open_usd: float = 0.0
    fees_close_usd: float = 0.0
    funding_usd: float = 0.0
    funding_unavailable: bool = False
    status: str = "pending_open"  # pending_open / open / hedge_broken / closing / closed / error
    leverage: int = 1
    position_usd: float = 0.0
    max_position_usd: float = 0.0
    target_base_qty: float = 0.0
    avg_close_sec: Optional[float] = None
    avg_close_count: Optional[int] = None

    @property
    def pair_key(self) -> str:
        return (
            f"{self.canonical_base}:{self.quote_asset}:"
            f"{self.buy_leg.exchange}:{self.sell_leg.exchange}"
        )

    @property
    def is_fully_open(self) -> bool:
        return self.buy_leg.is_open and self.sell_leg.is_open

    @property
    def has_open_legs(self) -> bool:
        return self.buy_leg.remaining_base_qty > 0 or self.sell_leg.remaining_base_qty > 0

    def leg(self, leg_name: str) -> LegState:
        if leg_name == "buy":
            return self.buy_leg
        if leg_name == "sell":
            return self.sell_leg
        raise KeyError(f"unknown leg_name={leg_name}")

    def other_leg_name(self, leg_name: str) -> str:
        return "sell" if leg_name == "buy" else "buy"

    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict) -> "PairedPosition":
        buy = data.get("buy_leg") or {}
        sell = data.get("sell_leg") or {}
        return PairedPosition(
            signal_id=data.get("signal_id", ""),
            canonical_base=data.get("canonical_base", ""),
            quote_asset=data.get("quote_asset", "USDT"),
            spread_open_pct=float(data.get("spread_open_pct", 0) or 0),
            spread_current_pct=float(data.get("spread_current_pct", 0) or 0),
            confidence=float(data.get("confidence", 0) or 0),
            buy_leg=LegState(**_filter_leg_fields(buy)),
            sell_leg=LegState(**_filter_leg_fields(sell)),
            opened_at=float(data.get("opened_at", 0) or 0),
            updated_at=float(data.get("updated_at", 0) or 0),
            closed_at=float(data.get("closed_at", 0) or 0),
            close_reason=data.get("close_reason", ""),
            gross_pnl_usd=float(data.get("gross_pnl_usd", 0) or 0),
            net_pnl_usd=float(data.get("net_pnl_usd", 0) or 0),
            fees_open_usd=float(data.get("fees_open_usd", 0) or 0),
            fees_close_usd=float(data.get("fees_close_usd", 0) or 0),
            funding_usd=float(data.get("funding_usd", 0) or 0),
            funding_unavailable=bool(data.get("funding_unavailable", False)),
            status=data.get("status", "pending_open"),
            leverage=int(data.get("leverage", 1) or 1),
            position_usd=float(data.get("position_usd", 0) or 0),
            max_position_usd=float(data.get("max_position_usd", 0) or 0),
            target_base_qty=float(data.get("target_base_qty", 0) or 0),
            avg_close_sec=_opt_float(data.get("avg_close_sec")),
            avg_close_count=_opt_int(data.get("avg_close_count")),
        )


def _filter_leg_fields(data: Dict) -> Dict:
    return {k: data[k] for k in LegState.__dataclass_fields__ if k in data}


def _opt_float(value) -> Optional[float]:
    if value in ("", None):
        return None
    return float(value)


def _opt_int(value) -> Optional[int]:
    if value in ("", None):
        return None
    return int(value)


class PositionManager:
    """Redis-backed store for paired spread positions."""

    HASH_KEY = "cex_spread_trader:positions"

    def __init__(self, client: redis.Redis):
        self.client = client

    async def load_all(self) -> Dict[str, PairedPosition]:
        raw = await self.client.hgetall(self.HASH_KEY)
        positions: Dict[str, PairedPosition] = {}
        for key, val in raw.items():
            try:
                positions[key] = PairedPosition.from_dict(json.loads(val))
            except Exception:
                logger.warning("Failed to load position key=%s", key)
        return positions

    async def get(self, key: str) -> Optional[PairedPosition]:
        raw = await self.client.hget(self.HASH_KEY, key)
        if not raw:
            return None
        try:
            return PairedPosition.from_dict(json.loads(raw))
        except Exception:
            logger.warning("Failed to decode position key=%s", key)
            return None

    async def save(self, pos: PairedPosition) -> None:
        await self.client.hset(self.HASH_KEY, pos.pair_key, json.dumps(pos.to_dict()))

    async def delete(self, key: str) -> None:
        await self.client.hdel(self.HASH_KEY, key)
