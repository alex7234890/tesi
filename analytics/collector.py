"""
Metrics collector — records one row per simulation day.
Produces a list of dicts consumable by pandas / reporter / charts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.pool import InsurancePool
    from core.claim_processor import Claim
    from core.oracle_network import OracleNetwork
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
        oracle_network: "OracleNetwork",
        patt: float,
        mode: int,
        users: Optional[Dict[str, "UserState"]] = None,
        n_upgrades: int = 0,
        premiums_today: float = 0.0,
        payouts_today: float = 0.0,
        oracle_rewards_today: float = 0.0,
        pending_liabilities_eth: float = 0.0,
        swap_details: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        n_approved = sum(1 for c in claims if c.decision == "approved")
        n_rejected = sum(1 for c in claims if c.decision == "rejected")
        n_captcha  = len(claims) - n_approved - n_rejected
        n_claims   = len(claims)

        avg_payout = (
            sum(c.payout_eth for c in claims if c.decision == "approved") / n_approved
            if n_approved > 0 else 0.0
        )
        avg_fs = (
            sum(c.final_score for c in claims) / n_claims if n_claims > 0 else 0.0
        )

        # Rejection reason breakdown
        n_fraud_gt_80      = sum(
            1 for c in claims
            if c.decision == "rejected" and c.rejection_reason == "fraud_score_gt_80"
        )
        n_pattern_invalid  = sum(
            1 for c in claims
            if c.decision == "rejected" and c.rejection_reason == "pattern_invalid"
        )
        n_captcha_failed   = sum(
            1 for c in claims
            if c.decision == "rejected" and c.rejection_reason == "captcha_failed"
        )

        # Avg fraud score split by outcome
        approved_scores = [c.final_score for c in claims if c.decision == "approved"]
        rejected_scores = [c.final_score for c in claims if c.decision == "rejected"]
        avg_fs_approved = sum(approved_scores) / len(approved_scores) if approved_scores else 0.0
        avg_fs_rejected = sum(rejected_scores) / len(rejected_scores) if rejected_scores else 0.0

        net_flow_today = premiums_today - payouts_today - oracle_rewards_today

        row: Dict[str, Any] = {
            # Tick
            "day": day,

            # Pool metrics
            "pool_balance_eth":             pool.balance_eth,
            "pending_liabilities_eth":      pending_liabilities_eth,
            "solvency_ratio":               pool.solvency_ratio(),
            "total_premiums_collected_eth": pool.total_premiums_eth,
            "total_payouts_eth":            pool.total_payouts_eth,
            "total_oracle_rewards_eth":     pool.total_oracle_rewards_eth,
            "profit_eth":                   pool.profit_eth,
            "madj_current":                 pool.get_madj(),
            "m_total_current":              pool.get_m_total(),

            # Daily flows
            "premiums_today":        premiums_today,
            "payouts_today":         payouts_today,
            "oracle_rewards_today":  oracle_rewards_today,
            "net_flow_today":        net_flow_today,

            # Claim metrics
            "n_claims_submitted":  n_claims,
            "n_claims_approved":   n_approved,
            "n_claims_rejected":   n_rejected,
            "n_claims_captcha":    n_captcha,
            "claim_approval_rate": n_approved / n_claims if n_claims > 0 else 0.0,
            "avg_payout_eth":      avg_payout,
            "avg_fraud_score":     avg_fs,
            "avg_fraud_score_approved": avg_fs_approved,
            "avg_fraud_score_rejected": avg_fs_rejected,

            # Rejection reason breakdown
            "n_rejected_fraud_score_gt_80": n_fraud_gt_80,
            "n_rejected_pattern_invalid":   n_pattern_invalid,
            "n_rejected_captcha_failed":    n_captcha_failed,

            # Market metrics
            "patt_current":        patt,
            "n_swaps_this_tick":   len(swaps),
            "n_swaps_insured":     len(swaps),
            "n_attacks_this_tick": sum(1 for s in swaps if s.is_attacked),
            "avg_loss_eth": (
                sum(s.loss_eth for s in swaps if s.is_attacked)
                / max(sum(1 for s in swaps if s.is_attacked), 1)
            ),
        }

        # Oracle metrics
        oracle_metrics = oracle_network.get_metrics()
        row.update(oracle_metrics)

        # User metrics (mode 2 only)
        if mode == 2 and users is not None:
            active = {uid: u for uid, u in users.items() if not u.is_blacklisted}
            row["n_users_bronze"]       = sum(1 for u in active.values() if u.tier == "bronze")
            row["n_users_silver"]       = sum(1 for u in active.values() if u.tier == "silver")
            row["n_users_gold"]         = sum(1 for u in active.values() if u.tier == "gold")
            row["n_users_platinum"]     = sum(1 for u in active.values() if u.tier == "platinum")
            row["n_users_blacklisted"]  = sum(1 for u in users.values() if u.is_blacklisted)
            row["n_upgrades_this_tick"] = n_upgrades
            claim_rates = [u.claim_rate for u in active.values()]
            row["avg_claim_rate"] = (
                sum(claim_rates) / len(claim_rates) if claim_rates else 0.0
            )
        else:
            row["n_users_bronze"]       = 0
            row["n_users_silver"]       = 0
            row["n_users_gold"]         = 0
            row["n_users_platinum"]     = 0
            row["n_users_blacklisted"]  = 0
            row["n_upgrades_this_tick"] = 0
            row["avg_claim_rate"]       = 0.0

        self.records.append(row)

        # Store per-swap details for Day-by-Day Explorer
        if swap_details is not None:
            self.daily_swap_details[day] = swap_details

    # ------------------------------------------------------------------
    def to_dataframe(self):
        import pandas as pd
        return pd.DataFrame(self.records)

    def summary(self, pool: "InsurancePool") -> Dict[str, Any]:
        df = self.to_dataframe()
        return {
            "total_profit_eth":     pool.profit_eth,
            "final_balance_eth":    pool.balance_eth,
            "final_solvency_ratio": pool.solvency_ratio(),
            "claim_approval_rate":  (
                df["claim_approval_rate"].mean() if not df.empty else 0.0
            ),
            "pool_survived":        pool.survived,
            "total_days":           len(df),
        }
