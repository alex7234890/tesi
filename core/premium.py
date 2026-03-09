"""
Premium formula:

  P = V × [(Patt × L%) + (Tint × E/(1−E)) / Vbase] × (1 + M) × Fcov

Where:
  V    — swap value in ETH
  Patt — sandwich attack probability (real or simulated)
  L%   — average loss percentage
  Tint — total ETH value of fraud swaps intercepted the previous day
  E    — False Negative Rate  →  E/(1−E) multiplier
  Vbase — number of insured swaps in last 24h
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
    tint: float = 8000.0,
    e: float = 0.20,
    vbase: float = 100.0,
) -> float:
    """P = V × [(Patt × L%) + (Tint × E/(1−E)) / Vbase] × (1+M) × Fcov"""
    fcov   = _FCOV.get(coverage.lower(), 1.00)
    term1  = patt * loss_pct
    e_safe = max(min(e, 0.9999), 0.0001)
    term2  = (tint * (e_safe / (1.0 - e_safe))) / max(vbase, 1.0)
    premium = value_eth * (term1 + term2) * (1.0 + m_total) * fcov
    return max(premium, 0.0)
