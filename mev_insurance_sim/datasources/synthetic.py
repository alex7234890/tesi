"""
Mode 2 datasource — fully synthetic swaps with a real Patt baseline loaded
from SQLite (table patt_history).  All users are generated with Bronze tier
and advance through Bronze → Silver → Gold → Platinum over time.
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .base import BaseDataSource, Swap


# ---------------------------------------------------------------------------
# Internal user state
# ---------------------------------------------------------------------------

@dataclass
class UserState:
    user_id: str
    tier: str                = "bronze"
    is_fraudulent: bool      = False
    total_swaps: int         = 0
    total_claims: int        = 0
    total_days_active: int   = 0
    is_blacklisted: bool     = False
    avg_fraud_score: float   = 0.0
    fraud_score_history: List[int] = field(default_factory=list)
    daily_swaps_today: int   = 0
    capital_eth: float       = 0.0  # Platinum stake

    @property
    def claim_rate(self) -> float:
        if self.total_swaps == 0:
            return 0.0
        return self.total_claims / self.total_swaps


# ---------------------------------------------------------------------------
# Datasource
# ---------------------------------------------------------------------------

class SyntheticDataSource(BaseDataSource):
    def __init__(
        self,
        config: dict,
        db_path: str,
        rng: np.random.Generator,
    ) -> None:
        super().__init__(config, db_path, rng)
        self.duration_days: int = config["simulation"]["duration_days"]

        mc = config["market"]
        self.loss_pct_mean: float = mc["loss_pct_mean"]
        self.loss_pct_std: float  = mc["loss_pct_std"]
        self.patt_osc: float      = mc["patt_oscillation_range"]

        uc = config["users"]
        self.initial_count: int        = uc["initial_count"]
        self.growth_rate_daily: float  = uc["growth_rate_daily"]
        self.fraud_rate: float         = uc["fraud_rate"]
        self.swap_freq_mean: float     = uc["swap_frequency_mean"]
        self.coverage_dist: dict       = uc["coverage_distribution"]

        self._patt_history: List[float] = self._load_patt_history()
        self._users: Dict[str, UserState] = {}
        self._next_user_id: int = 0
        self._spawn_users(self.initial_count)

    # ------------------------------------------------------------------
    # Patt loading
    # ------------------------------------------------------------------

    def _load_patt_history(self) -> List[float]:
        try:
            con = sqlite3.connect(self.db_path, check_same_thread=False)
            rows = con.execute(
                "SELECT patt FROM patt_history ORDER BY date"
            ).fetchall()
            con.close()
            if rows:
                return [float(r[0]) for r in rows]
        except Exception:
            pass
        return [0.01] * self.duration_days

    def get_patt(self, day: int) -> float:
        idx = day % len(self._patt_history)
        base = self._patt_history[idx]
        delta = self.rng.uniform(-self.patt_osc, self.patt_osc)
        return float(np.clip(base + delta, 0.001, 0.5))

    # ------------------------------------------------------------------
    # User management
    # ------------------------------------------------------------------

    def _spawn_users(self, n: int) -> None:
        for _ in range(n):
            uid = f"user_{self._next_user_id:06d}"
            self._next_user_id += 1
            is_fraud = self.rng.random() < self.fraud_rate
            self._users[uid] = UserState(user_id=uid, is_fraudulent=is_fraud)

    def _grow_users(self) -> None:
        n_new = max(1, int(len(self._users) * self.growth_rate_daily))
        self._spawn_users(n_new)

    # ------------------------------------------------------------------
    # Coverage assignment
    # ------------------------------------------------------------------

    def _pick_coverage(self) -> str:
        r = self.rng.random()
        if r < self.coverage_dist["low"]:
            return "low"
        elif r < self.coverage_dist["low"] + self.coverage_dist["medium"]:
            return "medium"
        return "high"

    # ------------------------------------------------------------------
    # Daily swaps generation
    # ------------------------------------------------------------------

    def get_daily_swaps(self, day: int) -> List[Swap]:
        # Grow user base
        if day > 0:
            self._grow_users()

        patt = self.get_patt(day)
        swaps: List[Swap] = []

        # Reset daily swap counter
        for u in self._users.values():
            u.daily_swaps_today = 0
            if not u.is_blacklisted:
                u.total_days_active += 1

        for uid, user in list(self._users.items()):
            if user.is_blacklisted:
                continue

            tier_limits = self.config.get("tiers", {})
            tier_cfg = tier_limits.get(user.tier, {})
            max_daily = int(tier_cfg.get("max_daily_swaps", 99))

            n_swaps = int(self.rng.poisson(self.swap_freq_mean))
            n_swaps = min(n_swaps, max_daily)

            for i in range(n_swaps):
                value_eth = float(self.rng.lognormal(mean=0.4, sigma=0.8))
                is_attacked = self.rng.random() < patt

                if is_attacked:
                    loss_pct = float(
                        np.clip(
                            self.rng.normal(self.loss_pct_mean, self.loss_pct_std),
                            0.0,
                            1.0,
                        )
                    )
                    loss_eth = value_eth * loss_pct
                else:
                    loss_eth = 0.0

                coverage = self._pick_coverage()
                tx = f"0x{uuid.uuid4().hex}"

                swaps.append(
                    Swap(
                        timestamp=day * 86400 + i,
                        value_eth=value_eth,
                        is_attacked=is_attacked,
                        loss_eth=loss_eth,
                        coverage=coverage,
                        user_id=uid,
                        user_tier=user.tier,
                        tx_hash=tx,
                    )
                )
                user.total_swaps += 1
                user.daily_swaps_today += 1

        return swaps

    def get_duration_days(self) -> int:
        return self.duration_days

    # ------------------------------------------------------------------
    # Expose user states (used by runner and tier manager)
    # ------------------------------------------------------------------

    @property
    def users(self) -> Dict[str, UserState]:
        return self._users
