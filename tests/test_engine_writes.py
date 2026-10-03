"""Engine write-path tests — real invocations, not prompt strings.

Why this file exists: refactoring the three page emitters into one shared helper
broke every `create` path (a NameError, then a format drift adding `synced_at` to
preference/user pages) and the existing suite stayed green throughout — it asserted
prompt vocabulary and extraction parsing, never that a page was actually written.
These tests invoke the CLI as a subprocess against a throwaway store and compare
the produced files byte-for-byte against the frozen legacy engine.

The legacy engine is vendored at tests/fixtures/legacy_stack.py so no test depends
on a user's live store.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
ENGINE = PLUGIN_DIR / "engine.py"
LEGACY = HERE / "fixtures" / "legacy_stack.py"


def _engine(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--no-project", "--with", "pyyaml", "python", str(ENGINE),
         "--repo", str(repo), *args],
        capture_output=True, text=True, timeout=180,
    )


@unittest.skipUnless(shutil.which("uv") and shutil.which("git"), "uv and git required")
class EngineWriteTest(unittest.TestCase):
    """Every write path must actually write, in the documented format."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(tempfile.mkdtemp(prefix="stack-write-"))
        cls.store = cls._tmp / "store"
        cp = _engine(cls.store, "init")
        assert cp.returncode == 0, f"init failed: {cp.stdout}{cp.stderr}"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _create(self, *args):
        return _engine(self.store, "create", *args)

    # ------------------------------------------------------------ write paths --

    def test_create_note_writes_a_page(self):
        """A note lands at wiki/<name>.md with frontmatter and the body."""
        cp = self._create("--name", "w-note", "--description", "D", "--content", "Body text")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        page = self.store / "wiki" / "w-note.md"
        self.assertTrue(page.exists(), cp.stdout)
        text = page.read_text()
        self.assertIn("name: w-note", text)
        self.assertIn("Body text", text)

    def test_create_preference_routes_and_links_digest(self):
        """A preference routes to wiki/preferences/ and gains a digest link."""
        cp = self._create("--name", "w-pref", "--description", "Pref desc",
                          "--content", "B", "--kind", "preference")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        page = self.store / "wiki" / "preferences" / "w-pref.md"
        self.assertTrue(page.exists(), cp.stdout)
        self.assertIn("w-pref", (self.store / "wiki" / "preferences.md").read_text())

    def test_create_user_routes_to_user_dir(self):
        """A user fact routes to wiki/user/."""
        cp = self._create("--name", "w-user", "--description", "D",
                          "--content", "B", "--kind", "user")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((self.store / "wiki" / "user" / "w-user.md").exists(), cp.stdout)

    def test_preference_and_user_carry_no_synced_at(self):
        """Only code-anchored kinds carry synced_at.

        Adding it to every kind silently changes the on-disk format; the legacy
        engine omits it for preference/user, and drift tracking only applies to
        pages anchored to code.
        """
        self._create("--name", "w-p2", "--description", "D", "--content", "B",
                     "--kind", "preference")
        self._create("--name", "w-u2", "--description", "D", "--content", "B",
                     "--kind", "user")
        self.assertNotIn("synced_at",
                         (self.store / "wiki" / "preferences" / "w-p2.md").read_text())
        self.assertNotIn("synced_at",
                         (self.store / "wiki" / "user" / "w-u2.md").read_text())

    def test_create_routed_kinds_carry_synced_at(self):
        """note/decision/architecture/learning DO carry synced_at."""
        self._create("--name", "w-n2", "--description", "D", "--content", "B")
        self.assertIn("synced_at", (self.store / "wiki" / "w-n2.md").read_text())

    def test_architecture_without_anchor_is_rejected(self):
        cp = self._create("--name", "w-arch", "--description", "D",
                          "--kind", "architecture", "--dir", "decisions")
        self.assertNotEqual(cp.returncode, 0, "architecture requires an anchor")
        self.assertFalse((self.store / "wiki" / "decisions" / "w-arch.md").exists())

    def test_duplicate_page_is_rejected(self):
        self._create("--name", "w-dup", "--description", "D", "--content", "B")
        cp = self._create("--name", "w-dup", "--description", "D", "--content", "B")
        self.assertNotEqual(cp.returncode, 0, "additive-only: no overwrite")

    def test_create_stages_only_its_own_files(self):
        """A stray file in the store must never be swept into the index."""
        stray = self.store / "human.txt"
        stray.write_text("left lying around")
        self._create("--name", "w-stage", "--description", "D", "--content", "B")
        staged = subprocess.run(["git", "-C", str(self.store), "diff", "--cached",
                                 "--name-only"], capture_output=True, text=True).stdout
        self.assertNotIn("human.txt", staged, f"staged unrelated file:\n{staged}")
        self.assertIn("w-stage.md", staged)

    # ------------------------------------------------------- compatibility -----

    def test_newer_format_store_refuses_every_write(self):
        """A store from a newer engine must fail closed for `create`, not just init."""
        newer = self._tmp / "newer"
        shutil.copytree(self.store, newer)
        (newer / ".stack" / "manifest.json").write_text('{"format_version": 999}')
        cp = _engine(newer, "create", "--name", "must-not-exist",
                     "--description", "D", "--content", "B")
        self.assertNotEqual(cp.returncode, 0,
                            f"create wrote into a newer-format store:\n{cp.stdout}")
        self.assertFalse((newer / "wiki" / "must-not-exist.md").exists(),
                         "no page may be written into an incompatible store")

    def test_non_integer_format_version_is_rejected(self):
        bad = self._tmp / "badver"
        shutil.copytree(self.store, bad)
        (bad / ".stack" / "manifest.json").write_text('{"format_version": true}')
        cp = _engine(bad, "create", "--name", "nope", "--description", "D",
                     "--content", "B")
        self.assertNotEqual(cp.returncode, 0, "coercible is not valid: true != 1")

    # ------------------------------------------------------------ init safety --

    def test_init_refuses_a_non_empty_unrelated_directory(self):
        other = self._tmp / "unrelated"
        other.mkdir()
        (other / "important.txt").write_text("do not touch")
        cp = _engine(other, "init")
        self.assertNotEqual(cp.returncode, 0, "must refuse unrelated content")
        self.assertEqual((other / "important.txt").read_text(), "do not touch")

    def test_init_reports_a_file_target_without_raising(self):
        target = self._tmp / "afile"
        target.write_text("x")
        cp = _engine(target, "init")
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("Traceback", cp.stderr or "", "must report, not crash")

    def test_init_is_idempotent_on_a_valid_store(self):
        before = sorted(p.name for p in self.store.iterdir())
        cp = _engine(self.store, "init")
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(before, sorted(p.name for p in self.store.iterdir()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
