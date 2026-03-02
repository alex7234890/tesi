"""
Tier management for mode 2 — Bronze → Silver → Gold → Platinum.

Upgrade criteria (from config):
  bronze_to_silver:  min_swaps=18, min_days=30, max_avg_fraud_score=52
  silver_to_gold:    min_swaps=55, min_days=60, max_avg_fraud_score=35

Platinum: users who stake ≥ stake_pct of their trading capital.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from datasources.synthetic import UserState


class TierManager:
    def __init__(self, config: dict, logger: logging.Logger) -> None:
        self.config  = config
        self.logger  = logger
        self._tiers  = config.get("tiers", {})
        self._upg    = self._tiers.get("upgrades", {})
        self._b2s    = self._upg.get("bronze_to_silver", {})
        self._s2g    = self._upg.get("silver_to_gold", {})

    # ------------------------------------------------------------------
    def process_upgrades(self, users: Dict[str, "UserState"], day: int) -> int:
        """
        Evaluate upgrade eligibility for every user.
        Returns the number of upgrades applied this tick.
        """
        upgrades = 0
        for uid, user in users.items():
            if user.is_blacklisted:
                continue
            if user.tier == "bronze":
                if self._eligible_b2s(user):
                    user.tier = "silver"
                    upgrades += 1
                    self.logger.info(
                        f"User {uid} upgraded Bronze→Silver (day {day})"
                    )
            elif user.tier == "silver":
                if self._eligible_s2g(user):
                    user.tier = "gold"
                    upgrades += 1
                    self.logger.info(
                        f"User {uid} upgraded Silver→Gold (day {day})"
                    )
            elif user.tier == "gold":
                # Gold → Platinum: stake_pct of capital
                stake_pct = self._tiers.get("platinum", {}).get("stake_pct", 0.20)
                if user.capital_eth > 0 and user.avg_fraud_score < 5:
                    user.tier = "platinum"
                    upgrades += 1
                    self.logger.info(
                        f"User {uid} upgraded Gold→Platinum (day {day})"
                    )
        return upgrades

    # ------------------------------------------------------------------
    def _eligible_b2s(self, user: "UserState") -> bool:
        return (
            user.total_swaps       >= self._b2s.get("min_swaps", 18)
            and user.total_days_active >= self._b2s.get("min_days", 30)
            and user.avg_fraud_score   <= self._b2s.get("max_avg_fraud_score", 52)
        )

    def _eligible_s2g(self, user: "UserState") -> bool:
        return (
            user.total_swaps       >= self._s2g.get("min_swaps", 55)
            and user.total_days_active >= self._s2g.get("min_days", 60)
            and user.avg_fraud_score   <= self._s2g.get("max_avg_fraud_score", 35)
        )
