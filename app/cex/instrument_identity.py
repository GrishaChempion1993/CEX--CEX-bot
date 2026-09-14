"""Canonical instrument identity helpers for futures symbols."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

COMMON_QUOTE_ASSETS = (
    "USDT",
    "USDC",
    "USD",
    "FDUSD",
    "BUSD",
    "TUSD",
    "DAI",
    "EUR",
    "TRY",
    "BRL",
    "JPY",
    "GBP",
    "AUD",
    "BTC",
    "ETH",
)

# Keep this conservative. We only normalize obviously renamed assets here.
CANONICAL_BASE_ALIASES = {
    "BCHABC": "BCHA",
    "BCHSV": "BSV",
    "XBT": "BTC",
}

MULTIPLIER_BASE_RE = re.compile(r"^(?P<mult>\d{3,})(?P<rest>[A-Z][A-Z0-9]*)$")


@dataclass(frozen=True)
class InstrumentIdentity:
    """Canonical identity used for grouping and spread matching."""

    symbol: str
    base_asset: str
    exchange: str
    raw_symbol: str
    canonical_base: str
    quote_asset: str = "USDT"
    unit_scale: float = 1.0
    contract_multiplier: float = 1.0
    is_perpetual: bool = True

    @property
    def canonical_symbol(self) -> str:
        return f"{self.canonical_base}_{self.quote_asset}"

    @property
    def identity_key(self) -> str:
        return canonical_identity_key(
            self.canonical_base,
            self.quote_asset,
            self.unit_scale,
            self.is_perpetual,
        )


def _normalize_symbol(raw: str) -> str:
    symbol = (raw or "").upper().strip()
    symbol = symbol.replace("-", "_").replace("/", "_").replace(".", "_")
    symbol = re.sub(r"_+", "_", symbol).strip("_")
    return symbol


def _split_quote(symbol: str, quote_asset: Optional[str] = None) -> tuple[str, str]:
    if not symbol:
        return "", (quote_asset or "USDT").upper()

    normalized_quote = (quote_asset or "").upper().strip()
    normalized = _normalize_symbol(symbol)

    if "_" in normalized:
        base, suffix = normalized.rsplit("_", 1)
        if suffix in COMMON_QUOTE_ASSETS or suffix == normalized_quote:
            return base, suffix

    for candidate in sorted(COMMON_QUOTE_ASSETS, key=len, reverse=True):
        if normalized.endswith(candidate) and len(normalized) > len(candidate):
            return normalized[: -len(candidate)], candidate

    if normalized_quote:
        return normalized, normalized_quote

    return normalized, "USDT"


def _strip_multiplier(base: str) -> tuple[str, float]:
    if not base:
        return "", 1.0

    match = MULTIPLIER_BASE_RE.match(base)
    if match:
        multiplier = float(match.group("mult"))
        rest = match.group("rest")
        if rest not in COMMON_QUOTE_ASSETS:
            return rest, multiplier
    return base, 1.0


def canonical_identity_key(
    canonical_base: str,
    quote_asset: str = "USDT",
    unit_scale: float = 1.0,
    is_perpetual: bool = True,
) -> str:
    """Stable key used to group comparable futures contracts."""
    return f"{canonical_base.upper()}:{quote_asset.upper()}:{unit_scale:g}:{int(is_perpetual)}"


def build_instrument_identity(
    *,
    symbol: str,
    base_asset: str,
    exchange: str,
    raw_symbol: Optional[str] = None,
    canonical_base: Optional[str] = None,
    quote_asset: Optional[str] = None,
    unit_scale: Optional[float] = None,
    contract_multiplier: Optional[float] = None,
    is_perpetual: bool = True,
) -> InstrumentIdentity:
    """Infer canonical grouping metadata from exchange-provided symbol data."""
    raw_symbol = _normalize_symbol(raw_symbol or symbol)
    symbol = _normalize_symbol(symbol or raw_symbol)
    base_asset = _normalize_symbol(base_asset or "")

    inferred_base, inferred_quote = _split_quote(raw_symbol or symbol, quote_asset)
    canonical_base_input = canonical_base or base_asset or inferred_base
    canonical_base_input = _normalize_symbol(canonical_base_input)

    if canonical_base_input in CANONICAL_BASE_ALIASES:
        canonical_base_input = CANONICAL_BASE_ALIASES[canonical_base_input]

    stripped_base, inferred_multiplier = _strip_multiplier(canonical_base_input)
    canonical_base = CANONICAL_BASE_ALIASES.get(stripped_base, stripped_base)

    if unit_scale is None or (unit_scale == 1.0 and inferred_multiplier != 1.0):
        unit_scale = inferred_multiplier
    if contract_multiplier is None:
        contract_multiplier = unit_scale

    return InstrumentIdentity(
        symbol=symbol,
        base_asset=base_asset or canonical_base,
        exchange=exchange,
        raw_symbol=raw_symbol,
        canonical_base=canonical_base or base_asset or inferred_base,
        quote_asset=(quote_asset or inferred_quote or "USDT").upper(),
        unit_scale=float(unit_scale or 1.0),
        contract_multiplier=float(contract_multiplier or unit_scale or 1.0),
        is_perpetual=is_perpetual,
    )
