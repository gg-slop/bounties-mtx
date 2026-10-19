#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK
"""Backfill bounty tooling: targets, submission -> delta, ledger payouts.

  bounty.py targets ROOM [--budget SATS]    -> bounty/ROOM/targets.json
  bounty.py build   SUBMISSION              -> deltas/ROOM/<digest>.delta.jsonl
                                               + <digest>.claim.json
  bounty.py merge   CLAIM --claimant NAME [--pr N]
                                            -> re-verifies, appends
                                               bounty/ROOM/ledger.jsonl

  bounty.py encode [FILE...] [--lines N]  -> stem.NN.jsonl.b64 (50K lines each)
  bounty.py decode [FILE...]              -> stem.NN.jsonl.b64 back into FILE
                                    (default files: from res/*.jsonl.b64)

ROOM is the 43-char room ID. The bounty a claim targets comes from where it
lives: submissions/ROOM/x.jsonl (build) or deltas/ROOM/x.claim.json (merge).
The base DAG is res/merged-ROOM-*; an optional submissions/ROOM/manifest.json
{"base": "merged-ROOM-..."} pins one, else the deepest wins. --base overrides.

Stored DAGs are per-line base64, in line-aligned parts. Any command that loads
a DAG accepts FILE or, if FILE is absent, its .b64 parts (or a single FILE.b64).

Closure is judged against BASE plus every delta in deltas/, so claims can build
on already-merged work. Requires orjson (no stdlib fallback: canonical bytes
must be identical everywhere).

A target is a deduplicated prev_event ID referenced by BASE but absent from it.
Event IDs are room-v12 reference hashes, so a submitted event is self-verifying:
its ID must equal the hash of its (redacted) content. A target is *closed* by a
delta when the target is present and every prev_events/auth_events link of every
ancestor resolves into BASE + delta.
"""

import argparse
import base64
import glob
import hashlib
import importlib
import itertools
import os
import re
import sys

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


def reference_id(ev):
    """Room-v12 event ID: sha256 of the redacted event, unpadded urlsafe b64."""
    t = ev.get("type")
    e = {
        k: v for k, v in ev.items() if k in TOP and k not in ("signatures", "event_id")
    }
    if t != "m.room.create":
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
    if t == "m.room.create":
        e.pop("room_id", None)
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
    """Yield event lines from PATH, else from its base64 parts or PATH.b64.

    b64_only skips PATH itself, so it can be the output of a decode.
    """
    stem = path.removesuffix(".jsonl")
    if os.path.exists(path) and not b64_only:
        sources, b64 = [path], False
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


def load_deltas(directory="deltas"):
    """Merge every *.delta.jsonl in directory into one {event_id: event}."""
    pool = {}
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            if name.endswith(".delta.jsonl"):
                pool.update(load(os.path.join(directory, name)))
    return pool


ROOM = re.compile(r"[A-Za-z0-9_-]{43}")


def room_of(path):
    """Room ID named by the directory holding PATH (e.g. submissions/ROOM/x)."""
    room = os.path.basename(os.path.dirname(os.path.abspath(path)))
    if not ROOM.fullmatch(room):
        sys.exit(f"{path}: parent directory {room!r} is not a room ID")
    return room


def find_base(room, pinned=None):
    """Logical path of ROOM's base DAG under res/ (whole file, parts or .b64).

    pinned names one file (from a manifest); otherwise the greatest depth in
    the file name wins.
    """
    found = {
        PART.sub(".jsonl", p).removesuffix(".b64")
        for p in glob.glob(f"res/merged-{room}-*")
    }
    if pinned:
        want = f"res/{pinned.removesuffix('.jsonl')}.jsonl"
        if want not in found:
            sys.exit(f"base {pinned!r} not found for room {room}")
        return want
    if not found:
        sys.exit(f"no base DAG for room {room} under res/")

    def depth(name):
        m = re.search(r"-d\d+-(\d+)\.jsonl$", name)
        return (int(m.group(1)) if m else 0, name)

    return max(found, key=depth)


def submission_base(path, override=None):
    """(room, base DAG path) for a submission, honouring its manifest.json."""
    room = room_of(path)
    man = os.path.join(os.path.dirname(path), "manifest.json")
    pinned = read_json(man).get("base") if os.path.exists(man) else None
    return room, override or find_base(room, pinned)


def targets_file(room):
    """Published target table for ROOM; exits if there is no such bounty."""
    path = f"bounty/{room}/targets.json"
    if not os.path.exists(path):
        sys.exit(f"no published bounty for room {room}")
    return path


def links(e):
    """All event IDs an event depends on (prev_events then auth_events)."""
    return list(e.get("prev_events", [])) + list(e.get("auth_events", []))


def rank_targets(base):
    """Missing IDs, hardest-looking first: fewest references, newest, then ID.

    refs is how many base events point at the target (few means few servers
    are likely to have seen it); min_child_depth is the shallowest of those
    events. Both are recomputable from the base DAG, so the order is auditable.
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
    """Targets present in delta whose whole ancestry resolves in base+delta."""
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


def cmd_targets(args):
    """Write bounty/ROOM/targets.json with ranks and prizes."""
    rows = rank_targets(load(args.base or find_base(args.room)))
    p = prizes(len(rows), args.budget)
    os.makedirs(f"bounty/{args.room}", exist_ok=True)
    write_json(
        f"bounty/{args.room}/targets.json",
        {
            "budget_sats": args.budget,
            "count": len(rows),
            "weight": "rank**-0.25",
            "order": "refs asc, min_child_depth desc, event_id asc",
            "targets": [
                {"rank": i + 1, **r, "prize_sats": p[i]} for i, r in enumerate(rows)
            ],
        },
    )
    print(
        f"{len(rows)} targets; first prize {p[0]} sats, last {p[-1]}; "
        f"zero-sat targets: {p.count(0)}"
    )


def parse_submission(path, base):
    """Verify each submitted event; return ({event_id: event}, [errors])."""
    sub, errs = {}, []
    room = next(e["room_id"] for e in base.values() if "room_id" in e)
    with open(path, "rb") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                e = orjson.loads(line)
            except orjson.JSONDecodeError:
                errs.append(f"line {n}: invalid JSON")
                continue
            eid = e.get("event_id")
            if reference_id(e) != eid:
                errs.append(f"line {n}: event_id {eid} does not match content hash")
            elif e.get("type") != "m.room.create" and e.get("room_id") != room:
                errs.append(f"line {n}: wrong room")
            elif eid in base and canon(base[eid]) != canon(e):
                errs.append(f"line {n}: {eid} conflicts with base")
            else:
                sub[eid] = e
    return sub, errs


def write_delta(out, delta):
    """Write DELTA as canonical sorted JSONL in OUT; return (digest, path)."""
    body = b"".join(canon(delta[k]) + b"\n" for k in sorted(delta))
    digest = hashlib.sha256(body).hexdigest()[:16]
    os.makedirs(out, exist_ok=True)
    path = f"{out}/{digest}.delta.jsonl"
    with open(path, "wb") as f:
        f.write(body)
    return digest, path


def cmd_build(args):
    """Validate a submission and write its delta and claim files."""
    room, base_name = submission_base(args.submission, args.base)
    base = load(base_name)
    tids = [x["event_id"] for x in read_json(targets_file(room))["targets"]]
    out = f"deltas/{room}"
    sub, errs = parse_submission(args.submission, base)
    if errs:
        print("\n".join(errs[:50]), file=sys.stderr)
        sys.exit(1)
    merged = load_deltas(out)
    delta = {k: v for k, v in sub.items() if k not in base and k not in merged}
    ok = closed_targets(base, {**merged, **delta}, tids)
    digest, dp = write_delta(out, delta)
    dangling = sorted(
        {
            lk
            for e in delta.values()
            for lk in links(e)
            if lk not in base and lk not in delta and lk not in merged
        }
    )
    write_json(
        f"{out}/{digest}.claim.json",
        {
            "room": room,
            "base": base_name,
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


def paid_targets(ledger):
    """Targets already awarded in LEDGER (empty if it doesn't exist yet)."""
    if not os.path.exists(ledger):
        return set()
    with open(ledger, "rb") as f:
        return {orjson.loads(line)["target"] for line in f}


def cmd_merge(args):
    """Re-verify a merged claim from scratch and append unpaid ledger rows."""
    room = room_of(args.claim)
    claim = read_json(args.claim)
    if os.path.dirname(claim["delta"]) != f"deltas/{room}":
        sys.exit(f"{args.claim}: delta is not under deltas/{room}/")
    base_name = args.base or claim["base"]
    if not re.fullmatch(rf"res/merged-{room}-[A-Za-z0-9._-]+\.jsonl", base_name):
        sys.exit(f"{args.claim}: bad base {base_name!r}")
    base = load(base_name)
    tg = {x["event_id"]: x for x in read_json(targets_file(room))["targets"]}
    with open(claim["delta"], "rb") as f:
        body = f.read()
    if hashlib.sha256(body).hexdigest()[:16] != claim["delta_sha256_16"]:
        sys.exit("delta does not match claim digest")
    for e in load(claim["delta"]).values():
        if reference_id(e) != e["event_id"] or e["event_id"] in base:
            sys.exit(f"bad event {e['event_id']}")
    closed = closed_targets(base, load_deltas(os.path.dirname(claim["delta"])), tg)
    led = f"bounty/{room}/ledger.jsonl"
    with open(led, "ab") as f:
        for t in sorted(closed - paid_targets(led)):
            row = {
                "target": t,
                "claimant": args.claimant,
                "pr": args.pr,
                "delta": claim["delta"],
                "prize_sats": tg[t]["prize_sats"],
                "status": "unpaid",
            }
            f.write(orjson.dumps(row) + b"\n")
            print("awarded", t, tg[t]["prize_sats"])


def write_parts(stem, lines, size):
    """Write lines as base64 STEM.NN.jsonl.b64 parts; return line counts."""
    counts = []
    for i, chunk in enumerate(itertools.batched(lines, size)):
        with open(f"{stem}.{i:02d}.jsonl.b64", "wb") as f:
            for line in chunk:
                f.write(base64.b64encode(line) + b"\n")
        counts.append(len(chunk))
    keep = len(counts)
    for stale in part_paths(stem, ".jsonl.b64")[keep:]:
        os.remove(stale)
    return counts


def default_paths():
    """Logical FILE paths for the .b64 files (whole or in parts) under res/."""
    found = glob.glob("res/merged-*.jsonl.b64")
    return sorted({PART.sub(".jsonl", p).removesuffix(".b64") for p in found})


def cmd_recode(args):
    """encode FILE into 50K-line .b64 parts, or decode the .b64 back to FILE."""
    paths = args.files or default_paths()
    if not paths:
        sys.exit(f"{args.cmd}: no .b64 files found under res/")
    for path in paths:
        stem = path.removesuffix(".jsonl")
        if args.cmd == "encode":
            counts = write_parts(stem, iter_lines(path), args.lines)
            print(f"encoded {path} -> {len(counts)} parts, {sum(counts)} lines")
        else:
            n = 0
            with open(path, "wb") as f:
                for line in iter_lines(path, b64_only=True):
                    f.write(line + b"\n")
                    n += 1
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
    s.set_defaults(f=cmd_build)
    s = sp.add_parser("merge")
    s.add_argument("claim")
    s.add_argument("--pr")
    s.add_argument("--claimant", required=True)
    s.add_argument("--base")
    s.set_defaults(f=cmd_merge)
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
