# Bounty for Matrix HQ (missing `prevs`)

Originally promised at 3 satoshi per completed head at ~6500 heads, depth 91136.

The published 20,000-sat table covers 2,163 unique missing event IDs. The rezzy
warning's 1,820 count is the number of local events that reference missing IDs;
multiple events can point to the same ID, and auth links add targets as well.

These 2,163 are the initial targets; fetching them can reveal more missing
ancestors. To claim one you must close its whole chain: every
`prev_events`/`auth_events` link of the target and of each ancestor,
recursively, until it reaches the base. Ancestors you find along the way are not
separate targets and are not paid separately.

## Terms

Payouts follow a mild power-law weighting (rank^-0.25) over 2,163 missing event
IDs, about 47 sats for the hardest-looking down to 7 sats (roughly 7:1). Targets
are ranked by fewest referencing events, then by the depth of the shallowest
referencing event (shallowest, i.e. oldest, first), then event ID. See
`<ROOM>/targets.csv` for every target's rank and prize. A target is awarded
once, to the first merged claim that makes its full trace resolvable in the
accumulated event pool.

If an edge cannot be resolved or followed (i.e., it's an event from another
room), you must prove or provide some evidence supporting this.

If the DAG is incomplete after some months, unclaimed prizes are not
redistributed; they return to the fund or roll into a later bounty.
