"""Tests for scripts/bounty_award.py using a fake Source (no git or GitHub)."""

# pylint: disable=missing-function-docstring

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# pylint: disable-next=wrong-import-position,import-error
import bounty_award as rc  # noqa: E402

ROOM = "A" * 43
OTHER = "B" * 43
FAIL = subprocess.CalledProcessError(1, "x")


def claim_path(room, digest):
    """Path of a claim file."""
    return f"{room}/deltas/{digest}.claim.json"


def claim_data(room, digest):
    """A well-formed claim body."""
    return {
        "delta": f"{room}/deltas/{digest}.delta.jsonl.b64",
        "delta_sha256_16": digest,
    }


class FakeSource:
    """Scripted stand-in for rc.Source."""

    def __init__(self):
        self.claims = {}
        self.trusted = {}
        self.prs_by_commit = {}
        self.merge_ok = {}
        self.merges = []
        self.broken = set()

    def read_claim(self, path):
        return self.claims.get(path)

    def trusted_file(self, room, name):
        return self.trusted.get((room, name), b"x")

    def prs(self, commit):
        if "prs" in self.broken:
            raise FAIL
        return self.prs_by_commit.get(commit, [])

    def pr_author(self, pr):
        if "author" in self.broken:
            raise FAIL
        return f"user{pr}"

    def pr_body(self, pr):
        if "body" in self.broken:
            raise FAIL
        return f"Payout: bc1q{pr}"

    def merge(self, argv):
        self.merges.append(argv)
        return self.merge_ok.get(argv[0], True)


def master_pr(number):
    """A pulls-API entry for a PR into master."""
    return {"number": number, "base": {"ref": "master"}}


class AwardTest(unittest.TestCase):
    """Ordering, attribution and blocking rules."""

    def setUp(self):
        self.src = FakeSource()
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.awarder = None
        self.addCleanup(self.tmp.cleanup)
        self.logs = []

    def run_history(self, history):
        r = rc.Awarder(self.src, self.tmp.name, log=self.logs.append)
        self.awarder = r
        return r.run(history)

    def add(self, room, digest, commit, prs=None):
        path = claim_path(room, digest)
        self.src.claims[path] = claim_data(room, digest)
        self.src.prs_by_commit[commit] = prs if prs is not None else [master_pr(7)]
        return (commit, path)

    def test_parse_history(self):
        text = (
            f"COMMIT\tc1\n{claim_path(ROOM, 'ab')}\n\n"
            f"COMMIT\tc2\nREADME.md\n{claim_path(OTHER, 'cd')}\n"
        )
        self.assertEqual(
            rc.parse_claim_history(text),
            [("c1", claim_path(ROOM, "ab")), ("c2", claim_path(OTHER, "cd"))],
        )

    def test_award_passes_pr_claimant_and_prior_deltas_in_order(self):
        h = [self.add(ROOM, "aa", "c1"), self.add(ROOM, "bb", "c2", [master_pr(8)])]
        self.assertEqual(self.run_history(h), [])
        first, second = self.src.merges[0], self.src.merges[1]
        self.assertIn("--claimant", first)
        self.assertEqual(first[first.index("--claimant") + 1], "user7")
        self.assertNotIn("--prior-delta", first)
        self.assertEqual(
            second[second.index("--prior-delta") + 1],
            f"{ROOM}/deltas/aa.delta.jsonl.b64",
        )

    def test_failed_merge_is_not_a_prior_delta(self):
        h = [self.add(ROOM, "aa", "c1"), self.add(ROOM, "bb", "c2")]
        self.src.merge_ok[h[0][1]] = False
        errors = self.run_history(h)
        self.assertEqual(len(errors), 1)
        self.assertIn("merge validation failed", errors[0])
        self.assertNotIn("--prior-delta", self.src.merges[1])

    def test_no_master_pr_verifies_without_awards(self):
        h = [self.add(ROOM, "aa", "c1", prs=[{"number": 3, "base": {"ref": "dev"}}])]
        self.assertEqual(self.run_history(h), [])
        self.assertIn("--no-awards", self.src.merges[0])
        self.assertNotIn("--claimant", self.src.merges[0])
        self.assertEqual(self.awarder.prior[ROOM], [claim_data(ROOM, "aa")["delta"]])

    def test_unattributable_claim_defers_later_claims_in_that_room_only(self):
        h = [
            self.add(ROOM, "aa", "c1"),
            self.add(ROOM, "bb", "c2"),
            self.add(OTHER, "cc", "c3"),
        ]
        self.src.broken.add("author")
        errors = self.run_history(h[:1])
        self.assertIn("could not resolve claimant", errors[0])
        self.src.broken.clear()
        r = self.awarder
        errors = r.run(h[1:])
        self.assertIn("deferred", errors[1])
        self.assertEqual(len(self.src.merges), 1)  # only the other room's claim
        self.assertTrue(self.src.merges[0][0].startswith(OTHER))

    def test_each_attribution_failure_blocks(self):
        for broken, text in (
            ("prs", "could not resolve PR"),
            ("author", "could not resolve claimant"),
            ("body", "could not read the description"),
        ):
            with self.subTest(broken=broken):
                self.src.broken = {broken}
                self.src.merges.clear()
                errors = self.run_history([self.add(ROOM, "aa", "c1")])
                self.assertIn(text, errors[0])
                self.assertIn(ROOM, self.awarder.blocked)
                self.assertEqual(self.src.merges, [])

    def test_malformed_pr_response_blocks(self):
        h = [self.add(ROOM, "aa", "c1", prs={"not": "a list"})]
        self.assertIn("could not parse PR attribution", self.run_history(h)[0])
        self.assertIn(ROOM, self.awarder.blocked)

    def test_missing_trusted_files_block(self):
        for name in ("targets.csv", "base.lock"):
            with self.subTest(name=name):
                self.src.trusted = {(ROOM, name): None}
                errors = self.run_history([self.add(ROOM, "aa", "c1")])
                self.assertIn("before this push", errors[0])
                self.assertIn(ROOM, self.awarder.blocked)

    def test_bad_embedded_delta_errors_without_blocking(self):
        h = [self.add(ROOM, "aa", "c1"), self.add(ROOM, "bb", "c2")]
        self.src.claims[h[0][1]]["delta_sha256_16"] = "zz"
        errors = self.run_history(h)
        self.assertIn("does not match its sibling", errors[0])
        self.assertEqual(len(self.src.merges), 1)
        self.assertEqual(len(errors), 1)

    def test_missing_claim_file_is_skipped(self):
        self.assertEqual(self.run_history([("c1", claim_path(ROOM, "aa"))]), [])
        self.assertEqual(self.src.merges, [])


if __name__ == "__main__":
    unittest.main()
