"""Tests for scripts/bounty_award.py using fakes (no git, GitHub or rezzy)."""

# pylint: disable=missing-function-docstring

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# pylint: disable-next=wrong-import-position,import-error
import bounty_award as rc  # noqa: E402

ROOM = "A" * 43
OTHER = "B" * 43
FAIL = subprocess.CalledProcessError(1, "x")
SHA = "0123456789abcdef0123456789abcdef01234567"


def commit(n):
    return f"{n:040x}"


def claim_path(room, digest):
    return f"{room}/deltas/{digest}.claim.json"


def claim_data(room, digest):
    return {
        "delta": f"{room}/deltas/{digest}.delta.jsonl.b64",
        "delta_sha256_16": digest,
    }


def master_pr(number):
    return {"number": number, "base": {"ref": "master"}}


class FakeRemote:
    """Scripted stand-in for rc.Remote."""

    def __init__(self):
        self.claims = {}
        self.malformed = set()
        self.trusted = {}
        self.prs_by_commit = {}
        self.broken = set()
        self.lookups = []

    def read_claim(self, path):
        if path in self.malformed:
            raise ValueError("invalid JSON")
        return self.claims.get(path)

    def trusted_file(self, room, name):
        return self.trusted.get((room, name), b"x")

    def prs(self, sha):
        self.lookups.append(sha)
        if "prs" in self.broken:
            raise FAIL
        return self.prs_by_commit.get(sha, [])

    def pr_author(self, pr):
        if "author" in self.broken:
            raise FAIL
        return f"user{pr}"

    def pr_body(self, pr):
        if "body" in self.broken:
            raise FAIL
        return f"Payout: bc1q{pr}"


def slurp(path):
    with open(path, "rb") as f:
        return f.read()


class FakeLocal:
    """Stand-in for rc.Local: claim files and a scripted merge."""

    def __init__(self, claims):
        self.claims = claims
        self.malformed = set()
        self.merge_ok = {}
        self.merges = []
        self.seen = []

    def read_claim(self, path):
        if path in self.malformed:
            raise ValueError("invalid JSON")
        return self.claims.get(path)

    def merge(self, argv):
        self.merges.append(argv)
        self.seen.append(
            {
                a.split("=", 1)[0]: slurp(a.split("=", 1)[1])
                for a in argv
                if a.startswith(("--trusted-targets=", "--pr-body="))
            }
        )
        return self.merge_ok.get(argv[0], True)


class Flow(unittest.TestCase):
    """Resolve then replay, as the two workflow jobs do."""

    def setUp(self):
        self.remote = FakeRemote()
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.tmp.cleanup)
        self.history = []
        self.awarder = None
        self.local = None

    def add(self, room, digest, n, prs=None):
        path = claim_path(room, digest)
        self.remote.claims[path] = claim_data(room, digest)
        self.remote.prs_by_commit[commit(n)] = (
            prs if prs is not None else [master_pr(7)]
        )
        self.history.append((commit(n), path))
        return path

    def resolve(self):
        return rc.Resolver(self.remote, log=lambda _: None).run(self.history)

    def replay(self, doc=None, merge_ok=None):
        doc = doc or self.resolve()
        path = os.path.join(self.tmp.name, "resolved.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        self.local = FakeLocal(self.remote.claims)
        self.local.malformed = self.remote.malformed.copy()
        self.local.merge_ok = merge_ok or {}
        self.awarder = rc.Awarder(
            self.local, rc.load_resolved(path), self.tmp.name, log=lambda _: None
        )
        return self.awarder.run()

    def arg(self, argv, name):
        return next(a.split("=", 1)[1] for a in argv if a.startswith(name + "="))


class ResolveReplayTest(Flow):
    """Ordering, attribution and deferral rules across both phases."""

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
        self.add(ROOM, "aa", 1)
        self.add(ROOM, "bb", 2, [master_pr(8)])
        self.assertEqual(self.replay(), [])
        first, second = self.local.merges[0], self.local.merges[1]
        self.assertEqual(self.arg(first, "--claimant"), "user7")
        self.assertFalse([a for a in first if a.startswith("--prior-delta")])
        self.assertEqual(
            self.arg(second, "--prior-delta"), f"{ROOM}/deltas/aa.delta.jsonl.b64"
        )

    def test_trusted_files_and_pr_body_reach_merge(self):
        self.add(ROOM, "aa", 1)
        self.replay()
        argv = self.local.merges[0]
        self.assertTrue(self.arg(argv, "--trusted-targets").endswith("-targets.csv"))
        self.assertEqual(self.local.seen[0]["--trusted-targets"], b"x")
        self.assertEqual(self.local.seen[0]["--pr-body"], b"Payout: bc1q7")

    def test_pr_lookups_are_cached_by_number(self):
        self.add(ROOM, "aa", 1)
        self.add(OTHER, "bb", 2)
        calls = []
        author = self.remote.pr_author
        self.remote.pr_author = lambda pr: calls.append(pr) or author(pr)
        self.resolve()
        self.assertEqual(calls, ["7"])

    def test_processed_claims_are_not_replayed_and_remain_prior_deltas(self):
        prior = claim_path(ROOM, "aa")
        current = self.add(ROOM, "bb", 1)
        doc = self.resolve()
        doc["processed"] = [
            {"claim": prior, "delta": prior.replace(".claim.json", ".delta.jsonl.b64")}
        ]
        self.assertEqual([claim for _, claim in doc["history"]], [current])
        self.assertEqual(self.replay(doc), [])
        self.assertEqual(len(self.local.merges), 1)
        self.assertEqual(
            self.arg(self.local.merges[0], "--prior-delta"),
            prior.replace(".claim.json", ".delta.jsonl.b64"),
        )
        self.assertEqual(
            [entry["claim"] for entry in self.awarder.completed], [prior, current]
        )

    def test_remote_history_filters_processed_claims(self):
        older, newer = claim_path(ROOM, "aa"), claim_path(OTHER, "bb")
        output = f"COMMIT\t{commit(1)}\n{older}\nCOMMIT\t{commit(2)}\n{newer}\n"
        with patch.object(rc.Remote, "_run", return_value=output.encode()):
            self.assertEqual(
                rc.Remote("0" * 40, "owner/repo").history(
                    [
                        {
                            "claim": older,
                            "delta": older.replace(".claim.json", ".delta.jsonl.b64"),
                        }
                    ]
                ),
                [(commit(2), newer)],
            )

    def test_malformed_claim_is_reported_and_others_still_award(self):
        bad = self.add(ROOM, "aa", 1)
        self.add(ROOM, "bb", 2)
        self.add(OTHER, "cc", 3)
        self.remote.malformed.add(bad)
        errors = self.replay()
        self.assertEqual(len(errors), 2)
        self.assertIn("malformed JSON", errors[0])
        self.assertIn("deferred", errors[1])
        self.assertEqual(len(self.local.merges), 1)

    def test_local_read_claim_raises_for_bad_json(self):
        path = os.path.join(self.tmp.name, "c.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{nope")
        with self.assertRaises(ValueError):
            rc.Local().read_claim(path)
        self.assertIsNone(rc.Local().read_claim(path + ".gone"))

    def test_failed_merge_is_not_a_prior_delta(self):
        first = self.add(ROOM, "aa", 1)
        self.add(ROOM, "bb", 2)
        errors = self.replay(merge_ok={first: False})
        self.assertEqual(len(errors), 1)
        self.assertIn("merge validation failed", errors[0])
        self.assertFalse(
            [a for a in self.local.merges[1] if a.startswith("--prior-delta")]
        )

    def test_no_master_pr_verifies_without_awards(self):
        self.add(ROOM, "aa", 1, prs=[{"number": 3, "base": {"ref": "dev"}}])
        self.assertEqual(self.replay(), [])
        self.assertIn("--no-awards", self.local.merges[0])
        self.assertFalse([a for a in self.local.merges[0] if "claimant" in a])
        self.assertEqual(self.awarder.prior[ROOM], [claim_data(ROOM, "aa")["delta"]])

    def test_unattributable_claim_defers_later_claims_in_that_room(self):
        self.add(ROOM, "aa", 1)
        self.add(ROOM, "bb", 2)
        self.remote.broken.add("author")
        errors = self.replay()
        self.assertIn("could not resolve claimant", errors[0])
        self.assertIn("deferred", errors[1])
        self.assertEqual(self.local.merges, [])

    def test_other_rooms_still_process(self):
        self.add(ROOM, "aa", 1)
        self.add(ROOM, "bb", 2)
        self.add(OTHER, "cc", 3)
        self.remote.broken.add("author")
        self.remote.trusted[(OTHER, "base.lock")] = b"y"
        doc = self.resolve()
        # author lookup fails for everything, so rebuild OTHER from a healthy remote
        self.remote.broken.clear()
        healthy = rc.Resolver(self.remote, log=lambda _: None).run(self.history[2:])
        doc["claims"].update(healthy["claims"])
        doc["rooms"].update(healthy["rooms"])
        errors = self.replay(doc)
        self.assertEqual(len(errors), 2)
        self.assertTrue(self.local.merges[0][0].startswith(OTHER))

    def test_resolver_skips_lookups_for_blocked_rooms(self):
        self.add(ROOM, "aa", 1)
        self.add(ROOM, "bb", 2)
        self.remote.broken.add("prs")
        self.resolve()
        self.assertEqual(self.remote.lookups, [commit(1)])

    def test_each_attribution_failure_blocks(self):
        for broken, text in (
            ("prs", "could not resolve PR"),
            ("author", "could not resolve claimant"),
            ("body", "could not read the description"),
        ):
            with self.subTest(broken=broken):
                self.remote.broken = {broken}
                self.history = []
                self.add(ROOM, "aa", 1)
                errors = self.replay()
                self.assertIn(text, errors[0])
                self.assertIn(ROOM, self.awarder.blocked)
                self.assertEqual(self.local.merges, [])

    def test_malformed_pr_response_blocks(self):
        self.add(ROOM, "aa", 1, prs={"not": "a list"})
        self.assertIn("could not parse PR attribution", self.replay()[0])
        self.assertIn(ROOM, self.awarder.blocked)

    def test_missing_trusted_files_block(self):
        for name in ("targets.csv", "base.lock"):
            with self.subTest(name=name):
                self.remote.trusted = {(ROOM, name): None}
                self.history = []
                self.add(ROOM, "aa", 1)
                errors = self.replay()
                self.assertIn("before this push", errors[0])
                self.assertIn(ROOM, self.awarder.blocked)

    def test_bad_embedded_delta_errors_without_blocking(self):
        first = self.add(ROOM, "aa", 1)
        self.add(ROOM, "bb", 2)
        self.remote.claims[first]["delta_sha256_16"] = "zz"
        errors = self.replay()
        self.assertIn("does not match its sibling", errors[0])
        self.assertEqual(len(self.local.merges), 1)
        self.assertEqual(len(errors), 1)

    def test_missing_claim_file_is_skipped(self):
        self.history = [(commit(1), claim_path(ROOM, "aa"))]
        self.assertEqual(self.replay(), [])
        self.assertEqual(self.local.merges, [])

    def test_claim_missing_from_resolved_metadata_fails_closed(self):
        self.add(ROOM, "aa", 1)
        doc = self.resolve()
        doc["claims"] = {}
        errors = self.replay(doc)
        self.assertIn("no resolved metadata", errors[0])
        self.assertEqual(self.local.merges, [])


class LoadResolvedTest(Flow):
    """The replay job must not trust a malformed resolved document."""

    def load(self, doc):
        path = os.path.join(self.tmp.name, "r.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return rc.load_resolved(path)

    def test_rejects_malformed_documents(self):
        self.add(ROOM, "aa", 1)
        good = self.resolve()
        self.load(good)
        key = next(iter(good["claims"]))
        for mutate in (
            lambda d: d["history"].append([commit(2), "../../etc/passwd"]),
            lambda d: d["history"].append(["not-a-sha", claim_path(ROOM, "aa")]),
            lambda d: d["rooms"].update({"bad": {}}),
            lambda d: d["claims"][key]["pr"].update({"number": "7; rm"}),
            lambda d: d["claims"][key]["pr"].update({"author": 5}),
            lambda d: d.pop("claims"),
        ):
            with self.subTest():
                doc = json.loads(json.dumps(good))
                mutate(doc)
                with self.assertRaises(SystemExit):
                    self.load(doc)


class PublishTest(unittest.TestCase):
    """Phase 3 must accept only append-only unpaid ledger rows."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "repo")
        self.src = os.path.join(self.tmp.name, "staged")
        os.makedirs(os.path.join(self.root, ROOM))
        os.makedirs(os.path.join(self.src, ROOM))
        self.write(
            os.path.join(self.root, ROOM, "targets.csv"),
            "event_id,n_refs,min_child_depth,prize_sats\n"
            + "".join(f"${n},1,1,3\n" for n in range(1, 4)),
        )
        self.resolved = {
            "history": [[commit(1), claim_path(ROOM, "aa")]],
            "processed": [],
            "claims": {
                rc.claim_key(commit(1), claim_path(ROOM, "aa")): {
                    "error": None,
                    "pr": {
                        "number": "1",
                        "author": "a",
                        "body": "Payout: bc1q" + "a" * 26,
                    },
                }
            },
        }
        self.write(os.path.join(self.src, "complete"), "ok\n")
        self.write(os.path.join(self.src, "processed.json"), "[]")

    @staticmethod
    def write(path, text):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    @staticmethod
    def row(target, status="unpaid", **extra):
        row = {
            "target": target,
            "claimant": "a",
            "address": "bc1q" + "a" * 26,
            "pr": "1",
            "delta": f"{ROOM}/deltas/aa.delta.jsonl.b64",
            "prize_sats": 3,
            "status": status,
        }
        row.update(extra)
        return json.dumps(row) + "\n"

    def pub(self, src):
        return rc.publish(src, self.resolved, self.root)

    def stage(self, text):
        self.write(os.path.join(self.src, ROOM, "ledger.jsonl"), text)

    def current(self, text):
        self.write(os.path.join(self.root, ROOM, "ledger.jsonl"), text)

    def test_appends_new_rows(self):
        self.current(self.row("$1"))
        self.stage(self.row("$1") + self.row("$2"))
        changed, _ = self.pub(self.src)
        self.assertEqual(changed, [ROOM])
        with open(os.path.join(self.root, ROOM, "ledger.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)

    def test_creates_first_ledger(self):
        self.stage(self.row("$1"))
        self.assertEqual(self.pub(self.src)[0], [ROOM])

    def test_publish_persists_only_resolved_processed_claims(self):
        claim = claim_path(ROOM, "aa")
        delta = claim.replace(".claim.json", ".delta.jsonl.b64")
        self.resolved["claims"] = {
            rc.claim_key(commit(1), claim): {
                "error": None,
                "pr": {"number": "1", "author": "a", "body": "Payout: bc1q" + "a" * 26},
            }
        }
        self.stage(self.row("$1"))
        self.write(
            os.path.join(self.src, "processed.json"),
            json.dumps([{"claim": claim, "delta": delta}]),
        )
        self.assertEqual(self.pub(self.src)[0], [ROOM])
        self.assertEqual(
            rc.load_processed(os.path.join(self.root, rc.PROCESSED_PATH))[0]["claim"],
            claim,
        )

    def test_unchanged_ledger_is_not_reported(self):
        self.current(self.row("$1"))
        self.stage(self.row("$1"))
        self.assertEqual(self.pub(self.src)[0], [])

    def test_rejects_rewrites_and_bad_rows(self):
        self.current(self.row("$1"))
        for staged in (
            self.row("$9"),  # does not extend the current ledger
            self.row("$1") + self.row("$2", status="paid"),
            self.row("$1") + self.row("$2", txid="t"),
            self.row("$1") + "not json\n",
            self.row("$1") + self.row("$2").rstrip("\n"),
            self.row("$1") + self.row("$2", prize_sats="3"),
            self.row("$1") + self.row("$2", prize_sats=True),
            self.row("$1") + self.row("$2", prize_sats=4),  # not the published prize
            self.row("$1") + self.row("$1"),  # already awarded
            self.row("$1") + self.row("$7"),  # not a target
            self.row("$1") + self.row(["$2"]),  # invalid target type
            self.row("$1") + self.row("$2", claimant="mallory"),
            self.row("$1") + self.row("$2", address="bc1qforged"),
            self.row("$1") + self.row("$2", pr="9"),
            self.row("$1") + self.row("$2", delta=f"{ROOM}/deltas/ff.delta.jsonl.b64"),
            self.row("$1")
            + self.row("$2")
            + self.row("$2"),  # repeated within the batch
        ):
            with self.subTest(staged=staged[-30:]):
                self.stage(staged)
                with self.assertRaises(SystemExit):
                    self.pub(self.src)

    def test_rejects_unexpected_entries(self):
        self.stage(self.row("$1"))
        os.makedirs(os.path.join(self.src, "B" * 43))  # room without targets.csv
        self.write(os.path.join(self.src, "B" * 43, "ledger.jsonl"), self.row("$1"))
        with self.assertRaises(SystemExit):
            self.pub(self.src)

    def test_rejects_extra_files_symlinks_and_missing_marker(self):
        self.stage(self.row("$1"))
        self.write(os.path.join(self.src, ROOM, "hook"), "x")
        with self.assertRaises(SystemExit):
            self.pub(self.src)
        os.remove(os.path.join(self.src, ROOM, "hook"))
        os.symlink("/etc/passwd", os.path.join(self.src, "link"))
        with self.assertRaises(SystemExit):
            self.pub(self.src)
        os.remove(os.path.join(self.src, "link"))
        os.remove(os.path.join(self.src, "complete"))
        with self.assertRaises(SystemExit):
            self.pub(self.src)

    def test_errors_cannot_inject_workflow_commands(self):
        self.stage(self.row("$1"))
        self.write(
            os.path.join(self.src, "errors.txt"),
            "::add-mask::x\nok" + chr(0x202E) + "\n",
        )
        errors = self.pub(self.src)[1]
        self.assertTrue(all(e.startswith("claim error: ") for e in errors))
        self.assertNotIn(chr(0x202E), "".join(errors))

    def test_stage_collects_ledgers_errors_and_marker(self):
        work = os.path.join(self.tmp.name, "work")
        os.makedirs(os.path.join(work, ROOM))
        self.write(os.path.join(work, ROOM, "ledger.jsonl"), self.row("$1"))
        os.makedirs(os.path.join(work, "notaroom"))
        self.write(os.path.join(work, "notaroom", "ledger.jsonl"), "x")
        out = os.path.join(self.tmp.name, "out")
        rc.stage(out, ["boom\nsecond"], [], root=work)
        self.assertEqual(
            sorted(os.listdir(out)), [ROOM, "complete", "errors.txt", "processed.json"]
        )
        with open(os.path.join(out, "errors.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "boom second\n")
        self.assertEqual(self.pub(out)[0], [ROOM])


if __name__ == "__main__":
    unittest.main()
