"""Helpers for constructing trade URLs for supported futures exchanges."""

from __future__ import annotations


def _split_symbol(symbol: str) -> tuple[str, str]:
    normalized = (symbol or "").upper().strip()
    if "_" in normalized:
        base, quote = normalized.split("_", 1)
        return base, quote
    if normalized.endswith("USDT"):
        return normalized[:-4], "USDT"
    return normalized, "USDT"


def build_exchange_symbol_url(exchange: str, symbol: str, raw_symbol: str | None = None) -> str:
    exchange = (exchange or "").strip().lower()
    source = (raw_symbol or symbol or "").strip().upper()
    raw_underscore = source.replace("/", "_").replace("-", "_")
    base, quote = _split_symbol(symbol or raw_underscore)
    compact = raw_underscore.replace("_", "")
    raw_dashed = source.replace("/", "-").replace("_", "-")
    dashed = raw_dashed if "-" in raw_dashed else f"{base}-{quote}"
    lower_dashed = dashed.lower()
    okx_path = lower_dashed if lower_dashed.endswith("-swap") else f"{lower_dashed}-swap"

    patterns = {
        "bybit": f"https://www.bybit.com/trade/usdt/{compact}",
        "okx": f"https://www.okx.com/trade-swap/{okx_path}",
        "gate": f"https://www.gate.com/futures/USDT/{raw_underscore}",
        "bitget": f"https://www.bitget.com/futures/usdt/{compact}",
        "blofin": f"https://blofin.com/futures/{dashed}",
        "kucoin": f"https://www.kucoin.com/trade/futures/{compact if compact.endswith('M') else compact + 'M'}",
        "htx": f"https://www.htx.com/futures/linear_swap/exchange#contract_code={dashed}",
        "bingx": f"https://bingx.com/en-us/futures/forward/{compact}",
        "phemex": f"https://phemex.com/trade/{compact}",
        "bitmart": f"https://www.bitmart.com/futures/{compact}",
        "coinex": f"https://www.coinex.com/futures/{compact.lower()}",
        "lbank": f"https://www.lbank.com/futures/{base.lower()}{quote.lower()}/",
    }
    return patterns.get(exchange, "")
