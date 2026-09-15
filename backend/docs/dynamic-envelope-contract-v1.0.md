# KAIRO Dynamic Option Envelope Acquisition & Qualification Contract v1.0
Document ID: DYNAMIC-ENVELOPE-CONTRACT-v1.0
Status: FROZEN
Authority Boundary: Acquisition & Evidence Qualification Only
Target Universe: TQQQ, SQQQ
Target Sessions: Regular Trading Hours
Target Expirations: Provider-listed option expirations satisfying 0 <= DTE <= 5
Target Acquisition Completeness: >= 95.00%
Strategy Neutrality: Required

1. Governing Principle
The dynamic acquisition system exists to capture an objectively complete, provider-grounded option surface around the underlying spot price at every completed 1-minute interval.
The acquisition envelope MUST NOT be designed around the preferences of Strategy 001, Strategy 001 v2, or any other downstream strategy.
Specifically, acquisition MUST NOT:
- favor CALL strikes over PUT strikes;
- favor OTM strikes over ITM strikes;
- favor particular premium ranges;
- filter on volume, open interest, spread, delta, or strategy eligibility;
- infer a universal numerical strike spacing;
- synthesize strikes that the provider does not list.

The acquisition system records evidence.
Downstream research systems determine which evidence is strategy-eligible.

2. Temporal Anchor
For every completed regular-session 1-minute underlying bar at timestamp t:
S_t = UnderlyingClose(t)
where S_t is the causally available underlying close at bar.completed_at.
The option-envelope snapshot for timestamp t:
- MUST be centered from S_t;
- MUST use only provider information available for the corresponding acquisition interval;
- MUST preserve UTC-aware timestamps;
- MUST bind to the underlying completed-bar timestamp.
No future underlying price may influence envelope construction.

3. Provider-Defined Strike Universe
For each underlying and active expiration at timestamp t, define:
K_provider,t
as the ordered set of unique strikes explicitly listed by the market-data provider for that expiration at that timestamp.
The provider strike set is authoritative.
KAIRO MUST NOT infer absent strikes from numerical gaps.
For example, the sequence:
50, 51, 52, 54, 56
does NOT automatically imply that strikes 53 and 55 are missing.
If the provider-defined listing universe says those contracts are not listed, their absence is not a continuity defect.
Conversely, if the provider listing metadata establishes that 53 is a listed strike but the acquisition payload omits it, that is a coverage defect.

4. ATM Resolution
For each provider-defined strike universe:
K_ATM,t = argmin_{K in K_provider,t} |K - S_t|
If two listed strikes are equidistant from spot, resolve deterministically to the lower numerical strike.
The chosen ATM strike MUST itself originate from the provider-defined strike universe.
No synthetic ATM strike may be created.
If no strike universe can be established:
PROVIDER_STRIKE_SET_UNAVAILABLE
and the interval fails qualification.

5. Single Canonical Strike-Coverage Rule
For every active expiration, the required acquisition envelope is:
ATM + the 10 immediately provider-listed strikes below ATM + the 10 immediately provider-listed strikes above ATM.

Define the ordered provider strike set:
K_provider,t = [K_0, K_1, ..., K_n]
and let:
K_j = K_ATM,t
Then the expected strike envelope is:
E_t = {K_{j-10}, ..., K_{j-1}, K_j, K_{j+1}, ..., K_{j+10}}

The canonical target therefore contains:
21 provider-listed strikes
whenever the provider listing universe contains at least 10 listed strikes on each side of ATM.
This is the only strike-coverage rule.
There is:
- no asymmetric CALL window;
- no asymmetric PUT window;
- no separate percentage-of-spot override;
- no assumed fixed strike increment;
- no strategy-specific premium window.

If the provider itself lists fewer than 10 strikes on either side of ATM, the interval records:
PROVIDER_WING_INSUFFICIENT
The acquisition engine MUST NOT manufacture additional strikes to satisfy the envelope.

6. CALL / PUT Neutrality
For every strike contained in E_t, KAIRO preserves all provider-listed option contracts for the active expiration, including both:
- CALL
- PUT
when each exists in the provider listing universe.
The acquisition contract does NOT require KAIRO to synthesize a CALL or PUT contract the provider does not list.
The required contract universe is therefore:
C_expected,t = {provider-listed contracts whose strike in E_t}

Qualification compares the acquired contract set against this provider-defined expected set.
Strategy-specific right selection occurs only downstream.

7. Expected-Set Coverage
An interval satisfies strike coverage only when the acquired strike set equals the required provider-defined strike set:
K_acquired,t >= E_t
Every required provider-listed strike must be represented.
The system MUST explicitly distinguish:
- Provider absence: The provider listing universe itself does not contain the strike. This is not automatically an acquisition defect.
- Acquisition absence: The provider listing universe contains the strike, but the acquired snapshot does not. This emits:
DYNAMIC_ENVELOPE_DEFICIT
No inferred numerical strike spacing may substitute for expected-set comparison.

8. Quote Integrity Contract
Quote integrity is evaluated independently from strike coverage.
A contract inside the required provider-defined envelope MUST preserve the provider-observed quote without cleansing, clamping, substitution, or interpolation.
For any acquired contract:
Bid >= 0, Ask > 0, Ask >= Bid

The following are explicit qualification failures:

8.1 Quote Inversion
If:
Ask < Bid
emit:
QUOTE_INVERSION
The offending quote MUST NOT simply be discarded and qualification continued using the remaining strikes. The interval fails.

8.2 Non-Positive Ask
If:
Ask <= 0
emit:
INVALID_ASK
The interval fails.

8.3 Negative Bid
If:
Bid < 0
emit:
INVALID_BID
The interval fails.
A negative Bid MUST NOT be clamped to zero.

8.4 Missing Required Quote
If a provider-listed expected contract exists but its required quote payload is missing:
EXPECTED_QUOTE_MISSING
The interval fails.

8.5 Zero Bid
A Bid of exactly:
Bid = 0
is valid evidence unless the provider contract or qualification specification separately declares otherwise.
Zero Bid MUST remain distinguishable from:
- missing Bid;
- negative Bid.
Acquisition qualification MUST NOT apply Strategy 001's Bid > 0 eligibility rule. That is a strategy predicate, not an evidence-integrity predicate.

9. No Silent Cleansing
The qualification engine MUST NOT:
- remove inverted quotes and continue;
- remove non-positive asks and continue;
- clamp negative prices;
- replace missing quotes with zero;
- synthesize missing contracts;
- interpolate missing strike quotes;
- infer missing volume or open interest as zero;
- discard records merely because they would fail a downstream strategy rule.
Evidence contradictions must remain visible and produce deterministic failure attribution.

10. Timestamp Integrity
Each dynamic option snapshot MUST bind to exactly one completed underlying 1-minute interval.
Required:
OptionSnapshot.timestamp = UnderlyingBar.completed_at
A mismatch emits:
TIMESTAMP_ALIGNMENT_FAILURE
and the interval fails qualification.
No nearest-neighbor timestamp substitution is permitted during qualification.

11. Interval Qualification State
Each interval produces exactly one top-level qualification result:
ENVELOPE_SATISFIED
Only if all of the following are true:
- provider strike universe successfully established;
- ATM resolved deterministically;
- provider contains at least 10 listed strikes below ATM;
- provider contains at least 10 listed strikes above ATM;
- all 21 required provider-listed strikes were acquired;
- all expected provider-listed contracts within that envelope are represented as required;
- zero required quote-integrity failures exist;
- option snapshot timestamp aligns with underlying completed_at.

Otherwise:
DYNAMIC_ENVELOPE_DEFICIT
with one or more explicit reason codes.

12. Canonical Failure Attribution
Permitted reason codes include:
- PROVIDER_STRIKE_SET_UNAVAILABLE
- PROVIDER_WING_INSUFFICIENT
- DYNAMIC_ENVELOPE_DEFICIT
- EXPECTED_STRIKE_MISSING
- EXPECTED_CONTRACT_MISSING
- EXPECTED_QUOTE_MISSING
- QUOTE_INVERSION
- INVALID_ASK
- INVALID_BID
- TIMESTAMP_ALIGNMENT_FAILURE

Failure attribution MUST be preserved in the interval qualification artifact.
Multiple causes MAY be recorded for one interval.
The top-level interval status remains fail-closed.

13. Quarterly Completeness Metric
For the complete research corpus:
Completeness = (N_ENVELOPE_SATISFIED / N_expected_regular_session_intervals) * 100

The denominator is determined from the authoritative regular-session trading calendar and expected underlying interval set.
Intervals MUST NOT disappear from the denominator merely because:
- provider retrieval failed;
- strike discovery failed;
- no option snapshot was returned;
- quote integrity failed.
Missing evidence counts against completeness.

14. Corpus Qualification Thresholds
- PASS: Completeness >= 95.00%
- MARGINAL_REVIEW: 90.00% <= Completeness < 95.00%
- FAIL: Completeness < 90.00%

A FAIL corpus cannot proceed to canonical strategy research requiring a qualified dynamic surface.
MARGINAL_REVIEW does not imply automatic strategy eligibility and requires separate Authority Line disposition.

15. Separate Integrity Counters
The qualification artifact MUST report at minimum:
- expected intervals;
- evaluated intervals;
- satisfied intervals;
- failed intervals;
- acquisition completeness percentage;
- provider-strike-set failures;
- provider-wing insufficiencies;
- expected-strike omissions;
- expected-contract omissions;
- missing quotes;
- quote inversions;
- invalid asks;
- invalid bids;
- timestamp alignment failures.

These counters MUST NOT be collapsed into one generic failure count.
The system must preserve enough attribution to distinguish:
acquisition breadth failure vs. provider listing limitation vs. quote integrity failure vs. temporal alignment failure.

16. Provider-Defined Expected Strike Set Requirement
The slicer implementation MUST consume or derive an authoritative provider listing set independently from the acquired filtered payload.
The expected strike set MUST NOT be constructed from the same already-filtered records being evaluated for completeness.
Otherwise, missing acquisition records would disappear from both the observed set and the expected set, making completeness self-validating.

Therefore qualification requires two conceptually distinct inputs:
- Expected universe: Provider-listed strike/contract identity set.
- Acquired universe: Contracts actually captured by the dynamic acquisition process.

Qualification compares:
Expected_t vs. Acquired_t
This separation is mandatory.

17. Maker / Checker Qualification Gate
The qualification test suite MUST be written and frozen before implementation of the dynamic slicer.
The test harness must independently establish:
- ATM resolution;
- lower-strike rank coverage;
- upper-strike rank coverage;
- provider-defined expected-set comparison;
- missing-listed-strike detection;
- provider-wing insufficiency handling;
- CALL/PUT neutrality;
- explicit quote inversion failure;
- invalid Ask failure;
- invalid Bid failure;
- expected quote absence;
- timestamp alignment;
- exact PASS boundary at 95.00%;
- exact marginal boundary at 90.00%;
- FAIL behavior below 90.00%;
- missing intervals remaining in the corpus denominator.

The slicer must then satisfy the independently frozen qualification contract.
The qualification implementation must not simply reuse the slicer's internal determination of what constitutes a complete envelope.

18. Pilot Gate
Before acquisition of an entire quarter:
Run a bounded multi-day pilot.
Recommended pilot:
5 consecutive trading sessions

Pilot requirements:
- dynamic centering at every expected regular-session minute;
- complete provider-listing comparison;
- zero silent integrity cleansing;
- deterministic receipt generation.

Pilot promotion threshold:
Completeness >= 98.00%
The 98% pilot threshold is deliberately higher than the final 95% corpus threshold to establish operating margin before full acquisition.
If the pilot fails:
STOP.
Do not acquire the full quarter until the acquisition defect is understood and corrected.

19. Strategy Independence
This acquisition contract remains valid if:
- Strategy 001 v2 fails;
- Strategy 001 is retired;
- a PUT strategy is later introduced;
- premium constraints change;
- EMA parameters change;
- strike-selection rules change.

That is intentional.
The evidence layer exists to support multiple downstream hypotheses without having to reacquire history merely because a strategy changes.

20. Governance Determination
The dynamic option acquisition pipeline succeeds only if it demonstrates:
The evidence captured what the provider actually listed around contemporaneous spot, with sufficient breadth, temporal alignment, and quote integrity—independently of whether any particular strategy likes those contracts.

The qualification gate evaluates the acquisition system.
It does not evaluate trading profitability.
Capital authorization remains outside this contract.