"""
MEV Insurance Protocol — Streamlit Dashboard (v2: no fraud/slashing).

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
        url = cfg.get("blockchain", {}).get("infura_url", "")
        if "/v3/" in url:
            return url.split("/v3/")[-1].strip().rstrip("/")
    except Exception:
        pass
    return ""

for _k, _v in [
    ("results", {}), ("summaries", {}), ("collectors", {}),
    ("last_mode", 2), ("confirm_clear_cache", False),
    ("infura_download_log", ""),
    ("infura_api_key", _read_infura_key_from_config()),
    ("infura_patt_value", None),
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
    oracle_reward_claim = st.number_input("Reward oracle per claim (ETH)", value=0.002, format="%.4f", key="prot_oracle_reward")
    initial_pool_balance = st.number_input("Saldo iniziale pool (ETH)", min_value=10.0, max_value=10000.0, value=50.0, step=10.0, key="prot_pool_balance")

with st.sidebar.expander("🔮 Parametri Oracle"):
    n_oracles = st.number_input("N oracle nella rete", min_value=3, max_value=100, value=10, step=1, key="ora_n_oracles")
    n_oracles_per_claim = st.number_input("N oracle per claim (costo)", min_value=1, max_value=21, value=7, step=1, key="ora_per_claim", help="Ogni claim costa reward × N oracle")

with st.sidebar.expander("🔬 Parametri Mode 2 — Sintetica"):
    patt_override = st.slider("Patt manuale (tasso attacco)", 0.01, 0.50, 0.10, step=0.01, key="m2_patt_override", disabled=is_mode1, help="Base + rumore ±30% ogni giorno")
    seed_manual   = st.toggle("Seed fisso (riproducibile)", value=False, key="m2_seed_manual", disabled=is_mode1)
    rng_seed      = st.number_input("Seed (solo se fisso)", min_value=0, max_value=99999, value=42, step=1, key="m2_seed", disabled=(is_mode1 or not seed_manual))
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
    oracle_reward_claim, initial_pool_balance,
    rng_seed, seed_manual, n_synthetic_users, max_daily_swaps,
    n_oracles, n_oracles_per_claim, patt_override,
) -> dict:
    cfg_path = os.path.join(
        _ROOT, "config",
        "mode1_realchain.yaml" if mode == 1 else "mode2_synthetic.yaml",
    )
    cfg = load_config(cfg_path)

    if seed_manual:
        run_seed = int(rng_seed)
    else:
        run_seed = int(_time_mod.time()) % 99999
        st.sidebar.info(f"Seed: {run_seed}")

    cfg["simulation"]["duration_days"] = int(duration_days)
    cfg["simulation"]["seed"]          = run_seed
    cfg["pool"]["mbase"]                              = float(mbase)
    cfg["pool"]["initial_balance_eth"]                = float(initial_pool_balance)
    cfg["pool"]["solvency_thresholds"]["high_risk"]   = float(sr_threshold_med)
    cfg["pool"]["solvency_thresholds"]["medium_risk"] = float(sr_threshold_high)
    cfg["market"]["loss_pct_mean"] = float(loss_pct)
    cfg["oracles"]["initial_count"]          = int(n_oracles)
    cfg["oracles"]["n_selected_per_claim"]   = int(n_oracles_per_claim)
    cfg["oracles"]["reward_patt_update_eth"] = float(oracle_reward_claim)

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
        oracle_reward_claim=oracle_reward_claim, initial_pool_balance=initial_pool_balance,
        n_oracles=n_oracles, n_oracles_per_claim=n_oracles_per_claim,
        patt_override=patt_override,
        seed_manual=seed_manual, rng_seed=rng_seed,
        n_synthetic_users=n_synthetic_users, max_daily_swaps=max_daily_swaps,
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
        oracle_reward_claim=kwargs.get("oracle_reward_claim", oracle_reward_claim),
        initial_pool_balance=kwargs.get("initial_pool_balance", initial_pool_balance),
        rng_seed=kwargs.get("rng_seed", rng_seed),
        seed_manual=kwargs.get("seed_manual", seed_manual),
        n_synthetic_users=kwargs.get("n_synthetic_users", n_synthetic_users),
        max_daily_swaps=kwargs.get("max_daily_swaps", max_daily_swaps),
        n_oracles=kwargs.get("n_oracles", n_oracles),
        n_oracles_per_claim=kwargs.get("n_oracles_per_claim", n_oracles_per_claim),
        patt_override=kwargs.get("patt_override", patt_override),
    )


# =========================================================================
# render_riepilogo
# =========================================================================

def render_riepilogo(p: dict) -> str:
    fcov  = _COVERAGE_FCOV[p["coverage_label"]]
    reimb = _COVERAGE_REIMB[p["coverage_label"]]
    patt_ex  = p["patt_override"] if p["mode"] == 2 else 0.05
    M_total  = p["mbase"]
    prem_ex  = patt_ex * p["loss_pct"] * (1.0 + M_total) * fcov
    pct_val  = prem_ex * 100.0

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

    return (
        f"## 📋 Riepilogo Simulazione\n\n"
        f"**Modalità:** {mode_str} | **Durata:** {p['duration_days']} giorni | "
        f"**Copertura:** {p['coverage_label']} → rimborso {reimb}, Fcov={fcov:.2f}\n\n"
        "---\n\n"
        "### Formula Premio\n\n"
        "```\nP = V × (Patt × L%) × (1 + M) × Fcov\n```\n\n"
        f"| Parametro | Valore |\n|---|---|\n"
        f"| Patt | {patt_ex:.2%} |\n"
        f"| L% | {p['loss_pct']:.2%} |\n"
        f"| Mbase | {p['mbase']:.2%} |\n"
        f"| M_adj | 0.00/0.05/0.10 in base al SR |\n"
        f"| Fcov | {fcov:.2f} |\n\n"
        f"**Esempio** — 1 ETH, stato sano: P = {prem_ex:.5f} ETH ({pct_val:.3f}%)\n\n"
        "---\n\n"
        f"- **Patt:** {patt_src}\n"
        f"- **Rottura pool:** solo se `balance_eth < 0` (SR = solo modulatore margine)\n"
        f"- **Claim:** tutti auto-approvati → payout = loss × Fcov_rimborso\n\n"
        f"~**{total_sw_est}** swap | ~**{exp_atk_est}** attacchi attesi | "
        f"oracle: {p['n_oracles']} in rete, {p['n_oracles_per_claim']} per claim\n"
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
# Area principale — 4 tab
# =========================================================================
st.title("🛡️ MEV Insurance Protocol Simulator")

tab_sim, tab_infura, tab_stress, tab_istr = st.tabs(
    ["📊 Simulazione", "🔗 Dati Infura", "🔬 Stress Test", "📖 Istruzioni"]
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
        fonte("InsurancePool: saldo ETH = premi incassati − payout erogati − costo oracle")

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

        st.markdown("---")

        # ---- Pannello 2: Flusso di Cassa ----
        st.subheader("2 — Flusso di Cassa")
        fonte("ETH cumulativi: premi da utenti, payout ai claim, costo oracle per claim approvato")

        last = first_df.iloc[-1]
        ca, cb, cc, cd = st.columns(4)
        ca.metric("Premi Totali (ETH)",        f"{float(last['total_premiums_collected_eth']):.4f}")
        cb.metric("Payout Totali (ETH)",        f"{float(last['total_payouts_eth']):.4f}")
        cc.metric("Reward Oracle Totali (ETH)", f"{float(last['total_oracle_rewards_eth']):.4f}")
        cd.metric("Profitto Netto (ETH)",       f"{float(last['profit_eth']):.4f}")

        cf_df = first_df[["day","premiums_today","payouts_today","oracle_rewards_today","net_flow_today"]].copy()
        cf_df.columns = ["Giorno","Premi Oggi (ETH)","Payout Oggi (ETH)","Reward Oracle (ETH)","Flusso Netto (ETH)"]
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

        # ---- Pannello 5: Oracle ----
        st.markdown("---")
        st.subheader("5 — Costo Rete Oracle")
        fonte("Costo oracle = reward_per_claim × n_oracle_per_claim × n_claim_approvati nel giorno")

        _or_avail = [c for c in ["day","n_oracles_active","avg_oracle_reward_eth","oracle_rewards_today"] if c in first_df.columns]
        oracle_df = first_df[_or_avail].copy()
        _ren = {"day":"Giorno","n_oracles_active":"Oracle Attivi","avg_oracle_reward_eth":"Reward/Claim (ETH)","oracle_rewards_today":"Costo Oggi (ETH)"}
        oracle_df.columns = [_ren.get(c,c) for c in _or_avail]
        st.dataframe(oracle_df.style.format({c:"{:.4f}" for c in oracle_df.columns if c!="Giorno"}),
                     use_container_width=True, height=250)

        # ---- Flussi Economici ----
        st.markdown("---")
        st.subheader("💰 Flussi Economici")

        lr = first_df.iloc[-1]
        premi_tot  = float(lr["total_premiums_collected_eth"])
        payout_tot = float(lr["total_payouts_eth"])
        reward_tot = float(lr["total_oracle_rewards_eth"])
        profit_tot = float(lr["profit_eth"])
        saldo_fin  = float(lr["pool_balance_eth"])
        ref        = max(premi_tot, 1e-9)

        eco_df = pd.DataFrame({
            "Voce": ["Premi incassati","Payout erogati","Costo oracle","Profitto netto","Saldo finale"],
            "Importo (ETH)": [premi_tot, payout_tot, reward_tot, profit_tot, saldo_fin],
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
        st.subheader("6 — Esploratore Giornaliero")

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
            lc, rc = st.columns(2)
            with lc:
                st.markdown("**Stato Pool**")
                net = float(row.get("net_flow_today", 0.0))
                st.dataframe(pd.DataFrame({
                    "Metrica": ["Saldo Pool (ETH)","Passività Pendenti (ETH)","Solvency Ratio",
                                "M_adj","Patt","Premi oggi (ETH)","Payout oggi (ETH)",
                                "Costo oracle oggi (ETH)","Flusso netto (ETH)"],
                    "Valore": [
                        f"{float(row['pool_balance_eth']):.4f}",
                        f"{float(row.get('pending_liabilities_eth',0)):.4f}",
                        f"{float(row['solvency_ratio']):.4f}",
                        f"{float(row.get('madj_current',0)):.2f}",
                        f"{float(row.get('patt_current',0)):.2%}",
                        f"{float(row.get('premiums_today',0)):.4f}",
                        f"{float(row.get('payouts_today',0)):.4f}",
                        f"{float(row.get('oracle_rewards_today',0)):.4f}",
                        f"{'+' if net>=0 else ''}{net:.4f}",
                    ]
                }), use_container_width=True, hide_index=True)

            with rc:
                st.markdown("**Attività del Giorno**")
                n_sw  = int(row.get("n_swaps_this_tick", 0))
                n_atk = int(row.get("n_attacks_this_tick", 0))
                n_ins = int(row.get("n_swaps_insured", n_sw))
                st.dataframe(pd.DataFrame({
                    "Metrica": ["Swap processati","  — attaccati","  — assicurati",
                                "Claim inviati","Claim approvati","Payout medio (ETH)"],
                    "Valore": [
                        str(n_sw),
                        f"{n_atk} ({n_atk/max(n_sw,1):.1%})",
                        str(n_ins),
                        str(int(row.get("n_claims_submitted",0))),
                        str(int(row.get("n_claims_approved",0))),
                        f"{float(row.get('avg_payout_eth',0)):.4f}",
                    ]
                }), use_container_width=True, hide_index=True)

            if first_collector:
                day_swaps = first_collector.daily_swap_details.get(selected_day, [])
                with st.expander(f"Swap del giorno {selected_day} ({len(day_swaps)})", expanded=False):
                    if day_swaps:
                        sw_df = pd.DataFrame(day_swaps)
                        for cn in ("value_ETH","premium_paid","payout_ETH"):
                            if cn in sw_df.columns:
                                sw_df[cn] = sw_df[cn].round(6)
                        st.dataframe(sw_df, use_container_width=True)
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
                ("Reward oracle/claim (ETH)", _p["oracle_reward_claim"]),
                ("N oracle", _p["n_oracles"]), ("Oracle per claim", _p["n_oracles_per_claim"]),
            ]
            if _p["mode"] == 2:
                rows += [
                    ("Patt manuale", f"{_p['patt_override']:.2%}"),
                    ("Swap/giorno", _p["swaps_per_day"]),
                    ("N utenti", _p["n_synthetic_users"]),
                    ("Max swap/utente/giorno", _p["max_daily_swaps"]),
                    ("Seed fisso", "Sì" if _p["seed_manual"] else "No"),
                    ("Seed", _p["rng_seed"] if _p["seed_manual"] else "auto"),
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
            for _, fr in first_df.iterrows():
                patt_d  = float(fr.get("patt_current", 0.05))
                madj_d  = float(fr.get("madj_current", 0.0))
                m_tot   = mbase + madj_d
                prem_ex = patt_d * loss_pct * (1 + m_tot) * _fcov_v
                rows_f.append({
                    "Giorno": int(fr["day"]), "Patt": f"{patt_d:.3%}",
                    "L%": f"{loss_pct:.2%}", "Mbase+Madj": f"{m_tot:.2f}",
                    "Fcov": f"{_fcov_v:.2f}", "P/V (1 ETH)": f"{prem_ex:.5f}",
                    "SR": f"{float(fr.get('solvency_ratio',0)):.4f}",
                })
            st.dataframe(pd.DataFrame(rows_f), use_container_width=True, hide_index=True, height=300)
            fonte("P = V × (Patt × L%) × (1+M) × Fcov")


# ==========================================================================
# TAB 2 — DATI INFURA
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
            "Tutti gli altri parametri (loss, premi) sono sintetici.")

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
    st.markdown("### Come Funziona il Fetch")
    st.markdown("""
| | Vecchio | Nuovo |
|---|---|---|
| Strategia | 1 call/blocco | 1 call per 500 blocchi |
| Chiamate per 2 giorni | ~13.300 | ~27 |
| Tempo | ~94 ore | < 60 s |

**Rilevamento sandwich:** tripla (frontrun, victim, backrun) stesso pool, blocchi consecutivi.
""")


# ==========================================================================
# TAB 3 — STRESS TEST
# ==========================================================================
with tab_stress:
    st.subheader("🔬 Stress Test — Analisi di Sensibilità")
    _st_mode = st.session_state.get("last_mode", 2)

    st.markdown("### 3a — Sweep Parametro Singolo")

    _sweep_params = {
        "Patt (tasso attacco %)":         ("market",     "attack_rate",         0.01, 0.30, 0.02, "percent"),
        "L% (perdita media per attacco)": ("market",     "loss_pct_mean",       0.05, 0.50, 0.05, "percent"),
        "Mbase (margine base)":           ("pool",       "mbase",               0.05, 0.50, 0.05, "percent"),
        "Saldo iniziale pool (ETH)":      ("pool",       "initial_balance_eth", 20,   500,  20,   "number"),
        "Durata simulazione (giorni)":    ("simulation", "duration_days",       10,   90,   10,   "int"),
    }

    _sw1, _sw2 = st.columns(2)
    with _sw1:
        _spl = st.selectbox("Parametro da variare", list(_sweep_params.keys()), key="stress_sweep_param")
    with _sw2:
        _sns = st.number_input("N passi", min_value=3, max_value=20, value=8, step=1, key="stress_sweep_steps")

    _sps, _spk, _spmi, _spma, _spst, _spty = _sweep_params[_spl]
    _c1, _c2 = st.columns(2)
    with _c1: _smin = st.number_input("Valore minimo", value=float(_spmi), key="stress_min")
    with _c2: _smax = st.number_input("Valore massimo", value=float(_spma), key="stress_max")

    if st.button("▶ Esegui Sweep", key="stress_sweep_run"):
        import numpy as _nps
        _svals = _nps.linspace(_smin, _smax, int(_sns))
        _sres  = []
        _sprog = st.progress(0, text="Avvio…")
        _bcfg  = _make_cfg(mode=_st_mode, seed_manual=True, rng_seed=rng_seed if seed_manual else int(_time_mod.time())%99999)
        _bseed = _bcfg["simulation"]["seed"]
        for _i, _v in enumerate(_svals):
            _sprog.progress(int(_i/len(_svals)*100), text=f"Step {_i+1}/{len(_svals)}: {_spl}={_v:.4f}")
            try:
                _c = copy.deepcopy(_bcfg)
                _c["simulation"]["seed"] = _bseed + _i
                _c.setdefault(_sps,{})[_spk] = int(_v) if _spty=="int" else float(_v)
                _, _, _s = run_single(_c, mode=_st_mode, coverage=coverage, db_path=_DB_PATH)
                _sres.append({_spl: round(float(_v),6), "Profitto (ETH)": round(_s["total_profit_eth"],4),
                               "SR finale": round(_s["final_solvency_ratio"],4), "Pool": "SÌ" if _s["pool_survived"] else "NO"})
            except Exception as ex:
                _sres.append({_spl: round(float(_v),6), "Profitto (ETH)": "ERR", "SR finale": "ERR", "Pool": str(ex)})
        _sprog.progress(100, text="Sweep completato!")
        _sdf = pd.DataFrame(_sres)
        st.dataframe(_sdf, use_container_width=True, hide_index=True)
        _fail = _sdf[_sdf["Pool"]=="NO"]
        if not _fail.empty:
            st.warning(f"⚠️ Breakpoint: pool fallisce per **{_spl} ≥ {_fail.iloc[0][_spl]}**")
        else:
            st.success("✅ Pool sopravvive per tutti i valori.")

    st.markdown("---")
    st.markdown("### 3b — Griglia 2D")

    _gpl = ["Patt (tasso attacco %)","L% (perdita media per attacco)","Mbase (margine base)","Saldo iniziale pool (ETH)"]
    _gc1, _gc2 = st.columns(2)
    with _gc1:
        _gpx = st.selectbox("Asse X", _gpl, index=0, key="stress_grid_x")
        _gxmi = st.number_input("X min", value=float(_sweep_params[_gpx][2]), key="stress_gx_min")
        _gxma = st.number_input("X max", value=float(_sweep_params[_gpx][3]), key="stress_gx_max")
        _gxs  = st.number_input("X passi", min_value=2, max_value=8, value=4, step=1, key="stress_gx_steps")
    with _gc2:
        _gpy = st.selectbox("Asse Y", _gpl, index=1, key="stress_grid_y")
        _gymi = st.number_input("Y min", value=float(_sweep_params[_gpy][2]), key="stress_gy_min")
        _gyma = st.number_input("Y max", value=float(_sweep_params[_gpy][3]), key="stress_gy_max")
        _gys  = st.number_input("Y passi", min_value=2, max_value=8, value=4, step=1, key="stress_gy_steps")

    _gm = st.selectbox("Metrica", ["Profitto (ETH)","SR finale"], key="stress_grid_metric")

    if st.button("▶ Esegui Griglia 2D", key="stress_grid_run"):
        import numpy as _npg
        _gxv = _npg.linspace(_gxmi, _gxma, int(_gxs))
        _gyv = _npg.linspace(_gymi, _gyma, int(_gys))
        _gd  = {}
        _gpxd = _sweep_params[_gpx]; _gpyd = _sweep_params[_gpy]
        _gprog = st.progress(0, text="Avvio…")
        _gtot  = len(_gxv)*len(_gyv); _gdn = 0
        _bgcfg = _make_cfg(mode=_st_mode, seed_manual=True, rng_seed=rng_seed if seed_manual else int(_time_mod.time())%99999)
        _bgseed = _bgcfg["simulation"]["seed"]
        for _xv in _gxv:
            _rd = {}
            for _yv in _gyv:
                _gdn += 1
                _gprog.progress(int(_gdn/_gtot*100), text=f"Step {_gdn}/{_gtot}")
                try:
                    _cg = copy.deepcopy(_bgcfg)
                    _cg["simulation"]["seed"] = _bgseed + _gdn
                    _cg.setdefault(_gpxd[0],{})[_gpxd[1]] = float(_xv)
                    _cg.setdefault(_gpyd[0],{})[_gpyd[1]] = float(_yv)
                    _, _, _sg = run_single(_cg, mode=_st_mode, coverage=coverage, db_path=_DB_PATH)
                    _rd[f"{_yv:.3f}"] = round({"Profitto (ETH)":_sg["total_profit_eth"],"SR finale":_sg["final_solvency_ratio"]}[_gm],4)
                except Exception: _rd[f"{_yv:.3f}"] = float("nan")
            _gd[f"{_xv:.3f}"] = _rd
        _gprog.progress(100, text="Griglia completata!")
        _gdf = pd.DataFrame(_gd).T
        _gdf.index.name = f"{_gpx} \\ {_gpy}"
        st.dataframe(_gdf.style.format("{:.4f}").background_gradient(cmap="RdYlGn", axis=None), use_container_width=True)

    st.markdown("---")
    st.markdown("### 3c — Scenari Preimpostati")

    _presets = {
        "🔴 Crisi Sistemica": {
            "desc": "Patt=25%, L%=40%, pool piccolo",
            "ov": {"market":{"attack_rate":0.25,"loss_pct_mean":0.40},"pool":{"initial_balance_eth":20.0}},
        },
        "🟡 Alta Frequenza": {
            "desc": "Patt=20%, volume elevato",
            "ov": {"market":{"attack_rate":0.20}},
        },
        "🟢 Mercato Stabile": {
            "desc": "Patt=1%, L%=5%, pool grande",
            "ov": {"market":{"attack_rate":0.01,"loss_pct_mean":0.05},"pool":{"initial_balance_eth":500.0}},
        },
        "🔵 Pool Minimo": {
            "desc": "Pool=20 ETH, Patt=10%, quanto dura?",
            "ov": {"pool":{"initial_balance_eth":20.0}},
        },
    }

    _pc = st.columns(4)
    for _pi, (_pn, _pd) in enumerate(_presets.items()):
        with _pc[_pi]:
            st.markdown(f"**{_pn}**"); st.caption(_pd["desc"])
            if st.button("▶ Esegui", key=f"preset_run_{_pi}"):
                try:
                    _cp = copy.deepcopy(_make_cfg())
                    for _s, _kv in _pd["ov"].items(): _cp.setdefault(_s,{}).update(_kv)
                    with st.spinner("…"):
                        _, _, _sp = run_single(_cp, mode=_st_mode, coverage=coverage, db_path=_DB_PATH)
                    st.metric("Profitto (ETH)", f"{_sp['total_profit_eth']:.4f}")
                    st.metric("SR finale",      f"{_sp['final_solvency_ratio']:.3f}")
                    st.metric("Pool",           "✓ SÌ" if _sp["pool_survived"] else "✗ NO")
                except Exception as ex: st.error(f"Errore: {ex}")


# ==========================================================================
# TAB 4 — ISTRUZIONI
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
| Oracle | Costo flat per verifica claim (nessun voto) |
| Pool | Raccoglie premi, paga rimborsi e costi oracle |
| MEV Bot | Esegue sandwich attack sulla vittima |

**Flusso:**
1. Utente paga premio P prima dello swap
2. Se attaccato → claim auto-approvato
3. Payout = loss_eth × Fcov_rimborso
4. Pool deduce costo oracle (flat fee per claim)
""")

    with st.expander("📐 Formula del Premio"):
        st.markdown("""
```
P = V × (Patt × L%) × (1 + M) × Fcov
```

- **Patt × L%** = costo puro del rischio (probabilità × perdita media)
- **M = Mbase + M_adj** = margine di profitto + aggiustamento dinamico
- **M_adj**: 0.00 (SR ≥ soglia alta) / 0.05 (SR ≥ soglia media) / 0.10 (SR < soglia media)
- **Fcov**: 0.70 Bassa / 0.90 Media / 1.00 Alta

**Rottura pool:**
- Solo `balance_eth < 0` è rottura
- SR è usato SOLO per modulare M_adj, non come condizione di stop
""")

    with st.expander("🔮 Rete Oracle"):
        st.markdown("""
Costo oracle semplificato — flat fee per claim approvato:

```
Costo giornaliero = n_claim × reward_per_claim × n_oracle_per_claim
```

Nessun voto, nessuna divergenza, nessuno slashing. Il costo è dedotto dal pool ogni giorno.
""")

    with st.expander("🎲 Randomicità e Riproducibilità"):
        st.markdown("""
- **Auto-seed**: `int(time.time()) % 99999` — ogni esecuzione è diversa
- **Seed fisso**: abilita il toggle in sidebar per risultati riproducibili
- **Patt giornaliero**: base × `rng.uniform(0.7, 1.3)` ogni giorno
- **Swap per utente**: `rng.poisson(swap_freq_mean)` ogni giorno
""")
