"""
Claim processing pipeline — simplified.

All attacked insured swaps are automatically approved.
Payout = loss_eth × coverage_multiplier.
Oracle cost is computed separately in the runner.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .pool import InsurancePool
from datasources.base import Swap

# Payout multipliers per coverage level
_COVERAGE_PAYOUT = {"low": 0.50, "medium": 0.70, "high": 1.00}


@dataclass
class Claim:
    swap_tx_hash: str
    user_id: str
    coverage: str
    loss_eth: float
    payout_eth: float
    decision: str = "approved"
    rejection_reason: Optional[str] = None


class ClaimProcessor:
    def __init__(
        self,
        config: dict,
        pool: InsurancePool,
        logger: logging.Logger,
        mode: int,
    ) -> None:
        self.config = config
        self.pool   = pool
        self.logger = logger
        self.mode   = mode

    def process(self, swap: Swap) -> Claim:
        multiplier = _COVERAGE_PAYOUT.get(swap.coverage.lower(), 1.00)
        payout     = swap.loss_eth * multiplier

        self.logger.debug(
            f"CLAIM APPROVED  payout={payout:.4f} ETH  user={swap.user_id}"
        )

        return Claim(
            swap_tx_hash=swap.tx_hash,
            user_id=swap.user_id,
            coverage=swap.coverage,
            loss_eth=swap.loss_eth,
            payout_eth=payout,
        )
