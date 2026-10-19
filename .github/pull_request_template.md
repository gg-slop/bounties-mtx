<!-- Delete this guidance once you've read it. -->

## Which bounty?

Room ID: `<ROOM>` (the 43-character folder name, e.g. `L58ME6ufiP49v97UIOBIpvWKEgj4912JmECPuDzlvCI`)

## How a claim works

Your PR should add exactly one file (per room) and nothing else:

    <ROOM>/submissions/<you>.00.jsonl.b64

1. Get the base DAG: `git submodule update --init`, then
   `python3 scripts/bounty.py decode` (writes plaintext to `res/<ROOM>/`; it is
   git-ignored).
2. Find the missing event IDs: `rezzy inspect gaps -i res/<ROOM>/merged-….jsonl`
   (the target list and prizes are in `<ROOM>/targets.json`).
3. Put the events you recovered, one JSON event per line, in
   `<ROOM>/submissions/<you>.jsonl`, then encode them:
   `python3 scripts/bounty.py encode <ROOM>/submissions/<you>.jsonl`
4. Commit **only** the resulting `.00.jsonl.b64`. Never commit plaintext
   `.jsonl` (it is git-ignored). Base64 is an encoding, not privacy.
5. Optionally pin a base DAG with `<ROOM>/submissions/manifest.json`
   (`{"base": "merged-<ROOM>-…"}`); by default the deepest one is used.

CI then verifies every event's ID against its room-v12 hash, builds
`<ROOM>/deltas/<digest>.delta.jsonl.b64` (only events not already known) and a
`.claim.json` listing the targets you closed. A target is closed when it and all
its ancestors' `prev_events`/`auth_events` resolve into the base or your events.
Partial traces earn nothing yet. Each target pays once, to the first merged
claim that closes it. See `<ROOM>/BOUNTY.md`.

## Payment

Put your Bitcoin address in `<ROOM>/submissions/manifest.json`:

    {"payout": "bc1q…"}

(it can also carry `"base"`). Prizes under the payout minimum accumulate until
they clear it. No address, no payout.

## Checklist

- [ ] Only `<ROOM>/submissions/*.jsonl.b64` (and optionally `manifest.json`) changed
- [ ] No plaintext `.jsonl` committed
- [ ] `manifest.json` has a `payout` address
- [ ] Events are for this room only; anything unresolvable has evidence attached
