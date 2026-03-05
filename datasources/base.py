from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Swap:
    timestamp: int
    value_eth: float
    is_attacked: bool
    loss_eth: float          # 0 if not attacked
    coverage: str            # "low" | "medium" | "high"
    user_id: str             # wallet address (real or simulated)
    user_tier: Optional[str] # None in mode 1
    tx_hash: str             # real or simulated


class BaseDataSource(ABC):
    def __init__(self, config: dict, db_path: str, rng) -> None:
        self.config = config
        self.db_path = db_path
        self.rng = rng

    @abstractmethod
    def get_daily_swaps(self, day: int) -> List[Swap]:
        """Return insured swaps for simulation day `day` (0-indexed)."""
        ...

    @abstractmethod
    def get_patt(self, day: int) -> float:
        """Return the sandwich attack probability for day `day`."""
        ...

    @abstractmethod
    def get_duration_days(self) -> int:
        """Return the total number of simulation days this source supports."""
        ...
