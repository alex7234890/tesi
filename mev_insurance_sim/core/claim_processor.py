"""
Claim processing pipeline.

For each attacked insured swap:
  1. FraudDetector computes a raw fraud score.
  2. OracleNetwork collects scores; median is the final score.
  3. Decision: approved / captcha / rejected.
  4. Captcha outcome is simulated (honest users pass, fraudulent users mostly fail).
  5. Pool is debited for approved claims.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from .fraud_detector import FraudDetector
from .oracle_network import OracleNetwork
from .pool import InsurancePool
from datasources.base import Swap

# Payout multipliers per coverage level (mode 2 default)
_COVERAGE_PAYOUT = {"low": 0.50, "medium": 0.70, "high": 1.00}


@dataclass
class Claim:
    swap_tx_hash: str
    user_id: str
    user_tier: Optional[str]
    coverage: str
    loss_eth: float
    fraud_score: int
    oracle_scores: List[int]
    final_score: int
    decision: str           # "approved" | "captcha_pass" | "rejected"
    payout_eth: float


class ClaimProcessor:
    def __init__(
        self,
        config: dict,
        fraud_detector: FraudDetector,
        oracle_network: OracleNetwork,
        pool: InsurancePool,
        rng: np.random.Generator,
        logger: logging.Logger,
        mode: int,
    ) -> None:
        self.config          = config
        self.fraud_detector  = fraud_detector
        self.oracle_network  = oracle_network
        self.pool            = pool
        self.rng             = rng
        self.logger          = logger
        self.mode            = mode
        self._fd_cfg         = config["fraud_detection"]

        # Captcha pass probabilities
        self._captcha_pass_honest    = 0.90
        self._captcha_pass_fraudulent = 0.15

    # ------------------------------------------------------------------
    def process(
        self,
        swap: Swap,
        claim_rate: float,
        is_fraudulent: bool,
        day: int,
    ) -> Claim:
        # Step 1: compute raw fraud score (FraudDetector)
        raw_score = self.fraud_detector.compute_fraud_score(
            tier=swap.user_tier,
            claim_rate=claim_rate,
            is_fraudulent=is_fraudulent,
        )

        # Step 2: oracle voting (they see the raw score as the true value)
        oracle_scores, participating = self.oracle_network.get_oracle_scores(
            true_score=raw_score
        )
        self.oracle_network.update_divergences(oracle_scores, participating, day)

        # Step 3: final score = oracle median (or raw if no oracles)
        final_score = (
            int(np.median(oracle_scores)) if oracle_scores else raw_score
        )

        # Step 4: initial decision
        decision = self.fraud_detector.get_decision(final_score, swap.user_tier)

        # Step 5: captcha simulation
        if decision == "captcha":
            pass_prob = (
                self._captcha_pass_fraudulent
                if is_fraudulent
                else self._captcha_pass_honest
            )
            decision = "approved" if self.rng.random() < pass_prob else "rejected"

        # Step 6: compute payout
        if decision == "approved":
            multiplier = _COVERAGE_PAYOUT.get(swap.coverage.lower(), 1.00)
            payout = swap.loss_eth * multiplier
            # Apply tier cap in mode 2
            if self.mode == 2 and swap.user_tier is not None:
                cap = self._get_tier_cap(swap.user_tier)
                payout = min(payout, cap)
        else:
            payout = 0.0

        if decision == "rejected":
            self.logger.info(
                f"CLAIM REJECTED — user={swap.user_id} score={final_score} "
                f"tx={swap.tx_hash[:10]}"
            )

        return Claim(
            swap_tx_hash=swap.tx_hash,
            user_id=swap.user_id,
            user_tier=swap.user_tier,
            coverage=swap.coverage,
            loss_eth=swap.loss_eth,
            fraud_score=raw_score,
            oracle_scores=oracle_scores,
            final_score=final_score,
            decision=decision,
            payout_eth=payout,
        )

    # ------------------------------------------------------------------
    def _get_tier_cap(self, tier: str) -> float:
        tiers = self.config.get("tiers", {})
        tier_data = tiers.get(tier.lower(), {})
        return float(tier_data.get("max_capital_eth", float("inf")))
