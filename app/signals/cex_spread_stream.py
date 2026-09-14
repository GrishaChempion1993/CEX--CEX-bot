"""Redis publisher for standalone CEX-CEX spread signals."""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import redis.asyncio as redis

from app.config.settings import Config
from app.core.models import CexSpreadSignal

logger = logging.getLogger(__name__)


class CexSpreadRedisPublisher:
    """Publish open/close spread signals to a dedicated Redis stream."""

    def __init__(
        self,
        redis_url: str = Config.REDIS_URL,
        stream_key: str = Config.CEX_SPREAD_STREAM_KEY,
        maxlen: int = Config.CEX_SPREAD_STREAM_MAXLEN,
    ):
        self.redis_url = redis_url
        self.stream_key = stream_key
        self.maxlen = maxlen
        self._redis: Optional[redis.Redis] = None

    async def _get_client(self) -> redis.Redis:
        if not self._redis:
            self._redis = redis.from_url(self.redis_url, decode_responses=True)
        return self._redis

    async def close(self) -> None:
        if self._redis:
            await self._redis.close()
            self._redis = None

    def _payload_for_signal(self, signal: CexSpreadSignal) -> dict[str, str]:
        closed_at = signal.closed_at.isoformat() if signal.closed_at else ""
        return {
            "event_type": signal.event_type,
            "signal_family": signal.signal_family,
            "signal_id": signal.signal_id,
            "canonical_base": signal.canonical_base,
            "quote_asset": signal.quote_asset,
            "unit_scale": f"{signal.unit_scale:g}",
            "spread_open_pct": f"{signal.spread_open_pct:.6f}",
            "spread_current_pct": f"{signal.spread_current_pct:.6f}",
            "confidence": f"{signal.confidence:.4f}",
            "leg_a_exchange": signal.buy_leg.exchange,
            "leg_a_symbol": signal.buy_leg.symbol,
            "leg_a_raw_symbol": signal.buy_leg.raw_symbol or "",
            "leg_a_url": signal.buy_leg.url or "",
            "leg_a_price": f"{signal.buy_leg.price:.10f}",
            "leg_a_price_source": signal.buy_leg.price_source,
            "leg_a_bid_price": f"{signal.buy_leg.bid_price:.10f}" if signal.buy_leg.bid_price is not None else "",
            "leg_a_ask_price": f"{signal.buy_leg.ask_price:.10f}" if signal.buy_leg.ask_price is not None else "",
            "leg_a_bid_size": f"{signal.buy_leg.bid_size:.8f}" if signal.buy_leg.bid_size is not None else "",
            "leg_a_ask_size": f"{signal.buy_leg.ask_size:.8f}" if signal.buy_leg.ask_size is not None else "",
            "leg_a_liquidity_usd": f"{signal.buy_leg.liquidity_usd:.4f}" if signal.buy_leg.liquidity_usd is not None else "",
            "leg_a_volume_24h_usd": f"{signal.buy_leg.volume_24h_usd:.4f}" if signal.buy_leg.volume_24h_usd is not None else "",
            "leg_a_funding_rate": f"{signal.buy_leg.funding_rate:.8f}" if signal.buy_leg.funding_rate is not None else "",
            "leg_a_timestamp_ms": str(signal.buy_leg.timestamp_ms or ""),
            "leg_b_exchange": signal.sell_leg.exchange,
            "leg_b_symbol": signal.sell_leg.symbol,
            "leg_b_raw_symbol": signal.sell_leg.raw_symbol or "",
            "leg_b_url": signal.sell_leg.url or "",
            "leg_b_price": f"{signal.sell_leg.price:.10f}",
            "leg_b_price_source": signal.sell_leg.price_source,
            "leg_b_bid_price": f"{signal.sell_leg.bid_price:.10f}" if signal.sell_leg.bid_price is not None else "",
            "leg_b_ask_price": f"{signal.sell_leg.ask_price:.10f}" if signal.sell_leg.ask_price is not None else "",
            "leg_b_bid_size": f"{signal.sell_leg.bid_size:.8f}" if signal.sell_leg.bid_size is not None else "",
            "leg_b_ask_size": f"{signal.sell_leg.ask_size:.8f}" if signal.sell_leg.ask_size is not None else "",
            "leg_b_liquidity_usd": f"{signal.sell_leg.liquidity_usd:.4f}" if signal.sell_leg.liquidity_usd is not None else "",
            "leg_b_volume_24h_usd": f"{signal.sell_leg.volume_24h_usd:.4f}" if signal.sell_leg.volume_24h_usd is not None else "",
            "leg_b_funding_rate": f"{signal.sell_leg.funding_rate:.8f}" if signal.sell_leg.funding_rate is not None else "",
            "leg_b_timestamp_ms": str(signal.sell_leg.timestamp_ms or ""),
            "max_position_usd": f"{signal.max_position_usd:.4f}" if signal.max_position_usd is not None else "",
            "opened_at": signal.opened_at.isoformat(),
            "last_seen_at": signal.last_seen_at.isoformat(),
            "closed_at": closed_at,
            "close_reason": signal.close_reason or "",
            "avg_close_sec": f"{signal.avg_close_sec:.4f}" if signal.avg_close_sec is not None else "",
            "avg_close_count": str(signal.avg_close_count or ""),
            "telegram_chat_id": signal.telegram_chat_id or "",
            "message_id": str(signal.message_id or ""),
            "metadata_json": json.dumps(signal.metadata, sort_keys=True),
            "timestamp_ms": str(int(time.time() * 1000)),
        }

    async def publish(self, signal: CexSpreadSignal) -> None:
        payload = self._payload_for_signal(signal)
        try:
            client = await self._get_client()
            await client.xadd(
                self.stream_key,
                payload,
                maxlen=self.maxlen,
                approximate=True,
            )
        except Exception as exc:
            logger.warning("Redis publish failed for CEX spread signal %s: %s", signal.signal_id, exc)

    async def publish_open(self, signal: CexSpreadSignal) -> None:
        signal.event_type = "open"
        await self.publish(signal)

    async def publish_close(self, signal: CexSpreadSignal, reason: str) -> None:
        signal.event_type = "close"
        signal.close_reason = reason
        await self.publish(signal)
