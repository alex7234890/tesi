"""
MEV Insurance Protocol — Streamlit Dashboard.

Run independently:
    streamlit run dashboard/app.py

Or launched automatically by runner.py after a simulation completes.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import streamlit as st

# Make sure the package root is importable
_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.config_loader import load_config
from runner import run_single, run_mode1_all

_DB_PATH = os.path.join(_ROOT, "data", "blockchain.db")
_OUT_DIR = os.path.join(_ROOT, "data")

# =========================================================================
# Page configuration
# =========================================================================
st.set_page_config(
    page_title="MEV Insurance Simulator",
    page_icon="🛡️",
    layout="wide",
)

# =========================================================================
# Sidebar — controls
# =========================================================================
st.sidebar.title("🛡️ MEV Insurance Simulator")
st.sidebar.markdown("---")

mode     = st.sidebar.selectbox("Mode", [1, 2],
                                format_func=lambda m: f"Mode {m} — {'Real chain + partial sim' if m==1 else 'Full synthetic'}")
coverage = st.sidebar.selectbox(
    "Coverage (mode 1 only)",
    ["all", "low", "medium", "high"],
    disabled=(mode == 2),
)

st.sidebar.markdown("### Key Parameters")

fraud_rate = st.sidebar.slider(
    "User Fraud Rate (%)", 0, 30, 5, step=1
) / 100.0

oracle_dishonest_rate = st.sidebar.slider(
    "Oracle Dishonest Rate (%)", 0, 30, 10, step=1
) / 100.0

mbase = st.sidebar.slider(
    "Base Margin Mbase (%)", 5, 40, 20, step=1
) / 100.0

initial_pool_balance = st.sidebar.slider(
    "Initial Pool Balance (ETH)", 10, 1000, 100, step=10
)

insurance_rate = (
    st.sidebar.slider("Insurance Rate (%) — mode 1", 10, 100, 50, step=5) / 100.0
    if mode == 1
    else 1.0
)

duration_days = st.sidebar.slider("Duration (days)", 30, 365, 180, step=10)

st.sidebar.markdown("---")
run_btn    = st.sidebar.button("▶ Run Simulation", type="primary")
export_btn = st.sidebar.button("📥 Export CSV")

# =========================================================================
# Session state — persist results across reruns
# =========================================================================
if "results" not in st.session_state:
    st.session_state["results"] = {}   # label → DataFrame
if "summaries" not in st.session_state:
    st.session_state["summaries"] = {}

# =========================================================================
# Run simulation
# =========================================================================
def _build_config(
    mode: int,
    fraud_rate: float,
    oracle_dishonest_rate: float,
    mbase: float,
    initial_pool_balance: float,
    insurance_rate: float,
    duration_days: int,
) -> dict:
    cfg_path = os.path.join(
        _ROOT, "config",
        "mode1_realchain.yaml" if mode == 1 else "mode2_synthetic.yaml",
    )
    config = load_config(cfg_path)
    config["fraud_detection"]["user_fraud_rate"]       = fraud_rate
    config["users"]["fraud_rate"]                      = fraud_rate
    config["fraud_detection"]["oracle_dishonest_rate"] = oracle_dishonest_rate
    config["oracles"]["honest_rate"]                   = 1.0 - oracle_dishonest_rate
    config["pool"]["mbase"]                            = mbase
    config["pool"]["initial_balance_eth"]              = float(initial_pool_balance)
    config["market"]["insurance_rate"]                 = insurance_rate
    config["simulation"]["duration_days"]              = duration_days
    return config


if run_btn:
    config = _build_config(
        mode, fraud_rate, oracle_dishonest_rate, mbase,
        initial_pool_balance, insurance_rate, duration_days,
    )

    with st.spinner("Running simulation …"):
        all_dfs      = {}
        all_summaries = {}

        if mode == 1 and coverage == "all":
            for cov in ("low", "medium", "high"):
                collector, pool, summary = run_single(
                    config, mode=1, coverage=cov, db_path=_DB_PATH
                )
                all_dfs[cov.capitalize()]      = collector.to_dataframe()
                all_summaries[cov.capitalize()] = summary
        else:
            cov = coverage if mode == 1 else "high"
            collector, pool, summary = run_single(
                config, mode=mode, coverage=cov, db_path=_DB_PATH
            )
            all_dfs[cov.capitalize()]      = collector.to_dataframe()
            all_summaries[cov.capitalize()] = summary

    st.session_state["results"]   = all_dfs
    st.session_state["summaries"] = all_summaries
    st.success("Simulation complete!")

# =========================================================================
# CSV export
# =========================================================================
if export_btn and st.session_state["results"]:
    frames = []
    for label, df in st.session_state["results"].items():
        df_copy         = df.copy()
        df_copy["run"]  = label
        frames.append(df_copy)
    combined = pd.concat(frames, ignore_index=True)
    csv_data = combined.to_csv(index=False).encode("utf-8")
    st.sidebar.download_button(
        "⬇ Download CSV",
        data=csv_data,
        file_name="mev_simulation_results.csv",
        mime="text/csv",
    )

# =========================================================================
# Main panels — only shown if results exist
# =========================================================================
results   = st.session_state["results"]
summaries = st.session_state["summaries"]

if not results:
    st.title("🛡️ MEV Insurance Protocol Simulator")
    st.info("Configure parameters in the sidebar and press **▶ Run Simulation** to start.")
    st.stop()

# ---- Key Metrics Summary (top bar) ----
st.title("🛡️ MEV Insurance Protocol — Results")
cols = st.columns(4)
first_summary = next(iter(summaries.values()))

cols[0].metric("Total Profit (ETH)",     f"{first_summary['total_profit_eth']:.4f}")
cols[1].metric("Final Solvency Ratio",   f"{first_summary['final_solvency_ratio']:.3f}")
cols[2].metric("Claim Approval Rate",    f"{first_summary['claim_approval_rate']:.1%}")
cols[3].metric("Pool Survived",
               "YES ✓" if first_summary["pool_survived"] else "NO ✗",
               delta=None,
               delta_color="off")

st.markdown("---")

# =========================================================================
# Panel 1 — Pool Health Over Time
# =========================================================================
st.subheader("1 — Pool Health Over Time")

import plotly.graph_objects as go

col1, col2 = st.columns(2)

with col1:
    fig = go.Figure()
    for label, df in results.items():
        fig.add_trace(go.Scatter(x=df["day"], y=df["pool_balance_eth"],
                                 name=f"{label} — Balance", mode="lines"))
    fig.update_layout(
        title="Pool Balance (ETH)",
        xaxis_title="Day",
        yaxis_title="ETH",
        template="plotly_white",
    )
    st.plotly_chart(fig, use_container_width=True)

with col2:
    fig2 = go.Figure()
    for label, df in results.items():
        fig2.add_trace(go.Scatter(x=df["day"], y=df["solvency_ratio"],
                                  name=f"{label} — SR", mode="lines"))
    # Colour zones
    fig2.add_hrect(y0=0,   y1=1.3, fillcolor="red",    opacity=0.07, line_width=0)
    fig2.add_hrect(y0=1.3, y1=1.5, fillcolor="orange", opacity=0.07, line_width=0)
    fig2.add_hrect(y0=1.5, y1=20,  fillcolor="green",  opacity=0.04, line_width=0)
    fig2.add_hline(y=1.0, line_dash="dash", line_color="red",    annotation_text="SR 1.0")
    fig2.add_hline(y=1.3, line_dash="dot",  line_color="orange", annotation_text="SR 1.3")
    fig2.add_hline(y=1.5, line_dash="dot",  line_color="green",  annotation_text="SR 1.5")
    fig2.update_layout(
        title="Solvency Ratio",
        xaxis_title="Day",
        yaxis_title="SR",
        template="plotly_white",
    )
    st.plotly_chart(fig2, use_container_width=True)

# =========================================================================
# Panel 2 — Cash Flow
# =========================================================================
st.subheader("2 — Cash Flow")
first_df = next(iter(results.values()))

fig3 = go.Figure()
fig3.add_trace(go.Scatter(
    x=first_df["day"], y=first_df["total_premiums_collected_eth"],
    name="Premiums", fill="tozeroy", fillcolor="rgba(0,180,0,0.2)", line_color="green",
))
fig3.add_trace(go.Scatter(
    x=first_df["day"], y=first_df["total_payouts_eth"],
    name="Payouts", fill="tozeroy", fillcolor="rgba(220,0,0,0.2)", line_color="red",
))
fig3.add_trace(go.Scatter(
    x=first_df["day"], y=first_df["total_oracle_rewards_eth"],
    name="Oracle Rewards", fill="tozeroy", fillcolor="rgba(0,0,200,0.15)", line_color="blue",
))
fig3.add_trace(go.Scatter(
    x=first_df["day"], y=first_df["profit_eth"],
    name="Running Profit", line=dict(color="purple", width=2, dash="dash"),
))
fig3.update_layout(
    title="Cumulative Cash Flow (ETH)",
    xaxis_title="Day",
    yaxis_title="ETH",
    template="plotly_white",
)
st.plotly_chart(fig3, use_container_width=True)

# =========================================================================
# Panel 3 — Claims Analysis
# =========================================================================
st.subheader("3 — Claims Analysis")
c1, c2, c3 = st.columns(3)

with c1:
    fig4 = go.Figure()
    fig4.add_trace(go.Scatter(
        x=first_df["day"], y=first_df["claim_approval_rate"] * 100,
        mode="lines", line_color="steelblue",
    ))
    fig4.update_layout(title="Approval Rate (%)", xaxis_title="Day", template="plotly_white")
    st.plotly_chart(fig4, use_container_width=True)

with c2:
    fig5 = go.Figure()
    fig5.add_trace(go.Histogram(x=first_df["avg_fraud_score"], nbinsx=30,
                                marker_color="salmon", name="FraudScore"))
    fig5.add_vline(x=60, line_dash="dash", line_color="orange", annotation_text="Captcha")
    fig5.add_vline(x=80, line_dash="dash", line_color="red",    annotation_text="Reject")
    fig5.update_layout(title="FraudScore Distribution", template="plotly_white")
    st.plotly_chart(fig5, use_container_width=True)

with c3:
    fig6 = go.Figure()
    fig6.add_trace(go.Bar(x=first_df["day"], y=first_df["n_claims_approved"],
                          name="Approved", marker_color="green"))
    fig6.add_trace(go.Bar(x=first_df["day"], y=first_df["n_claims_captcha"],
                          name="Captcha",  marker_color="gold"))
    fig6.add_trace(go.Bar(x=first_df["day"], y=first_df["n_claims_rejected"],
                          name="Rejected", marker_color="red"))
    fig6.update_layout(barmode="stack", title="Claims by Decision",
                       xaxis_title="Day", template="plotly_white")
    st.plotly_chart(fig6, use_container_width=True)

# =========================================================================
# Panel 4 — User Distribution (mode 2 only)
# =========================================================================
if mode == 2:
    st.subheader("4 — User Distribution")
    c4, c5 = st.columns(2)

    with c4:
        fig7 = go.Figure()
        for tier, color in [("n_users_bronze", "#cd7f32"), ("n_users_silver", "#c0c0c0"),
                             ("n_users_gold", "#ffd700"), ("n_users_platinum", "#e5e4e2")]:
            fig7.add_trace(go.Bar(x=first_df["day"], y=first_df[tier],
                                  name=tier.replace("n_users_", "").capitalize(),
                                  marker_color=color))
        fig7.update_layout(barmode="stack", title="Tier Distribution Over Time",
                            xaxis_title="Day", template="plotly_white")
        st.plotly_chart(fig7, use_container_width=True)

    with c5:
        fig8 = go.Figure()
        fig8.add_trace(go.Scatter(
            x=first_df["day"], y=first_df["n_users_blacklisted"],
            mode="lines", line_color="black", name="Blacklisted",
        ))
        fig8.update_layout(title="Blacklisted Users", xaxis_title="Day", template="plotly_white")
        st.plotly_chart(fig8, use_container_width=True)

# =========================================================================
# Panel 5 — Oracle Network (mode 2 only)
# =========================================================================
if mode == 2:
    st.subheader("5 — Oracle Network")
    c6, c7, c8 = st.columns(3)

    with c6:
        fig9 = go.Figure()
        fig9.add_trace(go.Scatter(x=first_df["day"], y=first_df["n_oracles_watchlist"],
                                   mode="lines", line_color="orange"))
        fig9.update_layout(title="Watchlist Entries", xaxis_title="Day", template="plotly_white")
        st.plotly_chart(fig9, use_container_width=True)

    with c7:
        fig10 = go.Figure()
        fig10.add_trace(go.Scatter(x=first_df["day"], y=first_df["avg_oracle_divergence"],
                                    mode="lines", line_color="purple"))
        fig10.update_layout(title="Avg Oracle Divergence", xaxis_title="Day", template="plotly_white")
        st.plotly_chart(fig10, use_container_width=True)

    with c8:
        slash_cum = first_df["n_oracles_slashed"].cumsum()
        fig11 = go.Figure()
        fig11.add_trace(go.Scatter(x=first_df["day"], y=slash_cum,
                                    mode="lines", line_color="red", name="Slashed (cum)"))
        fig11.update_layout(title="Cumulative Slashing Events", xaxis_title="Day", template="plotly_white")
        st.plotly_chart(fig11, use_container_width=True)

# =========================================================================
# Raw data table (expandable)
# =========================================================================
with st.expander("📊 Raw simulation data"):
    st.dataframe(first_df, use_container_width=True)
