"""Tests for the rooms/ROOM/<kind>/<file> layout check in scripts/bounty.py."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

# pylint: disable-next=wrong-import-position,import-error
import bounty  # noqa: E402

ROOM = "A" * 43


class RoomOfTest(unittest.TestCase):
    """Room path parsing accepts only the rooms/ROOM layout."""

    def test_accepts_nested_layout(self):
        for kind in ("submissions", "deltas"):
            path = f"rooms/{ROOM}/{kind}/x.jsonl.b64"
            self.assertEqual(bounty.room_of(path), ROOM)

    def test_rejects_old_root_layout(self):
        with self.assertRaises(SystemExit):
            bounty.room_of(f"{ROOM}/submissions/x.jsonl.b64")

    def test_rejects_wrong_parent_directory(self):
        with self.assertRaises(SystemExit):
            bounty.room_of(f"other/{ROOM}/deltas/x.claim.json")


if __name__ == "__main__":
    unittest.main()
