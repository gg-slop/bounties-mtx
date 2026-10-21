#!/usr/bin/env python3
# pylint: disable=too-many-lines
# PYTHON_ARGCOMPLETE_OK
"""Backfill bounty tooling: targets, submission -> delta, ledger payouts.

  bounty.py targets ROOM [--budget SATS]    -> ROOM/targets.csv
  bounty.py build   SUBMISSION              -> ROOM/deltas/<digest>.delta.jsonl.b64
                                               + <digest>.claim.json
  bounty.py merge   CLAIM --claimant NAME [--pr N]
                                            -> re-verifies, appends
                                               ROOM/ledger.jsonl
  bounty.py payout  ROOM [--min SATS] [--fee-rate N] [--change ADDR]
                                            [--replace]
                                            -> prints the unsigned payout txn
  bounty.py paid    ROOM TXID              -> records a completed payout
  bounty.py address ROOM CLAIMANT ADDRESS  -> sets the address on that claimant's
                                    unpaid rows that have none
  bounty.py award --before SHA --repo OWNER/NAME --errors FILE
                                            -> replays every claim on master in
                                               order, recording awards
  bounty.py encode [FILE...] [--lines N]  -> stem.NN.jsonl.b64 (50K lines each)
  bounty.py decode [FILE...]              -> stem.NN.jsonl.b64 back into FILE
                                    (default files: from res/*/*.jsonl.b64)

ROOM is the 43-char room ID. The bounty a claim targets comes from where it
lives: ROOM/submissions/x.jsonl.b64 (build) or ROOM/deltas/x.claim.json (merge).
The published base DAG, fingerprint, and budget are pinned in ROOM/base.lock;
before publication, targets selects the deepest matching merged DAG under
res/ROOM.
The payout address is a "Payout: <address>" line in the PR description (merge
--pr-body).

Stored DAGs are per-line base64, in line-aligned parts. Any command that loads
a DAG accepts FILE or, if FILE is absent, its .b64 parts (or a single FILE.b64).

Builds may use explicitly selected earlier deltas. The merge workflow replays
claims in order, adding each verified delta to the pool used for later claims.
Requires orjson (canonical bytes must be identical everywhere). build and merge
also need rezzy's C library: set REZZY_LIB to the absolute path of librezzy_ffi.
The other commands (targets, payout, paid, encode, decode) do not load it. rezzy
owns every room-version rule (reference hash, redaction, content hash); this
script only checks the results.

A target is a deduplicated prev_events/auth_events ID or m.relates_to event ID
referenced by BASE but absent from it. Event IDs are the room version's
reference hashes, which the base's own m.room.create declares. Submitted
content is kept only when its content hash validates; otherwise it is redacted.
A target is *closed* when it and every prev_events/auth_events link of its
ancestors resolve into the accumulated event pool (m.relates_to finds targets
but does not gate closure: a redacted copy of an event has lost it). Targets are
only the initial missing IDs: closing one means following every link,
recursively, down to events in BASE. Newly revealed gaps are not added as
targets; if a gap is also an initial target, it remains eligible under its own
row.
"""

import argparse
import base64
import csv
import ctypes
import glob
import hashlib
import importlib
import importlib.util
import itertools
import os
import re
import sys
import tempfile

import orjson


def _sibling(name):
    """Load scripts/NAME.py, which sits beside this file but is not a package."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_payout = _sibling("bounty_payout")
_award = _sibling("bounty_award")


def canon(o):
    """Canonical JSON bytes (sorted keys, compact, UTF-8)."""
    return orjson.dumps(o, option=orjson.OPT_SORT_KEYS)


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
        if os.path.islink(src) or not os.path.isfile(src):
            raise SystemExit(f"{src}: not a regular file")
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


NO_HASH_IDS = {"1", "2"}  # their event IDs are not content hashes


class Event(dict):
    """A checked event; `full` says whether its content hash covers its content."""

    full = False


def full_content(ev):
    """Whether EV carries content that its hash covers (not a redacted copy)."""
    return getattr(ev, "full", False)


def room_info(base):
    """(room_id, room_version) as the base DAG itself declares them."""
    room_id = version = None
    for e in base.values():
        room_id = room_id or e.get("room_id")
        if e.get("type") == "m.room.create":
            version = str(e.get("content", {}).get("room_version", "1"))
        if room_id and version:
            break
    if not (room_id and version):
        sys.exit("base has no m.room.create event or room_id")
    if version in NO_HASH_IDS:
        sys.exit(f"room version {version} event IDs are not hashes; unsupported")
    return room_id, version


def rezzy_events(lines, room_version):
    """Describe each canonical JSON line of LINES through rezzy's C ABI.

    rezzy owns the room-version rules (reference hash, redaction, content hash).
    REZZY_LIB must be an absolute path: the working directory may be a PR head.
    """
    path = os.environ.get("REZZY_LIB", "")
    if not os.path.isabs(path) or not os.path.isfile(path):
        sys.exit(
            "set REZZY_LIB to the absolute path of librezzy_ffi "
            "(see README.md, Running the tools locally)"
        )
    lib = ctypes.CDLL(path)
    lib.rezzy_hash_events.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    lib.rezzy_hash_events.restype = ctypes.c_int
    lib.rezzy_free.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t]
    lib.rezzy_free.restype = None
    data = b"\n".join(lines) + b"\n"
    out, out_len = ctypes.POINTER(ctypes.c_uint8)(), ctypes.c_size_t()
    status = lib.rezzy_hash_events(
        room_version.encode(), data, len(data), ctypes.byref(out), ctypes.byref(out_len)
    )
    text = ctypes.string_at(out, out_len.value) if out else b""
    lib.rezzy_free(out, out_len)
    if status != 0:
        sys.exit(f"rezzy failed: {text.decode(errors='replace')[:300]}")
    records = orjson.loads(text)["events"]
    if len(records) != len(lines):
        sys.exit("rezzy returned a different number of records than events")
    return records


def check_event(event, rec, room_id):
    """(Event, None) for a verified event, or (None, reason)."""
    if "error" in rec:
        return None, rec["error"]
    claimed = event.get("event_id") if isinstance(event, dict) else None
    if not isinstance(claimed, str):
        return None, "event has no event_id"
    if claimed != rec["event_id"]:
        return None, f"{claimed}: event_id does not match reference hash"
    room = rec.get("room_id")
    if room != room_id and not (room is None and rec.get("type") == "m.room.create"):
        return None, f"{claimed}: wrong room"
    if rec["content_hash_ok"]:
        kept = Event(event)
        kept.full = True
    else:  # keep only what the room version's redaction rules preserve
        kept = Event(rec["redacted"])
        kept["event_id"] = claimed
    return kept, None


BATCH = 256  # events per rezzy call, which bounds the copies held at once


def check_events(events, base):
    """[(Event, None) | (None, reason)] for each of EVENTS, in order."""
    checked = []
    if not events:
        return checked
    room_id, version = room_info(base)
    for start in range(0, len(events), BATCH):
        end = start + BATCH
        chunk = events[start:end]
        records = rezzy_events([canon(e) for e in chunk], version)
        checked.extend(check_event(e, r, room_id) for e, r in zip(chunk, records))
    return checked


def depth_cap(base):
    """Deepest event in the base: no submitted event may go beyond it."""
    return max(e["depth"] for e in base.values())


def load_delta(path, base):
    """Load and validate one delta; its filename digest covers canonical source rows."""
    events = {}
    digest = hashlib.sha256()
    previous_id = None
    batch = []
    cap = depth_cap(base)

    def validate_batch():
        for checked, err in check_events(batch, base):
            if err:
                sys.exit(f"{path}: {err}")
            eid = checked["event_id"]
            if eid in base:
                sys.exit(f"{path}: delta event {eid} already exists in base")
            if checked["depth"] > cap:
                sys.exit(f"{path}: delta event {eid} is deeper than the base ({cap})")
            previous = events.get(eid)
            if previous is None or (
                full_content(checked) and not full_content(previous)
            ):
                events[eid] = checked
        batch.clear()

    for line in iter_lines(path):
        event = orjson.loads(line)
        eid = event.get("event_id")
        if not isinstance(eid, str) or (previous_id is not None and eid <= previous_id):
            sys.exit(f"{path}: delta events are not sorted by unique event ID")
        previous_id = eid
        digest.update(canon(event) + b"\n")
        batch.append(event)
        if len(batch) >= BATCH:
            validate_batch()
    if batch:
        validate_batch()
    expected = os.path.basename(path).split(".", 1)[0]
    if digest.hexdigest()[:16] != expected:
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
    """Add EVENTS to POOL, preferring a copy whose content is hash-covered."""
    for eid, event in events.items():
        previous = pool.get(eid)
        if previous is None or (full_content(event) and not full_content(previous)):
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
        add_to_pool(pool, load_delta(path, base))
    return pool


def combine_pools(*pools):
    """Combine event pools, preferring a copy whose content is hash-covered."""
    combined = {}
    for pool in pools:
        for eid, event in pool.items():
            previous = combined.get(eid)
            if previous is None or (full_content(event) and not full_content(previous)):
                combined[eid] = event
    return combined


ROOM = re.compile(r"[A-Za-z0-9_-]{43}")
# A name is dot-separated parts of [A-Za-z0-9_@+=-]; if it has a dot, its last
# part must contain a non-digit, so a trailing ".NN" can only be a part number.
_NAME_PART = r"[A-Za-z0-9_@+=-]+"
_NAME_LAST = r"[A-Za-z0-9_@+=-]*[A-Za-z_@+=-][A-Za-z0-9_@+=-]*"
SUBMISSION_NAME = re.compile(
    rf"(?:{_NAME_PART}|(?:{_NAME_PART}\.)+{_NAME_LAST})(\.[0-9]{{2,}})?\.jsonl(\.b64)?"
)


def room_of(path):
    """Room ID in ROOM/<kind>/<file>, the layout of submissions and deltas."""
    room = os.path.abspath(path).split(os.sep)[-3]
    if not ROOM.fullmatch(room):
        sys.exit(f"{path}: expected ROOM/<submissions|deltas>/<file>, got {room!r}")
    return room


def find_base(room, pinned=None):
    """Logical path of ROOM's base DAG under res/ (whole file, parts or .b64).

    pinned names one file; otherwise the greatest depth in
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


def submission_base(path, override=None):
    """(room, base DAG path), respecting an explicit or the published selection."""
    room = room_of(path)
    if override:
        return room, override
    table = read_table(targets_file(room))
    return room, table.get("base") or find_base(room)


TABLE_COLS = ("event_id", "n_refs", "min_child_depth", "prize_sats")
BASE_LOCK_NAME = "base.lock"


def base_lock_path(room):
    """Path to the published room base name, fingerprint, and budget."""
    return f"{room}/{BASE_LOCK_NAME}"


def read_base_lock(room):
    """Return the published (base name, fingerprint, budget), if it exists."""
    path = base_lock_path(room)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="ascii") as f:
            name, fingerprint, budget = f.read().splitlines()
            budget = int(budget)
    except (OSError, ValueError):
        sys.exit(f"{path}: expected base path, SHA-256, and budget on separate lines")
    if (
        not re.fullmatch(rf"res/{room}/merged-{room}-[A-Za-z0-9._-]+\.jsonl", name)
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        or budget < 0
    ):
        sys.exit(f"{path}: invalid base lock")
    return name, fingerprint, budget


def write_base_lock(room, base_name, base, budget):
    """Pin the published room base and its canonical event-set fingerprint."""
    with open(base_lock_path(room), "w", encoding="ascii") as f:
        f.write(f"{base_name}\n{base_fingerprint(base)}\n{budget}\n")


def write_table(path, table):
    """Write the four-column target CSV."""
    with open(path, "w", encoding="utf-8", newline="") as f:
        out = csv.DictWriter(f, fieldnames=TABLE_COLS, lineterminator="\n")
        out.writeheader()
        out.writerows(table["targets"])


def read_table(path, room=None):
    """Read the four-column target CSV and resolve the room's base DAG."""
    with open(path, encoding="utf-8", newline="") as f:
        records = list(csv.reader(f))
    if not records or tuple(records[0]) != TABLE_COLS:
        sys.exit(f"{path}: invalid target table header")
    if any(len(record) != len(TABLE_COLS) for record in records[1:]):
        sys.exit(f"{path}: target rows must have {len(TABLE_COLS)} columns")
    rows = [
        {
            c: (record[i] if c == "event_id" else int(record[i]))
            for i, c in enumerate(TABLE_COLS)
        }
        for record in records[1:]
    ]
    room = room or os.path.basename(os.path.dirname(path))
    lock = read_base_lock(room)
    if lock:
        base = find_base(room, os.path.basename(lock[0]).removesuffix(".jsonl"))
    else:
        base = find_base(room)
    return {"base": base, "targets": rows}


def targets_file(room):
    """Published target table for ROOM; exits if there is no such bounty."""
    path = f"{room}/targets.csv"
    if not os.path.exists(path):
        sys.exit(f"no published bounty for room {room}")
    return path


def relation_ids(e):
    """Event IDs named by content["m.relates_to"] (relation target, reply)."""
    rel = (e.get("content") or {}).get("m.relates_to")
    if not isinstance(rel, dict):
        return []
    reply = rel.get("m.in_reply_to")
    ids = [rel.get("event_id")]
    if isinstance(reply, dict):
        ids.append(reply.get("event_id"))
    valid = (i for i in ids if isinstance(i, str) and i.startswith("$"))
    return list(dict.fromkeys(valid))


def links(e):
    """DAG edges an event depends on (prev_events then auth_events).

    Closure follows only these: redaction keeps them, so every copy of an event
    names the same ones. m.relates_to lives in content, which a redacted copy
    loses, so it can find targets (referenced_ids) but never gates closure.
    """
    return list(e.get("prev_events", [])) + list(e.get("auth_events", []))


def referenced_ids(e):
    """Every event ID an event names: its DAG edges and its relations."""
    return links(e) + relation_ids(e)


def rank_targets(base):
    """Missing IDs, hardest-looking first: fewest references, then oldest.

    n_refs is how many base events reference the target (few means few servers
    are likely to have seen it); min_child_depth is the shallowest depth among
    those referencing events (smaller means older history). Both are
    recomputable from the base DAG.
    """
    seen = {}
    for e in base.values():
        for p in referenced_ids(e):
            if p not in base:
                seen.setdefault(p, {})[e["event_id"]] = e["depth"]
    rows = [
        {"event_id": t, "n_refs": len(d), "min_child_depth": min(d.values())}
        for t, d in seen.items()
    ]
    rows.sort(key=lambda r: (r["n_refs"], r["min_child_depth"], r["event_id"]))
    return rows


def prizes(n, budget, alpha=0):
    """Rank k of n gets budget * k**-alpha / sum(j**-alpha), in whole sats.

    The default alpha=0 splits the budget uniformly. Largest-remainder rounding
    keeps the total exactly equal to budget.
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
    """Require the table and claim to use the base pinned at publication."""
    lock = read_base_lock(room)
    if lock is None:
        sys.exit(f"{base_lock_path(room)} is missing; publish the base lock first")
    expected_base, expected_fingerprint, budget = lock
    if expected_base != base_name or table.get("base") != base_name:
        sys.exit(f"claim must use the published room base {expected_base!r}")
    fingerprint = base_fingerprint(base)
    if fingerprint != expected_fingerprint:
        sys.exit(f"{base_name}: contents do not match the published base lock")
    expected = rank_targets(base)
    actual = [
        {key: row[key] for key in ("event_id", "n_refs", "min_child_depth")}
        for row in table["targets"]
    ]
    if actual != expected:
        sys.exit(f"{targets_file(room)} does not match the selected base DAG")
    paid = [row["prize_sats"] for row in table["targets"]]
    if paid != prizes(len(paid), budget):
        sys.exit(f"{targets_file(room)} prizes are not the published uniform split")
    table["base_sha256"] = fingerprint


def cmd_targets(args):
    """Write ROOM/targets.csv with ranks and prizes."""
    lock = read_base_lock(args.room)
    budget = args.budget if args.budget is not None else lock[2] if lock else None
    if budget is None:
        sys.exit("--budget SATS is required to publish a new table")
    if budget < 0:
        sys.exit("--budget must be non-negative")
    if lock and budget != lock[2] and not args.regenerate:
        sys.exit("changing the published budget requires --regenerate")
    base_name = find_base(args.room) if args.regenerate or lock is None else lock[0]
    if args.base and args.base != base_name:
        sys.exit(f"--base must name the room's published base {base_name!r}")
    base = load(base_name)
    rows = rank_targets(base)
    p = prizes(len(rows), budget)
    path = f"{args.room}/targets.csv"
    target_rows = [{**r, "prize_sats": p[i]} for i, r in enumerate(rows)]
    published = {"targets": target_rows}
    if os.path.exists(path):
        existing = read_table(path)
        differs = existing["targets"] != target_rows or (
            bool(target_rows)
            and sum(row["prize_sats"] for row in existing["targets"]) != budget
        )
        if differs:
            if not args.regenerate:
                sys.exit(
                    f"{path} is already published; refusing to change ranks or prizes"
                )
            write_table(path, published)
            write_base_lock(args.room, base_name, base, budget)
            print(f"regenerated target table for {base_name}")
            return
        write_table(path, existing)
        if args.regenerate or lock is None:
            write_base_lock(args.room, base_name, base, budget)
        print(f"target table verified for {base_name}")
        return
    os.makedirs(args.room, exist_ok=True)
    write_table(path, published)
    write_base_lock(args.room, base_name, base, budget)
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
    lines = enumerate(iter_lines(path), 1)
    try:
        while batch := list(itertools.islice(lines, BATCH)):
            parsed = []  # (line number, event)
            for n, line in batch:
                try:
                    parsed.append((n, orjson.loads(line)))
                except orjson.JSONDecodeError:
                    errs.append(f"line {n}: invalid JSON")
            results = check_events([e for _, e in parsed], base)
            for (n, _), (checked, err) in zip(parsed, results):
                if err:
                    errs.append(f"line {n}: {err}")
                else:
                    add_to_pool(sub, {checked["event_id"]: checked})
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


PAYOUT_LINE = re.compile(r"(?im)^[ \t]*payout(?: address)?[ \t]*:[ \t]*(\S+)[ \t\r]*$")


def payout_from_body(path, network="main"):
    """The one Bitcoin address on a "Payout: <address>" line of the PR body.

    Lines that are not address-shaped (such as the template's placeholder) are
    ignored. None, with a note, if no valid address or two different ones: the
    claim is still recorded, but nothing can be paid yet.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        found = {
            _payout.normalize_address(x)
            for x in PAYOUT_LINE.findall(f.read())
            if _payout.valid_payout_address(x, network)
        }
    if len(found) != 1:
        print("no single valid 'Payout: <address>' line in the PR description")
        return None
    return found.pop()


def verified_submission(path, base):
    """Events of the submission at PATH, exiting with the errors if any are bad."""
    sub, errs = parse_submission(path, base)
    if errs:
        print("\n".join(errs[:50]), file=sys.stderr)
        sys.exit(1)
    return sub


def new_events(sub, base, merged):
    """Events of SUB the base and earlier deltas lack, within the base's depth."""
    cap = depth_cap(base)
    deep = sum(1 for v in sub.values() if v["depth"] > cap)
    if deep:
        print(f"ignoring {deep} events deeper than the base ({cap})")
    return {
        k: v
        for k, v in sub.items()
        if k not in base and k not in merged and v["depth"] <= cap
    }


def cmd_build(args):
    """Validate a submission and write its delta and claim files."""
    name = os.path.basename(args.submission)
    if not SUBMISSION_NAME.fullmatch(name):
        sys.exit(f"{name}: not a valid submission name (see the room README)")
    room, base_name = submission_base(args.submission, args.base)
    base = load(base_name)
    table = read_table(targets_file(room))
    check_published_base(room, base_name, base, table)
    out = f"{room}/deltas"
    sub = verified_submission(args.submission, base)
    merged = load_deltas(
        out,
        room,
        base,
        paths=[] if args.no_prior_deltas else args.prior_delta or None,
        base_name=base_name,
    )
    delta = new_events(sub, base, merged)
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
    claim_stem = os.path.basename(args.claim)
    match = re.fullmatch(r"([0-9a-f]{16})\.claim\.json", claim_stem)
    if not match:
        sys.exit(f"{args.claim}: bad claim filename")
    digest = match.group(1)
    expected_delta = f"{room}/deltas/{digest}.delta.jsonl.b64"
    if not re.fullmatch(DELTA_PATH.format(room=room), claim.get("delta", "")):
        sys.exit(f"{args.claim}: bad delta path {claim.get('delta')!r}")
    if claim["delta"] != expected_delta:
        sys.exit(f"{args.claim}: delta must be its sibling {expected_delta!r}")
    if claim.get("delta_sha256_16") != digest:
        sys.exit(f"{args.claim}: claim digest does not match its filename")
    base_name = args.base or claim["base"]
    if not re.fullmatch(rf"res/{room}/merged-{room}-[A-Za-z0-9._-]+\.jsonl", base_name):
        sys.exit(f"{args.claim}: bad base {base_name!r}")
    return base_name


def trusted_table(args, room, base_name, base):
    """The published target table, checked against the base and the claim push."""
    table = read_table(args.trusted_targets or targets_file(room), room)
    if args.trusted_targets and table != read_table(targets_file(room)):
        sys.exit(f"{targets_file(room)} changed in the claim push")
    if args.trusted_base_lock:
        with (
            open(args.trusted_base_lock, "rb") as trusted,
            open(base_lock_path(room), "rb") as current,
        ):
            if trusted.read() != current.read():
                sys.exit(f"{base_lock_path(room)} changed in the claim push")
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
                "address": args.payout,
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
    current = load_delta(claim["delta"], base)
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
    closed = newly_closed(base, prior, current, tg)
    if args.no_awards:
        return
    if not args.claimant:
        sys.exit("--claimant is required unless --no-awards is set")
    args.payout = (
        payout_from_body(args.pr_body, _payout.room_network(room))
        if args.pr_body
        else None
    )
    append_awards(args, claim, tg, closed)


def chunked(items, size):
    """Yield lists of up to SIZE items."""
    if size < 1:
        raise ValueError("batch size must be at least one")
    it = iter(items)
    while chunk := list(itertools.islice(it, size)):
        yield chunk


def write_parts(stem, lines, size):
    """Write lines as base64 STEM.NN.jsonl.b64 parts; return line counts."""
    counts = []
    parent = os.path.dirname(stem) or "."
    os.makedirs(parent, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".b64-parts-", dir=parent) as tempdir:
        temp_paths = []
        for i, chunk in enumerate(chunked(lines, size)):
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


def add_ledger_commands(sp):
    """Register the commands that read or update a room's ledger."""
    s = sp.add_parser("payout")
    s.add_argument("room")
    s.add_argument("--min", type=int, default=1000, help="minimum sats per payee")
    s.add_argument("--fee-rate", type=int, default=5, help="sat/vB")
    s.add_argument("--change")
    s.add_argument("--address", action="append", default=[], metavar="WHO=ADDR")
    s.add_argument(
        "--replace",
        action="store_true",
        help="replace an unbroadcast pending payout batch",
    )
    s.set_defaults(f=_payout.cmd_payout)
    s = sp.add_parser("paid")
    s.add_argument("room")
    s.add_argument("txid")
    s.set_defaults(f=_payout.cmd_paid)
    s = sp.add_parser("address")
    s.add_argument("room")
    s.add_argument("claimant")
    s.add_argument("payout", metavar="ADDRESS")
    s.set_defaults(f=_payout.cmd_address)


def main():
    """Parse the command line and dispatch."""
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(required=True)
    s = sp.add_parser("targets")
    s.add_argument("room")
    s.add_argument("--budget", type=int)
    s.add_argument("--base")
    s.add_argument("--regenerate", action="store_true")
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
    s.add_argument("--claimant")
    s.add_argument(
        "--pr-body",
        metavar="FILE",
        help="file holding the PR description, which names the payout address",
    )
    s.add_argument(
        "--no-awards",
        action="store_true",
        help="validate and load a claim delta without recording prizes",
    )
    s.add_argument("--base")
    s.add_argument("--trusted-targets")
    s.add_argument("--trusted-base-lock")
    s.add_argument(
        "--prior-delta",
        action="append",
        default=[],
        help="previous claim delta, in merge order; repeat for each prior claim",
    )
    s.set_defaults(f=cmd_merge)
    add_ledger_commands(sp)
    s = sp.add_parser("award")
    s.add_argument("--before", required=True, help="trusted commit before the push")
    s.add_argument("--repo", required=True, help="OWNER/NAME on GitHub")
    s.add_argument("--errors", required=True, metavar="FILE")
    s.set_defaults(f=_award.cmd_award)
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
