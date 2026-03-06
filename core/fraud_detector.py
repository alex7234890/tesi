"""
FraudScore computation and claim decision logic.

FraudScore = Score_ClaimRate + Score_Network   [0..80]

Decision thresholds:
  score < auto_approve  → APPROVED
  auto_approve <= score <= auto_reject  → CAPTCHA
  score > auto_reject   → REJECTED + blacklist
"""
from __future__ import annotations

from typing import Optional

import numpy as np


class FraudDetector:
    def __init__(self, config: dict, rng: np.random.Generator) -> None:
        self.config   = config
        self.rng      = rng
        self._fd      = config["fraud_detection"]
        self._bfs     = self._fd["network_bfs_scores"]
        self._crt     = self._fd["claim_rate_thresholds"]
        self._dec     = self._fd["fraud_score_decision"]

    # ------------------------------------------------------------------
    def _score_claim_rate(self, claim_rate: float) -> int:
        cr = self._crt
        if claim_rate > cr["very_suspicious"]:    # > 0.30
            return 30
        if claim_rate > cr["suspicious_high"]:    # > 0.20
            return 25
        if claim_rate > cr["suspicious_med"]:     # > 0.10
            return 20
        if claim_rate > cr["normal"]:             # > 0.06
            return 15
        return 0

    def _score_network(self, is_fraudulent: bool) -> int:
        if is_fraudulent:
            dist = self.rng.choice([1, 2])
            return self._bfs[f"distance_{dist}"]
        # Honest user: no path (70%) or far distance (30%)
        if self.rng.random() < 0.70:
            return self._bfs["no_path"]
        return self._bfs["distance_3"]

    # ------------------------------------------------------------------
    def compute_fraud_score(
        self,
        tier: Optional[str],  # kept for API compatibility, ignored
        claim_rate: float,
        is_fraudulent: bool,
    ) -> int:
        total = (
            self._score_claim_rate(claim_rate)
            + self._score_network(is_fraudulent)
        )
        return min(total, 80)

    def get_decision(self, score: int, tier: Optional[str]) -> str:
        if score > self._dec["auto_reject"]:      # > 80
            return "rejected"
        if score >= self._dec["captcha_low"]:     # >= 60
            return "captcha"
        # score < auto_approve → always approved (no tier check)
        return "approved"
