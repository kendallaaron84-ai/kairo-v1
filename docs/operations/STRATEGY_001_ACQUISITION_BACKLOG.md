# Strategy 001 acquisition backlog

## Spot-relative `strike_range=10` window drift

Status: open; deliberately not fixed during qualification-policy v2.1 realignment.

The frozen Q1 acquisition sends `strike="*"` and `strike_range=10` to the provider.
The provider-selected 20-strike window can be centered using a reference spot that
differs from Strategy 001's underlying close at `signal_at`. Intraday movement can
therefore leave fewer than ten acquired strikes strictly below or strictly above
the actual decision spot even when twenty strikes were returned.

Future acquisition design must define and test a locally deterministic,
decision-spot-relative strike selection contract. It must separately preserve:

- venue listing deficits;
- provider delivery deficits;
- collected but Strategy 001-ineligible quotes, including zero bids; and
- exact provider/source provenance.

No Q1 reacquisition or reinterpretation is authorized by this backlog item.
