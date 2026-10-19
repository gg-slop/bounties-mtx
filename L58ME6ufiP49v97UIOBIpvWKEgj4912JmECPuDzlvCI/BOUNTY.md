# Backfill bounty: missing prev_events

`bounty/<ROOM>/targets.json` lists every event ID that
`res/merged-…-v12-d1-71756.jsonl` references (as `prev_events`/`auth_events`)
but does not contain: **2163 unique targets** (the 1820 in the rezzy warning
counts referencing events, and excludes some links; regenerate with
`scripts/bounty.py targets`).

## Claiming

1. Put your events as JSONL in `submissions/<ROOM>/<you>.jsonl`, where `<ROOM>`
   is the 43-character room ID of the bounty you are targeting; open a PR. The
   directory selects the bounty and its base DAG (`res/merged-<ROOM>-*`, deepest
   wins). To pin a specific base, add `submissions/<ROOM>/manifest.json` with
   `{"base": "merged-<ROOM>-…"}`.
2. CI verifies every event's ID equals its room-v12 reference hash (no trust
   needed), rejects conflicts with the base, and writes
   `deltas/<ROOM>/<digest>.delta.jsonl` (only events absent from base) plus
   `<digest>.claim.json` (closed targets, dangling links).
3. The raw submission is deleted from the branch; the raw file survives 14 days
   as a CI artifact. Fork PRs: a maintainer runs the same `build` command and
   pushes the delta.

## Closure

A target is _closed_ by a delta when the target is in the delta and every
`prev_events`/`auth_events` link of it and its in-delta ancestors resolves into
base ∪ delta. Partial traces earn nothing; the target stays open. A delta may
close many targets.

## Payout

- Budget: 20,000 sats (≈ the original 3 × 6,500). Targets are ranked hardest-
  looking first: fewest base events referencing them, then newest (deepest
  referencing event), then event ID. Both numbers are in `targets.json`. Rank
  _k_ of _N_ pays `budget · k^-0.25 / Σ j^-0.25`, rounded by largest remainder
  so the total is exact: about 47 sats for rank 1 down to 7 for rank 2163
  (roughly 7:1). This rewards a guess at difficulty, not a measure of it.
- Each target pays once, to the first **merged** delta that closes it;
  `bounty/<ROOM>/ledger.jsonl` (`tools/bounty.py merge`) records target,
  claimant, PR, delta, prize, status (`unpaid`→`paid`).
- Later deltas covering an already-awarded target pay nothing for it.
- The rank order is fixed and published before claims open, so it cannot depend
  on who claims what or when.
- The table is fixed once published. Prizes unclaimed at the deadline are not
  redistributed; they return to the fund or roll into a later bounty.
