# Backfill bounty: Matrix HQ missing `prev_events`

Originally promised at 3 satoshi per completed event at approximately 6,500
events, depth 91,136. The published budget is 20,000 sats.

The fixed four-column CSV in `<ROOM>/targets.csv` covers 2,235 unique initial
missing event IDs referenced by the base DAG
(`res/<ROOM>/merged-…-v12-d1-71756.jsonl`) or by event relations in that DAG.
The room base is the deepest matching merged DAG under `res/<ROOM>`; the CSV's
target IDs and ranking fields are checked against it. Rezzy warning's 1,820
count is the number of local events that reference missing `prev_events`;
multiple events can point to the same ID, and missing `auth_events` and
`m.relates_to` targets add more. The table was generated with
`scripts/bounty.py targets <ROOM>`.

These are only the initial targets, not the whole of the missing history.
Fetching one can reveal further missing dependencies, which can reveal more. A
target closes only when the base, earlier accepted deltas, and the current claim
collectively provide every `prev_events`, `auth_events`, and `m.relates_to`
(`event_id` and `m.in_reply_to.event_id`) link of the target and each
recursively referenced event, until the chain reaches the base. Dependency gaps
are needed to close their referring target but are not targets or paid
separately. If a dependency is also an initial target, its own row remains
eligible. The prize goes to the first merged claim that completes the target's
full dependency chain.

If an edge cannot be resolved or followed (for example, it points to an event
from another room), provide evidence explaining why.

## Claiming

For each room, one PR adds one logical submission. If it exceeds 50,000 lines,
it can contain multiple sequential `.NN` part files. Include every generated
part and `<ROOM>/submissions/manifest.json`. `<ROOM>` is the 43-character
base-64-url room ID; the directory selects the bounty.

1. Get the base DAG: run `git submodule update --init`, then
   `python3 scripts/bounty.py decode`. This writes plaintext under
   `res/<ROOM>/`, which is git-ignored.
2. Find missing IDs with `rezzy inspect gaps -i res/<ROOM>/merged-….jsonl`.
   Targets and prizes are in `<ROOM>/targets.csv`.
3. Put recovered events, one JSON event per line, in
   `<ROOM>/submissions/<username>.jsonl`, then run
   `python3 scripts/bounty.py encode <ROOM>/submissions/<username>.jsonl`. Use a
   filename-safe username containing only letters, digits, `-` and `_`: your
   GitHub username or a safe alias of your Matrix localpart. Replace other
   characters, such as `.`, with `_`. Add `-2` for a later claim. The `.00`
   suffix added by `encode` is a part number, not a claim counter.
4. Commit all generated parts, from `<username>.00.jsonl.b64` onward, and
   `<ROOM>/submissions/manifest.json`; then open the PR. Never commit plaintext
   `.jsonl`. Base64 is an encoding, not privacy: anyone can decode it.
5. Set the manifest to `{"payout": "bc1q…"}`. Without an address there is no
   payout. It may also name the selected base as `{"base": "merged-<ROOM>-…"}`,
   but cannot select a different base: the room's selected base is authoritative
   and claims against another base are rejected.

## What CI does

- CI verifies each event ID against its room version's reference hash, drops
  events already in the base, and writes
  `<ROOM>/deltas/<digest>.delta.jsonl.b64` (only events absent from the base,
  also base64) plus `<digest>.claim.json` (closed targets and dangling links).
- For same-repository PRs, CI replaces the encoded submission with its delta and
  keeps the submission as a 14-day artifact. For fork PRs, the source branch
  retains the encoded submission; a maintainer runs `scripts/bounty.py build`
  and adds the generated delta. See _Running the tools locally_ first.

## Running the tools locally

Claimants need `orjson` (`pip install orjson`) for `decode` and `encode`. A
maintainer running `scripts/bounty.py build` or `merge` by hand also needs
Rezzy's C library, `librezzy_ffi`, built from the same pinned commit CI uses
(`REZZY_SHA` at the top of `.github/workflows/claim.yml`). The `rezzy` command
alone is not enough.

```sh
REZZY_SHA=$(sed -n 's/^  REZZY_SHA: //p' .github/workflows/claim.yml)
git clone https://github.com/gamesguru/rezzy.git ../rezzy-pinned
git -C ../rezzy-pinned checkout "$REZZY_SHA"
rustup toolchain install 1.88.0 --profile minimal
(cd ../rezzy-pinned &&
  RUSTUP_TOOLCHAIN=1.88.0 cargo build --release --locked -p rezzy-ffi)
export REZZY_LIB="$(realpath ../rezzy-pinned)/target/release/librezzy_ffi.so"
```

Use the absolute path, and `.dylib` instead of `.so` on macOS. If `REZZY_LIB` is
unset or wrong, the script exits without accepting the claim.

## Payout

- Targets are listed by `n_refs` (ascending), `min_child_depth` (ascending),
  then event ID. These fields describe the references in the base; they do not
  determine prize size. The 20,000-sat budget is split uniformly across all
  2,235 targets: 2,120 rows pay 9 sats and 115 rows pay 8 sats. Integer rounding
  uses table order, and the total is exact.
- Each target pays once, to the first **merged** delta that closes it.
  `<ROOM>/ledger.jsonl` (`scripts/bounty.py merge`) records the target,
  claimant, PR, delta, prize, and unpaid status.
- Later deltas covering an already-awarded target pay nothing for it.
- The row order is fixed and published before claims open; it cannot depend on
  who claims what or when. The table is fixed once published.
- `scripts/bounty.py targets ROOM` refuses to change a published table's rows or
  prizes unless `--regenerate` is supplied. Claims must use the room's selected
  base DAG; CI checks the CSV's target IDs and ranking fields against that DAG
  and records its fingerprint in each claim.
- Claimants provide a Bitcoin address as `"payout"` in their manifest.
  `scripts/bounty.py payout <ROOM> --min 5000` aggregates unpaid prizes by
  address, applies the minimum, and prints an unsigned
  `bitcoin-cli walletcreatefundedpsbt` command to run manually with your own
  node. After broadcasting, run `scripts/bounty.py paid <ROOM> <TXID>` to mark
  those rows paid. A pending batch must be recorded as paid before creating
  another; use `--replace` only to discard a batch that was not broadcast. This
  repository does not sign or broadcast transactions. Address totals under
  `--min` carry over (on-chain outputs below roughly 300–550 sats are dust).
- Unclaimed prizes are not redistributed; at the deadline they return to the
  fund or roll into a later bounty.
