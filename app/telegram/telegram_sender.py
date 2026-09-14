"""Telegram Bot API sender"""
import html
import logging
from typing import Optional
import aiohttp
from app.config.settings import Config
from app.core.models import SpreadSignal

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org"


class TelegramSender:
    """Send messages via Telegram Bot API"""
    
    def __init__(self, bot_token: str = None, chat_id: str = None):
        self.bot_token = bot_token or Config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or Config.TELEGRAM_CHAT_ID
        self.timeout = aiohttp.ClientTimeout(total=Config.HTTP_TIMEOUT_SEC)
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def _get_session(self):
        """Get or create session"""
        if not self.session:
            self.session = aiohttp.ClientSession(timeout=self.timeout)
        return self.session

    @staticmethod
    def _format_duration(seconds: Optional[float]) -> str:
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

    def _chat_id_for_signal(self, signal: SpreadSignal) -> str:
        spread = float(signal.spread_pct or 0.0)
        if (
            Config.TELEGRAM_MID_SPREAD_CHAT_ID
            and Config.TELEGRAM_MID_SPREAD_MIN_PCT <= spread <= Config.TELEGRAM_MID_SPREAD_MAX_PCT
        ):
            return Config.TELEGRAM_MID_SPREAD_CHAT_ID
        return self.chat_id
    
    def _format_signal_message(self, signal: SpreadSignal) -> str:
        """Format spread signal as HTML message"""
        # Escape HTML entities
        token_symbol = html.escape(signal.token_symbol)
        chain = html.escape(signal.chain)
        contract = html.escape(signal.contract)
        mexc_symbol = html.escape(signal.mexc_symbol)
        
        origin = signal.origin_label or ""
        if not origin and (signal.dex_change_24h_pct is not None or signal.mexc_change_24h_pct is not None):
            def fmt(val):
                return f"{val:+.1f}%" if val is not None else "n/a"
            origin = f"M: {fmt(signal.mexc_change_24h_pct)} VS D: {fmt(signal.dex_change_24h_pct)}"
        
        max_size_line = ""
        if signal.max_size_tokens and signal.max_size_usd:
            max_size_line = (
                f"<b>⚖️ Max Size:</b> {signal.max_size_tokens:,.0f} ${token_symbol} "
                f"(${signal.max_size_usd:,.0f})"
            )
        
        ts_note = ""
        if signal.dex_ts_missing or signal.dex_ts_stale:
            ts_note = "<i>Note: DEX price timestamp missing/stale (used current price)</i>"

        funding_line = ""
        if signal.funding_rate is not None:
            rate_pct = signal.funding_rate * 100
            funding_line = f"<b>Funding:</b> {rate_pct:+.4f}%"
            if signal.funding_time_utc:
                funding_line += (
                    f" (next: {signal.funding_time_utc.strftime('%Y-%m-%d %H:%M:%S UTC')})"
                )

        direction_raw = (signal.direction or "").lower().strip()
        if direction_raw == "long":
            direction_label = "SELL DEX / LONG MEXC"
        elif direction_raw == "short":
            direction_label = "BUY DEX / SHORT MEXC"
        else:
            direction_label = signal.direction

        # Build message
        lines = [
            f"<b>🚀 {direction_label}</b>",
            origin,
            "",
            f"<b>Token:</b> {token_symbol} ({mexc_symbol})",
            f"<b>Chain:</b> {chain}",
            f"<b>Contract:</b> <code>{contract}</code>",
            "",
            f"<b>Spread:</b> {signal.spread_pct:.2f}%",
            "",
            f"<b>DEX Price:</b> ${signal.dex_price:.6f}",
            f"<b>MEXC Price:</b> ${signal.mexc_price:.6f}",
            f"<b>DEX Liquidity:</b> ${signal.dex_liquidity_usd:,.0f}",
            f"<b>DEX 24h Volume:</b> ${signal.dex_volume_24h_usd:,.0f}",
        ]

        if funding_line:
            lines.append(funding_line)
        if max_size_line:
            lines.append(max_size_line)
        if signal.avg_close_sec is not None:
            avg_close_line = f"<b>Avg Close:</b> {self._format_duration(signal.avg_close_sec)}"
            if signal.avg_close_count:
                avg_close_line += f" (n={signal.avg_close_count})"
            lines.append(avg_close_line)
        lines.append("")
        
        if signal.mexc_volume_24h_usd:
            lines.append(f"<b>MEXC 24h Volume:</b> ${signal.mexc_volume_24h_usd:,.0f}")
        
        if signal.confirmations:
            lines.append(f"<b>Confirmations:</b> {signal.confirmations}")
        if ts_note:
            lines.append(ts_note)
        
        lines.extend([
            "",
            f"<b>Links:</b>",
            f"• <a href=\"{signal.dex_url}\">DexScreener</a>",
            f"• <a href=\"{signal.mexc_url}\">MEXC</a>",
            "",
            f"<i>Time: {signal.timestamp_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}</i>"
        ])
        
        return "\n".join(lines)
    
    async def send_message(
        self,
        text: str,
        reply_to_message_id: Optional[int] = None,
        chat_id: Optional[str] = None,
    ) -> Optional[int]:
        """
        Send plain text message to Telegram.
        Returns message_id if successful, None otherwise.
        """
        if not self.bot_token or not self.chat_id:
            logger.error("Telegram bot token or chat ID not configured")
            return None
        
        session = await self._get_session()
        url = f"{TELEGRAM_API_URL}/bot{self.bot_token}/sendMessage"
        
        payload = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        
        try:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    message_id = None
                    if isinstance(data, dict):
                        message_id = data.get("result", {}).get("message_id")
                    logger.info("Telegram message sent successfully")
                    return message_id
                error_text = await response.text()
                logger.error(f"Telegram API error {response.status}: {error_text}")
                return None
        
        except aiohttp.ClientError as e:
            logger.error(f"Telegram API request failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending Telegram message: {e}")
            return None
    
    async def send_signal(self, signal: SpreadSignal) -> bool:
        """Send formatted spread signal to Telegram"""
        message = self._format_signal_message(signal)
        chat_id = self._chat_id_for_signal(signal)
        msg_id = await self.send_message(message, chat_id=chat_id)
        if msg_id:
            signal.message_id = msg_id
            signal.telegram_chat_id = chat_id
            return True
        return False

    async def edit_message(self, message_id: int, text: str, chat_id: Optional[str] = None) -> bool:
        """Edit an existing message."""
        if not self.bot_token or not self.chat_id:
            logger.error("Telegram bot token or chat ID not configured")
            return False
        session = await self._get_session()
        url = f"{TELEGRAM_API_URL}/bot{self.bot_token}/editMessageText"
        payload = {
            "chat_id": chat_id or self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        try:
            async with session.post(url, json=payload) as response:
                if response.status == 200:
                    return True
                error_text = await response.text()
                logger.error(f"Telegram edit error {response.status}: {error_text}")
                return False
        except Exception as e:
            logger.error(f"Unexpected error editing Telegram message: {e}")
            return False

    async def get_updates(self, offset: Optional[int] = None, timeout: int = 0) -> list:
        """Fetch updates for command polling."""
        if not self.bot_token:
            return []
        session = await self._get_session()
        url = f"{TELEGRAM_API_URL}/bot{self.bot_token}/getUpdates"
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    return []
                data = await response.json()
                if isinstance(data, dict):
                    return data.get("result", []) or []
        except Exception:
            return []
        return []


