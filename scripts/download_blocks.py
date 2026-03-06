"""
Infura data fetcher — usa eth_getLogs invece di block-by-block.

~14 chiamate Infura per 2 giorni invece di ~13.300.
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", message=".*ChainId.*")
warnings.filterwarnings("ignore", message=".*eth-typing.*")

import argparse
import os
import pickle
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from utils.config_loader import load_config
from utils.logger import get_logger

logger = get_logger("download_blocks")

SWAP_TOPIC_V2    = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
SWAP_TOPIC_V3    = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SWAP_TOPIC_CURVE = "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140"

_DEX_TOPIC_MAP: Dict[str, str] = {
    "Uniswap V2": SWAP_TOPIC_V2,
    "Sushiswap":  SWAP_TOPIC_V2,
    "Uniswap V3": SWAP_TOPIC_V3,
    "Curve":      SWAP_TOPIC_CURVE,
}

_DEX_DISPLAY_TO_DB: Dict[str, str] = {
    "Uniswap V2": "uniswap_v2",
    "Uniswap V3": "uniswap_v3",
    "Sushiswap":  "sushiswap",
    "Curve":      "curve",
}

POOL_ADDRESSES: Dict[str, List[str]] = {
    "Uniswap V2": [
        "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc",
        "0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852",
        "0xA478c2975Ab1Ea89e8196811F51A7B7Ade33eB11",
        "0xBb2b8038a1640196FbE3e38816F3e67Cba72D940",
    ],
    "Sushiswap": [
        "0x397FF1542f962076d0BFE58eA045FfA2d347ACa0",
        "0x06da0fd433C1A5d7a4faa01111c044910A184553",
    ],
    "Uniswap V3": [
        "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
        "0x8ad599c3A0ff1De082011EFDDc58f1908eb6e6D8",
        "0x4e68Ccd3E89f51C3074ca5072bbAC773960dFa36",
        "0xCBCdF9626bC03E24f779434178A73a0B4bad62eD",
    ],
    "Curve": [],
}

CHUNK_SIZE     = 500
BLOCKS_PER_DAY = 6646

# Converti tutti gli indirizzi pool in checksum format (richiesto da eth_getLogs Infura)
def _checksum_pool_addresses(addrs_map: Dict[str, List[str]]) -> Dict[str, List[str]]:
    try:
        from web3 import Web3
        return {dex: [Web3.to_checksum_address(a) for a in addrs]
                for dex, addrs in addrs_map.items()}
    except ImportError:
        return addrs_map

POOL_ADDRESSES = _checksum_pool_addresses(POOL_ADDRESSES)

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
    block_number     INTEGER,
    frontrun_hash    TEXT,
    victim_hash      TEXT,
    backrun_hash     TEXT,
    pool_address     TEXT,
    attacker_address TEXT,
    victim_loss_eth  REAL,
    timestamp        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_swaps_block     ON swaps(block_number);
CREATE INDEX IF NOT EXISTS idx_swaps_timestamp ON swaps(timestamp);
"""


def _get_db(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path, check_same_thread=False)
    con.executescript(_DDL)
    con.commit()
    return con


def _flush(con: sqlite3.Connection, swaps: list, attacks: list) -> None:
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


def _cache_dir(db_path: str) -> str:
    root = os.path.dirname(os.path.dirname(db_path))
    d    = os.path.join(root, "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _connect(infura_url: str):
    try:
        from web3 import Web3
    except ImportError:
        logger.error("web3 non installato. Esegui: pip install web3")
        sys.exit(1)
    if infura_url.startswith("wss://"):
        _ws = getattr(Web3, "WebsocketProvider",
               getattr(Web3, "LegacyWebSocketProvider", None))
        if _ws is None:
            from web3.providers import WebsocketProvider as _ws
        w3 = Web3(_ws(infura_url, websocket_timeout=60))
    else:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(infura_url))
    if not w3.is_connected():
        raise ConnectionError("Impossibile connettersi a Infura.")
    return w3


def fetch_dex_events(
    infura_url: str,
    days: int,
    dex_targets: List[str],
    cache_dir: str = "cache",
    progress_cb=None,
    force_refresh: bool = False,
) -> dict:
    """
    Scarica eventi Swap usando eth_getLogs (chunk da 2000 blocchi).

    Ritorna dict: {"swaps": [...], "sandwich_attacks": [...], "metadata": {...}}
    """
    os.makedirs(cache_dir, exist_ok=True)
    dex_key    = "_".join(sorted(d.replace(" ", "") for d in dex_targets))
    cache_file = os.path.join(cache_dir, f"dex_events_{days}d_{dex_key}.pkl")

    if os.path.isfile(cache_file) and not force_refresh:
        age_h = (time.time() - os.path.getmtime(cache_file)) / 3600
        if age_h < 23:
            logger.info(f"Cache ({age_h:.1f}h fa) — skip fetch")
            with open(cache_file, "rb") as f:
                return pickle.load(f)
        logger.info(f"Cache scaduta ({age_h:.1f}h fa)")

    w3 = _connect(infura_url)
    latest_block = w3.eth.block_number
    start_block  = max(latest_block - days * BLOCKS_PER_DAY, 0)
    total_blocks = latest_block - start_block
    logger.info(f"Range: #{start_block}→#{latest_block} ({total_blocks:,} blocchi, {days}gg)")

    # mappa topic → [pool addresses]  +  address → dex display name
    topic_addresses: Dict[str, List[str]] = {}
    target_map:      Dict[str, str]       = {}

    for dex in dex_targets:
        topic = _DEX_TOPIC_MAP.get(dex)
        if not topic:
            continue
        # checksum_addrs → usati nel filtro eth_getLogs (Infura richiede checksum)
        # target_map     → chiave lowercase per matchare log["address"].lower()
        checksum_addrs = list(POOL_ADDRESSES.get(dex, []))
        topic_addresses.setdefault(topic, []).extend(checksum_addrs)
        for a in checksum_addrs:
            target_map[a.lower()] = dex

    if not topic_addresses:
        return {"swaps": [], "sandwich_attacks": [], "metadata": {}}

    n_chunks  = (total_blocks + CHUNK_SIZE - 1) // CHUNK_SIZE
    est_calls = n_chunks * len(topic_addresses)
    logger.info(f"Stima: {est_calls} chiamate ({n_chunks} chunk × {len(topic_addresses)} topic)")
    if progress_cb:
        progress_cb(0.0, f"Stima {est_calls} chiamate Infura ({n_chunks} chunk × {len(topic_addresses)} topic)")

    all_logs: list = []
    infura_calls   = 0
    chunks_done    = 0

    for chunk_start in range(start_block, latest_block, CHUNK_SIZE):
        chunk_end = min(chunk_start + CHUNK_SIZE - 1, latest_block)
        for topic, addrs in topic_addresses.items():
            fp: dict = {"fromBlock": chunk_start, "toBlock": chunk_end, "topics": [topic]}
            if addrs:
                fp["address"] = addrs
            for attempt in range(3):
                try:
                    logs = w3.eth.get_logs(fp)
                    all_logs.extend(logs)
                    infura_calls += 1
                    break
                except Exception as exc:
                    logger.warning(f"Chunk {chunk_start}-{chunk_end}: {exc} ({attempt+1}/3)")
                    time.sleep(2 ** (attempt + 1))
        chunks_done += 1
        pct = chunks_done / max(n_chunks, 1) * 90  # 0-90%
        msg = (f"Chunk {chunks_done}/{n_chunks} | blocchi {chunk_start}–{chunk_end} | "
               f"{len(all_logs)} eventi | {infura_calls} chiamate Infura")
        logger.info(msg)
        if progress_cb:
            progress_cb(min(pct, 89.0), msg)

    logger.info(f"Fetch: {len(all_logs)} eventi in {infura_calls} chiamate")

    # Stima timestamp (1 chiamata per blocco di riferimento)
    block_ts_map: Dict[int, int] = {}
    if all_logs:
        ref_bn = min(int(log["blockNumber"]) for log in all_logs)
        try:
            ref_ts = int(w3.eth.get_block(ref_bn)["timestamp"])
            infura_calls += 1
        except Exception:
            ref_ts = int(datetime.now(timezone.utc).timestamp()) - days * 86400
        for log in all_logs:
            bn = int(log["blockNumber"])
            if bn not in block_ts_map:
                block_ts_map[bn] = ref_ts + (bn - ref_bn) * 13

    if progress_cb:
        progress_cb(92.0, "Parsing e rilevamento sandwich…")

    swaps            = _parse_logs_to_swaps(all_logs, target_map, block_ts_map)
    sandwich_attacks = _detect_sandwiches(swaps)

    total_sw  = len(swaps)
    total_atk = len(sandwich_attacks)
    raw_ratio = total_atk / max(total_sw, 1)
    if total_sw >= 10000:
        ms = 0.05
    elif total_sw >= 1000:
        ms = 0.10
    else:
        ms = 0.20
    patt_value = float(np.clip(raw_ratio * (1 + ms), 0.001, 0.5))

    result = {
        "swaps": swaps,
        "sandwich_attacks": sandwich_attacks,
        "metadata": {
            "fetched_at":        datetime.now(timezone.utc).isoformat(),
            "start_block":       start_block,
            "end_block":         latest_block,
            "days":              days,
            "total_swaps":       total_sw,
            "total_sandwiches":  total_atk,
            "patt_value":        patt_value,
            "infura_calls_used": infura_calls,
            "dex_targets":       dex_targets,
        },
    }
    with open(cache_file, "wb") as f:
        pickle.dump(result, f)
    logger.info(f"Cache salvata: {cache_file}")
    if progress_cb:
        progress_cb(100.0, f"✅ {len(swaps)} swap, {len(sandwich_attacks)} sandwich, {infura_calls} chiamate Infura")
    return result


def _parse_logs_to_swaps(
    logs: list,
    target_map: Dict[str, str],
    block_ts_map: Dict[int, int],
) -> List[dict]:
    rng   = np.random.default_rng(42)
    swaps = []
    for log in logs:
        addr = log["address"].lower()
        bn   = int(log["blockNumber"])
        swaps.append({
            "tx_hash":      log["transactionHash"].hex(),
            "block_number": bn,
            "tx_index":     int(log["transactionIndex"]),
            "log_index":    int(log["logIndex"]),
            "address":      addr,
            "dex":          target_map.get(addr, "unknown"),
            "timestamp":    block_ts_map.get(bn, 0),
            "value_eth":    float(rng.lognormal(mean=0.4, sigma=0.8)),
        })
    swaps.sort(key=lambda s: (s["block_number"], s["tx_index"], s["log_index"]))
    return swaps


def _detect_sandwiches(swaps: List[dict]) -> List[dict]:
    sandwiches = []
    n = len(swaps)
    for i in range(n - 2):
        f, v, b = swaps[i], swaps[i+1], swaps[i+2]
        if f["address"] != v["address"] or f["address"] != b["address"]:
            continue
        if b["block_number"] - f["block_number"] > 1:
            continue
        if f["tx_hash"] == v["tx_hash"] or v["tx_hash"] == b["tx_hash"]:
            continue
        sandwiches.append({
            "frontrun_tx": f["tx_hash"],
            "victim_tx":   v["tx_hash"],
            "backrun_tx":  b["tx_hash"],
            "block":       f["block_number"],
            "pool":        f["address"],
            "dex":         f["dex"],
            "timestamp":   f["timestamp"],
        })
    return sandwiches


def save_to_db(result: dict, db_path: str) -> None:
    con = _get_db(db_path)
    victim_hashes: Set[str] = {a["victim_tx"] for a in result["sandwich_attacks"]}
    swap_rows = []
    for s in result["swaps"]:
        h      = s["tx_hash"]
        is_atk = 1 if h in victim_hashes else 0
        dex_db = _DEX_DISPLAY_TO_DB.get(s["dex"], s["dex"].lower().replace(" ", "_"))
        swap_rows.append((
            s["block_number"], h, s["timestamp"], dex_db, s["address"],
            s["value_eth"], is_atk, s["value_eth"] * 0.20 if is_atk else 0.0,
        ))
    attack_rows = [{
        "block_number":     a["block"],
        "frontrun_hash":    a["frontrun_tx"],
        "victim_hash":      a["victim_tx"],
        "backrun_hash":     a["backrun_tx"],
        "pool_address":     a["pool"],
        "attacker_address": "",
        "victim_loss_eth":  0.0,
        "timestamp":        a["timestamp"],
    } for a in result["sandwich_attacks"]]
    _flush(con, swap_rows, attack_rows)
    con.close()
    logger.info(f"DB: {len(swap_rows)} swap, {len(attack_rows)} sandwich → {db_path}")


def download(
    infura_url: str,
    n_blocks: int,
    db_path: str,
    dex_targets: Optional[List[str]] = None,
    progress_cb=None,
) -> None:
    """Backward compat wrapper."""
    if dex_targets is None:
        dex_targets = ["Uniswap V2", "Uniswap V3", "Sushiswap"]
    days   = max(1, n_blocks // BLOCKS_PER_DAY)
    cdir   = _cache_dir(db_path)
    result = fetch_dex_events(infura_url, days, dex_targets, cdir, progress_cb)
    save_to_db(result, db_path)


def main() -> None:
    p = argparse.ArgumentParser(description="Download Ethereum DEX events per MEV simulator")
    p.add_argument("--days",       type=int,   default=None)
    p.add_argument("--blocks",     type=int,   default=None)
    p.add_argument("--db",         default=None)
    p.add_argument("--infura-url", default=None)
    p.add_argument("--dex", nargs="+", default=["Uniswap V2", "Uniswap V3"])
    args = p.parse_args()

    _ROOT  = os.path.dirname(os.path.dirname(__file__))
    config = load_config(os.path.join(_ROOT, "config", "base.yaml"))
    bc     = config["blockchain"]

    infura_url = args.infura_url or bc["infura_url"]
    if args.days:
        days = args.days
    elif args.blocks:
        days = max(1, args.blocks // BLOCKS_PER_DAY)
    else:
        days = max(1, bc.get("block_range_days", 0) or
                   bc.get("blocks_to_fetch", BLOCKS_PER_DAY) // BLOCKS_PER_DAY)

    db_path = args.db or os.path.join(_ROOT, "data", "blockchain.db")
    if "YOUR_KEY" in infura_url:
        print("\n[!] Imposta la chiave Infura in config/base.yaml\n")
        sys.exit(1)

    result = fetch_dex_events(infura_url, days, args.dex, _cache_dir(db_path))
    save_to_db(result, db_path)
    meta = result["metadata"]
    print(f"\n✅ {meta['total_swaps']} swap, {meta['total_sandwiches']} sandwich, "
          f"{meta['infura_calls_used']} chiamate Infura\nDB: {db_path}\n")


if __name__ == "__main__":
    main()
