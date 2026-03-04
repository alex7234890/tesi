# MEV Insurance Protocol Simulator

Simulatore Python di un protocollo assicurativo contro attacchi MEV (sandwich attack) su DEX Ethereum.
Il sistema supporta due modalità operative che condividono lo stesso motore di simulazione.

---

## Struttura del progetto

```
mev_insurance_sim/
├── config/
│   ├── base.yaml               # Tutti i parametri di default
│   ├── mode1_realchain.yaml    # Override per la modalità 1
│   └── mode2_synthetic.yaml    # Override per la modalità 2
├── datasources/
│   ├── base.py                 # Dataclass Swap + classe astratta
│   ├── blockchain.py           # Sorgente dati modalità 1 (SQLite)
│   └── synthetic.py            # Sorgente dati modalità 2 (generativa)
├── core/
│   ├── pool.py                 # Pool assicurativo (balance, SR, Madj)
│   ├── premium.py              # Formula del premio P
│   ├── fraud_detector.py       # FraudScore + decisione claim
│   ├── claim_processor.py      # Pipeline di processamento claim
│   ├── tier_manager.py         # Upgrade tier utenti (solo modalità 2)
│   └── oracle_network.py       # Rete oracle (watchlist, slashing)
├── analytics/
│   ├── collector.py            # Raccolta metriche per tick
│   ├── reporter.py             # Report testuale e CSV
│   └── charts.py               # Grafici matplotlib (PNG)
├── data/                       # Database SQLite + log + output CSV/PNG
├── scripts/
│   ├── download_blocks.py      # Scarica blocchi Ethereum via Infura
│   └── download_patt.py        # Calcola la serie storica di Patt
├── dashboard/
│   └── app.py                  # Dashboard interattiva Streamlit
├── runner.py                   # Entry point CLI
└── requirements.txt
```

---

## Installazione

```bash
cd mev_insurance_sim
pip install -r requirements.txt
```

> Requisito minimo: Python 3.10+

---

## Configurazione

Apri `config/base.yaml` e imposta la tua chiave Infura (necessaria solo per la Modalità 1):

```yaml
blockchain:
  infura_url: "wss://mainnet.infura.io/ws/v3/TUA_CHIAVE_QUI"
  blocks_to_fetch: 50000
```

Tutti gli altri parametri hanno valori di default funzionanti e possono essere modificati
direttamente nel file YAML oppure tramite le opzioni CLI o i cursori del dashboard.

---

## Modalità 1 — Blockchain reale + simulazione parziale

In questa modalità i dati di swap e sandwich attack provengono dalla blockchain Ethereum.
Il sistema di tier è **omesso** (scenario peggiore per il pool).

### 1. Scarica i dati blockchain

```bash
python scripts/download_blocks.py
```

Questo scarica gli ultimi `blocks_to_fetch` blocchi dalla mainnet Ethereum,
rileva i sandwich attack e salva tutto in `data/blockchain.db`.
Il download è riprendibile: i blocchi già scaricati vengono saltati.

```bash
# Override rapido di URL e numero blocchi
python scripts/download_blocks.py --infura-url wss://mainnet.infura.io/ws/v3/CHIAVE --blocks 20000
```

### 2. (Opzionale) Scarica la serie storica di Patt

```bash
python scripts/download_patt.py --days 180
```

Calcola il `Patt` giornaliero (tasso di sandwich attack) e lo salva in `patt_history`
nel database SQLite. Se la tabella `swaps` è già popolata, il calcolo avviene localmente
senza ulteriori chiamate di rete.

### 3. Esegui la simulazione

```bash
# Tutti e tre i livelli di copertura in sequenza (LOW / MEDIUM / HIGH)
python runner.py --mode 1 --coverage all

# Un solo livello
python runner.py --mode 1 --coverage high

# Con fraud rate personalizzato
python runner.py --mode 1 --coverage medium --fraud-rate 0.10

# Download fresco + simulazione in un solo comando
python runner.py --mode 1 --coverage all --download-fresh
```

Al termine il runner:
- stampa un riepilogo a terminale
- salva i risultati in `data/results_mode1_<coverage>.csv`
- genera i grafici PNG in `data/`
- avvia automaticamente il dashboard Streamlit

---

## Modalità 2 — Simulazione sintetica completa

Tutto è sintetico tranne il `Patt` (scaricato dalla blockchain).
Include il sistema completo di tier utenti: Bronze → Silver → Gold → Platinum.

### 1. (Opzionale) Scarica il Patt storico

Se si salta questo passo il simulatore usa un Patt di default pari a **1%**.

```bash
python scripts/download_patt.py --days 180
```

### 2. Esegui la simulazione

```bash
# Simulazione di 180 giorni con parametri di default
python runner.py --mode 2

# Config personalizzata
python runner.py --mode 2 --config config/mode2_synthetic.yaml

# Override parametri da CLI
python runner.py --mode 2 --fraud-rate 0.08 --oracle-dishonest-rate 0.15
```

Al termine vengono prodotti:
- `data/results_mode2_high.csv`
- `data/summary_mode2_high.json`
- Grafici PNG: `pool_health.png`, `cashflow.png`, `claims.png`, `users.png`, `oracles.png`
- Dashboard Streamlit avviato automaticamente

---

## Dashboard interattiva

Puoi avviare il dashboard in qualsiasi momento, anche senza aver eseguito runner.py:

```bash
streamlit run dashboard/app.py
```

Si aprirà nel browser (default: http://localhost:8501).

### Controlli disponibili nella sidebar

| Controllo | Descrizione |
|---|---|
| **Mode** | 1 = blockchain reale, 2 = sintetica |
| **Coverage** | low / medium / high / all (solo modalità 1) |
| **User Fraud Rate** | Percentuale di utenti fraudolenti (0–30%) |
| **Oracle Dishonest Rate** | Percentuale di oracle disonesti (0–30%) |
| **Mbase** | Margine base del premio (5–40%) |
| **Initial Pool Balance** | Bilancio iniziale del pool in ETH (10–1000) |
| **Insurance Rate** | % di swap assicurati (solo modalità 1, 10–100%) |
| **Duration** | Durata simulazione in giorni (30–365) |
| **▶ Run Simulation** | Avvia la simulazione con i parametri correnti |
| **📥 Export CSV** | Scarica i risultati come file CSV |

### Pannelli del dashboard

1. **Pool Health** — Balance ETH e Solvency Ratio nel tempo (con zone rosse/gialle/verdi)
2. **Cash Flow** — Premi, pagamenti, ricompense oracle e profitto cumulativo
3. **Claims Analysis** — Approval rate, distribuzione FraudScore, claim per decisione
4. **User Distribution** *(solo modalità 2)* — Evoluzione dei tier e utenti in blacklist
5. **Oracle Network** *(solo modalità 2)* — Watchlist, divergenza media, slashing

---

## Opzioni CLI complete

```
python runner.py [opzioni]

  --mode {1,2}                Modalità operativa (obbligatoria)
  --coverage {low,medium,high,all}
                              Livello di copertura, solo modalità 1 (default: high)
  --config PATH               File YAML di override della configurazione
  --fraud-rate FLOAT          Override del tasso di frode utente (es. 0.05)
  --oracle-dishonest-rate FLOAT
                              Override del tasso di oracle disonesti
  --download-fresh            Scarica dati blockchain prima di simulare
  --no-dashboard              Non avviare Streamlit al termine
  --db-path PATH              Percorso del database SQLite (default: data/blockchain.db)
```

---

## Output prodotti

Dopo ogni esecuzione trovi nella cartella `data/`:

| File | Contenuto |
|---|---|
| `results_mode<N>_<cov>.csv` | Metriche giornaliere complete (una riga per giorno) |
| `summary_mode<N>_<cov>.json` | Riepilogo finale (profitto, SR finale, sopravvivenza pool) |
| `pool_health.png` | Balance e Solvency Ratio nel tempo |
| `cashflow.png` | Flussi di cassa cumulativi |
| `claims.png` | Analisi dei claim |
| `users.png` | Distribuzione tier *(solo modalità 2)* |
| `oracles.png` | Rete oracle *(solo modalità 2)* |
| `simulation.log` | Log dettagliato di ogni evento significativo |

---

## Parametri chiave spiegati

| Parametro | Dove | Significato |
|---|---|---|
| `insurance_rate` | base.yaml → market | % di swap considerate assicurate (modalità 1) |
| `fraud_rate` | base.yaml → users | % di utenti fraudolenti |
| `false_negative_rate` (E) | fraud_detection | Tasso di frodi non rilevate (parametro E nella formula del premio) |
| `mbase` | pool | Margine base del premio |
| `initial_balance_eth` | pool | Liquidità iniziale del pool |
| `patt_oscillation_range` | market | Oscillazione ±% attorno al Patt reale (modalità 2) |
| `oracle_dishonest_rate` | fraud_detection | % di oracle disonesti nel network |
| `duration_days` | simulation | Durata della simulazione in giorni |

---

## Formula del premio (riferimento)

```
P = V × [(Patt × L%) + (Tint × E/(1-E)) / (Vbase × 1000)] × (1 + M) × Fcov
```

- **V** = valore dello swap in ETH
- **Patt** = tasso di sandwich attack
- **L%** = perdita media (fissa al 20% in modalità 1)
- **Tint** = frodi intercettate nelle ultime 24h (ETH)
- **E** = False Negative Rate
- **Vbase** = swap assicurati nelle ultime 24h
- **M** = Mbase + Madj (dinamico in base al Solvency Ratio)
- **Fcov** = 0.70 (low) | 0.90 (medium) | 1.00 (high)

---

## Riproducibilità

Tutti gli eventi casuali usano `numpy.random.default_rng(seed)`.
Il seed è configurabile in `base.yaml` → `simulation.seed` (default: 42).
