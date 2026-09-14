"""Exchange trading client registry."""
from __future__ import annotations

from typing import Dict, Type

from app.cex_spread_trader.base_trading_client import BaseTradingClient


def get_all_trading_clients() -> Dict[str, Type[BaseTradingClient]]:
    """Lazy import to avoid circular deps and heavy imports at module level."""
    from app.cex_spread_trader.exchanges.bybit_trader import BybitTrader
    from app.cex_spread_trader.exchanges.okx_trader import OkxTrader
    from app.cex_spread_trader.exchanges.gate_trader import GateTrader
    from app.cex_spread_trader.exchanges.bitget_trader import BitgetTrader
    from app.cex_spread_trader.exchanges.blofin_trader import BlofinTrader
    from app.cex_spread_trader.exchanges.kucoin_trader import KucoinTrader
    from app.cex_spread_trader.exchanges.htx_trader import HtxTrader
    from app.cex_spread_trader.exchanges.bingx_trader import BingxTrader
    from app.cex_spread_trader.exchanges.phemex_trader import PhemexTrader
    from app.cex_spread_trader.exchanges.bitmart_trader import BitmartTrader
    from app.cex_spread_trader.exchanges.coinex_trader import CoinexTrader
    from app.cex_spread_trader.exchanges.lbank_trader import LbankTrader
    from app.cex_spread_trader.exchanges.mexc_trader import MexcTrader

    return {
        "mexc": MexcTrader,
        "bybit": BybitTrader,
        "okx": OkxTrader,
        "gate": GateTrader,
        "bitget": BitgetTrader,
        "blofin": BlofinTrader,
        "kucoin": KucoinTrader,
        "htx": HtxTrader,
        "bingx": BingxTrader,
        "phemex": PhemexTrader,
        "bitmart": BitmartTrader,
        "coinex": CoinexTrader,
        "lbank": LbankTrader,
    }
