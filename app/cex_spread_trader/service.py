"""CEX-CEX spread trader service with real execution accounting."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from typing import Dict, Optional

import redis.asyncio as aioredis

from app.cex_spread_trader.base_trading_client import (
    BaseTradingClient,
    ContractInfo,
    OrderFill,
    OrderResult,
    TopOfBook,
)
from app.cex_spread_trader.dry_run_client import DryRunTradingClient
from app.cex_spread_trader.execution import (
    base_qty_to_native_qty,
    compute_common_base_qty,
    compute_leg_realized_pnl_usd,
    compute_leg_stop_price,
    compute_limit_price,
    is_leg_stop_triggered,
    native_qty_to_base_qty,
)
from app.cex_spread_trader.exchanges import get_all_trading_clients
from app.cex_spread_trader.position_manager import LegState, PairedPosition, PositionManager
from app.config.settings import Config
from app.telegram.telegram_sender import TelegramSender
from app.trader.rolling_stats import RollingStatsTracker

logger = logging.getLogger(__name__)


class CexSpreadTraderService:
    """Consume Redis signals and execute paired futures trades."""

    MAX_SIGNAL_AGE_SEC = 30

    def __init__(self):
        self.redis = aioredis.from_url(Config.REDIS_URL, decode_responses=True)
        self.stream_key = Config.CEX_SPREAD_STREAM_KEY
        self.group = Config.CEX_SPREAD_TRADER_GROUP
        self.consumer = f"cex-spread-trader-{os.getpid()}"
        self.position_mgr = PositionManager(self.redis)
        self.positions: Dict[str, PairedPosition] = {}
        self.pnl_tracker = RollingStatsTracker(Config.DATA_DIR / "cex_spread_trader_pnl.json")
        self.telegram: Optional[TelegramSender] = None
        self._clients: Dict[str, BaseTradingClient] = {}
        self._balances: Dict[str, float] = {}
        self._balance_ts: Dict[str, float] = {}

    async def run(self) -> None:
        await self._ensure_group()
        self.positions = await self.position_mgr.load_all()
        await self._init_clients()

        async with TelegramSender(
            bot_token=Config.CEX_SPREAD_TRADER_TELEGRAM_BOT_TOKEN,
            chat_id=Config.CEX_SPREAD_TRADER_TELEGRAM_CHAT_ID,
        ) as telegram:
            self.telegram = telegram
            await self._notify(
                f"<b>CEX Spread Trader started</b>\n"
                f"Mode: {'DRY-RUN' if Config.CEX_SPREAD_TRADER_DRY_RUN else 'LIVE'}\n"
                f"Exchanges: {', '.join(sorted(self._clients.keys()))}\n"
                f"Recovered positions: {len(self.positions)}"
            )
            tasks = [
                asyncio.create_task(self._consume_signals()),
                asyncio.create_task(self._monitor_positions()),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await self._close_clients()

    async def _init_clients(self) -> None:
        enabled = [name.lower() for name in Config.CEX_SPREAD_TRADER_EXCHANGES]
        if Config.CEX_SPREAD_TRADER_DRY_RUN:
            for name in enabled:
                client = DryRunTradingClient(name)
                await client.connect()
                self._clients[name] = client
                logger.info("Dry-run trading client connected: %s", name)
            return

        registry = get_all_trading_clients()
        for name in enabled:
            cls = registry.get(name)
            if not cls:
                logger.warning("No trading client for exchange=%s, skipping", name)
                continue
            try:
                client = cls()
                await client.connect()
                self._clients[name] = client
                logger.info("Trading client connected: %s", name)
            except Exception as exc:
                logger.error("Failed to init trading client %s: %s", name, exc)

    async def _close_clients(self) -> None:
        for name, client in self._clients.items():
            try:
                await client.close()
            except Exception:
                logger.debug("Close failed exchange=%s", name, exc_info=True)
        self._clients.clear()

    async def _ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream_key, self.group, id="0", mkstream=True)
        except aioredis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume_signals(self) -> None:
        while True:
            try:
                pending = await self.redis.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream_key: "0"},
                    count=1,
                )
                if pending:
                    for _stream, messages in pending:
                        for msg_id, fields in messages:
                            await self._safe_handle(msg_id, fields)
                    continue

                entries = await self.redis.xreadgroup(
                    self.group,
                    self.consumer,
                    {self.stream_key: ">"},
                    count=20,
                    block=5000,
                )
                if not entries:
                    continue
                for _stream, messages in entries:
                    for msg_id, fields in messages:
                        await self._safe_handle(msg_id, fields)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Redis consume error: %s", exc, exc_info=True)
                await asyncio.sleep(2)

    async def _safe_handle(self, msg_id: str, fields: Dict[str, str]) -> None:
        try:
            handled = await self._handle_signal(fields)
        except Exception as exc:
            logger.error("Signal error msg=%s: %s", msg_id, exc, exc_info=True)
            await self._notify_error(f"Signal error: {exc}")
            return
        if handled:
            await self.redis.xack(self.stream_key, self.group, msg_id)

    async def _handle_signal(self, fields: Dict[str, str]) -> bool:
        event_type = fields.get("event_type", "")
        ts_ms = _parse_float(fields.get("timestamp_ms"))
        if ts_ms is not None:
            age_sec = (time.time() * 1000 - ts_ms) / 1000
            if age_sec > self.MAX_SIGNAL_AGE_SEC:
                logger.info("SKIP stale signal age=%.1fs", age_sec)
                return True

        if event_type == "open":
            await self._handle_open(fields)
            return True
        if event_type == "close":
            await self._handle_close(fields)
            return True
        logger.debug("Unknown event_type=%s", event_type)
        return True

    async def _handle_open(self, fields: Dict[str, str]) -> None:
        signal_id = fields.get("signal_id", "")
        canonical_base = fields.get("canonical_base", "")
        quote_asset = fields.get("quote_asset", "USDT")
        spread_pct = _parse_float(fields.get("spread_open_pct")) or 0.0
        confidence = _parse_float(fields.get("confidence")) or 0.0

        buy_exchange = fields.get("leg_a_exchange", "").lower()
        sell_exchange = fields.get("leg_b_exchange", "").lower()
        buy_symbol = fields.get("leg_a_symbol", "")
        sell_symbol = fields.get("leg_b_symbol", "")
        buy_raw_symbol = fields.get("leg_a_raw_symbol", "") or buy_symbol
        sell_raw_symbol = fields.get("leg_b_raw_symbol", "") or sell_symbol
        buy_price = _parse_float(fields.get("leg_a_ask_price")) or _parse_float(fields.get("leg_a_price")) or 0.0
        sell_price = _parse_float(fields.get("leg_b_bid_price")) or _parse_float(fields.get("leg_b_price")) or 0.0
        max_position_usd = _parse_float(fields.get("max_position_usd"))
        avg_close_sec = _parse_float(fields.get("avg_close_sec"))

        if not buy_exchange or not sell_exchange or not canonical_base or not buy_symbol or not sell_symbol:
            logger.warning("OPEN skipped: missing required fields signal=%s", signal_id)
            return
        if buy_exchange not in self._clients or sell_exchange not in self._clients:
            logger.info("OPEN skipped: missing trading client pair=%s/%s", buy_exchange, sell_exchange)
            return

        pair_key = f"{canonical_base}:{quote_asset}:{buy_exchange}:{sell_exchange}"
        existing = self.positions.get(pair_key)
        if existing and existing.status in {"pending_open", "open", "hedge_broken", "closing"}:
            logger.info("OPEN skipped: pair already active pair=%s status=%s", pair_key, existing.status)
            return
        if avg_close_sec is not None and avg_close_sec < Config.CEX_SPREAD_TRADER_MIN_AVG_CLOSE_SEC:
            logger.info("OPEN skipped: avg close too short pair=%s avg=%.1fs", pair_key, avg_close_sec)
            return

        position_usd = await self._compute_position_usd(buy_exchange, sell_exchange, max_position_usd)
        if position_usd < Config.CEX_SPREAD_TRADER_MIN_USD:
            logger.info("OPEN skipped: position too small pair=%s usd=%.2f", pair_key, position_usd)
            return

        pos = PairedPosition(
            signal_id=signal_id,
            canonical_base=canonical_base,
            quote_asset=quote_asset,
            spread_open_pct=spread_pct,
            spread_current_pct=spread_pct,
            confidence=confidence,
            buy_leg=LegState(
                exchange=buy_exchange,
                symbol=buy_symbol,
                raw_symbol=buy_raw_symbol,
                url=fields.get("leg_a_url", ""),
                side="long",
                price_source=fields.get("leg_a_price_source", ""),
            ),
            sell_leg=LegState(
                exchange=sell_exchange,
                symbol=sell_symbol,
                raw_symbol=sell_raw_symbol,
                url=fields.get("leg_b_url", ""),
                side="short",
                price_source=fields.get("leg_b_price_source", ""),
            ),
            opened_at=time.time(),
            updated_at=time.time(),
            status="pending_open",
            leverage=Config.CEX_SPREAD_TRADER_LEVERAGE,
            position_usd=position_usd,
            max_position_usd=max_position_usd or 0.0,
            avg_close_sec=avg_close_sec,
            avg_close_count=_parse_int(fields.get("avg_close_count")),
        )

        await self.position_mgr.save(pos)
        logger.info(
            "OPEN signal pair=%s spread=%.2f%% buy=%s@%s sell=%s@%s usd=%.2f",
            pair_key,
            spread_pct,
            buy_symbol,
            buy_exchange,
            sell_symbol,
            sell_exchange,
            position_usd,
        )

        opened = await self._execute_open(pos, buy_price=buy_price, sell_price=sell_price)
        if not opened and pos.pair_key not in self.positions:
            await self.position_mgr.delete(pos.pair_key)

    async def _execute_open(self, pos: PairedPosition, buy_price: float = 0.0, sell_price: float = 0.0) -> bool:
        buy_client = self._clients[pos.buy_leg.exchange]
        sell_client = self._clients[pos.sell_leg.exchange]

        buy_contract = await self._get_contract_info(buy_client, pos.buy_leg)
        sell_contract = await self._get_contract_info(sell_client, pos.sell_leg)
        if buy_contract is None or sell_contract is None:
            pos.status = "error"
            if buy_contract is None:
                pos.buy_leg.error = "no_contract_info"
            if sell_contract is None:
                pos.sell_leg.error = "no_contract_info"
            await self._notify_error(f"Open failed (no contract info): {pos.pair_key}")
            return False

        pos.buy_leg.contract_multiplier = float(buy_contract.contract_multiplier or 1.0)
        pos.sell_leg.contract_multiplier = float(sell_contract.contract_multiplier or 1.0)

        try:
            await asyncio.gather(
                buy_client.set_leverage(pos.buy_leg.symbol, pos.leverage),
                sell_client.set_leverage(pos.sell_leg.symbol, pos.leverage),
            )
        except Exception as exc:
            logger.warning("Leverage set failed pair=%s: %s", pos.pair_key, exc)

        buy_book = await _maybe_get_top_of_book(buy_client, pos.buy_leg.symbol, pos.buy_leg.raw_symbol)
        sell_book = await _maybe_get_top_of_book(sell_client, pos.sell_leg.symbol, pos.sell_leg.raw_symbol)
        buy_ref_price = _book_or_fallback_price(buy_book, "buy", buy_price or float(getattr(buy_client, "price", 0.0)))
        sell_ref_price = _book_or_fallback_price(sell_book, "sell", sell_price or float(getattr(sell_client, "price", 0.0)))
        if buy_ref_price <= 0 or sell_ref_price <= 0:
            pos.status = "error"
            await self._notify_error(f"Open failed (no executable prices): {pos.pair_key}")
            return False

        desired_base_qty = compute_common_base_qty(
            pos.position_usd,
            buy_ref_price,
            sell_ref_price,
            pos.max_position_usd or None,
        )
        buy_native_qty = base_qty_to_native_qty(desired_base_qty, buy_contract)
        sell_native_qty = base_qty_to_native_qty(desired_base_qty, sell_contract)
        effective_base_qty = min(
            native_qty_to_base_qty(buy_native_qty, buy_contract),
            native_qty_to_base_qty(sell_native_qty, sell_contract),
        )
        if effective_base_qty <= 0:
            pos.status = "error"
            await self._notify_error(f"Open failed (qty rounded to zero): {pos.pair_key}")
            return False

        buy_native_qty = base_qty_to_native_qty(effective_base_qty, buy_contract)
        sell_native_qty = base_qty_to_native_qty(effective_base_qty, sell_contract)
        if buy_native_qty <= 0 or sell_native_qty <= 0:
            pos.status = "error"
            await self._notify_error(f"Open failed (native qty is zero): {pos.pair_key}")
            return False

        pos.target_base_qty = effective_base_qty
        pos.buy_leg.target_base_qty = effective_base_qty
        pos.sell_leg.target_base_qty = effective_base_qty
        pos.buy_leg.submitted_qty = buy_native_qty
        pos.sell_leg.submitted_qty = sell_native_qty
        pos.updated_at = time.time()
        await self.position_mgr.save(pos)

        open_results = await asyncio.gather(
            self._execute_leg_open(pos, "buy", buy_client, buy_contract, buy_native_qty),
            self._execute_leg_open(pos, "sell", sell_client, sell_contract, sell_native_qty),
        )
        buy_success, sell_success = open_results

        if buy_success and sell_success:
            pos.status = "open"
            pos.updated_at = time.time()
            self._recompute_position_pnl(pos)
            self.positions[pos.pair_key] = pos
            await self.position_mgr.save(pos)
            await self._notify_open(pos)
            logger.info("OPEN success pair=%s base_qty=%.8f", pos.pair_key, pos.target_base_qty)
            return True

        if buy_success:
            reverted = await self._close_single_leg(pos, "buy", "open_revert_sell_failed", notify=False)
            if reverted and pos.buy_leg.is_closed:
                self.positions.pop(pos.pair_key, None)
                await self.position_mgr.delete(pos.pair_key)
            else:
                pos.status = "hedge_broken"
                self.positions[pos.pair_key] = pos
                await self.position_mgr.save(pos)
        if sell_success:
            reverted = await self._close_single_leg(pos, "sell", "open_revert_buy_failed", notify=False)
            if reverted and pos.sell_leg.is_closed:
                self.positions.pop(pos.pair_key, None)
                await self.position_mgr.delete(pos.pair_key)
            else:
                pos.status = "hedge_broken"
                self.positions[pos.pair_key] = pos
                await self.position_mgr.save(pos)

        if not buy_success and not sell_success:
            pos.status = "error"
            await self.position_mgr.delete(pos.pair_key)

        await self._notify_error(
            f"Open failed pair={pos.pair_key}\n"
            f"buy={pos.buy_leg.error or pos.buy_leg.status}\n"
            f"sell={pos.sell_leg.error or pos.sell_leg.status}"
        )
        return False

    async def _execute_leg_open(
        self,
        pos: PairedPosition,
        leg_name: str,
        client: BaseTradingClient,
        contract: ContractInfo,
        requested_qty: float,
    ) -> bool:
        leg = pos.leg(leg_name)
        side = "buy" if leg.side == "long" else "sell"
        leg.status = "pending"

        result = await self._execute_target_order(
            client=client,
            symbol=leg.symbol,
            raw_symbol=leg.raw_symbol,
            side=side,
            requested_qty=requested_qty,
            reduce_only=False,
            contract=contract,
        )
        if not result.success:
            leg.status = "error"
            leg.error = result.error_msg or "open_failed"
            return False

        leg.register_order_id(result.order_id)
        base_qty = native_qty_to_base_qty(result.filled_qty, contract)
        leg.executed_qty = result.filled_qty
        leg.executed_base_qty = base_qty
        leg.remaining_qty = result.filled_qty
        leg.remaining_base_qty = base_qty
        leg.avg_entry_price = result.filled_price
        leg.fees_open_usd += result.fees_usd
        leg.last_fill_ts = time.time()
        leg.stop_loss_price = compute_leg_stop_price(leg.side, leg.avg_entry_price, Config.CEX_SPREAD_TRADER_STOP_LOSS_PCT)
        leg.status = "open"
        leg.error = ""
        return True

    async def _handle_close(self, fields: Dict[str, str]) -> None:
        canonical_base = fields.get("canonical_base", "")
        quote_asset = fields.get("quote_asset", "USDT")
        buy_exchange = fields.get("leg_a_exchange", "").lower()
        sell_exchange = fields.get("leg_b_exchange", "").lower()
        close_reason = fields.get("close_reason", "signal_close") or "signal_close"

        pair_key = f"{canonical_base}:{quote_asset}:{buy_exchange}:{sell_exchange}"
        pos = self.positions.get(pair_key)
        if not pos:
            return
        if pos.status not in {"open", "hedge_broken"}:
            return

        logger.info("CLOSE signal pair=%s reason=%s", pair_key, close_reason)
        await self._execute_close(pos, close_reason)

    async def _execute_close(self, pos: PairedPosition, reason: str) -> None:
        pos.status = "closing"
        pos.updated_at = time.time()
        await self.position_mgr.save(pos)

        tasks = []
        if pos.buy_leg.remaining_base_qty > 0 and pos.buy_leg.status != "closed":
            tasks.append(self._close_single_leg(pos, "buy", reason, notify=False))
        if pos.sell_leg.remaining_base_qty > 0 and pos.sell_leg.status != "closed":
            tasks.append(self._close_single_leg(pos, "sell", reason, notify=False))
        if tasks:
            await asyncio.gather(*tasks)
        await self._finalize_position_if_done(pos, reason)

    async def _close_single_leg(
        self,
        pos: PairedPosition,
        leg_name: str,
        reason: str,
        notify: bool = True,
    ) -> bool:
        leg = pos.leg(leg_name)
        if leg.remaining_base_qty <= 0 or leg.status == "closed":
            return True
        client = self._clients.get(leg.exchange)
        if client is None:
            leg.status = "error"
            leg.error = "missing_client"
            return False

        contract = await self._get_contract_info(client, leg)
        if contract is None:
            leg.status = "error"
            leg.error = "no_contract_info"
            return False

        side = "sell" if leg.side == "long" else "buy"
        result = await self._execute_target_order(
            client=client,
            symbol=leg.symbol,
            raw_symbol=leg.raw_symbol,
            side=side,
            requested_qty=leg.remaining_qty,
            reduce_only=True,
            contract=contract,
        )
        if not result.success:
            leg.status = "error"
            leg.error = result.error_msg or "close_failed"
            await self.position_mgr.save(pos)
            return False

        closed_native_qty = min(result.filled_qty, leg.remaining_qty)
        closed_base_qty = min(native_qty_to_base_qty(closed_native_qty, contract), leg.remaining_base_qty)
        previous_closed_base = leg.closed_base_qty
        total_closed_base = previous_closed_base + closed_base_qty
        if closed_base_qty > 0:
            leg.avg_exit_price = _weighted_price(
                leg.avg_exit_price,
                previous_closed_base,
                result.filled_price,
                closed_base_qty,
            )

        leg.register_order_id(result.order_id)
        leg.closed_qty += closed_native_qty
        leg.closed_base_qty = total_closed_base
        leg.remaining_qty = max(leg.remaining_qty - closed_native_qty, 0.0)
        leg.remaining_base_qty = max(leg.remaining_base_qty - closed_base_qty, 0.0)
        leg.fees_close_usd += result.fees_usd
        leg.last_fill_ts = time.time()
        leg.close_reason = reason
        leg.status = "closed" if leg.remaining_base_qty <= _base_qty_epsilon(contract) else "open"
        leg.error = ""

        funding = await client.get_funding_payment(leg.symbol, pos.opened_at, time.time())
        if funding is None:
            pos.funding_unavailable = True
        else:
            leg.funding_usd += funding

        pos.updated_at = time.time()
        self._recompute_position_pnl(pos)
        await self.position_mgr.save(pos)

        if leg.status == "closed":
            other_leg = pos.leg(pos.other_leg_name(leg_name))
            if other_leg.remaining_base_qty > 0:
                pos.status = "hedge_broken"
                await self.position_mgr.save(pos)
                if notify:
                    await self._notify_leg_close(pos, leg_name, reason)
            else:
                await self._finalize_position_if_done(pos, reason, notify=notify)
        return leg.status == "closed"

    async def _finalize_position_if_done(
        self,
        pos: PairedPosition,
        reason: str,
        notify: bool = True,
    ) -> bool:
        self._recompute_position_pnl(pos)
        if pos.has_open_legs:
            pos.status = "hedge_broken" if pos.status != "closing" else "hedge_broken"
            pos.updated_at = time.time()
            await self.position_mgr.save(pos)
            self.positions[pos.pair_key] = pos
            return False

        pos.status = "closed"
        pos.close_reason = reason
        pos.closed_at = time.time()
        pos.updated_at = pos.closed_at
        self.positions.pop(pos.pair_key, None)
        await self.position_mgr.delete(pos.pair_key)
        stats = await self._record_close_stats(pos)
        if notify:
            await self._notify_close(pos, stats)
        return True

    async def _monitor_positions(self) -> None:
        while True:
            try:
                await asyncio.sleep(Config.CEX_SPREAD_TRADER_MONITOR_SEC)
                if not Config.CEX_SPREAD_TRADER_STOP_LOSS_ENABLED:
                    continue
                for pos in list(self.positions.values()):
                    if pos.status not in {"open", "hedge_broken"}:
                        continue
                    for leg_name in ("buy", "sell"):
                        leg = pos.leg(leg_name)
                        if leg.remaining_base_qty <= 0 or leg.status != "open":
                            continue
                        client = self._clients.get(leg.exchange)
                        if client is None:
                            continue
                        current_price = await self._get_stop_reference_price(client, leg)
                        if current_price <= 0:
                            continue
                        if is_leg_stop_triggered(
                            leg.side,
                            leg.avg_entry_price,
                            current_price,
                            Config.CEX_SPREAD_TRADER_STOP_LOSS_PCT,
                        ):
                            logger.info(
                                "LEG stop triggered pair=%s leg=%s current=%.8f stop=%.8f",
                                pos.pair_key,
                                leg_name,
                                current_price,
                                leg.stop_loss_price,
                            )
                            await self._close_single_leg(pos, leg_name, "stop_loss", notify=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Monitor error: %s", exc, exc_info=True)

    async def _get_stop_reference_price(self, client: BaseTradingClient, leg: LegState) -> float:
        close_side = "sell" if leg.side == "long" else "buy"
        book = await _maybe_get_top_of_book(client, leg.symbol, leg.raw_symbol)
        if book is not None:
            price = book.price_for_side(close_side)
            if price > 0:
                return price
        position = await client.get_position(leg.symbol)
        if position and position.mark_price > 0:
            return position.mark_price
        return 0.0

    async def _compute_position_usd(
        self,
        buy_exchange: str,
        sell_exchange: str,
        max_position_usd: Optional[float],
        price: Optional[float] = None,
    ) -> float:
        buy_bal = await self._get_cached_balance(buy_exchange)
        sell_bal = await self._get_cached_balance(sell_exchange)
        min_bal = min(buy_bal, sell_bal)
        target = min_bal * Config.CEX_SPREAD_TRADER_POSITION_PCT * max(Config.CEX_SPREAD_TRADER_LEVERAGE, 1)
        if max_position_usd and max_position_usd > 0:
            target = min(target, max_position_usd)
        return max(target, 0.0)

    async def _get_cached_balance(self, exchange: str) -> float:
        now = time.time()
        if exchange in self._balances and (now - self._balance_ts.get(exchange, 0)) < 60:
            return self._balances[exchange]
        if Config.CEX_SPREAD_TRADER_DRY_RUN:
            bal = max(Config.CEX_SPREAD_TRADER_DRY_RUN_BALANCE_USD, 0.0)
            self._balances[exchange] = bal
            self._balance_ts[exchange] = now
            return bal
        client = self._clients.get(exchange)
        if client is None:
            return 0.0
        try:
            bal = await client.get_balance()
            self._balances[exchange] = bal
            self._balance_ts[exchange] = now
            return bal
        except Exception as exc:
            logger.warning("Balance fetch failed exchange=%s: %s", exchange, exc)
            return self._balances.get(exchange, 0.0)

    async def _get_contract_info(self, client: BaseTradingClient, leg: LegState) -> Optional[ContractInfo]:
        info = await client.get_contract_info(leg.symbol)
        if info is None:
            info = await client.get_public_contract_info(leg.symbol)
        if info is None:
            return None
        if not info.raw_symbol:
            info.raw_symbol = leg.raw_symbol or leg.symbol
        if not info.contract_multiplier:
            info.contract_multiplier = 1.0
        return info

    async def _execute_target_order(
        self,
        client: BaseTradingClient,
        symbol: str,
        raw_symbol: str,
        side: str,
        requested_qty: float,
        reduce_only: bool,
        contract: ContractInfo,
    ) -> OrderResult:
        if requested_qty <= 0:
            return OrderResult(
                success=False,
                exchange=client.EXCHANGE_NAME,
                symbol=symbol,
                side=side,
                requested_qty=requested_qty,
                reduce_only=reduce_only,
                error_msg="qty_zero",
            )

        book = await _maybe_get_top_of_book(client, symbol, raw_symbol)
        limit_supported = _supports_limit_orders(client) and book is not None
        if limit_supported:
            limit_price = compute_limit_price(side, book, Config.CEX_SPREAD_TRADER_SLIPPAGE_PCT)
            if limit_price > 0:
                limit_result = await client.place_limit_order(
                    symbol=symbol,
                    side=side,
                    qty=requested_qty,
                    price=limit_price,
                    reduce_only=reduce_only,
                )
                limit_result = await self._normalize_order_result(
                    client=client,
                    symbol=symbol,
                    side=side,
                    requested_qty=requested_qty,
                    reduce_only=reduce_only,
                    contract=contract,
                    result=limit_result,
                    fallback_price=limit_price,
                )
                if limit_result.success and limit_result.remaining_qty <= _native_qty_epsilon(contract):
                    return limit_result
                if limit_result.success and limit_result.order_id:
                    tracked = await self._wait_limit_fill_or_timeout(
                        client=client,
                        symbol=symbol,
                        side=side,
                        contract=contract,
                        reduce_only=reduce_only,
                        base_result=limit_result,
                    )
                    if tracked.success and tracked.remaining_qty <= _native_qty_epsilon(contract):
                        return tracked
                    if tracked.remaining_qty > _native_qty_epsilon(contract):
                        await client.cancel_order(symbol, tracked.order_id)
                        market_result = await self._place_market_and_reconcile(
                            client=client,
                            symbol=symbol,
                            raw_symbol=raw_symbol,
                            side=side,
                            qty=tracked.remaining_qty,
                            reduce_only=reduce_only,
                            contract=contract,
                        )
                        return _merge_order_results(tracked, market_result, requested_qty)
                    return tracked
                if limit_result.error_msg not in {"limit_not_supported", ""}:
                    return limit_result

        return await self._place_market_and_reconcile(
            client=client,
            symbol=symbol,
            raw_symbol=raw_symbol,
            side=side,
            qty=requested_qty,
            reduce_only=reduce_only,
            contract=contract,
        )

    async def _wait_limit_fill_or_timeout(
        self,
        client: BaseTradingClient,
        symbol: str,
        side: str,
        contract: ContractInfo,
        reduce_only: bool,
        base_result: OrderResult,
    ) -> OrderResult:
        deadline = time.monotonic() + max(Config.CEX_SPREAD_TRADER_ORDER_TIMEOUT_SEC, 0.0)
        current = base_result
        if not _supports_order_tracking(client) or not base_result.order_id:
            return current

        while time.monotonic() < deadline:
            status = await client.get_order_status(symbol, base_result.order_id)
            if status is not None:
                current = await self._normalize_order_result(
                    client=client,
                    symbol=symbol,
                    side=side,
                    requested_qty=base_result.requested_qty,
                    reduce_only=reduce_only,
                    contract=contract,
                    result=status,
                    fallback_price=current.filled_price,
                )
                if current.remaining_qty <= _native_qty_epsilon(contract):
                    return current
            await asyncio.sleep(min(2.0, max(Config.CEX_SPREAD_TRADER_MONITOR_SEC, 0.25)))
        return current

    async def _place_market_and_reconcile(
        self,
        client: BaseTradingClient,
        symbol: str,
        raw_symbol: str,
        side: str,
        qty: float,
        reduce_only: bool,
        contract: ContractInfo,
    ) -> OrderResult:
        book = await _maybe_get_top_of_book(client, symbol, raw_symbol)
        fallback_price = _book_or_fallback_price(book, side, 0.0)
        result = await client.place_market_order(symbol, side, qty, reduce_only=reduce_only)
        return await self._normalize_order_result(
            client=client,
            symbol=symbol,
            side=side,
            requested_qty=qty,
            reduce_only=reduce_only,
            contract=contract,
            result=result,
            fallback_price=fallback_price,
        )

    async def _normalize_order_result(
        self,
        client: BaseTradingClient,
        symbol: str,
        side: str,
        requested_qty: float,
        reduce_only: bool,
        contract: ContractInfo,
        result: OrderResult,
        fallback_price: float,
    ) -> OrderResult:
        result.exchange = result.exchange or client.EXCHANGE_NAME
        result.symbol = result.symbol or symbol
        result.side = result.side or side.lower()
        result.requested_qty = result.requested_qty or requested_qty
        result.order_type = result.order_type or "market"
        result.reduce_only = reduce_only
        if result.remaining_qty <= 0:
            result.remaining_qty = max(result.requested_qty - result.filled_qty, 0.0)

        if result.success and result.filled_qty > 0 and result.filled_price > 0:
            if result.fees_usd <= 0:
                result.fees_usd = _estimate_result_fees(client, contract, result)
            return result

        if not Config.CEX_SPREAD_TRADER_DRY_RUN and (result.filled_qty <= 0 or result.filled_price <= 0):
            return OrderResult(
                success=False,
                exchange=client.EXCHANGE_NAME,
                symbol=symbol,
                side=side.lower(),
                requested_qty=requested_qty,
                order_type=result.order_type or "market",
                reduce_only=reduce_only,
                order_id=result.order_id,
                error_msg=result.error_msg or "missing_fill_data",
            )

        if result.success and fallback_price > 0:
            result.filled_qty = result.filled_qty or requested_qty
            result.filled_price = result.filled_price or fallback_price
            result.remaining_qty = max(result.requested_qty - result.filled_qty, 0.0)
            if not result.fills and result.filled_qty > 0:
                result.fills = [
                    OrderFill(
                        qty=result.filled_qty,
                        price=result.filled_price,
                        fee_usd=_estimate_result_fees(client, contract, result),
                        fee_currency=contract.quote_asset or "USDT",
                        timestamp_ms=int(time.time() * 1000),
                        liquidity="taker",
                        order_id=result.order_id,
                    )
                ]
            if result.fees_usd <= 0:
                result.fees_usd = _estimate_result_fees(client, contract, result)
            return result

        return result

    def _recompute_position_pnl(self, pos: PairedPosition) -> None:
        gross_pnl = 0.0
        net_pnl = 0.0
        fees_open = 0.0
        fees_close = 0.0
        funding = 0.0
        for leg in (pos.buy_leg, pos.sell_leg):
            fees_open += leg.fees_open_usd
            fees_close += leg.fees_close_usd
            funding += leg.funding_usd
            if leg.closed_base_qty > 0 and leg.avg_entry_price > 0 and leg.avg_exit_price > 0:
                if leg.side == "long":
                    gross_pnl += (leg.avg_exit_price - leg.avg_entry_price) * leg.closed_base_qty
                else:
                    gross_pnl += (leg.avg_entry_price - leg.avg_exit_price) * leg.closed_base_qty
            net_pnl += compute_leg_realized_pnl_usd(
                side=leg.side,
                base_qty=leg.closed_base_qty,
                entry_price=leg.avg_entry_price,
                exit_price=leg.avg_exit_price,
                fees_open_usd=leg.fees_open_usd,
                fees_close_usd=leg.fees_close_usd,
                funding_usd=leg.funding_usd,
            )
        pos.gross_pnl_usd = gross_pnl
        pos.net_pnl_usd = net_pnl
        pos.fees_open_usd = fees_open
        pos.fees_close_usd = fees_close
        pos.funding_usd = funding

    async def _record_close_stats(self, pos: PairedPosition) -> Dict[str, Dict[str, float]]:
        total_fees = pos.fees_open_usd + pos.fees_close_usd
        updates = {
            "gross_pnl": pos.gross_pnl_usd,
            "net_pnl": pos.net_pnl_usd,
            "fees": total_fees,
            "funding": pos.funding_usd,
            "closed_positions": 1.0,
            "winning_positions": 1.0 if pos.net_pnl_usd > 0 else 0.0,
            "losing_positions": 1.0 if pos.net_pnl_usd < 0 else 0.0,
            "breakeven_positions": 1.0 if abs(pos.net_pnl_usd) <= 1e-9 else 0.0,
        }
        stats = await self.pnl_tracker.add_and_get_stats(updates)
        summary = self._build_pnl_summary(stats)
        logger.info(
            "CLOSE summary pair=%s net=%.4f total_net=%.4f closed=%.0f wins=%.0f losses=%.0f winrate=%.1f%%",
            pos.pair_key,
            pos.net_pnl_usd,
            summary["total_net_pnl_all"],
            summary["closed_positions_all"],
            summary["winning_positions_all"],
            summary["losing_positions_all"],
            summary["win_rate_all_pct"],
        )
        return stats

    def _build_pnl_summary(self, stats: Dict[str, Dict[str, float]]) -> Dict[str, float]:
        def _val(name: str, bucket: str) -> float:
            return float(stats.get(name, {}).get(bucket, 0.0) or 0.0)

        closed_all = _val("closed_positions", "val_all")
        closed_24h = _val("closed_positions", "val_24h")
        wins_all = _val("winning_positions", "val_all")
        wins_24h = _val("winning_positions", "val_24h")
        losses_all = _val("losing_positions", "val_all")
        losses_24h = _val("losing_positions", "val_24h")
        summary = {
            "total_net_pnl_all": _val("net_pnl", "val_all"),
            "total_net_pnl_24h": _val("net_pnl", "val_24h"),
            "total_gross_pnl_all": _val("gross_pnl", "val_all"),
            "total_fees_all": _val("fees", "val_all"),
            "total_funding_all": _val("funding", "val_all"),
            "closed_positions_all": closed_all,
            "closed_positions_24h": closed_24h,
            "winning_positions_all": wins_all,
            "winning_positions_24h": wins_24h,
            "losing_positions_all": losses_all,
            "losing_positions_24h": losses_24h,
            "win_rate_all_pct": (wins_all / closed_all * 100.0) if closed_all > 0 else 0.0,
            "win_rate_24h_pct": (wins_24h / closed_24h * 100.0) if closed_24h > 0 else 0.0,
            "avg_net_pnl_all": (_val("net_pnl", "val_all") / closed_all) if closed_all > 0 else 0.0,
            "avg_net_pnl_24h": (_val("net_pnl", "val_24h") / closed_24h) if closed_24h > 0 else 0.0,
        }
        return summary

    async def _notify(self, text: str) -> None:
        if self.telegram is None:
            return
        try:
            await self.telegram.send_message(text)
        except Exception as exc:
            logger.warning("Telegram send failed: %s", exc)

    async def _notify_error(self, text: str) -> None:
        await self._notify(f"<b>CEX Spread Trader Error</b>\n{html.escape(text)}")

    async def _notify_open(self, pos: PairedPosition) -> None:
        mode = "DRY-RUN" if Config.CEX_SPREAD_TRADER_DRY_RUN else "LIVE"
        msg = (
            f"<b>CEX Spread OPEN [{mode}]</b>\n"
            f"{pos.canonical_base}_{pos.quote_asset}\n"
            f"Spread: {pos.spread_open_pct:.2f}%\n"
            f"Target Base Qty: {pos.target_base_qty:.8f}\n"
            f"BUY {pos.buy_leg.exchange.upper()} {pos.buy_leg.symbol} @ ${pos.buy_leg.avg_entry_price:.8f}\n"
            f"SELL {pos.sell_leg.exchange.upper()} {pos.sell_leg.symbol} @ ${pos.sell_leg.avg_entry_price:.8f}\n"
            f"Open Fees: ${pos.fees_open_usd:.4f}\n"
            f"Stop Buy: ${pos.buy_leg.stop_loss_price:.8f}\n"
            f"Stop Sell: ${pos.sell_leg.stop_loss_price:.8f}"
        )
        await self._notify(msg)

    async def _notify_leg_close(self, pos: PairedPosition, leg_name: str, reason: str) -> None:
        leg = pos.leg(leg_name)
        other = pos.leg(pos.other_leg_name(leg_name))
        msg = (
            f"<b>CEX Spread Leg Closed</b>\n"
            f"{pos.canonical_base}_{pos.quote_asset}\n"
            f"Leg: {leg.exchange.upper()} {leg.symbol}\n"
            f"Reason: {html.escape(reason)}\n"
            f"Exit: ${leg.avg_exit_price:.8f}\n"
            f"Net PnL Leg: ${compute_leg_realized_pnl_usd(leg.side, leg.closed_base_qty, leg.avg_entry_price, leg.avg_exit_price, leg.fees_open_usd, leg.fees_close_usd, leg.funding_usd):.4f}\n"
            f"Remaining Leg: {other.exchange.upper()} {other.symbol}"
        )
        await self._notify(msg)

    async def _notify_close(
        self,
        pos: PairedPosition,
        stats: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> None:
        duration = pos.closed_at - pos.opened_at if pos.closed_at else 0.0
        pnl_prefix = "+" if pos.net_pnl_usd >= 0 else ""
        funding_note = "partial" if pos.funding_unavailable else "full"
        summary = self._build_pnl_summary(stats or {})
        msg = (
            f"<b>CEX Spread CLOSE</b>\n"
            f"{pos.canonical_base}_{pos.quote_asset}\n"
            f"Reason: {html.escape(pos.close_reason or 'signal_close')}\n"
            f"Gross PnL: {pnl_prefix}${pos.gross_pnl_usd:.4f}\n"
            f"Net PnL: {pnl_prefix}${pos.net_pnl_usd:.4f}\n"
            f"Fees: ${pos.fees_open_usd + pos.fees_close_usd:.4f}\n"
            f"Funding ({funding_note}): ${pos.funding_usd:.4f}\n"
            f"Duration: {duration:.0f}s\n"
            f"\n"
            f"<b>Total Stats</b>\n"
            f"Closed: {summary['closed_positions_all']:.0f} | Winrate: {summary['win_rate_all_pct']:.1f}%\n"
            f"Net All: ${summary['total_net_pnl_all']:.4f} | Avg: ${summary['avg_net_pnl_all']:.4f}\n"
            f"Net 24h: ${summary['total_net_pnl_24h']:.4f} | Closed 24h: {summary['closed_positions_24h']:.0f}"
        )
        await self._notify(msg)


def _parse_float(value: Optional[str]) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _weighted_price(current_price: float, current_qty: float, new_price: float, new_qty: float) -> float:
    total_qty = current_qty + new_qty
    if total_qty <= 0:
        return 0.0
    return ((current_price * current_qty) + (new_price * new_qty)) / total_qty


def _native_qty_epsilon(contract: ContractInfo) -> float:
    return max(float(contract.qty_step or 0.0), 1e-9)


def _base_qty_epsilon(contract: ContractInfo) -> float:
    return max(_native_qty_epsilon(contract) * max(float(contract.contract_multiplier or 1.0), 1.0), 1e-9)


def _book_or_fallback_price(book: Optional[TopOfBook], side: str, fallback: float) -> float:
    if book is None:
        return fallback
    price = book.price_for_side(side)
    if price > 0:
        return price
    return fallback


def _estimate_result_fees(client: BaseTradingClient, contract: ContractInfo, result: OrderResult) -> float:
    notional = native_qty_to_base_qty(result.filled_qty, contract) * max(result.filled_price, 0.0)
    estimator = getattr(client, "estimate_fee_usd", None)
    if callable(estimator):
        return estimator(notional, contract.symbol)
    return 0.0


def _supports_limit_orders(client) -> bool:
    fn = getattr(client, "supports_limit_orders", None)
    return bool(fn()) if callable(fn) else False


def _supports_order_tracking(client) -> bool:
    fn = getattr(client, "supports_order_tracking", None)
    return bool(fn()) if callable(fn) else False


async def _maybe_get_top_of_book(client, symbol: str, raw_symbol: str) -> Optional[TopOfBook]:
    fn = getattr(client, "get_top_of_book", None)
    if not callable(fn):
        return None
    try:
        return await fn(symbol, raw_symbol=raw_symbol)
    except TypeError:
        try:
            return await fn(symbol)
        except Exception:
            return None


def _merge_order_results(first: OrderResult, second: OrderResult, requested_qty: float) -> OrderResult:
    fills = list(first.fills) + list(second.fills)
    filled_qty = max(first.filled_qty, 0.0) + max(second.filled_qty, 0.0)
    filled_price = 0.0
    if fills:
        total_qty = sum(fill.qty for fill in fills)
        if total_qty > 0:
            filled_price = sum(fill.qty * fill.price for fill in fills) / total_qty
    elif filled_qty > 0:
        filled_price = (
            (max(first.filled_qty, 0.0) * max(first.filled_price, 0.0))
            + (max(second.filled_qty, 0.0) * max(second.filled_price, 0.0))
        ) / filled_qty
    return OrderResult(
        success=first.success and second.success and filled_qty > 0 and max(requested_qty - filled_qty, 0.0) <= 1e-9,
        order_id=second.order_id or first.order_id,
        exchange=second.exchange or first.exchange,
        symbol=second.symbol or first.symbol,
        side=second.side or first.side,
        requested_qty=requested_qty,
        filled_qty=filled_qty,
        remaining_qty=max(requested_qty - filled_qty, 0.0),
        filled_price=filled_price,
        fees_usd=max(first.fees_usd, 0.0) + max(second.fees_usd, 0.0),
        fee_currency=second.fee_currency or first.fee_currency,
        status=second.status or first.status,
        order_type=second.order_type or first.order_type,
        reduce_only=second.reduce_only or first.reduce_only,
        is_final=True,
        fill_source="+".join(part for part in [first.fill_source, second.fill_source] if part),
        error_msg=second.error_msg or first.error_msg,
        fills=fills,
    )
