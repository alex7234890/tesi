"""
MEV Insurance Protocol — Streamlit Dashboard (v6: fully synthetic, no Infura).

Avvio:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import copy
import os
import time as _time_mod
import sys
from itertools import product as _itertools_product

import numpy as np
import pandas as pd
import streamlit as st

_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.config_loader import load_config
from runner import run_single

_DB_PATH = os.path.join(_ROOT, "data", "blockchain.db")

_COVERAGE_LABELS   = ["Bassa", "Media", "Alta"]
_COVERAGE_INTERNAL = {"Bassa": "low", "Media": "medium", "Alta": "high"}
_COVERAGE_REIMB    = {"Bassa": "50%", "Media": "70%", "Alta": "100%"}
_COVERAGE_FCOV     = {"Bassa": 0.70, "Media": 0.90, "Alta": 1.00}

st.set_page_config(
    page_title="MEV Insurance Simulator",
    page_icon="🛡️",
    layout="wide",
)


def fonte(testo: str) -> None:
    st.caption(f"📡 Fonte: {testo}")


def info_box(titolo: str, contenuto: str, colore: str = "blue") -> None:
    icons  = {"blue": "ℹ️", "green": "✅", "orange": "⚠️", "red": "❌"}
    colors = {"blue": "#1f77b4", "green": "#2ca02c", "orange": "#ff7f0e", "red": "#d62728"}
    c = colors.get(colore, colors["blue"])
    i = icons.get(colore, icons["blue"])
    st.markdown(
        f'<div style="border-left:4px solid {c}; padding:8px 12px; '
        f'background:{c}15; border-radius:4px; margin:4px 0">'
        f'{i} <b>{titolo}</b><br>{contenuto}</div>',
        unsafe_allow_html=True,
    )


# =========================================================================
# Session state
# =========================================================================
for _k, _v in [
    ("results",    {}),
    ("summaries",  {}),
    ("collectors", {}),
    ("batch_results", None),
    ("sim_seed_info", None),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


# =========================================================================
# HEADER
# =========================================================================
st.markdown(
    '<h1 style="margin-bottom:4px">🛡️ MEV Insurance Protocol Simulator</h1>',
    unsafe_allow_html=True,
)
st.markdown("---")

# =========================================================================
# SEZIONE 2 — PARAMETRI SIMULAZIONE
# =========================================================================
st.markdown("### ⚙️ Parametri Simulazione")

_c1, _c2, _c3, _c4, _c5 = st.columns(5)

with _c1:
    st.markdown("**Simulazione**")
    duration_days  = st.number_input(
        "Durata (giorni)", min_value=1, max_value=365, value=30, step=1, key="sim_duration",
    )
    coverage_label = st.selectbox(
        "Copertura", _COVERAGE_LABELS, index=2, key="sim_coverage",
        help="Bassa → 50% rimborso | Media → 70% | Alta → 100%",
    )
    swaps_per_day  = st.number_input(
        "Swap/giorno", min_value=10, max_value=10000, value=100, step=10,
        key="sim_swaps_day",
    )

with _c2:
    st.markdown("**Rischio**")
    patt_override = st.number_input(
        "Patt (tasso attacco)", min_value=0.01, max_value=0.50, value=0.10,
        step=0.01, format="%.2f", key="m2_patt_override",
        help="Probabilità base sandwich attack. Oscilla ±30%/giorno.",
    )
    loss_pct = st.number_input(
        "L% (perdita/attacco)", min_value=0.05, max_value=0.40, value=0.25,
        step=0.01, format="%.2f", key="prot_loss_pct",
    )
    e_fnr = st.number_input(
        "E — FNR", min_value=0.01, max_value=0.99, value=0.20,
        step=0.01, format="%.2f", key="fp_e_fnr",
        help="False Negative Rate. Usato come E/(1−E) nella formula.",
    )

with _c3:
    st.markdown("**Protocollo**")
    mbase = st.number_input(
        "Mbase", min_value=0.05, max_value=0.50, value=0.15,
        step=0.01, format="%.2f", key="prot_mbase",
    )
    sr_threshold_high = st.number_input(
        "Soglia SR sano", min_value=1.30, max_value=2.00, value=1.50,
        step=0.05, format="%.2f", key="prot_sr_high",
        help="SR ≥ soglia → M_adj = 0",
    )
    sr_threshold_med = st.number_input(
        "Soglia SR rischio", min_value=1.00, max_value=1.50, value=1.30,
        step=0.05, format="%.2f", key="prot_sr_med",
        help="SR < soglia → M_adj = 0.10",
    )

with _c4:
    st.markdown("**Pool & Frodi**")
    initial_pool_balance = st.number_input(
        "Saldo iniziale (ETH)", min_value=10.0, max_value=10000.0, value=50.0,
        step=10.0, key="prot_pool_balance",
    )
    _fraud_raw = st.number_input(
        "Frodi sui claim (%)", min_value=0, max_value=50, value=5, step=1,
        key="fraud_claim_pct_input",
        help="% claim fraudolenti aggiuntivi sugli attacchi reali.",
    )
    fraud_claim_pct = _fraud_raw / 100.0
    oracle_reward_per_claim = st.number_input(
        "Oracle reward/claim (ETH)", min_value=0.0001, max_value=0.05, value=0.002,
        step=0.0005, format="%.4f", key="oracle_reward_eth",
    )

with _c5:
    st.markdown("**Avanzati**")
    min_premium_pct = st.number_input(
        "Floor premio (%)", min_value=0.001, max_value=0.10, value=0.015,
        step=0.001, format="%.3f", key="fp_min_premium_pct",
        help="Floor minimo premio prima di Fcov. Default 1.5%.",
    )
    n_synthetic_users = st.number_input(
        "N utenti sintetici", min_value=5, max_value=500, value=50, step=5,
        key="m2_n_users",
        help="Numero di utenti al giorno 0. Cresce del ~2%/giorno automaticamente.",
    )
    max_daily_swaps = st.number_input(
        "Max swap/utente/gg", min_value=1, max_value=100, value=10, step=1,
        key="m2_max_daily_swaps",
    )

coverage = _COVERAGE_INTERNAL[coverage_label]
fcov     = _COVERAGE_FCOV[coverage_label]

st.markdown("---")

# =========================================================================
# Config builder
# =========================================================================

def _build_config(
    duration_days, swaps_per_day, coverage,
    mbase, loss_pct, sr_threshold_high, sr_threshold_med,
    initial_pool_balance,
    n_synthetic_users, max_daily_swaps,
    patt_override,
    e_fnr, fraud_claim_pct,
    oracle_reward_per_claim=0.002,
    min_premium_pct=0.015,
) -> dict:
    cfg = load_config(os.path.join(_ROOT, "config", "mode2_synthetic.yaml"))

    run_seed = int(_time_mod.time()) % 99999
    st.session_state["sim_seed_info"] = run_seed

    cfg["simulation"]["duration_days"]                = int(duration_days)
    cfg["simulation"]["seed"]                         = run_seed
    cfg["simulation"]["swaps_per_day"]                = int(swaps_per_day)
    cfg["pool"]["mbase"]                              = float(mbase)
    cfg["pool"]["initial_balance_eth"]                = float(initial_pool_balance)
    cfg["pool"]["solvency_thresholds"]["high_risk"]   = float(sr_threshold_med)
    cfg["pool"]["solvency_thresholds"]["medium_risk"] = float(sr_threshold_high)
    cfg["market"]["loss_pct_mean"]                    = float(loss_pct)
    cfg["market"]["e"]                                = float(e_fnr)
    cfg["simulation"]["fraud_claim_pct"]              = float(fraud_claim_pct)
    cfg.setdefault("market", {})["attack_rate"]       = float(patt_override)
    cfg["users"]["initial_count"]                     = int(n_synthetic_users)
    cfg["users"]["max_daily_swaps"]                   = int(max_daily_swaps)
    cfg.setdefault("oracles", {})["oracle_reward_per_claim"] = float(oracle_reward_per_claim)
    cfg.setdefault("premium", {})["min_premium_pct"]         = float(min_premium_pct)

    return cfg


def _make_cfg(**kwargs) -> dict:
    return _build_config(
        duration_days=kwargs.get("duration_days", duration_days),
        swaps_per_day=kwargs.get("swaps_per_day", swaps_per_day),
        coverage=kwargs.get("coverage", coverage),
        mbase=kwargs.get("mbase", mbase),
        loss_pct=kwargs.get("loss_pct", loss_pct),
        sr_threshold_high=kwargs.get("sr_threshold_high", sr_threshold_high),
        sr_threshold_med=kwargs.get("sr_threshold_med", sr_threshold_med),
        initial_pool_balance=kwargs.get("initial_pool_balance", initial_pool_balance),
        n_synthetic_users=kwargs.get("n_synthetic_users", n_synthetic_users),
        max_daily_swaps=kwargs.get("max_daily_swaps", max_daily_swaps),
        patt_override=kwargs.get("patt_override", patt_override),
        e_fnr=kwargs.get("e_fnr", e_fnr),
        fraud_claim_pct=kwargs.get("fraud_claim_pct", fraud_claim_pct),
        oracle_reward_per_claim=kwargs.get("oracle_reward_per_claim", oracle_reward_per_claim),
        min_premium_pct=kwargs.get("min_premium_pct", min_premium_pct),
    )


# =========================================================================
# SEZIONE 3 — TIPO SIMULAZIONE
# =========================================================================
st.markdown("### 🚀 Tipo Simulazione")
sim_type = st.radio(
    "Modalità simulazione",
    ["📊 Singola", "🔁 Batch"],
    horizontal=True,
    key="sim_type_radio",
    label_visibility="collapsed",
)

st.markdown("---")

# ==========================================================================
# SIMULAZIONE SINGOLA
# ==========================================================================
if sim_type == "📊 Singola":

    _btn_col, _seed_col = st.columns([1, 3])
    with _btn_col:
        run_btn = st.button("▶ Avvia Simulazione", type="primary", key="run_btn")
    with _seed_col:
        if st.session_state.get("sim_seed_info"):
            st.caption(f"🎲 Ultimo seed: {st.session_state['sim_seed_info']}")
        _export_btn = st.button("📥 Esporta CSV", key="export_btn")

    if run_btn:
        cfg = _make_cfg()
        with st.spinner("Simulazione in corso…"):
            collector, pool, summary = run_single(cfg, coverage=coverage, db_path=_DB_PATH)
            label = coverage_label
            st.session_state["results"]    = {label: collector.to_dataframe()}
            st.session_state["summaries"]  = {label: summary}
            st.session_state["collectors"] = {label: collector}
        st.success("✅ Simulazione completata!")

    if _export_btn and st.session_state["results"]:
        frames   = [df.assign(run=lbl) for lbl, df in st.session_state["results"].items()]
        csv_data = pd.concat(frames, ignore_index=True).to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇ Scarica CSV", data=csv_data,
            file_name="mev_risultati_simulazione.csv", mime="text/csv",
            key="csv_download_top",
        )

    results    = st.session_state["results"]
    summaries  = st.session_state["summaries"]
    collectors = st.session_state["collectors"]

    if not results:
        st.info("Configura i parametri e premi **▶ Avvia Simulazione**.")
    else:
        first_label     = next(iter(results))
        first_df        = results[first_label]
        first_summary   = summaries[first_label]
        first_collector = collectors.get(first_label)

        _bd = first_summary.get("breakdown_event")

        st.markdown("---")
        st.subheader("📈 Risultati")

        c0, c1, c2, c3 = st.columns(4)
        c0.metric("Profitto totale (ETH)", f"{first_summary['total_profit_eth']:.4f}")
        c1.metric("SR finale",             f"{first_summary['final_solvency_ratio']:.3f}")
        c2.metric("Saldo finale (ETH)",    f"{first_summary['final_balance_eth']:.4f}")
        c3.metric("Pool sopravvissuto",    "SÌ ✓" if first_summary["pool_survived"] else "NO ✗")

        if _bd:
            st.error(
                f"💥 **Pool esaurito al giorno {_bd['day']}** — {_bd['reason']}\n\n"
                f"Saldo: {_bd['pool_balance']:.4f} ETH"
            )
        else:
            st.success("✅ Il pool ha superato l'intera simulazione senza rotture.")

        st.markdown("---")

        # ---- Pannello 1: Salute Pool ----
        st.subheader("1 — Salute del Pool nel Tempo")
        fonte("InsurancePool: saldo ETH = premi incassati − payout erogati")

        _bd1_day = _bd["day"] if _bd else None
        pool_df = first_df[[
            "day", "pool_balance_eth", "pending_liabilities_eth",
            "solvency_ratio", "madj_current", "net_flow_today",
        ]].copy()
        pool_df.columns = [
            "Giorno", "Saldo Pool (ETH)", "Passività Pendenti (ETH)",
            "Solvency Ratio", "M_adj", "Variazione Netta (ETH)",
        ]

        def _get_stato(row):
            d   = int(row["Giorno"])
            bal = float(row["Saldo Pool (ETH)"])
            sr  = float(row["Solvency Ratio"])
            if _bd1_day is not None and d == _bd1_day:
                return "💥 ROTTURA"
            elif _bd1_day is not None and d > _bd1_day:
                return "⚠️ post-rottura"
            elif bal < 0:
                return "🔴 saldo negativo"
            elif sr < 1.3:
                return "🟠 rischio medio"
            else:
                return "🟢 sano"

        pool_df["Stato"] = pool_df.apply(_get_stato, axis=1)

        def _color_net(v):
            try:
                return "color: green" if float(v) >= 0 else "color: red"
            except Exception:
                return ""

        def _highlight_bd(row):
            d = int(row["Giorno"])
            if _bd1_day is not None and d == _bd1_day:
                return ["background-color: #ff4444; color: white"] * len(row)
            elif _bd1_day is not None and d == _bd1_day - 1:
                return ["background-color: #ffaa00; color: black"] * len(row)
            return [""] * len(row)

        st.dataframe(
            pool_df.style
            .format({
                "Saldo Pool (ETH)": "{:.4f}", "Passività Pendenti (ETH)": "{:.4f}",
                "Solvency Ratio": "{:.4f}", "M_adj": "{:.2f}",
                "Variazione Netta (ETH)": "{:+.4f}",
            })
            .applymap(_color_net, subset=["Variazione Netta (ETH)"])
            .apply(_highlight_bd, axis=1),
            use_container_width=True, height=300,
        )

        # ---- Trend saldo pool ----
        with st.expander("📈 Trend Saldo Pool", expanded=False):
            _balances = first_df["pool_balance_eth"].values.astype(float)
            _days_arr = np.arange(len(_balances), dtype=float)
            if len(_balances) > 1:
                _slope, _intercept = np.polyfit(_days_arr, _balances, 1)
            else:
                _slope, _intercept = 0.0, float(_balances[0]) if len(_balances) else 0.0
            _trend_str = f"📈 +{_slope:.4f} ETH/giorno" if _slope >= 0 else f"📉 {_slope:.4f} ETH/giorno"
            _giorni_zero = (-_intercept / _slope) if _slope < 0 else None
            st.markdown(
                f"**Trend lineare:** {_trend_str}  \n"
                + (f"⚠️ Al trend attuale il pool si azzera in ~**{int(_giorni_zero)}** giorni"
                   if _giorni_zero and _giorni_zero > 0 else "✅ Trend positivo o neutro")
            )
            _changes = [_balances[i] - _balances[i-1] for i in range(1, len(_balances))]
            _ch_df = pd.DataFrame({
                "Giorno": list(range(1, len(_changes)+1)),
                "Variazione (ETH)": [round(c, 4) for c in _changes],
                "Saldo (ETH)": [round(b, 4) for b in _balances[1:]],
                "Δ": ["📈" if c >= 0 else "📉" for c in _changes],
            })
            st.dataframe(_ch_df, use_container_width=True, hide_index=True, height=200)

        st.markdown("---")

        # ---- Pannello 2: Flusso di Cassa ----
        st.subheader("2 — Flusso di Cassa")
        fonte("ETH cumulativi: premi da utenti, payout ai claim approvati")

        last = first_df.iloc[-1]
        ca, cb, cc = st.columns(3)
        ca.metric("Premi Totali (ETH)",   f"{float(last['total_premiums_collected_eth']):.4f}")
        cb.metric("Payout Totali (ETH)",  f"{float(last['total_payouts_eth']):.4f}")
        cc.metric("Profitto Netto (ETH)", f"{float(last['profit_eth']):.4f}")

        cf_df = first_df[["day", "premiums_today", "payouts_today", "net_flow_today"]].copy()
        cf_df.columns = ["Giorno", "Premi Oggi (ETH)", "Payout Oggi (ETH)", "Flusso Netto (ETH)"]
        st.dataframe(
            cf_df.style.format({c: "{:.4f}" for c in cf_df.columns if c != "Giorno"}),
            use_container_width=True, height=250,
        )

        st.markdown("---")

        # ---- Pannello 3: Analisi Claim ----
        st.subheader("3 — Analisi Claim")
        fonte("Ogni claim = swap attaccato; auto-approvato con payout = loss × Fcov_rimborso")

        tot_sub = int(first_df["n_claims_submitted"].sum())
        tot_app = int(first_df["n_claims_approved"].sum())
        avg_pay = first_df["avg_payout_eth"].replace(0, np.nan).mean()
        ce1, ce2, ce3 = st.columns(3)
        ce1.metric("Claim totali inviati", str(tot_sub))
        ce2.metric("Claim approvati",      str(tot_app))
        ce3.metric("Payout medio (ETH)",   f"{avg_pay:.4f}" if not np.isnan(avg_pay) else "N/A")

        cl_df = first_df[["day", "n_claims_submitted", "n_claims_approved", "avg_payout_eth"]].copy()
        cl_df.columns = ["Giorno", "Claim Inviati", "Claim Approvati", "Payout Medio (ETH)"]
        st.dataframe(
            cl_df.style.format({"Payout Medio (ETH)": "{:.4f}"}),
            use_container_width=True, height=250,
        )

        # ---- Pannello 4: Utenti ----
        if True:
            st.markdown("---")
            st.subheader("4 — Distribuzione Utenti")
            fonte("Utenti sintetici da SyntheticDataSource — solo simulazione sintetica")

            _ud_cols = [c for c in ["day", "n_users_active"] if c in first_df.columns]
            ud_df = first_df[_ud_cols].copy()
            ud_df.columns = ["Giorno", "Utenti Attivi"][:len(_ud_cols)]
            st.dataframe(ud_df, use_container_width=True, height=250)

        # ---- Flussi Economici ----
        st.markdown("---")
        st.subheader("💰 Flussi Economici")

        lr = first_df.iloc[-1]
        premi_tot  = float(lr["total_premiums_collected_eth"])
        payout_tot = float(lr["total_payouts_eth"])
        profit_tot = float(lr["profit_eth"])
        saldo_fin  = float(lr["pool_balance_eth"])
        ref        = max(premi_tot, 1e-9)

        eco_df = pd.DataFrame({
            "Voce": ["Premi incassati", "Payout erogati", "Profitto netto", "Saldo finale"],
            "Importo (ETH)": [premi_tot, payout_tot, profit_tot, saldo_fin],
        })
        eco_df["% dei premi"] = eco_df["Importo (ETH)"].apply(lambda x: f"{abs(x)/ref*100:.1f}%")
        eco_df["Importo (ETH)"] = eco_df["Importo (ETH)"].apply(lambda x: f"{x:.4f}")
        st.dataframe(eco_df, use_container_width=True, hide_index=True)
        fonte("InsurancePool — cumulativo al termine della simulazione")

        base_risk = patt_override * loss_pct
        prem_ex2  = base_risk * (1 + mbase) * fcov
        atk_frac  = base_risk * fcov / prem_ex2 * 100 if prem_ex2 > 0 else 0
        marg_frac = base_risk * mbase * fcov / prem_ex2 * 100 if prem_ex2 > 0 else 0
        st.markdown(
            f"**Su ogni ETH di premio:** ~{atk_frac:.1f}% copre rischio attacchi | "
            f"~{marg_frac:.1f}% è margine\n"
        )

        st.markdown("---")

        # ---- Esploratore Giornaliero ----
        st.subheader("5 — Esploratore Giornaliero")

        _bd6_day = _bd["day"] if _bd else None
        max_day  = int(first_df["day"].max())
        selected_day = st.slider(
            "Seleziona giorno", min_value=0, max_value=max_day,
            value=_bd6_day if _bd6_day else 0, step=1, key="day_explorer_slider",
        )

        if _bd6_day is not None and selected_day == _bd6_day:
            st.error(f"💥 **GIORNO DI ROTTURA** — {_bd['reason']} | Saldo: {_bd['pool_balance']:.4f} ETH")
        elif _bd6_day is not None and selected_day == _bd6_day - 1:
            st.warning("⚠️ Giorno precedente alla rottura.")

        row = first_df[first_df["day"] == selected_day]
        if not row.empty:
            row = row.iloc[0]
            n_sw     = int(row.get("n_swaps_this_tick", 0))
            n_ra     = int(row.get("n_real_attacks", 0))
            n_fc     = int(row.get("n_fraud_caught", 0))
            n_fe     = int(row.get("n_fraud_escaped", 0))
            n_normal = max(n_sw - n_ra - n_fc - n_fe, 0)
            net      = float(row.get("net_flow_today", 0.0))
            pr_today = float(row.get("payout_real_today", 0.0))
            pf_today = float(row.get("payout_fraud_today", 0.0))
            tint_d   = float(row.get("tint_today", 0.0))
            vbase_d  = float(row.get("vbase_today", 100.0))
            oc_today = float(row.get("oracle_cost_today", 0.0))
            nor_today = int(row.get("n_oracles_used_today", 0))

            st.dataframe(pd.DataFrame({
                "Metrica": [
                    "Swap totali", "  — normali", "  — attacchi reali",
                    "  — frodi intercettate", "  — frodi scappate",
                    "Premi incassati (tutti)", "Payout erogati",
                    "    da attacchi reali", "    da frodi scappate",
                    "Costo oracle totale oggi", "  — oracle utilizzati",
                    "Tint (frodi intercettate × avg_swap)", "Vbase (swap ieri)",
                    "Variazione netta pool", "Saldo pool",
                    "Solvency Ratio (solo per M_adj)", "M_adj applicato oggi",
                ],
                "Valore": [
                    str(n_sw),
                    str(n_normal),
                    f"{n_ra} ({n_ra/max(n_sw,1):.1%})",
                    f"{n_fc} ({n_fc/max(n_sw,1):.1%})",
                    f"{n_fe} ({n_fe/max(n_sw,1):.1%})",
                    f"{float(row.get('premiums_today',0)):.4f} ETH",
                    f"{float(row.get('payouts_today',0)):.4f} ETH",
                    f"{pr_today:.4f} ETH",
                    f"{pf_today:.4f} ETH",
                    f"{oc_today:.4f} ETH",
                    f"{nor_today} oracle-claim",
                    f"{tint_d:.4f} ETH",
                    f"{vbase_d:.0f} swap",
                    f"{'+' if net>=0 else ''}{net:.4f} ETH",
                    f"{float(row['pool_balance_eth']):.4f} ETH",
                    f"{float(row['solvency_ratio']):.4f}",
                    f"{float(row.get('madj_current',0)):.4f}",
                ],
            }), use_container_width=True, hide_index=True)

            # ---- Term1 / Term2 / Term3 ----
            st.markdown("#### 📐 Term1, Term2 e Term3 del Giorno")
            _t1_vis   = float(row.get("patt_current", 0.0)) * float(row.get("loss_pct_current", loss_pct))
            _e_vis    = max(min(float(row.get("e_today", e_fnr)), 0.9999), 0.0001)
            _tint_vis = float(row.get("tint_today", 0.0))
            _vbase_vis= float(row.get("vbase_today", 100.0))
            _t2_vis   = (_tint_vis * (_e_vis / (1.0 - _e_vis))) / max(_vbase_vis, 1.0) if _vbase_vis > 0 else 0.0
            _t3_vis   = float(row.get("term3_today", 0.0))
            _mtot_vis = float(row.get("m_total_current", mbase))
            _rate_vis = (_t1_vis + _t2_vis + _t3_vis) * (1.0 + _mtot_vis) * fcov * 100.0
            _cv1, _cv2, _cv3, _cv4 = st.columns(4)
            _cv1.metric("Term1 (Patt × L%)", f"{_t1_vis:.4f}", help="Rischio attacchi reali")
            _cv2.metric("Term2 (frodi non rilevate)", f"{_t2_vis:.4f}", help="Costo frodi che sfuggono al rilevamento")
            _cv3.metric("Term3 (costi oracle)", f"{_t3_vis:.4f}", help="C_oracle_24h / Vbase")
            _cv4.metric("Premio medio applicato", f"{_rate_vis:.3f}%", help="% del valore swap pagata come premio")
            _tot_vis = (_t1_vis + _t2_vis + _t3_vis) or 1e-9
            _pct1_vis = _t1_vis / _tot_vis * 100
            _pct2_vis = _t2_vis / _tot_vis * 100
            _pct3_vis = _t3_vis / _tot_vis * 100
            st.markdown(
                f'<div style="display:flex;height:20px;border-radius:4px;overflow:hidden;margin:4px 0">'
                f'<div style="width:{_pct1_vis:.0f}%;background:#1f77b4;display:flex;align-items:center;'
                f'justify-content:center;color:white;font-size:11px">Term1 {_pct1_vis:.0f}%</div>'
                f'<div style="width:{_pct2_vis:.0f}%;background:#ff7f0e;display:flex;align-items:center;'
                f'justify-content:center;color:white;font-size:11px">Term2 {_pct2_vis:.0f}%</div>'
                f'<div style="width:{_pct3_vis:.0f}%;background:#2ca02c;display:flex;align-items:center;'
                f'justify-content:center;color:white;font-size:11px">Term3 {_pct3_vis:.0f}%</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            # ---- Formula breakdown per giorno selezionato ----
            with st.expander("📐 Formula Premio — Calcolo del Giorno", expanded=False):
                _patt_d  = float(row.get("patt_current", 0.0))
                _tint_d  = float(row.get("tint_today", 0.0))
                _vbase_d = float(row.get("vbase_today", 100.0))
                _e_d     = float(row.get("e_today", e_fnr))
                _e_safe  = max(min(_e_d, 0.9999), 0.0001)
                _e_ratio = _e_safe / (1.0 - _e_safe)
                _madj_d  = float(row.get("madj_current", 0.0))
                _mtot_d  = float(row.get("m_total_current", mbase))
                _term1   = _patt_d * loss_pct
                _term2   = (_tint_d * _e_ratio) / max(_vbase_d, 1.0) if _vbase_d > 0 else 0.0
                _term3   = float(row.get("term3_today", 0.0))
                _sum_t   = _term1 + _term2 + _term3
                _ex_prem = 1.0 * _sum_t * (1.0 + _mtot_d) * fcov
                _n_fc_d  = int(row.get("n_fraud_caught", 0))
                _avg_sv  = float(row.get("avg_swap_value_eth", 0.0))
                _n_fe_d  = int(row.get("n_fraud_escaped", 0))
                _oc_d    = float(row.get("oracle_cost_today", 0.0))
                _vb_src  = "swap sintetici assicurati ieri"
                st.code(
                    f"Vbase        = {_vbase_d:.0f} swap  ({_vb_src})\n"
                    f"Tint         = {_tint_d:.4f} ETH  ({_n_fc_d} frodi × {_avg_sv:.4f} ETH/swap)\n"
                    f"C_oracle_24h = {_oc_d:.4f} ETH  (costo oracle giorno D-1)\n"
                    f"\n"
                    f"Patt          = {_patt_d:.5f}\n"
                    f"L%            = {loss_pct:.3f}\n"
                    f"Term1         = Patt × L%                           = {_term1:.6f}\n"
                    f"\n"
                    f"E (FNR)       = {_e_d:.3f}  →  E/(1−E)             = {_e_ratio:.4f}\n"
                    f"Term2         = (Tint × E/(1−E)) / Vbase             = {_term2:.6f}\n"
                    f"\n"
                    f"Term3         = C_oracle_24h / Vbase                 = {_term3:.6f}\n"
                    f"\n"
                    f"──────────────────────────────────────────────────────────────\n"
                    f"Sum           = Term1 + Term2 + Term3                = {_sum_t:.6f}\n"
                    f"Mbase         = {mbase:.3f}\n"
                    f"M_adj         = {_madj_d:.4f}\n"
                    f"M_tot         = Mbase + M_adj                        = {_mtot_d:.4f}\n"
                    f"Fcov          = {fcov:.2f}  ({coverage_label})\n"
                    f"──────────────────────────────────────────────────────────────\n"
                    f"Esempio 1 ETH: P = 1 × {_sum_t:.6f} × (1 + {_mtot_d:.4f}) × {fcov:.2f}\n"
                    f"             = {_ex_prem:.6f} ETH\n"
                    f"\n"
                    f"Frodi oggi:   {int(row.get('n_fraud_attempts',0))} tentativi "
                    f"| {_n_fc_d} intercettate | {_n_fe_d} scappate",
                    language=None,
                )

            if first_collector:
                day_swaps = first_collector.daily_swap_details.get(selected_day, [])
                _n_real = sum(1 for d in day_swaps if d.get("tipo_claim") == "reale")
                _n_fi   = sum(1 for d in day_swaps if d.get("tipo_claim") == "frode_intercettata")
                _n_fs   = sum(1 for d in day_swaps if d.get("tipo_claim") == "frode_scappata")
                with st.expander(
                    f"Swap del giorno {selected_day} "
                    f"({len(day_swaps)} righe: {_n_real} reali, {_n_fi} frodi_catch, {_n_fs} frodi_esc)",
                    expanded=False,
                ):
                    if day_swaps:
                        sw_df = pd.DataFrame(day_swaps)
                        col_rename = {
                            "swap_id": "Swap ID", "value_ETH": "Valore (ETH)",
                            "was_attacked": "Attaccato", "coverage_level": "Copertura",
                            "premium_paid": "Premio (ETH)", "premium_pct": "Premio (%)",
                            "claim_submitted": "Claim?", "claim_approved": "Approvato?",
                            "payout_ETH": "Rimborso (ETH)", "rimborso_pct": "Rimborso (%)",
                            "tipo_claim": "Tipo Claim",
                        }
                        sw_disp = sw_df[[c for c in col_rename if c in sw_df.columns]].rename(columns=col_rename)
                        for cn in ("Valore (ETH)", "Premio (ETH)", "Rimborso (ETH)"):
                            if cn in sw_disp.columns:
                                sw_disp[cn] = sw_disp[cn].round(6)
                        for cn in ("Premio (%)", "Rimborso (%)"):
                            if cn in sw_disp.columns:
                                sw_disp[cn] = sw_disp[cn].round(4)
                        st.dataframe(sw_disp, use_container_width=True)
                        st.caption(
                            "💡 Tipo Claim: **reale** = attacco reale | "
                            "**frode_intercettata** = frode bloccata (no payout) | "
                            "**frode_scappata** = frode non rilevata (payout erogato) | "
                            "**nessuno** = swap non attaccato"
                        )
                    else:
                        st.info("Nessun dettaglio swap per questo giorno.")

        with st.expander("📊 Dati grezzi simulazione", expanded=False):
            _brd = first_df.copy()
            def _st_full(r):
                d, bal = int(r["day"]), float(r.get("pool_balance_eth", 0))
                if _bd6_day and d == _bd6_day: return "💥 ROTTURA"
                elif _bd6_day and d > _bd6_day: return "⚠️ post-rottura"
                elif bal < 0: return "🔴 saldo negativo"
                elif float(r.get("solvency_ratio", 1.5)) < 1.3: return "🟠 rischio medio"
                else: return "🟢 sano"
            _brd.insert(1, "Stato", _brd.apply(_st_full, axis=1))
            st.dataframe(_brd, use_container_width=True)

        with st.expander("📐 Formula per giorno", expanded=False):
            _fcov_v = _COVERAGE_FCOV[coverage_label]
            rows_f  = []
            for _, fr in first_df.iterrows():
                patt_d  = float(fr.get("patt_current", 0.05))
                madj_d  = float(fr.get("madj_current", 0.0))
                m_tot   = mbase + madj_d
                tint_d  = float(fr.get("tint_today", 0.0))
                vbase_d = float(fr.get("vbase_today", 100.0))
                e_d     = float(fr.get("e_today", e_fnr))
                e_safe  = max(min(e_d, 0.9999), 0.0001)
                t1      = patt_d * loss_pct
                t2      = (tint_d * (e_safe / (1.0 - e_safe))) / max(vbase_d, 1.0) if vbase_d > 0 else 0.0
                t3      = float(fr.get("term3_today", 0.0))
                prem_ex = (t1 + t2 + t3) * (1 + m_tot) * _fcov_v
                n_fc    = int(fr.get("n_fraud_caught", 0))
                n_fe    = int(fr.get("n_fraud_escaped", 0))
                avg_sv  = float(fr.get("avg_swap_value_eth", 0.0))
                _vb_src = "sintetico D-1"
                rows_f.append({
                    "Giorno":     int(fr["day"]),
                    "Patt":       f"{patt_d:.3%}",
                    "L%":         f"{loss_pct:.2%}",
                    "Vbase":      f"{vbase_d:.0f} ({_vb_src})",
                    "Tint (ETH)": f"{tint_d:.4f} ({n_fc}×{avg_sv:.3f})",
                    "Term1":      f"{t1:.6f}",
                    "Term2":      f"{t2:.6f}",
                    "M_tot":      f"{m_tot:.3f}",
                    "Fcov":       f"{_fcov_v:.2f}",
                    "P/V (1 ETH)": f"{prem_ex:.5f}",
                    "SR":         f"{float(fr.get('solvency_ratio',0)):.4f}",
                    "Frodi catch": n_fc,
                    "Frodi esc":   n_fe,
                })
            st.dataframe(pd.DataFrame(rows_f), use_container_width=True, hide_index=True, height=300)
            fonte("P = V × [(Patt × L%) + (Tint × E/(1−E)) / Vbase + C_oracle_24h / Vbase] × (1+M) × Fcov")

        with st.expander("⬇️ Scarica CSV completo", expanded=False):
            _csv = first_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "📥 Scarica CSV", data=_csv,
                file_name="mev_simulazione.csv", mime="text/csv",
                key="csv_full_download",
            )
            st.caption(f"{len(first_df)} righe × {len(first_df.columns)} colonne")


# ==========================================================================
# BATCH SIMULAZIONI
# ==========================================================================
elif sim_type == "🔁 Batch":

    st.subheader("🔁 Batch Simulazioni")
    st.markdown("Esegui N simulazioni variando i parametri con prodotto cartesiano.")

    b1, b2 = st.columns([1, 2])
    with b1:
        _bn_steps = st.number_input(
            "Passi per parametro", min_value=2, max_value=100, value=5, step=1,
            key="batch_n_steps",
            help="N valori equidistanti per ogni parametro in modalità Range",
        )
        _runs_per_step = st.number_input(
            "Runs per passo (media)", min_value=1, max_value=50, value=1, step=1,
            key="batch_runs_per_step",
            help="Se > 1, ogni combinazione viene eseguita N volte e i risultati vengono mediati.",
        )
    with b2:
        _b_params = st.multiselect(
            "Parametri variabili",
            ["Patt", "L%", "E (FNR)", "Mbase", "Frodi%", "Pool iniziale", "Oracle reward", "Min premio%"],
            default=["Patt", "E (FNR)"],
            key="batch_vars",
        )

    _b_param_config: dict = {}
    if _b_params:
        st.markdown("**Range per parametro variabile:**")
        _pc_cols = st.columns(min(len(_b_params), 4))
        _bp_defaults = {
            "Patt":          (0.02, 0.20),
            "L%":            (0.05, 0.40),
            "E (FNR)":       (0.05, 0.50),
            "Mbase":         (0.05, 0.40),
            "Frodi%":        (0.0,  0.30),
            "Pool iniziale": (20.0, 200.0),
            "Oracle reward": (0.001, 0.01),
            "Min premio%":   (0.005, 0.05),
        }
        for _bi, _bp in enumerate(_b_params):
            with _pc_cols[_bi % len(_pc_cols)]:
                st.markdown(f"*{_bp}*")
                _bmode_p = st.selectbox(
                    "Modalità", ["Range lineare", "Casuale nel range", "Fisso"],
                    key=f"batch_pmode_{_bi}",
                )
                _def_min, _def_max = _bp_defaults.get(_bp, (0.01, 0.50))
                if _bmode_p == "Fisso":
                    _bfv = st.number_input(
                        "Valore fisso", value=round((_def_min+_def_max)/2, 4),
                        key=f"batch_fixed_{_bi}",
                    )
                    _b_param_config[_bp] = {"mode": "fixed", "value": float(_bfv)}
                else:
                    _bmin = st.number_input("Min", value=_def_min, key=f"batch_min_{_bi}")
                    _bmax = st.number_input("Max", value=_def_max, key=f"batch_max_{_bi}")
                    _b_param_config[_bp] = {
                        "mode": "range" if _bmode_p == "Range lineare" else "random",
                        "min": float(_bmin), "max": float(_bmax),
                    }

    def _batch_combos(pcfg: dict, n_steps: int) -> list:
        pv = {}
        for name, cfg in pcfg.items():
            if cfg["mode"] == "fixed":
                pv[name] = [cfg["value"]]
            elif cfg["mode"] == "range":
                pv[name] = list(np.linspace(cfg["min"], cfg["max"], n_steps))
            else:
                pv[name] = list(np.random.uniform(cfg["min"], cfg["max"], n_steps))
        keys   = list(pv.keys())
        combos = list(_itertools_product(*[pv[k] for k in keys]))
        return [dict(zip(keys, c)) for c in combos]

    _total_combos    = len(_batch_combos(_b_param_config, int(_bn_steps))) if _b_param_config else int(_bn_steps)
    _total_runs_batch = _total_combos * int(_runs_per_step)

    _rc1, _rc2 = st.columns(2)
    _rc1.info(f"**{_total_combos}** combinazioni × {int(_runs_per_step)} run = **{_total_runs_batch}** simulazioni totali")
    if _total_runs_batch > 400:
        _rc2.warning(f"⚠️ {_total_runs_batch} simulazioni — riduci passi/runs/parametri")
    elif _total_runs_batch > 100:
        _rc2.info(f"ℹ️ {_total_runs_batch} simulazioni — potrebbe richiedere qualche minuto")

    _batch_run_btn = st.button("✅ Avvia Batch", type="primary", key="batch_run_btn")

    if _batch_run_btn:
        _b_combos   = _batch_combos(_b_param_config, int(_bn_steps))
        _b_results  = []
        _b_prog     = st.progress(0, text="Avvio batch…")
        _b_cfg_base = _make_cfg()
        _rps        = int(_runs_per_step)
        _n_combos   = max(len(_b_combos), 1)
        _total_sims = _n_combos * _rps
        _batch_base_seed = int(_time_mod.time() * 1000) % 99999

        def _apply_params(rc: dict, run_p: dict) -> None:
            if "Patt"          in run_p: rc["market"]["attack_rate"]                      = run_p["Patt"]
            if "L%"            in run_p: rc["market"]["loss_pct_mean"]                    = run_p["L%"]
            if "E (FNR)"       in run_p: rc["market"]["e"]                                = run_p["E (FNR)"]
            if "Mbase"         in run_p: rc["pool"]["mbase"]                              = run_p["Mbase"]
            if "Frodi%"        in run_p: rc["simulation"]["fraud_claim_pct"]              = run_p["Frodi%"]
            if "Pool iniziale" in run_p: rc["pool"]["initial_balance_eth"]                = run_p["Pool iniziale"]
            if "Oracle reward" in run_p: rc.setdefault("oracles", {})["oracle_reward_per_claim"] = run_p["Oracle reward"]
            if "Min premio%"   in run_p: rc.setdefault("premium", {})["min_premium_pct"] = run_p["Min premio%"]

        _sim_done = 0
        for _bi, _run_p in enumerate(_b_combos):
            _run_p_rounded = {k: round(float(v), 6) for k, v in _run_p.items()}

            _rc_snap = copy.deepcopy(_b_cfg_base)
            _apply_params(_rc_snap, _run_p)
            _oracle_reward_val = float(
                _rc_snap.get("oracles", {}).get(
                    "oracle_reward_per_claim",
                    _rc_snap.get("oracles", {}).get("reward_patt_update_eth", oracle_reward_per_claim),
                )
            )
            _min_prem_val = float(_rc_snap.get("premium", {}).get("min_premium_pct", min_premium_pct))

            _run_summaries = []
            _run_dfs       = []
            for _ri in range(_rps):
                _rc = copy.deepcopy(_b_cfg_base)
                _rc["simulation"]["seed"] = (_batch_base_seed + _bi * 100 + _ri) % 99999
                _apply_params(_rc, _run_p)
                _sim_done += 1
                _b_prog.progress(
                    _sim_done / _total_sims,
                    text=f"Passo {_bi+1}/{_n_combos}, run {_ri+1}/{_rps} (tot {_sim_done}/{_total_sims})…",
                )
                try:
                    _c, _p_pool, _s = run_single(_rc, coverage=coverage, db_path=_DB_PATH)
                    _run_summaries.append(_s)
                    _run_dfs.append(_c.to_dataframe())
                except Exception as _bex:
                    _run_summaries.append({
                        "_error": str(_bex), "pool_survived": False,
                        "total_profit_eth": 0.0, "final_solvency_ratio": 0.0,
                        "breakdown_event": None, "trend_slope": 0.0,
                        "avg_premium_rate_pct": 0.0, "avg_term1": 0.0,
                        "avg_term2": 0.0, "total_oracle_cost_eth": 0.0,
                        "total_real_payouts_eth": 0.0, "total_fraud_payouts_eth": 0.0,
                    })
                    _run_dfs.append(None)

            _n_ok    = len(_run_summaries)
            _n_surv  = sum(1 for s in _run_summaries if s.get("pool_survived", False))
            _surv_rate = _n_surv / max(_n_ok, 1)

            def _smean(key: str, default: float = 0.0) -> float:
                vals = [s.get(key, default) for s in _run_summaries if not s.get("_error")]
                return float(np.mean(vals)) if vals else default

            _gg_rot_vals = [
                s.get("breakdown_event", {}).get("day")
                for s in _run_summaries
                if s.get("breakdown_event") and not s.get("_error")
            ]
            _giorno_rottura = min(_gg_rot_vals) if _gg_rot_vals else "—"
            _repr_df = next((df for df in _run_dfs if df is not None), None)

            _b_results.append({
                "run": _bi + 1,
                **_run_p_rounded,
                "oracle_reward_per_claim": round(_oracle_reward_val, 6),
                "min_premium_pct":         round(_min_prem_val, 6),
                "runs_per_step":           _rps,
                "pool_survival_rate":      round(_surv_rate, 4),
                "pool_survived":           _surv_rate == 1.0,
                "profitto_eth":            round(_smean("total_profit_eth"), 4),
                "sr_finale":               round(_smean("final_solvency_ratio"), 4),
                "giorno_rottura":          _giorno_rottura,
                "trend_eth_giorno":        round(_smean("trend_slope"), 4),
                "premio_medio_pct":        round(_smean("avg_premium_rate_pct"), 4),
                "term1_medio":             round(_smean("avg_term1"), 6),
                "term2_medio":             round(_smean("avg_term2"), 6),
                "oracle_cost_totale":      round(_smean("total_oracle_cost_eth"), 4),
                "payout_reali_eth":        round(_smean("total_real_payouts_eth"), 4),
                "payout_frodi_eth":        round(_smean("total_fraud_payouts_eth"), 4),
                "_df":                     _repr_df,
            })

        st.session_state["batch_results"] = _b_results
        _b_prog.progress(1.0, text="✅ Batch completato!")
        st.rerun()

    _br = st.session_state.get("batch_results")
    if _br:
        _br_clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in _br]
        _br_df    = pd.DataFrame(_br_clean)

        st.markdown("---")
        _mc1, _mc2, _mc3, _mc4 = st.columns(4)
        _n_surv_b  = sum(1 for r in _br_clean if r.get("pool_survived", False))
        _prem_vals = [r.get("premio_medio_pct", 0) for r in _br_clean]
        _mc1.metric("Simulazioni totali", len(_br_clean))
        _mc2.metric("Pool sopravvissuti", _n_surv_b,
                    delta=f"{_n_surv_b/max(len(_br_clean),1):.0%}")
        _mc3.metric("Trend positivo", sum(1 for r in _br_clean if r.get("trend_eth_giorno", 0) > 0))
        _mc4.metric("Premio medio", f"{np.mean(_prem_vals):.3f}%")

        st.subheader("A — Riepilogo tutte le run")

        def _color_row(row):
            if not row.get("pool_survived", True):
                return ["background-color: #ffcccc"] * len(row)
            elif row.get("trend_eth_giorno", 0) > 0:
                return ["background-color: #ccffcc"] * len(row)
            return [""] * len(row)

        st.dataframe(
            _br_df.style.apply(_color_row, axis=1),
            use_container_width=True, hide_index=True, height=350,
        )
        st.download_button(
            "📥 Scarica CSV batch",
            data=_br_df.to_csv(index=False).encode("utf-8"),
            file_name=f"batch_{len(_br)}runs.csv",
            mime="text/csv",
            key="batch_csv_dl",
        )

        st.markdown("---")
        st.subheader("B — Simulazioni fallite")
        _failed = [r for r in _br_clean if not r.get("pool_survived", True)]
        _fa, _fb = st.columns(2)
        _fa.metric("Simulazioni fallite", f"{len(_failed)}/{len(_br)}")
        _fb.metric("Tasso sopravvivenza", f"{(1-len(_failed)/max(len(_br),1))*100:.0f}%")
        if _failed:
            st.dataframe(pd.DataFrame(_failed), use_container_width=True, hide_index=True)

        st.markdown("---")
        st.subheader("C — Distribuzione trend")
        _slopes = [r.get("trend_eth_giorno", 0) for r in _br_clean if r.get("trend_eth_giorno") is not None]
        if _slopes:
            _pos = sum(1 for s in _slopes if s > 0)
            st.markdown(
                f"Trend positivo (pool cresce): **{_pos}/{len(_slopes)}** simulazioni  \n"
                f"Trend medio: **{np.mean(_slopes):.4f} ETH/giorno**  \n"
                f"Miglior trend: **{max(_slopes):.4f}** ETH/giorno  \n"
                f"Peggior trend: **{min(_slopes):.4f}** ETH/giorno"
            )
            _sl_df = pd.DataFrame({"Run": range(1, len(_slopes)+1), "Trend (ETH/gg)": _slopes})
            st.bar_chart(_sl_df.set_index("Run"))

        st.markdown("---")
        st.subheader("D — Dettaglio singola run")
        _sel_run = st.selectbox(
            "Seleziona run",
            [f"Run {r['run']}" for r in _br_clean],
            key="batch_detail_run",
        )
        _sel_idx = int(_sel_run.split()[1]) - 1
        _sel_r   = _br[_sel_idx]
        _sel_df  = _sel_r.get("_df")
        st.json({k: v for k, v in _sel_r.items() if not k.startswith("_")})
        if _sel_df is not None:
            st.dataframe(_sel_df, use_container_width=True, height=300)
