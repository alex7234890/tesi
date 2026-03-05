"""
Text and CSV reporting of simulation results.
"""
from __future__ import annotations

import os
from typing import Any, Dict

import pandas as pd


class Reporter:
    def __init__(self, output_dir: str = "data") -> None:
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    def print_summary(self, summary: Dict[str, Any], mode: int, coverage: str = "") -> None:
        tag = f"Mode {mode}" + (f" | Coverage: {coverage.upper()}" if coverage else "")
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"  SIMULATION SUMMARY — {tag}")
        print(sep)
        print(f"  Total profit (ETH)      : {summary['total_profit_eth']:>12.4f}")
        print(f"  Final balance (ETH)     : {summary['final_balance_eth']:>12.4f}")
        print(f"  Final solvency ratio    : {summary['final_solvency_ratio']:>12.4f}")
        print(f"  Avg claim approval rate : {summary['claim_approval_rate']:>11.1%}")
        survived = "YES ✓" if summary["pool_survived"] else "NO  ✗ — pool went insolvent"
        print(f"  Pool survived           : {survived}")
        print(f"  Simulation days         : {summary['total_days']:>12d}")
        print(sep)

    def save_csv(self, df: pd.DataFrame, label: str) -> str:
        name = f"results_{label}.csv".replace(" ", "_").lower()
        path = os.path.join(self.output_dir, name)
        df.to_csv(path, index=False)
        print(f"  Results saved → {path}")
        return path

    def save_summary_json(self, summary: Dict[str, Any], label: str) -> str:
        import json
        name = f"summary_{label}.json".replace(" ", "_").lower()
        path = os.path.join(self.output_dir, name)
        with open(path, "w") as f:
            json.dump(summary, f, indent=2)
        return path
