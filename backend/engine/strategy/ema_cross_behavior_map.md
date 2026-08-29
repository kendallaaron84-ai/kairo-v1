# Strategy 001 — EMA Cross Prototype Behavior Map

## Audit status

This document is the Phase 3 Step 1 source-fidelity record for `EMA-CROSS-001`.
It describes behavior only. It does not authorize or implement strategy runtime,
historical replay, simulated feeds, broker integration, order execution, or Risk
Governor changes.

Authoritative source:

- Source file: `Financial-options-bot/tqqq-sqqq-options-bot/options_bot.py`
- Audited source length: 635 lines
- SHA-256: `75993b9fd059b145fcfb2ac313e6ffe277f78591ab75b35884f14072398f9c6d`
- Audit rule: executable source wins over its comments, README, tests, UI copy, and
  earlier Phase 3 descriptions whenever they disagree.

No `EMA-CROSS-001` row or strategy configuration is currently seeded in Kairo's
PostgreSQL `strategy_registry`. The registry model only supplies a generic JSONB
`configuration` field. The freeze-candidate configuration and provenance map below
therefore define what may be persisted after this behavior map is reviewed; Step 1
does not silently seed or activate it.

## Executive behavior summary

The prototype independently polls TQQQ and SQQQ every 15 seconds and constructs a
close-only one-minute series for each symbol. A signal compares each symbol's
completed one-minute close with its own EMA-9. It is not a fast-EMA/slow-EMA system.

- Price crossing from at-or-below EMA-9 to above EMA-9 produces a CALL candidate.
- Price crossing from at-or-above EMA-9 to below EMA-9 produces a PUT candidate.
- The mapping applies identically and independently to both TQQQ and SQQQ. The
  prototype does not reserve TQQQ for calls or SQQQ for puts.
- An entry uses a same-day expiration when listed, otherwise the nearest upcoming
  expiration.
- The chain is filtered by valid bid/ask, premium, spread, and liquidity before the
  eligible strike nearest spot is selected.
- Exits are +10% option-premium return, -5% option-premium return, price/EMA trend
  reversal, or forced flatten from 15:45 ET.
- The startup budget is 50% of settled cash divided into three equal sizing slots.
- Two consecutive realized losing closes make the new-entry halt sticky for the
  remainder of that process's session.

## 1. Market-data and bar construction

Source references: lines 75-77, 182-245, and 557-611.

### Inputs

| Behavior | Exact prototype rule |
|---|---|
| Underlyings | `TQQQ`, then `SQQQ`, in that iteration order. |
| Poll cadence | Sleep 15 seconds after each completed loop. Work time is added to the cadence, so polls are not guaranteed to align exactly to wall-clock quarter-minutes. |
| Quote request | Latest underlying price with `includeExtendedHours=True`. |
| Bar key | Eastern-time timestamp truncated to minute (`second=0`, `microsecond=0`). |
| Bar close | Last successfully observed price assigned to the old minute when the first successful poll in a new minute arrives. |
| Stored bar data | Closes only. Despite source comments saying OHLC, no open, high, low, volume, or quote-count fields are built. |
| Missing polls | A failed symbol poll is logged and skipped. There is no gap marker, synthetic close, or backfill. The previous `_last` can therefore become the eventual close after an observation gap. |

The first observation initializes the current minute and does not close a bar. A
minute is closed only on a later observation whose minute key differs. Signals and
trend reversals therefore use completed closes, not the currently forming minute.

### Legacy replay implication

Exact prototype replay would need to reproduce the ordered 15-second quote samples,
poll failures, minute-boundary arrival behavior, and loop drift. Replaying canonical
one-minute bars can reproduce the indicator formula but cannot prove exact parity
with the prototype's quote-sampling path. This is why a future legacy compatibility
mode and a higher-fidelity research mode must remain separate. Neither is built in
Step 1.

## 2. Indicator calculation and readiness

Source references: lines 182-240.

For period `N = 9`, the source computes:

```text
k = 2 / (N + 1) = 0.2

closes 1 through 8:
    EMA = unavailable

close 9:
    EMA_9 = arithmetic mean(close_1 ... close_9)

close t, t > 9:
    EMA_t = close_t * 0.2 + EMA_(t-1) * 0.8
```

The seed is exactly an SMA of the first nine completed closes. The source uses
Python binary floating-point, not `Decimal` arithmetic and not a broker-provided
indicator.

Readiness requires at least ten completed closes because the crossover test needs
both current and previous close/EMA pairs. At close 9, the current EMA exists but
the preceding EMA is `None`; the strategy is not ready. The first possible signal
is evaluated after close 10.

### Signal conditions

```text
bullish CALL:
    previous_close <= previous_ema
    and current_close > current_ema

bearish PUT:
    previous_close >= previous_ema
    and current_close < current_ema
```

Equality is permitted only on the previous side of the cross. The current close
must move strictly beyond the current EMA. If neither condition holds, the result
is no signal.

The same completed-bar cross remains visible during every 15-second loop until a
new minute closes. If an entry attempt fails, the prototype may retry that same
cross on subsequent polls. If a position opens and then closes before a new bar is
completed, the unchanged cross can permit same-minute re-entry.

### Trend-reversal exits

The exit check does not require a new cross:

- CALL: exit when current completed close is strictly below current EMA.
- PUT: exit when current completed close is strictly above current EMA.
- Equality does not trigger a reversal exit.

## 3. Directional and concurrency selection

Source references: lines 75, 221-231, and 588-597.

TQQQ and SQQQ each own separate close and EMA state. Each can independently emit a
CALL or PUT signal. There is no cross-symbol confirmation, inverse-ETF agreement,
ranking, regime filter, delta filter, portfolio direction netting, or rule that a
bullish market must use TQQQ while a bearish market must use SQQQ.

The loop evaluates TQQQ before SQQQ. With adequate capacity, both can enter during
the same loop. A symbol is skipped while any slot already holds that symbol, so the
prototype permits at most one open position per underlying.

Although `NUM_SLOTS = 3`, the two-symbol and one-position-per-symbol rules cap actual
simultaneous positions at two. The third slot affects sizing but cannot be occupied
under the current symbol list. This is executable behavior, not an inferred design
goal.

## 4. Expiration and option-contract selection

Source references: lines 252-293 and 391-433.

### Expiration

Expiration is resolved once per underlying during startup:

1. Read the chain's `expiration_dates`.
2. Use today's `YYYY-MM-DD` string if it appears exactly.
3. Otherwise sort dates greater than or equal to today and use the earliest.
4. Fail setup if no upcoming expiration exists.

This is `0DTE if available, otherwise nearest upcoming expiration`; it is not
strictly weekly, and it does not target a fixed days-to-expiration range. The
prototype uses `date.today()` for the comparison rather than deriving the date from
the configured Eastern timezone.

### Right and strike

The signal supplies the option right directly: bullish means CALL and bearish means
PUT. There is no delta target, strike offset, ITM/OTM rule, or Greek-based selection.

For the selected right and expiration, every returned contract is evaluated. A
contract is eligible only when:

- bid is greater than zero;
- ask is greater than zero;
- ask is no more than `$0.50` per share;
- ask minus bid is no more than `$0.03` per share; and
- volume is at least 10 **or** open interest is at least 50.

The last rule is OR logic in executable code: a contract is rejected only when both
volume is below 10 and open interest is below 50.

After filtering, the eligible contract minimizing
`abs(strike - underlying_spot)` is selected. A distance tie retains the first item
in the chain's returned order. The prototype does not define a deterministic
secondary tie-breaker.

## 5. Capital allocation and order sizing

Source references: lines 158-177, 407-409, and 546-551.

At startup:

```text
daily_budget = startup_settled_cash * 0.50
slot_size = daily_budget / 3
contracts = floor(slot_size / (eligible_contract_ask * 100))
```

An entry is skipped when the resulting contract quantity is below one. Fractional
option contracts are never requested.

The economic behavior to preserve in Kairo is 50% of verified settled cash divided
into three sizing allocations, followed by whole-contract floor sizing. Kairo must
replace the prototype's hard-coded `100` with the selected canonical instrument's
positive `contract_multiplier`:

```text
contracts = floor(slot_size / (entry_limit_price * contract_multiplier))
```

That replacement is mandatory Standard v0.1 conformance, not a research change to
the 50%/three-slot economics.

Important source nuances:

- Startup cash is selected from the first parseable value in
  `cash_available_for_withdrawal`, `cash`, then `buying_power`. Those fields are not
  economically interchangeable.
- `slot_size` is calculated once. A close may refresh the dashboard balance, but it
  does not recompute the session's daily budget or slot size.
- A closed slot is immediately reusable in local bookkeeping even when sale proceeds
  may be unsettled.
- The nominal three slots do not create three-position capacity with only two
  underlyings because duplicate-symbol positions are blocked.

## 6. Entry and exit order semantics

Source references: lines 314-348 and 391-514.

The prototype uses marketable limit orders, not market orders:

- Entry: buy-to-open debit limit at the observed ask, rounded to cents, GFD.
- Exit: sell-to-close credit limit at the observed bid, rounded to cents, GFD.
- Entry and exit submission are deliberately not automatically retried.

In paper mode, submission immediately returns a fabricated `filled` result. In live
mode, any non-raising library response is treated as success. The source does not
poll order status, confirm fills, account for partial fills, reconcile broker
inventory, or derive actual average execution price. A successful function return
immediately reserves or clears the local slot.

Exit checks run before new-entry checks on every loop. For a position with a valid
positive bid, exit priority is:

1. forced end-of-day flatten;
2. option-premium return greater than or equal to +10%;
3. option-premium return less than or equal to -5%;
4. underlying price/EMA trend reversal.

Return is measured from the stored entry ask to the current bid. P&L uses the stored
quantity and, in the prototype, another hard-coded multiplier of 100. Kairo must use
the canonical multiplier for both requested cash and realized P&L.

## 7. Loss-streak behavior

Source references: lines 466-480 and 588.

- A close with dollar P&L greater than or equal to zero is a win and resets the
  consecutive-loss count to zero.
- A close with negative dollar P&L increments the count.
- Reaching two consecutive losses sets `entries_halted = True`.
- The halt blocks new entries but does not block exit monitoring.
- The halt flag is sticky even if a later winning close resets the numeric streak.
- State is in process memory only. A restart resets wins, losses, P&L, streak, halt,
  slots, closes, and EMA state.

This is a strategy-level entry pause, not the Standard v0.1 Risk Governor's durable
session loss limit or system halt.

## 8. Session boundaries

Source references: lines 92-95 and 518-635.

| Time (America/New_York) | Exact behavior |
|---|---|
| Before 09:30 | Login happens first; the process then sleeps until 09:30. |
| 09:30 through 15:44:59 | Poll, exit, and entry checks are allowed. EMA warm-up starts only after the process begins observing prices. |
| At or after 15:45 | A process started this late exits before setup. A running process forces every validly quoted position toward exit and forbids new entries. |
| 15:45 through 15:59:59 | Failed quote or sell attempts leave positions open and are retried on later loops. |
| At or after 16:00 | The loop stops without another guaranteed flatten attempt. If forced exits failed before 16:00, positions can remain open. |

There is no exchange calendar, holiday check, half-day schedule, market-status
verification, opening delay beyond EMA warm-up, or daylight-saving logic beyond the
IANA `America/New_York` zone. Normal startup expiration uses the host's `date.today()`.

Keyboard interrupt and fatal-error handlers each attempt one forced exit pass.

## 9. Prior-review findings: independent verification

| Prior finding | Audit result | Source-accurate qualification |
|---|---|---|
| Price/EMA-9 crossover, not dual EMA | **CONFIRMED** | Close crosses its own EMA-9; no second EMA exists. |
| Bullish CALL, bearish PUT | **CONFIRMED** | Mapping is direct in `signal()`. |
| Both TQQQ and SQQQ can generate either right | **CONFIRMED** | Separate identical `SymbolState` instances; no symbol/right restriction. |
| SMA seed at bar 9; readiness at 10 closes | **CONFIRMED** | Bar 9 has no previous available EMA, so it cannot signal. |
| 0DTE else nearest upcoming expiration | **CONFIRMED** | Not specifically a weekly fallback. |
| Filter first, then nearest eligible strike | **CONFIRMED** | Liquidity is volume **or** open interest, not both. |
| +10%, -5%, reversal, 15:45 flatten | **CONFIRMED** | Forced flatten has first priority; trend reversal uses the underlying's completed close and EMA. |
| 50% settled cash across three slots | **CONFIRMED** | Budget and slot size are fixed at startup; actual concurrent capacity is two. Canonical multiplier must replace 100. |
| Two consecutive losses halt entries | **CONFIRMED** | Halt is sticky for the process session but is not restart-safe. |

## 10. Discrepancies from earlier Phase 3/UI representations

No separate frozen Phase 3 strategy specification exists in the repository. This
comparison uses the Step 1 specification supplied for this audit and the existing
cockpit mock representation as the observable earlier draft.

| Earlier representation or ambiguity | Source finding | Required correction before freeze |
|---|---|---|
| “EMA crossover” can read as dual EMA. | One price series crosses one EMA-9. | Name the signal `PRICE_EMA_9_CROSS`, not a fast/slow EMA cross. |
| UI warm-up target is 9 bars. | First possible signal requires 10 completed closes. | Display readiness as 10 completed closes; bar 9 only seeds EMA. |
| UI labels one-minute bars `TEST_DEFAULT`. | One-minute minute-key aggregation is explicit source behavior. | Tag it `INHERITED_PROTOTYPE`. |
| UI labels 15-second polling `RESEARCH_HYPOTHESIS`. | Fifteen seconds is an explicit source constant. | Tag it `INHERITED_PROTOTYPE`; reserve `RESEARCH_VARIANT` for a changed data path. |
| UI provenance vocabulary uses `TEST_DEFAULT` and `RESEARCH_HYPOTHESIS`. | Step 1 permits only `INHERITED_PROTOTYPE` and `RESEARCH_VARIANT`. | Registry provenance must use the two required values. UI changes wait for an approved map. |
| “Bullish 9-EMA crossover” is underspecified. | Underlying close crosses above its own current EMA-9. | Include price, EMA period, prior/current comparisons, symbol, and completed-bar basis. |
| Mock ledger labels the entry type `Market`. | Prototype submits a limit at the live ask. | Represent it as marketable LIMIT after execution semantics are authorized. |
| Three slots can imply three simultaneous trades. | Duplicate-underlying guard caps current two-symbol strategy at two. | Preserve both budget divisor 3 and max one position per underlying in legacy fidelity. |
| “ATM option” can imply select ATM then validate it. | Source filters the whole chain first and then selects nearest spot. | Preserve filter-first ordering. |
| “0DTE/weekly” can imply weekly fallback. | Source chooses any nearest upcoming listed expiration. | Do not require weekly expiration in inherited mode. |

## 11. Divergences from Kairo Standard v0.1

These are differences to contain at the platform boundary, not reasons to rewrite
the source history.

| Area | Prototype behavior | Kairo Standard v0.1 authority |
|---|---|---|
| Numeric authority | Python `float`; hard-coded 100 in sizing and P&L. | Decimal financial arithmetic and canonical positive instrument `contract_multiplier`. |
| Option identity | Symbol, expiration, strike, and lower-case call/put dictionaries. | First-class canonical option instrument identity, including contract symbol, underlying, expiration, strike, right, multiplier, and listing type. |
| Cash identity | Falls through settled-like cash, generic cash, then buying power. | Broker cash, settled cash, unsettled cash, and buying power remain distinct; sizing authority comes from linked settled-cash authorization. |
| Capital authority | Locally calculates 50% of startup cash and immediately reuses closed slots. | Latest canonical capital authorization and committed obligations constrain every risk-increasing intent. Unsettled proceeds cannot be presumed reusable. |
| Strategy clearance | Source boolean can be manually flipped to live. | Registry clearance is independent, canonical, and Governor-enforced. Initial Kairo clearance remains `PAPER_ONLY`. |
| Governor state | No deterministic pre-trade Governor. | Persistent Governor must be ARMED and independently approve risk-increasing intents. New sessions start DISARMED. |
| Session P&L controls | Only two-loss strategy pause; all counters are volatile. | Durable portfolio session net-P&L boundaries: hard loss at `-$6.00` and profit lock at `+$20.00`, plus immutable transitions. |
| Market-data freshness | Fifteen-second quote polling and no explicit freshness measurement. | Entry evaluation fails closed when the canonical quote age exceeds 1.5 seconds or timestamp is invalid. Legacy sampled replay cannot be presented as live-authorizable freshness. |
| Broker capability | Assumes the unofficial client and options capability. | Canonical broker/account/instrument capabilities must authorize trading, options, sizing mode, and any extended-hours request. |
| Order intent | Direct function call with local fields. | Immutable canonical intent includes purpose, side, exactly one sizing mode, order type/prices, cell, strategy version, and instrument lineage. Option entry sizing is quantity-based. |
| Order lifecycle | Treats non-raising submission as filled; no partial-fill/rejection reconciliation. | Orders, observations, fills, positions, and broker reconciliation are separate canonical facts. |
| Exit safety | Local slot is the inventory authority. | Exit quantity and identity are checked against canonical current positions and may never reverse or enlarge exposure. |
| Daily halt durability | Two-loss flag resets on restart. | Governor state and immutable events survive restart; a hard halt cannot be silently re-armed. |
| Session calendar | Fixed 09:30/15:45/16:00 wall times; no exchange calendar or early close. | Canonical session windows are explicit, timezone-aware facts. Market status must not be inferred solely from wall time. |
| Forced flatten | Failed exits can remain open at hard stop. | A command is not a fill. Kairo must retain and reconcile open exposure until confirmed flat; this audit does not implement that workflow. |
| Evidence lineage | CSV plus mutable in-memory statistics; no planned-risk, regime, MFE/MAE, or settlement lineage. | Immutable ledger and evidence manifests are required; missing evidence remains insufficient for Trust promotion. |
| Retry/idempotency | Reads retry; submissions intentionally do not. | Writes require canonical idempotency and observation/reconciliation semantics rather than blind retry or assumed failure. |

The 15-second sampler also conflicts with using it as the primary research dataset.
It belongs only to future `LEGACY_REPLAY_MODE`. Future `RESEARCH_REPLAY_MODE` must
use actual historical trades, quotes, or bars transformed into canonical market
events. Step 2 remains unimplemented.

## 12. Freeze-candidate `strategy_registry.configuration`

### Provenance rule

- `INHERITED_PROTOTYPE`: exact executable behavior or economic rule found in the
  audited source.
- `RESEARCH_VARIANT`: any Kairo-only choice or deliberate departure from source.
  The label means “not inherited”; it is not a validation or performance claim.

Standard-mandated platform controls such as canonical instrument multipliers,
capital authorization, immutable intent lineage, broker capability checks, and the
Risk Governor are not tunable strategy parameters and must not be weakened through
this configuration.

The compatible registry shape keeps `clearance` at the top level because the
existing Governor reads that key, while providing one provenance entry for every
configuration value:

```json
{
  "clearance": "PAPER_ONLY",
  "parameters": {
    "symbols": ["TQQQ", "SQQQ"],
    "signal_model": "PRICE_EMA_CROSS",
    "ema_period": 9,
    "ema_seed": "SMA_FIRST_N_CLOSES",
    "ema_smoothing_alpha": "2/(N+1)",
    "minimum_completed_closes": 10,
    "bar_interval_seconds": 60,
    "quote_poll_interval_seconds": 15,
    "quote_include_extended_hours": true,
    "bullish_option_right": "CALL",
    "bearish_option_right": "PUT",
    "expiration_policy": "TODAY_ELSE_NEAREST_UPCOMING",
    "premium_cap_per_share_usd": "0.50",
    "max_bid_ask_spread_per_share_usd": "0.03",
    "minimum_volume": 10,
    "minimum_open_interest": 50,
    "liquidity_threshold_logic": "VOLUME_OR_OPEN_INTEREST",
    "strike_selection": "NEAREST_SPOT_AFTER_FILTERS",
    "daily_budget_fraction_of_settled_cash": "0.50",
    "budget_slot_count": 3,
    "maximum_positions_per_underlying": 1,
    "entry_limit_reference": "ASK",
    "exit_limit_reference": "BID",
    "time_in_force": "GFD",
    "take_profit_fraction": "0.10",
    "stop_loss_fraction": "0.05",
    "trend_reversal_exit": true,
    "maximum_consecutive_losses": 2,
    "loss_streak_action": "HALT_NEW_ENTRIES_FOR_SESSION",
    "market_timezone": "America/New_York",
    "market_open_time": "09:30:00",
    "forced_flatten_time": "15:45:00",
    "hard_stop_time": "16:00:00",
    "balance_milestone_usd": "2000.00"
  },
  "parameter_provenance": {
    "clearance": "RESEARCH_VARIANT",
    "symbols": "INHERITED_PROTOTYPE",
    "signal_model": "INHERITED_PROTOTYPE",
    "ema_period": "INHERITED_PROTOTYPE",
    "ema_seed": "INHERITED_PROTOTYPE",
    "ema_smoothing_alpha": "INHERITED_PROTOTYPE",
    "minimum_completed_closes": "INHERITED_PROTOTYPE",
    "bar_interval_seconds": "INHERITED_PROTOTYPE",
    "quote_poll_interval_seconds": "INHERITED_PROTOTYPE",
    "quote_include_extended_hours": "INHERITED_PROTOTYPE",
    "bullish_option_right": "INHERITED_PROTOTYPE",
    "bearish_option_right": "INHERITED_PROTOTYPE",
    "expiration_policy": "INHERITED_PROTOTYPE",
    "premium_cap_per_share_usd": "INHERITED_PROTOTYPE",
    "max_bid_ask_spread_per_share_usd": "INHERITED_PROTOTYPE",
    "minimum_volume": "INHERITED_PROTOTYPE",
    "minimum_open_interest": "INHERITED_PROTOTYPE",
    "liquidity_threshold_logic": "INHERITED_PROTOTYPE",
    "strike_selection": "INHERITED_PROTOTYPE",
    "daily_budget_fraction_of_settled_cash": "INHERITED_PROTOTYPE",
    "budget_slot_count": "INHERITED_PROTOTYPE",
    "maximum_positions_per_underlying": "INHERITED_PROTOTYPE",
    "entry_limit_reference": "INHERITED_PROTOTYPE",
    "exit_limit_reference": "INHERITED_PROTOTYPE",
    "time_in_force": "INHERITED_PROTOTYPE",
    "take_profit_fraction": "INHERITED_PROTOTYPE",
    "stop_loss_fraction": "INHERITED_PROTOTYPE",
    "trend_reversal_exit": "INHERITED_PROTOTYPE",
    "maximum_consecutive_losses": "INHERITED_PROTOTYPE",
    "loss_streak_action": "INHERITED_PROTOTYPE",
    "market_timezone": "INHERITED_PROTOTYPE",
    "market_open_time": "INHERITED_PROTOTYPE",
    "forced_flatten_time": "INHERITED_PROTOTYPE",
    "hard_stop_time": "INHERITED_PROTOTYPE",
    "balance_milestone_usd": "INHERITED_PROTOTYPE"
  }
}
```

`clearance = PAPER_ONLY` is tagged `RESEARCH_VARIANT` because it is Kairo's initial
governance envelope rather than an immutable behavior of the prototype, whose
`PAPER_TRADING` flag can be manually changed. The balance milestone is inherited
but presentation-only; it must not affect signal, sizing, risk, or promotion.

Before persistence, acceptance must freeze the parameter names, the legacy behavior
semantics, and whether non-behavioral operational settings belong in this strategy
document or a separate runtime policy. No registry mutation occurs in Step 1.

## 13. Acceptance decisions required before Step 2

1. Freeze the signal name as `PRICE_EMA_CROSS` and readiness at 10 completed closes.
2. Freeze filter-first option selection and the volume-OR-open-interest rule.
3. Confirm legacy fidelity preserves both three-way budget division and the effective
   two-position concurrency cap.
4. Confirm Kairo uses canonical contract multipliers while preserving the inherited
   50% settled-cash / three-slot economics.
5. Decide whether exact legacy replay must reproduce 15-second samples and missing-poll
   behavior, rather than only consume resulting one-minute closes.
6. Decide how a future runtime handles the prototype's unresolved 16:00 open-position
   hazard without claiming that command emission equals execution.
7. Approve the registry configuration/provenance shape before any row is seeded.

Phase 3 Step 2 remains locked pending review and freeze of this map.
