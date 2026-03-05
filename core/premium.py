"""
Premium formula:

  P = V × [(Patt × L%) + (Tint × E / (1-E)) / (Vbase × 1000)] × (1 + M) × Fcov

Where:
  V       — swap value in ETH
  Patt    — sandwich attack probability (real or simulated)
  L%      — average loss percentage
  Tint    — total fraud intercepted in the last 24 h (ETH)
  E       — False Negative Rate (config: fraud_detection.false_negative_rate)
  Vbase   — total insured swaps in the last 24 h (count)
  M       — Mbase + Madj
  Fcov    — 0.70 (low) | 0.90 (medium) | 1.00 (high)
"""
from __future__ import annotations

_FCOV = {"low": 0.70, "medium": 0.90, "high": 1.00}


def compute_premium(
    value_eth: float,
    patt: float,
    loss_pct: float,
    tint: float,
    e: float,
    vbase: int,
    m_total: float,
    coverage: str,
) -> float:
    fcov   = _FCOV.get(coverage.lower(), 1.00)
    vbase  = max(vbase, 1)
    e      = min(max(e, 0.0), 0.9999)

    base_risk   = patt * loss_pct
    fraud_term  = (tint * (e / (1.0 - e))) / (vbase * 1000.0)

    premium = value_eth * (base_risk + fraud_term) * (1.0 + m_total) * fcov
    return max(premium, 0.0)
