"""Tests for the shell helpers the GitHub workflows call."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import json
import os
import subprocess
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "scripts")
ROOM = "A" * 43
OTHER = "B" * 43


def run(script, *args, cwd=None):
    return subprocess.run(
        ["bash", os.path.join(SCRIPTS, script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


class TempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write(self, path, text=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


class ListSubmissionsTest(TempDir):
    def list(self, *names):
        changed, out = self.path("changed.txt"), self.path("out.txt")
        self.write(changed, "".join(json.dumps(n) + "\n" for n in names))
        result = run("list_submissions.sh", changed, out)
        lines = []
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                lines = f.read().split()
        return result, lines

    def sub(self, name, room=ROOM):
        return f"rooms/{room}/submissions/{name}.jsonl.b64"

    def test_accepts_valid_and_ignores_other_files(self):
        result, lines = self.list(self.sub("alice"), "README.md")
        self.assertEqual((result.returncode, lines), (0, [self.sub("alice")]))

    def test_orders_parts_numerically(self):
        parts = [self.sub("alice.100"), self.sub("alice.11"), self.sub("alice.02")]
        result, lines = self.list(*parts)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, [parts[2], parts[1], parts[0]])

    def test_rejects_old_layout_without_failing(self):
        result, lines = self.list(f"{ROOM}/submissions/alice.jsonl.b64")
        self.assertEqual((result.returncode, lines), (1, []))

    def test_rejects_bad_names(self):
        for name in ("bad:name", "v1.2", "a b"):
            with self.subTest(name=name):
                result, _ = self.list(self.sub(name))
                self.assertEqual(result.returncode, 1)
                self.assertIn("invalid submission file name", result.stderr)

    def test_rejects_two_names_in_one_room(self):
        result, _ = self.list(self.sub("alice"), self.sub("bob"))
        self.assertEqual(result.returncode, 1)
        self.assertIn(ROOM, result.stderr)

    def test_allows_one_name_per_room(self):
        result, lines = self.list(self.sub("alice"), self.sub("bob", OTHER))
        self.assertEqual((result.returncode, len(lines)), (0, 2))


class ApplyGeneratedTest(TempDir):
    def setUp(self):
        super().setUp()
        self.repo = self.path("work")
        self.gen = self.path("generated")
        self.submitted = self.path("submitted.txt")
        os.makedirs(self.repo)
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@invalid"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=self.repo, check=True)
        self.sub = f"rooms/{ROOM}/submissions/alice.jsonl.b64"
        self.write(os.path.join(self.repo, self.sub), "x")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.repo, check=True)
        self.write(self.submitted, self.sub + "\n")

    def staged(self):
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-status"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return sorted(out.stdout.splitlines())

    def apply(self):
        return run("apply_generated.sh", self.submitted, self.gen, cwd=self.repo)

    def test_replaces_submission_with_deltas(self):
        deltas = os.path.join(self.gen, "rooms", ROOM, "deltas")
        self.write(os.path.join(deltas, "ab.delta.jsonl.b64"), "d")
        self.write(os.path.join(deltas, "ab.claim.json"), "{}")
        result = self.apply()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.staged(),
            sorted(
                [
                    f"A\trooms/{ROOM}/deltas/ab.claim.json",
                    f"A\trooms/{ROOM}/deltas/ab.delta.jsonl.b64",
                    f"D\t{self.sub}",
                ]
            ),
        )

    def test_rejects_unexpected_top_level_entry(self):
        self.write(os.path.join(self.gen, ROOM, "deltas", "ab.claim.json"))
        result = self.apply()
        self.assertEqual(result.returncode, 1)
        self.assertIn("unexpected generated path", result.stderr)

    def test_rejects_non_room_directory(self):
        self.write(os.path.join(self.gen, "rooms", "notaroom", "deltas", "x"))
        self.assertEqual(self.apply().returncode, 1)

    def test_rejects_symlinked_generated_file(self):
        deltas = os.path.join(self.gen, "rooms", ROOM, "deltas")
        os.makedirs(deltas)
        os.symlink("/etc/passwd", os.path.join(deltas, "ab.delta.jsonl.b64"))
        result = self.apply()
        self.assertEqual(result.returncode, 1)
        self.assertIn("regular generated file", result.stderr)

    def test_rejects_symlinked_destination(self):
        deltas = os.path.join(self.gen, "rooms", ROOM, "deltas")
        self.write(os.path.join(deltas, "ab.claim.json"), "{}")
        dest = os.path.join(self.repo, "rooms", ROOM, "deltas")
        os.makedirs(dest)
        os.symlink("/tmp", os.path.join(dest, "ab.claim.json"))
        result = self.apply()
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink destination", result.stderr)


if __name__ == "__main__":
    unittest.main()
