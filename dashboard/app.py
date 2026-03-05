"""
MEV Insurance Protocol — Streamlit Dashboard (refactor completo).

Avvio:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import datetime
import os
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

_DB_PATH  = os.path.join(_ROOT, "data", "blockchain.db")
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

# =========================================================================
# Session state
# =========================================================================
for _k, _v in [
    ("results", {}), ("summaries", {}), ("collectors", {}),
    ("last_mode", 2), ("confirm_clear_cache", False),
    ("infura_download_log", ""),
]:
    if _k not in st.session_state:
        st.session_state[_k] = _v


# =========================================================================
# SIDEBAR
# =========================================================================
st.sidebar.title("🛡️ MEV Insurance Simulator")
st.sidebar.markdown("---")

# ---- Selezione modalità ----
mode = st.sidebar.radio(
    "Modalità",
    [1, 2],
    format_func=lambda m: "Mode 1 — Dati Reali" if m == 1 else "Mode 2 — Sintetica",
    index=1,
    key="mode_select",
)
is_mode1 = mode == 1
is_mode2 = mode == 2

st.sidebar.markdown("---")
st.sidebar.markdown("### Parametri Simulazione")

duration_days = st.sidebar.number_input(
    "Durata simulazione (giorni)", min_value=1, max_value=365, value=30, step=1,
    key="sim_duration",
)
swaps_per_day = st.sidebar.number_input(
    "N swap/giorno (solo Mode 2)", min_value=10, max_value=10000, value=100, step=10,
    key="sim_swaps_day", disabled=is_mode1,
    help="Media Poisson degli swap generati per giorno. Ignorato in Mode 1.",
)
coverage_label = st.sidebar.selectbox(
    "Livello di Copertura",
    _COVERAGE_LABELS, index=1,
    key="sim_coverage",
    help="Bassa → 50% rimborso, Fcov=0.70 | Media → 70%, Fcov=0.90 | Alta → 100%, Fcov=1.00",
)
coverage = _COVERAGE_INTERNAL[coverage_label]

omit_oracle_fraud = st.sidebar.toggle(
    "Ometti frodi oracle (simulazione semplificata)",
    value=False, key="sim_omit_oracle_fraud",
    help="Se attivo: tutti gli oracle sono onesti, nessuno slashing, watchlist sempre vuota.",
)

st.sidebar.markdown("---")

# ---- Parametri Protocollo ----
with st.sidebar.expander("⚙️ Parametri Protocollo — Avanzati"):
    mbase = st.slider(
        "Margine base Mbase", 0.05, 0.50, 0.20, step=0.01,
        key="prot_mbase",
        help="Margine base aggiunto ad ogni premio",
    )
    loss_pct = st.slider(
        "L% — perdita media per attacco", 0.05, 0.40, 0.20, step=0.01,
        key="prot_loss_pct",
    )
    false_negative_rate = st.slider(
        "E — Tasso Falsi Negativi (FNR)", 0.01, 0.50, 0.20, step=0.01,
        key="prot_fnr",
        help="Frazione di frodi che sfuggono al rilevamento",
    )
    sr_threshold_high = st.slider(
        "Soglia SR ALTA (sano)", 1.3, 2.0, 1.50, step=0.05,
        key="prot_sr_high",
        help="SR ≥ soglia → M_adj = 0.00",
    )
    sr_threshold_med = st.slider(
        "Soglia SR MEDIA (rischio medio)", 1.0, 1.5, 1.30, step=0.05,
        key="prot_sr_med",
        help="SR tra MED e HIGH → M_adj = 0.05",
    )
    oracle_reward_claim = st.number_input(
        "Reward oracle per claim (ETH)", value=0.002, format="%.4f",
        key="prot_oracle_reward",
    )
    captcha_reward = st.number_input(
        "Reward CAPTCHA (ETH)", value=0.001, format="%.4f",
        key="prot_captcha_reward",
    )
    initial_pool_balance = st.number_input(
        "Saldo iniziale pool (ETH)", min_value=10.0, max_value=10000.0, value=100.0, step=10.0,
        key="prot_pool_balance",
    )

# ---- Parametri Oracle ----
with st.sidebar.expander("🔮 Parametri Oracle"):
    n_oracles = st.number_input(
        "N oracle nella rete", min_value=3, max_value=100, value=10, step=1,
        key="ora_n_oracles",
    )
    n_oracles_per_claim = st.number_input(
        "N oracle sorteggiati per claim", min_value=3, max_value=21, value=7, step=1,
        key="ora_per_claim",
    )
    n_oracles_patt = st.number_input(
        "N oracle sorteggiati per aggiornamento Patt", min_value=3, max_value=15, value=5, step=1,
        key="ora_patt",
    )
    divergence_threshold = st.number_input(
        "Soglia divergenza watchlist (punti)", min_value=1, max_value=50, value=10, step=1,
        key="ora_div_threshold",
    )
    entry_divergences = st.number_input(
        "N divergenze per entrare in watchlist", min_value=1, max_value=10, value=2, step=1,
        key="ora_entry_diverg",
    )
    watchlist_months = st.number_input(
        "Mesi persistenza watchlist", min_value=1, max_value=12, value=3, step=1,
        key="ora_wl_months",
    )
    oracle_stake_min = st.number_input(
        "Stake minimo oracle (ETH)", min_value=0.1, value=1.0, step=0.1, format="%.1f",
        key="ora_stake_min",
    )

# ---- Parametri Slashing ----
with st.sidebar.expander("⚔️ Parametri Slashing"):
    slash_deposit = st.number_input(
        "Deposito contestazione (ETH)", min_value=0.01, value=0.10, step=0.01, format="%.2f",
        key="sl_deposit",
    )
    slash_pool_pct = st.slider(
        "% stake → pool assicurativo", 0, 100, 60, step=1,
        key="sl_pool_pct",
    )
    slash_reporter_pct = st.slider(
        "% stake → reporter", 0, 100, 25, step=1,
        key="sl_reporter_pct",
    )
    slash_jury_pct = st.slider(
        "% stake → jury", 0, 100, 15, step=1,
        key="sl_jury_pct",
    )
    slash_total = slash_pool_pct + slash_reporter_pct + slash_jury_pct
    if slash_total != 100:
        st.error(
            f"⚠️ Le percentuali di slashing sommano a {slash_total}% invece di 100%. "
            "Correggi i tre slider prima di avviare la simulazione."
        )

# ---- Parametri FraudScore ----
with st.sidebar.expander("🔍 Parametri FraudScore"):
    fs_bronze   = st.number_input("Score base tier Bronze",   min_value=0, max_value=130, value=50, step=1, key="fs_bronze")
    fs_silver   = st.number_input("Score base tier Silver",   min_value=0, max_value=130, value=30, step=1, key="fs_silver")
    fs_gold     = st.number_input("Score base tier Gold",     min_value=0, max_value=130, value=15, step=1, key="fs_gold")
    fs_platinum = st.number_input("Score base tier Platinum", min_value=0, max_value=130, value=0,  step=1, key="fs_platinum")
    fs_auto_approve = st.number_input(
        "Soglia approvazione automatica (< X → approvato)", min_value=0, max_value=130, value=60, step=1,
        key="fs_auto_approve",
    )
    fs_captcha = st.number_input(
        "Soglia CAPTCHA (≥ X → CAPTCHA; > Y → rigettato)", min_value=0, max_value=130, value=80, step=1,
        key="fs_captcha",
    )
    cr_very_susp = st.slider("Claim rate — soglia molto sospetta (%)", 0, 100, 30, step=1, key="fs_cr_high")
    cr_susp_high = st.slider("Claim rate — soglia alta (%)",           0, 100, 20, step=1, key="fs_cr_med")
    cr_susp_med  = st.slider("Claim rate — soglia media (%)",          0, 100, 10, step=1, key="fs_cr_low")

# ---- Parametri Upgrade Tier ----
with st.sidebar.expander("📈 Parametri Upgrade Tier"):
    b2s_swaps  = st.number_input("Bronze→Silver: min swap",             min_value=1,  value=18,  step=1, key="tu_b2s_swaps")
    b2s_days   = st.number_input("Bronze→Silver: min giorni attivi",    min_value=1,  value=30,  step=1, key="tu_b2s_days")
    b2s_maxfs  = st.number_input("Bronze→Silver: max FraudScore medio", min_value=0,  max_value=130, value=52, step=1, key="tu_b2s_maxfs")
    s2g_swaps  = st.number_input("Silver→Gold: min swap",               min_value=1,  value=55,  step=1, key="tu_s2g_swaps")
    s2g_days   = st.number_input("Silver→Gold: min giorni attivi",      min_value=1,  value=60,  step=1, key="tu_s2g_days")
    s2g_maxfs  = st.number_input("Silver→Gold: max FraudScore medio",   min_value=0,  max_value=130, value=35, step=1, key="tu_s2g_maxfs")
    pt_stake   = st.slider("Stake Platinum (% del limite swap scelto)", 5, 50, 20, step=1, key="tu_pt_stake")

st.sidebar.markdown("---")

# ---- Parametri Mode 1 (sempre nel DOM, disabilitati in Mode 2) ----
with st.sidebar.expander("🔗 Parametri Mode 1 — Dati Reali"):
    infura_api_key = st.text_input(
        "Chiave API Infura", value="", type="password",
        placeholder="Inserisci il Project ID Infura",
        key="m1_infura_key", disabled=is_mode2,
    )
    block_range_days = st.number_input(
        "Intervallo blocchi (giorni)", min_value=1, max_value=7, value=2, step=1,
        key="m1_block_range", disabled=is_mode2,
    )
    dex_targets = st.multiselect(
        "Contratti DEX monitorati",
        _DEX_OPTIONS, default=["Uniswap V2", "Uniswap V3"],
        key="m1_dex_targets", disabled=is_mode2,
    )

# ---- Parametri Mode 2 (sempre nel DOM, disabilitati in Mode 1) ----
with st.sidebar.expander("🔬 Parametri Mode 2 — Sintetica"):
    patt_file_path = st.text_input(
        "Percorso file Patt", value="data/patt_historical.csv",
        key="m2_patt_file", disabled=is_mode1,
    )
    rng_seed = st.number_input(
        "Seed casuale", min_value=0, max_value=99999, value=42, step=1,
        key="m2_seed", disabled=is_mode1,
    )
    n_synthetic_users = st.number_input(
        "N utenti sintetici (iniziali)", min_value=5, max_value=500, value=50, step=5,
        key="m2_n_users", disabled=is_mode1,
    )
    fraud_rate = st.slider(
        "Tasso frode utenti", 0.0, 0.30, 0.05, step=0.01,
        key="m2_fraud_rate", disabled=is_mode1,
    )
    st.markdown("**Distribuzione tier iniziale**")
    tier_bronze_pct   = st.slider("Bronze %",   0, 100, 70, step=5,  key="m2_tier_bronze",   disabled=is_mode1)
    tier_silver_pct   = st.slider("Silver %",   0, 100, 20, step=5,  key="m2_tier_silver",   disabled=is_mode1)
    tier_gold_pct     = st.slider("Gold %",     0, 100,  8, step=1,  key="m2_tier_gold",     disabled=is_mode1)
    tier_platinum_pct = st.slider("Platinum %", 0, 100,  2, step=1,  key="m2_tier_platinum", disabled=is_mode1)
    tier_total = tier_bronze_pct + tier_silver_pct + tier_gold_pct + tier_platinum_pct
    if is_mode2 and tier_total != 100:
        st.warning(f"⚠️ I tier sommano a {tier_total}% — devono sommare esattamente a 100%.")

st.sidebar.markdown("---")
run_btn    = st.sidebar.button("▶ Avvia Simulazione", type="primary", key="run_btn")
export_btn = st.sidebar.button("📥 Esporta CSV", key="export_btn")


# =========================================================================
# Funzioni di supporto
# =========================================================================

def _build_config(
    mode: int, duration_days: int, swaps_per_day: int, coverage: str,
    mbase: float, loss_pct: float, false_negative_rate: float,
    sr_threshold_high: float, sr_threshold_med: float,
    oracle_reward_claim: float, captcha_reward: float, initial_pool_balance: float,
    infura_api_key: str, block_range_days: int,
    rng_seed: int, n_synthetic_users: int,
    tier_bronze_pct: float, tier_silver_pct: float,
    tier_gold_pct: float, tier_platinum_pct: float, fraud_rate: float,
    omit_oracle_fraud: bool,
    n_oracles: int, n_oracles_per_claim: int, divergence_threshold: int,
    entry_divergences: int, watchlist_months: int, oracle_stake_min: float,
    slash_deposit: float, slash_pool_pct: int, slash_reporter_pct: int, slash_jury_pct: int,
    fs_bronze: int, fs_silver: int, fs_gold: int, fs_platinum: int,
    fs_auto_approve: int, fs_captcha: int,
    cr_very_susp: int, cr_susp_high: int, cr_susp_med: int,
    b2s_swaps: int, b2s_days: int, b2s_maxfs: int,
    s2g_swaps: int, s2g_days: int, s2g_maxfs: int, pt_stake: int,
) -> dict:
    cfg_path = os.path.join(
        _ROOT, "config",
        "mode1_realchain.yaml" if mode == 1 else "mode2_synthetic.yaml",
    )
    cfg = load_config(cfg_path)

    cfg["simulation"]["duration_days"] = int(duration_days)
    cfg["simulation"]["seed"]          = int(rng_seed)

    cfg["pool"]["mbase"]                              = float(mbase)
    cfg["pool"]["initial_balance_eth"]                = float(initial_pool_balance)
    cfg["pool"]["solvency_thresholds"]["high_risk"]   = float(sr_threshold_med)
    cfg["pool"]["solvency_thresholds"]["medium_risk"] = float(sr_threshold_high)

    cfg["market"]["loss_pct_mean"] = float(loss_pct)

    cfg["fraud_detection"]["false_negative_rate"] = float(false_negative_rate)
    cfg["fraud_detection"]["user_fraud_rate"]     = float(fraud_rate)
    cfg["fraud_detection"]["fraud_score_decision"]["auto_approve"] = int(fs_auto_approve)
    cfg["fraud_detection"]["fraud_score_decision"]["captcha_low"]  = int(fs_auto_approve)
    cfg["fraud_detection"]["fraud_score_decision"]["captcha_high"] = int(fs_captcha)
    cfg["fraud_detection"]["fraud_score_decision"]["auto_reject"]  = int(fs_captcha)
    cfg["fraud_detection"]["claim_rate_thresholds"]["very_suspicious"] = cr_very_susp / 100.0
    cfg["fraud_detection"]["claim_rate_thresholds"]["suspicious_high"] = cr_susp_high / 100.0
    cfg["fraud_detection"]["claim_rate_thresholds"]["suspicious_med"]  = cr_susp_med  / 100.0

    cfg["oracles"]["initial_count"]        = int(n_oracles)
    cfg["oracles"]["n_selected_per_claim"] = int(n_oracles_per_claim)
    cfg["oracles"]["stake_min_eth"]        = float(oracle_stake_min)
    cfg["oracles"]["reward_per_claim_eth"] = float(oracle_reward_claim)
    cfg["oracles"]["reward_captcha_eth"]   = float(captcha_reward)
    cfg["oracles"]["fraud_enabled"]        = not omit_oracle_fraud
    cfg["oracles"]["watchlist"]["divergence_threshold"] = int(divergence_threshold)
    cfg["oracles"]["watchlist"]["entry_divergences"]    = int(entry_divergences)
    cfg["oracles"]["watchlist"]["persistence_months"]   = int(watchlist_months)
    cfg["oracles"]["slashing"]["contestation_stake_eth"]      = float(slash_deposit)
    cfg["oracles"]["slashing"]["distribution"]["pool"]         = slash_pool_pct     / 100.0
    cfg["oracles"]["slashing"]["distribution"]["reporter"]     = slash_reporter_pct / 100.0
    cfg["oracles"]["slashing"]["distribution"]["jury"]         = slash_jury_pct     / 100.0

    cfg["tiers"]["bronze"]["fraud_score_base"]   = int(fs_bronze)
    cfg["tiers"]["silver"]["fraud_score_base"]   = int(fs_silver)
    cfg["tiers"]["gold"]["fraud_score_base"]     = int(fs_gold)
    cfg["tiers"]["platinum"]["fraud_score_base"] = int(fs_platinum)
    cfg["tiers"]["platinum"]["stake_pct"]        = pt_stake / 100.0
    cfg["tiers"]["upgrades"]["bronze_to_silver"]["min_swaps"]           = int(b2s_swaps)
    cfg["tiers"]["upgrades"]["bronze_to_silver"]["min_days"]            = int(b2s_days)
    cfg["tiers"]["upgrades"]["bronze_to_silver"]["max_avg_fraud_score"] = int(b2s_maxfs)
    cfg["tiers"]["upgrades"]["silver_to_gold"]["min_swaps"]             = int(s2g_swaps)
    cfg["tiers"]["upgrades"]["silver_to_gold"]["min_days"]              = int(s2g_days)
    cfg["tiers"]["upgrades"]["silver_to_gold"]["max_avg_fraud_score"]   = int(s2g_maxfs)

    if mode == 2:
        cfg["users"]["initial_count"]       = int(n_synthetic_users)
        cfg["users"]["fraud_rate"]          = float(fraud_rate)
        cfg["users"]["swap_frequency_mean"] = max(1, int(swaps_per_day / max(n_synthetic_users, 1)))
        cfg["users"]["initial_tier_distribution"] = {
            "bronze":   float(tier_bronze_pct)   / 100.0,
            "silver":   float(tier_silver_pct)   / 100.0,
            "gold":     float(tier_gold_pct)     / 100.0,
            "platinum": float(tier_platinum_pct) / 100.0,
        }

    if mode == 1 and infura_api_key:
        cfg["blockchain"]["infura_url"] = f"wss://mainnet.infura.io/ws/v3/{infura_api_key}"
        cfg["blockchain"]["block_range_days"] = int(block_range_days)

    return cfg


def _all_params() -> dict:
    """Raccoglie tutti i parametri sidebar in un unico dict."""
    return dict(
        mode=mode, duration_days=duration_days, swaps_per_day=swaps_per_day,
        coverage_label=coverage_label, coverage=coverage,
        mbase=mbase, loss_pct=loss_pct, false_negative_rate=false_negative_rate,
        sr_threshold_high=sr_threshold_high, sr_threshold_med=sr_threshold_med,
        oracle_reward_claim=oracle_reward_claim, captcha_reward=captcha_reward,
        initial_pool_balance=initial_pool_balance,
        omit_oracle_fraud=omit_oracle_fraud,
        n_oracles=n_oracles, n_oracles_per_claim=n_oracles_per_claim,
        n_oracles_patt=n_oracles_patt, divergence_threshold=divergence_threshold,
        entry_divergences=entry_divergences, watchlist_months=watchlist_months,
        oracle_stake_min=oracle_stake_min,
        slash_deposit=slash_deposit, slash_pool_pct=slash_pool_pct,
        slash_reporter_pct=slash_reporter_pct, slash_jury_pct=slash_jury_pct,
        fs_bronze=fs_bronze, fs_silver=fs_silver, fs_gold=fs_gold, fs_platinum=fs_platinum,
        fs_auto_approve=fs_auto_approve, fs_captcha=fs_captcha,
        cr_very_susp=cr_very_susp, cr_susp_high=cr_susp_high, cr_susp_med=cr_susp_med,
        b2s_swaps=b2s_swaps, b2s_days=b2s_days, b2s_maxfs=b2s_maxfs,
        s2g_swaps=s2g_swaps, s2g_days=s2g_days, s2g_maxfs=s2g_maxfs, pt_stake=pt_stake,
        infura_api_key=infura_api_key, block_range_days=block_range_days,
        dex_targets=dex_targets, patt_file_path=patt_file_path,
        rng_seed=rng_seed, n_synthetic_users=n_synthetic_users, fraud_rate=fraud_rate,
        tier_bronze_pct=tier_bronze_pct, tier_silver_pct=tier_silver_pct,
        tier_gold_pct=tier_gold_pct, tier_platinum_pct=tier_platinum_pct,
    )


# =========================================================================
# render_riepilogo — costruisce una stringa markdown (nessun widget interno)
# =========================================================================

def render_riepilogo(p: dict) -> str:
    fcov  = _COVERAGE_FCOV[p["coverage_label"]]
    reimb = _COVERAGE_REIMB[p["coverage_label"]]
    fnr   = p["false_negative_rate"]
    fnr_mult = fnr / (1.0 - fnr) if fnr < 1.0 else 999.0
    M_adj_ex = 0.0  # stato sano per l'esempio concreto

    # Esempio con V=1 ETH, Patt=5%, Tint=0
    patt_ex   = 0.05
    base_risk = patt_ex * p["loss_pct"]
    M_total   = p["mbase"] + M_adj_ex
    premium_ex = 1.0 * base_risk * (1.0 + M_total) * fcov
    pct_val    = premium_ex * 100.0

    attack_pct = 100.0 / (1.0 + M_total) if M_total >= 0 else 100.0
    margin_pct = M_total / (1.0 + M_total) * 100.0 if M_total >= 0 else 0.0

    mode_str  = "Mode 1 — Dati Reali" if p["mode"] == 1 else "Mode 2 — Sintetica"
    tier_str  = (
        "disabilitato (Mode 1)" if p["mode"] == 1
        else (
            f"attivo — Bronze {p['tier_bronze_pct']}% / Silver {p['tier_silver_pct']}% / "
            f"Gold {p['tier_gold_pct']}% / Platinum {p['tier_platinum_pct']}%"
        )
    )
    oracle_str = "disabilitate (modalità semplificata)" if p["omit_oracle_fraud"] else "attive"

    if p["mode"] == 1:
        patt_src = "dati reali da Infura (cache o fetch live)"
    else:
        patt_exists = os.path.isfile(os.path.join(_ROOT, p["patt_file_path"]))
        patt_src = f"file CSV `{p['patt_file_path']}`" if patt_exists else "sintetico ~5% ±2%"

    total_swaps_est = p["duration_days"] * p["swaps_per_day"] if p["mode"] == 2 else "—"
    exp_attacks_est = (
        int(p["duration_days"] * 0.05 * p["swaps_per_day"]) if p["mode"] == 2 else "—"
    )
    exp_fraud_est = (
        int(p["duration_days"] * p["fraud_rate"] * p["swaps_per_day"]) if p["mode"] == 2 else "—"
    )

    fraud_warn = (
        "\n> ⚠️ **Modalità semplificata:** frodi oracle disabilitate. "
        "Slashing, watchlist e divergenze non verranno simulati.\n"
        if p["omit_oracle_fraud"] else ""
    )
    slash_warn = (
        "\n> ⚠️ **Attenzione:** le percentuali di slashing non sommano a 100% — "
        "correggere prima di avviare.\n"
        if (p["slash_pool_pct"] + p["slash_reporter_pct"] + p["slash_jury_pct"]) != 100
        else ""
    )

    return (
        f"## 📋 Riepilogo Simulazione\n\n"
        f"**Modalità:** {mode_str} | "
        f"**Durata:** {p['duration_days']} giorni | "
        f"**Copertura:** {p['coverage_label']} → rimborso {reimb}, Fcov={fcov:.2f}\n"
        f"{fraud_warn}{slash_warn}\n"
        "---\n\n"
        "### Formula Premio\n\n"
        "```\n"
        "P = V × [(Patt × L%) + (Tint × E/(1−E)) / (Vbase × 1000)] × (1+M) × Fcov\n"
        "```\n\n"
        "Con i parametri attuali:\n\n"
        f"| Parametro | Valore |\n|---|---|\n"
        f"| L% | {p['loss_pct']:.2%} — perdita media attesa per attacco |\n"
        f"| E (FNR) | {fnr:.2%} → moltiplicatore frodi = E/(1−E) = **{fnr_mult:.4f}** |\n"
        f"| Mbase | {p['mbase']:.2%} |\n"
        f"| M_adj | dinamico: 0.00 (SR≥{p['sr_threshold_high']}) / "
        f"0.05 (SR≥{p['sr_threshold_med']}) / 0.10 (SR<{p['sr_threshold_med']}) |\n"
        f"| Fcov | {fcov:.2f} (Copertura {p['coverage_label']}) |\n\n"
        f"**Esempio concreto** — swap da 1 ETH, Patt=5%, Tint=0 (stato sano):\n\n"
        f"> P = 1 × [0.05 × {p['loss_pct']:.2f}] × (1 + {M_total:.2f}) × {fcov:.2f} = "
        f"**{premium_ex:.5f} ETH** ({pct_val:.3f}% del valore assicurato)\n"
        f"> — di cui copertura attacchi: ~{attack_pct:.1f}% del premio\n"
        f"> — di cui margine: ~{margin_pct:.1f}% del premio\n\n"
        "---\n\n"
        "### Fonti Dati\n\n"
        f"- **Patt:** {patt_src}\n"
        "- **Tint, Vbase:** aggiornati ogni giorno simulato dallo stato interno\n"
        f"- **Aggiornamento Patt:** ogni 24h simulati da {p['n_oracles_patt']} oracle sorteggiati\n\n"
        "---\n\n"
        "### Cosa Verrà Simulato\n\n"
        f"- ~**{total_swaps_est}** swap totali | "
        f"~**{exp_attacks_est}** attacchi sandwich | "
        f"~**{exp_fraud_est}** claim fraudolenti attesi\n"
        f"- Tier system: {tier_str}\n"
        f"- Frodi oracle: {oracle_str}\n"
        f"- Oracle in rete: **{p['n_oracles']}** | sorteggiati per claim: **{p['n_oracles_per_claim']}**\n"
        f"- Distribuzione slashing: pool {p['slash_pool_pct']}% / "
        f"reporter {p['slash_reporter_pct']}% / jury {p['slash_jury_pct']}%\n"
    )


# =========================================================================
# Avvio simulazione
# =========================================================================

if run_btn:
    errors = []
    if slash_total != 100:
        errors.append(
            f"Le percentuali di slashing sommano a {slash_total}% invece di 100%."
        )
    if is_mode2:
        t_sum = tier_bronze_pct + tier_silver_pct + tier_gold_pct + tier_platinum_pct
        if t_sum != 100:
            errors.append(f"La distribuzione tier somma a {t_sum}% invece di 100%.")

    if errors:
        for e in errors:
            st.error(f"⚠️ {e} Correggi i parametri prima di avviare.")
    else:
        p = _all_params()
        cfg = _build_config(
            mode=mode, duration_days=duration_days, swaps_per_day=swaps_per_day,
            coverage=coverage, mbase=mbase, loss_pct=loss_pct,
            false_negative_rate=false_negative_rate, sr_threshold_high=sr_threshold_high,
            sr_threshold_med=sr_threshold_med, oracle_reward_claim=oracle_reward_claim,
            captcha_reward=captcha_reward, initial_pool_balance=initial_pool_balance,
            infura_api_key=infura_api_key, block_range_days=block_range_days,
            rng_seed=rng_seed, n_synthetic_users=n_synthetic_users,
            tier_bronze_pct=tier_bronze_pct, tier_silver_pct=tier_silver_pct,
            tier_gold_pct=tier_gold_pct, tier_platinum_pct=tier_platinum_pct,
            fraud_rate=fraud_rate, omit_oracle_fraud=omit_oracle_fraud,
            n_oracles=n_oracles, n_oracles_per_claim=n_oracles_per_claim,
            divergence_threshold=divergence_threshold, entry_divergences=entry_divergences,
            watchlist_months=watchlist_months, oracle_stake_min=oracle_stake_min,
            slash_deposit=slash_deposit, slash_pool_pct=slash_pool_pct,
            slash_reporter_pct=slash_reporter_pct, slash_jury_pct=slash_jury_pct,
            fs_bronze=fs_bronze, fs_silver=fs_silver, fs_gold=fs_gold, fs_platinum=fs_platinum,
            fs_auto_approve=fs_auto_approve, fs_captcha=fs_captcha,
            cr_very_susp=cr_very_susp, cr_susp_high=cr_susp_high, cr_susp_med=cr_susp_med,
            b2s_swaps=b2s_swaps, b2s_days=b2s_days, b2s_maxfs=b2s_maxfs,
            s2g_swaps=s2g_swaps, s2g_days=s2g_days, s2g_maxfs=s2g_maxfs, pt_stake=pt_stake,
        )
        with st.spinner("Simulazione in corso…"):
            collector, pool, summary = run_single(
                cfg, mode=mode, coverage=coverage, db_path=_DB_PATH,
            )
            label = coverage_label
            st.session_state["results"]    = {label: collector.to_dataframe()}
            st.session_state["summaries"]  = {label: summary}
            st.session_state["collectors"] = {label: collector}
            st.session_state["last_mode"]  = mode
        st.success("✅ Simulazione completata! Esplora i risultati nella tab Simulazione.")

# Esporta CSV
if export_btn and st.session_state["results"]:
    frames = [
        df.assign(run=lbl)
        for lbl, df in st.session_state["results"].items()
    ]
    csv_data = pd.concat(frames, ignore_index=True).to_csv(index=False).encode("utf-8")
    st.sidebar.download_button(
        "⬇ Scarica CSV", data=csv_data,
        file_name="mev_risultati_simulazione.csv", mime="text/csv",
        key="csv_download",
    )


# =========================================================================
# Area principale — 3 tab
# =========================================================================
st.title("🛡️ MEV Insurance Protocol Simulator")

tab_sim, tab_infura, tab_istr = st.tabs(
    ["📊 Simulazione", "🔗 Dati Infura", "📖 Istruzioni"]
)

# ==========================================================================
# TAB 1 — SIMULAZIONE
# ==========================================================================
with tab_sim:
    # Riepilogo sempre visibile
    st.markdown(render_riepilogo(_all_params()))

    results    = st.session_state["results"]
    summaries  = st.session_state["summaries"]
    collectors = st.session_state["collectors"]

    if not results:
        st.info("Configura i parametri nella sidebar e premi **▶ Avvia Simulazione** per cominciare.")
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
        c1.metric("Solvency Ratio finale",  f"{first_summary['final_solvency_ratio']:.3f}")
        c2.metric("Tasso approvazione claim", f"{first_summary['claim_approval_rate']:.1%}")
        c3.metric("Pool sopravvissuto", "SÌ ✓" if first_summary["pool_survived"] else "NO ✗")

        st.markdown("---")

        # ---- Pannello 1: Salute Pool ----
        st.subheader("1 — Salute del Pool nel Tempo")
        st.caption("Fonte: saldo e Solvency Ratio tracciati da InsurancePool dopo ogni giornata simulata")
        pool_df = first_df[[
            "day", "pool_balance_eth", "pending_liabilities_eth",
            "solvency_ratio", "madj_current", "net_flow_today",
        ]].copy()
        pool_df.columns = [
            "Giorno", "Saldo Pool (ETH)", "Passività Pendenti (ETH)",
            "Solvency Ratio", "M_adj", "Variazione Netta (ETH)",
        ]

        def _color_net(v):
            try:
                return "color: green" if float(v) >= 0 else "color: red"
            except Exception:
                return ""

        st.dataframe(
            pool_df.style
            .format({
                "Saldo Pool (ETH)": "{:.4f}", "Passività Pendenti (ETH)": "{:.4f}",
                "Solvency Ratio": "{:.4f}", "M_adj": "{:.2f}", "Variazione Netta (ETH)": "{:+.4f}",
            })
            .applymap(_color_net, subset=["Variazione Netta (ETH)"]),
            use_container_width=True, height=300,
        )

        st.markdown("---")

        # ---- Pannello 2: Flusso di Cassa ----
        st.subheader("2 — Flusso di Cassa")
        st.caption("Fonte: ETH cumulativi incassati come premi, pagati come payout e reward oracle")

        last = first_df.iloc[-1]
        ca, cb, cc, cd = st.columns(4)
        ca.metric("Premi Totali (ETH)",         f"{float(last['total_premiums_collected_eth']):.4f}")
        cb.metric("Payout Totali (ETH)",         f"{float(last['total_payouts_eth']):.4f}")
        cc.metric("Reward Oracle Totali (ETH)",  f"{float(last['total_oracle_rewards_eth']):.4f}")
        cd.metric("Profitto Netto (ETH)",        f"{float(last['profit_eth']):.4f}")

        cf_df = first_df[[
            "day", "premiums_today", "payouts_today",
            "oracle_rewards_today", "net_flow_today",
        ]].copy()
        cf_df.columns = [
            "Giorno", "Premi Oggi (ETH)", "Payout Oggi (ETH)",
            "Reward Oracle Oggi (ETH)", "Flusso Netto (ETH)",
        ]
        st.dataframe(
            cf_df.style.format({c: "{:.4f}" for c in cf_df.columns if c != "Giorno"}),
            use_container_width=True, height=250,
        )

        st.markdown("---")

        # ---- Pannello 3: Analisi Claim ----
        st.subheader("3 — Analisi Claim")
        st.caption("Fonte: decisioni del ClaimProcessor per ogni claim processato")

        tot_sub  = int(first_df["n_claims_submitted"].sum())
        tot_app  = int(first_df["n_claims_approved"].sum())
        tot_cap  = int(first_df["n_claims_captcha"].sum())
        tot_rej  = int(first_df["n_claims_rejected"].sum())
        avg_rate = first_df["claim_approval_rate"].mean()
        avg_fs   = first_df["avg_fraud_score"].replace(0, np.nan).mean()

        ce1, ce2, ce3 = st.columns(3)
        ce1.metric("Tasso approvazione medio",  f"{avg_rate:.1%}")
        ce2.metric("FraudScore medio",          f"{avg_fs:.1f}" if not np.isnan(avg_fs) else "N/A")
        ce3.metric("Claim totali inviati",       str(tot_sub))

        cl_df = first_df[[
            "day", "n_claims_submitted", "n_claims_approved",
            "n_claims_captcha", "n_claims_rejected",
            "n_rejected_fraud_score_gt_80", "n_rejected_pattern_invalid",
            "n_rejected_captcha_failed",
        ]].copy()
        cl_df.columns = [
            "Giorno", "Inviati", "Approvati", "CAPTCHA", "Rigettati",
            "  — score > 80", "  — pattern non valido", "  — CAPTCHA fallito",
        ]
        st.dataframe(cl_df, use_container_width=True, height=250)

        # ---- Pannello 4: Distribuzione Utenti (Mode 2) ----
        if last_mode == 2:
            st.markdown("---")
            st.subheader("4 — Distribuzione Utenti")
            st.caption("Fonte: conteggio utenti attivi per tier tracciato dal TierManager (solo Mode 2)")

            ud_df = first_df[[
                "day", "n_users_bronze", "n_users_silver",
                "n_users_gold", "n_users_platinum", "n_users_blacklisted",
            ]].copy()
            ud_df.columns = ["Giorno", "Bronze", "Silver", "Gold", "Platinum", "Blacklistati"]
            st.dataframe(ud_df, use_container_width=True, height=250)

        # ---- Pannello 5: Rete Oracle ----
        st.markdown("---")
        st.subheader("5 — Rete Oracle")
        st.caption("Fonte: metriche OracleNetwork — divergenze, watchlist, slashing giornaliero")

        or_cols = ["day", "n_oracles_active", "n_oracles_watchlist",
                   "avg_oracle_divergence", "avg_oracle_reward_eth"]
        # cumulative slashing
        oracle_df = first_df[or_cols].copy()
        oracle_df.insert(3, "Slashati (cum.)", first_df["n_oracles_slashed"].cumsum())
        # total slashed ETH (from new metric column if present)
        if "total_slashed_eth_cum" in first_df.columns:
            oracle_df["Stake Slashato (ETH)"] = first_df["total_slashed_eth_cum"]
        oracle_df.columns = (
            ["Giorno", "Oracle Attivi", "In Watchlist", "Slashati (cum.)",
             "Divergenza Media", "Reward Medio (ETH)"]
            + (["Stake Slashato (ETH)"] if "total_slashed_eth_cum" in first_df.columns else [])
        )
        st.dataframe(
            oracle_df.style.format({
                "Divergenza Media": "{:.2f}", "Reward Medio (ETH)": "{:.4f}",
                **({
                    "Stake Slashato (ETH)": "{:.4f}"
                } if "Stake Slashato (ETH)" in oracle_df.columns else {}),
            }),
            use_container_width=True, height=250,
        )

        # ---- Pannello 💰: Flussi Economici e Stake ----
        st.markdown("---")
        st.subheader("💰 Flussi Economici e Stake")

        last_row   = first_df.iloc[-1]
        premi_tot  = float(last_row["total_premiums_collected_eth"])
        payout_tot = float(last_row["total_payouts_eth"])
        reward_tot = float(last_row["total_oracle_rewards_eth"])
        profit_tot = float(last_row["profit_eth"])
        saldo_fin  = float(last_row["pool_balance_eth"])

        slashed_tot = (
            float(last_row["total_slashed_eth_cum"])
            if "total_slashed_eth_cum" in last_row.index else 0.0
        )
        stake_tot = (
            float(last_row["total_oracle_stake_eth"])
            if "total_oracle_stake_eth" in last_row.index else 0.0
        )

        sl_pool_eth     = slashed_tot * slash_pool_pct     / 100.0
        sl_reporter_eth = slashed_tot * slash_reporter_pct / 100.0
        sl_jury_eth     = slashed_tot * slash_jury_pct     / 100.0

        totale_ref = max(premi_tot + slashed_tot, 1e-9)

        eco_data = {
            "Voce": [
                "Stake oracle totale bloccato",
                "  — di cui slashato",
                f"  — al pool ({slash_pool_pct}%)",
                f"  — ai reporter ({slash_reporter_pct}%)",
                f"  — alla jury ({slash_jury_pct}%)",
                "Premi incassati",
                "Payout erogati",
                "Reward oracle pagati",
                "Profitto netto",
                "Saldo pool finale",
            ],
            "Importo (ETH)": [
                stake_tot, slashed_tot, sl_pool_eth, sl_reporter_eth, sl_jury_eth,
                premi_tot, payout_tot, reward_tot, profit_tot, saldo_fin,
            ],
        }
        eco_df = pd.DataFrame(eco_data)
        eco_df["% del totale"] = eco_df["Importo (ETH)"].apply(
            lambda x: f"{abs(x) / totale_ref * 100:.1f}%"
        )
        eco_df["Importo (ETH)"] = eco_df["Importo (ETH)"].apply(lambda x: f"{x:.4f}")
        st.dataframe(eco_df, use_container_width=True, hide_index=True)

        # Dove va ogni ETH di stake slashato
        n_jury = n_oracles_per_claim
        jury_each = sl_jury_eth / max(n_jury, 1) if slashed_tot > 0 else 0.0
        jury_each_per_eth = (slash_jury_pct / 100.0) / max(n_jury, 1)

        M_total_ex = mbase
        fnr_ex     = false_negative_rate
        base_risk  = 0.05 * loss_pct
        raw_ex     = base_risk
        prem_example = raw_ex * (1 + M_total_ex) * fcov
        attack_frac = (base_risk * fcov / prem_example * 100) if prem_example > 0 else 0
        margin_frac = (raw_ex * M_total_ex * fcov / prem_example * 100) if prem_example > 0 else 0

        st.markdown(
            f"**Su ogni ETH di stake confiscato (distribuzione slashing configurata):**\n"
            f"- → {slash_pool_pct / 100:.2f} ETH al pool assicurativo\n"
            f"- → {slash_reporter_pct / 100:.2f} ETH al reporter\n"
            f"- → {slash_jury_pct / 100:.2f} ETH divisi tra {n_jury} giurati "
            f"(**{jury_each_per_eth:.4f} ETH** ciascuno)\n\n"
            f"**Su ogni ETH di premio pagato dall'utente** (Patt=5%, Tint=0, Mbase={mbase:.0%}):\n"
            f"- → ~{attack_frac:.1f}% copre il rischio attacchi reali (Patt × L% × Fcov)\n"
            f"- → ~0.0% frodi non rilevate (Tint=0 nell'esempio)\n"
            f"- → ~{margin_frac:.1f}% è margine di profitto (M × componenti)\n"
        )

        st.markdown("---")

        # ---- Pannello 6: Esploratore Giornaliero ----
        st.subheader("6 — Esploratore Giornaliero")

        max_day      = int(first_df["day"].max())
        selected_day = st.slider(
            "Seleziona giorno", min_value=0, max_value=max_day, value=0, step=1,
            key="day_explorer_slider",
        )

        row = first_df[first_df["day"] == selected_day]
        if row.empty:
            st.warning(f"Nessun dato per il giorno {selected_day}.")
        else:
            row = row.iloc[0]
            left_col, right_col = st.columns(2)

            with left_col:
                st.markdown("**Stato Pool**")
                net_flow = float(row.get("net_flow_today", 0.0))
                stato_df = pd.DataFrame({
                    "Metrica": [
                        "Saldo Pool (ETH)", "Passività Pendenti (ETH)", "Solvency Ratio",
                        "M_adj (margine dinamico)", "Patt (tasso attacco)",
                        "Premi raccolti oggi (ETH)", "Payout eseguiti oggi (ETH)",
                        "Reward oracle oggi (ETH)", "Flusso netto oggi (ETH)",
                    ],
                    "Valore": [
                        f"{float(row['pool_balance_eth']):.4f}",
                        f"{float(row.get('pending_liabilities_eth', 0.0)):.4f}",
                        f"{float(row['solvency_ratio']):.4f}",
                        f"{float(row.get('madj_current', 0.0)):.2f}",
                        f"{float(row.get('patt_current', 0.0)):.2%}",
                        f"{float(row.get('premiums_today', 0.0)):.4f}",
                        f"{float(row.get('payouts_today', 0.0)):.4f}",
                        f"{float(row.get('oracle_rewards_today', 0.0)):.4f}",
                        f"{'+' if net_flow >= 0 else ''}{net_flow:.4f}",
                    ],
                })
                st.dataframe(stato_df, use_container_width=True, hide_index=True)

            with right_col:
                st.markdown("**Attività del Giorno**")
                n_swaps    = int(row.get("n_swaps_this_tick", 0))
                n_attacked = int(row.get("n_attacks_this_tick", 0))
                n_insured  = int(row.get("n_swaps_insured", n_swaps))
                pct_att    = f"{n_attacked / max(n_swaps, 1):.1%}"
                attiv_df   = pd.DataFrame({
                    "Metrica": [
                        "Swap processati", "  — di cui attaccati", "  — di cui assicurati",
                        "Claim inviati", "Claim approvati", "Claim CAPTCHA", "Claim rigettati",
                        "  — pattern non valido", "  — score > 80", "  — CAPTCHA fallito",
                        "FraudScore medio (approvati)", "FraudScore medio (rigettati)",
                    ],
                    "Valore": [
                        str(n_swaps),
                        f"{n_attacked} ({pct_att})",
                        str(n_insured),
                        str(int(row.get("n_claims_submitted", 0))),
                        str(int(row.get("n_claims_approved", 0))),
                        str(int(row.get("n_claims_captcha", 0))),
                        str(int(row.get("n_claims_rejected", 0))),
                        str(int(row.get("n_rejected_pattern_invalid", 0))),
                        str(int(row.get("n_rejected_fraud_score_gt_80", 0))),
                        str(int(row.get("n_rejected_captcha_failed", 0))),
                        f"{float(row.get('avg_fraud_score_approved', 0.0)):.1f}",
                        f"{float(row.get('avg_fraud_score_rejected', 0.0)):.1f}",
                    ],
                })
                st.dataframe(attiv_df, use_container_width=True, hide_index=True)

            if first_collector is not None:
                day_swaps = first_collector.daily_swap_details.get(selected_day, [])
                with st.expander(f"Tutti gli swap del giorno {selected_day} ({len(day_swaps)} swap)", expanded=False):
                    if day_swaps:
                        sw_df = pd.DataFrame(day_swaps)
                        for col_n in ("value_ETH", "premium_paid", "payout_ETH"):
                            if col_n in sw_df.columns:
                                sw_df[col_n] = sw_df[col_n].round(6)
                        st.dataframe(sw_df, use_container_width=True)
                    else:
                        st.info("Nessun dettaglio swap disponibile per questo giorno.")

        with st.expander("📊 Dati grezzi simulazione", expanded=False):
            st.dataframe(first_df, use_container_width=True)


# ==========================================================================
# TAB 2 — DATI INFURA
# ==========================================================================
with tab_infura:
    st.subheader("🔗 Dati Infura — Gestione e Stato")

    # 5a — Stato connessione
    st.markdown("### Stato Connessione")
    key_ok = bool(infura_api_key and len(infura_api_key) > 8)
    key_masked = (infura_api_key[:4] + "****") if key_ok else "(non configurata)"

    _cache_files = _glob.glob(os.path.join(_CACHE_DIR, "blocks_*.pkl"))
    if _cache_files:
        _newest_cache  = max(_cache_files, key=os.path.getmtime)
        _cache_ts      = datetime.datetime.fromtimestamp(os.path.getmtime(_newest_cache))
        cache_status   = f"✓ presente — aggiornata il {_cache_ts.strftime('%d/%m/%Y %H:%M')}"
        cache_file_str = os.path.basename(_newest_cache)
    else:
        cache_status   = "✗ assente"
        cache_file_str = "—"

    db_exists    = os.path.isfile(_DB_PATH)
    last_fetch   = "mai"
    if db_exists:
        _ts = os.path.getmtime(_DB_PATH)
        last_fetch = datetime.datetime.fromtimestamp(_ts).strftime("%d/%m/%Y %H:%M")

    st.markdown(
        f"| Campo | Valore |\n|---|---|\n"
        f"| Chiave API | `{key_masked}` |\n"
        f"| Stato | {'🟢 Configurata' if key_ok else '🔴 Non configurata'} |\n"
        f"| Ultimo fetch DB | {last_fetch} |\n"
        f"| Cache locale | {cache_status} |\n"
        f"| File cache | `{cache_file_str}` |\n"
    )

    if not key_ok:
        st.warning(
            "⚠️ Chiave Infura non configurata. Inseriscila nel pannello "
            "**Parametri Mode 1 — Dati Reali** nella sidebar per abilitare il fetch."
        )

    st.markdown("---")

    # 5b — Pulsanti azione
    st.markdown("### Azioni")
    btn_col1, btn_col2, btn_col3 = st.columns(3)

    with btn_col1:
        if st.button("🔄 Scarica dati ora", key="infura_download_btn", disabled=not key_ok):
            infura_url = f"wss://mainnet.infura.io/ws/v3/{infura_api_key}"
            n_blocks   = int(block_range_days) * 6646
            try:
                from scripts.download_blocks import download as _dl_blocks
                with st.spinner(f"Scaricamento blocchi da Infura ({block_range_days} giorni ~ {n_blocks:,} blocchi)…"):
                    _dl_blocks(infura_url, n_blocks, _DB_PATH)
                st.session_state["infura_download_log"] = (
                    f"✅ Download completato: {n_blocks:,} blocchi, "
                    f"{datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}"
                )
                st.success(st.session_state["infura_download_log"])
            except ImportError:
                st.error("⚠️ La libreria `web3` non è installata. Esegui: `pip install web3`")
            except Exception as exc:
                st.error(f"⚠️ Errore durante il download: {exc}")

    with btn_col2:
        if st.button("🗑️ Cancella cache", key="infura_clear_btn"):
            st.session_state["confirm_clear_cache"] = True

        if st.session_state.get("confirm_clear_cache", False):
            st.warning("Sei sicuro di voler eliminare tutti i file cache (.pkl)?")
            conf_col1, conf_col2 = st.columns(2)
            with conf_col1:
                if st.button("✅ Conferma cancellazione", key="confirm_clear_yes"):
                    deleted = 0
                    for f in _glob.glob(os.path.join(_CACHE_DIR, "blocks_*.pkl")):
                        os.remove(f)
                        deleted += 1
                    st.session_state["confirm_clear_cache"] = False
                    st.success(f"🗑️ Eliminati {deleted} file cache.")
            with conf_col2:
                if st.button("❌ Annulla", key="confirm_clear_no"):
                    st.session_state["confirm_clear_cache"] = False

    with btn_col3:
        preview_btn = st.button("👁️ Anteprima dati", key="infura_preview_btn", disabled=not db_exists)

    if preview_btn and db_exists:
        try:
            con = sqlite3.connect(_DB_PATH, check_same_thread=False)
            prev_df = pd.read_sql("SELECT * FROM swaps LIMIT 50", con)
            con.close()
            st.markdown("**Primi 50 swap nel database locale:**")
            st.dataframe(prev_df, use_container_width=True)
        except Exception as exc:
            st.error(f"⚠️ Impossibile leggere il database: {exc}")

    st.markdown("---")

    # 5c — Dettaglio dati scaricati
    if db_exists:
        st.markdown("### Dettaglio Dati Scaricati")
        try:
            con = sqlite3.connect(_DB_PATH, check_same_thread=False)
            total_swaps = con.execute("SELECT COUNT(*) FROM swaps").fetchone()[0]
            min_block   = con.execute("SELECT MIN(block_number) FROM swaps").fetchone()[0] or 0
            max_block   = con.execute("SELECT MAX(block_number) FROM swaps").fetchone()[0] or 0
            min_ts      = con.execute("SELECT MIN(timestamp) FROM swaps").fetchone()[0] or 0
            max_ts      = con.execute("SELECT MAX(timestamp) FROM swaps").fetchone()[0] or 0
            sw_by_dex   = dict(con.execute("SELECT dex, COUNT(*) FROM swaps GROUP BY dex").fetchall())
            n_attacks   = con.execute("SELECT COUNT(*) FROM sandwich_attacks").fetchone()[0]
            con.close()

            def _ts(t):
                try:
                    return datetime.datetime.utcfromtimestamp(t).strftime("%d/%m/%Y")
                except Exception:
                    return "—"

            patt_pct = n_attacks / max(total_swaps, 1) * 100
            st.markdown(
                f"| Campo | Valore |\n|---|---|\n"
                f"| Blocchi analizzati | da #{min_block:,} a #{max_block:,} |\n"
                f"| Periodo coperto | {_ts(min_ts)} — {_ts(max_ts)} |\n"
                f"| Swap totali trovati | {total_swaps:,} |\n"
                f"| — Uniswap V2 | {sw_by_dex.get('uniswap_v2', 0):,} |\n"
                f"| — Uniswap V3 | {sw_by_dex.get('uniswap_v3', 0):,} |\n"
                f"| — Sushiswap | {sw_by_dex.get('sushiswap', 0):,} |\n"
                f"| — Curve | {sw_by_dex.get('curve', 0):,} |\n"
                f"| Sandwich attacks | {n_attacks:,} ({patt_pct:.2f}% degli swap) |\n"
            )
        except Exception as exc:
            st.warning(f"Impossibile leggere statistiche dal database: {exc}")
    else:
        st.info("Nessun database locale trovato. Scarica i dati con il pulsante sopra (richiede Mode 1 e chiave Infura).")

    st.markdown("---")

    # 5d — Spiegazione metodologia
    st.markdown("### Come Sono Calcolati i Dati")
    st.markdown(
        """
**Come vengono rilevati i sandwich attack:**
1. Per ogni blocco nell'intervallo, vengono lette tutte le transazioni
2. Si cercano triple (frontrun, victim, backrun) che rispettano:
   - Stesso blocco o blocchi consecutivi
   - Stesso pool DEX e stessa coppia di token
   - Stesso indirizzo per frontrun e backrun (l'attaccante)
   - Indirizzo diverso per la vittima
3. Il rapporto sandwich/swap totali diventa il valore Patt giornaliero

**Come viene calcolato Patt:**
```
Patt = (sandwich rilevati) / (swap totali) × (1 + ms)
```
dove `ms = 0.05` (volume ≥ 10.000 swap) / `0.10` (1.000–9.999) / `0.20` (< 1.000)

**Aggiornamento:**
- Il valore Patt viene ricalcolato ogni 24h di simulazione.
- **Mode 1:** calcolato dai dati reali in cache locale.
- **Mode 2:** caricato dal file CSV storico, oppure generato sinteticamente (~5% ±2%).
"""
    )


# ==========================================================================
# TAB 3 — ISTRUZIONI
# ==========================================================================
with tab_istr:
    st.subheader("📖 Guida al Simulatore MEV Insurance")

    st.markdown(
        """
### Come Usare il Simulatore

1. **Scegli la modalità** nella sidebar (Mode 1: dati reali da Ethereum / Mode 2: dati sintetici)
2. **Configura i parametri** negli expander della sidebar (i valori di default sono ragionevoli per iniziare)
3. **Leggi il Riepilogo Simulazione** nella tab *Simulazione* per capire cosa verrà calcolato
4. **Premi "▶ Avvia Simulazione"**
5. **Esplora i risultati** nei pannelli: Pool Health, Flusso di Cassa, Claim, Oracle, Flussi Economici, Esploratore Giornaliero

> **Per Mode 1:** configura prima la chiave Infura nella tab *Dati Infura* e scarica i dati.
> **Per Mode 2:** puoi usare direttamente i valori di default.
"""
    )

    with st.expander("🏛️ Come Funziona il Protocollo"):
        st.markdown(
            """
Il sistema **MEV Insurance** protegge i trader DEX dai sandwich attack su Ethereum.

**Attori principali:**

| Attore | Ruolo |
|---|---|
| **User / Policyholder** | Paga un premio per assicurare il proprio swap; presenta claim se attaccato |
| **Oracle** | Valuta i claim votando un FraudScore; tiene staked ETH come garanzia |
| **Smart Contract / Pool** | Raccoglie premi, paga payout, gestisce lo stake oracle |
| **MEV Bot** | Esegue l'attacco sandwich (frontrun + backrun) sulla vittima |
| **Reporter** | Segnala oracle disonesti depositando stake; riceve quota slashing se ha ragione |
| **Jury** | Gruppo di oracle selezionati via RANDAO che vota sulla contestazione |

**Flusso tipo:**
1. L'utente paga premio P al pool prima dello swap
2. Se attaccato, invia un claim con prova della transazione
3. Il sistema calcola il FraudScore dalla storia dell'utente e dalla rete
4. Oracle selezionati votano; la mediana determina la decisione finale
5. Claim approvato → il pool rimborsa; claim rigettato → utente in blacklist
"""
        )

    with st.expander("📐 Formula del Premio — Spiegazione Dettagliata"):
        st.markdown(
            """
```
P = V × [(Patt × L%) + (Tint × E/(1−E)) / (Vbase × 1000)] × (1+M) × Fcov
```

**Termine 1 — Rischio Attacchi Reali: `Patt × L%`**
- `Patt` = probabilità che uno swap generico venga attaccato (dal monitoraggio on-chain)
- `L%` = perdita percentuale media per swap attaccato
- *Esempio: se 5% degli swap viene attaccato con perdita media 20%, questo termine vale 0.05 × 0.20 = 0.01 (1% del valore swap)*

**Termine 2 — Costo Frodi Non Rilevate: `Tint × E/(1−E) / (Vbase × 1000)`**
- `E` = False Negative Rate: frazione di frodi che sfuggono al rilevamento
- `E/(1−E)` = moltiplicatore: se E=0.20, vale 0.25 (ogni 4 frodi rilevate, 1 sfugge)
- `Tint` = valore totale frodi intercettate nelle ultime 24h (ETH)
- `Vbase` = numero di swap assicurati nelle ultime 24h
- ⚠️ *Questo termine cresce esponenzialmente quando E → 1 (sistema inaffidabile)*

**Termine 3 — Margine: `(1+M) × Fcov`**
- `M = Mbase + M_adj`
- `M_adj` = 0.00 se SR ≥ soglia ALTA | 0.05 se SR ≥ soglia MEDIA | 0.10 se SR < soglia MEDIA
- `Fcov` = 0.70 (Bassa) / 0.90 (Media) / 1.00 (Alta)
"""
        )

    with st.expander("⚔️ Sistema di Slashing — Come Funziona"):
        st.markdown(
            f"""
1. Un oracle segnala un peer con prove + deposito contestazione ({slash_deposit:.2f} ETH)
2. RANDAO seleziona {n_oracles_per_claim} oracle come jury (nessuno può essere in watchlist)
3. Ogni giurato vota una percentuale di slash (0–100%)
4. Il sistema calcola la **mediana** dei voti
5. Se mediana = 0 → segnalazione rigettata, reporter perde il deposito
6. Se mediana 0–50% → slash parziale, oracle può reinserirsi pagando stake doppio
7. Se mediana > 50% → slash totale, oracle espulso permanentemente
8. **Distribuzione stake confiscato:**
   - {slash_pool_pct}% al pool assicurativo
   - {slash_reporter_pct}% al reporter
   - {slash_jury_pct}% divisi tra i {n_oracles_per_claim} giurati
"""
        )

    with st.expander("🔍 FraudScore — Come Viene Calcolato"):
        st.markdown(
            f"""
```
FraudScore = Score Tier + Score Claim Rate + Score Network
Range: 0–130 punti
```

**Score Tier (0–50):**
- Bronze = {fs_bronze} | Silver = {fs_silver} | Gold = {fs_gold} | Platinum = {fs_platinum}

**Score Claim Rate (0–30):**
- Claim Rate = claim totali / swap totali
- > {cr_very_susp}% → +30 | > {cr_susp_high}% → +25 | > {cr_susp_med}% → +20 | ≤ {cr_susp_med}% → +15 | < 6% → +0

**Score Network (0–50):**
- BFS dal nodo utente verso bot MEV noti nella blacklist
- Distanza 1 → +50 (collegamento diretto con bot noto)
- Distanza 2 → +30 (intermediario comune, 0 se intermediario è whitelist)
- Distanza 3 → +15 | Distanza 4–15 → valore crescente

**Decisione finale:**
- Score < {fs_auto_approve} → Approvato automaticamente (Gold/Platinum o Mode 1); altrimenti CAPTCHA
- {fs_auto_approve} ≤ Score ≤ {fs_captcha} → CAPTCHA richiesto (tutti i tier)
- Score > {fs_captcha} → Rigettato + blacklist
"""
        )

    with st.expander("🏅 Tier System — Come Funziona"):
        st.markdown(
            f"""
| Tier | Descrizione |
|---|---|
| **Bronze** | Tier iniziale per tutti i nuovi utenti |
| **Silver** | Capitale max esteso, accesso prioritario |
| **Gold** | Limite swap giornalieri aumentato |
| **Platinum** | Stake volontario, swap illimitati, FraudScore base 0 |

**Upgrade automatico Bronze → Silver:**
≥ {b2s_swaps} swap | ≥ {b2s_days} giorni attivi | FraudScore medio ≤ {b2s_maxfs}

**Upgrade automatico Silver → Gold:**
≥ {s2g_swaps} swap | ≥ {s2g_days} giorni attivi | FraudScore medio ≤ {s2g_maxfs}

**Upgrade manuale Gold → Platinum:**
CAPTCHA + deposito stake ({pt_stake}% del limite swap desiderato)

*Tutti i valori soglia sono modificabili nell'expander "Parametri Upgrade Tier" nella sidebar.*
"""
        )
