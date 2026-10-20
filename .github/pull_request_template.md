# Which bounty?

<!-- Delete this guidance once you've read it. -->

Room ID: `<ROOM>` (the 43-character folder name, e.g.
`L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI`)

## How a claim works

For each room, your PR should add exactly one submission file, plus that room's
`submissions/manifest.json` with your payout address:

    <ROOM>/submissions/<you>.00.jsonl.b64

1. Get the base DAG: `git submodule update --init`, then
   `python3 scripts/bounty.py decode` (writes plaintext to `res/<ROOM>/`; it is
   git-ignored).
2. Find the missing event IDs: `rezzy inspect gaps -i res/<ROOM>/merged-….jsonl`
   (the target list and prizes are in `<ROOM>/targets.csv`).
3. Put the events you recovered, one JSON event per line, in
   `<ROOM>/submissions/<you>.jsonl`, then encode them:
   `python3 scripts/bounty.py encode <ROOM>/submissions/<you>.jsonl`
4. Commit **only** the resulting `.00.jsonl.b64`. Never commit plaintext
   `.jsonl` (it is git-ignored). Base64 is an encoding, not privacy.
5. The published target table fixes the base DAG. The manifest may also pin that
   exact basename as `{"base": "merged-<ROOM>-…"}`; claims against another base
   are rejected.

CI then verifies each event's reference ID and content hash, redacting content
when the content hash does not validate. It builds
`<ROOM>/deltas/<digest>.delta.jsonl.b64` (only events not already known) and a
`.claim.json` listing targets newly closed by this claim. Closure uses the base,
earlier accepted deltas, and your events. A later claim can complete an earlier
partial trace; the award goes to the claim that first makes the full trace
resolvable. The listed targets are only the starting points: the base, earlier
accepted deltas, and this claim must collectively provide every missing
ancestor, recursively, down to the base, or the target stays open. See
`<ROOM>/BOUNTY.md`.

## Payment

Put your Bitcoin address in `<ROOM>/submissions/manifest.json`:

    {"payout": "bc1q…"}

(it can also carry `"base"`). Prizes under the payout minimum accumulate until
they clear it. No address, no payout.

## Checklist

- [ ] Only `<ROOM>/submissions/<you>.00.jsonl.b64` and
      `<ROOM>/submissions/manifest.json` changed
- [ ] No plaintext `.jsonl` committed
- [ ] `manifest.json` has a `payout` address
- [ ] Events are for this room only; anything unresolvable has evidence attached
