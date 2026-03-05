"""
MEV Insurance Protocol — Streamlit Dashboard (UI Overhaul v2).

Run independently:
    streamlit run dashboard/app.py

Or launched automatically by runner.py after a simulation completes.
"""
from __future__ import annotations

import os
import sys
import math

import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go

# Make sure the package root is importable
_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.config_loader import load_config
from runner import run_single

_DB_PATH = os.path.join(_ROOT, "data", "blockchain.db")

# Coverage level mapping
_COVERAGE_LABELS   = ["Low", "Medium", "High"]
_COVERAGE_INTERNAL = {"Low": "low", "Medium": "medium", "High": "high"}
_COVERAGE_REIMB    = {"Low": "50%", "Medium": "70%", "High": "100%"}
_COVERAGE_FCOV     = {"Low": 0.70, "Medium": 0.90, "High": 1.00}

_DEX_OPTIONS = ["Uniswap V2", "Uniswap V3", "Sushiswap", "Curve"]

# =========================================================================
# Page configuration
# =========================================================================
st.set_page_config(
    page_title="MEV Insurance Simulator",
    page_icon="🛡️",
    layout="wide",
)

# =========================================================================
# Session state initialisation
# =========================================================================
if "results" not in st.session_state:
    st.session_state["results"] = {}
if "summaries" not in st.session_state:
    st.session_state["summaries"] = {}
if "collectors" not in st.session_state:
    st.session_state["collectors"] = {}


# =========================================================================
# Sidebar — ALL widgets always rendered; mode-specific ones use disabled=
# NOTE: Every widget has an explicit, stable key= to prevent React DOM errors.
# =========================================================================
st.sidebar.title("🛡️ MEV Insurance Simulator")
st.sidebar.markdown("---")

# ---- Mode selection ----
mode = st.sidebar.radio(
    "Mode",
    [1, 2],
    format_func=lambda m: "Mode 1 — Real Chain" if m == 1 else "Mode 2 — Synthetic",
    index=1,
    key="mode_select",
)
is_mode1 = (mode == 1)
is_mode2 = (mode == 2)

st.sidebar.markdown("---")
st.sidebar.markdown("### Simulation Parameters")

duration_days = st.sidebar.number_input(
    "N days simulation", min_value=1, max_value=365, value=30, step=1,
    key="duration_days",
)

# swaps_per_day always rendered; disabled in Mode 1
swaps_per_day = st.sidebar.number_input(
    "N swaps/day (Mode 2 only)", min_value=10, max_value=10000, value=100, step=10,
    key="swaps_per_day",
    disabled=is_mode1,
    help="Poisson mean swaps generated per day in Mode 2. Ignored in Mode 1.",
)

coverage_label = st.sidebar.selectbox(
    "Coverage Level",
    _COVERAGE_LABELS,
    index=1,
    key="coverage_label",
    help=(
        "Low → 50% reimbursement, Fcov=0.70 | "
        "Medium → 70% reimbursement, Fcov=0.90 | "
        "High → 100% reimbursement, Fcov=1.00"
    ),
)
coverage = _COVERAGE_INTERNAL[coverage_label]

st.sidebar.markdown("---")

# ---- Protocol Parameters (Advanced) — always rendered ----
with st.sidebar.expander("⚙️ Protocol Parameters — Advanced"):
    mbase = st.slider(
        "Mbase (base margin)", 0.05, 0.50, 0.20, step=0.01,
        key="param_mbase",
        help="Base margin applied to every premium",
    )
    loss_pct = st.slider(
        "L% (avg loss per attack)", 0.05, 0.40, 0.20, step=0.01,
        key="param_loss_pct",
        help="Average fraction of swap value lost in a sandwich attack",
    )
    false_negative_rate = st.slider(
        "E — False Negative Rate (FNR)", 0.01, 0.50, 0.20, step=0.01,
        key="param_fnr",
        help="Fraction of fraudulent claims that slip through detection",
    )
    sr_threshold_high = st.slider(
        "Solvency threshold HIGH (healthy)", 1.3, 2.0, 1.50, step=0.05,
        key="param_sr_high",
        help="SR above this → Madj = 0.00 (healthy, no surcharge)",
    )
    sr_threshold_med = st.slider(
        "Solvency threshold MED (medium risk)", 1.0, 1.5, 1.30, step=0.05,
        key="param_sr_med",
        help="SR between MED and HIGH → Madj = 0.05",
    )
    oracle_reward_per_claim = st.number_input(
        "Oracle reward per claim (ETH)", value=0.002, format="%.4f",
        key="param_oracle_reward",
        help="ETH paid to oracle for each processed claim",
    )
    captcha_reward = st.number_input(
        "CAPTCHA reward (ETH)", value=0.001, format="%.4f",
        key="param_captcha_reward",
        help="ETH paid to oracle for each CAPTCHA verification",
    )
    initial_pool_balance = st.number_input(
        "Initial Pool Balance (ETH)", min_value=10.0, max_value=10000.0, value=100.0,
        step=10.0,
        key="param_pool_balance",
    )
    st.markdown("**Slashing distribution** *(read-only)*")
    st.markdown("Pool 60% / Reporter 25% / Jury 15%")

st.sidebar.markdown("---")

# ---- Mode 1 expander — always in DOM; widgets disabled when Mode 2 ----
with st.sidebar.expander("🔗 Mode 1 — Real Chain Parameters"):
    infura_api_key = st.text_input(
        "Infura API Key", value="", type="password",
        placeholder="Enter Infura project ID",
        key="m1_infura_key",
        disabled=is_mode2,
    )
    block_range_days = st.number_input(
        "Block range (days to fetch)", min_value=1, max_value=7, value=2, step=1,
        key="m1_block_range",
        disabled=is_mode2,
    )
    dex_targets = st.multiselect(
        "DEX contracts monitored",
        _DEX_OPTIONS,
        default=["Uniswap V2", "Uniswap V3"],
        key="m1_dex_targets",
        disabled=is_mode2,
    )

# ---- Mode 2 expander — always in DOM; widgets disabled when Mode 1 ----
with st.sidebar.expander("🔬 Mode 2 — Synthetic Parameters"):
    patt_file_path = st.text_input(
        "Patt file path", value="data/patt_historical.csv",
        key="m2_patt_file",
        disabled=is_mode1,
    )
    seed = st.number_input(
        "Random seed", min_value=0, max_value=99999, value=42, step=1,
        key="m2_seed",
        disabled=is_mode1,
    )
    n_synthetic_users = st.number_input(
        "N synthetic users (initial)", min_value=5, max_value=500, value=50, step=5,
        key="m2_n_users",
        disabled=is_mode1,
    )
    fraud_rate = st.slider(
        "Fraud rate (users)", 0.0, 0.30, 0.05, step=0.01,
        key="m2_fraud_rate",
        disabled=is_mode1,
    )
    st.markdown("**Tier distribution (initial users)**")
    tier_bronze_pct = st.slider(
        "Bronze %", 0, 100, 70, step=5,
        key="m2_tier_bronze",
        disabled=is_mode1,
    )
    tier_silver_pct = st.slider(
        "Silver %", 0, 100, 20, step=5,
        key="m2_tier_silver",
        disabled=is_mode1,
    )
    tier_gold_pct = st.slider(
        "Gold %", 0, 100, 8, step=1,
        key="m2_tier_gold",
        disabled=is_mode1,
    )
    tier_platinum_pct = st.slider(
        "Platinum %", 0, 100, 2, step=1,
        key="m2_tier_platinum",
        disabled=is_mode1,
    )
    tier_total = tier_bronze_pct + tier_silver_pct + tier_gold_pct + tier_platinum_pct
    if is_mode2 and tier_total != 100:
        st.warning(
            f"⚠️ Tier percentages sum to {tier_total}% — must be 100%. "
            "Adjust the sliders above."
        )

st.sidebar.markdown("---")
run_btn    = st.sidebar.button("▶ Run Simulation", type="primary", key="run_btn")
export_btn = st.sidebar.button("📥 Export CSV", key="export_btn")


# =========================================================================
# Helpers
# =========================================================================

def _safe_yrange(values: list):
    """Return a [min, max] y-range with at least 5% spread around mean."""
    arr = [v for v in values if v is not None and not math.isnan(v)]
    if not arr:
        return None
    mn, mx = min(arr), max(arr)
    mean_v = sum(arr) / len(arr)
    if mean_v != 0 and (mx - mn) < 0.01 * abs(mean_v):
        return [mean_v * 0.95, mean_v * 1.05]
    return None


def _apply_yrange(fig, values: list):
    rng = _safe_yrange(values)
    if rng:
        fig.update_layout(yaxis_range=rng)
    return fig


# =========================================================================
# Simulation Preview — builds a single markdown string (no nested widgets)
# so the DOM structure never changes between renders.
# =========================================================================

def build_preview_markdown(
    mode: int,
    duration_days: int,
    swaps_per_day: int,
    coverage_label: str,
    mbase: float,
    loss_pct: float,
    false_negative_rate: float,
    sr_threshold_high: float,
    sr_threshold_med: float,
    oracle_reward_per_claim: float,
    captcha_reward: float,
    initial_pool_balance: float,
    infura_api_key: str,
    block_range_days: int,
    dex_targets: list,
    patt_file_path: str,
    seed: int,
    n_synthetic_users: int,
    tier_bronze_pct: float,
    tier_silver_pct: float,
    tier_gold_pct: float,
    tier_platinum_pct: float,
    fraud_rate: float,
) -> str:
    """Return a single markdown string summarising the upcoming simulation."""
    fcov     = _COVERAGE_FCOV[coverage_label]
    reimb    = _COVERAGE_REIMB[coverage_label]
    fnr_denom = 1.0 - false_negative_rate
    fnr_mult  = false_negative_rate / fnr_denom if fnr_denom > 0 else 999.0

    blocks_per_day   = 6646
    est_infura_calls = int(block_range_days * blocks_per_day * 0.1)
    infura_warn      = (
        f"\n> ⚠️ Estimated {est_infura_calls:,} Infura calls — "
        "consider using cached data or reducing block range.\n"
        if est_infura_calls > 1000 else ""
    )

    mode_str = "Mode 1 — Real Chain" if mode == 1 else "Mode 2 — Synthetic"

    # ---- CONFIGURATION block ----
    cfg_block = (
        "### 📋 Simulation Preview — What will happen\n\n"
        "**CONFIGURATION**\n\n"
        f"| Parameter | Value |\n"
        f"|---|---|\n"
        f"| Mode | {mode_str} |\n"
        f"| Duration | {duration_days} days |\n"
        f"| Coverage Level | {coverage_label} — {reimb} reimbursement, Fcov={fcov:.2f} |\n"
        f"| Initial Pool Balance | {initial_pool_balance:.0f} ETH |\n\n"
    )

    # ---- DATA SOURCES block ----
    if mode == 1:
        key_masked = (infura_api_key[:4] + "****") if len(infura_api_key) > 4 else "(not set)"
        dex_list   = ", ".join(dex_targets) if dex_targets else "(none selected)"
        patt_exists_note = ""
        data_block = (
            "---\n\n"
            "**DATA SOURCES**\n\n"
            "- Swap data: fetched from **Infura** via web3.py\n"
            f"  - Endpoint: `wss://mainnet.infura.io/ws/v3/{key_masked}`\n"
            f"  - Block range: last **{block_range_days}** days "
            f"(~{block_range_days * blocks_per_day:,} blocks)\n"
            f"  - DEX contracts monitored: `{dex_list}`\n"
            f"  - Estimated Infura calls: ~**{est_infura_calls:,}** "
            "*(blocks × 10% DEX filter)*\n"
            f"{infura_warn}\n"
        )
    else:
        patt_exists = os.path.isfile(os.path.join(_ROOT, patt_file_path))
        patt_status = "✓ file found" if patt_exists else "✗ not found → Patt = random ~5% ±2%"
        data_block = (
            "---\n\n"
            "**DATA SOURCES**\n\n"
            "- Swap data: **synthetically generated**\n"
            f"  - N swaps/day (Poisson mean): **{swaps_per_day}**\n"
            f"  - Random seed: **{seed}**\n"
            f"  - Patt file: `{patt_file_path}` — *{patt_status}*\n"
            f"  - User distribution: Bronze **{tier_bronze_pct}%** / "
            f"Silver **{tier_silver_pct}%** / "
            f"Gold **{tier_gold_pct}%** / "
            f"Platinum **{tier_platinum_pct}%**\n\n"
        )

    # ---- PREMIUM FORMULA block ----
    patt_display = "fetched daily from chain" if mode == 1 else "loaded from Patt file + noise"
    formula_block = (
        "---\n\n"
        "**PREMIUM FORMULA**\n\n"
        "```\n"
        "P = V × [(Patt × L%) + (Tint × E/(1−E)) / (Vbase × 1000)] × (1+M) × Fcov\n"
        "```\n\n"
        f"| Parameter | Value |\n"
        f"|---|---|\n"
        f"| Patt | {patt_display} |\n"
        f"| L% | {loss_pct:.2%} |\n"
        f"| E (FNR) | {false_negative_rate:.2%} → E/(1−E) = **{fnr_mult:.4f}** |\n"
        f"| Mbase | {mbase:.2%} |\n"
        f"| M_adj | 0.00 (SR≥{sr_threshold_high}) / 0.05 (SR≥{sr_threshold_med}) / 0.10 (SR<{sr_threshold_med}) |\n"
        f"| Fcov | {fcov:.2f} (Coverage {coverage_label}) |\n"
        f"| Tint, Vbase | updated each simulated day from simulation state |\n\n"
    )

    # ---- WHAT WILL BE SIMULATED block ----
    if mode == 2:
        total_swaps      = duration_days * swaps_per_day
        exp_fraud_claims = int(duration_days * fraud_rate * swaps_per_day)
        exp_attacks      = int(duration_days * 0.05 * swaps_per_day)
        sim_block = (
            "---\n\n"
            "**WHAT WILL BE SIMULATED**\n\n"
            f"- **{duration_days}** days × ~**{swaps_per_day}** swaps/day = ~**{total_swaps:,}** swap events\n"
            f"- ~**{exp_fraud_claims:,}** fraudulent claims expected (fraud_rate={fraud_rate:.0%})\n"
            f"- ~**{exp_attacks:,}** sandwich attacks expected (~5% Patt baseline)\n"
            "- Tier upgrade checks: automatic daily\n"
            "- Slashing: triggered if oracle divergence ≥ 10 pts (2 occurrences)\n\n"
        )
    else:
        sim_block = (
            "---\n\n"
            "**WHAT WILL BE SIMULATED**\n\n"
            "- Real historical swaps from Ethereum mainnet\n"
            "- Tier system: **DISABLED** (not meaningful with real data)\n"
            f"- Coverage scenario: **{coverage_label}** → {reimb} reimbursement\n\n"
        )

    # ---- OUTPUT block ----
    output_block = (
        "---\n\n"
        "**OUTPUT**\n\n"
        "- SQLite DB: `simulation_results.db`"
        " (tables: swaps, claims, daily_stats, oracle_actions)\n"
        "- CSV export: `results_[timestamp].csv`\n"
        "- Dashboard: day-by-day explorer + aggregate charts\n"
    )

    return cfg_block + data_block + formula_block + sim_block + output_block


# =========================================================================
# Build config from UI parameters
# =========================================================================

def _build_config(
    mode: int,
    duration_days: int,
    swaps_per_day: int,
    coverage: str,
    mbase: float,
    loss_pct: float,
    false_negative_rate: float,
    sr_threshold_high: float,
    sr_threshold_med: float,
    oracle_reward_per_claim: float,
    captcha_reward: float,
    initial_pool_balance: float,
    infura_api_key: str,
    block_range_days: int,
    seed: int,
    n_synthetic_users: int,
    tier_bronze_pct: float,
    tier_silver_pct: float,
    tier_gold_pct: float,
    tier_platinum_pct: float,
    fraud_rate: float,
) -> dict:
    cfg_path = os.path.join(
        _ROOT, "config",
        "mode1_realchain.yaml" if mode == 1 else "mode2_synthetic.yaml",
    )
    config = load_config(cfg_path)

    config["simulation"]["duration_days"] = int(duration_days)
    config["simulation"]["seed"]          = int(seed)

    config["pool"]["mbase"]                              = float(mbase)
    config["pool"]["initial_balance_eth"]                = float(initial_pool_balance)
    config["pool"]["solvency_thresholds"]["high_risk"]   = float(sr_threshold_med)
    config["pool"]["solvency_thresholds"]["medium_risk"] = float(sr_threshold_high)

    config["market"]["loss_pct_mean"] = float(loss_pct)

    config["fraud_detection"]["false_negative_rate"] = float(false_negative_rate)
    config["fraud_detection"]["user_fraud_rate"]     = float(fraud_rate)

    config["oracles"]["reward_per_claim_eth"] = float(oracle_reward_per_claim)
    config["oracles"]["reward_captcha_eth"]   = float(captcha_reward)

    if mode == 2:
        config["users"]["initial_count"]       = int(n_synthetic_users)
        config["users"]["fraud_rate"]          = float(fraud_rate)
        config["users"]["swap_frequency_mean"] = max(
            1, int(swaps_per_day / max(n_synthetic_users, 1))
        )
        config["users"]["initial_tier_distribution"] = {
            "bronze":   float(tier_bronze_pct)   / 100.0,
            "silver":   float(tier_silver_pct)   / 100.0,
            "gold":     float(tier_gold_pct)     / 100.0,
            "platinum": float(tier_platinum_pct) / 100.0,
        }

    if mode == 1 and infura_api_key:
        config["blockchain"]["infura_url"] = (
            f"wss://mainnet.infura.io/ws/v3/{infura_api_key}"
        )
        config["blockchain"]["block_range_days"] = int(block_range_days)

    return config


# =========================================================================
# Run simulation
# =========================================================================

if run_btn:
    if is_mode2:
        tier_total_check = tier_bronze_pct + tier_silver_pct + tier_gold_pct + tier_platinum_pct
        if tier_total_check != 100:
            st.error(
                f"⚠️ Tier distribution sums to {tier_total_check}% instead of 100%. "
                "Fix the sliders in 'Mode 2 — Synthetic Parameters' before running."
            )
            st.stop()

    config = _build_config(
        mode=mode,
        duration_days=duration_days,
        swaps_per_day=swaps_per_day,
        coverage=coverage,
        mbase=mbase,
        loss_pct=loss_pct,
        false_negative_rate=false_negative_rate,
        sr_threshold_high=sr_threshold_high,
        sr_threshold_med=sr_threshold_med,
        oracle_reward_per_claim=oracle_reward_per_claim,
        captcha_reward=captcha_reward,
        initial_pool_balance=initial_pool_balance,
        infura_api_key=infura_api_key,
        block_range_days=block_range_days,
        seed=seed,
        n_synthetic_users=n_synthetic_users,
        tier_bronze_pct=tier_bronze_pct,
        tier_silver_pct=tier_silver_pct,
        tier_gold_pct=tier_gold_pct,
        tier_platinum_pct=tier_platinum_pct,
        fraud_rate=fraud_rate,
    )

    with st.spinner("Running simulation …"):
        collector, pool, summary = run_single(
            config, mode=mode, coverage=coverage, db_path=_DB_PATH,
        )
        label = coverage_label
        st.session_state["results"]    = {label: collector.to_dataframe()}
        st.session_state["summaries"]  = {label: summary}
        st.session_state["collectors"] = {label: collector}
        st.session_state["last_mode"]  = mode

    st.success("✅ Simulation complete! Scroll down for results.")

# =========================================================================
# CSV export
# =========================================================================
if export_btn and st.session_state["results"]:
    frames = []
    for lbl, df in st.session_state["results"].items():
        df_copy = df.copy()
        df_copy["run"] = lbl
        frames.append(df_copy)
    combined = pd.concat(frames, ignore_index=True)
    csv_data = combined.to_csv(index=False).encode("utf-8")
    st.sidebar.download_button(
        "⬇ Download CSV",
        data=csv_data,
        file_name="mev_simulation_results.csv",
        mime="text/csv",
        key="csv_download_btn",
    )

# =========================================================================
# Main area — build preview args once for reuse
# =========================================================================
_preview_kwargs = dict(
    mode=mode,
    duration_days=duration_days,
    swaps_per_day=swaps_per_day,
    coverage_label=coverage_label,
    mbase=mbase,
    loss_pct=loss_pct,
    false_negative_rate=false_negative_rate,
    sr_threshold_high=sr_threshold_high,
    sr_threshold_med=sr_threshold_med,
    oracle_reward_per_claim=oracle_reward_per_claim,
    captcha_reward=captcha_reward,
    initial_pool_balance=initial_pool_balance,
    infura_api_key=infura_api_key,
    block_range_days=block_range_days,
    dex_targets=dex_targets,
    patt_file_path=patt_file_path,
    seed=seed,
    n_synthetic_users=n_synthetic_users,
    tier_bronze_pct=tier_bronze_pct,
    tier_silver_pct=tier_silver_pct,
    tier_gold_pct=tier_gold_pct,
    tier_platinum_pct=tier_platinum_pct,
    fraud_rate=fraud_rate,
)

results    = st.session_state["results"]
summaries  = st.session_state["summaries"]
collectors = st.session_state["collectors"]

st.title("🛡️ MEV Insurance Protocol Simulator")

if not results:
    # Pre-simulation: show preview via single markdown call
    st.markdown(build_preview_markdown(**_preview_kwargs))
    st.stop()

# Post-simulation: collapsed preview + results
with st.expander("📋 Simulation Preview", expanded=False):
    st.markdown(build_preview_markdown(**_preview_kwargs))

# ---- Key Metrics ----
st.subheader("📊 Results")
first_summary = next(iter(summaries.values()))
cols = st.columns(4)
cols[0].metric("Total Profit (ETH)",   f"{first_summary['total_profit_eth']:.4f}")
cols[1].metric("Final Solvency Ratio", f"{first_summary['final_solvency_ratio']:.3f}")
cols[2].metric("Claim Approval Rate",  f"{first_summary['claim_approval_rate']:.1%}")
cols[3].metric(
    "Pool Survived",
    "YES ✓" if first_summary["pool_survived"] else "NO ✗",
    delta=None,
    delta_color="off",
)

st.markdown("---")
first_label = next(iter(results))
first_df    = results[first_label]


# =========================================================================
# Panel 1 — Pool Health Over Time
# =========================================================================
st.subheader("1 — Pool Health Over Time")
col1, col2 = st.columns(2)

with col1:
    st.caption(
        "Source: daily pool balance tracked by InsurancePool after "
        "each day's premiums, payouts and oracle rewards"
    )
    fig_bal = go.Figure()
    for lbl, df in results.items():
        fig_bal.add_trace(go.Scatter(
            x=df["day"], y=df["pool_balance_eth"],
            name=f"{lbl} — Balance", mode="lines",
        ))
    fig_bal.update_layout(
        title="Pool Balance (ETH)", xaxis_title="Day",
        yaxis_title="ETH", template="plotly_white",
    )
    all_bal = [v for df in results.values() for v in df["pool_balance_eth"].tolist()]
    _apply_yrange(fig_bal, all_bal)
    st.plotly_chart(fig_bal, use_container_width=True, key="chart_pool_balance")

with col2:
    st.caption(
        "Source: solvency_ratio = pool_balance / (pending_claims + projected_7d_payouts); "
        "coloured bands show risk zones"
    )
    fig_sr = go.Figure()
    for lbl, df in results.items():
        fig_sr.add_trace(go.Scatter(
            x=df["day"], y=df["solvency_ratio"],
            name=f"{lbl} — SR", mode="lines",
        ))
    fig_sr.add_hrect(y0=0,                y1=sr_threshold_med,  fillcolor="red",    opacity=0.07, line_width=0)
    fig_sr.add_hrect(y0=sr_threshold_med,  y1=sr_threshold_high, fillcolor="orange", opacity=0.07, line_width=0)
    fig_sr.add_hrect(y0=sr_threshold_high, y1=20,                fillcolor="green",  opacity=0.04, line_width=0)
    fig_sr.add_hline(y=1.0,               line_dash="dash", line_color="red",    annotation_text="SR 1.0")
    fig_sr.add_hline(y=sr_threshold_med,   line_dash="dot",  line_color="orange", annotation_text=f"SR {sr_threshold_med}")
    fig_sr.add_hline(y=sr_threshold_high,  line_dash="dot",  line_color="green",  annotation_text=f"SR {sr_threshold_high}")
    fig_sr.update_layout(
        title="Solvency Ratio", xaxis_title="Day",
        yaxis_title="SR (ratio)", template="plotly_white",
    )
    all_sr = [v for df in results.values() for v in df["solvency_ratio"].tolist()]
    _apply_yrange(fig_sr, all_sr)
    st.plotly_chart(fig_sr, use_container_width=True, key="chart_solvency_ratio")


# =========================================================================
# Panel 2 — Cash Flow
# =========================================================================
st.subheader("2 — Cash Flow")
st.caption(
    "Source: cumulative ETH as premiums, claim payouts, oracle rewards, "
    "and running profit = premiums − payouts − rewards"
)

prem_vals   = first_df["total_premiums_collected_eth"].tolist()
payout_vals = first_df["total_payouts_eth"].tolist()
max_prem = max(abs(v) for v in prem_vals)  if prem_vals  else 0.0
max_pay  = max(abs(v) for v in payout_vals) if payout_vals else 0.0

if max_prem < 1e-9 and max_pay < 1e-9:
    cf_df = first_df[[
        "day", "total_premiums_collected_eth",
        "total_payouts_eth", "total_oracle_rewards_eth", "profit_eth",
    ]].copy()
    cf_df.columns = ["Day", "Premiums (ETH)", "Payouts (ETH)", "Oracle Rewards (ETH)", "Profit (ETH)"]
    st.dataframe(
        cf_df.style.format(
            "{:.6f}",
            subset=["Premiums (ETH)", "Payouts (ETH)", "Oracle Rewards (ETH)", "Profit (ETH)"],
        ),
        use_container_width=True,
    )
else:
    fig_cf = go.Figure()
    fig_cf.add_trace(go.Scatter(
        x=first_df["day"], y=first_df["total_premiums_collected_eth"],
        name="Premiums", fill="tozeroy",
        fillcolor="rgba(0,180,0,0.2)", line_color="green",
    ))
    fig_cf.add_trace(go.Scatter(
        x=first_df["day"], y=first_df["total_payouts_eth"],
        name="Payouts", fill="tozeroy",
        fillcolor="rgba(220,0,0,0.2)", line_color="red",
    ))
    fig_cf.add_trace(go.Scatter(
        x=first_df["day"], y=first_df["total_oracle_rewards_eth"],
        name="Oracle Rewards", fill="tozeroy",
        fillcolor="rgba(0,0,200,0.15)", line_color="blue",
    ))
    fig_cf.add_trace(go.Scatter(
        x=first_df["day"], y=first_df["profit_eth"],
        name="Running Profit",
        line=dict(color="purple", width=2, dash="dash"),
    ))
    fig_cf.update_layout(
        title="Cumulative Cash Flow (ETH)", xaxis_title="Day",
        yaxis_title="ETH", template="plotly_white",
    )
    st.plotly_chart(fig_cf, use_container_width=True, key="chart_cashflow")


# =========================================================================
# Panel 3 — Claims Analysis
# =========================================================================
st.subheader("3 — Claims Analysis")
c1, c2, c3 = st.columns(3)

with c1:
    st.caption("Source: n_claims_approved / n_claims_submitted per day")
    appr_vals = (first_df["claim_approval_rate"] * 100).tolist()
    if max(abs(v) for v in appr_vals) < 1e-9:
        st.metric("Avg Claim Approval Rate", "0.0% (no claims)")
    else:
        fig_apr = go.Figure()
        fig_apr.add_trace(go.Scatter(
            x=first_df["day"], y=first_df["claim_approval_rate"] * 100,
            mode="lines", line_color="steelblue",
        ))
        fig_apr.update_layout(
            title="Approval Rate (%)", xaxis_title="Day",
            yaxis_title="Approval Rate (%)", template="plotly_white",
            yaxis_range=[0, 105],
        )
        _apply_yrange(fig_apr, appr_vals)
        st.plotly_chart(fig_apr, use_container_width=True, key="chart_approval_rate")

with c2:
    st.caption(
        "Source: avg_fraud_score per claim by FraudDetector; "
        "vertical lines = decision thresholds"
    )
    fs_vals    = first_df["avg_fraud_score"].tolist()
    nonzero_fs = [v for v in fs_vals if v > 0]
    if not nonzero_fs:
        st.metric("Avg Fraud Score", "N/A (no claims processed)")
        st.dataframe(
            pd.DataFrame({
                "Day": first_df["day"],
                "Avg Fraud Score": first_df["avg_fraud_score"],
            }),
            use_container_width=True,
        )
    else:
        fig_fs = go.Figure()
        fig_fs.add_trace(go.Histogram(
            x=nonzero_fs, nbinsx=30,
            marker_color="salmon", name="FraudScore",
        ))
        fig_fs.add_vline(x=60, line_dash="dash", line_color="orange", annotation_text="Captcha")
        fig_fs.add_vline(x=80, line_dash="dash", line_color="red",    annotation_text="Reject")
        fig_fs.update_layout(
            title="FraudScore Distribution", template="plotly_white",
            xaxis_title="Fraud Score", yaxis_title="Count (days)",
        )
        st.plotly_chart(fig_fs, use_container_width=True, key="chart_fraud_score")

with c3:
    st.caption("Source: daily claim counts split by decision (approved / captcha / rejected)")
    total_claims = (
        first_df["n_claims_approved"]
        + first_df["n_claims_captcha"]
        + first_df["n_claims_rejected"]
    ).sum()
    if total_claims == 0:
        st.info("No claims were processed in this simulation run.")
        st.dataframe(
            pd.DataFrame({
                "Metric": ["Total approved", "Total captcha", "Total rejected"],
                "Count": [0, 0, 0],
            }),
            use_container_width=True,
        )
    else:
        fig_cl = go.Figure()
        fig_cl.add_trace(go.Bar(
            x=first_df["day"], y=first_df["n_claims_approved"],
            name="Approved", marker_color="green",
        ))
        fig_cl.add_trace(go.Bar(
            x=first_df["day"], y=first_df["n_claims_captcha"],
            name="Captcha", marker_color="gold",
        ))
        fig_cl.add_trace(go.Bar(
            x=first_df["day"], y=first_df["n_claims_rejected"],
            name="Rejected", marker_color="red",
        ))
        fig_cl.update_layout(
            barmode="stack", title="Claims by Decision",
            xaxis_title="Day", yaxis_title="Number of Claims",
            template="plotly_white",
        )
        st.plotly_chart(fig_cl, use_container_width=True, key="chart_claims_decision")


# =========================================================================
# Panels 4 & 5 — Mode 2 only (shown based on stored last_mode, not live mode)
# =========================================================================
last_mode = st.session_state.get("last_mode", mode)

if last_mode == 2:
    # ---- Panel 4 — User Distribution ----
    st.subheader("4 — User Distribution")
    c4, c5 = st.columns(2)

    with c4:
        st.caption(
            "Source: daily count of active users per tier "
            "(Bronze→Silver→Gold→Platinum upgrades tracked by TierManager)"
        )
        tier_cols = ["n_users_bronze", "n_users_silver", "n_users_gold", "n_users_platinum"]
        tier_total_per_day = first_df[tier_cols].sum(axis=1)
        if tier_total_per_day.max() == 0:
            st.dataframe(first_df[["day"] + tier_cols], use_container_width=True)
        else:
            fig_tier = go.Figure()
            for t_col, t_color in [
                ("n_users_bronze",   "#cd7f32"),
                ("n_users_silver",   "#c0c0c0"),
                ("n_users_gold",     "#ffd700"),
                ("n_users_platinum", "#e5e4e2"),
            ]:
                fig_tier.add_trace(go.Bar(
                    x=first_df["day"], y=first_df[t_col],
                    name=t_col.replace("n_users_", "").capitalize(),
                    marker_color=t_color,
                ))
            fig_tier.update_layout(
                barmode="stack", title="Tier Distribution Over Time",
                xaxis_title="Day", yaxis_title="Number of Users",
                template="plotly_white",
            )
            st.plotly_chart(fig_tier, use_container_width=True, key="chart_tier_dist")

    with c5:
        st.caption(
            "Source: cumulative count of users blacklisted after "
            "fraud score > 80 rejection"
        )
        bl_vals = first_df["n_users_blacklisted"].tolist()
        if max(bl_vals) == 0:
            st.metric("Blacklisted Users", "0 (none blacklisted)")
        else:
            fig_bl = go.Figure()
            fig_bl.add_trace(go.Scatter(
                x=first_df["day"], y=first_df["n_users_blacklisted"],
                mode="lines", line_color="black", name="Blacklisted",
            ))
            fig_bl.update_layout(
                title="Blacklisted Users", xaxis_title="Day",
                yaxis_title="Count", template="plotly_white",
            )
            _apply_yrange(fig_bl, bl_vals)
            st.plotly_chart(fig_bl, use_container_width=True, key="chart_blacklisted")

    # ---- Panel 5 — Oracle Network ----
    st.subheader("5 — Oracle Network")
    c6, c7, c8 = st.columns(3)

    with c6:
        st.caption(
            "Source: OracleNetwork watchlist — oracles added when "
            "divergence ≥ 10 pts on 2 occasions"
        )
        wl_vals = first_df["n_oracles_watchlist"].tolist()
        if max(wl_vals) == 0:
            st.metric("Watchlist Entries", "0")
        else:
            fig_wl = go.Figure()
            fig_wl.add_trace(go.Scatter(
                x=first_df["day"], y=first_df["n_oracles_watchlist"],
                mode="lines", line_color="orange",
            ))
            fig_wl.update_layout(
                title="Watchlist Entries", xaxis_title="Day",
                yaxis_title="Count", template="plotly_white",
            )
            _apply_yrange(fig_wl, wl_vals)
            st.plotly_chart(fig_wl, use_container_width=True, key="chart_watchlist")

    with c7:
        st.caption(
            "Source: mean absolute divergence between honest and "
            "dishonest oracle votes per day"
        )
        div_vals = first_df["avg_oracle_divergence"].tolist()
        if max(abs(v) for v in div_vals) < 1e-9:
            st.metric("Avg Oracle Divergence", "0.00")
        else:
            fig_div = go.Figure()
            fig_div.add_trace(go.Scatter(
                x=first_df["day"], y=first_df["avg_oracle_divergence"],
                mode="lines", line_color="purple",
            ))
            fig_div.update_layout(
                title="Avg Oracle Divergence", xaxis_title="Day",
                yaxis_title="Divergence (pts)", template="plotly_white",
            )
            _apply_yrange(fig_div, div_vals)
            st.plotly_chart(fig_div, use_container_width=True, key="chart_divergence")

    with c8:
        st.caption(
            "Source: cumulative slashing events triggered by OracleNetwork "
            "(Pool 60% / Reporter 25% / Jury 15%)"
        )
        slash_cum  = first_df["n_oracles_slashed"].cumsum()
        slash_vals = slash_cum.tolist()
        if max(slash_vals) == 0:
            st.metric("Cumulative Slashing Events", "0")
        else:
            fig_sl = go.Figure()
            fig_sl.add_trace(go.Scatter(
                x=first_df["day"], y=slash_cum,
                mode="lines", line_color="red", name="Slashed (cum)",
            ))
            fig_sl.update_layout(
                title="Cumulative Slashing Events", xaxis_title="Day",
                yaxis_title="Count", template="plotly_white",
            )
            _apply_yrange(fig_sl, slash_vals)
            st.plotly_chart(fig_sl, use_container_width=True, key="chart_slashing")


# =========================================================================
# Panel 6 — Day-by-Day Explorer
# =========================================================================
st.markdown("---")
st.subheader("6 — Day-by-Day Explorer")

first_collector = collectors.get(first_label)
df_all          = results[first_label]

max_day      = int(df_all["day"].max())
selected_day = st.slider(
    "Select Day", min_value=0, max_value=max_day, value=0, step=1,
    key="day_explorer_slider",
)

row = df_all[df_all["day"] == selected_day]
if row.empty:
    st.warning(f"No data for day {selected_day}.")
else:
    row = row.iloc[0]
    left_col, right_col = st.columns(2)

    with left_col:
        st.markdown("**Pool State**")
        net_flow = float(row.get("net_flow_today", 0.0))
        net_sign = "+" if net_flow >= 0 else ""
        state_df = pd.DataFrame({
            "Metric": [
                "Pool Balance (ETH)",
                "Pending Liabilities (ETH)",
                "Solvency Ratio",
                "M_adj (dynamic margin)",
                "Patt (attack rate)",
                "Premiums collected today (ETH)",
                "Payouts executed today (ETH)",
                "Oracle rewards paid today (ETH)",
                "Net flow today (ETH)",
            ],
            "Value": [
                f"{float(row['pool_balance_eth']):.4f}",
                f"{float(row.get('pending_liabilities_eth', 0.0)):.4f}",
                f"{float(row['solvency_ratio']):.4f}",
                f"{float(row.get('madj_current', 0.0)):.2f}",
                f"{float(row.get('patt_current', 0.0)):.2%}",
                f"{float(row.get('premiums_today', 0.0)):.4f}",
                f"{float(row.get('payouts_today', 0.0)):.4f}",
                f"{float(row.get('oracle_rewards_today', 0.0)):.4f}",
                f"{net_sign}{net_flow:.4f}",
            ],
        })
        st.dataframe(state_df, use_container_width=True, hide_index=True)

    with right_col:
        st.markdown("**Day Activity**")
        n_swaps    = int(row.get("n_swaps_this_tick", 0))
        n_attacked = int(row.get("n_attacks_this_tick", 0))
        n_insured  = int(row.get("n_swaps_insured", n_swaps))
        pct_att    = f"{n_attacked / max(n_swaps, 1):.1%}"
        n_submitted = int(row.get("n_claims_submitted", 0))
        n_approved  = int(row.get("n_claims_approved",  0))
        n_rejected  = int(row.get("n_claims_rejected",  0))
        n_captcha_d = int(row.get("n_claims_captcha",   0))
        n_pattern   = int(row.get("n_rejected_pattern_invalid",   0))
        n_fs_gt80   = int(row.get("n_rejected_fraud_score_gt_80", 0))
        n_captcha_f = int(row.get("n_rejected_captcha_failed",    0))
        avg_fs_app  = float(row.get("avg_fraud_score_approved", 0.0))
        avg_fs_rej  = float(row.get("avg_fraud_score_rejected",  0.0))
        activity_df = pd.DataFrame({
            "Metric": [
                "Swaps processed",
                "  — of which attacked",
                "  — of which insured",
                "Claims submitted",
                "Claims approved",
                "Claims captcha",
                "Claims rejected",
                "  — pattern invalid",
                "  — fraud score > 80",
                "  — CAPTCHA failed",
                "Avg FraudScore (approved)",
                "Avg FraudScore (rejected)",
            ],
            "Value": [
                str(n_swaps),
                f"{n_attacked} ({pct_att})",
                str(n_insured),
                str(n_submitted),
                str(n_approved),
                str(n_captcha_d),
                str(n_rejected),
                str(n_pattern),
                str(n_fs_gt80),
                str(n_captcha_f),
                f"{avg_fs_app:.1f}",
                f"{avg_fs_rej:.1f}",
            ],
        })
        st.dataframe(activity_df, use_container_width=True, hide_index=True)

    if first_collector is not None:
        day_swaps = first_collector.daily_swap_details.get(selected_day, [])
        with st.expander(
            f"Show all swaps for day {selected_day} ({len(day_swaps)} swaps)",
            expanded=False,
        ):
            if day_swaps:
                swap_df = pd.DataFrame(day_swaps)
                for col_name in ("value_ETH", "premium_paid", "payout_ETH"):
                    if col_name in swap_df.columns:
                        swap_df[col_name] = swap_df[col_name].round(6)
                st.dataframe(swap_df, use_container_width=True)
            else:
                st.info("No detailed swap data available for this day.")

# =========================================================================
# Raw data table
# =========================================================================
with st.expander("📊 Raw simulation data", expanded=False):
    st.dataframe(first_df, use_container_width=True)
