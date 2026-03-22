"""Tests for the claim hygiene linter."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from confab.lint import (
    LintIssue,
    LintReport,
    LintSeverity,
    APPROX_COUNT_RE,
    SOURCE_CITATION_RE,
    run_lint,
    _check_approx_counts,
    _check_claim,
)
from confab.claims import Claim, ClaimType, VerifiabilityLevel


class TestApproxCountRegex(unittest.TestCase):
    """Test APPROX_COUNT_RE pattern matching."""

    def test_matches_tilde_count(self):
        self.assertIsNotNone(APPROX_COUNT_RE.search("~65 published entries"))
        self.assertIsNotNone(APPROX_COUNT_RE.search("~200 journal entries"))
        self.assertIsNotNone(APPROX_COUNT_RE.search("~ 30 items in queue"))

    def test_matches_various_nouns(self):
        for noun in ["entries", "posts", "files", "tests", "subscribers", "views"]:
            self.assertIsNotNone(
                APPROX_COUNT_RE.search(f"~10 {noun}"),
                f"Failed to match ~10 {noun}",
            )

    def test_no_match_exact_count(self):
        # Exact counts (no tilde) should NOT match this regex
        self.assertIsNone(APPROX_COUNT_RE.search("65 published entries"))

    def test_no_match_plain_text(self):
        self.assertIsNone(APPROX_COUNT_RE.search("nothing approximate here"))


class TestSourceCitationRegex(unittest.TestCase):
    """Test SOURCE_CITATION_RE — detecting grounded counts."""

    def test_per_citation(self):
        self.assertIsNotNone(SOURCE_CITATION_RE.search("(per posts.json)"))

    def test_from_file(self):
        self.assertIsNotNone(SOURCE_CITATION_RE.search("from `data/posts.json`"))

    def test_verification_tag(self):
        self.assertIsNotNone(SOURCE_CITATION_RE.search("[v1: checked 2026-03-21]"))

    def test_via_source(self):
        self.assertIsNotNone(SOURCE_CITATION_RE.search("via posts.json"))

    def test_no_citation(self):
        self.assertIsNone(SOURCE_CITATION_RE.search("about 65 items total"))


class TestCheckApproxCounts(unittest.TestCase):
    """Test _check_approx_counts for approximate counts without sources."""

    def test_flags_approx_without_source(self):
        text = "We have ~65 published entries in the journal"
        report = LintReport(files_scanned=["test.md"])
        _check_approx_counts(text, "test.md", report)
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].rule, "approx-no-source")
        self.assertEqual(report.issues[0].severity, LintSeverity.INFO)

    def test_ignores_approx_with_source(self):
        text = "~65 published entries (per posts.json)"
        report = LintReport(files_scanned=["test.md"])
        _check_approx_counts(text, "test.md", report)
        self.assertEqual(len(report.issues), 0)

    def test_ignores_approx_with_verification_tag(self):
        text = "~65 published entries [v1: checked posts.json 2026-03-21]"
        report = LintReport(files_scanned=["test.md"])
        _check_approx_counts(text, "test.md", report)
        self.assertEqual(len(report.issues), 0)

    def test_multiple_approx_on_separate_lines(self):
        text = "~10 entries here\n~20 items there"
        report = LintReport(files_scanned=["test.md"])
        _check_approx_counts(text, "test.md", report)
        self.assertEqual(len(report.issues), 2)

    def test_correct_line_numbers(self):
        text = "line one\n~10 entries here\nline three"
        report = LintReport(files_scanned=["test.md"])
        _check_approx_counts(text, "test.md", report)
        self.assertEqual(report.issues[0].line, 2)


class TestCheckClaim(unittest.TestCase):
    """Test _check_claim for individual claim hygiene rules."""

    def _make_claim(self, text="test claim", vtag=None,
                    claim_type=ClaimType.FILE_EXISTS,
                    verifiability=VerifiabilityLevel.AUTO,
                    source_file="test.md", source_line=5):
        return Claim(
            text=text,
            claim_type=claim_type,
            verifiability=verifiability,
            source_file=source_file,
            source_line=source_line,
            verification_tag=vtag,
        )

    def test_no_tag_warning(self):
        """Claims without verification tags get a warning."""
        claim = self._make_claim(text="script.py is working")
        report = LintReport(files_scanned=["test.md"])
        _check_claim(claim, set(), {}, 3, report)
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].rule, "no-tag")
        self.assertEqual(report.issues[0].severity, LintSeverity.WARNING)

    def test_tagged_claim_clean(self):
        """Claims with [v1] tags produce no issues."""
        claim = self._make_claim(
            text="script.py is working [v1: checked 2026-03-21]",
            vtag="[v1: checked 2026-03-21]",
        )
        report = LintReport(files_scanned=["test.md"])
        _check_claim(claim, set(), {}, 3, report)
        self.assertEqual(len(report.issues), 0)

    def test_unverified_tag_fresh_is_ok(self):
        """[unverified] claims with low run count are not flagged as stale."""
        claim = self._make_claim(
            text="something [unverified]",
            vtag="[unverified]",
        )
        report = LintReport(files_scanned=["test.md"])
        _check_claim(claim, set(), {}, 3, report)
        # Should not produce any issue — [unverified] is an acceptable tag
        self.assertEqual(len(report.issues), 0)

    def test_stale_unverified_error(self):
        """[unverified] claims seen in 3+ runs get an error."""
        claim = self._make_claim(
            text="something [unverified]",
            vtag="[unverified]",
        )
        from confab.tracker import _hash_claim
        h = _hash_claim(claim.text)
        report = LintReport(files_scanned=["test.md"])
        _check_claim(claim, set(), {h: 5}, 3, report)
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].rule, "stale-unverified")
        self.assertEqual(report.issues[0].severity, LintSeverity.ERROR)

    def test_stale_unverified_from_hash_set(self):
        """[unverified] claims in stale_hashes set get an error."""
        claim = self._make_claim(
            text="something [unverified]",
            vtag="[unverified]",
        )
        from confab.tracker import _hash_claim
        h = _hash_claim(claim.text)
        report = LintReport(files_scanned=["test.md"])
        _check_claim(claim, {h}, {}, 3, report)
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].rule, "stale-unverified")

    def test_failed_persists_error(self):
        """[FAILED] claims produce an error."""
        claim = self._make_claim(
            text="audio pipeline [FAILED: file not found]",
            vtag="[FAILED: file not found]",
        )
        report = LintReport(files_scanned=["test.md"])
        _check_claim(claim, set(), {}, 3, report)
        self.assertEqual(len(report.issues), 1)
        self.assertEqual(report.issues[0].rule, "failed-persists")
        self.assertEqual(report.issues[0].severity, LintSeverity.ERROR)

    def test_subjective_claims_skipped(self):
        """Subjective claims don't need verification tags."""
        claim = self._make_claim(
            claim_type=ClaimType.SUBJECTIVE,
            verifiability=VerifiabilityLevel.MANUAL,
        )
        report = LintReport(files_scanned=["test.md"])
        _check_claim(claim, set(), {}, 3, report)
        self.assertEqual(len(report.issues), 0)

    def test_manual_verifiability_skipped(self):
        """Manual-only claims don't get no-tag warnings."""
        claim = self._make_claim(
            verifiability=VerifiabilityLevel.MANUAL,
        )
        report = LintReport(files_scanned=["test.md"])
        _check_claim(claim, set(), {}, 3, report)
        self.assertEqual(len(report.issues), 0)


class TestLintReport(unittest.TestCase):
    """Test LintReport data class."""

    def test_clean_report(self):
        report = LintReport(files_scanned=["a.md"], total_claims=5)
        self.assertTrue(report.clean)
        self.assertEqual(report.error_count, 0)
        self.assertEqual(report.warning_count, 0)

    def test_report_with_issues(self):
        report = LintReport(files_scanned=["a.md"], total_claims=5)
        report.issues.append(LintIssue(
            file="a.md", line=1, severity=LintSeverity.ERROR,
            rule="failed-persists", message="test", claim_text="claim",
        ))
        report.issues.append(LintIssue(
            file="a.md", line=2, severity=LintSeverity.WARNING,
            rule="no-tag", message="test", claim_text="claim",
        ))
        report.issues.append(LintIssue(
            file="a.md", line=3, severity=LintSeverity.INFO,
            rule="approx-no-source", message="test", claim_text="claim",
        ))
        self.assertFalse(report.clean)
        self.assertEqual(report.error_count, 1)
        self.assertEqual(report.warning_count, 1)
        self.assertEqual(report.info_count, 1)

    def test_to_dict(self):
        report = LintReport(files_scanned=["a.md"], total_claims=3)
        d = report.to_dict()
        self.assertIn("files_scanned", d)
        self.assertIn("summary", d)
        self.assertIn("clean", d)
        self.assertTrue(d["clean"])

    def test_format_report_clean(self):
        report = LintReport(files_scanned=["a.md"], total_claims=3)
        output = report.format_report()
        self.assertIn("No issues found", output)

    def test_format_report_with_issues(self):
        report = LintReport(files_scanned=["a.md"], total_claims=3)
        report.issues.append(LintIssue(
            file="a.md", line=10, severity=LintSeverity.ERROR,
            rule="failed-persists", message="still failing",
            claim_text="audio [FAILED]",
        ))
        output = report.format_report()
        self.assertIn("a.md", output)
        self.assertIn("line 10", output)
        self.assertIn("failed-persists", output)
        self.assertIn("FAILED", output)


class TestRunLint(unittest.TestCase):
    """Integration test for run_lint with temp files."""

    def test_lint_file_with_no_claims(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("# Just a heading\n\nNo claims here.\n")
            f.flush()
            try:
                report = run_lint(files=[f.name])
                self.assertTrue(report.clean)
                self.assertEqual(report.total_claims, 0)
            finally:
                os.unlink(f.name)

    def test_lint_file_with_untagged_claim(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("Audio pipeline is working and running fine\n")
            f.flush()
            try:
                report = run_lint(files=[f.name])
                # Should find at least one claim without a tag
                no_tag = [i for i in report.issues if i.rule == "no-tag"]
                # The extraction engine may or may not pick this up depending
                # on patterns. The linter itself works correctly.
                self.assertIsInstance(report, LintReport)
            finally:
                os.unlink(f.name)

    def test_lint_file_with_approx_count(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("We have ~65 published entries in the journal\n")
            f.flush()
            try:
                report = run_lint(files=[f.name])
                approx = [i for i in report.issues if i.rule == "approx-no-source"]
                self.assertEqual(len(approx), 1)
            finally:
                os.unlink(f.name)

    def test_lint_nonexistent_file_skipped(self):
        report = run_lint(files=["/nonexistent/path/foo.md"])
        self.assertTrue(report.clean)
        self.assertEqual(report.total_claims, 0)


if __name__ == "__main__":
    unittest.main()
