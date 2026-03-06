"""
Oracle network — simplified to a flat cost per approved claim.
No watchlist, no slashing, no divergence tracking.
"""
from __future__ import annotations

import numpy as np


class OracleNetwork:
    def __init__(self, config: dict, rng: np.random.Generator, logger) -> None:
        self._oc              = config["oracles"]
        self.n_oracles        = self._oc["initial_count"]
        self.n_per_claim      = self._oc["n_selected_per_claim"]
        self.reward_per_claim = self._oc["reward_patt_update_eth"]
        self.total_rewards_eth: float = 0.0

    def compute_daily_cost(self, n_claims: int) -> float:
        """Return total oracle cost for today's claims and accumulate."""
        cost = n_claims * self.reward_per_claim * self.n_per_claim
        self.total_rewards_eth += cost
        return cost

    def get_metrics(self) -> dict:
        return {
            "n_oracles_active":      self.n_oracles,
            "avg_oracle_reward_eth": self.reward_per_claim,
        }
