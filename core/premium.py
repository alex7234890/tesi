"""
Premium formula:

  P = V × (Patt × L%) × (1 + M) × Fcov

Where:
  V    — swap value in ETH
  Patt — sandwich attack probability (real or simulated)
  L%   — average loss percentage
  M    — Mbase + Madj
  Fcov — 0.70 (low) | 0.90 (medium) | 1.00 (high)
"""
from __future__ import annotations

_FCOV = {"low": 0.70, "medium": 0.90, "high": 1.00}


def compute_premium(
    value_eth: float,
    patt: float,
    loss_pct: float,
    m_total: float,
    coverage: str,
) -> float:
    fcov    = _FCOV.get(coverage.lower(), 1.00)
    premium = value_eth * (patt * loss_pct) * (1.0 + m_total) * fcov
    return max(premium, 0.0)
