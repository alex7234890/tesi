"""
MEV Insurance Simulator — CLI runner.

Usage examples
--------------
# Mode 1 — all three coverage levels sequentially
python runner.py --mode 1 --coverage all

# Mode 1 — single coverage level
python runner.py --mode 1 --coverage high --fraud-rate 0.05

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
from core.fraud_detector import FraudDetector
from core.oracle_network import OracleNetwork
from core.claim_processor import ClaimProcessor
from core.tier_manager import TierManager

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
    pool           = InsurancePool(config)
    fraud_detector = FraudDetector(config, rng)
    oracle_net     = OracleNetwork(config, rng, logger)

    claim_proc = ClaimProcessor(
        config=config,
        fraud_detector=fraud_detector,
        oracle_network=oracle_net,
        pool=pool,
        rng=rng,
        logger=logger,
        mode=mode,
    )

    tier_mgr   = TierManager(config, logger) if mode == 2 else None
    collector  = MetricsCollector()

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
    # Rolling state across days
    # -----------------------------------------------------------------
    prev_tint:  float = 0.0   # ETH fraud intercepted yesterday
    prev_vbase: int   = 1     # insured swaps yesterday
    blacklisted: set  = set()

    # User states (for mode 2 we get them from the datasource)
    users = getattr(ds, "users", {})

    # Cumulative totals from previous day (for daily delta computation)
    prev_total_premiums:       float = 0.0
    prev_total_payouts:        float = 0.0
    prev_total_oracle_rewards: float = 0.0

    # -----------------------------------------------------------------
    # Main simulation loop
    # -----------------------------------------------------------------
    for day in range(duration):
        patt   = ds.get_patt(day)
        swaps  = ds.get_daily_swaps(day)

        # Users for mode 2 are updated inside the datasource per call
        if mode == 2:
            users = ds.users  # type: ignore[attr-defined]

        madj    = pool.get_madj()
        m_total = pool.get_m_total()
        e       = config["fraud_detection"]["false_negative_rate"]
        l_pct   = config["market"]["loss_pct_mean"]

        today_tint:   float = 0.0
        today_claims        = []
        today_swap_details  = []

        for swap in swaps:
            if swap.user_id in blacklisted:
                continue

            # ---- Premium for this insured swap ----
            premium = compute_premium(
                value_eth=swap.value_eth,
                patt=patt,
                loss_pct=l_pct,
                tint=prev_tint,
                e=e,
                vbase=prev_vbase,
                m_total=m_total,
                coverage=swap.coverage,
            )
            pool.add_premium(premium)
            pool.register_policy()

            # Start building swap detail record
            swap_detail: dict = {
                "swap_id":        swap.tx_hash,
                "value_ETH":      swap.value_eth,
                "was_attacked":   swap.is_attacked,
                "insured":        True,
                "coverage_level": swap.coverage,
                "premium_paid":   premium,
                "claim_submitted": False,
                "claim_approved":  False,
                "payout_ETH":     0.0,
                "fraud_score":    None,
                "rejection_reason": "",
            }

            # ---- Claim if attacked ----
            if swap.is_attacked:
                # Get user state
                user = users.get(swap.user_id) if users else None
                claim_rate    = user.claim_rate if user else 0.0
                is_fraudulent = user.is_fraudulent if user else False

                pool.register_pending_claim(swap.loss_eth)
                claim = claim_proc.process(
                    swap=swap,
                    claim_rate=claim_rate,
                    is_fraudulent=is_fraudulent,
                    day=day,
                )
                pool.resolve_pending_claim(swap.loss_eth)
                today_claims.append(claim)

                swap_detail["claim_submitted"]   = True
                swap_detail["claim_approved"]    = claim.decision == "approved"
                swap_detail["payout_ETH"]        = claim.payout_eth
                swap_detail["fraud_score"]        = claim.final_score
                swap_detail["rejection_reason"]  = claim.rejection_reason or ""

                if claim.decision == "approved":
                    pool.add_payout(claim.payout_eth)
                    if user:
                        user.total_claims += 1
                    logger.debug(
                        f"APPROVED  payout={claim.payout_eth:.4f} ETH  "
                        f"user={swap.user_id}  score={claim.final_score}"
                    )
                elif claim.decision == "rejected":
                    blacklisted.add(swap.user_id)
                    if user:
                        user.is_blacklisted = True
                    today_tint += swap.loss_eth  # saved from fraud

                # Track fraud score history
                if user:
                    user.fraud_score_history.append(claim.final_score)
                    user.avg_fraud_score = float(
                        sum(user.fraud_score_history) / len(user.fraud_score_history)
                    )

            today_swap_details.append(swap_detail)

        # ---- Oracle daily rewards ----
        oracle_rewards = oracle_net.distribute_daily_rewards(day)
        pool.add_oracle_reward(oracle_rewards)

        # ---- Tier upgrades (mode 2) ----
        n_upgrades = 0
        if mode == 2 and tier_mgr is not None:
            n_upgrades = tier_mgr.process_upgrades(users, day)
            if n_upgrades > 0:
                logger.info(f"Day {day}: {n_upgrades} tier upgrade(s)")

        # ---- End-of-day accounting ----
        pool.end_of_day()

        # Log significant events
        sr = pool.solvency_ratio()
        if sr < 1.3:
            logger.warning(f"Day {day}: Pool stress — SR={sr:.3f}")
        if sr < 1.0:
            logger.error(f"Day {day}: POOL INSOLVENT — SR={sr:.3f}")

        prev_tint  = today_tint
        prev_vbase = max(len(swaps), 1)

        # ---- Compute daily flow deltas ----
        premiums_today       = pool.total_premiums_eth       - prev_total_premiums
        payouts_today        = pool.total_payouts_eth        - prev_total_payouts
        oracle_rewards_today = pool.total_oracle_rewards_eth - prev_total_oracle_rewards
        prev_total_premiums       = pool.total_premiums_eth
        prev_total_payouts        = pool.total_payouts_eth
        prev_total_oracle_rewards = pool.total_oracle_rewards_eth

        # ---- Collect metrics ----
        collector.collect(
            day=day,
            pool=pool,
            claims=today_claims,
            swaps=swaps,
            oracle_network=oracle_net,
            patt=patt,
            mode=mode,
            users=users if mode == 2 else None,
            n_upgrades=n_upgrades,
            premiums_today=premiums_today,
            payouts_today=payouts_today,
            oracle_rewards_today=oracle_rewards_today,
            pending_liabilities_eth=pool.pending_liabilities_eth,
            swap_details=today_swap_details,
        )

    logger.info("Simulation complete.")
    summary = collector.summary(pool)
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
    p.add_argument("--fraud-rate", type=float, default=None,
                   help="Override fraud_detection.user_fraud_rate")
    p.add_argument("--oracle-dishonest-rate", type=float, default=None,
                   help="Override fraud_detection.oracle_dishonest_rate")
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

    # Apply CLI overrides
    if args.fraud_rate is not None:
        config["fraud_detection"]["user_fraud_rate"] = args.fraud_rate
        config["users"]["fraud_rate"]                = args.fraud_rate
    if args.oracle_dishonest_rate is not None:
        config["fraud_detection"]["oracle_dishonest_rate"] = args.oracle_dishonest_rate

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
