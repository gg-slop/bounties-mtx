"""Replay merged bounty claims in merge order and record their awards.

All I/O (git, GitHub, the merge subprocess) goes through a Source object, so
the ordering and blocking rules can be tested with a fake.
"""

import json
import os
import re
import subprocess
import sys
import tempfile

CLAIM_PATH = re.compile(r"([A-Za-z0-9_-]+)/deltas/([0-9a-f]+)\.claim\.json")
HERE = os.path.dirname(os.path.abspath(__file__))


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
    if not (isinstance(delta, str) and isinstance(digest, str)):
        return "missing or invalid embedded delta path/digest"
    room, name = match.group(1), match.group(2)
    if delta != f"{room}/deltas/{name}.delta.jsonl.b64" or digest != name:
        return "embedded delta path/digest does not match its sibling"
    return None


class Source:
    """The real git/GitHub/merge backend. Tests substitute their own."""

    def __init__(self, before, repo):
        self.before, self.repo = before, repo

    @staticmethod
    def _run(cmd, env=None):
        return subprocess.run(
            cmd, check=True, capture_output=True, env=env, text=False
        ).stdout

    def history(self):
        """Every claim added on master's first-parent history, oldest first."""
        out = self._run(
            ["git", "log", "--first-parent", "--reverse", "--diff-filter=A"]
            + ["--format=COMMIT%x09%H", "--name-only", "--", "*/deltas/*.claim.json"]
        )
        return parse_claim_history(out.decode())

    def read_claim(self, path):
        """The claim file's JSON, or None if it no longer exists."""
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return json.load(f)

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

    def merge(self, argv):
        """Run `bounty.py merge ARGV` without the GitHub token; True on success."""
        env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
        cmd = [sys.executable, "-I", os.path.join(HERE, "bounty.py"), "merge"] + argv
        return subprocess.run(cmd, env=env, check=False).returncode == 0


class Awarder:
    """Replays claims in order; collects errors; defers rooms it cannot attribute."""

    def __init__(self, source, tmp, log=print):
        self.src, self.tmp, self.log = source, tmp, log
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

    def trusted_paths(self, room, claim):
        """Write the pre-push targets and base lock to files; None if missing."""
        paths = []
        for name, label in (
            ("targets.csv", "target table"),
            ("base.lock", "base lock"),
        ):
            data = self.src.trusted_file(room, name)
            if data is None:
                self.block(room, claim, f"no published {label} before this push")
                return None
            path = os.path.join(self.tmp, f"{room}-{name}")
            with open(path, "wb") as f:
                f.write(data)
            paths.append(path)
        return paths

    def attribution(self, room, claim, commit):
        """('none',) / ('pr', number, author, body path), or None after an error."""
        try:
            pr = pr_for_commit(self.src.prs(commit))
        except (subprocess.CalledProcessError, OSError):
            self.block(room, claim, f"could not resolve PR for {commit}")
            return None
        except (ValueError, KeyError, TypeError):
            msg = f"could not parse PR attribution response for {commit}"
            self.block(room, claim, msg)
            return None
        if pr is None:
            return ("none",)
        try:
            who = self.src.pr_author(pr)
        except (subprocess.CalledProcessError, OSError):
            self.block(room, claim, f"could not resolve claimant for PR #{pr}")
            return None
        try:
            body = self.src.pr_body(pr)
        except (subprocess.CalledProcessError, OSError):
            msg = f"could not read the description of PR #{pr}"
            self.block(room, claim, msg)
            return None
        path = os.path.join(self.tmp, f"pr-{pr}-body.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return ("pr", pr, who, path)

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
        trusted = self.trusted_paths(room, claim)
        if trusted is None:
            return
        attr = self.attribution(room, claim, commit)
        if attr is None:
            return
        argv = [claim]
        if attr[0] == "pr":
            argv += ["--pr", attr[1], "--claimant", attr[2], "--pr-body", attr[3]]
        else:
            argv += ["--no-awards"]
        argv += ["--trusted-targets", trusted[0], "--trusted-base-lock", trusted[1]]
        for delta in self.prior.get(room, []):
            argv += ["--prior-delta", delta]
        if self.src.merge(argv):
            self.prior.setdefault(room, []).append(data["delta"])
            if attr[0] == "none":
                self.log(f"{claim}: verified delta has no master PR; skipping prizes")
        elif attr[0] == "none":
            self.error(claim, "claim validation failed without PR attribution")
        else:
            self.error(claim, "merge validation failed")

    def run(self, history):
        """Process HISTORY [(commit, claim)] in order; return the error list."""
        for commit, claim in history:
            self.one(commit, claim)
        return self.errors


def cmd_award(args):
    """Replay every claim on master and write any errors to --errors."""
    source = Source(args.before, args.repo)
    with tempfile.TemporaryDirectory() as tmp:
        errors = Awarder(source, tmp).run(source.history())
    with open(args.errors, "w", encoding="utf-8") as f:
        f.write("".join(e + "\n" for e in errors))
