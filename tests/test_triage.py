"""Tests for the confabulation triage module."""

import unittest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from confab.triage import (
    TriageItem,
    BatchGroup,
    TriageReport,
    run_triage,
    SEVERITY_WEIGHTS,
    EFFORT,
)


class TestTriageItem(unittest.TestCase):
    def test_priority_score(self):
        item = TriageItem(
            source="gate", category="gate_failed", severity=10,
            entry_id="abc", summary="test", detail="detail",
            suggested_cmd="fix", effort=2,
        )
        self.assertEqual(item.priority_score, 5.0)

    def test_priority_score_low_effort(self):
        item = TriageItem(
            source="gate", category="gate_stale", severity=5,
            entry_id="abc", summary="test", detail="detail",
            suggested_cmd="fix", effort=1,
        )
        self.assertEqual(item.priority_score, 5.0)

    def test_to_dict(self):
        item = TriageItem(
            source="tree", category="tree_expired", severity=8,
            entry_id="obs-123", summary="test obs", detail="long detail here",
            suggested_cmd="invalidate", effort=1, domain="finance",
        )
        d = item.to_dict()
        self.assertEqual(d["source"], "tree")
        self.assertEqual(d["entry_id"], "obs-123")
        self.assertEqual(d["domain"], "finance")
        self.assertIn("priority_score", d)


class TestRunTriageWithGate(unittest.TestCase):
    def _mock_gate_report(self, failed=None, stale=None, ttl_expired=None, registry=None):
        report = MagicMock()
        report.failed_details = failed or []
        report.stale_details = stale or []
        report.ttl_expired = ttl_expired or []
        report.registry_violations = registry or []
        return report

    def test_empty_gate(self):
        report = run_triage(gate_report=self._mock_gate_report())
        self.assertEqual(report.total_issues, 0)
        self.assertEqual(len(report.items), 0)

    def test_failed_claims_ranked_first(self):
        gate = self._mock_gate_report(
            failed=[{"claim_text": "broken claim", "evidence": "missing", "source_file": "f.md", "tracker_run_count": 1}],
            stale=[{"claim_text": "old claim", "source_file": "f.md", "tracker_run_count": 3}],
        )
        report = run_triage(gate_report=gate)
        self.assertEqual(report.total_issues, 2)
        self.assertEqual(report.items[0].category, "gate_failed")

    def test_stale_severity_scales_with_run_count(self):
        gate = self._mock_gate_report(
            stale=[
                {"claim_text": "old", "source_file": "f.md", "tracker_run_count": 30},
                {"claim_text": "recent", "source_file": "f.md", "tracker_run_count": 3},
            ],
        )
        report = run_triage(gate_report=gate)
        # Higher run count = higher severity = ranked first
        self.assertGreater(report.items[0].severity, report.items[1].severity)


class TestRunTriageWithTree(unittest.TestCase):
    def _mock_tree_report(self, expired=None, stale=None, no_ttl=None):
        report = MagicMock()
        report.expired = expired or []
        report.stale_unverified = stale or []
        report.perishable_no_ttl = no_ttl or []
        return report

    def _mock_issue(self, entry_id, content="test", domain="finance"):
        issue = MagicMock()
        issue.entry_id = entry_id
        issue.content = content
        issue.domain = domain
        return issue

    def test_expired_entries(self):
        tree = self._mock_tree_report(expired=[self._mock_issue("obs-100")])
        report = run_triage(tree_report=tree)
        self.assertEqual(report.total_issues, 1)
        self.assertEqual(report.items[0].category, "tree_expired")
        self.assertIn("invalidate", report.items[0].suggested_cmd)

    def test_no_ttl_limited(self):
        issues = [self._mock_issue(f"obs-{i}") for i in range(100)]
        tree = self._mock_tree_report(no_ttl=issues)
        report = run_triage(tree_report=tree, limit=10)
        # Items limited but summary has full count
        self.assertEqual(len(report.items), 10)
        self.assertEqual(report.summary["tree_no_ttl"], 100)


class TestBatchGroups(unittest.TestCase):
    def test_batches_formed_for_multiple_items(self):
        gate = MagicMock()
        gate.failed_details = []
        gate.ttl_expired = []
        gate.registry_violations = []
        gate.stale_details = [
            {"claim_text": f"stale {i}", "source_file": "f.md", "tracker_run_count": 5}
            for i in range(4)
        ]
        report = run_triage(gate_report=gate)
        stale_batch = [b for b in report.batches if b.category == "gate_stale"]
        self.assertEqual(len(stale_batch), 1)
        self.assertEqual(stale_batch[0].count, 4)

    def test_no_batch_for_single_item(self):
        gate = MagicMock()
        gate.failed_details = [{"claim_text": "one", "evidence": "", "source_file": "f.md", "tracker_run_count": 1}]
        gate.stale_details = []
        gate.ttl_expired = []
        gate.registry_violations = []
        report = run_triage(gate_report=gate)
        # Only 1 failed item, shouldn't create a batch
        self.assertEqual(len(report.batches), 0)


class TestTriageReport(unittest.TestCase):
    def test_format_report_empty(self):
        report = TriageReport(
            timestamp="2026-03-24T00:00:00Z",
            total_issues=0, items=[], batches=[], summary={},
        )
        text = report.format_report()
        self.assertIn("Total issues: 0", text)

    def test_format_slack_empty(self):
        report = TriageReport(
            timestamp="2026-03-24T00:00:00Z",
            total_issues=0, items=[], batches=[], summary={},
        )
        self.assertIn("CLEAN", report.format_slack())

    def test_format_slack_with_issues(self):
        report = TriageReport(
            timestamp="2026-03-24T00:00:00Z",
            total_issues=3,
            items=[TriageItem(
                source="gate", category="gate_stale", severity=5,
                entry_id="x", summary="test", detail="d",
                suggested_cmd="fix cmd", effort=1,
            )],
            batches=[],
            summary={"gate_stale": 3},
        )
        slack = report.format_slack()
        self.assertIn("gate stale", slack)
        self.assertIn("fix cmd", slack)


if __name__ == "__main__":
    unittest.main()
