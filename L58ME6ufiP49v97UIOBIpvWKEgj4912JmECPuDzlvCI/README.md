# Bounty for Matrix HQ (missing `prevs`)

Originally promised at 3 satoshi per completed head at ~6500 heads, depth 91136.

Later simplified to 20K over 1820 incomplete heads at total depth 71756.

## Terms

Payouts follow a mild power-law weighting (rank^-0.25) over 2163 missing prevs,
about 47 sats for the hardest-looking down to 7 sats (roughly 7:1). Targets are
ranked by fewest references, then newest; see `bounty/<ROOM>/targets.json` for
every target's rank and prize. A target is paid once, to the first merged claim
that closes it.

If an edge cannot be resolved or followed (i.e., it's an event from another
room), you must prove or provide some evidence supporting this.

If the DAG is incomplete after some months, unclaimed prizes are not
redistributed; they return to the fund or roll into a later bounty.
