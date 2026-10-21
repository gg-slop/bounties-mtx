# Backfill bounty: missing prev_events

`<ROOM>/targets.csv` lists every event ID that
`res/<ROOM>/merged-…-v12-d1-71756.jsonl` references (as
`prev_events`/`auth_events`) but does not contain: **2,163 unique targets**. The
1,820 in the rezzy warning counts local events that reference missing
`prev_events`; several events can reference the same ID, and `auth_events` links
contribute additional targets. The fixed table was generated with
`scripts/bounty.py targets L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI`.

The 2,163 targets are the _initial_ missing IDs referenced by the fixed base,
not the whole of the missing history. Fetching a target event can reveal further
missing `prev_events`/`auth_events` (newly revealed ancestor gaps), and those
can reveal more. Ancestor gaps are not added as new targets or paid just for
being ancestors. If an ancestor is also one of the published initial targets, it
remains eligible for the award in its own row.

## Claiming

For each room, one PR adds exactly one submission file, plus that room's
`submissions/manifest.json`. `<ROOM>` is the base-64-url room ID; the directory
selects the bounty.

1. Get the base DAG: `git submodule update --init`, then
   `python3 scripts/bounty.py decode` (writes plaintext to `res/<ROOM>/`, which
   is git-ignored).
2. Find the missing IDs: `rezzy inspect gaps -i res/<ROOM>/merged-….jsonl`. The
   targets and prizes are in `<ROOM>/targets.csv`.
3. Put the events you recovered, one JSON event per line, in
   `<ROOM>/submissions/<username>.jsonl`, then
   `python3 scripts/bounty.py encode <ROOM>/submissions/<username>.jsonl`.
   `<username>` is a unique name using only letters, digits, `-` and `_`: your
   GitHub username, or for a Matrix ID the localpart without `@` or the server.
   Add `-2` for a later claim. The `.00` that `encode` appends is a part number,
   not a claim counter.
4. Commit **only** `<ROOM>/submissions/<username>.00.jsonl.b64` and
   `<ROOM>/submissions/manifest.json`, then open the PR. Never commit plaintext
   `.jsonl`. Base64 is an encoding, not privacy: anyone can decode it.
5. The manifest is `{"payout": "bc1q…"}`. No address, no payout. It may also pin
   the base as `{"base": "merged-<ROOM>-…"}`, but it cannot select a different
   one: the published `targets.csv` fixes the authoritative base DAG, and claims
   against another base are rejected.

What CI then does:

- CI verifies every event's ID equals its room-v12 reference hash (no trust
  needed), rejects conflicts with the base, and writes
  `<ROOM>/deltas/<digest>.delta.jsonl.b64` (only events absent from base, also
  base64) plus `<digest>.claim.json` (closed targets, dangling links).
- For same-repository PRs, CI replaces the encoded submission with its delta and
  keeps a copy of the submission as a 14-day artifact. For fork PRs, the source
  branch retains the encoded submission; a maintainer runs
  `scripts/bounty.py build` and adds the resulting delta.

## Closure

A target is _closed_ when it and every `prev_events`/`auth_events` link of it
and its ancestors resolve into the base plus the accumulated verified deltas. A
later claim can complete an earlier partial trace by supplying a missing
ancestor; the award goes to the claim that first makes the full trace
resolvable. Partial traces earn nothing until closure. One claim can close many
targets.

Closure is recursive: the base, earlier accepted deltas, and the current claim
must collectively provide every link of the target, its ancestors, and so on,
until each chain reaches an event already in the base. A target whose event is
available but whose ancestors are not all resolved is still open. A shared
ancestor only needs to be supplied once to the accumulated event pool. If that
ancestor is also an initial target, its own row follows the same closure and
award rules. The prize for each target goes to the claim that first completes
its chain.

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
