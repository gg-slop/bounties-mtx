# Backfill bounty: Matrix HQ missing `prev_events`

Originally promised at 3 satoshi per completed event at approximately 6,500
events, depth 91,136. The published budget is 20,000 sats.

The fixed four-column CSV in `<ROOM>/targets.csv` covers 2,235 unique initial
missing event IDs referenced by the base DAG
(`res/<ROOM>/merged-…-v12-d1-71756.jsonl`) or by event relations in that DAG.
The room base is selected when the target table is first published and pinned in
`<ROOM>/base.lock` by its path, fingerprint, and budget. Each room can set its
own budget with `scripts/bounty.py targets <ROOM> --budget SATS`, which is
required when first publishing; later runs read it from the lock. A deeper
merged DAG added later does not silently replace the published base.
Regenerating the table for a different base invalidates existing claims; they
are not migrated automatically. Rezzy warning's 1,820 count is the number of
local events that reference missing `prev_events`; multiple events can point to
the same ID, and missing `auth_events` and `m.relates_to` targets add more. The
table was generated with `scripts/bounty.py targets <ROOM>`.

These are only the initial targets, not the whole of the missing history.
Fetching one can reveal further missing dependencies, which can reveal more. A
target closes only when the base, earlier accepted deltas, and the current claim
collectively provide every `prev_events` and `auth_events` link of the target
and each ancestor, recursively, until the chain reaches the base. A
`m.relates_to` reference (`event_id` or `m.in_reply_to.event_id`) in the base
only makes its target a row; it does not add to what a chain must supply,
because a redacted copy of an event no longer carries it. Ancestor gaps are
needed to close their descendant but are not targets or paid separately. If an
ancestor is also an initial target, its own row remains eligible. The prize goes
to the first merged claim that completes the target's full chain.

Events deeper than the base's deepest event (71,756) are out of scope: CI
ignores them in a submission and rejects them in a delta.

If an edge cannot be resolved or followed (for example, it points to an event
from another room), provide evidence explaining why.

## Claiming

For each room, one PR adds one logical submission. If it exceeds 50,000 lines,
it can contain multiple sequential `.NN` part files. Include every generated
part. `<ROOM>` is the 43-character room ID; the directory selects the bounty.

1. Get the base DAG: run `git submodule update --init`, then
   `python3 scripts/bounty.py decode`. This writes plaintext under
   `res/<ROOM>/`, which is git-ignored.
2. Find missing IDs with `rezzy inspect gaps -i res/<ROOM>/merged-….jsonl`, or
   any tool of your own (see _Running the tools locally_ for installing
   `rezzy`). Targets and prizes are in `<ROOM>/targets.csv`.
3. Put recovered events, one JSON event per line, in
   `<ROOM>/submissions/<username>.jsonl`, then run
   `python3 scripts/bounty.py encode <ROOM>/submissions/<username>.jsonl`. Use a
   username that is a valid file name: your GitHub username, or, if it is
   filename-safe, your Matrix ID with the `:` changed to `+`. For example
   `alice` stays `alice`, and `@alice:matrix.org` becomes `@alice+matrix.org`.
   Letters, digits, `.`, `-`, `_`, `@`, `+` and `=` are allowed; `:` and `/` are
   not, and a name with a `.` must not end in digits (`.45` would read as a part
   number). A Matrix server name never contains `+`, so the last `+` is always
   the separator. Matrix IDs that cannot be written this way (IPv6 server names,
   or any `/` or other disallowed character) are not supported: use your GitHub
   username. A later claim adds `-2`: `alice-2.jsonl` becomes
   `alice-2.00.jsonl.b64`. The `.00` is a part number, not a claim counter.
4. Commit all generated parts, from `<username>.00.jsonl.b64` onward, then open
   the PR. Never commit plaintext `.jsonl`. Base64 is an encoding, not privacy:
   anyone can decode it.
5. Put your payout address on its own line in the PR description:
   `Payout: bc1q…`. It is read once, when the claim is merged; editing the
   description afterwards does not change an award already recorded. With no
   valid address the award is still recorded, but unpayable until the maintainer
   supplies one (`scripts/bounty.py address <ROOM> <claimant> <address>`).

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

Claimants need Python 3.9 or newer and `orjson` (`pip install orjson`) for
`decode` and `encode`. The `rezzy` command used above is optional and can be
installed from the same pinned commit as below with
`cargo install --locked --path ../rezzy-pinned/rezzy-cli` after checkout. A
maintainer who runs `scripts/bounty.py build` or `merge` also needs Rezzy's C
library, `librezzy_ffi`, built from the same pinned commit CI uses (`REZZY_SHA`
at the top of `.github/workflows/claim.yml`). The `rezzy` command alone is not
enough.

```sh
REZZY_SHA=$(sed -n 's/^  REZZY_SHA: //p' .github/workflows/claim.yml)
git clone https://github.com/gamesguru/rezzy.git ../rezzy-pinned
git -C ../rezzy-pinned checkout "$REZZY_SHA"
rustup toolchain install 1.88.0 --profile minimal
(cd ../rezzy-pinned && RUSTUP_TOOLCHAIN=1.88.0 cargo install --locked --path rezzy-cli)
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
- Claimants give a Bitcoin address on a `Payout:` line of the PR description,
  read when the claim is merged (a row recorded without one is fixed with
  `scripts/bounty.py address <ROOM> <claimant> <address>`).
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
