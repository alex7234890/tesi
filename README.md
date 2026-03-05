# MEV Insurance Simulator

## Requisiti

```
Python 3.10+
```

```bash
cd mev_insurance_sim
pip install -r requirements.txt
```

Dipendenze principali: `streamlit`, `pandas`, `numpy`, `plotly`, `web3`, `pyyaml`, `scipy`.

---

## Configurazione

**Mode 1 (dati reali)** — imposta la chiave Infura in `config/base.yaml`:

```yaml
blockchain:
  infura_url: "wss://mainnet.infura.io/ws/v3/TUA_CHIAVE_QUI"
```

**Mode 2 (sintetica)** — facoltativo: fornisci la serie storica di Patt eseguendo prima:

```bash
python scripts/download_patt.py
```

Il file viene salvato automaticamente nel database SQLite in `data/blockchain.db`.
Se omesso, il simulatore usa un Patt di default dell'1%.

---

## Avvio

```bash
streamlit run dashboard/app.py
```

Si aprirà nel browser all'indirizzo `http://localhost:8501`.

---

## Mode 1 — Real Data

Usa swap e sandwich attack reali scaricati dalla blockchain Ethereum via Infura.
Richiede l'esecuzione preliminare di `scripts/download_blocks.py` per popolare il database.
Non include il sistema di tier utenti (scenario peggiore per il pool).

---

## Mode 2 — Synthetic Data

Genera utenti, swap e attacchi interamente in modo sintetico con un Patt opzionalmente reale.
Include il sistema completo di tier (Bronze → Silver → Gold → Platinum) con upgrade progressivi.
Non richiede chiavi API né download preliminari.

---

## Output

Il CSV esportabile dalla dashboard (`📥 Export CSV`) contiene una riga per ogni giorno simulato con:

- `day` — giorno della simulazione
- `pool_balance_eth`, `solvency_ratio`, `madj_current` — stato del pool
- `premiums_today`, `payouts_today`, `oracle_rewards_today`, `net_flow_today` — flussi giornalieri
- `n_claims_submitted`, `n_claims_approved`, `n_claims_rejected` — statistiche claim
- `n_rejected_fraud_score_gt_80`, `n_rejected_pattern_invalid`, `n_rejected_captcha_failed` — dettaglio rigetti
