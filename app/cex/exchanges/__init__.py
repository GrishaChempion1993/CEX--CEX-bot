"""Multi-exchange futures clients."""
from app.cex.exchanges.bybit_client import BybitClient
from app.cex.exchanges.okx_client import OKXClient
from app.cex.exchanges.gate_client import GateClient
from app.cex.exchanges.bitget_client import BitgetClient
from app.cex.exchanges.blofin_client import BloFinClient
from app.cex.exchanges.kucoin_client import KuCoinClient
from app.cex.exchanges.htx_client import HTXClient
from app.cex.exchanges.bingx_client import BingXClient
from app.cex.exchanges.phemex_client import PhemexClient
from app.cex.exchanges.bitmart_client import BitMartClient
from app.cex.exchanges.coinex_client import CoinExClient
from app.cex.exchanges.lbank_client import LBankClient

ALL_EXCHANGES = {
    "bybit": BybitClient,
    "okx": OKXClient,
    "gate": GateClient,
    "bitget": BitgetClient,
    "blofin": BloFinClient,
    "kucoin": KuCoinClient,
    "htx": HTXClient,
    "bingx": BingXClient,
    "phemex": PhemexClient,
    "bitmart": BitMartClient,
    "coinex": CoinExClient,
    "lbank": LBankClient,
}

__all__ = [
    "ALL_EXCHANGES",
    "BybitClient", "OKXClient", "GateClient", "BitgetClient",
    "BloFinClient", "KuCoinClient", "HTXClient", "BingXClient",
    "PhemexClient", "BitMartClient", "CoinExClient", "LBankClient",
]
