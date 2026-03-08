"""
MEV Insurance Simulator — CLI runner.

Usage examples
--------------
# Mode 1 — all three coverage levels sequentially
python runner.py --mode 1 --coverage all

# Mode 1 — single coverage level
python runner.py --mode 1 --coverage high

# Mode 2 — full synthetic
python runner.py --mode 2

# Custom config file
python runner.py --mode 2 --config config/mode2_synthetic.yaml

# Download fresh blockchain data before running mode 1
python runner.py --mode 1 --coverage all --download-fresh
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

# Ensure the package root is on the path when run directly
sys.path.insert(0, os.path.dirname(__file__))

from utils.config_loader import load_config
from utils.logger import get_logger

from core.pool import InsurancePool
from core.premium import compute_premium
from core.claim_processor import ClaimProcessor

from datasources.blockchain import BlockchainDataSource
from datasources.synthetic import SyntheticDataSource

from analytics.collector import MetricsCollector
from analytics.reporter import Reporter
from analytics.charts import generate_all_charts

logger = get_logger("runner")

# -------------------------------------------------------------------------
# Default paths
# -------------------------------------------------------------------------
_HERE    = os.path.dirname(__file__)
_DB_PATH = os.path.join(_HERE, "data", "blockchain.db")
_OUT_DIR = os.path.join(_HERE, "data")


# =========================================================================
# Simulation engine
# =========================================================================

_FCOV_PAYOUT = {"low": 0.50, "medium": 0.70, "high": 1.00}


def _n_oracles_for_claim(swap_value_eth: float) -> int:
    """Numero di oracle per claim in base al valore dello swap (PDF spec)."""
    if swap_value_eth < 1.0:
        return 3
    elif swap_value_eth < 5.0:
        return 5
    else:
        return 7


def run_single(
    config: dict,
    mode: int,
    coverage: str,
    db_path: str = _DB_PATH,
) -> tuple:
    """
    Run one simulation pass.
    Returns (MetricsCollector, InsurancePool, summary_dict).
    """
    rng = np.random.default_rng(config["simulation"]["seed"])

    # -----------------------------------------------------------------
    # Instantiate components
    # -----------------------------------------------------------------
    pool       = InsurancePool(config)

    claim_proc = ClaimProcessor(
        config=config,
        pool=pool,
        logger=logger,
        mode=mode,
    )

    collector = MetricsCollector()

    # -----------------------------------------------------------------
    # Datasource
    # -----------------------------------------------------------------
    if mode == 1:
        ds = BlockchainDataSource(config, db_path, rng, coverage=coverage)
    else:
        ds = SyntheticDataSource(config, db_path, rng, coverage=coverage)

    duration = ds.get_duration_days()
    logger.info(f"Starting simulation  mode={mode}  coverage={coverage}  days={duration}")

    # -----------------------------------------------------------------
    # Premium formula parameters (from config)
    # -----------------------------------------------------------------
    e_cfg           = float(config.get("market", {}).get("e", 0.20))
    fraud_claim_pct = float(config.get("simulation", {}).get("fraud_claim_pct", 0.05))
    fcov_payout     = _FCOV_PAYOUT.get(coverage.lower(), 1.00)
    oracle_reward   = float(config.get("oracles", {}).get("reward_patt_update_eth", 0.002))

    # -----------------------------------------------------------------
    # Rolling state across days
    # -----------------------------------------------------------------
    # User states (for mode 2 we get them from the datasource)
    users = getattr(ds, "users", {})

    # Cumulative totals from previous day (for daily delta computation)
    prev_total_premiums: float = 0.0
    prev_total_payouts:  float = 0.0

    # Dynamic tint/vbase: computed from D-1 (start with defaults for day 0)
    vbase_prev: float = 100.0   # insured swaps from previous day
    tint_prev:  float = 0.0     # n_fraud_caught * avg_swap_value from previous day

    # Oracle cost tracking
    total_oracle_cost_eth: float = 0.0

    # Breakdown tracking
    breakdown_event: dict | None = None
    already_broken: bool = False

    # -----------------------------------------------------------------
    # Main simulation loop
    # -----------------------------------------------------------------
    for day in range(duration):
        patt  = ds.get_patt(day)
        swaps = ds.get_daily_swaps(day)

        # Users for mode 2 are updated inside the datasource per call
        if mode == 2:
            users = ds.users  # type: ignore[attr-defined]

        m_total = pool.get_m_total()
        l_pct   = config["market"]["loss_pct_mean"]

        # Dynamic tint/vbase from previous day
        tint_today  = tint_prev
        vbase_today = vbase_prev

        today_claims       = []
        today_swap_details = []

        # Per-day counters (reset each day)
        n_real_attacks      = 0
        n_fraud_caught      = 0
        n_fraud_escaped     = 0
        payout_real_today   = 0.0
        payout_fraud_today  = 0.0
        oracle_cost_today   = 0.0
        n_oracles_used_today= 0

        # Average swap value for tint computation
        avg_swap_value_eth = sum(s.value_eth for s in swaps) / max(len(swaps), 1)

        for swap in swaps:
            # ---- Per-swap fraud classification ----
            # Only non-attacked swaps can be fraudulent claimants
            is_fraud_caught  = False
            is_fraud_escaped = False
            if not swap.is_attacked and fraud_claim_pct > 0.0:
                if rng.random() < fraud_claim_pct:
                    if rng.random() < (1.0 - e_cfg):
                        is_fraud_caught = True   # detected, blocked
                    else:
                        is_fraud_escaped = True  # slipped through

            # ---- Premium — ALL tx (real, fraud, normal) pay premium ----
            premium = compute_premium(
                value_eth=swap.value_eth,
                patt=patt,
                loss_pct=l_pct,
                m_total=m_total,
                coverage=swap.coverage,
                tint=tint_today,
                e=e_cfg,
                vbase=vbase_today,
            )
            pool.add_premium(premium)
            pool.register_policy()
            premium_pct = premium / swap.value_eth * 100.0 if swap.value_eth > 0 else 0.0

            # ---- Payout / claim classification ----
            payout_eth      = 0.0
            claim_submitted = swap.is_attacked or is_fraud_caught or is_fraud_escaped
            claim_approved  = False
            tipo_claim      = "nessuno"

            # Oracle cost — paid for every submitted claim (approved or not)
            _n_oracles = 0
            if claim_submitted:
                _n_oracles = _n_oracles_for_claim(swap.value_eth)
                _oc_swap   = _n_oracles * oracle_reward
                oracle_cost_today    += _oc_swap
                n_oracles_used_today += _n_oracles
                pool.add_payout(_oc_swap)          # deducted from pool immediately

            if swap.is_attacked:
                tipo_claim    = "reale"
                claim_approved = True
                n_real_attacks += 1
                user = users.get(swap.user_id) if users else None
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
                # Fraud detected — no payout; premium was already collected above
                n_fraud_caught += 1

            elif is_fraud_escaped:
                tipo_claim    = "frode_scappata"
                claim_approved = True
                # Fraudulent payout: simulated loss at l_pct of swap value
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

        logger.debug(
            f"Day {day}: real_atk={n_real_attacks} fraud_caught={n_fraud_caught} "
            f"fraud_escaped={n_fraud_escaped} "
            f"payout_real={payout_real_today:.4f} payout_fraud={payout_fraud_today:.4f}"
        )

        # ---- End-of-day accounting ----
        pool.end_of_day()

        # ---- Breakdown detection ----
        if not already_broken and pool.balance_eth < 0:
            breakdown_event = {
                "day":         day,
                "reason":      "Saldo pool esaurito (balance < 0 ETH)",
                "pool_balance": pool.balance_eth,
            }
            already_broken = True

        # ---- Compute daily flow deltas ----
        premiums_today = pool.total_premiums_eth - prev_total_premiums
        payouts_today  = pool.total_payouts_eth  - prev_total_payouts
        prev_total_premiums = pool.total_premiums_eth
        prev_total_payouts  = pool.total_payouts_eth

        # ---- Update dynamic tint/vbase for next day ----
        tint_prev  = n_fraud_caught * avg_swap_value_eth
        vbase_prev = float(len(swaps))

        # ---- Accumulate oracle cost ----
        total_oracle_cost_eth += oracle_cost_today

        # Compute Term1/Term2 for the day (for metrics)
        _e_safe_d = max(min(e_cfg, 0.9999), 0.0001)
        _l_pct_d  = l_pct
        _term1_d  = patt * _l_pct_d
        _term2_d  = (tint_today * (_e_safe_d / (1.0 - _e_safe_d))) / (max(vbase_today, 1.0) * 1000.0)
        _prem_rate_d = (_term1_d + _term2_d) * (1.0 + m_total) * fcov_payout

        # ---- Collect metrics ----
        collector.collect(
            day=day,
            pool=pool,
            claims=today_claims,
            swaps=swaps,
            patt=patt,
            mode=mode,
            users=users if mode == 2 else None,
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
            premium_rate_today=_prem_rate_d,
        )

    logger.info("Simulation complete.")
    summary = collector.summary(pool)
    summary["breakdown_event"]      = breakdown_event
    summary["pool_survived"]        = breakdown_event is None
    summary["total_oracle_cost_eth"] = round(total_oracle_cost_eth, 6)
    return collector, pool, summary


# =========================================================================
# Mode 1 helper: run all three coverage levels
# =========================================================================

def run_mode1_all(config: dict, db_path: str) -> dict:
    results = {}
    for cov in ("low", "medium", "high"):
        logger.info(f"\n{'='*50}\nMode 1 — coverage: {cov.upper()}\n{'='*50}")
        collector, pool, summary = run_single(config, mode=1, coverage=cov, db_path=db_path)
        results[cov] = (collector, pool, summary)
    return results


# =========================================================================
# CLI entry point
# =========================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MEV Insurance Protocol Simulator")
    p.add_argument("--mode", type=int, choices=[1, 2], required=True,
                   help="1 = real blockchain + partial sim | 2 = full synthetic")
    p.add_argument("--coverage", default="high",
                   choices=["low", "medium", "high", "all"],
                   help="Coverage level for mode 1 (default: high)")
    p.add_argument("--config", default=None,
                   help="Path to mode-specific YAML config override")
    p.add_argument("--download-fresh", action="store_true",
                   help="Download blockchain data before running (mode 1)")
    p.add_argument("--no-dashboard", action="store_true",
                   help="Skip launching Streamlit dashboard after run")
    p.add_argument("--db-path", default=_DB_PATH,
                   help=f"SQLite database path (default: {_DB_PATH})")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    # ---- Config ----
    if args.config:
        config_path = args.config
    elif args.mode == 1:
        config_path = os.path.join(_HERE, "config", "mode1_realchain.yaml")
    else:
        config_path = os.path.join(_HERE, "config", "mode2_synthetic.yaml")

    config = load_config(config_path)

    db_path = args.db_path

    # ---- Optional fresh download ----
    if args.download_fresh:
        if args.mode == 1:
            logger.info("Downloading blocks …")
            os.system(f"python {os.path.join(_HERE, 'scripts', 'download_blocks.py')}")
        logger.info("Downloading Patt history …")
        os.system(f"python {os.path.join(_HERE, 'scripts', 'download_patt.py')}")

    # ---- Mode 1: check for data ----
    if args.mode == 1 and not os.path.isfile(db_path):
        print(
            f"\n[!] SQLite database not found at: {db_path}\n"
            "    Run with --download-fresh to fetch blockchain data, or\n"
            "    run scripts/download_blocks.py manually.\n"
            "    Continuing with stub data.\n"
        )

    # ---- Run simulation ----
    reporter = Reporter(output_dir=_OUT_DIR)
    all_dfs  = {}

    if args.mode == 1 and args.coverage == "all":
        results = run_mode1_all(config, db_path)
        for cov, (collector, pool, summary) in results.items():
            reporter.print_summary(summary, mode=1, coverage=cov)
            df = collector.to_dataframe()
            reporter.save_csv(df, f"mode1_{cov}")
            all_dfs[f"Low ({cov})" if cov == "low" else cov.capitalize()] = df
    else:
        coverage = args.coverage if args.mode == 1 else "high"
        collector, pool, summary = run_single(
            config, mode=args.mode, coverage=coverage, db_path=db_path
        )
        reporter.print_summary(summary, mode=args.mode, coverage=coverage)
        df = collector.to_dataframe()
        label = f"mode{args.mode}_{coverage}"
        reporter.save_csv(df, label)
        reporter.save_summary_json(summary, label)
        all_dfs[coverage.capitalize()] = df

    # ---- Generate charts ----
    generate_all_charts(all_dfs, _OUT_DIR, mode=args.mode)

    # ---- Launch dashboard ----
    if not args.no_dashboard:
        dashboard_path = os.path.join(_HERE, "dashboard", "app.py")
        print(f"\nLaunching Streamlit dashboard …")
        os.system(f"streamlit run {dashboard_path}")


if __name__ == "__main__":
    main()
