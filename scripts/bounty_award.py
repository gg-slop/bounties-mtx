#!/usr/bin/env python3
"""Record bounty awards from claims merged to master, in three isolated phases.

  resolve  git + GitHub metadata -> resolved.json     (token; runs no rezzy)
  replay   resolved.json + rezzy -> staged ledgers    (no token; runs rezzy)
  publish  staged ledgers -> checkout                 (write token; no rezzy)

Each phase runs in its own workflow job and hands the next only a data file, so
code built from rezzy never shares a job with a credential. This module uses
only the standard library (publish needs nothing installed) and all I/O goes
through Remote/Local objects, so the ordering and deferral rules are tested
with fakes.
"""

import argparse
import base64
import binascii
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOM = re.compile(r"[A-Za-z0-9_-]{43}")
CLAIM_PATH = re.compile(r"([A-Za-z0-9_-]{43})/deltas/([0-9a-f]+)\.claim\.json")
HERE = os.path.dirname(os.path.abspath(__file__))
TRUSTED = (("targets.csv", "target table"), ("base.lock", "base lock"))
LEDGER_KEYS = {"target", "claimant", "address", "pr", "delta", "prize_sats", "status"}
MAX_ERROR_LINES = 1000
MAX_ERROR_BYTES = 1 << 20
PROCESSED_PATH = "bounty-processed.json"


def parse_claim_history(text):
    """[(commit, claim path)] from `git log --format=COMMIT<TAB><sha> --name-only`."""
    history, commit = [], None
    for line in text.splitlines():
        if line.startswith("COMMIT\t"):
            commit = line.split("\t", 1)[1]
        elif commit and CLAIM_PATH.fullmatch(line):
            history.append((commit, line))
    return history


def pr_for_commit(prs):
    """Number of the first PR into master among PRS, else None; raises if malformed."""
    if not isinstance(prs, list):
        raise ValueError("expected an array")
    for pr in prs:
        if pr["base"]["ref"] == "master":
            return str(pr["number"])
    return None


def sibling_problem(claim, path):
    """Why CLAIM's embedded delta path/digest is unusable, or None."""
    match = CLAIM_PATH.fullmatch(path)
    delta = claim.get("delta") if isinstance(claim, dict) else None
    digest = claim.get("delta_sha256_16") if isinstance(claim, dict) else None
    if match is None or not (isinstance(delta, str) and isinstance(digest, str)):
        return "missing or invalid embedded delta path/digest"
    room, name = match.group(1), match.group(2)
    if delta != f"{room}/deltas/{name}.delta.jsonl.b64" or digest != name:
        return "embedded delta path/digest does not match its sibling"
    return None


def claim_key(commit, claim):
    """Key identifying one claim as added by one commit."""
    return f"{commit}\t{claim}"


class Local:
    """Reads the checkout and runs merge; used by resolve and replay."""

    def read_claim(self, path):
        """The claim file's JSON, or None if it no longer exists."""
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return json.load(f)

    def merge(self, argv):
        """Run `bounty.py merge ARGV` without any GitHub token; True on success."""
        env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
        cmd = [sys.executable, "-I", os.path.join(HERE, "bounty.py"), "merge"] + argv
        return subprocess.run(cmd, env=env, check=False).returncode == 0


class Remote(Local):
    """The real git and GitHub lookups for the resolve phase."""

    def __init__(self, before, repo):
        self.before, self.repo = before, repo

    @staticmethod
    def _run(cmd):
        return subprocess.run(cmd, check=True, capture_output=True).stdout

    def history(self, processed=()):
        """Unprocessed claims on master's first-parent history, oldest first."""
        out = self._run(
            ["git", "log", "--first-parent", "--reverse", "--diff-filter=A"]
            + ["--format=COMMIT%x09%H", "--name-only", "--", "*/deltas/*.claim.json"]
        )
        known = {entry["claim"] for entry in processed}
        return [
            item for item in parse_claim_history(out.decode()) if item[1] not in known
        ]

    def trusted_file(self, room, name):
        """Bytes of ROOM/NAME as of before the push, or None."""
        try:
            return self._run(["git", "show", f"{self.before}:{room}/{name}"])
        except subprocess.CalledProcessError:
            return None

    def prs(self, commit):
        """PRs associated with COMMIT; raises on failure."""
        out = self._run(["gh", "api", f"repos/{self.repo}/commits/{commit}/pulls"])
        return json.loads(out)

    def pr_author(self, pr):
        """Login of the PR's author; raises on failure."""
        cmd = ["gh", "pr", "view", pr, "--repo", self.repo]
        cmd += ["--json", "author", "--jq", ".author.login"]
        return self._run(cmd).decode().strip()

    def pr_body(self, pr):
        """Description of the PR; raises on failure."""
        cmd = ["gh", "pr", "view", pr, "--repo", self.repo]
        cmd += ["--json", "body", "--jq", ".body"]
        return self._run(cmd).decode()


class Resolver:
    """Phase 1: look up everything replay needs that requires a credential."""

    def __init__(self, remote, log=print):
        self.remote, self.log = remote, log
        self.rooms, self.claims, self.history = {}, {}, []
        self.blocked = set()
        self.pr_cache = {}

    def fail(self, room, key, message):
        """Record an unresolvable claim; later claims in ROOM are deferred."""
        self.claims[key] = {"error": message, "pr": None}
        self.blocked.add(room)
        self.log(f"{key.split(chr(9))[1]}: {message}")

    def room_files(self, room):
        """Cache the pre-push targets and base lock; an error message if missing."""
        if room in self.rooms:
            return None
        files = {}
        for name, label in TRUSTED:
            data = self.remote.trusted_file(room, name)
            if data is None:
                return f"no published {label} before this push"
            files[name] = base64.b64encode(data).decode()
        self.rooms[room] = files
        return None

    def attribution(self, commit):
        """(pr dict or None, error or None) for the PR that merged COMMIT."""
        try:
            pr = pr_for_commit(self.remote.prs(commit))
        except (subprocess.CalledProcessError, OSError):
            return None, f"could not resolve PR for {commit}"
        except (ValueError, KeyError, TypeError):
            return None, f"could not parse PR attribution response for {commit}"
        return (None, None) if pr is None else self.pr_details(pr)

    def pr_details(self, pr):
        """(pr dict, error or None) for PR number PR; looked up once per PR."""
        if pr in self.pr_cache:
            return self.pr_cache[pr], None
        try:
            who = self.remote.pr_author(pr)
        except (subprocess.CalledProcessError, OSError):
            return None, f"could not resolve claimant for PR #{pr}"
        try:
            body = self.remote.pr_body(pr)
        except (subprocess.CalledProcessError, OSError):
            return None, f"could not read the description of PR #{pr}"
        self.pr_cache[pr] = {"number": pr, "author": who, "body": body}
        return self.pr_cache[pr], None

    def one(self, commit, claim):
        """Resolve one claim."""
        room = claim.split("/", 1)[0]
        key = claim_key(commit, claim)
        try:
            data = self.remote.read_claim(claim)
        except (OSError, UnicodeError, ValueError):
            self.fail(room, key, "claim file is unreadable or malformed JSON")
            return
        if data is None or room in self.blocked or sibling_problem(data, claim):
            return
        error = self.room_files(room)
        pr = None
        if error is None:
            pr, error = self.attribution(commit)
        if error:
            self.fail(room, key, error)
        else:
            self.claims[key] = {"error": None, "pr": pr}

    def run(self, history, processed=()):
        """Resolve unprocessed HISTORY and retain prior verified deltas."""
        self.history = [list(h) for h in history]
        for commit, claim in history:
            self.one(commit, claim)
        return {
            "history": self.history,
            "rooms": self.rooms,
            "claims": self.claims,
            "processed": list(processed),
        }


def load_processed(path=PROCESSED_PATH):
    """Load and validate the durable list of successfully replayed claims."""
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            raise ValueError("expected a list")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"claim", "delta"}:
                raise ValueError("invalid entry")
            claim, delta = entry["claim"], entry["delta"]
            if not isinstance(claim, str) or not isinstance(delta, str):
                raise ValueError("invalid claim or delta")
            if not CLAIM_PATH.fullmatch(claim):
                raise ValueError(f"invalid claim path {claim!r}")
            expected = claim.replace(".claim.json", ".delta.jsonl.b64")
            if delta != expected or claim in seen:
                raise ValueError(f"invalid or duplicate claim {claim!r}")
            seen.add(claim)
        return entries
    except (OSError, UnicodeError, ValueError) as exc:
        sys.exit(f"{path}: invalid processed claim state: {exc}")


def valid_claim(commit, claim):
    """Whether COMMIT is a full SHA and CLAIM a claim file path."""
    return bool(re.fullmatch(r"[0-9a-f]{40}", commit) and CLAIM_PATH.fullmatch(claim))


def load_resolved(path):
    """Read and strictly validate a resolved document; exit if it is malformed."""
    with open(path, "rb") as f:
        doc = json.load(f)
    try:
        for commit, claim in doc["history"]:
            if not valid_claim(commit, claim):
                raise ValueError(f"bad history entry {commit!r} {claim!r}")
        for room, files in doc["rooms"].items():
            if not ROOM.fullmatch(room) or set(files) != {n for n, _ in TRUSTED}:
                raise ValueError(f"bad room entry {room!r}")
            for text in files.values():
                base64.b64decode(text, validate=True)
        for key, entry in doc["claims"].items():
            commit, claim = key.split("\t", 1)
            if not valid_claim(commit, claim):
                raise ValueError(f"bad claim key {key!r}")
            pr = entry["pr"]
            if entry["error"] is not None and not isinstance(entry["error"], str):
                raise ValueError("bad claim error")
            if pr is not None and not (
                re.fullmatch(r"[0-9]+", pr["number"])
                and isinstance(pr["author"], str)
                and isinstance(pr["body"], str)
            ):
                raise ValueError(f"bad PR entry for {key!r}")
        for entry in doc["processed"]:
            claim, delta = entry["claim"], entry["delta"]
            if not isinstance(claim, str) or not isinstance(delta, str):
                raise ValueError("bad processed claim entry")
            if not CLAIM_PATH.fullmatch(claim) or delta != claim.replace(
                ".claim.json", ".delta.jsonl.b64"
            ):
                raise ValueError("bad processed claim path")
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        sys.exit(f"{path}: malformed resolved metadata: {exc}")
    return doc


class Awarder:
    """Phase 2: replay claims in order; collect errors; defer unattributable rooms."""

    def __init__(self, local, resolved, tmp, log=print):
        self.src, self.res, self.tmp, self.log = local, resolved, tmp, log
        self.errors = []
        self.blocked = set()
        self.prior = {}
        for entry in resolved["processed"]:
            room = entry["claim"].split("/", 1)[0]
            self.prior.setdefault(room, []).append(entry["delta"])
        self.completed = list(resolved["processed"])

    def error(self, claim, message):
        """Record and print an error for CLAIM."""
        line = f"{claim}: {message}"
        self.errors.append(line)
        self.log(line)

    def block(self, room, claim, message):
        """Record an error that also defers later claims in ROOM."""
        self.error(claim, message)
        self.blocked.add(room)

    def write_trusted(self, room):
        """Write ROOM's pre-push targets and base lock to files; return paths."""
        paths = []
        for name, _ in TRUSTED:
            path = os.path.join(self.tmp, f"{room}-{name}")
            with open(path, "wb") as f:
                f.write(base64.b64decode(self.res["rooms"][room][name]))
            paths.append(path)
        return paths

    def merge_args(self, claim, room, pr):
        """Command-line arguments for merging CLAIM."""
        argv = [claim]
        if pr is None:
            argv.append("--no-awards")
        else:
            body = os.path.join(self.tmp, f"pr-{pr['number']}-body.txt")
            with open(body, "w", encoding="utf-8") as f:
                f.write(pr["body"])
            argv += [f"--pr={pr['number']}", f"--claimant={pr['author']}"]
            argv.append(f"--pr-body={body}")
        paths = self.write_trusted(room)
        argv += [f"--trusted-targets={paths[0]}", f"--trusted-base-lock={paths[1]}"]
        return argv + [f"--prior-delta={d}" for d in self.prior.get(room, [])]

    def one(self, commit, claim):
        """Process one claim."""
        room = claim.split("/", 1)[0]
        try:
            data = self.src.read_claim(claim)
        except (OSError, UnicodeError, ValueError):
            self.block(room, claim, "claim file is unreadable or malformed JSON")
            return
        if data is None:
            return
        if room in self.blocked:
            msg = "deferred because an earlier claim in {} could not be attributed"
            self.error(claim, msg.format(room))
            return
        problem = sibling_problem(data, claim)
        if problem:
            self.error(claim, problem)
            return
        entry = self.res["claims"].get(claim_key(commit, claim))
        if entry is None:
            self.block(room, claim, "no resolved metadata for this claim")
        elif entry["error"]:
            self.block(room, claim, entry["error"])
        elif room not in self.res["rooms"]:
            self.block(room, claim, "no resolved target table or base lock")
        else:
            self.award(claim, room, entry["pr"], data["delta"])

    def award(self, claim, room, pr, delta):
        """Merge one resolved CLAIM and record its delta for later claims."""
        if self.src.merge(self.merge_args(claim, room, pr)):
            self.prior.setdefault(room, []).append(delta)
            self.completed.append({"claim": claim, "delta": delta})
            if pr is None:
                self.log(f"{claim}: verified delta has no master PR; skipping prizes")
        elif pr is None:
            self.error(claim, "claim validation failed without PR attribution")
        else:
            self.error(claim, "merge validation failed")

    def run(self):
        """Process the resolved history in order; return the error list."""
        for commit, claim in self.res["history"]:
            self.one(commit, claim)
        return self.errors


def stage(out, errors, processed, root="."):
    """Copy room ledgers, errors, and processed claim state for publish."""
    os.makedirs(out, exist_ok=True)
    for name in sorted(os.listdir(root)):
        src = os.path.join(root, name, "ledger.jsonl")
        if ROOM.fullmatch(name) and os.path.isfile(src) and not os.path.islink(src):
            os.makedirs(os.path.join(out, name), exist_ok=True)
            shutil.copyfile(src, os.path.join(out, name, "ledger.jsonl"))
    with open(os.path.join(out, "errors.txt"), "w", encoding="utf-8") as f:
        f.write("".join(e.replace("\n", " ") + "\n" for e in errors))
    with open(os.path.join(out, "complete"), "w", encoding="utf-8") as f:
        f.write("ok\n")
    with open(os.path.join(out, "processed.json"), "w", encoding="utf-8") as f:
        json.dump(processed, f)


def read_prizes(room, path):
    """{event_id: prize_sats} from the published target table at PATH."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            rows = csv.DictReader(f)
            if rows.fieldnames != [
                "event_id",
                "n_refs",
                "min_child_depth",
                "prize_sats",
            ]:
                raise ValueError("invalid target table header")
            prizes = {}
            for row in rows:
                target, prize = row["event_id"], int(row["prize_sats"])
                if not target or target in prizes or prize < 0:
                    raise ValueError("invalid or duplicate target")
                prizes[target] = prize
            return prizes
    except (OSError, KeyError, ValueError) as exc:
        sys.exit(f"{room}/targets.csv: unreadable target table: {exc}")


def trusted_claims(resolved):
    """({PR number: pr dict}, {delta paths}) from the resolved metadata."""
    prs = {e["pr"]["number"]: e["pr"] for e in resolved["claims"].values() if e["pr"]}
    deltas = {
        c.replace(".claim.json", ".delta.jsonl.b64") for _, c in resolved["history"]
    }
    return prs, deltas


def row_problem(row, prizes, prs, deltas):
    """Why ROW is not a legitimate new award, or None."""
    if not (isinstance(row, dict) and set(row) == LEDGER_KEYS):
        return "unexpected fields"
    target = row["target"]
    if not isinstance(target, str) or target not in prizes:
        return "target is not published"
    claimant, address, pr_number, delta = (
        row["claimant"],
        row["address"],
        row["pr"],
        row["delta"],
    )
    prize = row["prize_sats"]
    if not (
        isinstance(claimant, str)
        and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", claimant)
        and (address is None or isinstance(address, str) and bool(address))
        and isinstance(pr_number, str)
        and pr_number.isdigit()
        and isinstance(delta, str)
    ):
        return "non-string field"
    pr = prs.get(pr_number)
    payout_lines = re.findall(
        r"(?im)^[ \t]*payout(?: address)?[ \t]*:[ \t]*(\S+)[ \t\r]*$",
        pr["body"] if pr else "",
    )
    addresses = {
        value.lower() if value.lower().startswith(("bc1", "tb1", "bcrt1")) else value
        for value in payout_lines
        if re.fullmatch(
            r"(?:bc1|tb1|bcrt1)[a-zA-Z0-9]{20,87}|[13][1-9A-HJ-NP-Za-km-z]{25,34}",
            value,
        )
    }
    expected_address = next(iter(addresses)) if len(addresses) == 1 else None
    checks = (
        (row["status"] == "unpaid", "not unpaid"),
        (type(prize) is int, "bad prize"),
        (prize == prizes[target], "prize is not the published one"),
        (pr and claimant == pr["author"], "claimant is not the PR author"),
        (
            address == expected_address,
            "address is not the single payout address in the PR",
        ),
        (delta in deltas, "delta is not a claimed delta"),
    )
    return next((why for ok, why in checks if not ok), None)


def check_new_rows(room, tail, old, trusted):
    """Exit unless TAIL is a run of new unpaid awards that TRUSTED data backs.

    TRUSTED is (prizes, prs, deltas); OLD is the current ledger's bytes.
    """
    if tail and not tail.endswith(b"\n"):
        sys.exit(f"{room}/ledger.jsonl: appended rows must end with a newline")
    seen = {json.loads(line)["target"] for line in old.splitlines()}
    for line in tail.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            sys.exit(f"{room}/ledger.jsonl: appended row is not JSON")
        problem = row_problem(row, *trusted)
        if problem is None and not row["delta"].startswith(f"{room}/deltas/"):
            problem = "delta belongs to a different room"
        if problem is None and row["target"] in seen:
            problem = "target already awarded"
        if problem:
            sys.exit(f"{room}/ledger.jsonl: rejected appended row: {problem}")
        seen.add(row["target"])


def append_ledger(room, staged, root, claims):
    """Replace ROOM's ledger with STAGED if it validly extends it; True if changed."""
    current = os.path.join(root, room, "ledger.jsonl")
    old = b""
    if os.path.exists(current):
        with open(current, "rb") as f:
            old = f.read()
    with open(staged, "rb") as f:
        new = f.read()
    if not new.startswith(old):
        sys.exit(f"{room}/ledger.jsonl: staged ledger must extend the current one")
    if new == old:
        return False
    prizes = read_prizes(room, os.path.join(root, room, "targets.csv"))
    check_new_rows(room, new[len(old) :], old, (prizes, *claims))  # noqa: E203
    temp = f"{current}.tmp"
    with open(temp, "wb") as f:
        f.write(new)
    os.replace(temp, current)
    return True


def publish(src, resolved, root="."):
    """Validate the staged files in SRC and append their new rows under ROOT.

    SRC came from a job that ran rezzy, so treat it as untrusted: accept only
    `complete`, `errors.txt` and <ROOM>/ledger.jsonl regular files, for rooms
    that exist here, and only as append-only extensions of the current ledger
    consisting of new unpaid rows that the RESOLVED metadata and the published
    target table back (prize, claimant, address, delta, no repeated target).
    Returns (rooms changed, sanitized errors).
    """
    if not os.path.isfile(os.path.join(src, "complete")):
        sys.exit(f"{src}: missing completion marker; the replay did not finish")
    trusted = trusted_claims(resolved)
    old_processed = load_processed(os.path.join(root, PROCESSED_PATH))
    if old_processed != resolved["processed"]:
        sys.exit("processed claim state changed after resolution")
    processed_path = os.path.join(src, "processed.json")
    if not os.path.isfile(processed_path) or os.path.islink(processed_path):
        sys.exit(f"{processed_path}: missing or invalid processed claim state")
    staged_processed = load_processed(processed_path)
    if staged_processed[: len(old_processed)] != old_processed:
        sys.exit("staged processed claim state does not extend the current state")
    candidates = {}
    for commit, claim in resolved["history"]:
        entry = resolved["claims"].get(claim_key(commit, claim))
        if entry and entry["error"] is None:
            candidates[claim] = claim.replace(".claim.json", ".delta.jsonl.b64")
    for entry in staged_processed[len(old_processed) :]:  # noqa: E203
        if candidates.get(entry["claim"]) != entry["delta"]:
            sys.exit("staged processed state contains a claim not resolved in this run")
    updates = []
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if os.path.islink(path):
            sys.exit(f"{path}: symlinks are not accepted")
        if name in ("complete", "errors.txt", "processed.json"):
            if not os.path.isfile(path):
                sys.exit(f"{path}: expected a regular file")
            continue
        ledger = os.path.join(path, "ledger.jsonl")
        valid = ROOM.fullmatch(name) and os.path.isdir(path)
        valid = valid and os.listdir(path) == ["ledger.jsonl"]
        valid = valid and os.path.isfile(ledger) and not os.path.islink(ledger)
        if not valid:
            sys.exit(f"{path}: unexpected entry in the staged output")
        if not os.path.isfile(os.path.join(root, name, "targets.csv")):
            sys.exit(f"{name}: not a published bounty room here")
        updates.append((name, ledger))
    changed = [r for r, ledger in updates if append_ledger(r, ledger, root, trusted)]
    if staged_processed != old_processed:
        temp = f"{os.path.join(root, PROCESSED_PATH)}.tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(staged_processed, f)
        os.replace(temp, os.path.join(root, PROCESSED_PATH))
    return changed, sanitized_errors(os.path.join(src, "errors.txt"))


def sanitized_errors(path):
    """Error lines safe to print in a CI log (no line can start a command)."""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read(MAX_ERROR_BYTES).splitlines()[:MAX_ERROR_LINES]
    clean = (re.sub(r"[^\x20-\x7e]", "?", line)[:500] for line in lines)
    return ["claim error: " + line for line in clean]


def cmd_resolve(args):
    """Phase 1: write the resolved metadata document."""
    remote = Remote(args.before, args.repo)
    processed = load_processed()
    doc = Resolver(remote).run(remote.history(processed), processed)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f)


def cmd_replay(args):
    """Phase 2: replay claims with rezzy and stage the resulting ledgers."""
    resolved = load_resolved(args.resolved)
    with tempfile.TemporaryDirectory() as tmp:
        awarder = Awarder(Local(), resolved, tmp)
        errors = awarder.run()
    stage(args.out, errors, awarder.completed)


def cmd_publish(args):
    """Phase 3: validate staged ledgers into the checkout; write the error list."""
    changed, errors = publish(args.src, load_resolved(args.resolved))
    with open(args.errors, "w", encoding="utf-8") as f:
        f.write("".join(e + "\n" for e in errors))
    print(f"updated ledgers: {', '.join(changed) or 'none'}")


def main(argv=None):
    """Parse the command line and run one phase."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", maxsplit=1)[0])
    sp = ap.add_subparsers(required=True)
    s = sp.add_parser("resolve")
    s.add_argument("--before", required=True, help="trusted commit before the push")
    s.add_argument("--repo", required=True, help="OWNER/NAME on GitHub")
    s.add_argument("--out", required=True, metavar="FILE")
    s.set_defaults(f=cmd_resolve)
    s = sp.add_parser("replay")
    s.add_argument("--resolved", required=True, metavar="FILE")
    s.add_argument("--out", required=True, metavar="DIR")
    s.set_defaults(f=cmd_replay)
    s = sp.add_parser("publish")
    s.add_argument("--src", required=True, metavar="DIR")
    s.add_argument("--resolved", required=True, metavar="FILE")
    s.add_argument("--errors", required=True, metavar="FILE")
    s.set_defaults(f=cmd_publish)
    args = ap.parse_args(argv)
    args.f(args)


if __name__ == "__main__":
    main()
