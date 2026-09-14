"""Shared spread-analysis helpers for CEX futures scanning."""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.cex.instrument_identity import (
    InstrumentIdentity,
    build_instrument_identity,
    canonical_identity_key,
)


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TickerSnapshot:
    """Normalized quote snapshot used by scanners and services."""

    exchange: str
    symbol: str
    raw_symbol: str
    canonical_base: str
    quote_asset: str
    unit_scale: float
    contract_multiplier: float
    price: float
    timestamp_ms: int
    price_source: str = "last"
    change_24h_pct: Optional[float] = None
    volume_24h_usd: Optional[float] = None
    funding_rate: Optional[float] = None
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    mark_price: Optional[float] = None
    health_flags: Tuple[str, ...] = ()

    @property
    def identity_key(self) -> str:
        return canonical_identity_key(
            self.canonical_base,
            self.quote_asset,
            self.unit_scale,
        )

    @property
    def canonical_symbol(self) -> str:
        return f"{self.canonical_base}_{self.quote_asset}"

    def price_for_side(self, side: str) -> float:
        side = (side or "").lower()
        if side == "buy":
            if self.ask_price is not None and self.ask_price > 0:
                return self.ask_price
            if self.mark_price is not None and self.mark_price > 0:
                return self.mark_price
            return self.price
        if side == "sell":
            if self.bid_price is not None and self.bid_price > 0:
                return self.bid_price
            if self.mark_price is not None and self.mark_price > 0:
                return self.mark_price
            return self.price
        return self.price

    def price_source_for_side(self, side: str) -> str:
        side = (side or "").lower()
        if side == "buy":
            if self.ask_price is not None and self.ask_price > 0:
                return "ask"
            if self.mark_price is not None and self.mark_price > 0:
                return "mark"
            return "last"
        if side == "sell":
            if self.bid_price is not None and self.bid_price > 0:
                return "bid"
            if self.mark_price is not None and self.mark_price > 0:
                return "mark"
            return "last"
        return "last"

    def buy_price(self) -> float:
        if self.ask_price is not None and self.ask_price > 0:
            return self.ask_price
        if self.mark_price is not None and self.mark_price > 0:
            return self.mark_price
        return self.price

    def sell_price(self) -> float:
        if self.bid_price is not None and self.bid_price > 0:
            return self.bid_price
        if self.mark_price is not None and self.mark_price > 0:
            return self.mark_price
        return self.price

    def size_for_side(self, side: str) -> Optional[float]:
        side = (side or "").lower()
        if side == "buy":
            return self.ask_size
        if side == "sell":
            return self.bid_size
        return None

    def liquidity_usd_for_side(self, side: str) -> Optional[float]:
        size = self.size_for_side(side)
        if size is None or size <= 0:
            return None
        price = self.price_for_side(side)
        if price <= 0:
            return None
        multiplier = self.contract_multiplier or 1.0
        return price * size * multiplier

    def has_executable_quotes(self) -> bool:
        return (
            self.bid_price is not None
            and self.bid_price > 0
            and self.ask_price is not None
            and self.ask_price > 0
        )

    def has_executable_sizes(self) -> bool:
        return (
            self.bid_size is not None
            and self.bid_size > 0
            and self.ask_size is not None
            and self.ask_size > 0
        )


@dataclass(frozen=True)
class SpreadCandidate:
    """Directional CEX-CEX spread opportunity."""

    canonical_base: str
    quote_asset: str
    unit_scale: float
    buy_exchange: str
    sell_exchange: str
    buy_symbol: str
    sell_symbol: str
    buy_raw_symbol: str
    sell_raw_symbol: str
    buy_price: float
    sell_price: float
    spread_pct: float
    spread_abs: float
    buy_price_source: str
    sell_price_source: str
    buy_bid_price: Optional[float] = None
    buy_ask_price: Optional[float] = None
    buy_bid_size: Optional[float] = None
    buy_ask_size: Optional[float] = None
    sell_bid_price: Optional[float] = None
    sell_ask_price: Optional[float] = None
    sell_bid_size: Optional[float] = None
    sell_ask_size: Optional[float] = None
    buy_volume_24h_usd: Optional[float] = None
    sell_volume_24h_usd: Optional[float] = None
    buy_liquidity_usd: Optional[float] = None
    sell_liquidity_usd: Optional[float] = None
    max_position_usd: Optional[float] = None
    buy_funding_rate: Optional[float] = None
    sell_funding_rate: Optional[float] = None
    buy_change_24h_pct: Optional[float] = None
    sell_change_24h_pct: Optional[float] = None
    health_flags: Tuple[str, ...] = ()
    confidence: float = 1.0

    @property
    def pair_key(self) -> str:
        return canonical_identity_key(self.canonical_base, self.quote_asset, self.unit_scale)

    @property
    def direction_key(self) -> str:
        return f"{self.canonical_base}:{self.quote_asset}:{self.buy_exchange}->{self.sell_exchange}:{self.unit_scale:g}"


@dataclass(frozen=True)
class VenueHealth:
    """Aggregated venue quality summary."""

    exchange: str
    quote_count: int
    active_count: int
    stale_count: int
    error_count: int = 0
    outlier_count: int = 0
    median_deviation_pct: Optional[float] = None
    max_deviation_pct: Optional[float] = None
    quarantined: bool = False
    quarantine_reason: str = ""

    @property
    def stale_ratio(self) -> float:
        if self.quote_count <= 0:
            return 0.0
        return self.stale_count / self.quote_count


def snapshot_from_ticker(
    *,
    exchange: str,
    symbol: str,
    ticker: Any,
    canonical_base: Optional[str] = None,
    quote_asset: Optional[str] = None,
    unit_scale: Optional[float] = None,
    contract_multiplier: Optional[float] = None,
    raw_symbol: Optional[str] = None,
) -> TickerSnapshot:
    """Convert a raw ticker object into a normalized snapshot."""
    raw_symbol = raw_symbol or _get_attr(ticker, "raw_symbol", symbol) or symbol
    identity: InstrumentIdentity = build_instrument_identity(
        symbol=symbol,
        base_asset=canonical_base or _get_attr(ticker, "base_asset", canonical_base or ""),
        exchange=exchange,
        raw_symbol=raw_symbol,
        canonical_base=canonical_base,
        quote_asset=quote_asset,
        unit_scale=unit_scale,
        contract_multiplier=contract_multiplier,
        is_perpetual=True,
    )

    price = _float_or_none(_get_attr(ticker, "price")) or 0.0
    bid_price = _float_or_none(_get_attr(ticker, "bid_price"))
    ask_price = _float_or_none(_get_attr(ticker, "ask_price"))
    bid_size = _float_or_none(_get_attr(ticker, "bid_size"))
    ask_size = _float_or_none(_get_attr(ticker, "ask_size"))
    mark_price = _float_or_none(_get_attr(ticker, "mark_price"))

    return TickerSnapshot(
        exchange=exchange,
        symbol=symbol,
        raw_symbol=identity.raw_symbol,
        canonical_base=identity.canonical_base,
        quote_asset=identity.quote_asset,
        unit_scale=identity.unit_scale,
        contract_multiplier=identity.contract_multiplier,
        price=price,
        timestamp_ms=int(_get_attr(ticker, "timestamp_ms", 0) or 0),
        price_source="ask" if ask_price and ask_price > 0 else "bid" if bid_price and bid_price > 0 else "mark" if mark_price and mark_price > 0 else "last",
        change_24h_pct=_float_or_none(_get_attr(ticker, "change_24h_pct")),
        volume_24h_usd=_float_or_none(_get_attr(ticker, "volume_24h_usd")),
        funding_rate=_float_or_none(_get_attr(ticker, "funding_rate")),
        bid_price=bid_price,
        ask_price=ask_price,
        bid_size=bid_size,
        ask_size=ask_size,
        mark_price=mark_price,
        health_flags=tuple(_get_attr(ticker, "health_flags", ()) or ()),
    )


def collect_snapshots(
    tokens: Mapping[str, Any] | Sequence[Any],
    all_tickers: Mapping[str, Mapping[str, Any]],
    *,
    max_data_age_sec: Optional[float] = None,
    min_volume_usd: Optional[float] = None,
    quarantined_exchanges: Optional[Iterable[str]] = None,
    max_median_deviation_pct: Optional[float] = None,
) -> Tuple[List[TickerSnapshot], Dict[str, VenueHealth]]:
    """Build normalized ticker snapshots and venue health summaries."""
    snapshots: List[TickerSnapshot] = []
    venue_errors: Dict[str, int] = {}
    deviations_by_exchange: Dict[str, List[float]] = {}
    stale_by_exchange: Dict[str, int] = {}
    active_by_exchange: Dict[str, int] = {}
    total_by_exchange: Dict[str, int] = {}
    filtered_by_exchange: Dict[str, int] = {}
    quarantined = {exchange.lower() for exchange in (quarantined_exchanges or [])}
    token_values = tokens.values() if isinstance(tokens, Mapping) else tokens

    for token in token_values:
        exchanges = getattr(token, "exchanges", {})
        for exchange, symbol in exchanges.items():
            if exchange.lower() in quarantined:
                continue
            total_by_exchange[exchange] = total_by_exchange.get(exchange, 0) + 1
            ticker = (all_tickers.get(exchange) or {}).get(symbol)
            if ticker is None:
                venue_errors[exchange] = venue_errors.get(exchange, 0) + 1
                continue

            snap = snapshot_from_ticker(
                exchange=exchange,
                symbol=symbol,
                ticker=ticker,
                canonical_base=getattr(token, "canonical_base", None),
                quote_asset=getattr(token, "quote_asset", None),
                unit_scale=getattr(token, "unit_scale", None),
                contract_multiplier=getattr(token, "contract_multiplier", None),
                raw_symbol=_get_attr(ticker, "raw_symbol", symbol),
            )

            if snap.price <= 0:
                venue_errors[exchange] = venue_errors.get(exchange, 0) + 1
                continue

            active_by_exchange[exchange] = active_by_exchange.get(exchange, 0) + 1

            if min_volume_usd is not None and (
                snap.volume_24h_usd is None or snap.volume_24h_usd < min_volume_usd
            ):
                continue

            if max_data_age_sec is not None:
                if snap.timestamp_ms <= 0:
                    stale_by_exchange[exchange] = stale_by_exchange.get(exchange, 0) + 1
                    continue
                age_sec = max(0.0, (time_now_ms() - snap.timestamp_ms) / 1000.0)
                if age_sec > max_data_age_sec:
                    stale_by_exchange[exchange] = stale_by_exchange.get(exchange, 0) + 1
                    continue

            snapshots.append(snap)

    # Group once, compute deviations, and optionally filter outliers in a single pass
    grouped: Dict[str, List[TickerSnapshot]] = {}
    for snap in snapshots:
        grouped.setdefault(snap.identity_key, []).append(snap)

    for group in grouped.values():
        prices = [snap.price for snap in group if snap.price > 0]
        if len(prices) < 2:
            continue
        group_median = median(prices)
        if group_median <= 0:
            continue
        for snap in group:
            deviation = abs(snap.price - group_median) / group_median * 100.0
            deviations_by_exchange.setdefault(snap.exchange, []).append(deviation)

    if max_median_deviation_pct is not None:
        filtered: List[TickerSnapshot] = []
        for group in grouped.values():
            prices = [snap.price for snap in group if snap.price > 0]
            if len(prices) < 2:
                filtered.extend(group)
                continue
            group_median = median(prices)
            for snap in group:
                deviation = abs(snap.price - group_median) / group_median * 100.0 if group_median > 0 else 0.0
                if deviation > max_median_deviation_pct:
                    filtered_by_exchange[snap.exchange] = filtered_by_exchange.get(snap.exchange, 0) + 1
                    continue
                filtered.append(snap)
        snapshots = filtered

    # Count active snapshots per exchange in one pass
    final_active_by_exchange: Dict[str, int] = {}
    for snap in snapshots:
        final_active_by_exchange[snap.exchange] = final_active_by_exchange.get(snap.exchange, 0) + 1

    venue_health: Dict[str, VenueHealth] = {}
    exchanges = (
        set(total_by_exchange)
        | set(active_by_exchange)
        | set(venue_errors)
        | set(stale_by_exchange)
        | set(filtered_by_exchange)
    )
    for exchange in exchanges:
        deviations = deviations_by_exchange.get(exchange, [])
        median_deviation = median(deviations) if deviations else None
        max_deviation = max(deviations) if deviations else None
        venue_health[exchange] = VenueHealth(
            exchange=exchange,
            quote_count=total_by_exchange.get(exchange, 0),
            active_count=final_active_by_exchange.get(exchange, 0),
            stale_count=stale_by_exchange.get(exchange, 0),
            error_count=venue_errors.get(exchange, 0),
            outlier_count=filtered_by_exchange.get(exchange, 0),
            median_deviation_pct=median_deviation,
            max_deviation_pct=max_deviation,
            quarantined=exchange.lower() in quarantined,
            quarantine_reason="manual" if exchange.lower() in quarantined else "",
        )

    return snapshots, venue_health


def build_spread_candidates(
    snapshots: Sequence[TickerSnapshot],
    *,
    min_spread_pct: float = 0.0,
    require_bid_ask: bool = False,
    min_liquidity_usd: Optional[float] = None,
) -> List[SpreadCandidate]:
    """Generate all directional pairwise spreads for each instrument group."""
    grouped: Dict[str, List[TickerSnapshot]] = {}
    for snap in snapshots:
        grouped.setdefault(snap.identity_key, []).append(snap)

    candidates: List[SpreadCandidate] = []
    for group in grouped.values():
        if len(group) < 2:
            continue

        for buy_snap, sell_snap in combinations(group, 2):
            low, high = sorted(
                (buy_snap, sell_snap),
                key=lambda item: item.price_for_side("buy"),
            )

            buy_price = low.price_for_side("buy")
            sell_price = high.price_for_side("sell")
            if buy_price <= 0 or sell_price <= 0:
                continue

            if require_bid_ask and not (low.has_executable_quotes() and high.has_executable_quotes()):
                continue

            buy_liquidity_usd = low.liquidity_usd_for_side("buy")
            sell_liquidity_usd = high.liquidity_usd_for_side("sell")
            if min_liquidity_usd is not None:
                if buy_liquidity_usd is None or buy_liquidity_usd < min_liquidity_usd:
                    continue
                if sell_liquidity_usd is None or sell_liquidity_usd < min_liquidity_usd:
                    continue

            spread_pct = (sell_price - buy_price) / buy_price * 100.0
            if spread_pct < min_spread_pct:
                continue

            spread_abs = sell_price - buy_price
            buy_source = low.price_source_for_side("buy")
            sell_source = high.price_source_for_side("sell")
            flags = tuple(
                sorted(
                    set(low.health_flags)
                    | set(high.health_flags)
                    | ({f"buy:{buy_source}"} if buy_source else set())
                    | ({f"sell:{sell_source}"} if sell_source else set())
                )
            )
            confidence = 1.0
            if buy_source == "mark" or sell_source == "mark":
                confidence *= 0.95
            if buy_source == "last" or sell_source == "last":
                confidence *= 0.9

            candidates.append(
                SpreadCandidate(
                    canonical_base=low.canonical_base,
                    quote_asset=low.quote_asset,
                    unit_scale=low.unit_scale,
                    buy_exchange=low.exchange,
                    sell_exchange=high.exchange,
                    buy_symbol=low.symbol,
                    sell_symbol=high.symbol,
                    buy_raw_symbol=low.raw_symbol,
                    sell_raw_symbol=high.raw_symbol,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    spread_pct=spread_pct,
                    spread_abs=spread_abs,
                    buy_price_source=buy_source,
                    sell_price_source=sell_source,
                    buy_bid_price=low.bid_price,
                    buy_ask_price=low.ask_price,
                    buy_bid_size=low.bid_size,
                    buy_ask_size=low.ask_size,
                    sell_bid_price=high.bid_price,
                    sell_ask_price=high.ask_price,
                    sell_bid_size=high.bid_size,
                    sell_ask_size=high.ask_size,
                    buy_volume_24h_usd=low.volume_24h_usd,
                    sell_volume_24h_usd=high.volume_24h_usd,
                    buy_liquidity_usd=buy_liquidity_usd,
                    sell_liquidity_usd=sell_liquidity_usd,
                    max_position_usd=min(
                        value
                        for value in (
                            buy_liquidity_usd,
                            sell_liquidity_usd,
                        )
                        if value is not None
                    ) if buy_liquidity_usd is not None and sell_liquidity_usd is not None else None,
                    buy_funding_rate=low.funding_rate,
                    sell_funding_rate=high.funding_rate,
                    buy_change_24h_pct=low.change_24h_pct,
                    sell_change_24h_pct=high.change_24h_pct,
                    health_flags=flags,
                    confidence=confidence,
                )
            )

    candidates.sort(key=lambda item: (-item.spread_pct, item.canonical_base, item.buy_exchange, item.sell_exchange))
    return candidates


def time_now_ms() -> int:
    import time

    return int(time.time() * 1000)
