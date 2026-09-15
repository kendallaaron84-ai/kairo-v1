# KAIRO Strategy 001 Research Contract v1.0 — Q1 2024

Policy ID: RESEARCH-POLICY-v1.0  
Status: FROZEN  
Authority: KAIRO Authority Line  
Dataset: ab8036fc-87cb-5a5b-abaf-38c704f68ddd  
Qualification: FAIL — 41.58%  
Research Only — Zero Live Capital Authority  

---

## 1. Methodological Risk & Q1 Inference Boundaries

The canonical qualification manifest permanently records an `overall_qualification_verdict == "FAIL"` with an acquisition-envelope completeness score of 41.58% (3,931 satisfied / 9,454 decision intervals; 5,523 intervals marked `STRIKE_ENVELOPE_DEFICIT`).

To preserve epistemic integrity, simulation results must not be represented as an unconstrained representation of real-world Q1 performance. Research findings derived from this corpus are bound by four explicit limits:

* **Boundary 1: Localized Candidate Viability vs. Global Market Surface**  
  The 99.99% candidate availability diagnostic (9,453 / 9,454 decisions) establishes strictly that the static ingestion window captured at least one option satisfying the frozen candidate-selection rules. It does not establish that the entire cross-sectional volatility surface or the theoretical optimal strike was observed. All reporting must state: *"Performance conditional on the subset of options captured by the static window."*
* **Boundary 2: Asymmetric Selection Bias on Trend Days**  
  Because all 5,523 envelope deficits resulted from intraday price drift away from the morning anchor, the available strike menu grew systematically thinner on the trailing wing during sustained trends. Inferences regarding calls on large rally days or puts on large selloff days carry elevated selection bias and must be isolated in stability reporting.
* **Boundary 3: Canonical Missing Decision Handling**  
  Exactly one decision point out of 9,454 contains zero eligible candidates under the frozen candidate-selection rules. The simulation engine must handle this decision strictly via `CANDIDATE_UNAVAILABLE_SKIP`. No synthetic pricing or proxy interpolation is permitted.
* **Boundary 4: Absolute Prohibition of Live Extrapolation**  
  No outcome, positive expectancy, or stability score from this research run may be used to justify live capital allocation without re-evaluating the strategy against an uncompromised ($\ge 95\%$) dynamically centered acquisition corpus.

---

## 2. Signal Contract: `EMA-CROSS-001`

Signal generation operates exclusively on completed 1-minute bars of the underlying instruments (`TQQQ` and `SQQQ`) with zero forward-looking bias.

* **Underlying Streams:** Evaluated strictly on 1-minute consolidated bars (`UNDERLYING_SIGNAL_BARS`).
* **Indicators:** Dual Exponential Moving Averages calculated on bar close prices:
  * Fast EMA: $N_{\text{fast}} = 9$ bars
  * Slow EMA: $N_{\text{slow}} = 21$ bars
* **Signal Condition (Long Entry):**
  $$\text{Signal}_{\text{Bullish}}(t) \iff \text{EMA}_9(t-1) \le \text{EMA}_{21}(t-1) \land \text{EMA}_9(t) > \text{EMA}_{21}(t)$$
* **Signal Condition (Bearish / Exit Signal):**
  $$\text{Signal}_{\text{Bearish}}(t) \iff \text{EMA}_9(t-1) \ge \text{EMA}_{21}(t-1) \land \text{EMA}_9(t) < \text{EMA}_{21}(t)$$
* **Temporal Precedence:** A signal is observable only after bar $t$ completes (`bar.completed_at`). The earliest legal execution timestamp is $t+1$ (the opening quote/tick of the subsequent 1-minute interval).

---

## 3. Contract-Selection Contract

Upon confirmation of a Bullish signal at bar $t$, the contract resolver filters the snapshot chain at $t+1$ using strictly frozen Strategy 001 canonical predicates:

* **Target Expiration:** Front-weekly expiration ($0 \le \text{DTE} \le 5$). Zero-DTE options take strict precedence; otherwise, the nearest calendar expiration is selected.
* **Option Right:** Calls (`C`) are selected for bullish underlying crosses.
* **Strike Relationship:** First out-of-the-money (OTM) strike strictly greater than the underlying spot price at bar $t$ close:
  $$\text{Strike} > \text{Spot}_{\text{close}}(t)$$
* **Candidate-Selection Predicates:**
  1. $\text{Ask} \le \$0.50$ (Maximum entry premium: $\$50.00$ per contract).
  2. $\text{Bid} > \$0.00$ (Non-zero bid; zero-bid contracts disqualified).
  3. Spread Tolerance: $(\text{Ask} - \text{Bid}) \le \$0.03$.
  4. Liquidity Threshold: $\text{Volume} \ge 10 \lor \text{Open Interest} \ge 50$.
* **Deterministic Tie-Breaking:**
  1. Primary: Minimum absolute delta to $\$0.35$ target ask ($|\text{Ask} - 0.35|$).
  2. Secondary: Tightest absolute bid-ask spread ($\text{Ask} - \text{Bid}$).
  3. Tertiary: Highest Open Interest.
  4. Quaternary: Lexicographically lowest canonical instrument symbol ID.
* **Missing Candidate Behavior:**
  If zero candidates survive predicates, the resolver emits `CANDIDATE_UNAVAILABLE_SKIP`. Zero orders are placed; the signal is logged as non-executable.

---

## 4. Execution & Friction Contract

Mid-price fills and zero-fee executions are strictly prohibited.

* **Entry Pricing:** Fills strictly at the published **`Ask`** price of the selected contract on the $t+1$ opening tick.
* **Exit Pricing:** Fills strictly at the published **`Bid`** price on the exit bar.
* **Fill Latency:** 1-bar execution delay. Orders submitted at bar $t$ close are filled at $t+1$.
* **Broker Fee Model:**
  * Base Commission: $\$0.65$ per contract per side ($\$1.30$ round trip).
  * Regulatory / Exchange Pass-Through: $\$0.05$ per contract per side ($\$0.10$ round trip).
  * Minimum Ticket Charge: $\$1.00$ per order event (enforces a $\$2.00$ minimum round-trip floor on single-contract orders).
* **Adverse Fill & Slippage Stress Test:**
  The baseline engine executes at quoted Ask/Bid. Sensitivity testing applies a mandatory 1-tick ($\$0.01$) adverse penalty on both entry and exit:
  $$\text{Execution Drag} = (\text{Ask} - \text{Bid}) + \text{Commissions} + \text{Fees} + \text{Slippage Stress}$$

---

## 5. Trade Lifecycle & Risk Engine

Strategy 001 operates as a strictly intraday momentum system with zero overnight exposure.

* **Maximum Concurrent Positions:** Exactly 1 active contract position per underlying symbol. Pyramiding and scaling are prohibited. While a position is active, subsequent entry signals for that symbol are ignored.
* **Exit Rules (Evaluated in strict ordinal priority):**
  1. **Catastrophic Stop Loss ($SL$):** If current contract bid falls $\ge 20\%$ below entry ask, exit at market bid on that bar.
  2. **Profit Target ($PT$):** If current contract bid appreciates $\ge 20\%$ above entry ask, exit at market bid.
  3. **Signal Invalidation (Trend Reversal):** On the close of any bar where $\text{EMA}_9$ crosses below $\text{EMA}_{21}$, exit at market bid on the next bar ($t+1$).
  4. **Session Force-Close:** All positions open at 15:58 ET are forcibly liquidated at the market bid recorded at the close of the 15:58 ET bar. Zero positions are carried past 15:59 ET.

---

## 6. Two Decoupled Analysis Streams

The simulation engine outputs two strictly separated ledgers:

### Stream A: Pure Strategy Economics (Unconstrained Edge)
Evaluates whether Strategy 001 possesses positive expectancy independent of account scale:
* Sizing: Exactly 1 contract per trade.
* Capital Assumption: Infinite capital; zero trades skipped due to purchasing power.
* Reference Denominator: Performance and drawdown metrics are normalized against a fixed reference base of $\$1,000.00$.

### Stream B: Capital Feasibility (Tier Constraints)
Evaluates account survivability across six initial-capital tiers under a fixed 25% risk-allocation rule.

**Position Sizing Contract:**
For each eligible trade:
$$B_t = \text{account equity immediately before entry}$$
$$A_t = 0.25 \times B_t$$

Where $A_t$ is the maximum capital allocation for the position.

For a selected option with ask price $P_t$, the all-in cash requirement per contract is:
$$C_t = (P_t \times 100) + F_{\text{entry}}$$

Where $F_{\text{entry}}$ is the entry commission and transaction fee.

Position quantity is:
$$Q_t = \min\left( \left\lfloor \frac{A_t}{C_t} \right\rfloor, \left\lfloor \frac{\text{Available Cash}_t}{C_t} \right\rfloor \right)$$

Rounding is strictly downward to the nearest whole contract. Fractional contracts and rounding up are prohibited.

**Initial Tier Matrix:**

| Tier | Starting Capital ($B_0$) | Initial Allocation Ceiling ($A_0 = 0.25 B_0$) |
| :--- | :---: | :---: |
| **T1** | $\$100.00$ | $\$25.00$ |
| **T2** | $\$250.00$ | $\$62.50$ |
| **T3** | $\$500.00$ | $\$125.00$ |
| **T4** | $\$1,000.00$ | $\$250.00$ |
| **T5** | $\$1,500.00$ | $\$375.00$ |
| **T6** | $\$2,500.00$ | $\$625.00$ |

**Affordability Boundary:**
If $Q_t = 0$, the engine must not force a 1-contract position. The simulator emits `SKIPPED_INSUFFICIENT_FUNDS`. The trade remains in the Strategy Economics ledger, but is omitted from that capital tier's execution history. Allocation percentages are identical across all tiers; the search rules are never relaxed to accommodate smaller balances.

---

## 7. Frozen Success & Disqualification Benchmarks

| Metric | Minimum Passing Threshold | Disqualification Floor | Evaluation Scope |
| :--- | :---: | :---: | :--- |
| **Net Expectancy ($\mathbb{E}$)** | $\ge +\$1.50$ / trade | $\le \$0.00$ | After all fees and crossing costs |
| **Profit Factor ($PF$)** | $\ge 1.25$ | $< 1.05$ | Gross Profits / Gross Losses |
| **Sample Adequacy Floor ($N$)** | $\ge 150$ executed trades | $< 75$ executed trades | Minimum sample size requirement |
| **Maximum Drawdown ($MDD$)** | $\le 15.0\%$ of peak | $> 25.0\%$ of peak | Relative to $\$1,000$ base (Stream A) or peak tier equity (Stream B) |
| **Win Rate ($WR$)** | Informational | Informational | Evaluated via Net Expectancy |
| **1-Tick Stress Resilience** | $\mathbb{E}_{\text{stressed}} > \$0.00$ | $\mathbb{E}_{\text{stressed}} \le \$0.00$ | Robustness under adverse $\$0.01$ fill |

---

## 8. Cryptographic Governance

* **File Location:** `backend/docs/research-policy-v1.0.md`
* **Integrity Binding:** The simulation engine must ingest this Markdown document, compute its SHA-256 digest, and embed the digest into all generated simulation receipts and reports.
