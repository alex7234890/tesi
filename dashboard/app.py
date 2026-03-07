"""
MEV Insurance Protocol — Streamlit Dashboard (v3: full premium formula, no oracle costs).

Avvio:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import copy
import datetime
import os
import time as _time_mod
import sqlite3
import sys
import glob as _glob

import numpy as np
import pandas as pd
import streamlit as st

_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.config_loader import load_config
from runner import run_single

_DB_PATH   = os.path.join(_ROOT, "data", "blockchain.db")
_CACHE_DIR = os.path.join(_ROOT, "cache")

_COVERAGE_LABELS   = ["Bassa", "Media", "Alta"]
_COVERAGE_INTERNAL = {"Bassa": "low", "Media": "medium", "Alta": "high"}
_COVERAGE_REIMB    = {"Bassa": "50%", "Media": "70%", "Alta": "100%"}
_COVERAGE_FCOV     = {"Bassa": 0.70, "Media": 0.90, "Alta": 1.00}

_DEX_OPTIONS = ["Uniswap V2", "Uniswap V3", "Sushiswap", "Curve"]

# =========================================================================
# Configurazione pagina
# =========================================================================
st.set_page_config(
    page_title="MEV Insurance Simulator",
    page_icon="🛡️",
    layout="wide",
)


def fonte(testo: str) -> None:
    st.caption(f"📡 Fonte: {testo}")


# =========================================================================
# Session state
# =========================================================================
def _read_infura_key_from_config() -> str:
    try:
        cfg = load_config(os.path.join(_ROOT, "config", "base.yaml"))
        url = cfg.get("infura_url", "")
        if "/v3/" in url:
            return url.split("/v3/")[-1].rstrip("/")
    except Exception:
        pass
    return ""

for _k, _v in [
    ("results",    {}),
    ("summaries",  {}),
    ("collectors", {}),
    ("last_mode",  2),
    ("infura_api_key", _read_infura_key_from_config()),
    ("confirm_clear_cache", False),
    ("infura_patt_value", None),
    ("infura_last_result", None),
    ("batch_results", None),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


def _db_swap_count() -> int:
    if not os.path.isfile(_DB_PATH):
        return 0
    try:
        con = sqlite3.connect(_DB_PATH, check_same_thread=False)
        n = con.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
        con.close()
        return int(n)
    except Exception:
        return 0


# =========================================================================
# SIDEBAR
# =========================================================================
st.sidebar.title("🛡️ MEV Insurance Simulator")
st.sidebar.markdown("---")

mode = st.sidebar.radio(
    "Modalità",
    [1, 2],
    format_func=lambda m: "Mode 1 — Dati Reali" if m == 1 else "Mode 2 — Sintetica",
    index=1,
    key="mode_select",
)
is_mode1 = mode == 1
is_mode2 = mode == 2

if is_mode1:
    _n_swaps = _db_swap_count()
    if _n_swaps > 0:
        st.sidebar.success(f"✅ DB Infura: {_n_swaps:,} swap disponibili")
    else:
        st.sidebar.error(
            "⚠️ DB Infura vuoto — vai alla tab **🔗 Dati Infura** e scarica i dati "
            "prima di avviare la simulazione Mode 1."
        )

st.sidebar.markdown("---")
st.sidebar.markdown("### Parametri Simulazione")

duration_days = st.sidebar.number_input(
    "Durata simulazione (giorni)", min_value=1, max_value=365, value=30, step=1,
    key="sim_duration",
)
swaps_per_day = st.sidebar.number_input(
    "N swap/giorno (solo Mode 2)", min_value=10, max_value=10000, value=100, step=10,
    key="sim_swaps_day", disabled=is_mode1,
)
coverage_label = st.sidebar.selectbox(
    "Livello di Copertura",
    _COVERAGE_LABELS, index=2,
    key="sim_coverage",
    help="Bassa → 50% rimborso | Media → 70% | Alta → 100%",
)
coverage = _COVERAGE_INTERNAL[coverage_label]
fcov     = _COVERAGE_FCOV[coverage_label]

st.sidebar.markdown("---")

with st.sidebar.expander("⚙️ Parametri Protocollo"):
    mbase = st.slider("Margine base Mbase", 0.05, 0.50, 0.15, step=0.01, key="prot_mbase")
    loss_pct = st.slider("L% — perdita media per attacco", 0.05, 0.40, 0.25, step=0.01, key="prot_loss_pct")
    sr_threshold_high = st.slider("Soglia SR ALTA (sano)", 1.3, 2.0, 1.50, step=0.05, key="prot_sr_high")
    sr_threshold_med  = st.slider("Soglia SR MEDIA (rischio)", 1.0, 1.5, 1.30, step=0.05, key="prot_sr_med")
    initial_pool_balance = st.number_input("Saldo iniziale pool (ETH)", min_value=10.0, max_value=10000.0, value=50.0, step=10.0, key="prot_pool_balance")

with st.sidebar.expander("📐 Formula Premio"):
    e_fnr = st.slider("E — False Negative Rate (FNR)", 0.01, 0.99, 0.20, 0.01, key="fp_e_fnr",
                      help="Tasso di falsi negativi (attacchi non rilevati). Usato come E/(1−E) nella formula.")
    st.caption("ℹ️ Vbase e Tint sono calcolati automaticamente ogni giorno simulato.")

with st.sidebar.expander("🚨 Parametri Frodi"):
    fraud_claim_pct = st.slider(
        "Percentuale frodi sui claim (%)", 0, 50, 5, step=1, key="fraud_claim_pct",
        help="Percentuale di claim fraudolenti aggiuntivi rispetto agli attacchi reali. Es: 5% → per 50 attacchi reali, si aggiungono 2-3 claim fraudolenti.",
    ) / 100.0

with st.sidebar.expander("🔬 Parametri Mode 2 — Sintetica"):
    patt_override = st.slider("Patt manuale (tasso attacco)", 0.01, 0.50, 0.10, step=0.01, key="m2_patt_override", disabled=is_mode1, help="Base + rumore ±30% ogni giorno")
    n_synthetic_users = st.number_input("N utenti sintetici (iniziali)", min_value=5, max_value=500, value=50, step=5, key="m2_n_users", disabled=is_mode1)
    max_daily_swaps   = st.number_input("Max swap/giorno per utente", min_value=1, max_value=100, value=10, step=1, key="m2_max_daily_swaps", disabled=is_mode1)

st.sidebar.markdown("---")
if is_mode1:
    st.sidebar.caption("🔑 Chiave Infura nella tab **🔗 Dati Infura**")

run_btn    = st.sidebar.button("▶ Avvia Simulazione", type="primary", key="run_btn")
export_btn = st.sidebar.button("📥 Esporta CSV", key="export_btn")


# =========================================================================
# Config builder
# =========================================================================

def _build_config(
    mode, duration_days, swaps_per_day, coverage,
    mbase, loss_pct, sr_threshold_high, sr_threshold_med,
    initial_pool_balance,
    n_synthetic_users, max_daily_swaps,
    patt_override,
    e_fnr, fraud_claim_pct,
) -> dict:
    cfg_path = os.path.join(
        _ROOT, "config",
        "mode1_realchain.yaml" if mode == 1 else "mode2_synthetic.yaml",
    )
    cfg = load_config(cfg_path)

    run_seed = int(_time_mod.time()) % 99999
    st.sidebar.info(f"🎲 Seed: {run_seed}")

    cfg["simulation"]["duration_days"] = int(duration_days)
    cfg["simulation"]["seed"]          = run_seed
    cfg["pool"]["mbase"]                              = float(mbase)
    cfg["pool"]["initial_balance_eth"]                = float(initial_pool_balance)
    cfg["pool"]["solvency_thresholds"]["high_risk"]   = float(sr_threshold_med)
    cfg["pool"]["solvency_thresholds"]["medium_risk"] = float(sr_threshold_high)
    cfg["market"]["loss_pct_mean"] = float(loss_pct)
    cfg["market"]["e"]             = float(e_fnr)
    cfg["simulation"]["fraud_claim_pct"] = float(fraud_claim_pct)

    if mode == 2:
        cfg["users"]["initial_count"]       = int(n_synthetic_users)
        cfg["users"]["swap_frequency_mean"] = max(1, int(swaps_per_day / max(n_synthetic_users, 1)))
        cfg["users"]["max_daily_swaps"]     = int(max_daily_swaps)
        cfg["market"]["attack_rate"]        = float(patt_override)

    if mode == 1:
        _pv = st.session_state.get("infura_patt_value")
        if _pv is not None and _pv > 0:
            cfg.setdefault("market", {})["attack_rate"] = float(_pv)

    return cfg


def _all_params() -> dict:
    return dict(
        mode=mode, duration_days=duration_days, swaps_per_day=swaps_per_day,
        coverage_label=coverage_label, coverage=coverage,
        mbase=mbase, loss_pct=loss_pct,
        sr_threshold_high=sr_threshold_high, sr_threshold_med=sr_threshold_med,
        initial_pool_balance=initial_pool_balance,
        patt_override=patt_override,
        n_synthetic_users=n_synthetic_users, max_daily_swaps=max_daily_swaps,
        e_fnr=e_fnr, fraud_claim_pct=fraud_claim_pct,
    )


def _make_cfg(**kwargs) -> dict:
    return _build_config(
        mode=kwargs.get("mode", mode),
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
    )


# =========================================================================
# render_riepilogo
# =========================================================================

def render_riepilogo(p: dict) -> str:
    _fcov   = _COVERAGE_FCOV[p["coverage_label"]]
    reimb   = _COVERAGE_REIMB[p["coverage_label"]]
    patt_ex = p["patt_override"] if p["mode"] == 2 else 0.05
    M_total = p["mbase"]
    _e      = float(p["e_fnr"])
    _e_safe = max(min(_e, 0.9999), 0.0001)
    _fcp    = float(p["fraud_claim_pct"])

    mode_str = "Mode 1 — Dati Reali" if p["mode"] == 1 else "Mode 2 — Sintetica"

    if p["mode"] == 1:
        _pv = st.session_state.get("infura_patt_value")
        patt_src = (f"dati reali Infura — **Patt = {_pv:.2%}** (con margine sicurezza)"
                    if _pv and _pv > 0
                    else "dati reali Infura (scarica nella tab Dati Infura)")
    else:
        patt_src = f"override manuale **{p['patt_override']:.2%}** + rumore ±30%/giorno"

    total_sw_est = p["duration_days"] * p["swaps_per_day"] if p["mode"] == 2 else "—"
    exp_atk_est  = int(p["duration_days"] * p["patt_override"] * p["swaps_per_day"]) if p["mode"] == 2 else "—"

    # Fraud example numbers (hypothetical 100 swaps)
    ex_sw   = 100
    ex_atk  = round(ex_sw * patt_ex)
    ex_fr   = round(ex_atk * _fcp)
    ex_tot  = ex_atk + ex_fr
    ex_caught  = round(ex_fr * (1.0 - _e))
    ex_escaped = ex_fr - ex_caught

    # Data source declaration
    if p["mode"] == 1:
        ds_block = (
            "**Fonti dati — Mode 1:**\n"
            "- Swap: hash e pool reali da Infura (`eth_getLogs`)\n"
            "- Valore swap in ETH: stimato sinteticamente (log-normale, media ~0.5 ETH) — "
            "`eth_getLogs` non contiene il valore, solo l'evento\n"
            "- Patt: calcolato da sandwich/swap reali scaricati da Infura\n"
            "- **Vbase**: conteggio swap assicurati reali del giorno D-1 (da Infura)\n"
            "- **Tint**: n_frodi_intercettate_{D-1} × valore_medio_swap_{D-1} [ETH]"
        )
    else:
        ds_block = (
            "**Fonti dati — Mode 2:**\n"
            "- Swap: generati sinteticamente (Poisson con media N/giorno, seed casuale)\n"
            "- Valore swap: log-normale (media ~0.5 ETH, σ=0.4)\n"
            "- Patt: override manuale + oscillazione ±30% ogni giorno\n"
            "- **Vbase**: conteggio swap sintetici assicurati del giorno D-1\n"
            "- **Tint**: n_frodi_intercettate_{D-1} × valore_medio_swap_{D-1} [ETH]"
        )

    return (
        f"## 📋 Riepilogo Simulazione\n\n"
        f"**Modalità:** {mode_str} | **Durata:** {p['duration_days']} giorni | "
        f"**Copertura:** {p['coverage_label']} → rimborso {reimb}, Fcov={_fcov:.2f}\n\n"
        "---\n\n"
        "### Formula Premio\n\n"
        "```\nP = V × [(Patt × L%) + (Tint × E/(1−E)) / (Vbase × 1000)] × (1 + M) × Fcov\n```\n\n"
        f"| Parametro | Valore | Fonte |\n|---|---|---|\n"
        f"| Patt | {patt_ex:.2%} | {patt_src} |\n"
        f"| L% | {p['loss_pct']:.2%} | configurabile |\n"
        f"| E (FNR) | {_e:.3f} → E/(1−E) = {_e_safe/(1-_e_safe):.4f} | configurabile |\n"
        f"| **Vbase** | calcolato automaticamente | swap assicurati giorno D-1 |\n"
        f"| **Tint** | calcolato automaticamente | frodi_caught_{'{D-1}'} × avg_swap_value |\n"
        f"| Mbase | {p['mbase']:.2%} | configurabile |\n"
        f"| M_adj | 0.00/0.05/0.10 in base al SR | dinamico |\n"
        f"| Fcov | {_fcov:.2f} | copertura {p['coverage_label']} |\n\n"
        "---\n\n"
        f"{ds_block}\n\n"
        "---\n\n"
        f"### Logica Frodi (fraud_claim_pct = {_fcp:.0%})\n\n"
        f"Esempio con {ex_sw} swap totali, Patt={patt_ex:.0%}, FNR={_e:.0%}:\n\n"
        f"```\n"
        f"Swap totali:          {ex_sw}\n"
        f"Attacchi reali:       {ex_sw} × {patt_ex:.0%} = {ex_atk}\n"
        f"Frodi aggiuntive:     {ex_atk} × {_fcp:.0%} = {ex_fr}   ← fraud_claim_pct applicato\n"
        f"Claim totali:         {ex_tot}\n"
        f"Frodi intercettate:   {ex_fr} × {1-_e:.0%} = {ex_caught}  ← (1 - FNR)\n"
        f"Frodi scappate:       {ex_fr} × {_e:.0%} = {ex_escaped}   ← FNR\n"
        f"Payout reali:         {ex_atk}\n"
        f"Payout fraudolenti:   {ex_escaped}\n"
        f"Tint (giorno succ.):  {ex_caught} × avg_swap_value ETH\n"
        f"```\n\n"
        "---\n\n"
        f"- **Rottura pool:** solo se `balance_eth < 0` (SR = solo modulatore margine)\n\n"
        f"~**{total_sw_est}** swap | ~**{exp_atk_est}** attacchi attesi\n"
    )


# =========================================================================
# Avvio simulazione
# =========================================================================

if run_btn:
    errors = []
    if is_mode1 and _db_swap_count() == 0:
        errors.append("Il database Infura è vuoto. Scarica i dati prima di usare Mode 1.")
    if errors:
        for e in errors:
            st.error(f"⚠️ {e}")
    else:
        cfg = _make_cfg()
        with st.spinner("Simulazione in corso…"):
            collector, pool, summary = run_single(cfg, mode=mode, coverage=coverage, db_path=_DB_PATH)
            label = coverage_label
            st.session_state["results"]    = {label: collector.to_dataframe()}
            st.session_state["summaries"]  = {label: summary}
            st.session_state["collectors"] = {label: collector}
            st.session_state["last_mode"]  = mode
        st.success("✅ Simulazione completata!")

if export_btn and st.session_state["results"]:
    frames   = [df.assign(run=lbl) for lbl, df in st.session_state["results"].items()]
    csv_data = pd.concat(frames, ignore_index=True).to_csv(index=False).encode("utf-8")
    st.sidebar.download_button("⬇ Scarica CSV", data=csv_data,
        file_name="mev_risultati_simulazione.csv", mime="text/csv", key="csv_download")


# =========================================================================
# Area principale — 3 tab
# =========================================================================
st.title("🛡️ MEV Insurance Protocol Simulator")

tab_sim, tab_batch, tab_infura, tab_istr = st.tabs(
    ["📊 Simulazione", "🔁 Batch Simulazioni", "🔗 Dati Infura", "📖 Istruzioni"]
)

# ==========================================================================
# TAB 1 — SIMULAZIONE
# ==========================================================================
with tab_sim:
    st.markdown(render_riepilogo(_all_params()))

    results    = st.session_state["results"]
    summaries  = st.session_state["summaries"]
    collectors = st.session_state["collectors"]

    if not results:
        if is_mode1 and _db_swap_count() == 0:
            st.warning("**Mode 1:** scarica prima i dati nella tab 🔗 Dati Infura.")
        else:
            st.info("Configura i parametri nella sidebar e premi **▶ Avvia Simulazione**.")
    else:
        first_label     = next(iter(results))
        first_df        = results[first_label]
        first_summary   = summaries[first_label]
        first_collector = collectors.get(first_label)
        last_mode       = st.session_state.get("last_mode", 2)

        st.markdown("---")

        # ---- Metriche chiave ----
        st.subheader("📈 Risultati")
        c0, c1, c2, c3 = st.columns(4)
        c0.metric("Profitto totale (ETH)", f"{first_summary['total_profit_eth']:.4f}")
        c1.metric("SR finale",             f"{first_summary['final_solvency_ratio']:.3f}")
        c2.metric("Saldo finale (ETH)",    f"{first_summary['final_balance_eth']:.4f}")
        c3.metric("Pool sopravvissuto",    "SÌ ✓" if first_summary["pool_survived"] else "NO ✗")

        _bd = first_summary.get("breakdown_event")
        if _bd:
            st.error(f"💥 **Pool esaurito al giorno {_bd['day']}** — {_bd['reason']}\n\nSaldo: {_bd['pool_balance']:.4f} ETH")
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
        pool_df.columns = ["Giorno", "Saldo Pool (ETH)", "Passività Pendenti (ETH)",
                           "Solvency Ratio", "M_adj", "Variazione Netta (ETH)"]

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
            .format({"Saldo Pool (ETH)": "{:.4f}", "Passività Pendenti (ETH)": "{:.4f}",
                     "Solvency Ratio": "{:.4f}", "M_adj": "{:.2f}", "Variazione Netta (ETH)": "{:+.4f}"})
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
        ca.metric("Premi Totali (ETH)",  f"{float(last['total_premiums_collected_eth']):.4f}")
        cb.metric("Payout Totali (ETH)", f"{float(last['total_payouts_eth']):.4f}")
        cc.metric("Profitto Netto (ETH)",f"{float(last['profit_eth']):.4f}")

        cf_df = first_df[["day","premiums_today","payouts_today","net_flow_today"]].copy()
        cf_df.columns = ["Giorno","Premi Oggi (ETH)","Payout Oggi (ETH)","Flusso Netto (ETH)"]
        st.dataframe(cf_df.style.format({c: "{:.4f}" for c in cf_df.columns if c != "Giorno"}),
                     use_container_width=True, height=250)

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

        cl_df = first_df[["day","n_claims_submitted","n_claims_approved","avg_payout_eth"]].copy()
        cl_df.columns = ["Giorno","Claim Inviati","Claim Approvati","Payout Medio (ETH)"]
        st.dataframe(cl_df.style.format({"Payout Medio (ETH)": "{:.4f}"}),
                     use_container_width=True, height=250)

        # ---- Pannello 4: Utenti (Mode 2) ----
        if last_mode == 2:
            st.markdown("---")
            st.subheader("4 — Distribuzione Utenti")
            fonte("Utenti sintetici da SyntheticDataSource — solo Mode 2")

            _ud_cols = [c for c in ["day","n_users_active"] if c in first_df.columns]
            ud_df = first_df[_ud_cols].copy()
            ud_df.columns = ["Giorno","Utenti Attivi"][:len(_ud_cols)]
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
            "Voce": ["Premi incassati","Payout erogati","Profitto netto","Saldo finale"],
            "Importo (ETH)": [premi_tot, payout_tot, profit_tot, saldo_fin],
        })
        eco_df["% dei premi"] = eco_df["Importo (ETH)"].apply(lambda x: f"{abs(x)/ref*100:.1f}%")
        eco_df["Importo (ETH)"] = eco_df["Importo (ETH)"].apply(lambda x: f"{x:.4f}")
        st.dataframe(eco_df, use_container_width=True, hide_index=True)
        fonte("InsurancePool — cumulativo al termine della simulazione")

        base_risk   = (patt_override if is_mode2 else 0.05) * loss_pct
        prem_ex2    = base_risk * (1 + mbase) * fcov
        atk_frac    = base_risk * fcov / prem_ex2 * 100 if prem_ex2 > 0 else 0
        marg_frac   = base_risk * mbase * fcov / prem_ex2 * 100 if prem_ex2 > 0 else 0
        st.markdown(
            f"**Su ogni ETH di premio:** ~{atk_frac:.1f}% copre rischio attacchi | "
            f"~{marg_frac:.1f}% è margine\n"
        )

        st.markdown("---")

        # ---- Esploratore Giornaliero ----
        st.subheader("5 — Esploratore Giornaliero")

        _bd6_day = _bd["day"] if _bd else None
        max_day  = int(first_df["day"].max())
        selected_day = st.slider("Seleziona giorno", min_value=0, max_value=max_day,
                                  value=_bd6_day if _bd6_day else 0, step=1, key="day_explorer_slider")

        if _bd6_day is not None and selected_day == _bd6_day:
            st.error(f"💥 **GIORNO DI ROTTURA** — {_bd['reason']} | Saldo: {_bd['pool_balance']:.4f} ETH")
        elif _bd6_day is not None and selected_day == _bd6_day - 1:
            st.warning("⚠️ Giorno precedente alla rottura.")

        row = first_df[first_df["day"] == selected_day]
        if not row.empty:
            row = row.iloc[0]
            # ---- Full breakdown table ----
            n_sw      = int(row.get("n_swaps_this_tick", 0))
            n_ra      = int(row.get("n_real_attacks", 0))
            n_fc      = int(row.get("n_fraud_caught", 0))
            n_fe      = int(row.get("n_fraud_escaped", 0))
            n_normal  = max(n_sw - n_ra - n_fc - n_fe, 0)
            net       = float(row.get("net_flow_today", 0.0))
            pr_today  = float(row.get("payout_real_today", 0.0))
            pf_today  = float(row.get("payout_fraud_today", 0.0))
            tint_d    = float(row.get("tint_today", 0.0))
            vbase_d   = float(row.get("vbase_today", 100.0))
            st.dataframe(pd.DataFrame({
                "Metrica": [
                    "Swap totali",
                    "  — normali",
                    "  — attacchi reali",
                    "  — frodi intercettate",
                    "  — frodi scappate",
                    "Premi incassati (tutti)",
                    "Payout erogati",
                    "    da attacchi reali",
                    "    da frodi scappate",
                    "Tint (frodi intercettate × avg_swap)",
                    "Vbase (swap ieri)",
                    "Variazione netta pool",
                    "Saldo pool",
                    "Solvency Ratio (solo per M_adj)",
                    "M_adj applicato oggi",
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
                    f"{tint_d:.4f} ETH",
                    f"{vbase_d:.0f} swap",
                    f"{'+' if net>=0 else ''}{net:.4f} ETH",
                    f"{float(row['pool_balance_eth']):.4f} ETH",
                    f"{float(row['solvency_ratio']):.4f}",
                    f"{float(row.get('madj_current',0)):.4f}",
                ]
            }), use_container_width=True, hide_index=True)

            # ---- Formula breakdown for selected day ----
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
                _term2   = (_tint_d * _e_ratio) / (max(_vbase_d, 1.0) * 1000.0)
                _sum_t   = _term1 + _term2
                _fcov_d  = fcov
                _ex_prem = 1.0 * _sum_t * (1.0 + _mtot_d) * _fcov_d
                _n_fc_d  = int(row.get("n_fraud_caught", 0))
                _avg_sv  = float(row.get("avg_swap_value_eth", 0.0))
                _n_fe_d  = int(row.get("n_fraud_escaped", 0))

                _mode_lbl = last_mode
                _vbase_src = (
                    f"swap assicurati ieri — Mode 1: da Infura"
                    if _mode_lbl == 1 else
                    f"swap sintetici assicurati ieri"
                )
                _tint_src = f"{_n_fc_d} frodi intercettate × {_avg_sv:.4f} ETH/swap"

                st.code(
                    f"Vbase = {_vbase_d:.0f} swap  ({_vbase_src})\n"
                    f"Tint  = {_tint_d:.4f} ETH  ({_tint_src})\n"
                    f"\n"
                    f"Patt          = {_patt_d:.5f}\n"
                    f"L%            = {loss_pct:.3f}\n"
                    f"Term1         = Patt × L%                           = {_term1:.6f}\n"
                    f"\n"
                    f"Tint (ETH)    = {_tint_d:.4f}\n"
                    f"E (FNR)       = {_e_d:.3f}  →  E/(1−E)             = {_e_ratio:.4f}\n"
                    f"Vbase         = {_vbase_d:.0f} swap\n"
                    f"Term2         = (Tint × E/(1−E)) / (Vbase × 1000)   = {_term2:.6f}\n"
                    f"\n"
                    f"──────────────────────────────────────────────────────────────\n"
                    f"Sum           = Term1 + Term2                        = {_sum_t:.6f}\n"
                    f"Mbase         = {mbase:.3f}\n"
                    f"M_adj         = {_madj_d:.4f}\n"
                    f"M_tot         = Mbase + M_adj                        = {_mtot_d:.4f}\n"
                    f"Fcov          = {_fcov_d:.2f}  ({coverage_label})\n"
                    f"──────────────────────────────────────────────────────────────\n"
                    f"Esempio 1 ETH: P = 1 × {_sum_t:.6f} × (1 + {_mtot_d:.4f}) × {_fcov_d:.2f}\n"
                    f"             = {_ex_prem:.6f} ETH\n"
                    f"\n"
                    f"Frodi oggi:   {int(row.get('n_fraud_attempts',0))} tentativi "
                    f"| {_n_fc_d} intercettate | {_n_fe_d} scappate",
                    language=None,
                )

            if first_collector:
                day_swaps = first_collector.daily_swap_details.get(selected_day, [])
                _n_real   = sum(1 for d in day_swaps if d.get("tipo_claim") == "reale")
                _n_fi     = sum(1 for d in day_swaps if d.get("tipo_claim") == "frode_intercettata")
                _n_fs     = sum(1 for d in day_swaps if d.get("tipo_claim") == "frode_scappata")
                with st.expander(
                    f"Swap del giorno {selected_day} "
                    f"({len(day_swaps)} righe: {_n_real} reali, {_n_fi} frodi_catch, {_n_fs} frodi_esc)",
                    expanded=False,
                ):
                    if day_swaps:
                        sw_df = pd.DataFrame(day_swaps)
                        # Rename columns for display
                        col_rename = {
                            "swap_id":      "Swap ID",
                            "value_ETH":    "Valore (ETH)",
                            "was_attacked": "Attaccato",
                            "coverage_level": "Copertura",
                            "premium_paid": "Premio (ETH)",
                            "premium_pct":  "Premio (%)",
                            "claim_submitted": "Claim?",
                            "claim_approved":  "Approvato?",
                            "payout_ETH":   "Rimborso (ETH)",
                            "rimborso_pct": "Rimborso (%)",
                            "tipo_claim":   "Tipo Claim",
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
                d, bal = int(r["day"]), float(r.get("pool_balance_eth",0))
                if _bd6_day and d == _bd6_day: return "💥 ROTTURA"
                elif _bd6_day and d > _bd6_day: return "⚠️ post-rottura"
                elif bal < 0: return "🔴 saldo negativo"
                elif float(r.get("solvency_ratio",1.5)) < 1.3: return "🟠 rischio medio"
                else: return "🟢 sano"
            _brd.insert(1, "Stato", _brd.apply(_st_full, axis=1))
            st.dataframe(_brd, use_container_width=True)

        st.markdown("---")
        st.subheader("📁 Tutti i Dati")

        with st.expander("📋 Parametri usati", expanded=False):
            _p = _all_params()
            rows = [
                ("Modalità", "Mode 1" if _p["mode"]==1 else "Mode 2"),
                ("Durata (giorni)", _p["duration_days"]),
                ("Copertura", f"{_p['coverage_label']} (Fcov={_COVERAGE_FCOV[_p['coverage_label']]:.2f})"),
                ("Mbase", f"{_p['mbase']:.2%}"), ("L%", f"{_p['loss_pct']:.2%}"),
                ("Soglia SR Alta", _p["sr_threshold_high"]), ("Soglia SR Media", _p["sr_threshold_med"]),
                ("Pool iniziale (ETH)", _p["initial_pool_balance"]),
                ("E (FNR)", f"{_p['e_fnr']:.3f}"),
                ("Frodi/claim (%)", f"{_p['fraud_claim_pct']:.1%}"),
                ("Vbase", "auto — swap assicurati D-1"),
                ("Tint", "auto — frodi_caught_{D-1} × avg_swap_value"),
            ]
            if _p["mode"] == 2:
                rows += [
                    ("Patt manuale", f"{_p['patt_override']:.2%}"),
                    ("Swap/giorno", _p["swaps_per_day"]),
                    ("N utenti", _p["n_synthetic_users"]),
                    ("Max swap/utente/giorno", _p["max_daily_swaps"]),
                    ("Seed", "auto (time-based)"),
                ]
            if _p["mode"] == 1:
                pv = st.session_state.get("infura_patt_value")
                if pv: rows.append(("Patt da Infura", f"{pv:.2%}"))
            st.dataframe(pd.DataFrame(rows, columns=["Parametro","Valore"]),
                         use_container_width=True, hide_index=True)

        with st.expander("⬇️ Scarica CSV completo", expanded=False):
            _csv = first_df.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Scarica CSV", data=_csv,
                               file_name="mev_simulazione.csv", mime="text/csv", key="csv_full_download")
            st.caption(f"{len(first_df)} righe × {len(first_df.columns)} colonne")

        with st.expander("📐 Formula per giorno", expanded=False):
            _fcov_v = _COVERAGE_FCOV[coverage_label]
            rows_f  = []
            _mode_l = last_mode
            for _, fr in first_df.iterrows():
                patt_d   = float(fr.get("patt_current", 0.05))
                madj_d   = float(fr.get("madj_current", 0.0))
                m_tot    = mbase + madj_d
                tint_d   = float(fr.get("tint_today", 0.0))
                vbase_d  = float(fr.get("vbase_today", 100.0))
                e_d      = float(fr.get("e_today", e_fnr))
                e_safe   = max(min(e_d, 0.9999), 0.0001)
                t1       = patt_d * loss_pct
                t2       = (tint_d * (e_safe / (1.0 - e_safe))) / (max(vbase_d, 1.0) * 1000.0)
                prem_ex  = (t1 + t2) * (1 + m_tot) * _fcov_v
                n_fc     = int(fr.get("n_fraud_caught", 0))
                n_fe     = int(fr.get("n_fraud_escaped", 0))
                avg_sv   = float(fr.get("avg_swap_value_eth", 0.0))
                _vb_src  = "Infura D-1" if _mode_l == 1 else "sintetico D-1"
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
            fonte("P = V × [(Patt × L%) + (Tint × E/(1−E)) / (Vbase × 1000)] × (1+M) × Fcov")


# ==========================================================================
# ==========================================================================
# TAB 2 — BATCH SIMULAZIONI
# ==========================================================================
with tab_batch:
    st.subheader("🔁 Batch Simulazioni")
    st.markdown("Esegui N simulazioni variando i parametri. Analizza la distribuzione dei risultati.")

    b1, b2 = st.columns([1, 2])
    with b1:
        _bn_sims = st.number_input("N simulazioni", min_value=2, max_value=400, value=10, step=1, key="batch_n")
        _b_mode  = st.radio("Modalità", [2, 1], format_func=lambda m: "Mode 2 — Sintetica" if m==2 else "Mode 1 — Reale", key="batch_mode")
    with b2:
        _b_params = st.multiselect(
            "Parametri variabili",
            ["Patt", "L%", "E (FNR)", "Frodi%", "Pool iniziale"],
            default=["Patt", "E (FNR)"],
            key="batch_vars",
        )

    # Per-parameter range config
    _b_ranges: dict = {}
    if _b_params:
        st.markdown("**Range per parametro variabile:**")
        _pc_cols = st.columns(min(len(_b_params), 3))
        for _bi, _bp in enumerate(_b_params):
            with _pc_cols[_bi % len(_pc_cols)]:
                st.markdown(f"*{_bp}*")
                _bmode_p = st.selectbox("Modalità", ["Range lineare", "Casuale nel range", "Fisso"],
                                        key=f"batch_pmode_{_bi}")
                if _bmode_p == "Fisso":
                    _bfv = st.number_input("Valore fisso", value=0.10, key=f"batch_fixed_{_bi}")
                    _b_ranges[_bp] = ("fixed", _bfv, _bfv)
                else:
                    _bmin = st.number_input("Min", value=0.02, key=f"batch_min_{_bi}")
                    _bmax = st.number_input("Max", value=0.20, key=f"batch_max_{_bi}")
                    _b_ranges[_bp] = ("linear" if _bmode_p=="Range lineare" else "random", _bmin, _bmax)

    _batch_run_btn = st.button("▶ Avvia Batch", type="primary", key="batch_run_btn")

    if _batch_run_btn:
        _b_results = []
        _b_prog    = st.progress(0, text="Avvio batch…")
        _b_cfg_base = _make_cfg(mode=_b_mode)

        for _bi in range(int(_bn_sims)):
            # Generate params for this run
            _run_p: dict = {}
            for _bp, (_bmt, _bmn, _bmx) in _b_ranges.items():
                if _bmt == "fixed":
                    _rv = _bmn
                elif _bmt == "linear":
                    _rv = _bmn + (_bmx - _bmn) * _bi / max(int(_bn_sims)-1, 1)
                else:
                    import random as _rand
                    _rv = _rand.uniform(_bmn, _bmx)
                _run_p[_bp] = round(float(_rv), 6)

            import copy as _copy
            _rc = _copy.deepcopy(_b_cfg_base)
            _rc["simulation"]["seed"] = (int(_time_mod.time() * 1000) + _bi) % 99999
            if "Patt"        in _run_p: _rc["market"]["attack_rate"]       = _run_p["Patt"]
            if "L%"          in _run_p: _rc["market"]["loss_pct_mean"]     = _run_p["L%"]
            if "E (FNR)"     in _run_p: _rc["market"]["e"]                 = _run_p["E (FNR)"]
            if "Frodi%"      in _run_p: _rc["simulation"]["fraud_claim_pct"]= _run_p["Frodi%"]
            if "Pool iniziale" in _run_p: _rc["pool"]["initial_balance_eth"]= _run_p["Pool iniziale"]

            try:
                _c, _p, _s = run_single(_rc, mode=_b_mode, coverage=coverage, db_path=_DB_PATH)
                _df_run = _c.to_dataframe()
                _b_results.append({
                    "run": _bi + 1,
                    **_run_p,
                    "pool_survived":    _s["pool_survived"],
                    "profitto_eth":     round(_s["total_profit_eth"], 4),
                    "sr_finale":        round(_s["final_solvency_ratio"], 4),
                    "giorno_rottura":   (_s.get("breakdown_event") or {}).get("day"),
                    "trend_slope":      round(_s.get("trend_slope", 0.0), 4),
                    "_df":              _df_run,
                })
            except Exception as _bex:
                _b_results.append({
                    "run": _bi + 1, **_run_p,
                    "pool_survived": False, "profitto_eth": 0.0,
                    "sr_finale": 0.0, "giorno_rottura": 0,
                    "trend_slope": 0.0, "_df": None,
                    "_error": str(_bex),
                })
            _b_prog.progress((_bi + 1) / int(_bn_sims), text=f"Run {_bi+1}/{_bn_sims}…")

        st.session_state["batch_results"] = _b_results
        _b_prog.progress(100, text="✅ Batch completato!")
        st.rerun()

    # --- Show results ---
    _br = st.session_state.get("batch_results")
    if _br:
        _br_clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in _br]
        _br_df    = pd.DataFrame(_br_clean)

        st.markdown("---")
        st.subheader("A — Riepilogo tutte le run")

        def _color_row(row):
            if not row.get("pool_survived", True):
                return ["background-color: #ffcccc"] * len(row)
            elif row.get("trend_slope", 0) > 0:
                return ["background-color: #ccffcc"] * len(row)
            return [""] * len(row)

        st.dataframe(
            _br_df.style.apply(_color_row, axis=1),
            use_container_width=True, hide_index=True, height=300,
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
        _slopes = [r.get("trend_slope", 0) for r in _br_clean if r.get("trend_slope") is not None]
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


# ==========================================================================
# TAB 3 — DATI INFURA
# ==========================================================================
with tab_infura:
    st.subheader("🔗 Dati Infura — Gestione e Download")
    st.markdown("### 🔑 Chiave API Infura")

    _current_key = st.session_state.get("infura_api_key", "")
    _kc, _bc = st.columns([4,1])
    with _kc:
        _key_input = st.text_input("Project ID Infura", value=_current_key,
                                    key="infura_key_field", placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    with _bc:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("💾 Salva", key="save_infura_key_btn"):
            _nk = _key_input.strip()
            if _nk:
                import re as _re
                _by = os.path.join(_ROOT, "config", "base.yaml")
                try:
                    with open(_by) as _f: _cnt = _f.read()
                    _cnt = _re.sub(r'(infura_url:\s*")[^"]*(")', rf'\g<1>wss://mainnet.infura.io/ws/v3/{_nk}\2', _cnt)
                    with open(_by, "w") as _f: _f.write(_cnt)
                    st.session_state["infura_api_key"] = _nk
                    st.success("✅ Chiave salvata")
                except Exception as ex: st.error(f"⚠️ {ex}")
            else:
                st.warning("Chiave non valida.")

    _key_ok = len(st.session_state.get("infura_api_key","")) > 8
    if _key_ok:
        _k = st.session_state["infura_api_key"]
        st.success(f"🟢 Chiave: `{_k[:6]}…{_k[-4:]}`")
    else:
        st.error("🔴 Chiave non configurata.")

    st.info("ℹ️ **Mode 1:** Patt calcolato da dati Infura reali con margine sicurezza ms. "
            "Tutti gli altri parametri (loss, Tint, Vbase, E) sono configurati manualmente.")

    st.markdown("---")
    st.markdown("### ⚙️ Parametri Download")
    _d1, _d2 = st.columns(2)
    with _d1:
        _block_range_days = st.number_input("Intervallo (giorni)", min_value=1, max_value=7, value=2, key="infura_block_range")
    with _d2:
        _dex_targets = st.multiselect("DEX da monitorare", _DEX_OPTIONS, default=["Uniswap V2","Uniswap V3"], key="infura_dex_targets")

    if _dex_targets:
        from scripts.download_blocks import CHUNK_SIZE as _CS, BLOCKS_PER_DAY as _BPD, _DEX_TOPIC_MAP as _DTM
        _nc2 = (_block_range_days * _BPD + _CS - 1) // _CS
        _ut  = len({_DTM[d] for d in _dex_targets if d in _DTM})
        st.info(f"**Stima chiamate Infura:** {_nc2*_ut+1} (CHUNK_SIZE={_CS})")
    else:
        st.warning("Seleziona almeno un DEX.")

    st.markdown("---")
    st.markdown("### Azioni")
    _a1, _a2, _a3 = st.columns(3)

    with _a1:
        if st.button("🔄 Scarica dati ora", key="infura_download_btn", disabled=not(_key_ok and _dex_targets)):
            _iu = f"wss://mainnet.infura.io/ws/v3/{st.session_state['infura_api_key']}"
            try:
                from scripts.download_blocks import fetch_dex_events as _fe, save_to_db as _sd
                _pb = st.progress(0, text="Avvio…")
                def _cb(p,m): _pb.progress(int(min(p,100)), text=m)
                _res = _fe(infura_url=_iu, days=int(_block_range_days), dex_targets=_dex_targets,
                           cache_dir=os.path.join(_ROOT,"cache"), progress_cb=_cb, force_refresh=True)
                _sd(_res, _DB_PATH)
                _pb.progress(100, text="Completato!")
                _m   = _res["metadata"]
                _pv2 = _m.get("patt_value", 0.0)
                st.session_state["infura_patt_value"] = _pv2
                # Prepare lightweight example for "Come funziona" section
                _sw_hashes = set()
                for _sa in _res["sandwich_attacks"]:
                    _sw_hashes.update([_sa["frontrun_tx"], _sa["victim_tx"], _sa["backrun_tx"]])
                _ex_non_sw = next((s for s in _res["swaps"] if s["tx_hash"] not in _sw_hashes), None)
                st.session_state["infura_last_result"] = {
                    "metadata": _m,
                    "example_sandwich": _res["sandwich_attacks"][0] if _res["sandwich_attacks"] else None,
                    "example_non_sandwich": _ex_non_sw,
                }
                st.success(f"✅ {_m['total_swaps']:,} swap | {_m['total_sandwiches']:,} sandwich | Patt={_pv2:.2%} | {_m['infura_calls_used']} chiamate")
                st.rerun()
            except ImportError: st.error("⚠️ `web3` non installato. `pip install web3`")
            except Exception as ex: st.error(f"⚠️ {ex}")

    with _a2:
        if st.button("🗑️ Cancella cache", key="infura_clear_btn"):
            st.session_state["confirm_clear_cache"] = True
        if st.session_state.get("confirm_clear_cache", False):
            st.warning("Eliminare i file .pkl dalla cache?")
            _cc1, _cc2 = st.columns(2)
            with _cc1:
                if st.button("✅ Sì", key="confirm_clear_yes"):
                    _del = 0
                    for _cf in _glob.glob(os.path.join(_CACHE_DIR,"*.pkl")):
                        os.remove(_cf); _del += 1
                    st.session_state["confirm_clear_cache"] = False
                    st.success(f"🗑️ Eliminati {_del} file."); st.rerun()
            with _cc2:
                if st.button("❌ No", key="confirm_clear_no"):
                    st.session_state["confirm_clear_cache"] = False

    with _a3:
        _dbe = os.path.isfile(_DB_PATH)
        _prev_btn = st.button("👁️ Anteprima", key="infura_preview_btn", disabled=not _dbe)

    if _prev_btn and _dbe:
        try:
            _pc = sqlite3.connect(_DB_PATH, check_same_thread=False)
            _pd2 = pd.read_sql("SELECT * FROM swaps ORDER BY timestamp DESC LIMIT 50", _pc)
            _pc.close()
            st.dataframe(_pd2, use_container_width=True)
        except Exception as ex: st.error(f"⚠️ {ex}")

    st.markdown("---")
    st.markdown("### 📊 Stato Database")

    _dbe2 = os.path.isfile(_DB_PATH)
    _cfa  = _glob.glob(os.path.join(_CACHE_DIR,"*.pkl"))
    _lfs  = "mai"
    if _dbe2: _lfs = datetime.datetime.fromtimestamp(os.path.getmtime(_DB_PATH)).strftime("%d/%m/%Y %H:%M")
    _ci = f"✓ {len(_cfa)} file" if _cfa else "✗ assente"

    if _dbe2:
        try:
            _sc = sqlite3.connect(_DB_PATH, check_same_thread=False)
            from scripts.download_blocks import _DDL as _DB_DDL
            _sc.executescript(_DB_DDL); _sc.commit()
            _tsw = _sc.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
            _mb  = _sc.execute("SELECT MIN(block_number) FROM swaps").fetchone()[0] or 0
            _xb  = _sc.execute("SELECT MAX(block_number) FROM swaps").fetchone()[0] or 0
            _mt  = _sc.execute("SELECT MIN(timestamp) FROM swaps").fetchone()[0] or 0
            _xt  = _sc.execute("SELECT MAX(timestamp) FROM swaps").fetchone()[0] or 0
            _dc  = dict(_sc.execute("SELECT dex,COUNT(*) FROM swaps GROUP BY dex").fetchall())
            _na  = _sc.execute("SELECT COUNT(*) FROM sandwich_attacks").fetchone()[0]
            _sc.close()
            def _fmt(t):
                try: return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%d/%m/%Y")
                except: return "—"
            _pp = _na / max(_tsw,1) * 100
            _ps = st.session_state.get("infura_patt_value")
            _pss = f"{_ps:.2%}" if _ps else "—"
            st.markdown(
                f"| Campo | Valore |\n|---|---|\n"
                f"| Aggiornamento DB | {_lfs} |\n| Cache | {_ci} |\n"
                f"| Blocchi | #{_mb:,}→#{_xb:,} |\n| Periodo | {_fmt(_mt)}—{_fmt(_xt)} |\n"
                f"| **Swap totali** | **{_tsw:,}** |\n"
                f"| — Uniswap V2 | {_dc.get('uniswap_v2',0):,} |\n"
                f"| — Uniswap V3 | {_dc.get('uniswap_v3',0):,} |\n"
                f"| — Sushiswap | {_dc.get('sushiswap',0):,} |\n"
                f"| — Curve | {_dc.get('curve',0):,} |\n"
                f"| **Sandwich rilevati** | **{_na:,}** ({_pp:.2f}% raw) |\n"
                f"| **Patt con ms** | **{_pss}** |\n"
            )
            st.info("ℹ️ Patt = raw_ratio × (1 + ms): ms=0.05 se ≥10k swap, 0.10 se ≥1k, 0.20 altrimenti.")
        except Exception as ex: st.warning(f"Statistiche non disponibili: {ex}")
    else:
        st.info("Nessun database locale. Configura la chiave Infura e scarica i dati.")

    st.markdown("---")
    st.markdown("### 📅 Dettaglio Blocchi Scaricati")

    if _dbe2:
        try:
            _bc2 = sqlite3.connect(_DB_PATH, check_same_thread=False)
            _block_rows = _bc2.execute(
                "SELECT block_number, timestamp, COUNT(*) as n_swaps "
                "FROM swaps GROUP BY block_number ORDER BY block_number"
            ).fetchall()
            # sandwich count per block
            _sw_per_blk = dict(_bc2.execute(
                "SELECT block_number, COUNT(*) FROM sandwich_attacks GROUP BY block_number"
            ).fetchall())
            _bc2.close()
            if _block_rows:
                import datetime as _dt
                _br_data = []
                for bn, ts, ns in _block_rows:
                    try:
                        _dt_obj = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
                        _date_s = _dt_obj.strftime("%d/%m/%Y")
                        _time_s = _dt_obj.strftime("%H:%M:%S")
                    except Exception:
                        _date_s, _time_s = "—", "—"
                    _n_sw_blk = _sw_per_blk.get(bn, 0)
                    _pct_sw   = round(_n_sw_blk / ns * 100, 2) if ns > 0 else 0.0
                    _br_data.append({
                        "Blocco #":             bn,
                        "Data (UTC)":           _date_s,
                        "Ora (UTC)":            _time_s,
                        "Swap totali nel pool": ns,
                        "Di cui sandwich":      _n_sw_blk,
                        "% sandwich":           _pct_sw,
                    })
                _br_df = pd.DataFrame(_br_data)
                st.dataframe(_br_df, use_container_width=True, hide_index=True, height=250)
                st.caption(
                    f"Periodo: blocco #{_br_data[0]['Blocco #']:,} "
                    f"({_br_data[0]['Data (UTC)']} {_br_data[0]['Ora (UTC)']}) "
                    f"→ #{_br_data[-1]['Blocco #']:,} "
                    f"({_br_data[-1]['Data (UTC)']} {_br_data[-1]['Ora (UTC)']})"
                    f" | {len(_br_data)} blocchi con swap"
                )
                st.caption(
                    "**Swap totali nel pool** = tutti gli eventi Swap rilevati da `eth_getLogs` "
                    "per quel pool in quel blocco. "
                    "**Di cui sandwich** = sandwich attack individuati con frontrun in quel blocco."
                )
            else:
                st.info("Nessun dato di blocco disponibile.")
        except Exception as _bex:
            st.warning(f"Blocchi non disponibili: {_bex}")
    else:
        st.info("Scarica i dati per vedere il dettaglio dei blocchi.")

    st.markdown("---")
    st.markdown("### 🔍 Come Funziona il Rilevamento Sandwich")

    _ilr = st.session_state.get("infura_last_result")
    if _ilr:
        _meta  = _ilr["metadata"]
        _esw   = _ilr.get("example_sandwich")
        _ensw  = _ilr.get("example_non_sandwich")
        _ts    = _meta.get("total_swaps", 0)
        _ta    = _meta.get("total_sandwiches", 0)
        _raw   = _meta.get("raw_ratio", _ta / max(_ts, 1))
        _ms    = _meta.get("ms_applied", 0.10)
        _patt  = _meta.get("patt_value", _raw * (1 + _ms))
        _vol_s = "≥10.000" if _ts >= 10000 else "1.000–9.999" if _ts >= 1000 else "<1.000"
        from scripts.download_blocks import CHUNK_SIZE as _CS
        st.markdown(
            f"**Fetch:** `eth_getLogs` scarica tutti gli eventi Swap in chunk da {_CS} blocchi. "
            f"Nessun fetch blocco per blocco.\n\n"
            f"**Rilevamento:** per ogni tripla consecutiva nello stesso pool:\n"
            f"1. Stesso `address` (pool)\n"
            f"2. Blocchi distanti al massimo 1\n"
            f"3. Tutti e tre gli hash diversi"
        )
        if _esw:
            st.markdown("**✅ Esempio SANDWICH dall'ultimo fetch:**")
            st.code(
                f"frontrun:  {_esw['frontrun_tx'][:20]}…  blocco #{_esw.get('frontrun_block', _esw.get('block','?')):,}\n"
                f"victim:    {_esw['victim_tx'][:20]}…  blocco #{_esw.get('victim_block','?'):,}\n"
                f"backrun:   {_esw['backrun_tx'][:20]}…  blocco #{_esw.get('backrun_block','?'):,}\n"
                f"pool:      {_esw['pool']}\n"
                f"→ Stesso pool ✓ | Blocchi consecutivi ✓ | Hash diversi ✓",
                language=None,
            )
        if _ensw:
            st.markdown("**❌ Esempio swap NON sandwich:**")
            st.code(
                f"tx:      {_ensw['tx_hash'][:20]}…  blocco #{_ensw['block_number']:,}\n"
                f"→ Nessuna tripla valida attorno a questo swap",
                language=None,
            )
        st.markdown(
            f"**Calcolo Patt:**\n"
            f"```\n"
            f"Sandwich:    {_ta:,}\n"
            f"Swap totali: {_ts:,}\n"
            f"Ratio grezzo: {_ta}/{_ts} = {_raw:.4f}\n"
            f"ms = {_ms}  (volume {_vol_s})\n"
            f"Patt = {_raw:.4f} × (1 + {_ms}) = {_patt:.4f}  ({_patt*100:.2f}%)\n"
            f"```"
        )
    else:
        st.info("Scarica i dati Infura per vedere esempi di rilevamento sandwich in tempo reale.")
        st.markdown("""
| | Vecchio | Nuovo |
|---|---|---|
| Strategia | 1 call/blocco | 1 call per 500 blocchi |
| Chiamate per 2 giorni | ~13.300 | ~27 |
| Tempo | ~94 ore | < 60 s |

Solo dati `eth_getLogs`, nessuna chiamata `eth_getTransactionByHash`.
""")


# ==========================================================================
# TAB 3 — ISTRUZIONI
# ==========================================================================
with tab_istr:
    st.subheader("📖 Guida al Simulatore MEV Insurance")

    st.markdown("""
### Come Usare il Simulatore

1. **Scegli la modalità** (Mode 1: dati reali / Mode 2: sintetica)
2. **Configura** negli expander della sidebar
3. **Leggi il Riepilogo** nella tab *Simulazione*
4. **Premi ▶ Avvia Simulazione**
5. **Esplora i risultati** nei pannelli

> Mode 1: prima scarica i dati dalla tab **🔗 Dati Infura**.
""")

    with st.expander("🏛️ Come Funziona il Protocollo"):
        st.markdown("""
**Attori:**

| Attore | Ruolo |
|---|---|
| User | Paga premio, riceve rimborso se attaccato |
| Pool | Raccoglie premi, paga rimborsi |
| MEV Bot | Esegue sandwich attack sulla vittima |

**Flusso:**
1. Utente paga premio P prima dello swap
2. Se attaccato → claim auto-approvato
3. Payout = loss_eth × Fcov_rimborso
""")

    with st.expander("📐 Formula del Premio"):
        st.markdown("""
```
P = V × [(Patt × L%) + (Tint × E/(1−E)) / (Vbase × 1000)] × (1 + M) × Fcov
```

**Termine 1** — costo puro del rischio stocastico:
- `Patt × L%` = probabilità attacco × perdita media

**Termine 2** — costo del rischio da falsi negativi:
- `Tint` = frodi intercettate (D-1) × valore medio swap [ETH] — **calcolato automaticamente**
- `E` = False Negative Rate — configurabile in sidebar
- `Vbase` = swap assicurati giorno D-1 — **calcolato automaticamente** dal simulatore

**Aggiornamento giornaliero:**
- `Vbase` = numero di swap assicurati del giorno precedente (Mode 1: dati Infura, Mode 2: sintetici)
- `Tint` = n_frodi_intercettate_{D-1} × avg_swap_value_{D-1} in ETH

**Logica frodi:**
- `n_fraud_attempts` = n_real_attacks × fraud_claim_pct
- `n_fraud_caught` = n_fraud_attempts × (1 − E)  → nessun payout, contribuisce a Tint
- `n_fraud_escaped` = n_fraud_attempts × E         → payout erogato (frode non rilevata)

**Margine dinamico:**
- `M = Mbase + M_adj`
- `M_adj`: 0.00 (SR ≥ soglia alta) / 0.05 (SR ≥ soglia media) / 0.10 (SR < soglia media)

**Copertura:**
- `Fcov`: 0.70 Bassa / 0.90 Media / 1.00 Alta

**Rottura pool:**
- Solo `balance_eth < 0` è rottura
- SR è usato SOLO per modulare M_adj, non come condizione di stop
""")

    with st.expander("🎲 Randomicità"):
        st.markdown("""
- **Seed automatico**: `int(time.time()) % 99999` — ogni esecuzione produce risultati diversi
- **Patt giornaliero**: base × `rng.uniform(0.7, 1.3)` ogni giorno (noise ±30%)
- **Swap per utente**: `rng.poisson(swap_freq_mean)` ogni giorno
""")

    with st.expander("🔍 Rilevamento Sandwich"):
        st.markdown("""
**Algoritmo in `scripts/download_blocks.py`:**

1. Raccoglie eventi `Swap` da Uniswap/Sushiswap/Curve via `eth_getLogs` (CHUNK_SIZE=500)
2. Ordina per blocco, indice transazione, indice log
3. Cerca triple consecutive (frontrun, victim, backrun) che soddisfano **tutti** i criteri:
   - Stessa pool address
   - Blocchi consecutivi (max distanza 1)
   - Tutti e tre i tx_hash diversi tra loro
4. Calcola `Patt = (n_sandwich / n_swap) × (1 + ms)` con margine di sicurezza ms

> **Nota:** il rilevamento usa esclusivamente i dati già disponibili nei log (`eth_getLogs`),
> senza chiamate aggiuntive `eth_getTransactionByHash`. Questo rende il fetch molto più veloce
> e riduce le chiamate Infura al minimo.
""")
