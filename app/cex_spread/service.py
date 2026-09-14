"""Standalone CEX-CEX futures spread scanning service."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Dict, Mapping, Optional, Sequence

from app.cex.multi_exchange_registry import MultiExchangeRegistry
from app.cex.instrument_identity import canonical_identity_key
from app.cex.spread_analysis import (
    SpreadCandidate,
    TickerSnapshot,
    build_spread_candidates,
    collect_snapshots,
    snapshot_from_ticker,
)
from app.cex_spread.exchange_links import build_exchange_symbol_url
from app.cex_spread.formatter import format_close_message, format_open_message
from app.config.settings import Config
from app.core.close_duration_tracker import CloseDurationTracker
from app.core.models import CexSpreadLeg, CexSpreadSignal
from app.signals.cex_spread_stream import CexSpreadRedisPublisher
from app.telegram.telegram_sender import TelegramSender

logger = logging.getLogger(__name__)


def _fmt_market_price(value: float) -> str:
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:.4f}"
    if value >= 0.001:
        return f"${value:.6f}"
    return f"${value:.10f}"


@dataclass
class VenueRuntimeState:
    error_count: int = 0
    quarantined_until: float = 0.0
    quarantine_reason: str = ""
    last_success_ts: float = 0.0

    def is_quarantined(self) -> bool:
        return time.monotonic() < self.quarantined_until


class CexSpreadService:
    """All-vs-all CEX spread scanner with open/close lifecycle tracking."""

    def __init__(self):
        self.registry = MultiExchangeRegistry(
            exchange_names=Config.CEX_SPREAD_EXCHANGES,
            cache_enabled=Config.CEX_SPREAD_CACHE_ENABLED,
            cache_file=Config.CEX_SPREAD_CACHE_FILE or None,
        )
        self.telegram = TelegramSender(
            bot_token=Config.CEX_SPREAD_TELEGRAM_BOT_TOKEN,
            chat_id=Config.CEX_SPREAD_TELEGRAM_CHAT_ID,
        )
        self.redis = CexSpreadRedisPublisher()
        self.close_duration_tracker = CloseDurationTracker(Config.CEX_SPREAD_CLOSE_DURATION_STATS_FILE)
        self.active_signals: Dict[str, CexSpreadSignal] = {}
        self.venue_state: Dict[str, VenueRuntimeState] = {}
        self.last_registry_refresh_ts: float = 0.0
        self.last_sent_ts: Dict[str, float] = {}
        self._send_cooldown_sec = max(float(Config.RESEND_COOLDOWN_SEC), Config.CEX_SPREAD_POLL_INTERVAL_SEC)

    async def close(self) -> None:
        await self.telegram.__aexit__(None, None, None)
        await self.redis.close()

    def quarantined_exchanges(self) -> set[str]:
        return {
            exchange
            for exchange, state in self.venue_state.items()
            if state.is_quarantined()
        }

    def _signal_key(self, candidate: SpreadCandidate) -> str:
        return f"{candidate.pair_key}:{candidate.buy_exchange}:{candidate.sell_exchange}"

    def _signal_key_from_signal(self, signal: CexSpreadSignal) -> str:
        return (
            f"{signal.canonical_base}:{signal.quote_asset}:{signal.unit_scale:g}:1:"
            f"{signal.buy_exchange}:{signal.sell_exchange}"
        )

    @staticmethod
    def _stats_key(candidate_or_signal: SpreadCandidate | CexSpreadSignal) -> str:
        unit_scale = float(candidate_or_signal.unit_scale or 1.0)
        scale_prefix = ""
        if unit_scale != 1.0:
            scale_prefix = f"{int(unit_scale) if unit_scale.is_integer() else unit_scale}"
        return f"{scale_prefix}{candidate_or_signal.canonical_base}_{candidate_or_signal.quote_asset}"

    def _record_success(self, exchange: str) -> None:
        state = self.venue_state.setdefault(exchange, VenueRuntimeState())
        state.error_count = 0
        state.last_success_ts = time.monotonic()
        if not state.is_quarantined():
            state.quarantine_reason = ""

    def _record_failure(self, exchange: str, reason: str) -> None:
        state = self.venue_state.setdefault(exchange, VenueRuntimeState())
        state.error_count += 1
        if state.error_count >= Config.CEX_SPREAD_VENUE_ERROR_LIMIT:
            state.quarantined_until = time.monotonic() + Config.CEX_SPREAD_VENUE_COOLDOWN_SEC
            state.quarantine_reason = reason
            logger.warning(
                "Quarantined venue=%s for %ss reason=%s",
                exchange,
                Config.CEX_SPREAD_VENUE_COOLDOWN_SEC,
                reason,
            )

    def _note_cycle_health(self, all_tickers: Mapping[str, Mapping[str, object]]) -> None:
        exchange_names = getattr(self.registry, "exchange_names", list(all_tickers.keys()))
        for exchange in exchange_names:
            count = len(all_tickers.get(exchange) or {})
            if count <= 0:
                self._record_failure(exchange, "empty_tickers")
            else:
                self._record_success(exchange)

    def _apply_snapshot_health(self, venue_health: Mapping[str, object]) -> None:
        for exchange, health in venue_health.items():
            stale_ratio = getattr(health, "stale_ratio", 0.0)
            quote_count = getattr(health, "quote_count", 0)
            active_count = getattr(health, "active_count", 0)
            if quote_count > 0 and active_count == 0:
                self._record_failure(exchange, "no_active_quotes")
                continue
            if stale_ratio >= 0.5:
                self._record_failure(exchange, "stale_ratio")
                continue
            if active_count > 0:
                self._record_success(exchange)

    async def _maybe_refresh_registry(self) -> None:
        force_refresh = (
            not self.registry.tokens
            or (time.monotonic() - self.last_registry_refresh_ts) >= Config.CEX_SPREAD_REGISTRY_REFRESH_SEC
        )
        if not force_refresh:
            return
        await self.registry.build_registry(force_refresh=True)
        self.last_registry_refresh_ts = time.monotonic()

    def _candidate_stub_from_signal(self, signal: CexSpreadSignal) -> SpreadCandidate:
        return SpreadCandidate(
            canonical_base=signal.canonical_base,
            quote_asset=signal.quote_asset,
            unit_scale=signal.unit_scale,
            buy_exchange=signal.buy_exchange,
            sell_exchange=signal.sell_exchange,
            buy_symbol=signal.buy_symbol,
            sell_symbol=signal.sell_symbol,
            buy_raw_symbol=signal.buy_leg.raw_symbol or signal.buy_symbol,
            sell_raw_symbol=signal.sell_leg.raw_symbol or signal.sell_symbol,
            buy_price=signal.buy_leg.price,
            sell_price=signal.sell_leg.price,
            spread_pct=signal.spread_current_pct,
            spread_abs=max(signal.sell_leg.price - signal.buy_leg.price, 0.0),
            buy_price_source=signal.buy_leg.price_source,
            sell_price_source=signal.sell_leg.price_source,
        )

    async def _confirm_candidates(
        self,
        candidates: list[SpreadCandidate],
        *,
        min_spread_pct: float,
        require_bid_ask: bool = True,
        require_orderbook: bool = True,
    ) -> list[SpreadCandidate]:
        survivors = list(candidates)
        if not survivors:
            return []

        for round_index in range(Config.CEX_SPREAD_CONFIRM_ROUNDS):
            symbols_by_exchange: Dict[str, set[str]] = {}
            for candidate in survivors:
                symbols_by_exchange.setdefault(candidate.buy_exchange, set()).add(candidate.buy_symbol)
                symbols_by_exchange.setdefault(candidate.sell_exchange, set()).add(candidate.sell_symbol)

            refreshed = await self.registry.fetch_selected_tickers(
                symbols_by_exchange,
                require_orderbook=require_orderbook,
            )

            confirmed: list[SpreadCandidate] = []
            for candidate in survivors:
                buy_ticker = (refreshed.get(candidate.buy_exchange) or {}).get(candidate.buy_symbol)
                sell_ticker = (refreshed.get(candidate.sell_exchange) or {}).get(candidate.sell_symbol)
                if not buy_ticker or not sell_ticker:
                    continue

                buy_snap = snapshot_from_ticker(
                    exchange=candidate.buy_exchange,
                    symbol=candidate.buy_symbol,
                    ticker=buy_ticker,
                    canonical_base=candidate.canonical_base,
                    quote_asset=candidate.quote_asset,
                    unit_scale=candidate.unit_scale,
                    raw_symbol=candidate.buy_raw_symbol or buy_ticker.raw_symbol or candidate.buy_symbol,
                )
                sell_snap = snapshot_from_ticker(
                    exchange=candidate.sell_exchange,
                    symbol=candidate.sell_symbol,
                    ticker=sell_ticker,
                    canonical_base=candidate.canonical_base,
                    quote_asset=candidate.quote_asset,
                    unit_scale=candidate.unit_scale,
                    raw_symbol=candidate.sell_raw_symbol or sell_ticker.raw_symbol or candidate.sell_symbol,
                )

                rebuilt = build_spread_candidates(
                    [buy_snap, sell_snap],
                    min_spread_pct=min_spread_pct,
                    require_bid_ask=require_bid_ask,
                    min_liquidity_usd=Config.CEX_SPREAD_MIN_BOOK_LIQUIDITY_USD if require_bid_ask else None,
                )
                if rebuilt:
                    fresh = rebuilt[0]
                    confirmed.append(
                        replace(
                            fresh,
                            buy_raw_symbol=fresh.buy_raw_symbol or candidate.buy_raw_symbol,
                            sell_raw_symbol=fresh.sell_raw_symbol or candidate.sell_raw_symbol,
                            buy_volume_24h_usd=fresh.buy_volume_24h_usd or candidate.buy_volume_24h_usd,
                            sell_volume_24h_usd=fresh.sell_volume_24h_usd or candidate.sell_volume_24h_usd,
                            buy_funding_rate=fresh.buy_funding_rate if fresh.buy_funding_rate is not None else candidate.buy_funding_rate,
                            sell_funding_rate=fresh.sell_funding_rate if fresh.sell_funding_rate is not None else candidate.sell_funding_rate,
                            buy_change_24h_pct=fresh.buy_change_24h_pct if fresh.buy_change_24h_pct is not None else candidate.buy_change_24h_pct,
                            sell_change_24h_pct=fresh.sell_change_24h_pct if fresh.sell_change_24h_pct is not None else candidate.sell_change_24h_pct,
                            health_flags=tuple(sorted(set(candidate.health_flags) | set(fresh.health_flags))),
                        )
                    )

            survivors = confirmed
            if round_index + 1 < Config.CEX_SPREAD_CONFIRM_ROUNDS and survivors:
                await asyncio.sleep(Config.CEX_SPREAD_CONFIRM_DELAY_MS / 1000.0)

        return survivors

    async def _build_signal(self, candidate: SpreadCandidate) -> CexSpreadSignal:
        now = datetime.utcnow()
        avg_close_sec, avg_close_count = await self.close_duration_tracker.get_stats(
            self._stats_key(candidate)
        )
        return CexSpreadSignal(
            canonical_base=candidate.canonical_base,
            quote_asset=candidate.quote_asset,
            unit_scale=candidate.unit_scale,
            spread_open_pct=candidate.spread_pct,
            spread_current_pct=candidate.spread_pct,
            confidence=candidate.confidence,
            max_position_usd=candidate.max_position_usd,
            avg_close_sec=avg_close_sec,
            avg_close_count=avg_close_count or None,
            buy_leg=CexSpreadLeg(
                exchange=candidate.buy_exchange,
                symbol=candidate.buy_symbol,
                raw_symbol=candidate.buy_raw_symbol,
                url=build_exchange_symbol_url(
                    candidate.buy_exchange,
                    candidate.buy_symbol,
                    raw_symbol=candidate.buy_raw_symbol,
                ),
                price=candidate.buy_price,
                price_source=candidate.buy_price_source,
                volume_24h_usd=candidate.buy_volume_24h_usd,
                liquidity_usd=candidate.buy_liquidity_usd,
                funding_rate=candidate.buy_funding_rate,
                bid_price=candidate.buy_bid_price,
                ask_price=candidate.buy_ask_price,
                bid_size=candidate.buy_bid_size,
                ask_size=candidate.buy_ask_size,
            ),
            sell_leg=CexSpreadLeg(
                exchange=candidate.sell_exchange,
                symbol=candidate.sell_symbol,
                raw_symbol=candidate.sell_raw_symbol,
                url=build_exchange_symbol_url(
                    candidate.sell_exchange,
                    candidate.sell_symbol,
                    raw_symbol=candidate.sell_raw_symbol,
                ),
                price=candidate.sell_price,
                price_source=candidate.sell_price_source,
                volume_24h_usd=candidate.sell_volume_24h_usd,
                liquidity_usd=candidate.sell_liquidity_usd,
                funding_rate=candidate.sell_funding_rate,
                bid_price=candidate.sell_bid_price,
                ask_price=candidate.sell_ask_price,
                bid_size=candidate.sell_bid_size,
                ask_size=candidate.sell_ask_size,
            ),
            opened_at=now,
            last_seen_at=now,
            metadata={
                "close_ratio": f"{Config.CEX_SPREAD_CLOSE_RATIO:.2f}",
                "min_volume_usd": f"{Config.CEX_SPREAD_MIN_VOLUME_USD:.0f}",
                "min_book_liquidity_usd": f"{Config.CEX_SPREAD_MIN_BOOK_LIQUIDITY_USD:.0f}",
            },
        )

    async def _send_open(self, signal: CexSpreadSignal) -> None:
        if Config.CEX_SPREAD_DRY_RUN:
            logger.info("DRY RUN open signal %s", signal.signal_id)
            return
        message = format_open_message(signal)
        message_id = await self.telegram.send_message(
            message, chat_id=self.telegram.chat_id, disable_web_page_preview=True,
        )
        if message_id:
            signal.message_id = message_id
            signal.telegram_chat_id = self.telegram.chat_id
        if Config.CEX_SPREAD_REDIS_ENABLED:
            await self.redis.publish_open(signal)

    async def _send_anomaly(
        self,
        candidate: SpreadCandidate,
        snapshots: Sequence[TickerSnapshot],
    ) -> None:
        if Config.CEX_SPREAD_DRY_RUN:
            return
        market_ctx = self._build_market_context(candidate, snapshots)
        msg = (
            f"⚠️ <b>Anomaly {candidate.spread_pct:.1f}%</b> "
            f"{candidate.canonical_base}_{candidate.quote_asset}\n"
            f"BUY {candidate.buy_exchange.upper()} {candidate.buy_raw_symbol} "
            f"${candidate.buy_price:g}\n"
            f"SELL {candidate.sell_exchange.upper()} {candidate.sell_raw_symbol} "
            f"${candidate.sell_price:g}"
        )
        if market_ctx:
            msg += f"\n{market_ctx}"
        await self.telegram.send_message(
            msg, chat_id=self.telegram.chat_id, disable_web_page_preview=True,
        )

    async def _send_close(self, signal: CexSpreadSignal) -> None:
        if Config.CEX_SPREAD_DRY_RUN:
            logger.info("DRY RUN close signal %s", signal.signal_id)
            return
        message = format_close_message(signal)
        await self.telegram.send_message(
            message,
            reply_to_message_id=signal.message_id,
            chat_id=signal.telegram_chat_id or self.telegram.chat_id,
            disable_web_page_preview=True,
        )
        if Config.CEX_SPREAD_REDIS_ENABLED:
            await self.redis.publish_close(signal, signal.close_reason or "close")

    async def _close_signal(
        self,
        key: str,
        signal: CexSpreadSignal,
        reason: str,
        spread_pct: Optional[float] = None,
    ) -> None:
        signal.event_type = "close"
        signal.close_reason = reason
        signal.closed_at = datetime.utcnow()
        signal.last_seen_at = signal.closed_at
        if spread_pct is not None:
            signal.spread_current_pct = spread_pct
        duration_sec = max((signal.closed_at - signal.opened_at).total_seconds(), 0.0)
        avg_close_sec, avg_close_count = await self.close_duration_tracker.add(
            self._stats_key(signal),
            duration_sec,
        )
        signal.avg_close_sec = avg_close_sec
        signal.avg_close_count = avg_close_count or None
        await self._send_close(signal)
        self.active_signals.pop(key, None)
        self.last_sent_ts[key] = time.monotonic()

    async def _reconcile_active_signals(
        self,
        candidates_by_key: Mapping[str, SpreadCandidate],
    ) -> None:
        now = datetime.utcnow()
        for key, signal in list(self.active_signals.items()):
            candidate = candidates_by_key.get(key)
            if candidate is not None:
                close_threshold = signal.spread_open_pct * (1.0 - Config.CEX_SPREAD_CLOSE_RATIO)
                if candidate.spread_pct <= close_threshold:
                    await self._close_signal(key, signal, "spread_close", candidate.spread_pct)
                    continue
            if candidate is None:
                # No fresh data — keep signal alive, just update last_seen
                signal.last_seen_at = now
                continue

            signal.spread_current_pct = candidate.spread_pct
            signal.confidence = candidate.confidence
            signal.max_position_usd = candidate.max_position_usd
            signal.last_seen_at = now
            signal.buy_leg.price = candidate.buy_price
            signal.sell_leg.price = candidate.sell_price
            signal.buy_leg.raw_symbol = candidate.buy_raw_symbol
            signal.sell_leg.raw_symbol = candidate.sell_raw_symbol
            signal.buy_leg.volume_24h_usd = candidate.buy_volume_24h_usd
            signal.sell_leg.volume_24h_usd = candidate.sell_volume_24h_usd
            signal.buy_leg.liquidity_usd = candidate.buy_liquidity_usd
            signal.sell_leg.liquidity_usd = candidate.sell_liquidity_usd
            signal.buy_leg.funding_rate = candidate.buy_funding_rate
            signal.sell_leg.funding_rate = candidate.sell_funding_rate
            signal.buy_leg.bid_price = candidate.buy_bid_price
            signal.buy_leg.ask_price = candidate.buy_ask_price
            signal.buy_leg.bid_size = candidate.buy_bid_size
            signal.buy_leg.ask_size = candidate.buy_ask_size
            signal.sell_leg.bid_price = candidate.sell_bid_price
            signal.sell_leg.ask_price = candidate.sell_ask_price
            signal.sell_leg.bid_size = candidate.sell_bid_size
            signal.sell_leg.ask_size = candidate.sell_ask_size
            signal.buy_leg.url = build_exchange_symbol_url(
                candidate.buy_exchange,
                candidate.buy_symbol,
                raw_symbol=candidate.buy_raw_symbol,
            )
            signal.sell_leg.url = build_exchange_symbol_url(
                candidate.sell_exchange,
                candidate.sell_symbol,
                raw_symbol=candidate.sell_raw_symbol,
            )

    def _build_market_context(
        self,
        candidate: SpreadCandidate,
        snapshots: Sequence[TickerSnapshot],
    ) -> str:
        """Build a sorted price list from all exchanges for context."""
        identity_key = canonical_identity_key(
            candidate.canonical_base, candidate.quote_asset, candidate.unit_scale,
        )
        prices: list[tuple[str, float]] = []
        for snap in snapshots:
            if snap.identity_key == identity_key:
                prices.append((snap.exchange, snap.price))
        prices.sort(key=lambda x: x[1])
        if not prices:
            return ""
        parts = [f"{ex.upper()} {_fmt_market_price(p)}" for ex, p in prices]
        return " | ".join(parts)

    async def _open_new_signals(
        self,
        candidates_by_key: Mapping[str, SpreadCandidate],
        snapshots: Sequence[TickerSnapshot],
    ) -> None:
        for key, candidate in candidates_by_key.items():
            if key in self.active_signals:
                continue
            if (time.monotonic() - self.last_sent_ts.get(key, 0.0)) < self._send_cooldown_sec:
                continue
            if candidate.spread_pct > 200.0:
                self.last_sent_ts[key] = time.monotonic()
                continue
            signal = await self._build_signal(candidate)
            market_ctx = self._build_market_context(candidate, snapshots)
            if market_ctx:
                signal.metadata["market_context"] = market_ctx
            await self._send_open(signal)
            self.active_signals[key] = signal
            self.last_sent_ts[key] = time.monotonic()

    async def run_cycle(self) -> None:
        await self._maybe_refresh_registry()
        all_tickers = await self.registry.fetch_all_tickers(force_refresh=True)
        self._note_cycle_health(all_tickers)

        snapshots, venue_health = collect_snapshots(
            self.registry.tokens,
            all_tickers,
            max_data_age_sec=Config.CEX_SPREAD_MAX_DATA_AGE_SEC,
            min_volume_usd=Config.CEX_SPREAD_MIN_VOLUME_USD,
            quarantined_exchanges=self.quarantined_exchanges(),
        )
        self._apply_snapshot_health(venue_health)

        preliminary_candidates = build_spread_candidates(
            snapshots,
            min_spread_pct=Config.CEX_SPREAD_MIN_SPREAD_OPEN_PCT,
        )
        confirmed_open = await self._confirm_candidates(
            preliminary_candidates,
            min_spread_pct=Config.CEX_SPREAD_MIN_SPREAD_OPEN_PCT,
        )
        active_watch = [self._candidate_stub_from_signal(signal) for signal in self.active_signals.values()]
        confirmed_active = await self._confirm_candidates(
            active_watch,
            min_spread_pct=0.0,
            require_bid_ask=False,
            require_orderbook=False,
        )

        confirmed_open_by_key = {self._signal_key(candidate): candidate for candidate in confirmed_open}
        candidates_by_key = {self._signal_key(candidate): candidate for candidate in confirmed_active}
        candidates_by_key.update(confirmed_open_by_key)

        await self._reconcile_active_signals(candidates_by_key)
        await self._open_new_signals(confirmed_open_by_key, snapshots)

        logger.info(
            "CEX spread cycle complete: tokens=%d snapshots=%d prelim=%d confirmed_open=%d "
            "confirmed_active=%d active=%d quarantined=%d",
            len(self.registry.tokens),
            len(snapshots),
            len(preliminary_candidates),
            len(confirmed_open),
            len(confirmed_active),
            len(self.active_signals),
            len(self.quarantined_exchanges()),
        )

    async def run(self) -> None:
        Config.validate_cex_spread_config()
        await self.telegram.__aenter__()
        try:
            while True:
                cycle_started = time.monotonic()
                try:
                    await self.run_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("CEX spread cycle failed")
                elapsed = time.monotonic() - cycle_started
                await asyncio.sleep(max(0.0, Config.CEX_SPREAD_POLL_INTERVAL_SEC - elapsed))
        finally:
            await self.close()
