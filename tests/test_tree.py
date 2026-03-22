"""Tests for the knowledge tree factual health scanner (confab tree)."""

import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from confab.tree import check_tree, TreeHealthReport, TreeIssue, DEFAULT_STALE_DAYS


def _make_tree(nodes: dict) -> str:
    """Write a temporary KNOWLEDGE_TREE.json and return the path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="test_tree_"
    )
    json.dump({"nodes": nodes}, tmp)
    tmp.flush()
    return tmp.name


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


class TestCheckTreeEmpty(unittest.TestCase):
    """Test with an empty or minimal tree."""

    def test_empty_tree(self):
        path = _make_tree({})
        report = check_tree(tree_path=path)
        self.assertEqual(report.total_observations, 0)
        self.assertEqual(report.total_issues, 0)
        self.assertFalse(report.has_issues)

    def test_no_observations(self):
        nodes = {
            "idea-001": {
                "type": "idea",
                "status": "active",
                "content": "Some idea",
                "supports": ["obs-001"],
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(report.total_observations, 0)
        self.assertFalse(report.has_issues)


class TestCheckTreeExpired(unittest.TestCase):
    """Test detection of expired observations."""

    def test_expired_observation(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Fed rate at 3.50% as of 2026-01-15",
                "expires": "2026-02-01",
                "domain": "finance",
                "source": "web search",
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.expired), 1)
        self.assertEqual(report.expired[0].entry_id, "obs-001")
        self.assertEqual(report.expired[0].category, "expired")
        self.assertTrue(report.has_expired)

    def test_not_yet_expired(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Some future fact",
                "expires": future,
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.expired), 0)

    def test_invalidated_not_counted(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "invalidated",
                "content": "Old expired thing",
                "expires": "2025-01-01",
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(report.total_observations, 0)
        self.assertEqual(len(report.expired), 0)


class TestCheckTreePerishable(unittest.TestCase):
    """Test detection of perishable observations without TTL."""

    def test_date_pattern(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "LKNCY Q4 earnings reported on 2026-02-26",
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.perishable_no_ttl), 1)
        self.assertIn("date", report.perishable_no_ttl[0].matched_patterns)

    def test_price_pattern(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "NVDA trading at $189 per share",
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.perishable_no_ttl), 1)
        self.assertIn("price", report.perishable_no_ttl[0].matched_patterns)

    def test_percentage_pattern(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Unemployment rate rose to 4.2% in February",
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.perishable_no_ttl), 1)
        self.assertIn("percentage", report.perishable_no_ttl[0].matched_patterns)

    def test_financial_term_pattern(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "CPI data shows persistent inflation in services sector",
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.perishable_no_ttl), 1)
        self.assertIn("financial-term", report.perishable_no_ttl[0].matched_patterns)

    def test_perishable_with_expires_not_flagged(self):
        """Observations with time-sensitive content AND an expires date should NOT be flagged."""
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Fed rate at 3.50% as of 2026-03-01",
                "expires": future,
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.perishable_no_ttl), 0)

    def test_non_perishable_not_flagged(self):
        """Observations without time-sensitive content should NOT be flagged."""
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Dennis prefers simplicity over complexity in system design",
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.perishable_no_ttl), 0)


class TestCheckTreeStaleUnverified(unittest.TestCase):
    """Test detection of stale unverified observations."""

    def test_stale_unverified(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Some claim from training data",
                "verified": "unverified",
                "created": _days_ago(20),
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path, stale_days=14)
        self.assertEqual(len(report.stale_unverified), 1)
        self.assertEqual(report.stale_unverified[0].category, "stale_unverified")

    def test_recent_unverified_not_flagged(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Just added this observation",
                "verified": "unverified",
                "created": _days_ago(3),
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path, stale_days=14)
        self.assertEqual(len(report.stale_unverified), 0)

    def test_verified_observation_not_flagged(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Verified fact from web search",
                "verified": "web_search",
                "created": _days_ago(30),
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.stale_unverified), 0)

    def test_unverified_no_created_flagged(self):
        """Unverified obs with no created date — conservative: flag it."""
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "No date on this one",
                "verified": "unverified",
            }
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertEqual(len(report.stale_unverified), 1)

    def test_custom_stale_days(self):
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Claim from 5 days ago",
                "verified": "unverified",
                "created": _days_ago(5),
            }
        }
        path = _make_tree(nodes)
        # With 3-day threshold, this should be flagged
        report = check_tree(tree_path=path, stale_days=3)
        self.assertEqual(len(report.stale_unverified), 1)
        # With 7-day threshold, it should not
        report = check_tree(tree_path=path, stale_days=7)
        self.assertEqual(len(report.stale_unverified), 0)


class TestCoverageMetrics(unittest.TestCase):
    """Test TTL and verification coverage calculations."""

    def test_ttl_coverage(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        nodes = {
            "obs-001": {
                "type": "observation", "status": "active",
                "content": "Price at $100", "expires": future,
            },
            "obs-002": {
                "type": "observation", "status": "active",
                "content": "Rate at 5.0%",  # perishable, no expires
            },
            "obs-003": {
                "type": "observation", "status": "active",
                "content": "Non-perishable fact",  # no time-sensitive content
            },
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        # 2 perishable observations (obs-001 and obs-002), 1 has expires
        self.assertEqual(report.ttl_coverage, 50.0)

    def test_verified_coverage(self):
        nodes = {
            "obs-001": {
                "type": "observation", "status": "active",
                "content": "Fact A", "verified": "web_search",
            },
            "obs-002": {
                "type": "observation", "status": "active",
                "content": "Fact B", "verified": "file_read",
            },
            "obs-003": {
                "type": "observation", "status": "active",
                "content": "Fact C",  # no verified field
            },
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)
        self.assertAlmostEqual(report.verified_coverage, 66.7, places=0)


class TestReportFormatting(unittest.TestCase):
    """Test report format methods."""

    def _make_report(self, **kwargs):
        defaults = dict(
            tree_path="core/knowledge/KNOWLEDGE_TREE.json",
            scan_date="2026-03-21",
            total_observations=100,
            expired=[],
            perishable_no_ttl=[],
            stale_unverified=[],
            ttl_coverage=15.0,
            verified_coverage=5.0,
            stale_threshold_days=14,
        )
        defaults.update(kwargs)
        return TreeHealthReport(**defaults)

    def test_clean_report(self):
        report = self._make_report()
        text = report.format_report()
        self.assertIn("CLEAN", text)

    def test_report_with_expired(self):
        issue = TreeIssue(
            entry_id="obs-001",
            content="Expired fact",
            category="expired",
            domain="finance",
            source="web search",
            expires="2026-03-01",
        )
        report = self._make_report(expired=[issue])
        text = report.format_report()
        self.assertIn("EXPIRED (1)", text)
        self.assertIn("obs-001", text)
        self.assertIn("2026-03-01", text)

    def test_slack_format(self):
        issue = TreeIssue(
            entry_id="obs-001",
            content="Expired fact",
            category="expired",
            domain="finance",
            source="web search",
            expires="2026-03-01",
        )
        report = self._make_report(expired=[issue])
        text = report.format_slack()
        self.assertIn(":x:", text)
        self.assertIn("obs-001", text)

    def test_slack_clean(self):
        report = self._make_report()
        text = report.format_slack()
        self.assertIn(":white_check_mark:", text)

    def test_summary_line(self):
        report = self._make_report(perishable_no_ttl=[
            TreeIssue(entry_id="obs-001", content="X", category="perishable_no_ttl",
                      domain=None, source=None)
        ])
        line = report.format_summary_line()
        self.assertIn("1 no-TTL", line)

    def test_to_dict(self):
        report = self._make_report()
        d = report.to_dict()
        self.assertEqual(d["total_observations"], 100)
        self.assertEqual(d["expired"], 0)
        self.assertIn("ttl_coverage", d)

    def test_report_perishable_section(self):
        issues = [
            TreeIssue(
                entry_id=f"obs-{i:03d}",
                content=f"Price at ${i * 10}",
                category="perishable_no_ttl",
                domain="finance",
                source=None,
                matched_patterns=["price"],
            )
            for i in range(20)
        ]
        report = self._make_report(perishable_no_ttl=issues)
        text = report.format_report()
        self.assertIn("PERISHABLE WITHOUT TTL (20)", text)
        self.assertIn("...and 5 more", text)


class TestMixedTree(unittest.TestCase):
    """Test with a realistic mix of entries."""

    def test_mixed_entries(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        nodes = {
            # Expired
            "obs-001": {
                "type": "observation", "status": "active",
                "content": "CPI at 3.2% for January",
                "expires": "2026-02-15", "domain": "finance",
            },
            # Perishable without TTL
            "obs-002": {
                "type": "observation", "status": "active",
                "content": "NVDA at $189 after GTC keynote",
                "domain": "finance",
            },
            # Stale unverified
            "obs-003": {
                "type": "observation", "status": "active",
                "content": "Some claim from LLM training data",
                "verified": "unverified", "created": _days_ago(20),
            },
            # Healthy — has expires, verified
            "obs-004": {
                "type": "observation", "status": "active",
                "content": "Fed rate 3.50-3.75%",
                "expires": future, "verified": "web_search",
            },
            # Healthy — not perishable
            "obs-005": {
                "type": "observation", "status": "active",
                "content": "Dennis prefers simplicity",
            },
            # Non-observation — should be skipped
            "idea-001": {
                "type": "idea", "status": "active",
                "content": "Some idea with $100 reference",
                "supports": ["obs-001"],
            },
            # Invalidated — should be skipped
            "obs-006": {
                "type": "observation", "status": "invalidated",
                "content": "Old wrong thing at $50",
            },
        }
        path = _make_tree(nodes)
        report = check_tree(tree_path=path)

        self.assertEqual(report.total_observations, 5)  # obs-001 through obs-005
        self.assertEqual(len(report.expired), 1)
        self.assertEqual(len(report.perishable_no_ttl), 1)
        self.assertEqual(len(report.stale_unverified), 1)
        self.assertEqual(report.total_issues, 3)
        self.assertTrue(report.has_issues)
        self.assertTrue(report.has_expired)

        # Coverage: 1 perishable with expires (obs-004) out of 3 perishable total
        # (obs-001 is expired so counted separately, obs-002 perishable no ttl, obs-004 perishable with ttl)
        # obs-001 has expires but is expired — still counts as perishable_with_expires
        # Actually obs-001 hits the expired check first and `continue`s, so it's not in perishable count
        # Perishable total = obs-002 (no TTL) + obs-004 (has TTL) = 2
        # Perishable with expires = obs-004 = 1
        self.assertEqual(report.ttl_coverage, 50.0)


if __name__ == "__main__":
    unittest.main()
