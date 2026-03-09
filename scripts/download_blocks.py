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
from collections import defaultdict, deque
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
        progress_cb(92.0, "Parsing log…")

    swaps = _parse_logs_to_swaps(all_logs, target_map, block_ts_map)

    total_sw = len(swaps)
    # Count swaps per DEX for metadata
    dex_counts: Dict[str, int] = {}
    for s in swaps:
        dex_counts[s["dex"]] = dex_counts.get(s["dex"], 0) + 1

    result = {
        "swaps":             swaps,
        "sandwich_attacks":  [],   # kept for schema compat; detection no longer run
        "metadata": {
            "fetched_at":        datetime.now(timezone.utc).isoformat(),
            "start_block":       start_block,
            "end_block":         latest_block,
            "total_blocks":      total_blocks,
            "total_chunks":      n_chunks,
            "days":              days,
            "total_swaps":       total_sw,
            "dex_counts":        dex_counts,
            "infura_calls_used": infura_calls,
            "dex_targets":       dex_targets,
        },
    }
    with open(cache_file, "wb") as f:
        pickle.dump(result, f)
    logger.info(f"Cache salvata: {cache_file}")
    if progress_cb:
        progress_cb(100.0, f"✅ {total_sw:,} swap | {infura_calls} chiamate Infura")
    return result


def _decode_swap_amounts(log: dict, dex: str):
    """
    Decode token amounts directly from Swap log data — no extra RPC calls.

    Uniswap V2 / Sushiswap  (topic: SWAP_TOPIC_V2)
        event Swap(address indexed sender,
                   uint256 amount0In, uint256 amount1In,
                   uint256 amount0Out, uint256 amount1Out,
                   address indexed to)
        Non-indexed data: [amount0In | amount1In | amount0Out | amount1Out]
        4 × uint256 = 128 bytes.

    Uniswap V3  (topic: SWAP_TOPIC_V3)
        event Swap(address indexed sender, address indexed recipient,
                   int256 amount0, int256 amount1,
                   uint160 sqrtPriceX96, uint128 liquidity, int24 tick)
        Non-indexed data starts with amount0, amount1 (each int256 = 32 bytes).
        Positive = token flows INTO the pool; negative = token flows OUT.

    Returns (amount_in, amount_out, price, direction) where:
        price     = amount_in / amount_out   (cost per output unit;
                    higher means a worse rate for the swapper)
        direction = "0_to_1" | "1_to_0" | "unknown"
    """
    try:
        raw = log.get("data", b"")
        if isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
        elif isinstance(raw, str):
            data = bytes.fromhex(raw.removeprefix("0x"))
        else:
            return 0.0, 0.0, 0.0, "unknown"

        if dex in ("Uniswap V2", "Sushiswap"):
            # Need at least 4 × 32 bytes = 128 bytes of non-indexed data.
            if len(data) < 128:
                return 0.0, 0.0, 0.0, "unknown"
            a0in  = int.from_bytes(data[0:32],   "big")
            a1in  = int.from_bytes(data[32:64],  "big")
            a0out = int.from_bytes(data[64:96],  "big")
            a1out = int.from_bytes(data[96:128], "big")
            # Exactly one of the two "in" values should be non-zero.
            if a0in > 0 and a1out > 0:
                direction, amount_in, amount_out = "0_to_1", a0in, a1out
            elif a1in > 0 and a0out > 0:
                direction, amount_in, amount_out = "1_to_0", a1in, a0out
            else:
                return 0.0, 0.0, 0.0, "unknown"

        elif dex == "Uniswap V3":
            # First 64 bytes = amount0 (int256) and amount1 (int256).
            if len(data) < 64:
                return 0.0, 0.0, 0.0, "unknown"

            def _int256(b: bytes) -> int:
                # Two's-complement decode for int256.
                v = int.from_bytes(b, "big")
                return v - (1 << 256) if v >= (1 << 255) else v

            a0 = _int256(data[0:32])
            a1 = _int256(data[32:64])
            # Positive delta  = token flows INTO the pool (spent by the swapper).
            # Negative delta = token flows OUT of the pool (received by the swapper).
            if a0 > 0 and a1 < 0:
                direction, amount_in, amount_out = "0_to_1", a0, -a1
            elif a1 > 0 and a0 < 0:
                direction, amount_in, amount_out = "1_to_0", a1, -a0
            else:
                return 0.0, 0.0, 0.0, "unknown"
        else:
            return 0.0, 0.0, 0.0, "unknown"

        if amount_out == 0:
            return float(amount_in), 0.0, 0.0, direction

        # price = cost per output unit (higher value ↔ worse rate for the swapper).
        price = float(amount_in) / float(amount_out)
        return float(amount_in), float(amount_out), price, direction

    except Exception:
        return 0.0, 0.0, 0.0, "unknown"


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
        dex  = target_map.get(addr, "unknown")
        # Decode swap direction, amounts, and approximate price from log data.
        # This uses only data already present in the eth_getLogs response —
        # no additional RPC calls.
        amount_in, amount_out, price, direction = _decode_swap_amounts(log, dex)
        swaps.append({
            "tx_hash":      log["transactionHash"].hex(),
            "block_number": bn,
            "tx_index":     int(log["transactionIndex"]),
            "log_index":    int(log["logIndex"]),
            "address":      addr,
            "dex":          dex,
            "timestamp":    block_ts_map.get(bn, 0),
            # Synthetic ETH value (token prices are not derivable from logs alone).
            "value_eth":    float(rng.lognormal(mean=0.4, sigma=0.8)),
            # Decoded from log data — used by the sandwich detector.
            "amount_in":    amount_in,
            "amount_out":   amount_out,
            "price":        price,       # amount_in / amount_out; higher = worse rate
            "direction":    direction,   # "0_to_1" | "1_to_0" | "unknown"
        })
    swaps.sort(key=lambda s: (s["block_number"], s["tx_index"], s["log_index"]))
    return swaps


def _detect_sandwiches(swaps: List[dict]) -> List[dict]:
    """
    Research-grade sandwich detection using a per-pool sliding window.

    Complexity: O(n) over swaps — the inner scan is bounded by WINDOW_SIZE^2 = 36.
    No additional RPC calls: all signals are derived from log data decoded in
    _parse_logs_to_swaps / _decode_swap_amounts.

    ── Sliding-window structure ────────────────────────────────────────────────
    Swaps are processed in sorted order (block, tx_index, log_index).
    For each pool address a deque of at most WINDOW_SIZE recent swaps is kept.
    When a new swap arrives it is appended; the oldest entry is auto-evicted
    once the window exceeds WINDOW_SIZE.  Because swaps are ordered, a swap
    that falls outside the window is too far back to be part of any new sandwich.

    ── Pattern: frontrun  victim(s)  backrun ──────────────────────────────────
    All three roles must belong to the same pool.  1–3 victims are allowed between
    the attacker's two legs.

    ── Nine filters applied per candidate (frontrun f, victims, backrun b) ────

    1. Block distance  — b.block − f.block ≤ 1
       MEV bots place both legs in the same or consecutive block.

    2. Direction consistency
       Swap direction is decoded from Swap log amounts (Step 1 in spec):
           V2/Sushi: amount0In>0, amount1Out>0  → "0_to_1" (buy token1 with token0)
                     amount1In>0, amount0Out>0  → "1_to_0"
           V3:       amount0>0 (into pool)       → "0_to_1"
                     amount1>0 (into pool)       → "1_to_0"
       Valid sandwich: f.dir ≠ b.dir  AND  every victim.dir == f.dir.
       (Attacker opens with one direction and closes with the opposite.)

    3. Price impact — victim's effective price must be worse than frontrun's.
       Price is estimated as amount_in / amount_out (cost per output unit).
       BUY sandwich (f.dir = "0_to_1"):
           Frontrun pushes pool price up; victim pays more → price_f < price_v1.
       SELL sandwich (f.dir = "1_to_0"):
           Frontrun pushes pool price down; victim receives less → price_f > price_v1.

    4. Multi-victim slippage progression
       Each successive victim must get an even worse price, confirming cumulative
       pool-state manipulation:
           BUY:  price_v1 ≤ price_v2 ≤ …
           SELL: price_v1 ≥ price_v2 ≥ …

    5. Backrun price reversion
       Backrun closes the attacker's position, partially reversing the price:
           BUY:  price_b < price_v1  (backrun SELL drives price back down)
           SELL: price_b > price_v1  (backrun BUY drives price back up)

    6. Round-trip size consistency
       Attacker buys and sells a roughly equal position.
       Proxy: max(amount_in, amount_out) per swap (token-agnostic magnitude).
           0.3 ≤ back_size / front_size ≤ 3.0

    7. Gas-ordering heuristic (MEV bot signal)
       MEV bots submit both legs in the same block with very tight ordering.
       Checked only when f.block == b.block:
           0 < b.tx_index − f.tx_index ≤ MAX_TI_DIST (= 5)
       Skipped across block boundaries (tx_index is not comparable between blocks).

    8. Transaction uniqueness — f.tx_hash ≠ b.tx_hash.

    9. At least one victim swap between f and b.
    """
    WINDOW_SIZE    = 6
    MAX_BLOCK_DIST = 1
    MAX_TI_DIST    = 5    # same-block tx_index gap; captures up to 4 victims + both legs
    MAX_VICTIMS    = 3
    ROUND_TRIP_MIN = 0.3
    ROUND_TRIP_MAX = 3.0

    # Per-pool sliding window (auto-bounded to WINDOW_SIZE by deque maxlen).
    pool_windows: Dict[str, deque] = defaultdict(lambda: deque(maxlen=WINDOW_SIZE))
    sandwiches: List[dict] = []
    seen_pairs: Set        = set()   # (frontrun_tx_hash, backrun_tx_hash)

    for swap in swaps:
        pool = swap["address"]
        win  = pool_windows[pool]
        win.append(swap)

        # Need at least 3 swaps in this pool's window before any pattern is possible.
        if len(win) < 3:
            continue

        # ── Treat the newest swap as the candidate backrun ────────────────────
        # We scan all earlier entries as candidate frontruns, with victims in between.
        # This single-pass approach is O(WINDOW_SIZE²) = O(1) per swap → O(n) overall.
        win_list = list(win)   # snapshot; len ≤ WINDOW_SIZE
        b = win_list[-1]       # candidate backrun

        if b["direction"] == "unknown":
            continue

        b_p    = b["price"]
        b_size = max(b["amount_in"], b["amount_out"])

        for fi, f in enumerate(win_list[:-2]):

            # ── Filter 1: block distance ─────────────────────────────────────
            blk_dist = b["block_number"] - f["block_number"]
            if blk_dist > MAX_BLOCK_DIST or blk_dist < 0:
                continue

            # ── Filter 2: direction consistency ──────────────────────────────
            f_dir = f["direction"]
            if f_dir == "unknown":
                continue
            if f_dir == b["direction"]:   # same direction → not an attacker round-trip
                continue

            # ── Filter 8: transaction uniqueness ─────────────────────────────
            if f["tx_hash"] == b["tx_hash"]:
                continue

            # ── Filter 7: gas-ordering heuristic (same-block only) ───────────
            # MEV bots place both legs tightly in the same block.
            # tx_index is not comparable across blocks, so skip for blk_dist = 1.
            if blk_dist == 0:
                ti_delta = b["tx_index"] - f["tx_index"]
                if ti_delta <= 0 or ti_delta > MAX_TI_DIST:
                    continue

            # ── Filter 9: collect victims between f and b ────────────────────
            # Victims must share the frontrun direction and be distinct transactions
            # from both attacker legs.
            victims = [
                s for s in win_list[fi + 1 : -1]
                if (s["direction"] == f_dir
                    and s["tx_hash"] != f["tx_hash"]
                    and s["tx_hash"] != b["tx_hash"])
            ]
            if not victims:
                continue
            victims = victims[:MAX_VICTIMS]   # cap at 3
            v1      = victims[0]
            v1_p    = v1["price"]

            # ── Filter 3: price impact ───────────────────────────────────────
            # Victim must receive a worse price than the frontrun.
            # price = amount_in / amount_out (higher ↔ worse for the swapper).
            # BUY  (0_to_1): frontrun buys, drives pool price up  → victim pays more
            #                 → price_f  <  price_v1
            # SELL (1_to_0): frontrun sells, drives pool price down → victim gets less
            #                 → price_f  >  price_v1
            f_p = f["price"]
            if f_p > 0.0 and v1_p > 0.0:
                if f_dir == "0_to_1":
                    if not (f_p < v1_p):
                        continue
                else:
                    if not (f_p > v1_p):
                        continue

            # ── Filter 4: multi-victim slippage progression ──────────────────
            # Every subsequent victim must get an even worse price, proving that
            # each swap worsens pool state monotonically.
            if len(victims) > 1:
                ok = True
                for vi in range(1, len(victims)):
                    p_prev = victims[vi - 1]["price"]
                    p_cur  = victims[vi]["price"]
                    if p_prev <= 0.0 or p_cur <= 0.0:
                        continue   # skip pairs with unknown price
                    if f_dir == "0_to_1" and p_cur < p_prev:
                        ok = False
                        break
                    if f_dir == "1_to_0" and p_cur > p_prev:
                        ok = False
                        break
                if not ok:
                    continue

            # ── Filter 5: backrun price reversion ────────────────────────────
            # Backrun closes the attacker's position; pool price partially reverts.
            # BUY:  attacker's SELL backrun drives price back down → price_b < price_v1
            # SELL: attacker's BUY  backrun drives price back up   → price_b > price_v1
            if b_p > 0.0 and v1_p > 0.0:
                if f_dir == "0_to_1":
                    if not (b_p < v1_p):
                        continue
                else:
                    if not (b_p > v1_p):
                        continue

            # ── Filter 6: round-trip size consistency ────────────────────────
            # Attacker opens and closes a position of similar magnitude.
            # max(amount_in, amount_out) is a token-agnostic size proxy.
            f_size = max(f["amount_in"], f["amount_out"])
            if f_size > 0.0 and b_size > 0.0:
                ratio = b_size / f_size
                if not (ROUND_TRIP_MIN <= ratio <= ROUND_TRIP_MAX):
                    continue

            # ── Dedup ─────────────────────────────────────────────────────────
            key = (f["tx_hash"], b["tx_hash"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)

            # ── Record sandwich ───────────────────────────────────────────────
            victims_info = [
                {
                    "tx_hash":      v["tx_hash"],
                    "block_number": v["block_number"],
                    "tx_index":     v["tx_index"],
                    "address":      v["address"],
                    "price":        v["price"],
                }
                for v in victims
            ]
            sandwiches.append({
                # ── Backward-compatible flat fields (used by save_to_db) ──────
                "frontrun_tx":    f["tx_hash"],
                "frontrun_block": f["block_number"],
                "victim_tx":      victims[0]["tx_hash"],   # primary (first) victim
                "victim_block":   victims[0]["block_number"],
                "backrun_tx":     b["tx_hash"],
                "backrun_block":  b["block_number"],
                "block":          f["block_number"],
                "pool":           f["address"],
                "dex":            f["dex"],
                "timestamp":      f["timestamp"],
                # ── Nested dicts for dashboard visualisation ──────────────────
                "front": {
                    "tx_hash":      f["tx_hash"],
                    "block_number": f["block_number"],
                    "tx_index":     f["tx_index"],
                    "address":      f["address"],
                    "price":        f["price"],
                },
                "victim": {
                    "tx_hash":      victims[0]["tx_hash"],
                    "block_number": victims[0]["block_number"],
                    "tx_index":     victims[0]["tx_index"],
                    "address":      victims[0]["address"],
                    "price":        victims[0]["price"],
                },
                "back": {
                    "tx_hash":      b["tx_hash"],
                    "block_number": b["block_number"],
                    "tx_index":     b["tx_index"],
                    "address":      b["address"],
                    "price":        b["price"],
                },
                # ── Multi-victim extension ────────────────────────────────────
                "victims":   victims_info,
                "n_victims": len(victims),
                "direction": f_dir,   # "0_to_1" (buy sandwich) | "1_to_0" (sell sandwich)
            })

    return sandwiches


def save_to_db(result: dict, db_path: str) -> None:
    con = _get_db(db_path)
    # Collect ALL victim hashes, including multi-victim sandwiches.
    victim_hashes: Set[str] = set()
    for a in result["sandwich_attacks"]:
        for v in a.get("victims", [{"tx_hash": a.get("victim_tx", "")}]):
            h = v.get("tx_hash", "")
            if h:
                victim_hashes.add(h)
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
    print(f"\n✅ {meta['total_swaps']:,} swap | {meta['infura_calls_used']} chiamate Infura\nDB: {db_path}\n")


if __name__ == "__main__":
    main()
