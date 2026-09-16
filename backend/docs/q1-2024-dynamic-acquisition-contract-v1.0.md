# Q1 2024 DYNAMIC OPTION EVIDENCE REACQUISITION RUNBOOK & ACCEPTANCE CONTRACT

Document ID: Q1-2024-DYNAMIC-ACQUISITION-CONTRACT-v1.0  
Policy ID: DYNAMIC-ACQUISITION-Q1-v1.0  
Status: FROZEN  
Authority: KAIRO Authority Line  
Governing Artifacts:
  - DYNAMIC-ENVELOPE-CONTRACT-v1.0 (Blob: c02a53c0ebb2c03b71658f7e5965d214465a8e42)
  - Conformance Suite (Blob: a9a654ee0a617bd3ef9aeb1768376f3e8d8d65b2)
  - Production Qualifier (Blob: 123bc3c9fef6a0db9a1d8909bff01517c7c3cb5b)
  - Databento Listing Provider (Blob: e51355c4c69c5b7aa09a6ce92a9ab4fde15c2e68)
  - Dynamic Option Evidence Slicer (Blob: 8a3b6c79fc2a4e3534602016d2aabb325d6cd536)
Predecessor Gate: DYNAMIC-ENVELOPE-PILOT-v1.0 (Commit 40be384, 100.00% Completeness, CLEAN)
Target Symbols: TQQQ, SQQQ  
Historical Window: 2024-01-02 to 2024-03-28 inclusive (61 Regular Trading Sessions)  
Corpus Denominator: Exactly 47,580 Regular-Session 1-Minute Interval Decisions  
Corpus Qualification Pass Floor: Completeness >= 95.00% (Maximum 2,379 Failures)  
Corpus Operational Target: Completeness >= 98.00% (Maximum 951 Failures)  
Scope: Historical Evidence Acquisition, Local Staging & Sealed Manifest Generation Only  
Prohibitions: Zero Strategy Execution | Zero Simulation | Zero P&L | Zero Capital Authority
================================================================================

1. GOVERNING PURPOSE & ROLE SEPARATION
This contract defines the operational execution, local storage topology, error recovery,
and cryptographic acceptance criteria for acquiring the complete Q1 2024 dynamic option
evidence corpus across TQQQ and SQQQ.

The reacquisition runner is strictly an operational orchestration pipeline:
- Expected Universe (Expected_t): Ingested causally from Databento OPRA.PILLAR 
  definitions using DatabentoListingProvider.
- Acquired Universe (Acquired_t): Acquired from ThetaData without quote cleansing 
  using ThetaDataHistoricalQuoteProvider.
- Ordinal Slicing: Executed at each completed minute bar via DynamicOptionEvidenceSlicer.
- Interval Qualification: Evaluated exclusively by ProductionDynamicEnvelopeQualifier.

The reacquisition runner contains ZERO independent expiration-selection, strike-selection, 
or qualification semantics. All envelope and expiration construction is delegated exclusively 
to the frozen DYNAMIC-ENVELOPE-CONTRACT-v1.0 and verified production slicer.

2. TEMPORAL BOUNDARIES & IMMUTABLE DENOMINATOR
The corpus denominator is fixed to the regular trading sessions of Q1 2024:
- Sessions (61 regular trading days):
  * January 2024 (21 sessions): 02, 03, 04, 05, 08, 09, 10, 11, 12, 16, 17, 18, 19, 22, 23, 24, 25, 26, 29, 30, 31 (Jan 15 closed: MLK Day)
  * February 2024 (20 sessions): 01, 02, 05, 06, 07, 08, 09, 12, 13, 14, 15, 16, 20, 21, 22, 23, 26, 27, 28, 29 (Feb 19 closed: Washington's Birthday)
  * March 2024 (20 sessions): 01, 04, 05, 06, 07, 08, 11, 12, 13, 14, 15, 18, 19, 20, 21, 22, 25, 26, 27, 28 (Mar 29 closed: Good Friday)

- Canonical Session Bar Invariance:
  Each session consists of exactly 390 canonical completed 1-minute underlying bars 
  derived from the sealed Q1 underlying bar dataset. For regular trading hours, these 
  correspond to the 390 completed-bar timestamps (from the bar completed at 09:31:00 ET 
  through the bar completed at 16:00:00 ET).
  
  Intervals per Symbol = 61 sessions * 390 bars = 23,790 intervals
  Corpus Denominator (N_expected) = 23,790 * 2 symbols = 47,580 intervals

Denominator Invariance Law:
N_expected is immutable. Missing snapshots, provider dropouts, transport timeouts, 
corrupted quotes, or absent bars CANNOT reduce or decrement the denominator.

3. ACCEPTANCE BENCHMARKS & DUAL VERDICTS
Corpus completeness is computed via deterministic decimal arithmetic:
  Completeness = (N_satisfied / 47,580) * 100

Two distinct decisions MUST be reported independently:

A. CORPUS QUALIFICATION VERDICT (Downstream Research Eligibility):
   - PASS: Completeness >= 95.00% (Satisfied >= 45,201; Failures <= 2,379)
     Authorizes generation of the sealed Q1 dynamic manifest and establishes eligibility
     for future Strategy 001 v2 simulation.
   - MARGINAL_REVIEW: 90.00% <= Completeness < 95.00% (42,822 <= Satisfied <= 45,200)
     Withholds simulation eligibility. Requires mandatory failure attribution review.
   - FAIL: Completeness < 90.00% (Satisfied < 42,822; Failures >= 4,759)
     Total corpus rejection.

B. OPERATIONAL TARGET VERDICT (Acquisition Quality Benchmark):
   - OPERATIONAL_TARGET_MET: Completeness >= 98.00% (Satisfied >= 46,629; Failures <= 951)
   - OPERATIONAL_TARGET_NOT_MET: Completeness < 98.00%
   If completeness lands in [95.00%, 97.99%], the corpus is qualified (PASS), but the 
   operational target is marked NOT_MET, triggering an advisory failure-distribution review.

4. IRRECOVERABILITY ABORT TRIGGER
The acquisition harness must track cumulative failed intervals across all evaluated cells:
  Cumulative Failures = N_evaluated - N_satisfied

Irrecoverability Rule:
The maximum allowable failures to achieve 95.00% completeness is 2,379.
Upon recording the 2,380th failed interval, perfect acquisition across all remaining 
intervals mathematically cannot achieve 95.00%. The runner must immediately invoke:
  EARLY_ABORT: IRRECOVERABLE_COMPLETENESS_DEFICIT
Execution stops, preserves all partial evidence and logs, and returns control to the 
Authority Line.

5. RETRY POLICY & TRANSPORT BOUNDS
Retries are permitted strictly for transient network and transport errors.

- Authorized Retryable Exceptions:
  * TCP connection drops / resets
  * HTTP transport timeouts (connect / read timeout)
  * Gateway errors (HTTP 502, 503, 504)
- Retry Schedule:
  * Maximum 3 retry attempts per transport operation.
  * Backoff delays: exactly 1.0s, 2.0s, 4.0s.
- Explicitly Prohibited:
  * NO retries for NaN quote observations.
  * NO retries for valid empty/zero-bid quote evidence.
  * NO retries for contract identity contradictions.
  * NO retries seeking alternate strikes, expirations, or nearby minutes.
Every retry attempt must increment the operational retry counter in the session receipt.

6. STORAGE ARCHITECTURE & SCRATCH TOPOLOGY
Evidence is staged under local scratch storage before canonical sealing:

scratch/q1_2024_dynamic/
├── definitions/
│   ├── TQQQ/
│   │   ├── TQQQ_definition_2024-01-02.dbn
│   │   └── ... (61 files)
│   └── SQQQ/
│       ├── SQQQ_definition_2024-01-02.dbn
│       └── ... (61 files)
├── intervals/
│   ├── symbol=TQQQ/
│   │   ├── date=20240102/
│   │   │   └── intervals.parquet
│   │   └── ... (61 partition directories)
│   └── symbol=SQQQ/
│       ├── date=20240102/
│       │   └── intervals.parquet
│       └── ... (61 partition directories)
├── receipts/
│   ├── daily/
│   │   ├── receipt_TQQQ_20240102.json
│   │   ├── receipt_SQQQ_20240102.json
│   │   └── ... (122 session-symbol receipts)
│   └── q1_2024_provisional_summary.json
└── state/
    └── checkpoint.json

7. IDEMPOTENCY & REPRODUCIBILITY CONTRACT
The runner must maintain both logical and physical reproducibility standards:

A. Logical Idempotency:
   Reprocessing identical interval evidence must produce an identical ordered row set 
   and an identical canonical semantic SHA-256 digest over the decoded interval records.

B. Physical Reproducibility:
   Parquet serialization must enforce frozen configuration settings:
   - Engine: PyArrow
   - Compression: SNAPPY
   - Schema: Strict typing (no dynamic schema inference)
   - Row Group Size: 390 intervals per row group (one session per row group)
   - Row Sorting: Lexicographically sorted by (completed_at ASC, expiration ASC, strike ASC, right ASC)
   Raw-byte Parquet file SHA-256 equality is evaluated against these pinned settings.

8. CANONICAL MANIFEST SPECIFICATION
Upon completion of all 122 cells, generate `MANIFEST.sha256` under `scratch/q1_2024_dynamic/`.

Format Specification:
- Record format: `<sha256><SPACE><byte_count><SPACE><relative_posix_path>\n`
- Path formatting: Relative to `scratch/q1_2024_dynamic/`, using POSIX forward slashes ("/")
- Ordering: Lexicographically sorted by relative POSIX path
- Encoding: UTF-8, strictly LF line endings, no BOM, trailing newline required
- Self-Exclusion: `MANIFEST.sha256` is strictly excluded from its own contents
- Root Digest: `manifest_root_sha256 = SHA256(exact UTF-8 bytes of MANIFEST.sha256)`

9. RESUMABILITY & STATE RECOVERY
Execution operates across 122 discrete cells (61 dates * 2 symbols).
After each cell completes, the runner commits `scratch/q1_2024_dynamic/state/checkpoint.json`:
- `last_completed_cell`: `"{symbol}_{date}"`
- `completed_cells_count`: integer (0 to 122)
- `cumulative_satisfied`: integer
- `cumulative_failed`: integer
- `cell_receipt_sha256`: SHA-256 digest of the completed daily receipt

On resume:
1. Verify all previously recorded cell receipts and Parquet files match their recorded SHA-256.
2. If any discrepancy is detected: STOP -> `RESUME_ABORTED_PROVENANCE_MISMATCH`.
3. If intact, resume execution from the first uncompleted cell.

10. PROVIDER RESOURCE BOUNDS
- Databento:
  * Expected files: Exactly 122 definition files (.dbn).
  * Expected size: ~5.38 MB total.
  * Incurred cost cap: <= $2.00 USD total.
- ThetaData:
  * Quote requests: ~1,998,360 contract-minutes across the quarter.
  * Concurrency cap: Maximum 10 concurrent requests.
  * Marginal cost: $0.00 (subscription-funded).

11. GOVERNANCE BOUNDARIES (HARD STOP)
- ZERO writes to Google Cloud Storage (bucket `kairo-market-artifacts-507516`).
- ZERO writes to Cloud SQL production databases.
- ZERO execution of Strategy 001 or Strategy 001 v2 trading logic.
- ZERO simulated order fills, P&L calculations, or capital sizing.
- ZERO live capital activity.
================================================================================