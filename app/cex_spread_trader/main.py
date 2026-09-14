"""Entrypoint for CEX spread trader service.

Usage:
    python -m app.cex_spread_trader.main
"""
import asyncio
import logging
import signal
import sys
from logging.handlers import RotatingFileHandler

from app.config.settings import Config
from app.cex_spread_trader.service import CexSpreadTraderService

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if Config.CEX_SPREAD_TRADER_LOG_FILE:
        handlers.append(
            RotatingFileHandler(
                Config.CEX_SPREAD_TRADER_LOG_FILE,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        level=getattr(logging, Config.CEX_SPREAD_TRADER_LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


_configure_logging()


async def main() -> None:
    Config.validate_cex_spread_trader_config()
    service = CexSpreadTraderService()
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("Shutdown signal received, stopping CEX spread trader...")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    task = asyncio.create_task(service.run())
    shutdown_wait = asyncio.create_task(shutdown_event.wait())
    done, _ = await asyncio.wait(
        [task, shutdown_wait], return_when=asyncio.FIRST_COMPLETED,
    )
    if shutdown_event.is_set():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    try:
        await service.redis.close()
    except Exception:
        pass
    logger.info("CEX spread trader stopped.")


if __name__ == "__main__":
    asyncio.run(main())
