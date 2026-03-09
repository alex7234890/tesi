"""
Metrics collector — records one row per simulation day.
Produces a list of dicts consumable by pandas / reporter / charts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.pool import InsurancePool
    from core.claim_processor import Claim
    from datasources.base import Swap
    from datasources.synthetic import UserState


class MetricsCollector:
    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        # Per-day swap detail records: day → list of swap detail dicts
        self.daily_swap_details: Dict[int, List[Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    def collect(
        self,
        day: int,
        pool: "InsurancePool",
        claims: List["Claim"],
        swaps: List["Swap"],
        patt: float,
        mode: int,
        users: Optional[Dict[str, "UserState"]] = None,
        premiums_today: float = 0.0,
        payouts_today: float = 0.0,
        pending_liabilities_eth: float = 0.0,
        swap_details: Optional[List[Dict[str, Any]]] = None,
        tint: float = 0.0,
        e: float = 0.20,
        vbase: float = 100.0,
        n_real_attacks: int = 0,
        n_fraud_attempts: int = 0,
        n_fraud_caught: int = 0,
        n_fraud_escaped: int = 0,
        avg_swap_value_eth: float = 0.0,
        payout_real_today: float = 0.0,
        payout_fraud_today: float = 0.0,
        oracle_cost_today: float = 0.0,
        n_oracles_used_today: int = 0,
        term1_today: float = 0.0,
        term2_today: float = 0.0,
        premium_rate_today: float = 0.0,
    ) -> None:
        n_claims   = len(claims)
        n_approved = n_claims  # all claims are approved

        avg_payout = (
            sum(c.payout_eth for c in claims) / n_claims if n_claims > 0 else 0.0
        )

        net_flow_today = premiums_today - payouts_today

        # Formula intermediate values
        loss_pct = pool.get_m_total()  # not ideal but we don't have it here; dashboard reads from config
        term1    = patt  # just store patt; term1 = patt * loss_pct computed in dashboard
        e_safe   = max(min(e, 0.9999), 0.0001)
        term2    = (tint * (e_safe / (1.0 - e_safe))) / (vbase * 1000.0)

        row: Dict[str, Any] = {
            # Tick
            "day": day,

            # Pool metrics
            "pool_balance_eth":             pool.balance_eth,
            "pending_liabilities_eth":      pending_liabilities_eth,
            "solvency_ratio":               pool.solvency_ratio(),
            "total_premiums_collected_eth": pool.total_premiums_eth,
            "total_payouts_eth":            pool.total_payouts_eth,
            "profit_eth":                   pool.profit_eth,
            "madj_current":                 pool.get_madj(),
            "m_total_current":              pool.get_m_total(),

            # Daily flows
            "premiums_today": premiums_today,
            "payouts_today":  payouts_today,
            "net_flow_today": net_flow_today,

            # Claim metrics
            "n_claims_submitted": n_claims,
            "n_claims_approved":  n_approved,
            "avg_payout_eth":     avg_payout,

            # Market metrics
            "patt_current":        patt,
            "n_swaps_this_tick":   len(swaps),
            "n_swaps_insured":     len(swaps),
            "n_attacks_this_tick": sum(1 for s in swaps if s.is_attacked),
            "avg_loss_eth": (
                sum(s.loss_eth for s in swaps if s.is_attacked)
                / max(sum(1 for s in swaps if s.is_attacked), 1)
            ),

            # Premium formula parameters (for Day-by-Day breakdown)
            "tint_today":         tint,
            "vbase_today":        vbase,
            "e_today":            e,
            "term2_today":        term2,

            # Fraud tracking
            "n_real_attacks":     n_real_attacks,
            "n_fraud_attempts":   n_fraud_attempts,
            "n_fraud_caught":     n_fraud_caught,
            "n_fraud_escaped":    n_fraud_escaped,
            "avg_swap_value_eth": avg_swap_value_eth,
            "payout_real_today":  payout_real_today,
            "payout_fraud_today": payout_fraud_today,

            # Oracle
            "oracle_cost_today":    oracle_cost_today,
            "n_oracles_used_today": n_oracles_used_today,

            # Formula terms (pre-computed)
            "term1_today":        term1_today,
            "term2_today":        term2_today,
            "premium_rate_today": premium_rate_today,
            "loss_pct_current":   pool.get_m_total(),  # reuse field (m_total stored separately)
        }

        # User metrics (mode 2 only)
        if mode == 2 and users is not None:
            row["n_users_active"] = len(users)
            claim_rates = [u.claim_rate for u in users.values()]
            row["avg_claim_rate"] = (
                sum(claim_rates) / len(claim_rates) if claim_rates else 0.0
            )
        else:
            row["n_users_active"] = 0
            row["avg_claim_rate"] = 0.0

        self.records.append(row)

        # Store per-swap details for Day-by-Day Explorer
        if swap_details is not None:
            self.daily_swap_details[day] = swap_details

    # ------------------------------------------------------------------
    def to_dataframe(self):
        import pandas as pd
        return pd.DataFrame(self.records)

    def summary(self, pool: "InsurancePool") -> Dict[str, Any]:
        import numpy as np
        df = self.to_dataframe()
        trend_slope = 0.0
        if len(df) > 1:
            balances = df["pool_balance_eth"].values.astype(float)
            days     = np.arange(len(balances), dtype=float)
            slope, _ = np.polyfit(days, balances, 1)
            trend_slope = float(slope)

        avg_prem_rate = float(df["premium_rate_today"].mean()) * 100.0 if "premium_rate_today" in df.columns else 0.0
        avg_term1     = float(df["term1_today"].mean()) if "term1_today" in df.columns else 0.0
        avg_term2     = float(df["term2_today"].mean()) if "term2_today" in df.columns else 0.0
        total_oracle  = float(df["oracle_cost_today"].sum()) if "oracle_cost_today" in df.columns else 0.0
        total_real_p  = float(df["payout_real_today"].sum()) if "payout_real_today" in df.columns else 0.0
        total_fraud_p = float(df["payout_fraud_today"].sum()) if "payout_fraud_today" in df.columns else 0.0

        return {
            "total_profit_eth":      pool.profit_eth,
            "final_balance_eth":     pool.balance_eth,
            "final_solvency_ratio":  pool.solvency_ratio(),
            "pool_survived":         pool.survived,
            "total_days":            len(df),
            "trend_slope":           trend_slope,
            "avg_premium_rate_pct":  avg_prem_rate,
            "avg_term1":             avg_term1,
            "avg_term2":             avg_term2,
            "total_oracle_cost_eth": total_oracle,
            "total_real_payouts_eth":  total_real_p,
            "total_fraud_payouts_eth": total_fraud_p,
        }
