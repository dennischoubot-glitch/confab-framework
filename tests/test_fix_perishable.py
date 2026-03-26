"""Tests for confab fix --perishable (perishable observation TTL assignment)."""

import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

from confab.cli import _propose_expires, cmd_fix_perishable


def _make_tree(nodes: dict) -> str:
    """Write a temporary KNOWLEDGE_TREE.json and return the path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="test_fix_perishable_"
    )
    json.dump({"nodes": nodes}, tmp)
    tmp.flush()
    return tmp.name


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


class TestProposeExpires(unittest.TestCase):
    """Test the _propose_expires heuristic function."""

    def test_iso_date_in_content(self):
        """ISO date in content -> date + 1 day."""
        date, rule = _propose_expires(
            "Fed meeting on 2026-03-19 decided rates",
            "2026-03-10T00:00:00+00:00",
            ["date"]
        )
        self.assertEqual(date, "2026-03-20")
        self.assertEqual(rule, "event_date+1d")

    def test_multiple_iso_dates_uses_latest(self):
        """Multiple ISO dates -> latest + 1 day."""
        date, rule = _propose_expires(
            "Between 2026-01-15 and 2026-06-30 the rate changed",
            "2026-01-01T00:00:00+00:00",
            ["date"]
        )
        self.assertEqual(date, "2026-07-01")
        self.assertEqual(rule, "event_date+1d")

    def test_month_day_date(self):
        """Month-day format (Feb 26) -> date + 1 day."""
        date, rule = _propose_expires(
            "LKNCY Q4 earnings Feb 26",
            "2026-02-20T00:00:00+00:00",
            ["date"]
        )
        self.assertEqual(rule, "event_date+1d")
        self.assertEqual(date, f"{datetime.now(timezone.utc).year}-02-27")

    def test_month_day_year(self):
        """Month day, year format (Mar 15, 2026) -> date + 1 day."""
        date, rule = _propose_expires(
            "Deadline is Mar 15, 2026 for submission",
            "2026-03-01T00:00:00+00:00",
            ["date"]
        )
        self.assertEqual(date, "2026-03-16")
        self.assertEqual(rule, "event_date+1d")

    def test_price_pattern(self):
        """Dollar amount without dates -> created + 30 days."""
        date, rule = _propose_expires(
            "TSMC produces ~90% of advanced semiconductors at $500 per wafer",
            "2026-02-09T00:00:00+00:00",
            ["price", "percentage"]
        )
        self.assertEqual(date, "2026-03-11")
        self.assertEqual(rule, "price_or_pct+30d")

    def test_percentage_pattern(self):
        """Percentage without dates -> created + 30 days."""
        date, rule = _propose_expires(
            "Market share dropped to 45% in the quarter",
            "2026-03-01T00:00:00+00:00",
            ["percentage"]
        )
        self.assertEqual(date, "2026-03-31")
        self.assertEqual(rule, "price_or_pct+30d")

    def test_financial_term_pattern(self):
        """Financial term without dates/prices -> created + 60 days."""
        date, rule = _propose_expires(
            "The earnings report showed strong revenue growth",
            "2026-02-01T00:00:00+00:00",
            ["financial-term"]
        )
        self.assertEqual(date, "2026-04-02")
        self.assertEqual(rule, "financial_term+60d")

    def test_fallback(self):
        """Unknown patterns -> created + 60 days."""
        date, rule = _propose_expires(
            "Some generic content with time-sensitive marker",
            "2026-03-01T00:00:00+00:00",
            ["time-sensitive"]
        )
        self.assertEqual(date, "2026-04-30")
        self.assertEqual(rule, "fallback+60d")

    def test_no_created_date_uses_today(self):
        """Missing created date -> use today as base."""
        date, rule = _propose_expires(
            "Rate at 5.0%",
            None,
            ["percentage"]
        )
        expected = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
        self.assertEqual(date, expected)
        self.assertEqual(rule, "price_or_pct+30d")

    def test_date_takes_priority_over_price(self):
        """When content has both dates and prices, dates win."""
        date, rule = _propose_expires(
            "On 2026-05-01 the price hit $500",
            "2026-03-01T00:00:00+00:00",
            ["date", "price"]
        )
        self.assertEqual(date, "2026-05-02")
        self.assertEqual(rule, "event_date+1d")


class TestFixPerishableDryRun(unittest.TestCase):
    """Test that dry-run mode doesn't modify the tree file."""

    def test_dry_run_does_not_write(self):
        """Default (no --apply) should not modify the tree."""
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Rate at 5.0% as of 2026-03-01",
                "created": "2026-03-01T00:00:00+00:00",
            }
        }
        path = _make_tree(nodes)
        original = Path(path).read_text()

        # Simulate args
        class Args:
            perishable = True
            apply = False
            json = False
            tree = path

        cmd_fix_perishable(Args())

        # File should be unchanged
        self.assertEqual(Path(path).read_text(), original)

    def test_dry_run_no_perishable(self):
        """Tree with no perishable observations prints clean message."""
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "A philosophical observation with no dates or prices",
            }
        }
        path = _make_tree(nodes)

        class Args:
            perishable = True
            apply = False
            json = False
            tree = path

        # Should not raise
        cmd_fix_perishable(Args())


class TestFixPerishableApply(unittest.TestCase):
    """Test that --apply writes correct expires dates."""

    def test_apply_writes_expires(self):
        """--apply should add expires field to perishable observations."""
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Fed rate at 5.25% on 2026-03-19",
                "created": "2026-03-10T00:00:00+00:00",
            },
            "obs-002": {
                "type": "observation",
                "status": "active",
                "content": "A timeless philosophical truth",
            },
            "obs-003": {
                "type": "observation",
                "status": "active",
                "content": "Stock price hit $500",
                "created": "2026-03-01T00:00:00+00:00",
                "expires": "2026-04-01",  # already has expires
            }
        }
        path = _make_tree(nodes)

        class Args:
            perishable = True
            apply = True
            json = False
            tree = path

        cmd_fix_perishable(Args())

        # Read back the tree
        updated = json.loads(Path(path).read_text())

        # obs-001 should have expires (ISO date 2026-03-19 + 1 day)
        self.assertEqual(updated["nodes"]["obs-001"]["expires"], "2026-03-20")

        # obs-002 should NOT have expires (not perishable)
        self.assertNotIn("expires", updated["nodes"]["obs-002"])

        # obs-003 should keep its original expires (not touched)
        self.assertEqual(updated["nodes"]["obs-003"]["expires"], "2026-04-01")

    def test_apply_preserves_other_fields(self):
        """--apply should only add expires, not modify other fields."""
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Rate at 5.0%",
                "created": "2026-03-01T00:00:00+00:00",
                "domain": "finance",
                "tags": ["operational"],
                "salience": 1.5,
            }
        }
        path = _make_tree(nodes)

        class Args:
            perishable = True
            apply = True
            json = False
            tree = path

        cmd_fix_perishable(Args())

        updated = json.loads(Path(path).read_text())
        node = updated["nodes"]["obs-001"]
        self.assertEqual(node["domain"], "finance")
        self.assertEqual(node["tags"], ["operational"])
        self.assertEqual(node["salience"], 1.5)
        self.assertIn("expires", node)


class TestFixPerishableJson(unittest.TestCase):
    """Test JSON output mode."""

    def test_json_output_structure(self):
        """JSON output should have the expected structure."""
        nodes = {
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Price hit $200 on 2026-03-15",
                "created": "2026-03-10T00:00:00+00:00",
            }
        }
        path = _make_tree(nodes)

        import io
        from contextlib import redirect_stdout

        class Args:
            perishable = True
            apply = False
            json = True
            tree = path

        f = io.StringIO()
        with redirect_stdout(f):
            cmd_fix_perishable(Args())

        output = json.loads(f.getvalue())
        self.assertIn("total_perishable_no_ttl", output)
        self.assertIn("proposals", output)
        self.assertEqual(output["mode"], "dry-run")
        self.assertEqual(len(output["proposals"]), 1)

        proposal = output["proposals"][0]
        self.assertEqual(proposal["id"], "obs-001")
        self.assertEqual(proposal["proposed_expires"], "2026-03-16")
        self.assertEqual(proposal["rule"], "event_date+1d")


if __name__ == "__main__":
    unittest.main()
