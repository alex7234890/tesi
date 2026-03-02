Build a Python simulation system for an MEV Insurance protocol. 
The system has two operating modes that share the same core engine.

## PROJECT STRUCTURE

mev_insurance_sim/
├── config/
│   ├── base.yaml
│   ├── mode1_realchain.yaml
│   └── mode2_synthetic.yaml
├── datasources/
│   ├── __init__.py
│   ├── base.py
│   ├── blockchain.py
│   └── synthetic.py
├── core/
│   ├── __init__.py
│   ├── pool.py
│   ├── premium.py
│   ├── fraud_detector.py
│   ├── claim_processor.py
│   ├── tier_manager.py
│   └── oracle_network.py
├── analytics/
│   ├── __init__.py
│   ├── collector.py
│   ├── reporter.py
│   └── charts.py
├── data/
│   └── (SQLite database stored here)
├── scripts/
│   ├── download_blocks.py
│   └── download_patt.py
├── dashboard/
│   └── app.py
├── runner.py
├── requirements.txt
└── utils/
    ├── __init__.py
    ├── config_loader.py
    └── logger.py

---

## SHARED DATA MODEL

Every datasource must produce Swap objects with this structure:

@dataclass
class Swap:
    timestamp: int
    value_eth: float
    is_attacked: bool
    loss_eth: float          # 0 if not attacked
    coverage: str            # "low" | "medium" | "high"
    user_id: str             # wallet address (real or simulated)
    user_tier: str | None    # None in mode 1
    tx_hash: str             # real or simulated

---

## MODE 1 — Real Blockchain + Partial Simulation

Data source: historical Ethereum blocks fetched via Infura WebSocket.
Use mev-inspect-py logic (or equivalent) to identify sandwich attacks 
in historical blocks. Store results in SQLite for reuse.

Fetch the most recent blocks available efficiently — not live, 
but as recent as possible historically.

What is REAL in mode 1:
- Swaps (from real DEX transactions: Uniswap V2/V3, Sushiswap, Curve)
- Sandwich attacks (detected from block analysis)
- Patt (calculated from real data)
- Loss per attacked swap (calculated from real price impact)

What is SIMULATED in mode 1:
- Whether each swap is "insured" (random assignment based on 
  configurable insurance_rate parameter)
- Coverage level assigned to each insured swap 
  (three separate runs: all LOW, all MEDIUM, all HIGH)
- Fraud behavior: user_fraud_rate and oracle_dishonest_rate 
  are configurable parameters, not derived from chain data
- FraudScore calculation uses simulated parameters
- Oracle network behavior (stake, rewards, watchlist)

Tier system is OMITTED in mode 1 entirely. No Bronze/Silver/Gold/Platinum.
No coverage limits per user. Every insured swap gets 100% of its 
coverage level paid out if approved. This is the worst-case scenario 
for the pool — if it holds here, it holds with tier constraints too.

---

## MODE 2 — Full Synthetic + Real Patt

Everything is synthetic EXCEPT Patt, which is downloaded as a 
historical time series from the blockchain and stored in SQLite.
During simulation, Patt oscillates within a configurable range 
around the real historical values.

What is REAL in mode 2:
- Patt time series (pre-downloaded, used as oscillating baseline)

What is SIMULATED in mode 2:
- All swaps (generated from statistical distributions)
- All users with full tier system: Bronze, Silver, Gold, Platinum
- All coverage levels: Low (50% reimbursement), Medium (70%), High (100%)
- Fraud behavior for users and oracles (configurable rates)
- Oracle network with stake, watchlist, slashing
- All economic parameters oscillate in configurable ranges

---

## CONFIG SYSTEM

base.yaml must contain ALL parameters. Mode-specific yamls only override.

base.yaml structure:

simulation:
  seed: 42
  duration_days: 180
  time_step: "1d"
  n_runs: 1

blockchain:
  infura_url: "wss://mainnet.infura.io/ws/v3/YOUR_KEY"
  blocks_to_fetch: 50000
  dex_contracts:
    uniswap_v2: "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f"
    uniswap_v3: "0x1F98431c8aD98523631AE4a59f267346ea31F984"
    sushiswap: "0xC0AEe478e3658e2610c5F7A4A2E1777cE9e4f2Ac"

market:
  insurance_rate: 0.50         # mode 1: % of swaps considered insured
  loss_pct_mean: 0.20          # L% fixed at 20%
  loss_pct_std: 0.02
  patt_oscillation_range: 0.02 # mode 2: Patt oscillates ±2% around real

users:
  initial_count: 500
  growth_rate_daily: 0.02
  fraud_rate: 0.05
  fraud_strategy: "mixed"      # random | slow_burn | sybil | mixed
  swap_frequency_mean: 3
  coverage_distribution:       # mode 2 only
    low: 0.33
    medium: 0.34
    high: 0.33

pool:
  initial_balance_eth: 100.0
  mbase: 0.20
  solvency_thresholds:
    high_risk: 1.3
    medium_risk: 1.5
  madj:
    high_risk: 0.10
    medium_risk: 0.05
    healthy: 0.00

tiers:                         # mode 2 only
  bronze:
    max_capital_eth: 1.0
    max_daily_swaps: 3
    fraud_score_base: 50
  silver:
    max_capital_eth: 5.0
    max_daily_swaps: 3
    fraud_score_base: 30
  gold:
    max_capital_eth: 20.0
    max_daily_swaps: 4
    fraud_score_base: 15
  platinum:
    stake_pct: 0.20
    fraud_score_base: 0
  upgrades:
    bronze_to_silver:
      min_swaps: 18
      min_days: 30
      max_avg_fraud_score: 52
    silver_to_gold:
      min_swaps: 55
      min_days: 60
      max_avg_fraud_score: 35

fraud_detection:
  user_fraud_rate: 0.05
  oracle_dishonest_rate: 0.10
  false_negative_rate: 0.20    # E parameter in premium formula
  claim_rate_thresholds:
    very_suspicious: 0.30
    suspicious_high: 0.20
    suspicious_med: 0.10
    normal: 0.06
  fraud_score_decision:
    auto_approve: 60
    captcha_low: 60
    captcha_high: 80
    auto_reject: 80
  network_bfs_scores:
    distance_1: 50
    distance_2: 30
    distance_3: 15
    no_path: 0

oracles:
  initial_count: 20
  honest_rate: 0.90
  availability_rate: 0.95
  n_selected_per_claim: 5
  stake_min_eth: 2.0
  reward_per_claim_eth: 0.005
  reward_patt_update_eth: 0.002
  reward_captcha_eth: 0.001
  watchlist:
    entry_divergences: 2
    divergence_threshold: 10
    persistence_months: 3
  slashing:
    contestation_stake_eth: 0.1
    distribution:
      pool: 0.60
      reporter: 0.25
      jury: 0.15

---

## CORE ENGINE — Premium Formula

P = V × [(Patt × L%) + (Tint × E/(1-E)) / (Vbase × 1000)] × (1 + M) × Fcov

Where:
- V: swap value in ETH
- Patt: sandwich attack rate (real from blockchain)
- L%: average loss percentage (fixed at 0.20 in mode 1, configurable in mode 2)
- Tint: total fraud intercepted in last 24h (ETH)
- E: False Negative Rate (configurable parameter)
- Vbase: total insured swaps in last 24h (count)
- M: Mbase + Madj (dynamic margin based on Solvency Ratio)
- Fcov: 0.70 for Low, 0.90 for Medium, 1.00 for High

Solvency Ratio:
SR = pool_balance / (total_pending_claims + expected_claims_7d)

Expected claims 7 days:
expected_7d = payout_24h × (n_policies_now / n_policies_24h_ago) × 7

Madj:
- SR >= 1.5 → Madj = 0.00
- 1.3 <= SR < 1.5 → Madj = 0.05
- SR < 1.3 → Madj = 0.10

---

## CORE ENGINE — FraudScore

FraudScore = Score_Tier + Score_ClaimRate + Score_Network
Range: 0–130

Score_Tier (simulated, not from real chain):
- Bronze: +50, Silver: +30, Gold: +15, Platinum: +0
- Mode 1: always 0 (no tiers)

Score_ClaimRate:
- > 30%: +30
- > 20%: +25  
- > 10%: +20
- <= 10%: +15
- < 6%: +0

Score_Network (simulated BFS):
- Simulated based on user fraud probability
- If user is fraudulent: assign distance 1 or 2 randomly
- If user is honest: assign no_path or distance >= 4

Decision thresholds:
- Score < 60 → APPROVED (Gold/Platinum) or CAPTCHA (Bronze/Silver)
- 60 <= Score <= 80 → CAPTCHA required (all tiers)
- Score > 80 → REJECTED, user blacklisted

---

## CORE ENGINE — Oracle Network (mode 2 mainly)

- Oracle pool with configurable honest/dishonest ratio
- N oracles selected randomly per claim (RANDAO simulated)
- Honest oracles: FraudScore close to real value ± small noise
- Dishonest oracles: FraudScore systematically biased
- Median of submitted scores used for decision
- Divergence tracking per oracle
- Watchlist entry after 2 divergences >= 10 points
- Reward distribution based on divergence from median:
  - divergence < 15: receives reward
  - divergence >= 15: no reward
- Watchlist reward penalty formula:
  Reward = BaseReward × (0.50 + (100 - position) / 250)

---

## ANALYTICS — Metrics to collect every tick

Pool metrics:
- pool_balance_eth
- solvency_ratio
- total_premiums_collected_eth
- total_payouts_eth
- total_oracle_rewards_eth
- profit_eth (premiums - payouts - rewards)
- madj_current
- m_total_current

Claim metrics:
- n_claims_submitted
- n_claims_approved
- n_claims_rejected
- n_claims_captcha
- claim_approval_rate
- avg_payout_eth
- avg_fraud_score

User metrics (mode 2):
- n_users_bronze / silver / gold / platinum
- n_users_blacklisted
- n_upgrades_this_tick
- avg_claim_rate

Oracle metrics (mode 2):
- n_oracles_active
- n_oracles_watchlist
- n_oracles_slashed
- avg_oracle_divergence
- avg_oracle_reward_eth

Market metrics:
- patt_current
- n_swaps_this_tick
- n_swaps_insured
- n_attacks_this_tick
- avg_loss_eth

---

## SCRIPTS — Data Download

scripts/download_blocks.py:
- Connect to Infura via web3.py
- Fetch the most recent N blocks efficiently (use batch requests)
- For each block, extract all DEX swap transactions
  (filter by known Uniswap V2/V3, Sushiswap contract addresses)
- Detect sandwich patterns:
  same block + same pool + same token pair + 
  frontrun/victim/backrun sequence + different addresses
- Calculate real loss for victim transaction
- Store in SQLite: table "swaps", table "sandwich_attacks"
- Show progress bar during download
- Skip already-downloaded blocks (resumable)

scripts/download_patt.py:
- Fetch last 180 days of sandwich attack data efficiently
- Calculate daily Patt = sandwich_attacks / total_dex_swaps
- Store as time series in SQLite: table "patt_history"
- This is used by mode 2 as the real Patt baseline

SQLite schema:
CREATE TABLE swaps (
    block_number INTEGER,
    tx_hash TEXT PRIMARY KEY,
    timestamp INTEGER,
    dex TEXT,
    token_pair TEXT,
    value_eth REAL,
    is_attacked INTEGER,
    loss_eth REAL
);

CREATE TABLE sandwich_attacks (
    block_number INTEGER,
    frontrun_hash TEXT,
    victim_hash TEXT,
    backrun_hash TEXT,
    pool_address TEXT,
    attacker_address TEXT,
    victim_loss_eth REAL,
    timestamp INTEGER
);

CREATE TABLE patt_history (
    date TEXT PRIMARY KEY,
    patt REAL,
    total_swaps INTEGER,
    total_attacks INTEGER
);

---

## RUNNER

runner.py CLI interface:

# Mode 1 - run all three coverage levels sequentially
python runner.py --mode 1 --coverage all

# Mode 1 - single coverage level
python runner.py --mode 1 --coverage high --fraud-rate 0.05

# Mode 2 - full synthetic
python runner.py --mode 2

# With custom config override
python runner.py --mode 2 --config config/mode2_synthetic.yaml

# Download fresh blockchain data before running
python runner.py --mode 1 --coverage all --download-fresh

The runner must:
1. Load and merge config files
2. If mode 1: check SQLite has data, offer to download if missing
3. Instantiate correct datasource
4. Run simulation loop
5. Pass results to analytics
6. Launch Streamlit dashboard automatically after run

---

## DASHBOARD — Streamlit app

dashboard/app.py must include:

Sidebar controls:
- Mode selector (1 or 2)
- Coverage selector (Low / Medium / High / All)
- Key parameter sliders:
  - fraud_rate (0% to 30%)
  - oracle_dishonest_rate (0% to 30%)
  - mbase (5% to 40%)
  - initial_pool_balance (10 to 1000 ETH)
  - insurance_rate (10% to 100%) — mode 1 only
  - duration_days (30 to 365)
- "Run Simulation" button
- "Export CSV" button

Main panels:
1. Pool Health Over Time
   - Line chart: pool_balance, solvency_ratio over time
   - Color zones: red (SR<1.3), yellow (1.3-1.5), green (>1.5)
   
2. Cash Flow
   - Stacked area: premiums vs payouts vs oracle rewards
   - Running profit/loss line
   
3. Claims Analysis
   - Approval rate over time
   - FraudScore distribution histogram
   - Claim rate by tier (mode 2)
   
4. User Distribution (mode 2 only)
   - Tier distribution over time (stacked bar)
   - Blacklist growth over time
   
5. Oracle Network (mode 2 only)
   - Watchlist entries over time
   - Average divergence over time
   - Slashing events timeline
   
6. Key Metrics Summary (top of page)
   - Total profit ETH
   - Final solvency ratio
   - Claim approval rate
   - Pool survival: YES/NO (did SR ever drop below 1.0?)

Mode 1 specific: show three overlapping lines (Low/Medium/High) 
on pool balance chart when coverage=all is selected.

---

## IMPLEMENTATION NOTES

- Use dataclasses throughout for all data models
- Every random event must use numpy.random.default_rng(seed) 
  for full reproducibility
- All monetary values in ETH (float64)
- SQLite connection must be thread-safe
- Streamlit must re-run simulation when parameters change via sliders
- Log every significant event (claim approved/rejected, 
  oracle slashed, tier upgrade, pool stress) to a simulation.log file
- requirements.txt must include all dependencies with pinned versions:
  web3, pandas, numpy, matplotlib, streamlit, pyyaml, 
  scipy, sqlite3 (builtin), requests, tqdm

Do not use any placeholder or TODO comments. 
Implement everything fully and completely.
Start with the project structure, then implement file by file 
in dependency order (utils → config → datasources → core → 
analytics → dashboard → runner → scripts).