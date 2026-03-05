"""
Download historical Patt (sandwich attack rate) time series.

Reads the existing swaps / sandwich_attacks tables in SQLite
(populated by download_blocks.py) and computes daily Patt.
If the tables are empty, falls back to querying Infura directly
for a lighter-weight scan over the past 180 days.

Stores results in:
  Table: patt_history (date TEXT PK, patt REAL, total_swaps INT, total_attacks INT)

Usage:
    python scripts/download_patt.py
    python scripts/download_patt.py --days 90 --db data/blockchain.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.config_loader import load_config
from utils.logger import get_logger

logger = get_logger("download_patt")

_DDL_PATT = """
CREATE TABLE IF NOT EXISTS patt_history (
    date         TEXT PRIMARY KEY,
    patt         REAL,
    total_swaps  INTEGER,
    total_attacks INTEGER
);
"""

_SWAP_TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
_SWAP_TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"


# ---------------------------------------------------------------------------
# Compute Patt from existing SQLite tables
# ---------------------------------------------------------------------------
def compute_from_db(con: sqlite3.Connection) -> list:
    rows = con.execute(
        """
        SELECT
            DATE(timestamp, 'unixepoch') AS day,
            COUNT(*)                     AS total_swaps,
            SUM(is_attacked)             AS total_attacks
        FROM swaps
        GROUP BY day
        ORDER BY day
        """
    ).fetchall()
    records = []
    for row in rows:
        date_str, total, attacks = row
        attacks = attacks or 0
        patt    = attacks / total if total > 0 else 0.0
        records.append((date_str, patt, total, attacks))
    return records


# ---------------------------------------------------------------------------
# Light-weight Patt scan via Infura (alternative when no local DB data)
# ---------------------------------------------------------------------------
def compute_from_infura(
    infura_url: str,
    days: int,
    db_path: str,
) -> list:
    try:
        from web3 import Web3
    except ImportError:
        logger.error("web3 not installed.")
        return []

    logger.info(f"Connecting to {infura_url} for Patt computation …")
    if infura_url.startswith("wss://"):
        w3 = Web3(Web3.WebsocketProvider(infura_url))
    else:
        w3 = Web3(Web3.HTTPProvider(infura_url))

    if not w3.is_connected():
        logger.error("Cannot connect. Check Infura URL.")
        return []

    records = []
    now     = datetime.now(tz=timezone.utc)

    for d in tqdm(range(days, 0, -1), desc="Days", unit="day"):
        target_date = now - timedelta(days=d)
        date_str    = target_date.strftime("%Y-%m-%d")

        # Estimate block numbers for start/end of that day
        blocks_per_day = 7200  # ~12s block time
        latest         = w3.eth.block_number
        end_block      = latest - (d - 1) * blocks_per_day
        start_block    = end_block - blocks_per_day

        total_swaps   = 0
        total_attacks = 0

        try:
            # Count Swap events via getLogs (much faster than fetching full blocks)
            swap_logs = w3.eth.get_logs({
                "fromBlock": max(start_block, 0),
                "toBlock":   end_block,
                "topics":    [[_SWAP_TOPIC_V2, _SWAP_TOPIC_V3]],
            })
            total_swaps = len(swap_logs)

            # Rough sandwich estimate: look for repeated addresses in same block
            block_pools: dict = {}
            for log in swap_logs:
                bn   = log["blockNumber"]
                addr = log["address"].lower()
                block_pools.setdefault(bn, {}).setdefault(addr, []).append(
                    log["transactionHash"].hex()
                )

            for bn, pools in block_pools.items():
                for pool_addr, txs in pools.items():
                    if len(txs) >= 3:
                        total_attacks += 1   # conservative estimate: 1 sandwich per pool/block

        except Exception as exc:
            logger.warning(f"Day {date_str}: {exc}")
            total_swaps   = 100   # fallback defaults
            total_attacks = 1

        patt = total_attacks / max(total_swaps, 1)
        records.append((date_str, patt, total_swaps, total_attacks))

    return records


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Download Patt history for MEV simulator")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--db", default=None)
    p.add_argument("--infura-url", default=None)
    args = p.parse_args()

    _ROOT  = os.path.dirname(os.path.dirname(__file__))
    config = load_config(os.path.join(_ROOT, "config", "base.yaml"))

    infura_url = args.infura_url or config["blockchain"]["infura_url"]
    db_path    = args.db or os.path.join(_ROOT, "data", "blockchain.db")

    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.executescript(_DDL_PATT)
    con.commit()

    # Try computing from existing swaps table first
    try:
        n_swaps = con.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
    except Exception:
        n_swaps = 0

    if n_swaps > 0:
        logger.info("Computing Patt from existing swaps table …")
        records = compute_from_db(con)
    elif "YOUR_KEY" not in infura_url:
        logger.info("No local swaps data — fetching from Infura …")
        records = compute_from_infura(infura_url, args.days, db_path)
    else:
        logger.warning(
            "No local data and no Infura key configured.\n"
            "Using synthetic default Patt = 0.010 for all days."
        )
        from datetime import datetime, timedelta, timezone
        now = datetime.now(tz=timezone.utc)
        records = []
        for d in range(args.days, 0, -1):
            date_str = (now - timedelta(days=d)).strftime("%Y-%m-%d")
            records.append((date_str, 0.010, 1000, 10))

    if records:
        con.executemany(
            "INSERT OR REPLACE INTO patt_history (date,patt,total_swaps,total_attacks) "
            "VALUES (?,?,?,?)",
            records,
        )
        con.commit()
        logger.info(f"Stored {len(records)} daily Patt records.")
    else:
        logger.warning("No records to store.")

    con.close()


if __name__ == "__main__":
    main()
