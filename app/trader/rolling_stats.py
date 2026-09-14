"""Rolling stats tracker for trade PnL."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


class RollingStatsTracker:
    """Track numeric values over time and compute rolling sums."""

    def __init__(self, path: Path, max_records: int = 200000):
        self.path = path
        self.max_records = max_records
        self._series: Dict[str, List[Tuple[int, float]]] = {}
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            series = data.get("series")
            if not isinstance(series, dict):
                return
            loaded: Dict[str, List[Tuple[int, float]]] = {}
            for name, records in series.items():
                if not isinstance(records, list):
                    continue
                cleaned = []
                for item in records:
                    ts = item.get("ts") if isinstance(item, dict) else None
                    val = item.get("value") if isinstance(item, dict) else None
                    if isinstance(ts, int) and isinstance(val, (int, float)):
                        cleaned.append((ts, float(val)))
                if cleaned:
                    loaded[name] = cleaned[-self.max_records :]
            self._series = loaded
        except Exception as exc:
            logger.warning("Failed to load trade stats: %s", exc)

    def _save(self) -> None:
        try:
            data = {
                "series": {
                    name: [{"ts": ts, "value": val} for ts, val in records]
                    for name, records in self._series.items()
                }
            }
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            tmp.replace(self.path)
        except Exception as exc:
            logger.warning("Failed to save trade stats: %s", exc)

    async def _save_async(self) -> None:
        """Non-blocking save via thread pool to avoid blocking the event loop."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save)

    def _compute_stats(self, records: List[Tuple[int, float]], now: int) -> Dict[str, float]:
        day_cutoff = now - 86400
        week_cutoff = now - 7 * 86400
        val_24h = 0.0
        val_week = 0.0
        val_all = 0.0
        for ts, val in records:
            val_all += val
            if ts >= week_cutoff:
                val_week += val
            if ts >= day_cutoff:
                val_24h += val
        return {
            "val_24h": val_24h,
            "val_week": val_week,
            "val_all": val_all,
        }

    async def get_stats(self, names: list[str] | None = None) -> Dict[str, Dict[str, float]]:
        """Get current stats without adding new records."""
        now = int(time.time())
        async with self._lock:
            if names is None:
                names = list(self._series.keys())
            return {
                name: self._compute_stats(self._series.get(name, []), now)
                for name in names
            }

    async def add_and_get_stats(self, updates: Dict[str, float]) -> Dict[str, Dict[str, float]]:
        now = int(time.time())
        async with self._lock:
            for name, value in updates.items():
                records = self._series.setdefault(name, [])
                records.append((now, float(value)))
                if len(records) > self.max_records:
                    self._series[name] = records[-self.max_records :]
            await self._save_async()
            return {
                name: self._compute_stats(self._series.get(name, []), now)
                for name in updates.keys()
            }
