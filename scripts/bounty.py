#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Backfill bounty tooling: targets, submission -> delta, ledger payouts.

  bounty.py targets ROOM [--budget SATS]    -> ROOM/targets.json
  bounty.py build   SUBMISSION              -> ROOM/deltas/<digest>.delta.jsonl.b64
                                               + <digest>.claim.json
  bounty.py merge   CLAIM --claimant NAME [--pr N]
                                            -> re-verifies, appends
                                               ROOM/ledger.jsonl
  bounty.py payout  ROOM [--min SATS] [--fee-rate N] [--change ADDR] [--run]
                                            -> prints the unsigned payout txn
  bounty.py encode [FILE...] [--lines N]  -> stem.NN.jsonl.b64 (50K lines each)
  bounty.py decode [FILE...]              -> stem.NN.jsonl.b64 back into FILE
                                    (default files: from res/*/*.jsonl.b64)

ROOM is the 43-char room ID. The bounty a claim targets comes from where it
lives: ROOM/submissions/x.jsonl.b64 (build) or ROOM/deltas/x.claim.json (merge).
The base DAG is res/ROOM/merged-ROOM-*; an optional ROOM/submissions/manifest.json
{"base": "merged-ROOM-..."} pins one, else the deepest wins. --base overrides.

Stored DAGs are per-line base64, in line-aligned parts. Any command that loads
a DAG accepts FILE or, if FILE is absent, its .b64 parts (or a single FILE.b64).

Builds may use explicitly selected earlier deltas. The merge workflow replays
claims in order, adding each verified delta to the pool used for later claims.
Requires orjson (canonical bytes must be identical everywhere).

A target is a deduplicated prev_events/auth_events ID referenced by BASE but
absent from it. Event IDs are room-v12 reference hashes. Submitted content is
kept only when its Matrix content hash validates; otherwise it is redacted.
A target is *closed* when it and every prev_events/auth_events link of its
ancestors resolve into the accumulated event pool.
"""

import argparse
import base64
import glob
import hashlib
import importlib
import itertools
import os
import re
import subprocess
import sys
import tempfile

import orjson

# Redaction allow-lists (room v11+; v12 additionally drops room_id on create).
TOP = {
    "event_id",
    "type",
    "room_id",
    "sender",
    "state_key",
    "content",
    "hashes",
    "signatures",
    "depth",
    "prev_events",
    "auth_events",
    "origin_server_ts",
}
CONTENT = {
    "m.room.create": {"creator", "room_version", "m.federate", "predecessor", "type"},
    "m.room.member": {"membership", "join_authorised_via_users_server"},
    "m.room.power_levels": {
        "ban",
        "events",
        "events_default",
        "kick",
        "redact",
        "state_default",
        "users",
        "users_default",
        "invite",
    },
    "m.room.join_rules": {"join_rule", "allow"},
    "m.room.history_visibility": {"history_visibility"},
    "m.room.redaction": {"redacts"},
}


def canon(o):
    """Canonical JSON bytes (sorted keys, compact, UTF-8)."""
    return orjson.dumps(o, option=orjson.OPT_SORT_KEYS)


def reference_object(ev):
    """The redacted event object used to calculate its room-v12 event ID."""
    t = ev.get("type")
    e = {
        k: v for k, v in ev.items() if k in TOP and k not in ("signatures", "event_id")
    }
    if t == "m.room.create":
        e.pop("room_id", None)
    c = e.get("content", {})
    keep = CONTENT.get(t, set())
    e["content"] = {k: v for k, v in c.items() if k in keep}
    if (
        t == "m.room.member"
        and "third_party_invite" in c
        and "signed" in c["third_party_invite"]
    ):
        e["content"]["third_party_invite"] = {
            "signed": c["third_party_invite"]["signed"]
        }
    return e


def reference_id(ev):
    """Room-v12 event ID: sha256 of the redacted event, unpadded urlsafe b64."""
    e = reference_object(ev)
    h = hashlib.sha256(canon(e)).digest()
    return "$" + base64.urlsafe_b64encode(h).rstrip(b"=").decode()


PART = re.compile(r"\.\d+\.jsonl(\.b64)?$")


def part_paths(stem, ext):
    """Existing STEM.NN<ext> files in numeric order."""
    pat = re.compile(re.escape(stem) + r"\.(\d+)" + re.escape(ext) + "$")
    found = []
    for p in glob.glob(glob.escape(stem) + ".*" + ext):
        m = pat.match(p)
        if m:
            found.append((int(m.group(1)), p))
    return [p for _, p in sorted(found)]


def iter_lines(path, b64_only=False):
    """Yield event lines from PATH (plain, or base64 if *.b64), else its parts.

    b64_only skips PATH itself, so it can be the output of a decode.
    """
    stem = path.removesuffix(".jsonl")
    if os.path.exists(path) and not b64_only:
        sources, b64 = [path], path.endswith(".b64")
    else:
        sources, b64 = part_paths(stem, ".jsonl.b64"), True
        if not sources and os.path.exists(path + ".b64"):
            sources = [path + ".b64"]
    if not sources:
        raise FileNotFoundError(f"no input for {path}")
    for src in sources:
        with open(src, "rb") as f:
            for line in f:  # bytes: splits on \n only, never on U+2028
                line = line.rstrip(b"\n")
                if line:
                    yield b64strict(line) if b64 else line


def load(path):
    """Read a JSONL file (or its parts) of events into {event_id: event}."""
    evs = {}
    for line in iter_lines(path):
        e = orjson.loads(line)
        evs[e["event_id"]] = e
    return evs


def content_hash_valid(ev):
    """Whether hashes.sha256 covers the complete event content."""
    hashes = ev.get("hashes")
    expected = hashes.get("sha256") if isinstance(hashes, dict) else None
    if not isinstance(expected, str):
        return False
    unhashed = {
        k: v
        for k, v in ev.items()
        if k not in ("event_id", "unsigned", "signatures", "hashes")
    }
    actual = base64.b64encode(hashlib.sha256(canon(unhashed)).digest())
    actual = actual.rstrip(b"=").decode("ascii")
    return actual == expected


def redact_event(ev):
    """Return the event with content restricted to the room-version allow-list."""
    if not isinstance(ev.get("content", {}), dict):
        sys.exit(f"{ev.get('event_id', '<unknown>')}: content must be an object")
    redacted = {k: v for k, v in ev.items() if k in TOP}
    t = ev.get("type")
    c = ev.get("content", {})
    redacted["content"] = {k: v for k, v in c.items() if k in CONTENT.get(t, set())}
    if (
        t == "m.room.member"
        and "third_party_invite" in c
        and "signed" in c["third_party_invite"]
    ):
        redacted["content"]["third_party_invite"] = {
            "signed": c["third_party_invite"]["signed"]
        }
    return redacted


def validate_event(event, room):
    """Return a hash-checked or safely redacted event, or exit on invalid ID/room."""
    if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
        sys.exit("event has no event_id")
    eid = event["event_id"]
    if event.get("type") == "m.room.create":
        if event.get("room_id", room) != room:
            sys.exit(f"{eid}: wrong room")
    elif event.get("room_id") != room:
        sys.exit(f"{eid}: wrong room")
    try:
        normalized = event if content_hash_valid(event) else redact_event(event)
        actual = reference_id(normalized)
    except (KeyError, TypeError, ValueError) as exc:
        sys.exit(f"{eid}: malformed event: {exc}")
    if actual != eid:
        sys.exit(f"{eid}: event_id does not match reference hash")
    return normalized


def equivalent_events(left, right):
    """Compare copies by their redacted reference-hash representation."""
    return canon(reference_object(left)) == canon(reference_object(right))


def load_delta(path, room, base):
    """Load and validate one delta; its filename digest covers canonical source rows."""
    events = {}
    source = []
    for line in iter_lines(path):
        event = orjson.loads(line)
        source.append(event)
        checked = validate_event(event, f"!{room}")  # v12 room IDs are "!" + hash
        eid = checked["event_id"]
        if eid in base:
            sys.exit(f"{path}: delta event {eid} already exists in base")
        previous = events.get(eid)
        if previous is not None and not equivalent_events(previous, checked):
            sys.exit(f"{path}: conflicting copies of event {eid}")
        if previous is None or (
            content_hash_valid(checked) and not content_hash_valid(previous)
        ):
            events[eid] = checked
    body = b"".join(
        canon(e) + b"\n" for e in sorted(source, key=lambda e: e["event_id"])
    )
    expected = os.path.basename(path).split(".", 1)[0]
    if hashlib.sha256(body).hexdigest()[:16] != expected:
        sys.exit(f"{path}: delta digest does not match its contents")
    return events


def delta_paths(directory):
    """Every *.delta.jsonl.b64 in DIRECTORY, sorted (empty if it doesn't exist)."""
    if not os.path.isdir(directory):
        return []
    return [
        os.path.join(directory, name)
        for name in sorted(os.listdir(directory))
        if name.endswith(".delta.jsonl.b64")
    ]


def claim_agrees(claim, room, path, base, base_name):
    """Whether CLAIM's metadata matches its delta at PATH and the base DAG."""
    name = os.path.basename(path)
    return all(
        [
            claim.get("room") == room,
            claim.get("delta") == f"{room}/deltas/{name}",
            claim.get("delta_sha256_16") == name.split(".", 1)[0],
            claim.get("base_sha256") == base_fingerprint(base),
            base_name is None or claim.get("base") == base_name,
        ]
    )


def add_to_pool(pool, events):
    """Add EVENTS to POOL, rejecting conflicts and preferring valid content."""
    for eid, event in events.items():
        previous = pool.get(eid)
        if previous is not None and not equivalent_events(previous, event):
            sys.exit(f"conflicting delta copies of event {eid}")
        if previous is None or (
            content_hash_valid(event) and not content_hash_valid(previous)
        ):
            pool[eid] = event


def load_deltas(directory, room, base, paths=None, base_name=None):
    """Load validated deltas into one event pool, rejecting conflicting copies."""
    pool = {}
    for path in delta_paths(directory) if paths is None else paths:
        claim_path = path.removesuffix(".delta.jsonl.b64") + ".claim.json"
        if not os.path.isfile(claim_path):
            sys.exit(f"{path}: no matching claim file")
        if not claim_agrees(read_json(claim_path), room, path, base, base_name):
            sys.exit(f"{claim_path}: metadata does not match its delta or base")
        add_to_pool(pool, load_delta(path, room, base))
    return pool


def combine_pools(*pools):
    """Combine event pools without allowing one copy to silently override another."""
    combined = {}
    for pool in pools:
        for eid, event in pool.items():
            previous = combined.get(eid)
            if previous is not None and not equivalent_events(previous, event):
                sys.exit(f"conflicting copies of event {eid}")
            if previous is None or (
                content_hash_valid(event) and not content_hash_valid(previous)
            ):
                combined[eid] = event
    return combined


ROOM = re.compile(r"[A-Za-z0-9_-]{43}")


def room_of(path):
    """Room ID in ROOM/<kind>/<file>, the layout of submissions and deltas."""
    room = os.path.abspath(path).split(os.sep)[-3]
    if not ROOM.fullmatch(room):
        sys.exit(f"{path}: expected ROOM/<submissions|deltas>/<file>, got {room!r}")
    return room


def find_base(room, pinned=None):
    """Logical path of ROOM's base DAG under res/ (whole file, parts or .b64).

    pinned names one file (from a manifest); otherwise the greatest depth in
    the file name wins.
    """
    found = {
        PART.sub(".jsonl", p).removesuffix(".b64")
        for p in glob.glob(f"res/{room}/merged-{room}-*")
    }
    if pinned:
        want = f"res/{room}/{pinned.removesuffix('.jsonl')}.jsonl"
        if want not in found:
            sys.exit(f"base {pinned!r} not found for room {room}")
        return want
    if not found:
        sys.exit(f"no base DAG for room {room} under res/{room}/")

    def depth(name):
        m = re.search(r"-d\d+-(\d+)\.jsonl$", name)
        return (int(m.group(1)) if m else 0, name)

    return max(found, key=depth)


ADDR = re.compile(r"(?:bc1|tb1|bcrt1)[a-z0-9]{8,87}|[13mn2][a-km-zA-HJ-NP-Z1-9]{25,39}")


def read_manifest(submission):
    """The optional manifest.json next to SUBMISSION ({} if absent)."""
    man = os.path.join(os.path.dirname(submission), "manifest.json")
    return read_json(man) if os.path.exists(man) else {}


def submission_base(path, override=None):
    """(room, base DAG path) for a submission, honouring its manifest.json."""
    room = room_of(path)
    return room, override or find_base(room, read_manifest(path).get("base"))


def targets_file(room):
    """Published target table for ROOM; exits if there is no such bounty."""
    path = f"{room}/targets.json"
    if not os.path.exists(path):
        sys.exit(f"no published bounty for room {room}")
    return path


def links(e):
    """All event IDs an event depends on (prev_events then auth_events)."""
    return list(e.get("prev_events", [])) + list(e.get("auth_events", []))


def rank_targets(base):
    """Missing IDs, hardest-looking first: fewest references, shallowest depth.

    refs is how many base events point at the target (few means few servers
    are likely to have seen it); min_child_depth is the shallowest depth among
    those referencing events. Both are recomputable from the base DAG.
    """
    seen = {}
    for e in base.values():
        for p in links(e):
            if p not in base:
                seen.setdefault(p, {})[e["event_id"]] = e["depth"]
    rows = [
        {"event_id": t, "refs": len(d), "min_child_depth": min(d.values())}
        for t, d in seen.items()
    ]
    rows.sort(key=lambda r: (r["refs"], -r["min_child_depth"], r["event_id"]))
    return rows


def prizes(n, budget, alpha=0.25):
    """Rank k of n gets budget * k**-alpha / sum(j**-alpha), in whole sats.

    A mild power law (about 7:1 top to bottom at n=2163, alpha=0.25). Largest-
    remainder rounding keeps the total exactly equal to budget.
    """
    w = [k**-alpha for k in range(1, n + 1)]
    total = sum(w)
    raw = [budget * x / total for x in w]
    out = [int(x) for x in raw]
    rest = budget - sum(out)
    for i in sorted(range(n), key=lambda i: raw[i] - out[i], reverse=True)[:rest]:
        out[i] += 1
    return out


def closed_targets(base, delta, tgts):
    """Targets in delta whose ancestry resolves in the accumulated event pool."""
    ok, memo = set(), {}

    def closed(i):  # iterative DFS; memo: 1 closed, 0 open
        stack, seen = [i], set()
        while stack:
            x = stack.pop()
            if x in base or memo.get(x) == 1:
                continue
            if x not in delta:
                return False
            if x in seen:
                continue
            seen.add(x)
            stack.extend(links(delta[x]))
        for x in seen:
            memo[x] = 1
        return True

    for t in tgts:
        if t in delta and closed(t):
            ok.add(t)
    return ok


def read_json(path):
    """Load one JSON document."""
    with open(path, "rb") as f:
        return orjson.loads(f.read())


def write_json(path, obj):
    """Write one JSON document, indented."""
    with open(path, "wb") as f:
        f.write(orjson.dumps(obj, option=orjson.OPT_INDENT_2))


def base_fingerprint(base):
    """Stable digest of the canonical event set, independent of source encoding."""
    digest = hashlib.sha256()
    for eid in sorted(base):
        digest.update(canon(base[eid]) + b"\n")
    return digest.hexdigest()


def check_published_base(room, base_name, base, table):
    """Require claims to use the exact DAG used to publish the target table."""
    expected = table.get("base_sha256")
    if not expected or table.get("base") != base_name:
        sys.exit(f"claim must use the published base {table.get('base')!r}")
    if base_fingerprint(base) != expected:
        sys.exit(f"base DAG fingerprint does not match published bounty for {room}")


def cmd_targets(args):
    """Write ROOM/targets.json with ranks and prizes."""
    base_name = args.base or find_base(args.room)
    base = load(base_name)
    rows = rank_targets(base)
    p = prizes(len(rows), args.budget)
    path = f"{args.room}/targets.json"
    target_rows = [{"rank": i + 1, **r, "prize_sats": p[i]} for i, r in enumerate(rows)]
    published = {
        "budget_sats": args.budget,
        "count": len(rows),
        "weight": "rank**-0.25",
        "order": "refs asc, min_child_depth desc, event_id asc",
        "targets": target_rows,
    }
    if os.path.exists(path):
        existing = read_json(path)
        if any(existing.get(k) != v for k, v in published.items()):
            sys.exit(f"{path} is already published; refusing to change ranks or prizes")
        fingerprint = base_fingerprint(base)
        if existing.get("base") not in (None, base_name):
            sys.exit(f"{path} is already tied to base {existing['base']!r}")
        if existing.get("base_sha256") not in (None, fingerprint):
            sys.exit(f"{path} is already tied to a different base DAG")
        existing["base"] = base_name
        existing["base_sha256"] = fingerprint
        write_json(path, existing)
        print(f"published table metadata verified for {base_name}")
        return
    os.makedirs(args.room, exist_ok=True)
    published["base"] = base_name
    published["base_sha256"] = base_fingerprint(base)
    write_json(path, published)
    if p:
        print(
            f"{len(rows)} targets; first prize {p[0]} sats, last {p[-1]}; "
            f"zero-sat targets: {p.count(0)}"
        )
    else:
        print("0 targets; no prizes to publish")


def parse_submission(path, base):
    """Verify each submitted event; return ({event_id: event}, [errors]).

    PATH is per-line base64 (*.jsonl.b64); plain JSONL is accepted for local use.
    """
    sub, errs = {}, []
    room = next(e["room_id"] for e in base.values() if "room_id" in e)
    try:
        for n, line in enumerate(iter_lines(path), 1):
            try:
                e = orjson.loads(line)
            except orjson.JSONDecodeError:
                errs.append(f"line {n}: invalid JSON")
                continue
            try:
                checked = validate_event(e, room)
            except SystemExit as exc:
                errs.append(f"line {n}: {exc}")
                continue
            eid = checked["event_id"]
            if eid in base and not equivalent_events(base[eid], checked):
                errs.append(f"line {n}: {eid} conflicts with base")
            else:
                sub[eid] = checked
    except ValueError as exc:  # bad base64
        errs.append(f"invalid base64: {exc}")
    return sub, errs


def write_delta(out, delta):
    """Write DELTA as sorted per-line base64 in OUT; return (digest, path).

    The digest is over the canonical plaintext, so it is independent of encoding.
    """
    lines = [canon(delta[k]) for k in sorted(delta)]
    digest = hashlib.sha256(b"".join(x + b"\n" for x in lines)).hexdigest()[:16]
    os.makedirs(out, exist_ok=True)
    path = f"{out}/{digest}.delta.jsonl.b64"
    with open(path, "wb") as f:
        f.writelines(base64.b64encode(x) + b"\n" for x in lines)
    return digest, path


def newly_closed(base, prior, delta, tids):
    """Targets closed by adding DELTA to PRIOR that PRIOR alone did not close."""
    before = closed_targets(base, prior, tids)
    return closed_targets(base, combine_pools(prior, delta), tids) - before


def dangling_links(base, delta, merged):
    """Links from DELTA that resolve in neither the base nor earlier deltas."""
    return sorted(
        {
            lk
            for e in delta.values()
            for lk in links(e)
            if lk not in base and lk not in delta and lk not in merged
        }
    )


def checked_payout(submission):
    """The manifest's payout address, exiting if it isn't address-shaped."""
    payout = read_manifest(submission).get("payout")
    if payout is not None and not ADDR.fullmatch(str(payout)):
        sys.exit(f"{submission}: manifest payout is not a Bitcoin address")
    return payout


def verified_submission(path, base):
    """Events of the submission at PATH, exiting with the errors if any are bad."""
    sub, errs = parse_submission(path, base)
    if errs:
        print("\n".join(errs[:50]), file=sys.stderr)
        sys.exit(1)
    return sub


def cmd_build(args):
    """Validate a submission and write its delta and claim files."""
    room, base_name = submission_base(args.submission, args.base)
    base = load(base_name)
    table = read_json(targets_file(room))
    check_published_base(room, base_name, base, table)
    out = f"{room}/deltas"
    payout = checked_payout(args.submission)
    sub = verified_submission(args.submission, base)
    merged = load_deltas(
        out,
        room,
        base,
        paths=[] if args.no_prior_deltas else args.prior_delta or None,
        base_name=base_name,
    )
    delta = {k: v for k, v in sub.items() if k not in base and k not in merged}
    if not delta:
        sys.exit(f"{args.submission}: contains no new events")
    ok = newly_closed(base, merged, delta, [x["event_id"] for x in table["targets"]])
    digest, dp = write_delta(out, delta)
    dangling = dangling_links(base, delta, merged)
    write_json(
        f"{out}/{digest}.claim.json",
        {
            "room": room,
            "base": base_name,
            "base_sha256": table["base_sha256"],
            "payout": payout,
            "delta": dp,
            "delta_sha256_16": digest,
            "events": len(delta),
            "dangling": dangling,
            "closed_targets": sorted(ok),
        },
    )
    print(
        f"{len(delta)} new events, {len(ok)} targets closed, "
        f"{len(dangling)} dangling links -> {dp}"
    )


def awarded_targets(ledger):
    """Targets already present in LEDGER, regardless of payment status."""
    if not os.path.exists(ledger):
        return set()
    with open(ledger, "rb") as f:
        return {orjson.loads(line)["target"] for line in f}


DELTA_PATH = r"{room}/deltas/[0-9a-f]{{16}}\.delta\.jsonl\.b64"


def claim_base_name(args, room, claim):
    """Validate the claim's room, delta path and base name; return the base name."""
    if claim.get("room") != room:
        sys.exit(f"{args.claim}: claim room does not match its directory")
    if not re.fullmatch(DELTA_PATH.format(room=room), claim["delta"]):
        sys.exit(f"{args.claim}: bad delta path {claim['delta']!r}")
    base_name = args.base or claim["base"]
    if not re.fullmatch(rf"res/{room}/merged-{room}-[A-Za-z0-9._-]+\.jsonl", base_name):
        sys.exit(f"{args.claim}: bad base {base_name!r}")
    return base_name


def trusted_table(args, room, base_name, base):
    """The published target table, checked against the base and the claim push."""
    table = read_json(args.trusted_targets or targets_file(room))
    if args.trusted_targets and table != read_json(targets_file(room)):
        sys.exit(f"{targets_file(room)} changed in the claim push")
    check_published_base(room, base_name, base, table)
    return table


def check_claim_delta(claim, table, base_name):
    """Exit unless CLAIM names the published base and its delta matches its digest."""
    if (
        claim.get("base") != base_name
        or claim.get("base_sha256") != table["base_sha256"]
    ):
        sys.exit("claim does not match the published base DAG")
    body = b"".join(x + b"\n" for x in iter_lines(claim["delta"]))
    if hashlib.sha256(body).hexdigest()[:16] != claim["delta_sha256_16"]:
        sys.exit("delta does not match claim digest")


def append_awards(args, claim, tg, targets):
    """Append an unpaid ledger row for each of TARGETS not already awarded."""
    led = f"{room_of(args.claim)}/ledger.jsonl"
    with open(led, "ab") as f:
        for t in sorted(targets - awarded_targets(led)):
            row = {
                "target": t,
                "claimant": args.claimant,
                "address": claim.get("payout"),
                "pr": args.pr,
                "delta": claim["delta"],
                "prize_sats": tg[t]["prize_sats"],
                "status": "unpaid",
            }
            f.write(orjson.dumps(row) + b"\n")
            print("awarded", t, tg[t]["prize_sats"])


def cmd_merge(args):
    """Re-verify a claim against earlier claims and award newly closed targets."""
    room = room_of(args.claim)
    claim = read_json(args.claim)
    base_name = claim_base_name(args, room, claim)
    base = load(base_name)
    table = trusted_table(args, room, base_name, base)
    check_claim_delta(claim, table, base_name)
    tg = {x["event_id"]: x for x in table["targets"]}
    current = load_delta(claim["delta"], room, base)
    for path in args.prior_delta or []:
        if not re.fullmatch(DELTA_PATH.format(room=room), path):
            sys.exit(f"bad prior delta path {path!r}")
    prior = load_deltas(
        os.path.dirname(claim["delta"]),
        room,
        base,
        paths=args.prior_delta or [],
        base_name=base_name,
    )
    append_awards(args, claim, tg, newly_closed(base, prior, current, tg))


def btc(sats):
    """Whole satoshis as a BTC amount with 8 decimals (integer math, no floats)."""
    return f"{sats // 10**8}.{sats % 10**8:08d}"


def unpaid_totals(room, overrides):
    """Sum unpaid ledger prizes per (claimant, address); address may be None."""
    totals = {}
    path = f"{room}/ledger.jsonl"
    if not os.path.exists(path):
        sys.exit(f"no ledger for room {room}")
    with open(path, "rb") as f:
        for line in f:
            row = orjson.loads(line)
            if row["status"] != "unpaid":
                continue
            key = (row["claimant"], overrides.get(row["claimant"], row.get("address")))
            totals[key] = totals.get(key, 0) + row["prize_sats"]
    return totals


def cmd_payout(args):
    """Print the unsigned payout transaction for a room's unpaid prizes."""
    overrides = dict(x.split("=", 1) for x in args.address)
    totals = unpaid_totals(args.room, overrides)
    pay, carry, noaddr = {}, [], []
    for (who, addr), sats in sorted(totals.items()):
        if addr is None or not ADDR.fullmatch(addr):
            noaddr.append((who, sats))
        elif sats < args.min:
            carry.append((who, sats))
        else:
            pay[addr] = pay.get(addr, 0) + sats
    for who, sats in noaddr:
        print(f"no valid address, skipped: {who} {sats} sats", file=sys.stderr)
    for who, sats in carry:
        print(
            f"below {args.min} sats, carried over: {who} {sats} sats", file=sys.stderr
        )
    if not pay:
        sys.exit("nothing to pay")
    outputs = (
        "[" + ",".join(f'{{"{a}":{btc(v)}}}' for a, v in sorted(pay.items())) + "]"
    )
    options = {"fee_rate": args.fee_rate}
    if args.change:
        options["changeAddress"] = args.change
    cmd = ["bitcoin-cli", "-named", "walletcreatefundedpsbt", "inputs=[]"]
    cmd += [f"outputs={outputs}", f"options={orjson.dumps(options).decode()}"]
    print(f"# {len(pay)} outputs, {sum(pay.values())} sats (+ network fee)")
    print(" ".join(f"'{c}'" if " " in c or "[" in c or "{" in c else c for c in cmd))
    if args.run:
        res = orjson.loads(subprocess.run(cmd, check=True, capture_output=True).stdout)
        print(f"# unsigned PSBT, fee {res['fee']} BTC")
        print(res["psbt"])


def write_parts(stem, lines, size):
    """Write lines as base64 STEM.NN.jsonl.b64 parts; return line counts."""
    counts = []
    parent = os.path.dirname(stem) or "."
    os.makedirs(parent, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".b64-parts-", dir=parent) as tempdir:
        temp_paths = []
        for i, chunk in enumerate(itertools.batched(lines, size)):
            temp_path = os.path.join(tempdir, f"{i:02d}.jsonl.b64")
            with open(temp_path, "wb") as f:
                for line in chunk:
                    f.write(base64.b64encode(line) + b"\n")
            temp_paths.append(temp_path)
            counts.append(len(chunk))
        for i, temp_path in enumerate(temp_paths):
            os.replace(temp_path, f"{stem}.{i:02d}.jsonl.b64")
    keep = len(counts)
    for stale in part_paths(stem, ".jsonl.b64")[keep:]:
        os.remove(stale)
    return counts


def default_paths():
    """Logical FILE paths for the .b64 files (whole or in parts) under res/*/."""
    found = glob.glob("res/*/merged-*.jsonl.b64")
    return sorted({PART.sub(".jsonl", p).removesuffix(".b64") for p in found})


def cmd_recode(args):
    """encode FILE into 50K-line .b64 parts, or decode the .b64 back to FILE."""
    paths = args.files or default_paths()
    if not paths:
        sys.exit(f"{args.cmd}: no .b64 files found under res/*/")
    for path in paths:
        stem = path.removesuffix(".jsonl")
        if args.cmd == "encode":
            counts = write_parts(stem, iter_lines(path), args.lines)
            print(f"encoded {path} -> {len(counts)} parts, {sum(counts)} lines")
        else:
            n = 0
            parent = os.path.dirname(path) or "."
            fd, temp_path = tempfile.mkstemp(prefix=".decoded-", dir=parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    for line in iter_lines(path, b64_only=True):
                        f.write(line + b"\n")
                        n += 1
                os.replace(temp_path, path)
            except BaseException:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise
            print(f"decoded {stem}.NN -> {path}, {n} lines")


def b64strict(line):
    """Decode one base64 line, rejecting stray characters."""
    return base64.b64decode(line, validate=True)


def main():
    """Parse the command line and dispatch."""
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(required=True)
    s = sp.add_parser("targets")
    s.add_argument("room")
    s.add_argument("--budget", type=int, default=20000)
    s.add_argument("--base")
    s.set_defaults(f=cmd_targets)
    s = sp.add_parser("build")
    s.add_argument("submission")
    s.add_argument("--base")
    s.add_argument(
        "--prior-delta",
        action="append",
        default=[],
        help="previous accepted delta; repeat for each one",
    )
    s.add_argument(
        "--no-prior-deltas",
        action="store_true",
        help="do not load delta files from the current checkout",
    )
    s.set_defaults(f=cmd_build)
    s = sp.add_parser("merge")
    s.add_argument("claim")
    s.add_argument("--pr")
    s.add_argument("--claimant", required=True)
    s.add_argument("--base")
    s.add_argument("--trusted-targets")
    s.add_argument(
        "--prior-delta",
        action="append",
        default=[],
        help="previous claim delta, in merge order; repeat for each prior claim",
    )
    s.set_defaults(f=cmd_merge)
    s = sp.add_parser("payout")
    s.add_argument("room")
    s.add_argument("--min", type=int, default=1000, help="minimum sats per payee")
    s.add_argument("--fee-rate", type=int, default=5, help="sat/vB")
    s.add_argument("--change")
    s.add_argument("--address", action="append", default=[], metavar="WHO=ADDR")
    s.add_argument("--run", action="store_true", help="ask bitcoin-cli for the PSBT")
    s.set_defaults(f=cmd_payout)
    for name in ("encode", "decode"):
        s = sp.add_parser(name)
        s.add_argument("files", nargs="*")
        if name == "encode":
            s.add_argument("--lines", type=int, default=50000)
        s.set_defaults(f=cmd_recode, cmd=name)
    try:  # optional: shell completion only
        importlib.import_module("argcomplete").autocomplete(ap)
    except ImportError:
        pass
    args = ap.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
