"""Tests for the confab scan CLI subcommand."""

import tempfile
import unittest
from pathlib import Path

from confab.claims import extract_claims_from_file
from confab.config import ConfabConfig, set_config, reset_config
from confab.verify import verify_all, VerificationResult


class TestScanFlow(unittest.TestCase):
    """Test the scan extraction + verification flow on arbitrary files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=Path(self.tmpdir) / "test_tracker.db",
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def test_scan_file_with_file_claim(self):
        """Scan a markdown file containing a file existence claim."""
        # Create the referenced file so the claim passes
        target = Path(self.tmpdir) / "data.json"
        target.write_text("{}")

        md = Path(self.tmpdir) / "test.md"
        md.write_text("- Config at `data.json` is ready.\n")

        claims = extract_claims_from_file(str(md))
        self.assertGreater(len(claims), 0)

        outcomes = verify_all(claims)
        self.assertEqual(len(outcomes), len(claims))
        # At least one should be auto-verified
        auto = [o for o in outcomes if o.result != VerificationResult.SKIPPED]
        self.assertGreater(len(auto), 0)

    def test_scan_file_with_missing_file_claim(self):
        """Scan detects a file that doesn't exist."""
        md = Path(self.tmpdir) / "test.md"
        md.write_text("- Config at `nonexistent_xyz.json` is ready.\n")

        claims = extract_claims_from_file(str(md))
        self.assertGreater(len(claims), 0)

        outcomes = verify_all(claims)
        failed = [o for o in outcomes if o.result == VerificationResult.FAILED]
        self.assertGreater(len(failed), 0)

    def test_scan_empty_file(self):
        """Scanning an empty file produces no claims."""
        md = Path(self.tmpdir) / "empty.md"
        md.write_text("")

        claims = extract_claims_from_file(str(md))
        self.assertEqual(len(claims), 0)

    def test_scan_nonexistent_file(self):
        """Scanning a nonexistent file returns empty list."""
        claims = extract_claims_from_file("/tmp/confab_scan_test_does_not_exist.md")
        self.assertEqual(len(claims), 0)

    def test_scan_multiple_files(self):
        """Claims from multiple files are combined."""
        f1 = Path(self.tmpdir) / "a.md"
        f1.write_text("- Script `deploy.sh` is deployed.\n")

        f2 = Path(self.tmpdir) / "b.md"
        f2.write_text("- Config at `settings.toml` exists.\n")

        claims_a = extract_claims_from_file(str(f1))
        claims_b = extract_claims_from_file(str(f2))

        all_claims = claims_a + claims_b
        self.assertGreater(len(all_claims), 0)

        # Source files should be tracked
        sources = {c.source_file for c in all_claims}
        self.assertTrue(any("a.md" in s for s in sources if s))
        self.assertTrue(any("b.md" in s for s in sources if s))

    def test_scan_blocker_claim(self):
        """Scan picks up blocker claims (blocked on X)."""
        md = Path(self.tmpdir) / "test.md"
        md.write_text("- Audio blocked on OPENAI_API_KEY\n")

        claims = extract_claims_from_file(str(md))
        self.assertGreater(len(claims), 0)
        # Should detect as a blocker/pipeline claim
        texts = " ".join(c.text for c in claims)
        self.assertIn("blocked", texts.lower())

    def test_scan_no_verify_mode(self):
        """Claims can be extracted without verification (the --no-verify flow)."""
        md = Path(self.tmpdir) / "test.md"
        md.write_text("- Audio pipeline: WORKING\n- Config at `app.toml` is ready.\n")

        claims = extract_claims_from_file(str(md))
        self.assertGreater(len(claims), 0)
        # Just extraction, no verify_all call — simulates --no-verify


class TestResolveDirectories(unittest.TestCase):
    """Test directory expansion for confab scan."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=Path(self.tmpdir) / "test_tracker.db",
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def test_directory_expands_to_md_files(self):
        """A directory argument should expand to all nested .md files."""
        from confab.cli import _resolve_scan_paths

        subdir = Path(self.tmpdir) / "docs"
        subdir.mkdir()
        (subdir / "a.md").write_text("- Config at `x.json` is ready.\n")
        (subdir / "b.md").write_text("- Script `y.sh` deployed.\n")
        (subdir / "skip.txt").write_text("not markdown\n")

        paths = _resolve_scan_paths([str(subdir)])
        names = [p.name for p in paths]
        self.assertIn("a.md", names)
        self.assertIn("b.md", names)
        self.assertNotIn("skip.txt", names)

    def test_nested_directory_recursion(self):
        """Subdirectories are scanned recursively."""
        from confab.cli import _resolve_scan_paths

        deep = Path(self.tmpdir) / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.md").write_text("- File `deep.json` exists.\n")

        paths = _resolve_scan_paths([str(Path(self.tmpdir) / "a")])
        names = [p.name for p in paths]
        self.assertIn("deep.md", names)

    def test_mixed_files_and_dirs(self):
        """Mix of file and directory arguments both resolve correctly."""
        from confab.cli import _resolve_scan_paths

        single = Path(self.tmpdir) / "single.md"
        single.write_text("- Env `FOO` is set.\n")

        subdir = Path(self.tmpdir) / "sub"
        subdir.mkdir()
        (subdir / "nested.md").write_text("- File `bar.py` exists.\n")

        paths = _resolve_scan_paths([str(single), str(subdir)])
        names = [p.name for p in paths]
        self.assertIn("single.md", names)
        self.assertIn("nested.md", names)

    def test_empty_directory_warns(self):
        """A directory with no .md files produces a warning but no crash."""
        from confab.cli import _resolve_scan_paths

        empty = Path(self.tmpdir) / "empty"
        empty.mkdir()

        paths = _resolve_scan_paths([str(empty)])
        self.assertEqual(len(paths), 0)


if __name__ == "__main__":
    unittest.main()
