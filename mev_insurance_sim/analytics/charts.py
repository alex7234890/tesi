"""
Static chart generation with matplotlib.
Called by the runner to produce PNG files after each simulation run.
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd


def _sr_color_zones(ax: plt.Axes, df: pd.DataFrame) -> None:
    """Shade solvency-ratio risk zones on an axis."""
    xmin, xmax = df["day"].min(), df["day"].max()
    ax.axhspan(0, 1.3, color="red",    alpha=0.08, label="High risk (SR<1.3)")
    ax.axhspan(1.3, 1.5, color="orange", alpha=0.08, label="Medium risk")
    ax.axhspan(1.5, ax.get_ylim()[1] if ax.get_ylim()[1] > 1.5 else 5,
               color="green", alpha=0.05, label="Healthy (SR≥1.5)")


def plot_pool_health(
    dfs: Dict[str, pd.DataFrame],
    output_dir: str,
    filename: str = "pool_health.png",
) -> str:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    for label, df in dfs.items():
        ax1.plot(df["day"], df["pool_balance_eth"], label=label, linewidth=1.5)
        ax2.plot(df["day"], df["solvency_ratio"],   label=label, linewidth=1.5)

    ax1.set_ylabel("Pool Balance (ETH)")
    ax1.set_title("Pool Health Over Time")
    ax1.legend()
    ax1.grid(alpha=0.3)

    # SR zones on ax2
    if dfs:
        sample_df = next(iter(dfs.values()))
        _sr_color_zones(ax2, sample_df)

    ax2.axhline(1.0, color="red",    linestyle="--", linewidth=1, label="SR = 1.0 (insolvency)")
    ax2.axhline(1.3, color="orange", linestyle=":",  linewidth=1)
    ax2.axhline(1.5, color="green",  linestyle=":",  linewidth=1)
    ax2.set_ylabel("Solvency Ratio")
    ax2.set_xlabel("Day")
    ax2.legend(fontsize=8)
    ax2.grid(alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, filename)
    plt.savefig(path, dpi=150)
    plt.close()
    return path


def plot_cashflow(df: pd.DataFrame, output_dir: str, filename: str = "cashflow.png") -> str:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    ax1.fill_between(df["day"], df["total_premiums_collected_eth"],
                     alpha=0.5, label="Premiums", color="green")
    ax1.fill_between(df["day"], df["total_payouts_eth"],
                     alpha=0.5, label="Payouts",  color="red")
    ax1.fill_between(df["day"], df["total_oracle_rewards_eth"],
                     alpha=0.5, label="Oracle rewards", color="blue")
    ax1.set_ylabel("Cumulative ETH")
    ax1.set_title("Cash Flow (Cumulative)")
    ax1.legend()
    ax1.grid(alpha=0.3)

    ax2.plot(df["day"], df["profit_eth"], color="purple", linewidth=1.5)
    ax2.axhline(0, color="black", linewidth=0.8, linestyle="--")
    ax2.set_ylabel("Running Profit (ETH)")
    ax2.set_xlabel("Day")
    ax2.grid(alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, filename)
    plt.savefig(path, dpi=150)
    plt.close()
    return path


def plot_claims(df: pd.DataFrame, output_dir: str, filename: str = "claims.png") -> str:
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    # Approval rate
    axes[0].plot(df["day"], df["claim_approval_rate"] * 100, color="steelblue")
    axes[0].set_title("Claim Approval Rate (%)")
    axes[0].set_xlabel("Day")
    axes[0].set_ylabel("%")
    axes[0].grid(alpha=0.3)

    # FraudScore distribution
    fraud_scores = df["avg_fraud_score"].dropna()
    axes[1].hist(fraud_scores, bins=30, color="salmon", edgecolor="white")
    axes[1].axvline(60, color="orange", linestyle="--", label="Captcha threshold")
    axes[1].axvline(80, color="red",    linestyle="--", label="Reject threshold")
    axes[1].set_title("FraudScore Distribution (daily avg)")
    axes[1].set_xlabel("Score")
    axes[1].legend(fontsize=8)
    axes[1].grid(alpha=0.3)

    # Claims submitted/approved/rejected
    axes[2].stackplot(
        df["day"],
        df["n_claims_approved"],
        df["n_claims_captcha"],
        df["n_claims_rejected"],
        labels=["Approved", "Captcha", "Rejected"],
        colors=["green", "gold", "red"],
        alpha=0.7,
    )
    axes[2].set_title("Claims by Decision")
    axes[2].set_xlabel("Day")
    axes[2].legend(fontsize=8)
    axes[2].grid(alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, filename)
    plt.savefig(path, dpi=150)
    plt.close()
    return path


def plot_user_distribution(df: pd.DataFrame, output_dir: str, filename: str = "users.png") -> str:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    ax1.stackplot(
        df["day"],
        df["n_users_bronze"],
        df["n_users_silver"],
        df["n_users_gold"],
        df["n_users_platinum"],
        labels=["Bronze", "Silver", "Gold", "Platinum"],
        colors=["#cd7f32", "#c0c0c0", "#ffd700", "#e5e4e2"],
        alpha=0.8,
    )
    ax1.set_title("User Tier Distribution Over Time")
    ax1.set_xlabel("Day")
    ax1.set_ylabel("Number of Users")
    ax1.legend(fontsize=8)
    ax1.grid(alpha=0.3)

    ax2.plot(df["day"], df["n_users_blacklisted"], color="black", linewidth=1.5)
    ax2.set_title("Blacklisted Users Over Time")
    ax2.set_xlabel("Day")
    ax2.set_ylabel("Count")
    ax2.grid(alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, filename)
    plt.savefig(path, dpi=150)
    plt.close()
    return path


def plot_oracle_network(df: pd.DataFrame, output_dir: str, filename: str = "oracles.png") -> str:
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))

    axes[0].plot(df["day"], df["n_oracles_watchlist"], color="orange")
    axes[0].set_title("Watchlist Entries Over Time")
    axes[0].set_xlabel("Day")
    axes[0].grid(alpha=0.3)

    axes[1].plot(df["day"], df["avg_oracle_divergence"], color="purple")
    axes[1].set_title("Average Oracle Divergence")
    axes[1].set_xlabel("Day")
    axes[1].grid(alpha=0.3)

    axes[2].plot(df["day"], df["n_oracles_slashed"].cumsum()
                 if hasattr(df["n_oracles_slashed"], "cumsum")
                 else df["n_oracles_slashed"],
                 color="red")
    axes[2].set_title("Cumulative Slashing Events")
    axes[2].set_xlabel("Day")
    axes[2].grid(alpha=0.3)

    plt.tight_layout()
    path = os.path.join(output_dir, filename)
    plt.savefig(path, dpi=150)
    plt.close()
    return path


def generate_all_charts(
    dfs: Dict[str, pd.DataFrame],
    output_dir: str,
    mode: int,
) -> List[str]:
    os.makedirs(output_dir, exist_ok=True)
    paths = []

    # Pool health — can overlay multiple coverage runs
    paths.append(plot_pool_health(dfs, output_dir))

    # Single-run charts (use first df)
    first_df = next(iter(dfs.values()))
    paths.append(plot_cashflow(first_df, output_dir))
    paths.append(plot_claims(first_df, output_dir))

    if mode == 2:
        paths.append(plot_user_distribution(first_df, output_dir))
        paths.append(plot_oracle_network(first_df, output_dir))

    print(f"  Charts saved → {output_dir}")
    return paths
