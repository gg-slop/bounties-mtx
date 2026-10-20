# Backfill bounty: missing prev_events

`<ROOM>/targets.csv` lists every event ID that
`res/<ROOM>/merged-…-v12-d1-71756.jsonl` references (as
`prev_events`/`auth_events`) but does not contain: **2,163 unique targets**. The
1,820 in the rezzy warning counts local events that reference missing
`prev_events`; several events can reference the same ID, and `auth_events` links
contribute additional targets. The fixed table was generated with
`scripts/bounty.py targets L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI`.

The 2,163 targets are the _initial_ missing IDs, not the whole of the missing
history. Fetching a target event usually reveals further
`prev_events`/`auth_events` that are also missing (newly revealed ancestor
gaps), and those can reveal more. None of those deeper ancestors is a target of
its own and none is paid separately; they are the work needed to close the
target that leads to them.

## Claiming

1. Encode your events as per-line base64 and put the file in
   `<ROOM>/submissions/<you>.00.jsonl.b64`, where `<ROOM>` is the 43-character
   room ID of the bounty you are targeting
   (`scripts/bounty.py encode <ROOM>/submissions/<you>.jsonl`, then commit only
   the `.b64`; plain `.jsonl` is git-ignored and must never be committed). One
   file per claim. Open a PR. The directory selects the bounty, and its
   published `targets.csv` selects the authoritative base DAG. A submission
   manifest may provide a payout address but cannot select a different base.
2. CI verifies every event's ID equals its room-v12 reference hash (no trust
   needed), rejects conflicts with the base, and writes
   `<ROOM>/deltas/<digest>.delta.jsonl.b64` (only events absent from base, also
   base64) plus `<digest>.claim.json` (closed targets, dangling links).
3. For same-repository PRs, CI replaces the encoded submission with its delta
   and keeps a copy of the submission as a 14-day artifact. For fork PRs, the
   source branch retains the encoded submission; a maintainer runs
   `scripts/bounty.py build` and adds the resulting delta.

Base64 is an encoding, not privacy: anyone can decode it.

## Closure

A target is _closed_ when it and every `prev_events`/`auth_events` link of it
and its ancestors resolve into the base plus the accumulated verified deltas. A
later claim can complete an earlier partial trace by supplying a missing
ancestor; the award goes to the claim that first makes the full trace
resolvable. Partial traces earn nothing until closure. One claim can close many
targets.

Closure is recursive: you must follow every link of the target, of its
ancestors, and so on, until each chain reaches an event already in the base. A
target whose event you hold, but whose ancestors are not all resolved, is still
open. Shared ancestors count once and can be supplied by any claim, but the
prize goes only to the claim that completes the target.

## Payout

- Budget: 20,000 sats (≈ the original 3 × 6,500). Targets are ranked hardest-
  looking first: fewest base events referencing them, then the depth of the
  shallowest referencing event (shallowest first), then event ID. Both numbers
  are in `targets.csv`. Rank _k_ of _N_ pays `budget · k^-0.25 / Σ j^-0.25`,
  rounded by largest remainder so the total is exact: about 47 sats for rank 1
  down to 7 for rank 2163 (roughly 7:1). This rewards a guess at difficulty, not
  a measure of it.
- Each target pays once, to the first **merged** delta that closes it;
  `<ROOM>/ledger.jsonl` (`scripts/bounty.py merge`) records target, claimant,
  PR, delta, prize, and unpaid status.
- Later deltas covering an already-awarded target pay nothing for it.
- The rank order is fixed and published before claims open, so it cannot depend
  on who claims what or when.
- The table is fixed once published. Prizes unclaimed at the deadline are not
  redistributed; they return to the fund or roll into a later bounty.
- Claims must use the exact base DAG and fingerprint recorded in the published
  target table. `scripts/bounty.py targets ROOM` refuses to change a published
  table's ranks or prizes.
- Payouts: claimants list a Bitcoin address as `"payout"` in their
  `manifest.json`. `scripts/bounty.py payout <ROOM> --min 5000` aggregates
  unpaid prizes by address, then applies the minimum, and prints the unsigned
  `bitcoin-cli walletcreatefundedpsbt` command to run manually with your own
  node. After broadcasting, run `scripts/bounty.py paid <ROOM> <TXID>` to mark
  the rows in that payout paid. A pending batch must be recorded as paid before
  creating another; use `--replace` only to discard a batch that was not
  broadcast. Nothing is signed or broadcast by this repo. Address totals under
  `--min` carry over (on-chain outputs below ~300–550 sats are dust).
