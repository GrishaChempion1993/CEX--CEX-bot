"""Token manager for MEXC web auth."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import os
import re
import struct
import time
from pathlib import Path
from typing import Optional

from app.config.settings import Config

logger = logging.getLogger(__name__)


class TokenManager:
    """Manage MEXC web token with optional browser-based refresh."""

    def __init__(self):
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    def _token_path(self) -> Optional[Path]:
        token_file = (Config.MEXC_WEB_TOKEN_FILE or "").strip()
        if token_file:
            return Path(token_file)
        return Config.DATA_DIR / "mexc_web_token.txt"

    def _storage_state_path(self) -> Path:
        return Config.DATA_DIR / "mexc_web_storage_state.json"

    def _find_playwright_chromium(self) -> Optional[str]:
        env_path = (os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or "").strip()
        if env_path and Path(env_path).exists():
            return env_path

        browser_roots = []
        env_browsers = (os.getenv("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
        if env_browsers and env_browsers != "0":
            browser_roots.append(Path(env_browsers))

        browser_roots.extend(
            [
                Path.home() / ".cache" / "ms-playwright",
            ]
        )

        seen: set[Path] = set()
        patterns = [
            "chromium-*/chrome-linux/chrome",
            "chromium-*/chrome-linux64/chrome",
            "chromium-*/chrome-win/chrome.exe",
            "chromium-*/chrome-win64/chrome.exe",
        ]
        for root in browser_roots:
            if root in seen or not root.exists():
                continue
            seen.add(root)
            for pattern in patterns:
                matches = sorted(root.glob(pattern), reverse=True)
                if matches:
                    return str(matches[0])
        return None

    def _load_from_file(self) -> Optional[str]:
        path = self._token_path()
        if not path:
            return None
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception:
            return None

    def _save_to_file(self, token: str) -> None:
        path = self._token_path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(token, encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save web token to file: %s", exc)

    @staticmethod
    def _normalize_token(token: str) -> str:
        value = (token or "").strip().strip("\"'")
        if value.lower().startswith("bearer "):
            value = value[7:].strip()
        return value

    def _load_bootstrap_token(self) -> str:
        token = self._normalize_token(self._load_from_file() or "")
        if token:
            return token
        return self._normalize_token(Config.MEXC_WEB_TOKEN)

    async def set_token(self, token: str, persist: bool = True) -> str:
        normalized = self._normalize_token(token)
        if not normalized or not normalized.startswith("WEB"):
            raise RuntimeError("MEXC web token must start with WEB")
        async with self._lock:
            self._token = normalized
        if persist:
            self._save_to_file(normalized)
        return normalized

    @staticmethod
    def _normalize_secret(secret: str) -> str:
        return re.sub(r"[\s-]+", "", (secret or "").strip()).upper()

    def _generate_totp_code(self) -> str:
        secret = self._normalize_secret(Config.MEXC_TOTP_SECRET)
        if not secret or secret == "BASE32SECRET":
            raise RuntimeError(
                "MEXC_TOTP_SECRET must contain the real Google Authenticator setup key, not a placeholder"
            )
        padding = "=" * ((8 - len(secret) % 8) % 8)
        try:
            key = base64.b32decode(secret + padding, casefold=True)
        except binascii.Error as exc:
            raise RuntimeError("MEXC_TOTP_SECRET is not a valid base32 TOTP secret") from exc
        counter = int(time.time() // 30)
        digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
        return f"{code_int:06d}"

    async def _first_visible(self, candidates: list) -> Optional[object]:
        for locator in candidates:
            try:
                if await locator.count() and await locator.first.is_visible():
                    return locator.first
            except Exception:
                continue
        return None

    async def _wait_for_visible(self, candidates: list, timeout_sec: float = 10.0) -> Optional[object]:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            locator = await self._first_visible(candidates)
            if locator:
                return locator
            await asyncio.sleep(0.25)
        return None

    async def _click_if_visible(self, candidates: list, timeout_sec: float = 3.0) -> bool:
        locator = await self._wait_for_visible(candidates, timeout_sec=timeout_sec)
        if not locator:
            return False
        await locator.click()
        return True

    async def _extract_token_from_page(self, page, captured: dict[str, str]) -> str:
        token = self._normalize_token(captured.get("token") or "")
        if token:
            return token
        keys = [k for k in Config.MEXC_TOKEN_STORAGE_KEYS if k]
        storage_token = await page.evaluate(
            """
            (keys) => {
              const stores = [window.localStorage, window.sessionStorage];
              for (const key of keys) {
                for (const store of stores) {
                  try {
                    const value = store.getItem(key);
                    if (value) {
                      return value;
                    }
                  } catch (err) {}
                }
              }
              return "";
            }
            """,
            keys,
        )
        return self._normalize_token(storage_token)

    async def _wait_for_token(self, page, captured: dict[str, str], timeout_sec: float) -> str:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            token = await self._extract_token_from_page(page, captured)
            if token:
                return token
            await asyncio.sleep(0.5)
        return ""

    async def _goto_login_page(self, page) -> None:
        for url in (
            (Config.MEXC_LOGIN_URL or "").strip(),
            "https://www.mexc.com/login",
            "https://www.mexc.com/ru-RU/login",
        ):
            if not url:
                continue
            await page.goto(url, wait_until="domcontentloaded")
            if "/404" not in page.url:
                return
        raise RuntimeError("Unable to open a valid MEXC login page")

    async def _perform_browser_login(self, page) -> None:
        if not Config.MEXC_LOGIN_EMAIL or not Config.MEXC_LOGIN_PASSWORD:
            raise RuntimeError("MEXC login email/password are required for auto-refresh")

        await self._goto_login_page(page)

        email_field = await self._wait_for_visible(
            [
                page.locator("input[autocomplete='username']"),
                page.locator("input[type='email']"),
                page.get_by_role("textbox", name=re.compile(r"email|почт|телефон|phone", re.I)),
                page.locator("input[type='text']"),
            ],
            timeout_sec=20.0,
        )
        if not email_field:
            raise RuntimeError("Could not find the MEXC login email field")
        await email_field.fill(Config.MEXC_LOGIN_EMAIL)

        await self._click_if_visible(
            [
                page.get_by_role("button", name=re.compile(r"next|continue|далее|продолж", re.I)),
            ],
            timeout_sec=5.0,
        )

        password_field = await self._wait_for_visible(
            [
                page.locator("input[type='password']"),
                page.locator("input[autocomplete='current-password']"),
            ],
            timeout_sec=20.0,
        )
        if not password_field:
            raise RuntimeError("Could not find the MEXC password field after the email step")
        await password_field.fill(Config.MEXC_LOGIN_PASSWORD)

        await self._click_if_visible(
            [
                page.get_by_role("button", name=re.compile(r"login|sign in|войти", re.I)),
                page.get_by_role("button", name=re.compile(r"next|continue|далее|продолж", re.I)),
            ],
            timeout_sec=5.0,
        )

        code = self._generate_totp_code()
        otp_field = await self._wait_for_visible(
            [
                page.locator("input[autocomplete='one-time-code']"),
                page.locator("input[name*='otp' i]"),
                page.locator("input[name*='code' i]"),
                page.locator("input[maxlength='6']"),
                page.locator("input[inputmode='numeric'][maxlength='6']"),
            ],
            timeout_sec=20.0,
        )
        if otp_field:
            await otp_field.fill(code)
        else:
            digit_inputs = page.locator("input[inputmode='numeric'], input[maxlength='1']")
            if await digit_inputs.count() < 6:
                raise RuntimeError("Could not find the MEXC 2FA input fields")
            for idx, digit in enumerate(code):
                field = digit_inputs.nth(idx)
                await field.fill(digit)

        await self._click_if_visible(
            [
                page.get_by_role("button", name=re.compile(r"confirm|verify|submit|подтверд|провер|войти", re.I)),
            ],
            timeout_sec=3.0,
        )

    async def _refresh_with_browser(self) -> str:
        from playwright.async_api import async_playwright

        storage_state_path = self._storage_state_path()
        storage_state = str(storage_state_path) if storage_state_path.exists() else None
        captured: dict[str, str] = {}

        async with async_playwright() as playwright:
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
                launch_args.append("--no-sandbox")
            launch_kwargs = {
                "headless": Config.MEXC_WEB_HEADLESS,
                "args": launch_args,
            }
            chromium_path = self._find_playwright_chromium()
            if chromium_path:
                launch_kwargs["executable_path"] = chromium_path
                logger.info("Using Playwright Chromium executable: %s", chromium_path)
            browser = await playwright.chromium.launch(**launch_kwargs)
            context_kwargs = {
                "ignore_https_errors": True,
                "locale": "en-US",
                "user_agent": Config.MEXC_WEB_USER_AGENT,
            }
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            context = await browser.new_context(**context_kwargs)
            page = await context.new_page()
            page.set_default_timeout(max(10_000, int(Config.MEXC_WEB_TIMEOUT_SEC * 1000)))

            def on_request(request) -> None:
                headers = request.headers
                for key, value in headers.items():
                    if key.lower() == "authorization" and value:
                        captured["token"] = value
                        break

            page.on("request", on_request)
            try:
                await page.goto("https://www.mexc.com/futures/BTC_USDT", wait_until="domcontentloaded")
                token = await self._wait_for_token(page, captured, timeout_sec=8.0)
                if not token:
                    await self._perform_browser_login(page)
                    await page.goto("https://www.mexc.com/futures/BTC_USDT", wait_until="domcontentloaded")
                    token = await self._wait_for_token(page, captured, timeout_sec=max(15.0, Config.MEXC_WEB_TIMEOUT_SEC))
                if not token:
                    raise RuntimeError("MEXC browser login completed but no authorization token was captured")
                storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(storage_state_path))
                return token
            finally:
                await context.close()
                await browser.close()

    async def get_token(self) -> str:
        async with self._lock:
            if self._token:
                return self._token
            token = self._load_bootstrap_token()
            if not token:
                token = ""
            if token:
                self._token = token
                return token
            # No token loaded — must refresh while still holding _lock
            # to prevent concurrent callers from also entering refresh.
            if not Config.MEXC_WEB_REFRESH_ENABLED:
                raise RuntimeError("MEXC web token is missing")
        return await self.refresh_token(force=True)

    async def refresh_token(self, force: bool = False) -> str:
        if not Config.MEXC_WEB_REFRESH_ENABLED:
            raise RuntimeError("MEXC web token expired and auto-refresh is disabled")
        async with self._refresh_lock:
            if not force:
                async with self._lock:
                    if self._token:
                        return self._token
            logger.info("Refreshing MEXC web session via Playwright")
            token = self._normalize_token(await self._refresh_with_browser())
            if not token:
                raise RuntimeError("MEXC auto-refresh did not return a token")
            self._save_to_file(token)
            async with self._lock:
                self._token = token
                logger.info("MEXC web session refresh succeeded")
                return token

    async def clear(self) -> None:
        async with self._lock:
            self._token = None
