"""
Download recent Ethereum blocks from Infura and detect sandwich attacks.

Stores results in SQLite:
  - Table: swaps           (all DEX swap transactions)
  - Table: sandwich_attacks (detected sandwiches)

Usage:
    python scripts/download_blocks.py
    python scripts/download_blocks.py --blocks 10000 --db data/blockchain.db
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.config_loader import load_config
from utils.logger import get_logger

logger = get_logger("download_blocks")

# ---------------------------------------------------------------------------
# DEX contract addresses (from config — these are factory / router addresses)
# ---------------------------------------------------------------------------
_UNISWAP_V2_FACTORY  = "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f".lower()
_UNISWAP_V3_FACTORY  = "0x1F98431c8aD98523631AE4a59f267346ea31F984".lower()
_SUSHISWAP_FACTORY   = "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac".lower()

# Swap event topics
_SWAP_TOPIC_V2 = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
_SWAP_TOPIC_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"

_KNOWN_TOPICS = {_SWAP_TOPIC_V2, _SWAP_TOPIC_V3}


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS swaps (
    block_number INTEGER,
    tx_hash      TEXT PRIMARY KEY,
    timestamp    INTEGER,
    dex          TEXT,
    token_pair   TEXT,
    value_eth    REAL,
    is_attacked  INTEGER DEFAULT 0,
    loss_eth     REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sandwich_attacks (
    block_number   INTEGER,
    frontrun_hash  TEXT,
    victim_hash    TEXT,
    backrun_hash   TEXT,
    pool_address   TEXT,
    attacker_address TEXT,
    victim_loss_eth  REAL,
    timestamp      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_swaps_block ON swaps(block_number);
CREATE INDEX IF NOT EXISTS idx_swaps_timestamp ON swaps(timestamp);
"""


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------
def _get_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.executescript(_DDL)
    con.commit()
    return con


# ---------------------------------------------------------------------------
# Block range helpers
# ---------------------------------------------------------------------------
def _already_downloaded(con: sqlite3.Connection) -> Set[int]:
    rows = con.execute("SELECT DISTINCT block_number FROM swaps").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Swap value estimation (rough — uses gas price as proxy)
# ---------------------------------------------------------------------------
def _estimate_value_eth(receipt, tx) -> float:
    gas_used  = receipt.get("gasUsed", 0)
    gas_price = tx.get("gasPrice", 0)
    fee_eth   = (gas_used * gas_price) / 1e18
    # Very rough proxy: value ≈ 100× fee (typical DeFi swap)
    return max(fee_eth * 100.0, 0.01)


# ---------------------------------------------------------------------------
# Sandwich detection in a single block
# ---------------------------------------------------------------------------
def _detect_sandwiches(
    txs: list,
    receipts: dict,
) -> List[Dict]:
    """
    Simple heuristic: within a block, find triples (front, victim, back)
    where:
      - front and back share the same from-address
      - front and back interact with the same pool
      - victim is sandwiched between them
    Returns list of attack dicts.
    """
    # Group swap txs by pool address (from logs)
    pool_txs: Dict[str, List[dict]] = {}
    for tx in txs:
        receipt = receipts.get(tx["hash"].hex() if hasattr(tx["hash"], "hex") else tx["hash"], {})
        for log in receipt.get("logs", []):
            topic0 = log["topics"][0].hex() if log["topics"] else ""
            if topic0 in _KNOWN_TOPICS:
                pool = log["address"].lower()
                pool_txs.setdefault(pool, []).append({
                    "tx": tx,
                    "log": log,
                    "topic": topic0,
                })

    attacks = []
    for pool, entries in pool_txs.items():
        if len(entries) < 3:
            continue
        for i in range(len(entries) - 2):
            front  = entries[i]
            victim = entries[i + 1]
            back   = entries[i + 2]

            front_from = front["tx"].get("from", "").lower()
            back_from  = back["tx"].get("from", "").lower()
            vic_from   = victim["tx"].get("from", "").lower()

            if (
                front_from == back_from           # same attacker
                and front_from != vic_from        # different from victim
                and front_from != ""
            ):
                value_eth = _estimate_value_eth(
                    receipts.get(victim["tx"]["hash"].hex()
                                 if hasattr(victim["tx"]["hash"], "hex")
                                 else victim["tx"]["hash"], {}),
                    victim["tx"],
                )
                loss_eth = value_eth * 0.20  # fixed 20% loss estimate
                attacks.append({
                    "block_number":    front["tx"]["blockNumber"],
                    "frontrun_hash":   front["tx"]["hash"].hex()
                                       if hasattr(front["tx"]["hash"], "hex")
                                       else front["tx"]["hash"],
                    "victim_hash":     victim["tx"]["hash"].hex()
                                       if hasattr(victim["tx"]["hash"], "hex")
                                       else victim["tx"]["hash"],
                    "backrun_hash":    back["tx"]["hash"].hex()
                                       if hasattr(back["tx"]["hash"], "hex")
                                       else back["tx"]["hash"],
                    "pool_address":    pool,
                    "attacker_address": front_from,
                    "victim_loss_eth": loss_eth,
                    "timestamp":       0,  # filled later
                })
    return attacks


# ---------------------------------------------------------------------------
# Main download routine
# ---------------------------------------------------------------------------
def download(
    infura_url: str,
    n_blocks: int,
    db_path: str,
) -> None:
    try:
        from web3 import Web3
    except ImportError:
        logger.error("web3 not installed. Run: pip install web3")
        sys.exit(1)

    logger.info(f"Connecting to {infura_url} …")
    if infura_url.startswith("wss://"):
        w3 = Web3(Web3.LegacyWebSocketProvider(infura_url))
    else:
        w3 = Web3(Web3.HTTPProvider(infura_url))

    if not w3.is_connected():
        logger.error("Cannot connect to Ethereum node. Check your Infura URL / API key.")
        sys.exit(1)

    logger.info("Connected.")
    con = _get_db(db_path)
    downloaded = _already_downloaded(con)

    latest      = w3.eth.block_number
    start_block = max(latest - n_blocks, 0)
    blocks_todo = [b for b in range(start_block, latest + 1) if b not in downloaded]

    logger.info(f"Fetching {len(blocks_todo)} new blocks (latest={latest}) …")

    swap_rows    = []
    attack_rows  = []
    victim_hashes: Set[str] = set()

    for block_num in tqdm(blocks_todo, desc="Blocks", unit="block"):
        try:
            block = w3.eth.get_block(block_num, full_transactions=True)
        except Exception as exc:
            logger.warning(f"Block {block_num}: {exc}")
            continue

        timestamp = block["timestamp"]

        # Collect swap transactions (filter by known event logs)
        swap_txs  = []
        receipts  = {}
        for tx in block["transactions"]:
            try:
                receipt = w3.eth.get_transaction_receipt(tx["hash"])
            except Exception:
                continue
            receipts[tx["hash"].hex()] = dict(receipt)

            is_swap = any(
                log["topics"] and log["topics"][0].hex() in _KNOWN_TOPICS
                for log in receipt["logs"]
                if log["topics"]
            )
            if is_swap:
                swap_txs.append(dict(tx))

        # Detect sandwiches in this block
        attacks = _detect_sandwiches(swap_txs, receipts)
        for atk in attacks:
            atk["timestamp"] = timestamp
            victim_hashes.add(atk["victim_hash"])
            attack_rows.append(atk)

        # Store swap rows
        for tx in swap_txs:
            h = tx["hash"].hex() if hasattr(tx["hash"], "hex") else tx["hash"]
            receipt = receipts.get(h, {})
            value_eth = _estimate_value_eth(receipt, tx)
            is_atk    = 1 if h in victim_hashes else 0
            loss_eth  = value_eth * 0.20 if is_atk else 0.0
            dex       = "unknown"
            for log in receipt.get("logs", []):
                if log["topics"] and log["topics"][0].hex() == _SWAP_TOPIC_V2:
                    dex = "uniswap_v2"
                    break
                if log["topics"] and log["topics"][0].hex() == _SWAP_TOPIC_V3:
                    dex = "uniswap_v3"
                    break

            swap_rows.append((
                block_num, h, timestamp, dex, "",
                value_eth, is_atk, loss_eth,
            ))

        # Batch insert every 100 blocks
        if len(swap_rows) >= 1000:
            _flush(con, swap_rows, attack_rows)
            swap_rows   = []
            attack_rows = []

    _flush(con, swap_rows, attack_rows)
    con.close()
    logger.info("Download complete.")


def _flush(
    con: sqlite3.Connection,
    swaps: list,
    attacks: list,
) -> None:
    if swaps:
        con.executemany(
            "INSERT OR IGNORE INTO swaps "
            "(block_number,tx_hash,timestamp,dex,token_pair,value_eth,is_attacked,loss_eth) "
            "VALUES (?,?,?,?,?,?,?,?)",
            swaps,
        )
    if attacks:
        con.executemany(
            "INSERT OR IGNORE INTO sandwich_attacks "
            "(block_number,frontrun_hash,victim_hash,backrun_hash,"
            "pool_address,attacker_address,victim_loss_eth,timestamp) "
            "VALUES (:block_number,:frontrun_hash,:victim_hash,:backrun_hash,"
            ":pool_address,:attacker_address,:victim_loss_eth,:timestamp)",
            attacks,
        )
    con.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Download Ethereum blocks for MEV simulator")
    p.add_argument("--blocks", type=int, default=None,
                   help="Number of blocks to fetch (default: from config)")
    p.add_argument("--db", default=None,
                   help="SQLite path (default: data/blockchain.db)")
    p.add_argument("--infura-url", default=None,
                   help="Infura WebSocket URL (default: from config)")
    args = p.parse_args()

    _ROOT   = os.path.dirname(os.path.dirname(__file__))
    config  = load_config(os.path.join(_ROOT, "config", "mode1_realchain.yaml"))
    bc      = config["blockchain"]

    infura_url = args.infura_url or bc["infura_url"]
    n_blocks   = args.blocks     or bc["blocks_to_fetch"]
    db_path    = args.db         or os.path.join(_ROOT, "data", "blockchain.db")

    if "YOUR_KEY" in infura_url:
        print(
            "\n[!] Set your Infura API key in config/base.yaml (blockchain.infura_url)\n"
            "    or pass --infura-url wss://mainnet.infura.io/ws/v3/<YOUR_KEY>\n"
        )
        sys.exit(1)

    download(infura_url, n_blocks, db_path)


if __name__ == "__main__":
    main()
