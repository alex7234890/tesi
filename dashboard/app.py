"""
MEV Insurance Protocol — Streamlit Dashboard (v4: UI visiva, batch avanzato, oracle, toggle mode).

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
from itertools import product as _itertools_product

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


def info_box(titolo: str, contenuto: str, colore: str = "blue") -> None:
    """Colori: blue, green, orange, red"""
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

# --- Controlla disponibilità cache Infura ---
_n_swaps_sidebar = _db_swap_count()
_cache_exists     = _n_swaps_sidebar > 0

st.sidebar.markdown("### Fonte Dati")
if not _cache_exists:
    st.sidebar.warning(
        "⚠️ Nessun dato Infura in cache — simulazione completamente sintetica.\n\n"
        "Vai alla tab **🔗 Dati Infura** per scaricare i dati reali."
    )
else:
    st.sidebar.success(f"✅ DB Infura: {_n_swaps_sidebar:,} swap disponibili")

use_infura_txs = st.sidebar.toggle(
    "Usa transazioni reali da Infura",
    value=_cache_exists,
    disabled=not _cache_exists,
    key="use_infura",
)
# Patt is always set manually — never derived from Infura
use_infura_patt = False

# Derive mode (1 = real txs from Infura, 2 = synthetic)
mode     = 1 if use_infura_txs else 2
is_mode1 = mode == 1
is_mode2 = mode == 2

st.sidebar.markdown("---")
st.sidebar.markdown("### Parametri Simulazione")

duration_days = st.sidebar.number_input(
    "Durata simulazione (giorni)", min_value=1, max_value=365, value=30, step=1,
    key="sim_duration",
)
swaps_per_day = st.sidebar.number_input(
    "N swap/giorno (solo sintetica)", min_value=10, max_value=10000, value=100, step=10,
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

with st.sidebar.expander("🔮 Oracle"):
    oracle_reward_per_claim = st.number_input(
        "Reward per oracle per claim (ETH)",
        min_value=0.0001, max_value=0.05, value=0.002, step=0.0005, format="%.4f",
        key="oracle_reward_eth",
        help="Costo ETH pagato a ciascun oracle per ogni claim valutato",
    )

with st.sidebar.expander("🔬 Parametri Sintetica / Patt"):
    patt_override = st.slider(
        "Patt (tasso attacco)", 0.01, 0.50, 0.10, step=0.01, key="m2_patt_override",
        help="Probabilità base di sandwich attack. Ogni giorno oscilla ±30%.",
    )
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
    oracle_reward_per_claim=0.002,
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

    # Patt is always set from the manual slider — regardless of data source
    cfg.setdefault("market", {})["attack_rate"] = float(patt_override)

    if mode == 2:
        cfg["users"]["initial_count"]       = int(n_synthetic_users)
        cfg["users"]["swap_frequency_mean"] = max(1, int(swaps_per_day / max(n_synthetic_users, 1)))
        cfg["users"]["max_daily_swaps"]     = int(max_daily_swaps)

    # Oracle reward
    cfg.setdefault("oracles", {})["reward_patt_update_eth"] = float(oracle_reward_per_claim)

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
        oracle_reward_per_claim=oracle_reward_per_claim,
        use_infura_txs=use_infura_txs,
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
        oracle_reward_per_claim=kwargs.get("oracle_reward_per_claim", oracle_reward_per_claim),
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

    _use_infura_txs  = p.get("use_infura_txs", p["mode"] == 1)
    # Patt is always manual
    patt_src = f"slider manuale **{p['patt_override']:.2%}** + rumore ±30%/giorno"

    total_sw_est = p["duration_days"] * p["swaps_per_day"] if not _use_infura_txs else "—"
    exp_atk_est  = int(p["duration_days"] * p["patt_override"] * p["swaps_per_day"]) if not _use_infura_txs else "—"

    # Fraud example numbers (hypothetical 100 swaps)
    ex_sw   = 100
    ex_atk  = round(ex_sw * patt_ex)
    ex_fr   = round(ex_atk * _fcp)
    ex_tot  = ex_atk + ex_fr
    ex_caught  = round(ex_fr * (1.0 - _e))
    ex_escaped = ex_fr - ex_caught

    # Data source declaration
    if _use_infura_txs:
        ds_block = (
            "**Fonti dati — Reali Infura:**\n"
            "- Swap: hash e pool reali da Infura (`eth_getLogs`)\n"
            "- Valore swap in ETH: stimato sinteticamente (log-normale, media ~0.5 ETH)\n"
            "- Patt: slider manuale + rumore ±30%/giorno\n"
            "- **Vbase**: conteggio swap assicurati reali del giorno D-1 (da Infura)\n"
            "- **Tint** (ETH): n_frodi_intercettate_{D-1} × valore_medio_swap_{D-1}"
        )
    else:
        ds_block = (
            "**Fonti dati — Sintetica:**\n"
            "- Swap: generati sinteticamente (Poisson con media N/giorno, seed casuale)\n"
            "- Valore swap: log-normale (media ~0.5 ETH, σ=0.4)\n"
            "- Patt: slider manuale + rumore ±30%/giorno\n"
            "- **Vbase**: conteggio swap sintetici assicurati del giorno D-1\n"
            "- **Tint** (ETH): n_frodi_intercettate_{D-1} × valore_medio_swap_{D-1}"
        )

    return (
        f"## 📋 Riepilogo Simulazione\n\n"
        f"**Transazioni:** {'🔗 Reali Infura' if _use_infura_txs else '🎲 Sintetiche'} | "
        f"**Patt:** ✏️ Manuale ({p['patt_override']:.2%}) | "
        f"**Durata:** {p['duration_days']} giorni | "
        f"**Copertura:** {p['coverage_label']} → rimborso {reimb}, Fcov={_fcov:.2f}\n\n"
        "---\n\n"
        "### Formula Premio\n\n"
        "```\nP = V × [(Patt × L%) + (Tint × E/(1−E)) / Vbase] × (1 + M) × Fcov\n```\n\n"
        "> Tint in ETH = somma valore swap delle frodi intercettate ieri  \n"
        "> Vbase = numero di swap assicurati ieri\n\n"
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
    if use_infura_txs and _db_swap_count() == 0:
        errors.append("Il database Infura è vuoto. Scarica i dati prima di usare transazioni reali.")
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

    # --- Fonte dati visiva ---
    _fc1, _fc2, _fc3 = st.columns(3)
    with _fc1:
        info_box("Transazioni",
                 "🔗 Reali da Infura" if use_infura_txs else "🎲 Sintetiche",
                 "green" if use_infura_txs else "orange")
    with _fc2:
        _patt_disp = f"{patt_override:.2%}"
        info_box("Patt",
                 f"🔗 Da Infura: {_patt_disp}" if use_infura_patt else f"✏️ Manuale: {_patt_disp}",
                 "green" if use_infura_patt else "blue")
    with _fc3:
        info_box("Vbase/Tint", "📊 Calcolati automaticamente dai dati del giorno precedente", "blue")

    results    = st.session_state["results"]
    summaries  = st.session_state["summaries"]
    collectors = st.session_state["collectors"]

    if not results:
        if use_infura_txs and _db_swap_count() == 0:
            info_box("Dati mancanti",
                     "Scarica prima i dati nella tab 🔗 Dati Infura, oppure disabilita "
                     "il toggle <b>Usa transazioni reali da Infura</b>.", "orange")
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

        # ---- Pannello 4: Utenti (solo sintetica) ----
        if last_mode == 2:
            st.markdown("---")
            st.subheader("4 — Distribuzione Utenti")
            fonte("Utenti sintetici da SyntheticDataSource — solo simulazione sintetica")

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
            oc_today  = float(row.get("oracle_cost_today", 0.0))
            nor_today = int(row.get("n_oracles_used_today", 0))
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
                    "Costo oracle totale oggi",
                    "  — oracle utilizzati",
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
                    f"{oc_today:.4f} ETH",
                    f"{nor_today} oracle-claim",
                    f"{tint_d:.4f} ETH",
                    f"{vbase_d:.0f} swap",
                    f"{'+' if net>=0 else ''}{net:.4f} ETH",
                    f"{float(row['pool_balance_eth']):.4f} ETH",
                    f"{float(row['solvency_ratio']):.4f}",
                    f"{float(row.get('madj_current',0)):.4f}",
                ]
            }), use_container_width=True, hide_index=True)

            # ---- Term1 / Term2 visual section ----
            st.markdown("#### 📐 Term1 e Term2 del Giorno")
            _t1_vis = float(row.get("patt_current", 0.0)) * float(row.get("loss_pct_current", loss_pct))
            _e_vis  = float(row.get("e_today", e_fnr))
            _e_vis  = max(min(_e_vis, 0.9999), 0.0001)
            _tint_vis  = float(row.get("tint_today", 0.0))
            _vbase_vis = float(row.get("vbase_today", 100.0))
            _t2_vis = (_tint_vis * (_e_vis / (1.0 - _e_vis))) / max(_vbase_vis, 1.0) if _vbase_vis > 0 else 0.0
            _madj_vis = float(row.get("madj_current", 0.0))
            _mtot_vis = float(row.get("m_total_current", mbase))
            _rate_vis = (_t1_vis + _t2_vis) * (1.0 + _mtot_vis) * fcov * 100.0
            _cv1, _cv2, _cv3 = st.columns(3)
            _cv1.metric("Term1 (Patt × L%)", f"{_t1_vis:.4f}", help="Rischio attacchi reali")
            _cv2.metric("Term2 (frodi non rilevate)", f"{_t2_vis:.4f}", help="Costo frodi che sfuggono al rilevamento")
            _cv3.metric("Premio medio applicato", f"{_rate_vis:.3f}%", help="% del valore swap pagata come premio")
            _tot_vis = _t1_vis + _t2_vis if (_t1_vis + _t2_vis) > 0 else 1e-9
            _pct1_vis = _t1_vis / _tot_vis * 100
            _pct2_vis = _t2_vis / _tot_vis * 100
            st.markdown(
                f'<div style="display:flex;height:20px;border-radius:4px;overflow:hidden;margin:4px 0">'
                f'<div style="width:{_pct1_vis:.0f}%;background:#1f77b4;display:flex;align-items:center;'
                f'justify-content:center;color:white;font-size:11px">Term1 {_pct1_vis:.0f}%</div>'
                f'<div style="width:{_pct2_vis:.0f}%;background:#ff7f0e;display:flex;align-items:center;'
                f'justify-content:center;color:white;font-size:11px">Term2 {_pct2_vis:.0f}%</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

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
                _term2   = (_tint_d * _e_ratio) / max(_vbase_d, 1.0) if _vbase_d > 0 else 0.0
                _sum_t   = _term1 + _term2
                _fcov_d  = fcov
                _ex_prem = 1.0 * _sum_t * (1.0 + _mtot_d) * _fcov_d
                _n_fc_d  = int(row.get("n_fraud_caught", 0))
                _avg_sv  = float(row.get("avg_swap_value_eth", 0.0))
                _n_fe_d  = int(row.get("n_fraud_escaped", 0))

                _mode_lbl = last_mode
                _vbase_src = (
                    "swap assicurati ieri — da Infura"
                    if _mode_lbl == 1 else
                    "swap sintetici assicurati ieri"
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
                    f"Term2         = (Tint × E/(1−E)) / Vbase             = {_term2:.6f}\n"
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
                ("Transazioni", "Reali Infura" if _p.get("use_infura_txs", _p["mode"]==1) else "Sintetiche"),
                ("Patt", "Manuale (slider)"),
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
                rows.append(("Patt manuale (Infura mode)", f"{_p['patt_override']:.2%}"))
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
                t2       = (tint_d * (e_safe / (1.0 - e_safe))) / max(vbase_d, 1.0) if vbase_d > 0 else 0.0
                prem_ex  = (t1 + t2) * (1 + m_tot) * _fcov_v
                n_fc     = int(fr.get("n_fraud_caught", 0))
                n_fe     = int(fr.get("n_fraud_escaped", 0))
                avg_sv   = float(fr.get("avg_swap_value_eth", 0.0))
                _vb_src  = "Infura D-1" if (_mode_l == 1) else "sintetico D-1"
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
            fonte("P = V × [(Patt × L%) + (Tint × E/(1−E)) / Vbase] × (1+M) × Fcov")


# ==========================================================================
# ==========================================================================
# TAB 2 — BATCH SIMULAZIONI
# ==========================================================================
with tab_batch:
    st.subheader("🔁 Batch Simulazioni")
    st.markdown("Esegui N simulazioni variando i parametri con prodotto cartesiano. Analizza la distribuzione.")

    b1, b2 = st.columns([1, 2])
    with b1:
        _bn_steps = st.number_input("Passi per parametro", min_value=2, max_value=20, value=5, step=1, key="batch_n_steps",
                                     help="N valori equidistanti per ogni parametro in modalità Range")
        _b_use_real = st.toggle("Usa transazioni Infura", value=_cache_exists, disabled=not _cache_exists, key="batch_use_infura")
        _b_mode = 1 if _b_use_real else 2
    with b2:
        _b_params = st.multiselect(
            "Parametri variabili",
            ["Patt", "L%", "E (FNR)", "Frodi%", "Pool iniziale", "Oracle reward"],
            default=["Patt", "E (FNR)"],
            key="batch_vars",
        )

    # Per-parameter range config
    _b_param_config: dict = {}
    if _b_params:
        st.markdown("**Range per parametro variabile:**")
        _pc_cols = st.columns(min(len(_b_params), 3))
        _bp_defaults = {
            "Patt":          (0.02, 0.20),
            "L%":            (0.05, 0.40),
            "E (FNR)":       (0.05, 0.50),
            "Frodi%":        (0.0,  0.30),
            "Pool iniziale": (20.0, 200.0),
            "Oracle reward": (0.001, 0.01),
        }
        for _bi, _bp in enumerate(_b_params):
            with _pc_cols[_bi % len(_pc_cols)]:
                st.markdown(f"*{_bp}*")
                _bmode_p = st.selectbox("Modalità", ["Range lineare", "Casuale nel range", "Fisso"],
                                        key=f"batch_pmode_{_bi}")
                _def_min, _def_max = _bp_defaults.get(_bp, (0.01, 0.50))
                if _bmode_p == "Fisso":
                    _bfv = st.number_input("Valore fisso", value=round((_def_min+_def_max)/2, 4), key=f"batch_fixed_{_bi}")
                    _b_param_config[_bp] = {"mode": "fixed", "value": float(_bfv)}
                else:
                    _bmin = st.number_input("Min", value=_def_min, key=f"batch_min_{_bi}")
                    _bmax = st.number_input("Max", value=_def_max, key=f"batch_max_{_bi}")
                    _b_param_config[_bp] = {
                        "mode": "range" if _bmode_p == "Range lineare" else "random",
                        "min": float(_bmin), "max": float(_bmax),
                    }

    # Compute total combinations
    def _batch_combos(pcfg: dict, n_steps: int) -> list:
        pv = {}
        for name, cfg in pcfg.items():
            if cfg["mode"] == "fixed":
                pv[name] = [cfg["value"]]
            elif cfg["mode"] == "range":
                pv[name] = list(np.linspace(cfg["min"], cfg["max"], n_steps))
            else:  # random
                pv[name] = list(np.random.uniform(cfg["min"], cfg["max"], n_steps))
        keys = list(pv.keys())
        combos = list(_itertools_product(*[pv[k] for k in keys]))
        return [dict(zip(keys, c)) for c in combos]

    _total_combos = len(_batch_combos(_b_param_config, int(_bn_steps))) if _b_param_config else int(_bn_steps)

    # --- Riepilogo configurazione batch ---
    with st.expander("📋 Riepilogo configurazione batch", expanded=True):
        _b_patt_mode  = _b_param_config.get("Patt",  {}).get("mode", "—")
        _b_fnr_mode   = _b_param_config.get("E (FNR)", {}).get("mode", "—")
        _b_mbase_mode = "fisso"
        _b_fraud_mode = _b_param_config.get("Frodi%", {}).get("mode", "—")
        _b_orc_mode   = _b_param_config.get("Oracle reward", {}).get("mode", "—")
        st.markdown(f"""
| Parametro | Valore base | Variazione |
|-----------|------------|-----------|
| Passi per parametro | {_bn_steps} | — |
| Patt | {patt_override:.3f} | {_b_patt_mode} |
| E (FNR) | {e_fnr:.2f} | {_b_fnr_mode} |
| Mbase | {mbase:.2f} | {_b_mbase_mode} |
| Frodi% | {fraud_claim_pct:.2f} | {_b_fraud_mode} |
| Oracle reward | {oracle_reward_per_claim:.4f} ETH | {_b_orc_mode} |
| Pool iniziale | {initial_pool_balance:.1f} ETH | — |
| **Combinazioni totali** | **{_total_combos}** | — |
""")
        if _total_combos > 400:
            st.warning(f"⚠️ {_total_combos} combinazioni — riduci i passi o i parametri variabili")
        elif _total_combos > 100:
            st.info(f"ℹ️ {_total_combos} combinazioni — potrebbe richiedere qualche minuto")

    _batch_run_btn = st.button("✅ Confermo — Avvia Batch", type="primary", key="batch_run_btn")

    if _batch_run_btn:
        _b_combos  = _batch_combos(_b_param_config, int(_bn_steps))
        _b_results = []
        _b_prog    = st.progress(0, text="Avvio batch…")
        _b_cfg_base = _make_cfg(mode=_b_mode)

        for _bi, _run_p in enumerate(_b_combos):
            _rc = copy.deepcopy(_b_cfg_base)
            _rc["simulation"]["seed"] = (int(_time_mod.time() * 1000) + _bi) % 99999
            if "Patt"          in _run_p: _rc["market"]["attack_rate"]          = _run_p["Patt"]
            if "L%"            in _run_p: _rc["market"]["loss_pct_mean"]        = _run_p["L%"]
            if "E (FNR)"       in _run_p: _rc["market"]["e"]                    = _run_p["E (FNR)"]
            if "Frodi%"        in _run_p: _rc["simulation"]["fraud_claim_pct"]  = _run_p["Frodi%"]
            if "Pool iniziale" in _run_p: _rc["pool"]["initial_balance_eth"]    = _run_p["Pool iniziale"]
            if "Oracle reward" in _run_p: _rc.setdefault("oracles", {})["reward_patt_update_eth"] = _run_p["Oracle reward"]

            _run_p_rounded = {k: round(float(v), 6) for k, v in _run_p.items()}
            try:
                _c, _p, _s = run_single(_rc, mode=_b_mode, coverage=coverage, db_path=_DB_PATH)
                _df_run = _c.to_dataframe()
                _b_results.append({
                    "run": _bi + 1,
                    **_run_p_rounded,
                    "pool_survived":      _s["pool_survived"],
                    "profitto_eth":       round(_s["total_profit_eth"], 4),
                    "sr_finale":          round(_s["final_solvency_ratio"], 4),
                    "giorno_rottura":     (_s.get("breakdown_event") or {}).get("day", "—"),
                    "trend_eth_giorno":   round(_s.get("trend_slope", 0.0), 4),
                    "premio_medio_pct":   round(_s.get("avg_premium_rate_pct", 0.0), 4),
                    "term1_medio":        round(_s.get("avg_term1", 0.0), 6),
                    "term2_medio":        round(_s.get("avg_term2", 0.0), 6),
                    "oracle_cost_totale": round(_s.get("total_oracle_cost_eth", 0.0), 4),
                    "payout_reali_eth":   round(_s.get("total_real_payouts_eth", 0.0), 4),
                    "payout_frodi_eth":   round(_s.get("total_fraud_payouts_eth", 0.0), 4),
                    "_df":                _df_run,
                })
            except Exception as _bex:
                _b_results.append({
                    "run": _bi + 1, **_run_p_rounded,
                    "pool_survived": False, "profitto_eth": 0.0,
                    "sr_finale": 0.0, "giorno_rottura": "—",
                    "trend_eth_giorno": 0.0, "premio_medio_pct": 0.0,
                    "term1_medio": 0.0, "term2_medio": 0.0,
                    "oracle_cost_totale": 0.0, "payout_reali_eth": 0.0, "payout_frodi_eth": 0.0,
                    "_df": None, "_error": str(_bex),
                })
            _b_prog.progress((_bi + 1) / max(len(_b_combos), 1), text=f"Run {_bi+1}/{len(_b_combos)}…")

        st.session_state["batch_results"] = _b_results
        _b_prog.progress(100, text="✅ Batch completato!")
        st.rerun()

    # --- Show results ---
    _br = st.session_state.get("batch_results")
    if _br:
        _br_clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in _br]
        _br_df    = pd.DataFrame(_br_clean)

        st.markdown("---")

        # --- Metriche top ---
        _mc1, _mc2, _mc3, _mc4 = st.columns(4)
        _n_surv = sum(1 for r in _br_clean if r.get("pool_survived", False))
        _prem_vals = [r.get("premio_medio_pct", 0) for r in _br_clean]
        _mc1.metric("Simulazioni totali", len(_br_clean))
        _mc2.metric("Pool sopravvissuti", _n_surv,
                    delta=f"{_n_surv/max(len(_br_clean),1):.0%}")
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

    st.info("ℹ️ **Mode 1:** Le transazioni reali vengono scaricate da Infura. "
            "Patt e tutti gli altri parametri (loss, Tint, Vbase, E) sono configurati manualmente.")

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
                st.session_state["infura_last_result"] = {
                    "metadata": _m,
                }
                st.success(f"✅ {_m['total_swaps']:,} swap scaricati | {_m['infura_calls_used']} chiamate Infura")
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
            _sc.close()
            def _fmt(t):
                try: return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%d/%m/%Y")
                except: return "—"
            st.markdown(
                f"| Campo | Valore |\n|---|---|\n"
                f"| Aggiornamento DB | {_lfs} |\n| Cache | {_ci} |\n"
                f"| Blocchi | #{_mb:,}→#{_xb:,} |\n| Periodo | {_fmt(_mt)}—{_fmt(_xt)} |\n"
                f"| **Swap totali** | **{_tsw:,}** |\n"
                f"| — Uniswap V2 | {_dc.get('uniswap_v2',0):,} |\n"
                f"| — Uniswap V3 | {_dc.get('uniswap_v3',0):,} |\n"
                f"| — Sushiswap | {_dc.get('sushiswap',0):,} |\n"
                f"| — Curve | {_dc.get('curve',0):,} |\n"
            )
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
                    _br_data.append({
                        "Blocco #":             bn,
                        "Data (UTC)":           _date_s,
                        "Ora (UTC)":            _time_s,
                        "Swap totali nel pool": ns,
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
                    "per quel pool in quel blocco."
                )
            else:
                st.info("Nessun dato di blocco disponibile.")
        except Exception as _bex:
            st.warning(f"Blocchi non disponibili: {_bex}")
    else:
        st.info("Scarica i dati per vedere il dettaglio dei blocchi.")

    st.markdown("---")
    st.markdown("### 🔍 Come Funziona il Download Dati")

    _ilr = st.session_state.get("infura_last_result")
    try:
        from scripts.download_blocks import CHUNK_SIZE as _CS, BLOCKS_PER_DAY as _BPD2
    except Exception:
        _CS, _BPD2 = 500, 6600

    info_box("Metodo di fetch",
        f"<b>eth_getLogs</b> scarica in blocco tutti gli eventi Swap dai pool DEX selezionati, "
        f"divisi in chunk da {_CS} blocchi. Nessun fetch blocco per blocco.", "blue")

    _ci1, _ci2, _ci3 = st.columns(3)
    _ci1.markdown(
        '<div style="background:#1f77b415;border-left:3px solid #1f77b4;padding:8px;border-radius:4px">'
        '🏊 <b>Pool DEX</b><br><small>Scarica eventi Swap da Uniswap V2/V3, Sushiswap, Curve</small></div>',
        unsafe_allow_html=True)
    _ci2.markdown(
        '<div style="background:#2ca02c15;border-left:3px solid #2ca02c;padding:8px;border-radius:4px">'
        f'📦 <b>Chunk da {_CS} blocchi</b><br><small>~{(_BPD2 + _CS - 1) // _CS} chiamate per giorno invece di ~{_BPD2:,}</small></div>',
        unsafe_allow_html=True)
    _ci3.markdown(
        '<div style="background:#ff7f0e15;border-left:3px solid #ff7f0e;padding:8px;border-radius:4px">'
        '💾 <b>Cache locale</b><br><small>I dati vengono salvati in SQLite per riuso offline</small></div>',
        unsafe_allow_html=True)

    if _ilr:
        _meta = _ilr["metadata"]
        _ts   = _meta.get("total_swaps", 0)
        _nc   = _meta.get("total_chunks", 0)
        _nb   = _meta.get("total_blocks", 0)
        _ic   = _meta.get("infura_calls_used", 0)
        st.markdown(
            f"**Ultimo fetch:** {_ts:,} swap in {_nc} chunk ({_nb:,} blocchi) — {_ic} chiamate Infura"
        )
    else:
        st.info("Scarica i dati Infura per vedere le statistiche del fetch.")


# ==========================================================================
# TAB 3 — ISTRUZIONI
# ==========================================================================
with tab_istr:
    st.subheader("📖 Guida al Simulatore MEV Insurance")

    st.markdown("""
### Come Usare il Simulatore

1. **Scarica i dati Infura** nella tab 🔗 (opzionale, per transazioni reali)
2. **Attiva il toggle** nella sidebar: *Usa transazioni reali da Infura* (se disponibile)
3. **Configura Patt** nel slider *Patt (tasso attacco)* — sempre manuale
4. **Configura** gli altri parametri negli expander della sidebar
5. **Leggi il Riepilogo** nella tab *Simulazione*
6. **Premi ▶ Avvia Simulazione**
7. **Esplora i risultati** nei pannelli

> Senza dati Infura: il toggle è disabilitato e la simulazione è completamente sintetica.
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
P = V × [(Patt × L%) + (Tint × E/(1−E)) / Vbase] × (1 + M) × Fcov
```

**Termine 1** — costo puro del rischio stocastico:
- `Patt × L%` = probabilità attacco × perdita media

**Termine 2** — costo del rischio da falsi negativi:
- `Tint` = frodi intercettate (D-1) × valore medio swap [ETH] — **calcolato automaticamente**
- `E` = False Negative Rate — configurabile in sidebar
- `Vbase` = swap assicurati giorno D-1 — **calcolato automaticamente** dal simulatore

**Aggiornamento giornaliero:**
- `Vbase` = numero di swap assicurati del giorno precedente (Infura reali o sintetici)
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

    with st.expander("🔍 Download Dati Infura"):
        st.markdown("""
**Algoritmo in `scripts/download_blocks.py`:**

1. Raccoglie eventi `Swap` da Uniswap/Sushiswap/Curve via `eth_getLogs` (CHUNK_SIZE=500)
2. Ordina per blocco, indice transazione, indice log
3. Salva gli swap nel database SQLite locale per riuso
4. Patt è **sempre impostato manualmente** tramite lo slider sidebar

> **Note:** il fetch usa esclusivamente `eth_getLogs` senza chiamate aggiuntive
> `eth_getTransactionByHash`. ~27 chiamate invece di ~13.300 per 2 giorni.
> Patt non viene calcolato dai dati — usa il valore dello slider.
""")
