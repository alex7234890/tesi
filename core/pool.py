"""
Insurance pool — tracks balance, premiums, payouts and oracle rewards.
Computes the Solvency Ratio and dynamic margin Madj.
"""
from __future__ import annotations

from collections import deque
from typing import List


class InsurancePool:
    def __init__(self, config: dict) -> None:
        pool_cfg = config["pool"]
        self.balance_eth: float = pool_cfg["initial_balance_eth"]
        self.mbase: float       = pool_cfg["mbase"]
        self._sr_th             = pool_cfg["solvency_thresholds"]
        self._madj_v            = pool_cfg["madj"]

        self.total_premiums_eth: float      = 0.0
        self.total_payouts_eth: float       = 0.0
        self.total_oracle_rewards_eth: float = 0.0

        # Rolling 8-slot window (7 + current day)
        self._daily_payouts: deque  = deque(maxlen=8)
        self._daily_policies: deque = deque(maxlen=8)
        self._pending_claims: List[float] = []

        self._today_payout: float  = 0.0
        self._today_policies: int  = 0
        self._ever_insolvent: bool = False

    # ------------------------------------------------------------------
    def add_premium(self, amount: float) -> None:
        self.balance_eth += amount
        self.total_premiums_eth += amount

    def add_payout(self, amount: float) -> None:
        amount = min(amount, max(self.balance_eth, 0.0))
        self.balance_eth -= amount
        self.total_payouts_eth += amount
        self._today_payout += amount

    def add_oracle_reward(self, amount: float) -> None:
        amount = min(amount, max(self.balance_eth, 0.0))
        self.balance_eth -= amount
        self.total_oracle_rewards_eth += amount

    def register_policy(self) -> None:
        self._today_policies += 1

    def register_pending_claim(self, amount: float) -> None:
        self._pending_claims.append(amount)

    def resolve_pending_claim(self, amount: float) -> None:
        try:
            self._pending_claims.remove(amount)
        except ValueError:
            pass

    def end_of_day(self) -> None:
        self._daily_payouts.append(self._today_payout)
        self._daily_policies.append(self._today_policies)
        self._today_payout   = 0.0
        self._today_policies = 0
        if self.solvency_ratio() < 1.0:
            self._ever_insolvent = True

    # ------------------------------------------------------------------
    def _expected_claims_7d(self) -> float:
        if len(self._daily_payouts) < 2:
            return 0.0
        payout_24h     = self._daily_payouts[-1]
        policies_now   = self._daily_policies[-1] if self._daily_policies else 0
        policies_prev  = (
            self._daily_policies[-2]
            if len(self._daily_policies) >= 2
            else max(policies_now, 1)
        )
        if policies_prev == 0:
            return payout_24h * 7.0
        return payout_24h * (policies_now / policies_prev) * 7.0

    def solvency_ratio(self) -> float:
        denominator = sum(self._pending_claims) + self._expected_claims_7d()
        if denominator <= 0:
            return 999.0
        return self.balance_eth / denominator

    def get_madj(self) -> float:
        sr = self.solvency_ratio()
        if sr >= self._sr_th["medium_risk"]:   # >= 1.5
            return self._madj_v["healthy"]
        if sr >= self._sr_th["high_risk"]:     # >= 1.3
            return self._madj_v["medium_risk"]
        return self._madj_v["high_risk"]

    def get_m_total(self) -> float:
        return self.mbase + self.get_madj()

    @property
    def pending_liabilities_eth(self) -> float:
        return max(sum(self._pending_claims) + self._expected_claims_7d(), 0.0)

    @property
    def profit_eth(self) -> float:
        return (
            self.total_premiums_eth
            - self.total_payouts_eth
            - self.total_oracle_rewards_eth
        )

    @property
    def survived(self) -> bool:
        return not self._ever_insolvent
