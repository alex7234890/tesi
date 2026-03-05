"""
Oracle network simulation.

- Pool of honest/dishonest oracles with ETH stakes.
- N oracles selected per claim via simulated RANDAO.
- Honest oracles submit scores close to the true value (± noise).
- Dishonest oracles submit systematically biased scores.
- Median of submitted scores is the final decision score.
- Divergence tracking → watchlist → potential slashing.
- oracle_fraud_enabled=False: all oracles behave honestly, no slashing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np


@dataclass
class Oracle:
    oracle_id: str
    stake_eth: float
    is_honest: bool
    is_active: bool                = True
    divergences: int               = 0
    on_watchlist: bool             = False
    watchlist_entry_day: Optional[int] = None
    total_rewards_eth: float       = 0.0
    claims_processed: int          = 0
    total_divergence: float        = 0.0


class OracleNetwork:
    def __init__(self, config: dict, rng: np.random.Generator, logger) -> None:
        self.config  = config
        self.rng     = rng
        self.logger  = logger
        self._oc     = config["oracles"]
        self._wl     = self._oc["watchlist"]
        self._sl     = self._oc["slashing"]

        # When False: all oracles honest, no watchlist, no slashing
        self.fraud_enabled: bool = self._oc.get("fraud_enabled", True)

        n_total    = self._oc["initial_count"]
        n_honest   = int(n_total * self._oc["honest_rate"])
        self.oracles: List[Oracle] = []
        for i in range(n_total):
            stake = float(
                max(
                    self.rng.exponential(self._oc["stake_min_eth"] * 2.0),
                    self._oc["stake_min_eth"],
                )
            )
            self.oracles.append(
                Oracle(
                    oracle_id=f"oracle_{i:04d}",
                    stake_eth=stake,
                    is_honest=(i < n_honest),
                )
            )

        self.total_slashed_eth: float = 0.0
        self.n_slashed: int           = 0
        self._last_divergences: List[float] = []

    # ------------------------------------------------------------------
    def _select_oracles(self) -> List[Oracle]:
        active = [o for o in self.oracles if o.is_active]
        n      = min(self._oc["n_selected_per_claim"], len(active))
        idx    = self.rng.choice(len(active), size=n, replace=False)
        return [active[i] for i in idx]

    def get_oracle_scores(self, true_score: int) -> Tuple[List[int], List[Oracle]]:
        """
        Each selected oracle submits a fraud score.
        When fraud_enabled=False, all oracles behave honestly.
        Returns (list of scores, list of participating oracles).
        """
        selected = self._select_oracles()
        scores: List[int] = []
        participating: List[Oracle] = []

        for oracle in selected:
            if self.rng.random() > self._oc["availability_rate"]:
                continue
            # Dishonest behaviour only when fraud is enabled
            if self.fraud_enabled and not oracle.is_honest:
                if self.rng.random() < 0.5:
                    raw = self.rng.uniform(0, 40)
                else:
                    raw = self.rng.uniform(90, 130)
            else:
                raw = self.rng.normal(true_score, 5.0)
            score = int(np.clip(raw, 0, 130))
            scores.append(score)
            participating.append(oracle)
            oracle.claims_processed += 1

        return scores, participating

    def update_divergences(
        self,
        scores: List[int],
        oracles: List[Oracle],
        day: int,
    ) -> None:
        if not scores:
            return
        # When fraud is disabled, divergences are zero and no watchlist updates
        if not self.fraud_enabled:
            self._last_divergences = [0.0] * len(scores)
            return
        median = int(np.median(scores))
        self._last_divergences = []
        for oracle, score in zip(oracles, scores):
            div = abs(score - median)
            self._last_divergences.append(div)
            oracle.total_divergence += div

            if div >= self._oc["watchlist"]["divergence_threshold"]:
                oracle.divergences += 1
                if (
                    oracle.divergences >= self._oc["watchlist"]["entry_divergences"]
                    and not oracle.on_watchlist
                ):
                    oracle.on_watchlist = True
                    oracle.watchlist_entry_day = day
                    self.logger.info(
                        f"Oracle {oracle.oracle_id} added to watchlist (day {day})"
                    )

                if oracle.on_watchlist and div >= 20 and self.rng.random() < 0.10:
                    self._slash(oracle, day)

    def _slash(self, oracle: Oracle, day: int) -> None:
        slashed = self._sl["contestation_stake_eth"]
        slashed = min(slashed, oracle.stake_eth)
        oracle.stake_eth  -= slashed
        oracle.is_active   = slashed < oracle.stake_eth or oracle.stake_eth < 0.5
        self.total_slashed_eth += slashed
        self.n_slashed         += 1
        self.logger.info(
            f"Oracle {oracle.oracle_id} SLASHED {slashed:.4f} ETH (day {day})"
        )

    def distribute_daily_rewards(self, day: int) -> float:
        """Pay oracles and expire watchlist entries; return total paid."""
        total = 0.0
        persistence_days = self._oc["watchlist"]["persistence_months"] * 30
        watchlist_oracles = [o for o in self.oracles if o.on_watchlist]

        for oracle in self.oracles:
            if not oracle.is_active:
                continue

            if oracle.on_watchlist and oracle.watchlist_entry_day is not None:
                if day - oracle.watchlist_entry_day > persistence_days:
                    oracle.on_watchlist = False
                    oracle.divergences  = 0
                    self.logger.info(
                        f"Oracle {oracle.oracle_id} removed from watchlist (expired)"
                    )

            base = self._oc["reward_patt_update_eth"]
            if oracle.on_watchlist:
                pos = watchlist_oracles.index(oracle) if oracle in watchlist_oracles else 0
                penalty = 0.50 + (100 - min(pos, 100)) / 250.0
                reward  = base * penalty
            else:
                reward = base

            oracle.total_rewards_eth += reward
            total += reward

        return total

    # ------------------------------------------------------------------
    def get_metrics(self) -> dict:
        active    = [o for o in self.oracles if o.is_active]
        watchlist = [o for o in self.oracles if o.on_watchlist]
        rewards   = [o.total_rewards_eth for o in self.oracles if o.claims_processed > 0]
        avg_div   = float(np.mean(self._last_divergences)) if self._last_divergences else 0.0
        total_stake = sum(o.stake_eth for o in self.oracles if o.is_active)

        return {
            "n_oracles_active":         len(active),
            "n_oracles_watchlist":      len(watchlist),
            "n_oracles_slashed":        self.n_slashed,
            "avg_oracle_divergence":    avg_div,
            "avg_oracle_reward_eth":    float(np.mean(rewards)) if rewards else 0.0,
            "total_slashed_eth_cum":    self.total_slashed_eth,
            "total_oracle_stake_eth":   total_stake,
        }
