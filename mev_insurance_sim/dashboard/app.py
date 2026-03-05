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
from runner import run_single

_DB_PATH = os.path.join(_ROOT, "data", "blockchain.db")
_OUT_DIR = os.path.join(_ROOT, "data")

# Coverage level mapping (hardcoded per protocol spec)
_COVERAGE_LABELS   = ["Low", "Medium", "High"]
_COVERAGE_INTERNAL = {"Low": "low", "Medium": "medium", "High": "high"}

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

mode = st.sidebar.selectbox(
    "Mode",
    [1, 2],
    format_func=lambda m: (
        "Mode 1 — Real chain + partial sim" if m == 1
        else "Mode 2 — Full synthetic"
    ),
)

# Coverage Level selectbox — same for both modes (Low/Medium/High)
coverage_label = st.sidebar.selectbox(
    "Coverage Level",
    _COVERAGE_LABELS,
    index=1,   # Medium is default
    help=(
        "Low → 50% reimbursement, Fcov=0.70 | "
        "Medium → 70% reimbursement, Fcov=0.90 | "
        "High → 100% reimbursement, Fcov=1.00"
    ),
)
coverage = _COVERAGE_INTERNAL[coverage_label]

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

duration_days = st.sidebar.slider("Duration (days)", 30, 365, 180, step=10)

st.sidebar.markdown("---")
run_btn    = st.sidebar.button("▶ Run Simulation", type="primary")
export_btn = st.sidebar.button("📥 Export CSV")

# =========================================================================
# Session state — persist results across reruns
# =========================================================================
if "results" not in st.session_state:
    st.session_state["results"]    = {}   # label → DataFrame
if "summaries" not in st.session_state:
    st.session_state["summaries"]  = {}
if "collectors" not in st.session_state:
    st.session_state["collectors"] = {}   # label → MetricsCollector


# =========================================================================
# Run simulation
# =========================================================================
def _build_config(
    mode: int,
    fraud_rate: float,
    oracle_dishonest_rate: float,
    mbase: float,
    initial_pool_balance: float,
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
    config["simulation"]["duration_days"]              = duration_days
    return config


if run_btn:
    config = _build_config(
        mode, fraud_rate, oracle_dishonest_rate, mbase,
        initial_pool_balance, duration_days,
    )

    with st.spinner("Running simulation …"):
        collector, pool, summary = run_single(
            config, mode=mode, coverage=coverage, db_path=_DB_PATH
        )
        label = coverage_label
        all_dfs        = {label: collector.to_dataframe()}
        all_summaries  = {label: summary}
        all_collectors = {label: collector}

    st.session_state["results"]    = all_dfs
    st.session_state["summaries"]  = all_summaries
    st.session_state["collectors"] = all_collectors
    st.success("Simulation complete!")

# =========================================================================
# CSV export
# =========================================================================
if export_btn and st.session_state["results"]:
    frames = []
    for label, df in st.session_state["results"].items():
        df_copy        = df.copy()
        df_copy["run"] = label
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
results    = st.session_state["results"]
summaries  = st.session_state["summaries"]
collectors = st.session_state["collectors"]

if not results:
    st.title("🛡️ MEV Insurance Protocol Simulator")
    st.info("Configure parameters in the sidebar and press **▶ Run Simulation** to start.")
    st.stop()

# ---- Key Metrics Summary (top bar) ----
st.title("🛡️ MEV Insurance Protocol — Results")
cols = st.columns(4)
first_summary = next(iter(summaries.values()))

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

# =========================================================================
# Panel 1 — Pool Health Over Time
# =========================================================================
import plotly.graph_objects as go

st.subheader("1 — Pool Health Over Time")
col1, col2 = st.columns(2)

with col1:
    fig = go.Figure()
    for label, df in results.items():
        fig.add_trace(go.Scatter(
            x=df["day"], y=df["pool_balance_eth"],
            name=f"{label} — Balance", mode="lines",
        ))
    fig.update_layout(
        title="Pool Balance (ETH)", xaxis_title="Day",
        yaxis_title="ETH", template="plotly_white",
    )
    st.plotly_chart(fig, use_container_width=True)

with col2:
    fig2 = go.Figure()
    for label, df in results.items():
        fig2.add_trace(go.Scatter(
            x=df["day"], y=df["solvency_ratio"],
            name=f"{label} — SR", mode="lines",
        ))
    fig2.add_hrect(y0=0,   y1=1.3, fillcolor="red",    opacity=0.07, line_width=0)
    fig2.add_hrect(y0=1.3, y1=1.5, fillcolor="orange", opacity=0.07, line_width=0)
    fig2.add_hrect(y0=1.5, y1=20,  fillcolor="green",  opacity=0.04, line_width=0)
    fig2.add_hline(y=1.0, line_dash="dash", line_color="red",    annotation_text="SR 1.0")
    fig2.add_hline(y=1.3, line_dash="dot",  line_color="orange", annotation_text="SR 1.3")
    fig2.add_hline(y=1.5, line_dash="dot",  line_color="green",  annotation_text="SR 1.5")
    fig2.update_layout(
        title="Solvency Ratio", xaxis_title="Day",
        yaxis_title="SR", template="plotly_white",
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
    name="Premiums", fill="tozeroy",
    fillcolor="rgba(0,180,0,0.2)", line_color="green",
))
fig3.add_trace(go.Scatter(
    x=first_df["day"], y=first_df["total_payouts_eth"],
    name="Payouts", fill="tozeroy",
    fillcolor="rgba(220,0,0,0.2)", line_color="red",
))
fig3.add_trace(go.Scatter(
    x=first_df["day"], y=first_df["total_oracle_rewards_eth"],
    name="Oracle Rewards", fill="tozeroy",
    fillcolor="rgba(0,0,200,0.15)", line_color="blue",
))
fig3.add_trace(go.Scatter(
    x=first_df["day"], y=first_df["profit_eth"],
    name="Running Profit",
    line=dict(color="purple", width=2, dash="dash"),
))
fig3.update_layout(
    title="Cumulative Cash Flow (ETH)", xaxis_title="Day",
    yaxis_title="ETH", template="plotly_white",
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
    fig4.update_layout(
        title="Approval Rate (%)", xaxis_title="Day", template="plotly_white",
    )
    st.plotly_chart(fig4, use_container_width=True)

with c2:
    fig5 = go.Figure()
    fig5.add_trace(go.Histogram(
        x=first_df["avg_fraud_score"], nbinsx=30,
        marker_color="salmon", name="FraudScore",
    ))
    fig5.add_vline(x=60, line_dash="dash", line_color="orange", annotation_text="Captcha")
    fig5.add_vline(x=80, line_dash="dash", line_color="red",    annotation_text="Reject")
    fig5.update_layout(title="FraudScore Distribution", template="plotly_white")
    st.plotly_chart(fig5, use_container_width=True)

with c3:
    fig6 = go.Figure()
    fig6.add_trace(go.Bar(
        x=first_df["day"], y=first_df["n_claims_approved"],
        name="Approved", marker_color="green",
    ))
    fig6.add_trace(go.Bar(
        x=first_df["day"], y=first_df["n_claims_captcha"],
        name="Captcha",  marker_color="gold",
    ))
    fig6.add_trace(go.Bar(
        x=first_df["day"], y=first_df["n_claims_rejected"],
        name="Rejected", marker_color="red",
    ))
    fig6.update_layout(
        barmode="stack", title="Claims by Decision",
        xaxis_title="Day", template="plotly_white",
    )
    st.plotly_chart(fig6, use_container_width=True)

# =========================================================================
# Panel 4 — User Distribution (mode 2 only)
# =========================================================================
if mode == 2:
    st.subheader("4 — User Distribution")
    c4, c5 = st.columns(2)

    with c4:
        fig7 = go.Figure()
        for tier, color in [
            ("n_users_bronze",   "#cd7f32"),
            ("n_users_silver",   "#c0c0c0"),
            ("n_users_gold",     "#ffd700"),
            ("n_users_platinum", "#e5e4e2"),
        ]:
            fig7.add_trace(go.Bar(
                x=first_df["day"], y=first_df[tier],
                name=tier.replace("n_users_", "").capitalize(),
                marker_color=color,
            ))
        fig7.update_layout(
            barmode="stack", title="Tier Distribution Over Time",
            xaxis_title="Day", template="plotly_white",
        )
        st.plotly_chart(fig7, use_container_width=True)

    with c5:
        fig8 = go.Figure()
        fig8.add_trace(go.Scatter(
            x=first_df["day"], y=first_df["n_users_blacklisted"],
            mode="lines", line_color="black", name="Blacklisted",
        ))
        fig8.update_layout(
            title="Blacklisted Users", xaxis_title="Day", template="plotly_white",
        )
        st.plotly_chart(fig8, use_container_width=True)

# =========================================================================
# Panel 5 — Oracle Network (mode 2 only)
# =========================================================================
if mode == 2:
    st.subheader("5 — Oracle Network")
    c6, c7, c8 = st.columns(3)

    with c6:
        fig9 = go.Figure()
        fig9.add_trace(go.Scatter(
            x=first_df["day"], y=first_df["n_oracles_watchlist"],
            mode="lines", line_color="orange",
        ))
        fig9.update_layout(
            title="Watchlist Entries", xaxis_title="Day", template="plotly_white",
        )
        st.plotly_chart(fig9, use_container_width=True)

    with c7:
        fig10 = go.Figure()
        fig10.add_trace(go.Scatter(
            x=first_df["day"], y=first_df["avg_oracle_divergence"],
            mode="lines", line_color="purple",
        ))
        fig10.update_layout(
            title="Avg Oracle Divergence", xaxis_title="Day", template="plotly_white",
        )
        st.plotly_chart(fig10, use_container_width=True)

    with c8:
        slash_cum = first_df["n_oracles_slashed"].cumsum()
        fig11 = go.Figure()
        fig11.add_trace(go.Scatter(
            x=first_df["day"], y=slash_cum,
            mode="lines", line_color="red", name="Slashed (cum)",
        ))
        fig11.update_layout(
            title="Cumulative Slashing Events", xaxis_title="Day", template="plotly_white",
        )
        st.plotly_chart(fig11, use_container_width=True)

# =========================================================================
# Panel 6 — Day-by-Day Explorer
# =========================================================================
st.markdown("---")
st.subheader("6 — Day-by-Day Explorer")

first_label     = next(iter(results))
first_collector = collectors.get(first_label)
df_all          = results[first_label]

max_day    = int(df_all["day"].max())
selected_day = st.slider(
    "Select Day", min_value=0, max_value=max_day, value=0, step=1,
)

row = df_all[df_all["day"] == selected_day]
if row.empty:
    st.warning(f"No data for day {selected_day}.")
else:
    row = row.iloc[0]

    left_col, right_col = st.columns(2)

    # ---- Left column: Pool State ----
    with left_col:
        st.markdown("**Pool State**")

        net_flow = float(row.get("net_flow_today", 0.0))
        net_color = "green" if net_flow >= 0 else "red"
        net_sign  = "+" if net_flow >= 0 else ""

        madj_val = float(row.get("madj_current", 0.0))
        madj_str = f"{madj_val:.2f}"

        patt_val = float(row.get("patt_current", 0.0))

        st.markdown(f"""
| Metric | Value |
|---|---|
| **Pool Balance (ETH)** | `{float(row['pool_balance_eth']):.4f}` |
| **Pending Liabilities (ETH)** | `{float(row.get('pending_liabilities_eth', 0.0)):.4f}` |
| **Solvency Ratio** | `{float(row['solvency_ratio']):.4f}` |
| **M_adj (dynamic margin)** | `{madj_str}` |
| **Patt (attack rate)** | `{patt_val:.2%}` |
| **Premiums collected today** | `{float(row.get('premiums_today', 0.0)):.4f} ETH` |
| **Payouts executed today** | `{float(row.get('payouts_today', 0.0)):.4f} ETH` |
| **Oracle rewards paid today** | `{float(row.get('oracle_rewards_today', 0.0)):.4f} ETH` |
""")
        st.markdown(
            f"| **Net flow today** | "
            f"<span style='color:{net_color}'>`{net_sign}{net_flow:.4f} ETH`</span> |",
            unsafe_allow_html=True,
        )

    # ---- Right column: Activity ----
    with right_col:
        st.markdown("**Day Activity**")

        n_swaps    = int(row.get("n_swaps_this_tick", 0))
        n_attacked = int(row.get("n_attacks_this_tick", 0))
        n_insured  = int(row.get("n_swaps_insured", n_swaps))
        pct_att    = f"{n_attacked / max(n_swaps, 1):.1%}"

        n_submitted = int(row.get("n_claims_submitted", 0))
        n_approved  = int(row.get("n_claims_approved",  0))
        n_rejected  = int(row.get("n_claims_rejected",  0))
        n_pattern   = int(row.get("n_rejected_pattern_invalid",   0))
        n_fs_gt80   = int(row.get("n_rejected_fraud_score_gt_80", 0))
        n_captcha_f = int(row.get("n_rejected_captcha_failed",    0))
        avg_fs_app  = float(row.get("avg_fraud_score_approved", 0.0))
        avg_fs_rej  = float(row.get("avg_fraud_score_rejected",  0.0))

        st.markdown(f"""
| Metric | Value |
|---|---|
| **Swaps processed** | `{n_swaps}` |
| &nbsp;&nbsp; of which attacked | `{n_attacked}` (`{pct_att}`) |
| &nbsp;&nbsp; of which insured | `{n_insured}` |
| &nbsp;&nbsp; insured + attacked | `{n_attacked}` |
| **Claims submitted** | `{n_submitted}` |
| **Claims approved** | `{n_approved}` |
| **Claims rejected** | `{n_rejected}` |
| &nbsp;&nbsp; pattern invalid | `{n_pattern}` |
| &nbsp;&nbsp; fraud score > 80 | `{n_fs_gt80}` |
| &nbsp;&nbsp; CAPTCHA failed | `{n_captcha_f}` |
| **Avg FraudScore (approved)** | `{avg_fs_app:.1f}` |
| **Avg FraudScore (rejected)** | `{avg_fs_rej:.1f}` |
""", unsafe_allow_html=True)

    # ---- Expandable swap table ----
    if first_collector is not None:
        day_swaps = first_collector.daily_swap_details.get(selected_day, [])
        with st.expander(f"Show all swaps for day {selected_day} ({len(day_swaps)} swaps)"):
            if day_swaps:
                swap_df = pd.DataFrame(day_swaps)
                # Rename for display
                swap_df = swap_df.rename(columns={
                    "swap_id":         "swap_id",
                    "value_ETH":       "value_ETH",
                    "was_attacked":    "was_attacked",
                    "insured":         "insured",
                    "coverage_level":  "coverage_level",
                    "premium_paid":    "premium_paid",
                    "claim_submitted": "claim_submitted",
                    "claim_approved":  "claim_approved",
                    "payout_ETH":      "payout_ETH",
                    "fraud_score":     "fraud_score",
                    "rejection_reason": "rejection_reason",
                })
                # Round floats for readability
                for col in ("value_ETH", "premium_paid", "payout_ETH"):
                    if col in swap_df.columns:
                        swap_df[col] = swap_df[col].round(6)
                st.dataframe(swap_df, use_container_width=True)
            else:
                st.info("No detailed swap data available for this day.")

# =========================================================================
# Raw data table (expandable)
# =========================================================================
with st.expander("📊 Raw simulation data"):
    st.dataframe(first_df, use_container_width=True)
