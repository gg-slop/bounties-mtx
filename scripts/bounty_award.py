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

    def history(self):
        """Every claim added on master's first-parent history, oldest first."""
        out = self._run(
            ["git", "log", "--first-parent", "--reverse", "--diff-filter=A"]
            + ["--format=COMMIT%x09%H", "--name-only", "--", "*/deltas/*.claim.json"]
        )
        return parse_claim_history(out.decode())

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
        if pr is None:
            return None, None
        try:
            who = self.remote.pr_author(pr)
        except (subprocess.CalledProcessError, OSError):
            return None, f"could not resolve claimant for PR #{pr}"
        try:
            body = self.remote.pr_body(pr)
        except (subprocess.CalledProcessError, OSError):
            return None, f"could not read the description of PR #{pr}"
        return {"number": pr, "author": who, "body": body}, None

    def one(self, commit, claim):
        """Resolve one claim."""
        room = claim.split("/", 1)[0]
        key = claim_key(commit, claim)
        data = self.remote.read_claim(claim)
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

    def run(self, history):
        """Resolve HISTORY [(commit, claim)]; return the resolved document."""
        self.history = [list(h) for h in history]
        for commit, claim in history:
            self.one(commit, claim)
        return {"history": self.history, "rooms": self.rooms, "claims": self.claims}


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
        data = self.src.read_claim(claim)
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


def stage(out, errors, root="."):
    """Copy every room ledger under ROOT to OUT, with the errors, for publish."""
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


def check_new_rows(room, tail):
    """Exit unless TAIL is a run of well-formed unpaid ledger rows."""
    if tail and not tail.endswith(b"\n"):
        sys.exit(f"{room}/ledger.jsonl: appended rows must end with a newline")
    for line in tail.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            sys.exit(f"{room}/ledger.jsonl: appended row is not JSON")
        ok = isinstance(row, dict) and set(row) == LEDGER_KEYS
        ok = ok and row["status"] == "unpaid" and isinstance(row["prize_sats"], int)
        if not ok:
            sys.exit(f"{room}/ledger.jsonl: appended row is not a new unpaid award")


def publish(src, root="."):
    """Validate the staged files in SRC and append their new rows under ROOT.

    SRC came from a job that ran rezzy, so treat it as untrusted: accept only
    `complete`, `errors.txt` and <ROOM>/ledger.jsonl regular files, for rooms
    that exist here, and only as append-only extensions of the current ledger
    consisting of new unpaid rows. Returns (rooms changed, sanitized errors).
    """
    if not os.path.isfile(os.path.join(src, "complete")):
        sys.exit(f"{src}: missing completion marker; the replay did not finish")
    updates = []
    for name in sorted(os.listdir(src)):
        path = os.path.join(src, name)
        if os.path.islink(path):
            sys.exit(f"{path}: symlinks are not accepted")
        if name in ("complete", "errors.txt"):
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
    changed = []
    for room, ledger in updates:
        current = os.path.join(root, room, "ledger.jsonl")
        old = b""
        if os.path.exists(current):
            with open(current, "rb") as f:
                old = f.read()
        with open(ledger, "rb") as f:
            new = f.read()
        if not new.startswith(old):
            sys.exit(f"{room}/ledger.jsonl: staged ledger must extend the current one")
        if new == old:
            continue
        check_new_rows(room, new[len(old) :])  # noqa: E203
        temp = f"{current}.tmp"
        with open(temp, "wb") as f:
            f.write(new)
        os.replace(temp, current)
        changed.append(room)
    return changed, sanitized_errors(os.path.join(src, "errors.txt"))


def sanitized_errors(path):
    """Error lines safe to print in a CI log (no line can start a command)."""
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()[:MAX_ERROR_LINES]
    clean = (re.sub(r"[^\x20-\x7e]", "?", line)[:500] for line in lines)
    return ["claim error: " + line for line in clean]


def cmd_resolve(args):
    """Phase 1: write the resolved metadata document."""
    remote = Remote(args.before, args.repo)
    doc = Resolver(remote).run(remote.history())
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f)


def cmd_replay(args):
    """Phase 2: replay claims with rezzy and stage the resulting ledgers."""
    resolved = load_resolved(args.resolved)
    with tempfile.TemporaryDirectory() as tmp:
        errors = Awarder(Local(), resolved, tmp).run()
    stage(args.out, errors)


def cmd_publish(args):
    """Phase 3: validate staged ledgers into the checkout; write the error list."""
    changed, errors = publish(args.src)
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
    s.add_argument("--errors", required=True, metavar="FILE")
    s.set_defaults(f=cmd_publish)
    args = ap.parse_args(argv)
    args.f(args)


if __name__ == "__main__":
    main()
