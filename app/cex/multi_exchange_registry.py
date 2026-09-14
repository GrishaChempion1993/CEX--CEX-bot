"""Multi-exchange futures registry.

Fetches futures symbols from all configured exchanges, merges them,
and provides unified access to tickers across all exchanges.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.cex.base_exchange import BaseExchangeClient, FuturesSymbol, FuturesTicker
from app.cex.exchanges import ALL_EXCHANGES
from app.cex.instrument_identity import (
    InstrumentIdentity,
    build_instrument_identity,
    canonical_identity_key,
)

logger = logging.getLogger(__name__)


def _default_cache_file() -> Path:
    return Path(tempfile.gettempdir()) / "parser" / "multi_exchange_tokens.json"


@dataclass
class MultiExchangeToken:
    """A token that has futures on one or more exchanges."""

    canonical_base: str
    quote_asset: str = "USDT"
    unit_scale: float = 1.0
    contract_multiplier: float = 1.0
    exchanges: Dict[str, str] = field(default_factory=dict)
    raw_symbols: Dict[str, str] = field(default_factory=dict)
    exchange_identities: Dict[str, InstrumentIdentity] = field(default_factory=dict)
    mexc_symbol: str = ""

    @property
    def base_asset(self) -> str:
        return self.canonical_base

    @property
    def symbol(self) -> str:
        return f"{self.canonical_base}_{self.quote_asset}"

    @property
    def identity_key(self) -> str:
        return canonical_identity_key(
            self.canonical_base,
            self.quote_asset,
            self.unit_scale,
        )

    @property
    def exchange_list(self) -> List[str]:
        return sorted(self.exchanges.keys())

    @property
    def exchange_count(self) -> int:
        return len(self.exchanges)

    def add_symbol(self, exchange: str, symbol: FuturesSymbol, identity: InstrumentIdentity) -> None:
        self.exchanges[exchange] = symbol.symbol
        self.raw_symbols[exchange] = identity.raw_symbol
        self.exchange_identities[exchange] = identity
        if exchange == "mexc" and not self.mexc_symbol:
            self.mexc_symbol = symbol.symbol


class MultiExchangeRegistry:
    """Registry that aggregates futures symbols from multiple exchanges."""

    CACHE_FILE = _default_cache_file()
    CACHE_TTL = 86400  # 24 hours

    def __init__(
        self,
        exchange_names: Optional[List[str]] = None,
        *,
        cache_enabled: bool = True,
        cache_file: Optional[str | Path] = None,
        cache_ttl: Optional[int] = None,
        tickers_cache_ttl: float = 2.0,
    ):
        self.exchange_names = exchange_names or list(ALL_EXCHANGES.keys())
        self.tokens: Dict[str, MultiExchangeToken] = {}
        self._clients: Dict[str, BaseExchangeClient] = {}
        self._tickers_cache: Dict[str, Dict[str, FuturesTicker]] = {}
        self._tickers_cache_ts: float = 0
        self._tickers_cache_ttl: float = tickers_cache_ttl
        self.cache_enabled = cache_enabled
        self.cache_file = Path(cache_file) if cache_file else self.CACHE_FILE
        self.cache_ttl = cache_ttl or self.CACHE_TTL

    def _init_clients(self) -> Dict[str, BaseExchangeClient]:
        clients: Dict[str, BaseExchangeClient] = {}
        for name in self.exchange_names:
            cls = ALL_EXCHANGES.get(name)
            if cls:
                clients[name] = cls()
            else:
                logger.warning("Unknown exchange: %s", name)
        return clients

    async def _fetch_symbols_from_exchange(
        self, name: str, client: BaseExchangeClient
    ) -> List[FuturesSymbol]:
        try:
            await client.connect()
            symbols = await client.get_futures_symbols()
            logger.info("Fetched %d symbols from %s", len(symbols), name)
            return symbols
        except Exception as e:
            logger.error("Failed to fetch symbols from %s: %s", name, e)
            return []

    async def build_registry(self, force_refresh: bool = False) -> Dict[str, MultiExchangeToken]:
        if self.cache_enabled and not force_refresh:
            cached = self._load_cache()
            if cached:
                self.tokens = cached
                logger.info(
                    "Loaded %d tokens from cache (%d exchanges configured)",
                    len(self.tokens),
                    len(self.exchange_names),
                )
                return self.tokens

        clients = self._init_clients()
        self._clients = clients
        tasks = {
            name: asyncio.create_task(self._fetch_symbols_from_exchange(name, client))
            for name, client in clients.items()
        }

        results: Dict[str, List[FuturesSymbol]] = {}
        for name, task in tasks.items():
            try:
                results[name] = await task
            except Exception as e:
                logger.error("Task failed for %s: %s", name, e)
                results[name] = []

        for client in clients.values():
            try:
                await client.close()
            except Exception:
                pass

        tokens: Dict[str, MultiExchangeToken] = {}
        for exchange_name, symbols in results.items():
            for fs in symbols:
                identity = build_instrument_identity(
                    symbol=fs.symbol,
                    base_asset=fs.base_asset,
                    exchange=fs.exchange or exchange_name,
                    raw_symbol=fs.raw_symbol or fs.symbol,
                    canonical_base=fs.canonical_base or fs.base_asset,
                    quote_asset=fs.quote_asset,
                    unit_scale=fs.unit_scale,
                    contract_multiplier=fs.contract_multiplier,
                    is_perpetual=fs.is_perpetual,
                )
                token_key = identity.identity_key
                if token_key not in tokens:
                    tokens[token_key] = MultiExchangeToken(
                        canonical_base=identity.canonical_base,
                        quote_asset=identity.quote_asset,
                        unit_scale=identity.unit_scale,
                        contract_multiplier=identity.contract_multiplier,
                    )
                tokens[token_key].add_symbol(exchange_name, fs, identity)

        self.tokens = dict(
            sorted(
                tokens.items(),
                key=lambda item: (
                    -item[1].exchange_count,
                    item[1].canonical_base,
                    item[1].quote_asset,
                    item[1].unit_scale,
                ),
            )
        )

        if self.cache_enabled:
            self._save_cache()

        total = len(self.tokens)
        multi = sum(1 for t in self.tokens.values() if t.exchange_count > 1)
        exchange_counts = {name: len(syms) for name, syms in results.items() if syms}
        logger.info(
            "Multi-exchange registry: %d tokens total, %d on 2+ exchanges. Per-exchange: %s",
            total,
            multi,
            exchange_counts,
        )
        return self.tokens

    def get_tokens_on_exchanges(
        self,
        min_exchanges: int = 2,
        required_exchange: Optional[str] = None,
    ) -> List[MultiExchangeToken]:
        result = []
        for token in self.tokens.values():
            if token.exchange_count < min_exchanges:
                continue
            if required_exchange and required_exchange not in token.exchanges:
                continue
            result.append(token)
        return result

    def get_exchange_pairs(self) -> List[Tuple[str, str, str]]:
        pairs: List[Tuple[str, str, str]] = []
        for token in self.tokens.values():
            exchanges = token.exchange_list
            for i in range(len(exchanges)):
                for j in range(i + 1, len(exchanges)):
                    pairs.append((token.canonical_base, exchanges[i], exchanges[j]))
        return pairs

    def raw_symbol_for(self, exchange: str, symbol: str) -> str:
        for token in self.tokens.values():
            token_symbol = token.exchanges.get(exchange)
            if token_symbol == symbol:
                return token.raw_symbols.get(exchange, symbol)
        return symbol

    async def fetch_all_tickers(self, force_refresh: bool = False) -> Dict[str, Dict[str, FuturesTicker]]:
        now = time.monotonic()
        if (
            not force_refresh
            and now - self._tickers_cache_ts < self._tickers_cache_ttl
            and self._tickers_cache
        ):
            return self._tickers_cache

        clients = self._init_clients()
        results: Dict[str, Dict[str, FuturesTicker]] = {}

        async def fetch_one(name: str, client: BaseExchangeClient):
            try:
                await client.connect()
                tickers = await client.get_all_tickers()
                fetched_at_ms = int(time.time() * 1000)
                for symbol, ticker in tickers.items():
                    if not ticker.exchange:
                        ticker.exchange = name
                    if not ticker.raw_symbol:
                        ticker.raw_symbol = symbol
                    if ticker.fetched_at_ms is None:
                        ticker.fetched_at_ms = fetched_at_ms
                return name, tickers
            except Exception as e:
                logger.error("Tickers fetch failed for %s: %s", name, e)
                return name, {}
            finally:
                try:
                    await client.close()
                except Exception:
                    pass

        tasks = [fetch_one(name, client) for name, client in clients.items()]
        for coro in asyncio.as_completed(tasks):
            name, tickers = await coro
            results[name] = tickers

        self._tickers_cache = results
        self._tickers_cache_ts = time.monotonic()
        return results

    async def fetch_selected_tickers(
        self,
        symbols_by_exchange: Dict[str, set[str]],
        *,
        require_orderbook: bool = False,
    ) -> Dict[str, Dict[str, FuturesTicker]]:
        if not symbols_by_exchange:
            return {}

        clients = self._init_clients()
        results: Dict[str, Dict[str, FuturesTicker]] = {}

        async def fetch_one(name: str, client: BaseExchangeClient):
            requested = symbols_by_exchange.get(name) or set()
            if not requested:
                return name, {}
            try:
                await client.connect()
                try:
                    tickers = await client.get_all_tickers()
                except Exception as e:
                    if not require_orderbook:
                        raise
                    logger.debug("Selected tickers batch fetch failed for %s, falling back to top-of-book: %s", name, e)
                    tickers = {}
                filtered = {symbol: ticker for symbol, ticker in tickers.items() if symbol in requested}
                if require_orderbook:
                    for symbol in requested:
                        existing = filtered.get(symbol)
                        if existing and existing.has_orderbook_prices() and existing.has_orderbook_size():
                            continue
                        try:
                            book_ticker = await client.get_top_of_book(
                                symbol,
                                raw_symbol=self.raw_symbol_for(name, symbol),
                            )
                        except Exception as e:
                            logger.debug("Top-of-book fetch failed for %s %s: %s", name, symbol, e)
                            continue
                        if book_ticker is None:
                            continue
                        if existing is None:
                            filtered[symbol] = book_ticker
                        else:
                            filtered[symbol] = existing.merged_with(book_ticker)
                fetched_at_ms = int(time.time() * 1000)
                for symbol, ticker in filtered.items():
                    if not ticker.exchange:
                        ticker.exchange = name
                    if not ticker.raw_symbol:
                        ticker.raw_symbol = symbol
                    if ticker.fetched_at_ms is None:
                        ticker.fetched_at_ms = fetched_at_ms
                return name, filtered
            except Exception as e:
                logger.error("Selected tickers fetch failed for %s: %s", name, e)
                return name, {}
            finally:
                try:
                    await client.close()
                except Exception:
                    pass

        tasks = [fetch_one(name, client) for name, client in clients.items()]
        for coro in asyncio.as_completed(tasks):
            name, tickers = await coro
            results[name] = tickers
        return results

    async def fetch_selected_top_of_book(
        self,
        symbols_by_exchange: Dict[str, Dict[str, str]],
    ) -> Dict[str, Dict[str, FuturesTicker]]:
        if not symbols_by_exchange:
            return {}

        clients = self._init_clients()
        results: Dict[str, Dict[str, FuturesTicker]] = {}

        async def fetch_one(name: str, client: BaseExchangeClient):
            requested = symbols_by_exchange.get(name) or {}
            if not requested:
                return name, {}
            try:
                await client.connect()
                exchange_results: Dict[str, FuturesTicker] = {}
                for symbol, raw_symbol in requested.items():
                    ticker = await client.get_top_of_book(symbol, raw_symbol=raw_symbol or symbol)
                    if not ticker:
                        continue
                    if not ticker.exchange:
                        ticker.exchange = name
                    if not ticker.raw_symbol:
                        ticker.raw_symbol = raw_symbol or symbol
                    exchange_results[symbol] = ticker
                return name, exchange_results
            except Exception as e:
                logger.error("Top-of-book fetch failed for %s: %s", name, e)
                return name, {}
            finally:
                try:
                    await client.close()
                except Exception:
                    pass

        tasks = [fetch_one(name, client) for name, client in clients.items()]
        for coro in asyncio.as_completed(tasks):
            name, tickers = await coro
            results[name] = tickers
        return results

    def _load_cache(self) -> Optional[Dict[str, MultiExchangeToken]]:
        if not self.cache_file.exists():
            return None
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            ts = data.get("timestamp", "")
            if ts:
                cache_time = datetime.fromisoformat(ts)
                if datetime.utcnow() - cache_time > timedelta(seconds=self.cache_ttl):
                    logger.info("Multi-exchange cache expired")
                    return None
            tokens: Dict[str, MultiExchangeToken] = {}
            for item in data.get("tokens", []):
                token = MultiExchangeToken(
                    canonical_base=item.get("canonical_base") or item.get("base_asset", ""),
                    quote_asset=item.get("quote_asset", "USDT"),
                    unit_scale=float(item.get("unit_scale", 1.0) or 1.0),
                    contract_multiplier=float(item.get("contract_multiplier", 1.0) or 1.0),
                    mexc_symbol=item.get("mexc_symbol", ""),
                )
                exchanges = item.get("exchanges", {}) or {}
                raw_symbols = item.get("raw_symbols", {}) or {}
                for exchange_name, symbol in exchanges.items():
                    raw_symbol = raw_symbols.get(exchange_name, symbol)
                    identity = build_instrument_identity(
                        symbol=symbol,
                        base_asset=token.canonical_base,
                        exchange=exchange_name,
                        raw_symbol=raw_symbol,
                        canonical_base=token.canonical_base,
                        quote_asset=token.quote_asset,
                        unit_scale=token.unit_scale,
                        contract_multiplier=token.contract_multiplier,
                    )
                    fake_symbol = FuturesSymbol(
                        symbol=symbol,
                        base_asset=token.canonical_base,
                        exchange=exchange_name,
                        raw_symbol=raw_symbol,
                        canonical_base=identity.canonical_base,
                        quote_asset=identity.quote_asset,
                        unit_scale=identity.unit_scale,
                        contract_multiplier=identity.contract_multiplier,
                    )
                    token.add_symbol(exchange_name, fake_symbol, identity)
                tokens[token.identity_key] = token
            return tokens
        except Exception as e:
            logger.error("Failed to load multi-exchange cache: %s", e)
            return None

    def _save_cache(self):
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "timestamp": datetime.utcnow().isoformat(),
                "exchange_names": self.exchange_names,
                "tokens": [
                    {
                        "canonical_base": t.canonical_base,
                        "quote_asset": t.quote_asset,
                        "unit_scale": t.unit_scale,
                        "contract_multiplier": t.contract_multiplier,
                        "exchanges": t.exchanges,
                        "raw_symbols": t.raw_symbols,
                        "mexc_symbol": t.mexc_symbol,
                    }
                    for t in self.tokens.values()
                ],
            }
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.info("Saved %d tokens to multi-exchange cache", len(self.tokens))
        except Exception as e:
            logger.error("Failed to save multi-exchange cache: %s", e)
