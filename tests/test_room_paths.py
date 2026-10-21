"""Tests that room data is read and written under rooms/ROOM/."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import contextlib
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# pylint: disable=wrong-import-position,import-error
import bounty  # noqa: E402
import bounty_award  # noqa: E402
import bounty_payout  # noqa: E402

ROOM = "A" * 43
DIGEST = "0123456789abcdef"
BASE = f"res/{ROOM}/merged-{ROOM}-v1-d1-1.jsonl"
CLAIM = f"rooms/{ROOM}/deltas/{DIGEST}.claim.json"
DELTA = f"rooms/{ROOM}/deltas/{DIGEST}.delta.jsonl.b64"


@contextlib.contextmanager
def chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class TempCwd(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        cwd = chdir(self.tmp)
        cwd.__enter__()
        self.addCleanup(cwd.__exit__, None, None, None)

    def touch(self, path, text=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


class BountyPathsTest(TempCwd):
    def test_room_files_live_under_rooms(self):
        self.assertEqual(bounty.room_dir(ROOM), f"rooms/{ROOM}")
        self.assertEqual(bounty.base_lock_path(ROOM), f"rooms/{ROOM}/base.lock")

    def test_targets_file_ignores_old_root_layout(self):
        self.touch(f"{ROOM}/targets.csv")
        with self.assertRaises(SystemExit):
            bounty.targets_file(ROOM)
        self.touch(f"rooms/{ROOM}/targets.csv")
        self.assertEqual(bounty.targets_file(ROOM), f"rooms/{ROOM}/targets.csv")

    def claim_args(self):
        return SimpleNamespace(claim=CLAIM, base=None)

    def claim(self, **over):
        claim = {
            "room": ROOM,
            "delta": DELTA,
            "delta_sha256_16": DIGEST,
            "base": BASE,
        }
        return {**claim, **over}

    def test_claim_base_name_accepts_nested_delta(self):
        name = bounty.claim_base_name(self.claim_args(), ROOM, self.claim())
        self.assertEqual(name, BASE)

    def test_claim_base_name_rejects_old_layout_delta(self):
        old = f"{ROOM}/deltas/{DIGEST}.delta.jsonl.b64"
        with self.assertRaises(SystemExit):
            bounty.claim_base_name(self.claim_args(), ROOM, self.claim(delta=old))

    def test_claim_base_name_rejects_other_room_delta(self):
        other = f"rooms/{'B' * 43}/deltas/{DIGEST}.delta.jsonl.b64"
        with self.assertRaises(SystemExit):
            bounty.claim_base_name(self.claim_args(), ROOM, self.claim(delta=other))

    def test_append_awards_writes_ledger_under_rooms(self):
        os.makedirs(f"rooms/{ROOM}/deltas")
        args = SimpleNamespace(claim=CLAIM, claimant="u", payout="addr", pr="7")
        bounty.append_awards(args, {"delta": DELTA}, {"$t": {"prize_sats": 5}}, {"$t"})
        with open(f"rooms/{ROOM}/ledger.jsonl", encoding="utf-8") as f:
            row = json.loads(f.readline())
        self.assertEqual((row["target"], row["delta"]), ("$t", DELTA))
        self.assertFalse(os.path.exists(f"{ROOM}/ledger.jsonl"))


class PayoutPathsTest(TempCwd):
    def test_unpaid_rows_reads_rooms_ledger_only(self):
        self.touch(f"{ROOM}/ledger.jsonl", "")
        with self.assertRaises(SystemExit):
            bounty_payout.unpaid_rows(ROOM, {})
        self.touch(f"rooms/{ROOM}/ledger.jsonl", "")
        self.assertEqual(bounty_payout.unpaid_rows(ROOM, {})[0], {})


class AwardLayoutTest(TempCwd):
    def test_claim_room_and_sibling_check(self):
        self.assertEqual(bounty_award.claim_room(CLAIM), ROOM)
        claim = {"delta": DELTA, "delta_sha256_16": DIGEST}
        self.assertIsNone(bounty_award.sibling_problem(claim, CLAIM))

    def test_old_layout_claim_is_rejected(self):
        old = f"{ROOM}/deltas/{DIGEST}.claim.json"
        claim = {
            "delta": f"{ROOM}/deltas/{DIGEST}.delta.jsonl.b64",
            "delta_sha256_16": DIGEST,
        }
        self.assertIsNotNone(bounty_award.sibling_problem(claim, old))

    def test_staged_ledger_for_unpublished_room_is_rejected(self):
        src = os.path.join(self.tmp, "src")
        self.touch(f"src/{ROOM}/ledger.jsonl", "")
        self.touch(f"{ROOM}/targets.csv")  # old layout does not count
        with self.assertRaises(SystemExit):
            bounty_award.staged_ledgers(src, self.tmp)
        self.touch(f"rooms/{ROOM}/targets.csv")
        self.assertEqual(len(bounty_award.staged_ledgers(src, self.tmp)), 1)


if __name__ == "__main__":
    unittest.main()
