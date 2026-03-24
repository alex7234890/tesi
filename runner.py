"""
MEV Insurance Simulator — CLI runner (mode 2: fully synthetic).

Usage examples
--------------
python runner.py
python runner.py --config config/mode2_synthetic.yaml
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

from utils.config_loader import load_config
from utils.logger import get_logger

from core.pool import InsurancePool
from core.premium import compute_premium
from core.claim_processor import ClaimProcessor

from datasources.synthetic import SyntheticDataSource

from analytics.collector import MetricsCollector
from analytics.reporter import Reporter
from analytics.charts import generate_all_charts

logger = get_logger("runner")

_HERE    = os.path.dirname(__file__)
_DB_PATH = os.path.join(_HERE, "data", "blockchain.db")
_OUT_DIR = os.path.join(_HERE, "data")


# =========================================================================
# Simulation engine
# =========================================================================

_FCOV_PAYOUT = {"low": 0.50, "medium": 0.70, "high": 1.00}


def _n_oracles_for_claim(swap_value_eth: float) -> int:
    if swap_value_eth < 1.0:
        return 3
    elif swap_value_eth < 5.0:
        return 5
    else:
        return 7


def run_single(
    config: dict,
    coverage: str,
    db_path: str = _DB_PATH,
    # kept for backwards-compat with callers that still pass mode=2
    mode: int = 2,
) -> tuple:
    """
    Run one simulation pass (always synthetic).
    Returns (MetricsCollector, InsurancePool, summary_dict).
    """
    rng = np.random.default_rng(config["simulation"]["seed"])

    pool       = InsurancePool(config)
    claim_proc = ClaimProcessor(config=config, pool=pool, logger=logger, mode=2)
    collector  = MetricsCollector()

    ds = SyntheticDataSource(config, db_path, rng, coverage=coverage)

    duration = ds.get_duration_days()
    logger.info(f"Starting simulation  coverage={coverage}  days={duration}")

    e_cfg           = float(config.get("market", {}).get("e", 0.20))
    fraud_claim_pct = float(config.get("simulation", {}).get("fraud_claim_pct", 0.05))
    fcov_payout     = _FCOV_PAYOUT.get(coverage.lower(), 1.00)
    min_premium_pct = float(config.get("premium", {}).get("min_premium_pct", 0.015))
    _oracles_cfg    = config.get("oracles", {})
    oracle_reward   = float(
        _oracles_cfg.get("oracle_reward_per_claim",
        _oracles_cfg.get("reward_patt_update_eth", 0.002))
    )

    prev_total_premiums: float = 0.0
    prev_total_payouts:  float = 0.0

    vbase_prev: float       = 100.0
    tint_prev:  float       = 0.0
    oracle_cost_prev: float = 0.0

    total_oracle_cost_eth: float = 0.0
    breakdown_event: dict | None = None
    already_broken: bool         = False

    for day in range(duration):
        patt  = ds.get_patt(day)
        swaps = ds.get_daily_swaps(day)
        users = ds.users

        m_total = pool.get_m_total()
        l_pct   = config["market"]["loss_pct_mean"]

        tint_today            = tint_prev
        vbase_today           = vbase_prev
        oracle_cost_24h_today = oracle_cost_prev

        today_claims       = []
        today_swap_details = []

        n_real_attacks       = 0
        n_fraud_caught       = 0
        n_fraud_escaped      = 0
        payout_real_today    = 0.0
        payout_fraud_today   = 0.0
        oracle_cost_today    = 0.0
        n_oracles_used_today = 0

        avg_swap_value_eth = sum(s.value_eth for s in swaps) / max(len(swaps), 1)

        _n_real_today  = sum(1 for s in swaps if s.is_attacked)
        _n_fraud_total = int(rng.binomial(_n_real_today, fraud_claim_pct)) if _n_real_today > 0 else 0
        _non_atk_idx   = [i for i, s in enumerate(swaps) if not s.is_attacked]
        _n_fraud_total = min(_n_fraud_total, len(_non_atk_idx))
        _fraud_idx: set = set()
        if _n_fraud_total > 0 and _non_atk_idx:
            _chosen    = rng.choice(len(_non_atk_idx), size=_n_fraud_total, replace=False)
            _fraud_idx = {_non_atk_idx[j] for j in _chosen}

        for _swap_i, swap in enumerate(swaps):
            is_fraud_caught  = False
            is_fraud_escaped = False
            if _swap_i in _fraud_idx:
                if rng.random() < (1.0 - e_cfg):
                    is_fraud_caught = True
                else:
                    is_fraud_escaped = True

            premium = compute_premium(
                value_eth=swap.value_eth,
                patt=patt,
                loss_pct=l_pct,
                m_total=m_total,
                coverage=swap.coverage,
                tint=tint_today,
                e=e_cfg,
                vbase=vbase_today,
                min_premium_pct=min_premium_pct,
                oracle_cost_24h=oracle_cost_24h_today,
            )
            pool.add_premium(premium)
            pool.register_policy()
            premium_pct = premium / swap.value_eth * 100.0 if swap.value_eth > 0 else 0.0

            payout_eth      = 0.0
            claim_submitted = swap.is_attacked or is_fraud_caught or is_fraud_escaped
            claim_approved  = False
            tipo_claim      = "nessuno"

            _n_oracles = 0
            if claim_submitted:
                _n_oracles = _n_oracles_for_claim(swap.value_eth)
                _oc_swap   = _n_oracles * oracle_reward
                oracle_cost_today    += _oc_swap
                n_oracles_used_today += _n_oracles
                pool.add_payout(_oc_swap)

            if swap.is_attacked:
                tipo_claim     = "reale"
                claim_approved = True
                n_real_attacks += 1
                user = users.get(swap.user_id)
                pool.register_pending_claim(swap.loss_eth)
                claim = claim_proc.process(swap=swap)
                pool.resolve_pending_claim(swap.loss_eth)
                today_claims.append(claim)
                payout_eth = claim.payout_eth
                pool.add_payout(payout_eth)
                payout_real_today += payout_eth
                if user:
                    user.total_claims += 1

            elif is_fraud_caught:
                tipo_claim = "frode_intercettata"
                n_fraud_caught += 1

            elif is_fraud_escaped:
                tipo_claim     = "frode_scappata"
                claim_approved = True
                fcov_sw    = _FCOV_PAYOUT.get(swap.coverage.lower(), 1.00)
                payout_eth = swap.value_eth * l_pct * fcov_sw
                pool.add_payout(payout_eth)
                payout_fraud_today += payout_eth
                n_fraud_escaped += 1

            rimborso_pct = payout_eth / swap.value_eth * 100.0 if swap.value_eth > 0 else 0.0

            today_swap_details.append({
                "swap_id":         swap.tx_hash,
                "value_ETH":       swap.value_eth,
                "was_attacked":    swap.is_attacked,
                "insured":         True,
                "coverage_level":  swap.coverage,
                "premium_paid":    premium,
                "premium_pct":     round(premium_pct, 4),
                "claim_submitted": claim_submitted,
                "claim_approved":  claim_approved,
                "payout_ETH":      payout_eth,
                "rimborso_pct":    round(rimborso_pct, 4),
                "tipo_claim":      tipo_claim,
                "oracle_cost_eth": round(_n_oracles * oracle_reward, 6),
                "n_oracles_used":  _n_oracles,
            })

        n_fraud_attempts = n_fraud_caught + n_fraud_escaped

        pool.end_of_day()

        if not already_broken and pool.balance_eth < 0:
            breakdown_event = {
                "day":          day,
                "reason":       "Saldo pool esaurito (balance < 0 ETH)",
                "pool_balance": pool.balance_eth,
            }
            already_broken = True

        premiums_today = pool.total_premiums_eth - prev_total_premiums
        payouts_today  = pool.total_payouts_eth  - prev_total_payouts
        prev_total_premiums = pool.total_premiums_eth
        prev_total_payouts  = pool.total_payouts_eth

        tint_prev        = n_fraud_caught * avg_swap_value_eth
        vbase_prev       = float(len(swaps))
        oracle_cost_prev = oracle_cost_today

        total_oracle_cost_eth += oracle_cost_today

        _e_safe_d  = max(min(e_cfg, 0.9999), 0.0001)
        _term1_d   = patt * l_pct
        _term2_d   = (tint_today * (_e_safe_d / (1.0 - _e_safe_d))) / max(vbase_today, 1.0) if vbase_today > 0 else 0.0
        _term3_d   = oracle_cost_24h_today / max(vbase_today, 1.0) if vbase_today > 0 else 0.0
        _prem_rate_d = (_term1_d + _term2_d + _term3_d) * (1.0 + m_total) * fcov_payout

        collector.collect(
            day=day,
            pool=pool,
            claims=today_claims,
            swaps=swaps,
            patt=patt,
            mode=2,
            users=users,
            premiums_today=premiums_today,
            payouts_today=payouts_today,
            pending_liabilities_eth=pool.pending_liabilities_eth,
            swap_details=today_swap_details,
            tint=tint_today,
            e=e_cfg,
            vbase=vbase_today,
            n_real_attacks=n_real_attacks,
            n_fraud_attempts=n_fraud_attempts,
            n_fraud_caught=n_fraud_caught,
            n_fraud_escaped=n_fraud_escaped,
            avg_swap_value_eth=avg_swap_value_eth,
            payout_real_today=payout_real_today,
            payout_fraud_today=payout_fraud_today,
            oracle_cost_today=oracle_cost_today,
            n_oracles_used_today=n_oracles_used_today,
            term1_today=_term1_d,
            term2_today=_term2_d,
            term3_today=_term3_d,
            premium_rate_today=_prem_rate_d,
        )

    logger.info("Simulation complete.")
    summary = collector.summary(pool)
    summary["breakdown_event"]       = breakdown_event
    summary["pool_survived"]         = breakdown_event is None
    summary["total_oracle_cost_eth"] = round(total_oracle_cost_eth, 6)
    return collector, pool, summary


# =========================================================================
# CLI entry point
# =========================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MEV Insurance Protocol Simulator (synthetic)")
    p.add_argument("--coverage", default="high", choices=["low", "medium", "high"],
                   help="Coverage level (default: high)")
    p.add_argument("--config", default=None, help="Path to YAML config override")
    p.add_argument("--db-path", default=_DB_PATH,
                   help=f"SQLite database path (default: {_DB_PATH})")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    config_path = args.config or os.path.join(_HERE, "config", "mode2_synthetic.yaml")
    config      = load_config(config_path)
    db_path     = args.db_path

    reporter = Reporter(output_dir=_OUT_DIR)

    collector, pool, summary = run_single(config, coverage=args.coverage, db_path=db_path)
    reporter.print_summary(summary, mode=2, coverage=args.coverage)
    df = collector.to_dataframe()
    reporter.save_csv(df, f"mode2_{args.coverage}")
    reporter.save_summary_json(summary, f"mode2_{args.coverage}")

    generate_all_charts({"synthetic": df}, _OUT_DIR, mode=2)

    dashboard_path = os.path.join(_HERE, "dashboard", "app.py")
    print("\nLaunching Streamlit dashboard …")
    os.system(f"streamlit run {dashboard_path}")


if __name__ == "__main__":
    main()
