"""Telegram message formatting for CEX-CEX spread signals."""

from __future__ import annotations

import html

from app.core.models import CexSpreadSignal


REASON_LABELS = {
    "spread_close": "spread dropped below close threshold",
    "spread_open": "spread failed the open threshold during confirmation",
    "quote_missing": "one of the exchanges stopped returning the pair quote",
    "book_quote_missing": "top-of-book quote is missing on one side",
    "stale_data": "quote became stale",
    "volume_filter": "24h volume filter failed",
    "liquidity_filter": "book liquidity filter failed",
    "liquidity_missing": "book size is missing on one side",
    "filter_failed": "pair failed current filters",
    "no_longer_qualified": "pair no longer passes book, volume or spread filters",
    "venue_quarantined": "one of the exchanges became unhealthy and was quarantined",
    "max_lifetime": "signal exceeded max lifetime",
}


def _fmt_price(value: float) -> str:
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:,.4f}"
    if value >= 0.001:
        return f"${value:.6f}"
    return f"${value:.10f}"


def _fmt_volume(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    total = max(int(round(seconds)), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _fmt_confidence(signal: CexSpreadSignal) -> str:
    if signal.confidence >= 0.999:
        return "1.00 (best bid/ask confirmed on both legs)"
    if signal.confidence >= 0.95:
        return "0.95 (one leg used mark-price fallback)"
    if signal.confidence >= 0.90:
        return "0.90 (at least one leg used last-price fallback)"
    return f"{signal.confidence:.2f}"


def _link(label: str, url: str | None) -> str:
    if not url:
        return html.escape(label)
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def _leg_link_label(signal: CexSpreadSignal, side: str) -> str:
    leg = signal.buy_leg if side == "buy" else signal.sell_leg
    symbol = leg.raw_symbol or leg.symbol
    return f"{leg.exchange.upper()} {symbol}"


def format_open_message(signal: CexSpreadSignal) -> str:
    base = html.escape(signal.canonical_base)
    buy_exchange = html.escape(signal.buy_leg.exchange.upper())
    sell_exchange = html.escape(signal.sell_leg.exchange.upper())
    buy_symbol = html.escape(signal.buy_leg.symbol)
    sell_symbol = html.escape(signal.sell_leg.symbol)
    close_ratio = float(signal.metadata.get("close_ratio", "0.80"))
    close_at_pct = signal.spread_open_pct * (1.0 - close_ratio)
    close_hint = f"{close_at_pct:.2f}% ({close_ratio:.0%} captured)"
    min_volume = html.escape(signal.metadata.get("min_volume_usd", "n/a"))
    min_liquidity = html.escape(
        signal.metadata.get("min_book_liquidity_usd", signal.metadata.get("min_liquidity_usd", "n/a"))
    )
    avg_close_line = "n/a"
    if signal.avg_close_sec is not None:
        avg_close_line = _fmt_duration(signal.avg_close_sec)
        if signal.avg_close_count:
            avg_close_line += f" (n={signal.avg_close_count})"
    market_ctx = signal.metadata.get("market_context", "")
    lines = [
        f"<b>CEX Spread OPEN</b>",
        f"<b>Pair:</b> {base}_{html.escape(signal.quote_asset)}",
        f"<b>Direction:</b> LONG {buy_exchange} / SHORT {sell_exchange}",
        f"<b>Spread:</b> {signal.spread_current_pct:.2f}%",
        f"<b>Confidence:</b> {_fmt_confidence(signal)}",
        f"<b>Avg Close:</b> {avg_close_line}",
        f"<b>Max Position:</b> {_fmt_volume(signal.max_position_usd)}",
        "",
        f"<b>Buy Book:</b> {buy_exchange} {buy_symbol} ask {_fmt_price(signal.buy_leg.price)}",
        f"<b>Sell Book:</b> {sell_exchange} {sell_symbol} bid {_fmt_price(signal.sell_leg.price)}",
        f"<b>Book Liquidity:</b> {_fmt_volume(signal.buy_leg.liquidity_usd)} / {_fmt_volume(signal.sell_leg.liquidity_usd)}",
        f"<b>24h Volumes:</b> {_fmt_volume(signal.buy_leg.volume_24h_usd)} / {_fmt_volume(signal.sell_leg.volume_24h_usd)}",
        f"<b>Funding:</b> {signal.buy_leg.funding_rate if signal.buy_leg.funding_rate is not None else 'n/a'} / {signal.sell_leg.funding_rate if signal.sell_leg.funding_rate is not None else 'n/a'}",
        f"<b>Filters:</b> vol &gt; ${min_volume}, book &gt; ${min_liquidity}",
        f"<b>Close Below:</b> {close_hint}",
    ]
    if market_ctx:
        lines.append(f"<b>Market:</b> {html.escape(market_ctx)}")
    lines.extend([
        f"<b>Links:</b> {_link(_leg_link_label(signal, 'buy'), signal.buy_leg.url)} / {_link(_leg_link_label(signal, 'sell'), signal.sell_leg.url)}",
        "",
        f"<i>Opened: {signal.opened_at.strftime('%Y-%m-%d %H:%M:%S UTC')}</i>",
    ])
    return "\n".join(lines)


def format_close_message(signal: CexSpreadSignal) -> str:
    reason_code = signal.close_reason or "unknown"
    reason = html.escape(REASON_LABELS.get(reason_code, reason_code))
    duration_line = ""
    if signal.closed_at is not None:
        duration_line = f"<b>Open Time:</b> {_fmt_duration((signal.closed_at - signal.opened_at).total_seconds())}"
    avg_close_line = ""
    if signal.avg_close_sec is not None:
        avg_close_line = f"<b>Avg Close:</b> {_fmt_duration(signal.avg_close_sec)}"
        if signal.avg_close_count:
            avg_close_line += f" (n={signal.avg_close_count})"
    return "\n".join(
        [
            "<b>CEX Spread CLOSE</b>",
            f"<b>Pair:</b> {html.escape(signal.canonical_base)}_{html.escape(signal.quote_asset)}",
            f"<b>Direction:</b> LONG {html.escape(signal.buy_leg.exchange.upper())} / SHORT {html.escape(signal.sell_leg.exchange.upper())}",
            f"<b>Open Spread:</b> {signal.spread_open_pct:.2f}%",
            f"<b>Close Spread:</b> {signal.spread_current_pct:.2f}%",
            f"<b>Reason:</b> {reason}",
            duration_line,
            avg_close_line,
            f"<b>Max Position:</b> {_fmt_volume(signal.max_position_usd)}",
            f"<b>Links:</b> {_link(_leg_link_label(signal, 'buy'), signal.buy_leg.url)} / {_link(_leg_link_label(signal, 'sell'), signal.sell_leg.url)}",
            "",
            f"<i>Closed: {(signal.closed_at or signal.last_seen_at).strftime('%Y-%m-%d %H:%M:%S UTC')}</i>",
        ]
    )
