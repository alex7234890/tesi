"""
Mode 1 datasource — reads real blockchain swaps from SQLite, randomly assigns
insurance and coverage level.  The data is grouped into calendar days; if the
configured duration_days exceeds the available real days the data cycles.
"""
from __future__ import annotations

import sqlite3
import uuid
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

from .base import BaseDataSource, Swap


class BlockchainDataSource(BaseDataSource):
    def __init__(
        self,
        config: dict,
        db_path: str,
        rng: np.random.Generator,
        coverage: str = "high",
    ) -> None:
        super().__init__(config, db_path, rng)
        self.coverage = coverage.lower()
        self.insurance_rate: float = config["market"]["insurance_rate"]
        self.duration_days: int = config["simulation"]["duration_days"]

        self._days: List[List[dict]] = []      # insured swaps per real day
        self._patt_per_day: List[float] = []   # Patt per real day
        self._load_data()

    # ------------------------------------------------------------------
    def _load_data(self) -> None:
        con = sqlite3.connect(self.db_path, check_same_thread=False)
        con.row_factory = sqlite3.Row

        swaps_by_day: Dict[int, List[sqlite3.Row]] = defaultdict(list)
        attacks_hashes: set = set()

        try:
            rows = con.execute(
                "SELECT block_number, tx_hash, timestamp, value_eth, is_attacked, loss_eth "
                "FROM swaps ORDER BY timestamp"
            ).fetchall()
        except Exception:
            rows = []

        try:
            attack_rows = con.execute("SELECT victim_hash FROM sandwich_attacks").fetchall()
            attacks_hashes = {r["victim_hash"] for r in attack_rows}
        except Exception:
            pass

        for r in rows:
            day_idx = int(r["timestamp"]) // 86400
            swaps_by_day[day_idx].append(dict(r))

        con.close()

        if not swaps_by_day:
            # No data downloaded yet — create a single synthetic stub day
            self._days = [self._stub_day()]
            self._patt_per_day = [0.01]
            return

        sorted_days = sorted(swaps_by_day.keys())
        for day_key in sorted_days:
            raw = swaps_by_day[day_key]
            total = len(raw)
            attacked = sum(1 for r in raw if r["is_attacked"])
            patt = attacked / total if total > 0 else 0.01
            self._patt_per_day.append(patt)

            # Randomly pick insured swaps
            insured_rows = [
                r for r in raw if self.rng.random() < self.insurance_rate
            ]
            insured_swaps = []
            for r in insured_rows:
                insured_swaps.append(
                    dict(
                        tx_hash=r["tx_hash"],
                        value_eth=float(r["value_eth"]),
                        is_attacked=bool(r["is_attacked"]),
                        loss_eth=float(r["loss_eth"]),
                        timestamp=int(r["timestamp"]),
                        user_id=f"addr_{r['tx_hash'][:10]}",
                    )
                )
            self._days.append(insured_swaps)

    def _stub_day(self) -> List[dict]:
        """Fallback stub if no SQLite data is available."""
        swaps = []
        for i in range(100):
            val = float(self.rng.lognormal(mean=0.4, sigma=0.8))
            attacked = self.rng.random() < 0.01
            loss = val * 0.20 if attacked else 0.0
            swaps.append(
                dict(
                    tx_hash=str(uuid.uuid4()),
                    value_eth=val,
                    is_attacked=attacked,
                    loss_eth=loss,
                    timestamp=0,
                    user_id=f"addr_{i:06d}",
                )
            )
        return swaps

    # ------------------------------------------------------------------
    def get_daily_swaps(self, day: int) -> List[Swap]:
        real_day = day % len(self._days)
        raw = self._days[real_day]
        swaps = []
        for r in raw:
            swaps.append(
                Swap(
                    timestamp=r["timestamp"],
                    value_eth=r["value_eth"],
                    is_attacked=r["is_attacked"],
                    loss_eth=r["loss_eth"],
                    coverage=self.coverage,
                    user_id=r["user_id"],
                    user_tier=None,   # mode 1: no tiers
                    tx_hash=r["tx_hash"],
                )
            )
        return swaps

    def get_patt(self, day: int) -> float:
        if not self._patt_per_day:
            return 0.01
        return self._patt_per_day[day % len(self._patt_per_day)]

    def get_duration_days(self) -> int:
        return self.duration_days
