"""Tests for the system health report (confab report)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from confab.cli import _format_health_dashboard, _format_health_slack
from confab.config import ConfabConfig, set_config, reset_config
from confab.gate import GateReport
from confab.supports import SupportsReport, WeakEntry


class TestFormatHealthDashboard(unittest.TestCase):
    """Test the terminal dashboard formatter."""

    def _make_gate(self, **kwargs):
        defaults = dict(
            timestamp="2026-03-20T13:00:00",
            files_scanned=["builder_priorities.md"],
            total_claims=10,
            auto_verified=8,
            passed=6,
            failed=0,
            inconclusive=2,
            skipped=2,
            stale_claims=0,
            failed_details=[],
            stale_details=[],
            all_outcomes=[],
        )
        defaults.update(kwargs)
        return GateReport(**defaults)

    def _make_supports(self, **kwargs):
        defaults = dict(
            tree_path="/test/KNOWLEDGE_TREE.json",
            total_entries=100,
            checked_entries=50,
            total_supports_checked=120,
            zombies=[],
            weakened=[],
            degraded=[],
            healthy=50,
            no_supports=5,
            invalidated_count=20,
            by_type={"idea": {"checked": 40, "zombie": 0, "weakened": 0, "degraded": 0, "healthy": 40}},
            by_domain={"technology": {"checked": 30, "zombie": 0, "weakened": 0, "degraded": 0}},
        )
        defaults.update(kwargs)
        return SupportsReport(**defaults)

    def _make_zombie(self, entry_id="idea-099"):
        return WeakEntry(
            entry_id=entry_id,
            entry_type="idea",
            content="Test zombie entry",
            domain="technology",
            total_supports=3,
            dead_supports=3,
            dead_ids=["obs-1", "obs-2", "obs-3"],
            missing_ids=[],
        )

    def test_healthy_dashboard(self):
        gate = self._make_gate()
        supports = self._make_supports()
        output = _format_health_dashboard(gate, supports)

        self.assertIn("CONFAB SYSTEM HEALTH REPORT", output)
        self.assertIn("CLAIMS", output)
        self.assertIn("KNOWLEDGE TREE SUPPORTS", output)
        self.assertIn("VERIFICATION COVERAGE", output)
        self.assertIn("STATUS: HEALTHY", output)

    def test_critical_dashboard_with_failures(self):
        gate = self._make_gate(
            failed=2,
            failed_details=[
                {"claim_text": "missing file", "claim_type": "file_exists",
                 "evidence": "NOT FOUND", "action": "fix"},
                {"claim_text": "bad env", "claim_type": "env_var",
                 "evidence": "NOT SET", "action": "fix"},
            ],
        )
        supports = self._make_supports()
        output = _format_health_dashboard(gate, supports)

        self.assertIn("FAILURES (2)", output)
        self.assertIn("STATUS: CRITICAL", output)

    def test_critical_dashboard_with_zombies(self):
        zombie = self._make_zombie()
        gate = self._make_gate()
        supports = self._make_supports(
            zombies=[zombie],
            healthy=49,
        )
        output = _format_health_dashboard(gate, supports)

        self.assertIn("Zombies: 1", output)
        self.assertIn("idea-099", output)
        self.assertIn("STATUS: CRITICAL", output)

    def test_warning_dashboard_with_stale(self):
        gate = self._make_gate(
            stale_claims=2,
            stale_details=[
                {"claim_text": "old claim", "claim_type": "status", "age_builds": 5},
                {"claim_text": "older claim", "claim_type": "env_var", "age_builds": 8},
            ],
        )
        supports = self._make_supports()
        output = _format_health_dashboard(gate, supports)

        self.assertIn("STALE (2)", output)
        self.assertIn("STATUS: WARNING", output)

    def test_dashboard_without_supports(self):
        gate = self._make_gate()
        output = _format_health_dashboard(gate, None)

        self.assertIn("unavailable", output)
        self.assertIn("STATUS: HEALTHY", output)

    def test_coverage_calculation(self):
        gate = self._make_gate(total_claims=10, passed=5)
        supports = self._make_supports(checked_entries=90, healthy=85)
        output = _format_health_dashboard(gate, supports)

        # 5 + 85 = 90 verified out of 10 + 90 = 100 total = 90%
        self.assertIn("90.0%", output)
        self.assertIn("90/100", output)


class TestFormatHealthSlack(unittest.TestCase):
    """Test the Slack-friendly health formatter."""

    def _make_gate(self, **kwargs):
        defaults = dict(
            timestamp="2026-03-20T13:00:00",
            files_scanned=["test.md"],
            total_claims=5,
            auto_verified=3,
            passed=3,
            failed=0,
            inconclusive=1,
            skipped=1,
            stale_claims=0,
            failed_details=[],
            stale_details=[],
            all_outcomes=[],
        )
        defaults.update(kwargs)
        return GateReport(**defaults)

    def _make_supports(self, **kwargs):
        defaults = dict(
            tree_path="/test/tree.json",
            total_entries=100,
            checked_entries=50,
            total_supports_checked=120,
            zombies=[],
            weakened=[],
            degraded=[],
            healthy=50,
            no_supports=5,
            invalidated_count=10,
            by_type={},
            by_domain={},
        )
        defaults.update(kwargs)
        return SupportsReport(**defaults)

    def test_clean_slack(self):
        gate = self._make_gate()
        supports = self._make_supports()
        output = _format_health_slack(gate, supports)

        self.assertIn("Gate CLEAN", output)
        self.assertIn("Supports CLEAN", output)
        self.assertIn("Coverage:", output)

    def test_slack_with_failures(self):
        gate = self._make_gate(
            failed=1,
            failed_details=[{"claim_text": "bad", "evidence": "MISSING", "action": "fix"}],
        )
        supports = self._make_supports()
        output = _format_health_slack(gate, supports)

        self.assertIn(":x:", output)
        self.assertIn("1 failed", output)

    def test_slack_with_zombies(self):
        zombie = WeakEntry(
            entry_id="idea-001",
            entry_type="idea",
            content="Test",
            domain="test",
            total_supports=1,
            dead_supports=1,
            dead_ids=["obs-1"],
            missing_ids=[],
        )
        gate = self._make_gate()
        supports = self._make_supports(zombies=[zombie], healthy=49)
        output = _format_health_slack(gate, supports)

        self.assertIn(":skull:", output)
        self.assertIn("1 zombie", output)

    def test_coverage_percentage(self):
        gate = self._make_gate(total_claims=10, passed=5)
        supports = self._make_supports(checked_entries=10, healthy=5)
        output = _format_health_slack(gate, supports)

        # 5 + 5 = 10 verified out of 10 + 10 = 20 total = 50%
        self.assertIn("Coverage: 50%", output)


class TestPerFileBreakdown(unittest.TestCase):
    """Test per-file breakdown in the dashboard when multiple files are scanned."""

    def _make_outcome(self, source_file, result_value):
        from confab.claims import Claim, ClaimType, VerifiabilityLevel
        from confab.verify import VerificationOutcome, VerificationResult
        claim = Claim(
            text="test claim",
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
            source_file=source_file,
        )
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult(result_value),
            evidence="test evidence",
            checked_at="2026-03-30T22:00:00",
            method="test",
        )

    def _make_gate(self, **kwargs):
        defaults = dict(
            timestamp="2026-03-30T22:00:00",
            files_scanned=["file_a.md", "file_b.md"],
            total_claims=6,
            auto_verified=6,
            passed=4,
            failed=2,
            inconclusive=0,
            skipped=0,
            stale_claims=0,
            failed_details=[
                {"claim_text": "missing", "evidence": "NOT FOUND", "action": "fix"},
                {"claim_text": "also missing", "evidence": "NOT FOUND", "action": "fix"},
            ],
            stale_details=[],
            all_outcomes=[
                self._make_outcome("file_a.md", "passed"),
                self._make_outcome("file_a.md", "passed"),
                self._make_outcome("file_a.md", "passed"),
                self._make_outcome("file_b.md", "passed"),
                self._make_outcome("file_b.md", "failed"),
                self._make_outcome("file_b.md", "failed"),
            ],
        )
        defaults.update(kwargs)
        return GateReport(**defaults)

    def test_per_file_breakdown_shown(self):
        gate = self._make_gate()
        output = _format_health_dashboard(gate, None)

        self.assertIn("PER-FILE BREAKDOWN", output)
        self.assertIn("file_a.md", output)
        self.assertIn("file_b.md", output)

    def test_per_file_breakdown_hidden_single_file(self):
        gate = self._make_gate(files_scanned=["only_one.md"])
        output = _format_health_dashboard(gate, None)

        self.assertNotIn("PER-FILE BREAKDOWN", output)

    def test_per_file_risk_order(self):
        """Files with failures should appear first in the breakdown."""
        gate = self._make_gate()
        output = _format_health_dashboard(gate, None)

        # Extract just the PER-FILE BREAKDOWN section
        breakdown_start = output.index("PER-FILE BREAKDOWN")
        breakdown = output[breakdown_start:]
        # file_b has 2 failures, should come before file_a in the breakdown
        b_pos = breakdown.index("file_b.md")
        a_pos = breakdown.index("file_a.md")
        self.assertLess(b_pos, a_pos, "File with failures should appear first in breakdown")


class TestRecommendedActions(unittest.TestCase):
    """Test the recommended actions section."""

    def _make_gate(self, **kwargs):
        defaults = dict(
            timestamp="2026-03-30T22:00:00",
            files_scanned=["test.md"],
            total_claims=5,
            auto_verified=5,
            passed=5,
            failed=0,
            inconclusive=0,
            skipped=0,
            stale_claims=0,
            failed_details=[],
            stale_details=[],
            all_outcomes=[],
        )
        defaults.update(kwargs)
        return GateReport(**defaults)

    def _make_supports(self, **kwargs):
        defaults = dict(
            tree_path="/test/tree.json",
            total_entries=50,
            checked_entries=30,
            total_supports_checked=60,
            zombies=[],
            weakened=[],
            degraded=[],
            healthy=30,
            no_supports=5,
            invalidated_count=10,
            by_type={},
            by_domain={},
        )
        defaults.update(kwargs)
        return SupportsReport(**defaults)

    def test_no_actions_when_healthy(self):
        gate = self._make_gate()
        supports = self._make_supports()
        output = _format_health_dashboard(gate, supports)

        self.assertNotIn("RECOMMENDED ACTIONS", output)

    def test_actions_for_failures(self):
        gate = self._make_gate(
            failed=3,
            failed_details=[
                {"claim_text": "x", "evidence": "y", "action": "fix"},
            ] * 3,
        )
        output = _format_health_dashboard(gate, None)

        self.assertIn("RECOMMENDED ACTIONS", output)
        self.assertIn("Fix 3 failed claim(s)", output)

    def test_actions_for_stale(self):
        gate = self._make_gate(
            stale_claims=2,
            stale_details=[
                {"claim_text": "old", "claim_type": "status", "age_builds": 5},
            ] * 2,
        )
        output = _format_health_dashboard(gate, None)

        self.assertIn("RECOMMENDED ACTIONS", output)
        self.assertIn("Re-verify 2 stale claim(s)", output)

    def test_actions_for_zombies(self):
        zombie = WeakEntry(
            entry_id="idea-001",
            entry_type="idea",
            content="Test",
            domain="test",
            total_supports=1,
            dead_supports=1,
            dead_ids=["obs-1"],
            missing_ids=[],
        )
        gate = self._make_gate()
        supports = self._make_supports(zombies=[zombie], healthy=29)
        output = _format_health_dashboard(gate, supports)

        self.assertIn("RECOMMENDED ACTIONS", output)
        self.assertIn("confab check-supports --fix", output)


class TestReportWithoutTree(unittest.TestCase):
    """Test that report works cleanly when tree/supports are unavailable."""

    def _make_gate(self, **kwargs):
        defaults = dict(
            timestamp="2026-03-30T22:00:00",
            files_scanned=["docs/README.md"],
            total_claims=3,
            auto_verified=3,
            passed=3,
            failed=0,
            inconclusive=0,
            skipped=0,
            stale_claims=0,
            failed_details=[],
            stale_details=[],
            all_outcomes=[],
        )
        defaults.update(kwargs)
        return GateReport(**defaults)

    def test_dashboard_no_tree_no_supports(self):
        gate = self._make_gate()
        output = _format_health_dashboard(gate, None, None)

        self.assertIn("CLAIMS", output)
        self.assertIn("unavailable", output)
        self.assertIn("STATUS: HEALTHY", output)
        # Should still show claims-only coverage
        self.assertIn("Claims verified: 3/3", output)

    def test_dashboard_no_tree_shows_clean_output(self):
        """External user experience: just claims, no tree noise."""
        gate = self._make_gate()
        output = _format_health_dashboard(gate, None, None)

        # Should NOT have zombie/weakened details
        self.assertNotIn("Zombies", output)
        self.assertNotIn("Weakened", output)
        # Should show the tree sections as unavailable
        self.assertIn("knowledge tree not found", output)


if __name__ == "__main__":
    unittest.main()
