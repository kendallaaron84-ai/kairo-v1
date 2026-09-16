# KAIRO Dynamic Envelope 5-Day Pilot Execution & Acceptance Contract v1.0

**Policy ID:** `DYNAMIC-ENVELOPE-PILOT-v1.0`
**Status:** `FROZEN`
**Authority:** KAIRO Authority Line
**Purpose:** Bounded production validation of the dynamic cross-provider evidence-acquisition architecture
**Research Only:** Zero Strategy Execution | Zero P&L | Zero Live Capital Authority

---

## 1. Governing Purpose

This pilot determines whether the verified dynamic evidence architecture is sufficiently complete, deterministic, and operationally stable to justify a larger historical reacquisition.

The pilot evaluates **evidence acquisition fidelity only**.

It does not evaluate:

* Strategy 001;
* Strategy 001 v2;
* trading expectancy;
* profitability;
* capital sizing;
* live execution readiness.

The governing evidence flow is:

```text
Databento OPRA Definitions
        ↓
     Expected_t
        │
        │
ThetaData Historical NBBO
        ↓
     Acquired_t
        │
        └──────────┐
                   ▼
       DynamicOptionEvidenceSlicer
                   ↓
       Frozen DynamicEnvelopeQualifier
                   ↓
        Interval Qualification Evidence
                   ↓
          Five-Day Pilot Report
```

The independent-source boundary is mandatory:

**Databento establishes what should exist.**

**ThetaData establishes what was acquired.**

Neither source may redefine the other's evidence.

---

## 2. Frozen Governing Architecture

The pilot must execute against the already-verified production architecture without semantic modification.

Governing artifacts include:

### Dynamic Envelope Contract

`DYNAMIC-ENVELOPE-CONTRACT-v1.0`

Git blob:

`c02a53c0ebb2c03b71658f7e5965d214465a8e42`

### Frozen Qualification Conformance Suite

Git blob:

`a9a654ee0a617bd3ef9aeb1768376f3e8d8d65b2`

### Production Dynamic Qualifier

Git blob:

`123bc3c9fef6a0db9a1d8909bff01517c7c3cb5b`

### Databento Listing Provider

Production blob:

`e51355c4c69c5b7aa09a6ce92a9ab4fde15c2e68`

### Dynamic Option Evidence Slicer

Git blob:

`8a3b6c79fc2a4e3534602016d2aabb325d6cd536`

### ThetaData Historical Quote Provider

Verified implementation commit:

`bae984770321be3acfb76f01ce2c9447f4d4a3bb`

Any mismatch in these frozen identities before execution is a hard stop.

---

## 3. Pilot Scope

The pilot consists of exactly five regular U.S. trading sessions:

* January 2, 2024
* January 3, 2024
* January 4, 2024
* January 5, 2024
* January 8, 2024

Symbols:

* `TQQQ`
* `SQQQ`

Expected full regular-session interval count:

```text
390 intervals/session
× 5 sessions
× 2 symbols
= 3,900 expected intervals
```

Therefore:

$$
N_{\text{pilot expected}} = 3,900
$$

This denominator is frozen before acquisition.

No missing provider response, acquisition failure, application crash, timeout, malformed quote, or absent interval may reduce the denominator.

If the authoritative exchange calendar proves that one of the specified sessions did not contain 390 regular-session one-minute intervals, execution must STOP and report the calendar contradiction before scoring. The denominator must not be silently changed.

---

## 4. Interval Definition

Each expected interval is identified by:

```text
(symbol, regular-session completed_at)
```

For each interval \(t\):

$$
S_t = \text{sealed underlying close at completed\_at}(t)
$$

The underlying source must be the already-sealed canonical Q1 evidence.

No underlying bar may be reconstructed from Databento or ThetaData for purposes of this pilot.

Each interval must bind:

* symbol;
* session date;
* completed timestamp;
* sealed underlying close;
* underlying source artifact identity/hash.

---

## 5. Expected_t Construction

For every interval, `Expected_t` must be reconstructed from Databento `OPRA.PILLAR` instrument-definition evidence.

Permitted schema:

`definition`

Expected contract existence must remain independent of:

* ThetaData quote availability;
* ThetaData trade activity;
* volume;
* open interest;
* strategy eligibility.

Definition replay must include only events causally available at or before interval \(t\).

The provider-relative strike envelope remains:

$$
E_t = \{K_{j-10},\ldots,K_j,\ldots,K_{j+10}\}
$$

where \(K_j\) is the provider-listed ATM strike determined from \(S_t\).

The envelope therefore contains exactly:

* 10 immediately provider-listed strikes below ATM;
* ATM;
* 10 immediately provider-listed strikes above ATM;

when sufficient provider wings exist.

CALL and PUT contracts are required only where provider-listed.

---

## 6. Acquired_t Construction

ThetaData receives only contract identities derived from `Expected_t`.

ThetaData must not perform:

* contract discovery;
* expiration discovery;
* strike discovery;
* chain discovery;
* alternate-contract search.

For each requested contract, raw acquisition state must remain observable.

This includes:

* finite Bid/Ask;
* zero Bid;
* NaN;
* missing quote;
* inverted quote;
* negative quote;
* provider failure;
* identity contradiction.

No cleansing, interpolation, forward-fill, clamping, or substitution is permitted.

---

## 7. Interval Qualification

Each expected interval must be evaluated by the frozen production qualifier.

The interval result is exactly one of:

`ENVELOPE_SATISFIED`

or:

`DYNAMIC_ENVELOPE_DEFICIT`

with all applicable specific failure reasons.

Qualification failure is evidence and must remain in the corpus.

---

## 8. Frozen Pilot Denominator

The pilot denominator is:

$$
N_{\text{expected}} = 3,900
$$

Define:

$$
N_{\text{satisfied}}
=
\text{number of intervals with ENVELOPE\_SATISFIED}
$$

and:

$$
N_{\text{failed}}
=
N_{\text{expected}} - N_{\text{satisfied}}
$$

The following identity must hold:

$$
N_{\text{satisfied}} + N_{\text{failed}} = 3,900
$$

`evaluated_intervals` must be reported separately.

An unevaluated expected interval is **not satisfied**.

Therefore an interruption or unresolved provider failure cannot improve completeness.

---

## 9. Completeness Calculation

Pilot completeness is:

$$
\text{PilotCompleteness}
=
\frac{N_{\text{satisfied}}}{3,900}
\times 100
$$

Calculation must use deterministic decimal arithmetic.

Final reporting precision:

`0.01 percentage points`

using the same approved deterministic rounding semantics as the production qualifier.

The report must also preserve the exact numerator and denominator so the rounded percentage never becomes the sole authority.

Example:

```text
Satisfied: 3,830
Expected:  3,900
Raw ratio: 3830 / 3900
Reported completeness: 98.21%
```

---

## 10. Qualification Threshold vs. Pilot Promotion Threshold

Two distinct decisions must remain separate.

### Corpus Qualification

The frozen dynamic-envelope contract retains:

* `>=95.00%` → `PASS`
* `90.00%–94.99%` → `MARGINAL_REVIEW`
* `<90.00%` → `FAIL`

### Pilot Promotion

The pilot imposes the stricter requirement:

$$
\boxed{\text{PilotCompleteness} \ge 98.00\%}
$$

for promotion to larger historical reacquisition.

Therefore a result such as:

`96.75%`

may qualify as corpus `PASS` while still producing:

`PILOT_PROMOTION_WITHHELD`

This distinction must never be collapsed.

---

## 11. Promotion Decision

The final pilot decision is deterministic.

### PROMOTE

If:

$$
\text{PilotCompleteness} \ge 98.00\%
$$

and no unresolved integrity/provenance hard-stop condition exists:

`PILOT_PROMOTION: APPROVED`

This authorizes Authority Line consideration of a larger historical reacquisition.

It does **not** automatically authorize full Q1 acquisition.

### WITHHOLD

If:

$$
\text{PilotCompleteness} < 98.00\%
$$

then:

`PILOT_PROMOTION: WITHHELD`

No threshold relaxation is permitted after observing results.

The failure distribution must be reviewed before another acquisition is authorized.

---

## 12. Failure Counters

The pilot report must preserve distinct counts for at least:

* `expected_intervals`
* `evaluated_intervals`
* `satisfied_intervals`
* `failed_intervals`
* `PROVIDER_STRIKE_SET_UNAVAILABLE`
* `PROVIDER_WING_INSUFFICIENT`
* `EXPECTED_STRIKE_MISSING`
* `EXPECTED_CONTRACT_MISSING`
* `EXPECTED_QUOTE_MISSING`
* `QUOTE_INVERSION`
* `INVALID_ASK`
* `INVALID_BID`
* `TIMESTAMP_ALIGNMENT_FAILURE`

Failure categories must not be collapsed into a generic deficit total.

Because one interval may contain multiple defects:

$$
\sum \text{failure reason counters}
$$

is not required to equal:

$$
N_{\text{failed}}
$$

The report must preserve multi-cause attribution.

---

## 13. Provider Operational Counters

Separate from qualification reasons, report operational acquisition statistics including:

### Databento

* definition files requested;
* definition records retrieved;
* bytes retrieved;
* API/request failures;
* definition replay failures.

### ThetaData

* contract-minute requests attempted;
* records returned;
* finite Bid/Ask records;
* NaN records;
* no-data responses;
* identity contradictions;
* authentication failures;
* entitlement failures;
* provider/network failures.

Operational counters must not silently replace qualification counters.

---

## 14. Daily and Symbol-Level Reporting

Completeness must be reported at three levels:

### Overall Pilot

All 3,900 intervals.

### Per Symbol

* TQQQ: 1,950 expected intervals
* SQQQ: 1,950 expected intervals

### Per Session / Symbol

Each symbol/day:

`390 expected intervals`

This produces 10 symbol-session cells.

For each cell report:

* expected;
* evaluated;
* satisfied;
* failed;
* completeness;
* failure-reason distribution.

This prevents a strong day or symbol from hiding a concentrated acquisition defect elsewhere.

---

## 15. Provider Cost Accounting

Actual provider cost must be recorded as evidence, not estimated after the fact.

### Databento

Record:

* metadata-estimated cost before retrieval;
* actual query parameters;
* billable bytes;
* actual charged/billable cost where exposed;
* total bytes downloaded.

### ThetaData

Record the marginal pilot cost according to the actual subscription/account model.

If no per-request charge is exposed:

`ThetaData marginal API cost: $0.00 observed / subscription-funded`

or the closest factually supportable description.

Do not invent amortized subscription costs.

### Total

Report:

`Pilot incremental provider cost`

using only directly attributable incremental costs.

Infrastructure/storage costs should be reported separately if incurred.

---

## 16. Source Hashes & Provenance

Every downloaded Databento source artifact must be hashed before use.

For each source artifact record:

* provider;
* dataset;
* schema;
* symbol scope;
* requested time range;
* byte count;
* SHA-256;
* retrieval timestamp.

The pilot report must also bind:

* repository commit;
* frozen governing Git blobs;
* sealed underlying artifact hashes;
* Databento source hashes;
* Theta request identities;
* pilot date range;
* symbol set.

Theta responses need not be persisted solely to obtain a source-file hash; their request/result identities and interval evidence must instead be deterministically bound into pilot output.

---

## 17. Ephemeral Staging

Provider data must initially reside in an ephemeral pilot staging area outside canonical Stage 1–4 evidence.

Permitted:

* temporary local/provider files required to execute the pilot;
* resumability state;
* pilot diagnostic output.

Prohibited:

* mutation of the sealed Q1 dataset;
* insertion into existing Stage 1–4 lineage;
* Cloud SQL canonical evidence writes;
* capital/risk table writes;
* live-execution permissions.

Pilot evidence is provisional until Authority Line review.

---

## 18. Resumability

The five-day pilot may be operationally interrupted.

Resumption is permitted only when it does not change experimental semantics.

A resumability checkpoint must bind:

* pilot policy identity/hash;
* repository commit;
* frozen component identities;
* Databento source hashes;
* completed interval identities;
* completed interval result hashes;
* next unprocessed interval.

On resume:

1. verify all checkpoint identities;
2. verify previously completed interval results;
3. continue from the first unprocessed expected interval;
4. do not recompute successful intervals merely because later results are undesirable.

If provenance differs:

`RESUME_ABORTED_PROVENANCE_MISMATCH`

and STOP.

---

## 19. Idempotency

Every interval must have a deterministic identity based on:

```text
pilot policy
symbol
completed_at
underlying source identity
Expected_t identity
```

Reprocessing an identical interval against identical provider evidence must yield the identical qualification result.

Duplicate interval results must not increase:

* evaluated count;
* satisfied count;
* failed count.

---

## 20. Retry Policy

Retries are permitted only for operational transport failures and must be explicitly counted.

Retries must not alter:

* Expected_t;
* target timestamp;
* requested contracts;
* provider source;
* qualification rules.

No retry may:

* select another minute;
* shrink the envelope;
* omit failed contracts;
* recenter using later spot;
* substitute another expiration.

The final interval evidence must preserve whether retries occurred.

A provider returning legitimate NaN or no-data evidence is not automatically a transport failure and must not be repeatedly queried until a finite quote appears.

---

## 21. Hard-Stop Conditions

Immediately stop the pilot if:

1. Any frozen governing artifact identity differs.
2. Sealed underlying evidence cannot be verified.
3. Databento Expected_t becomes dependent on Theta observations.
4. Future definition events leak into earlier intervals.
5. Theta performs or requires contract discovery.
6. Contract identity contradiction occurs in a way the verified provider cannot safely represent.
7. Qualification logic requires modification.
8. Slicer logic requires modification.
9. Denominator cannot remain fixed.
10. Source hashes cannot be established.
11. Resumption provenance cannot be verified.
12. Execution attempts canonical GCS/Cloud SQL persistence.
13. Any Strategy 001/v2 or live-capital path is invoked.

On hard stop:

`PILOT_STATUS: ABORTED`

No promotion decision may be issued.

---

## 22. Prohibited Post-Hoc Changes

After the first pilot interval is evaluated, the following are frozen:

* dates;
* symbols;
* denominator;
* envelope width;
* provider sources;
* qualifier;
* failure definitions;
* retry semantics;
* completeness formula;
* 98.00% promotion threshold.

No parameter may be changed because of observed pilot performance.

Any future changed experiment requires a separately versioned pilot policy.

---

## 23. Acceptance Report

The final report must contain:

### Provenance

* Pilot policy ID/hash
* Git commit
* Frozen artifact identities
* Underlying evidence hashes
* Databento source hashes

### Acquisition

* Expected intervals
* Evaluated intervals
* Expected contracts
* Theta contracts requested
* Records returned
* finite / NaN / missing counts

### Qualification

* satisfied intervals
* failed intervals
* overall completeness
* corpus qualification verdict
* all failure counters

### Stability

* TQQQ completeness
* SQQQ completeness
* all 10 symbol-session cells
* concentrated failure patterns

### Operations

* Databento bytes/cost
* ThetaData marginal cost
* retries
* interruptions/resumes
* execution duration

### Decision

Exactly one:

`PILOT_PROMOTION: APPROVED`

`PILOT_PROMOTION: WITHHELD`

or:

`PILOT_STATUS: ABORTED`

---

## 24. Promotion Does Not Authorize Reacquisition

A successful pilot does not itself authorize:

* full Q1 dynamic reacquisition;
* additional quarters;
* Strategy 001 v2 simulation;
* strategy optimization;
* live capital.

A successful result establishes only:

> The dynamic acquisition architecture demonstrated sufficient bounded historical evidence fidelity to be considered for larger-scale acquisition.

A separate Authority Line decision is required for the next stage.

---

## 25. Final Governance Rule

The pilot exists to test the evidence machinery.

It must therefore be allowed to fail.

A score of:

`97.99%`

is below the frozen promotion threshold.

The system must report:

`PILOT_PROMOTION: WITHHELD`

without relaxing the threshold, changing the denominator, removing failed intervals, or modifying provider semantics.

The pilot succeeds methodologically when it tells the truth about acquisition fidelity—regardless of whether promotion is approved.
