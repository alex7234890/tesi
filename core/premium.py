"""
Premium formula:

  P = V × max([(Patt × L%) + (Tint × E/(1−E)) / Vbase + C_oracle_24h / Vbase] × (1 + M), min_premium_pct) × Fcov

Where:
  V               — swap value in ETH
  Patt            — sandwich attack probability (real or simulated)
  L%              — average loss percentage
  Tint            — total ETH value of fraud swaps intercepted the previous day
  E               — False Negative Rate  →  E/(1−E) multiplier
  Vbase           — number of insured swaps in last 24h
  C_oracle_24h    — total oracle cost observed in the last 24h (sum of all rewards paid for claims,
                    Patt updates, CAPTCHA verifications, jury, etc.)
  M               — Mbase + Madj
  min_premium_pct — floor applicato PRIMA di Fcov (default 1.5% del valore swap)
  Fcov            — 0.70 (low) | 0.90 (medium) | 1.00 (high)
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
    min_premium_pct: float = 0.015,
    oracle_cost_24h: float = 0.0,
) -> float:
    """P = V × max([(Patt×L%) + (Tint×E/(1−E))/Vbase + C_oracle_24h/Vbase] × (1+M), min_premium_pct) × Fcov

    Il floor min_premium_pct viene applicato PRIMA di moltiplicare per Fcov,
    in modo da rimanere coerente con i livelli di copertura.
    """
    fcov     = _FCOV.get(coverage.lower(), 1.00)
    term1    = patt * loss_pct
    e_safe   = max(min(e, 0.9999), 0.0001)
    term2    = (tint * (e_safe / (1.0 - e_safe))) / max(vbase, 1.0)
    term3    = oracle_cost_24h / max(vbase, 1.0)
    base_pct = (term1 + term2 + term3) * (1.0 + m_total)
    base_pct = max(base_pct, min_premium_pct)   # floor prima di Fcov
    premium  = value_eth * base_pct * fcov
    return max(premium, 0.0)
