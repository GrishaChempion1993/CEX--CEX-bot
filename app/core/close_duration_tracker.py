"""Track close durations for previously closed spreads."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CloseDurationTracker:
    """Persist close durations per MEXC symbol and compute historical averages."""

    def __init__(self, path: Path, max_records: int = 500):
        self.path = path
        self.max_records = max_records
        self._records: Dict[str, List[Tuple[int, float]]] = {}
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        return (symbol or "").strip().upper()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            symbols = data.get("symbols", {})
            if not isinstance(symbols, dict):
                return
            loaded: Dict[str, List[Tuple[int, float]]] = {}
            for symbol, records in symbols.items():
                if not isinstance(records, list):
                    continue
                cleaned: List[Tuple[int, float]] = []
                for item in records:
                    ts = item.get("ts") if isinstance(item, dict) else None
                    duration = item.get("duration_sec") if isinstance(item, dict) else None
                    if isinstance(ts, int) and isinstance(duration, (int, float)) and duration > 0:
                        cleaned.append((ts, float(duration)))
                if cleaned:
                    loaded[self._normalize_symbol(symbol)] = cleaned[-self.max_records :]
            self._records = loaded
        except Exception as exc:
            logger.warning("Failed to load close duration stats: %s", exc)

    def _save(self) -> None:
        try:
            data = {
                "symbols": {
                    symbol: [
                        {"ts": ts, "duration_sec": duration}
                        for ts, duration in records
                    ]
                    for symbol, records in self._records.items()
                }
            }
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as exc:
            logger.warning("Failed to save close duration stats: %s", exc)

    @staticmethod
    def _stats(records: List[Tuple[int, float]]) -> Tuple[Optional[float], int]:
        if not records:
            return None, 0
        avg = sum(duration for _, duration in records) / len(records)
        return avg, len(records)

    async def get_stats(self, symbol: str) -> Tuple[Optional[float], int]:
        normalized = self._normalize_symbol(symbol)
        if not normalized:
            return None, 0
        async with self._lock:
            return self._stats(self._records.get(normalized, []))

    async def add(self, symbol: str, duration_sec: float) -> Tuple[Optional[float], int]:
        normalized = self._normalize_symbol(symbol)
        if not normalized or duration_sec <= 0:
            return None, 0
        now = int(time.time())
        async with self._lock:
            records = self._records.setdefault(normalized, [])
            records.append((now, float(duration_sec)))
            if len(records) > self.max_records:
                self._records[normalized] = records[-self.max_records :]
                records = self._records[normalized]
            self._save()
            return self._stats(records)
