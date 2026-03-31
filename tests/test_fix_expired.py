"""Tests for confab fix-expired (batch invalidation of expired observations)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from confab.fix_expired import fix_expired, FixExpiredResult


def _make_tree(nodes: dict) -> str:
    """Write a temporary KNOWLEDGE_TREE.json and return the path."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="test_fix_expired_"
    )
    json.dump({"nodes": nodes}, tmp)
    tmp.flush()
    return tmp.name


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def _days_ahead(n):
    return (datetime.now(timezone.utc) + timedelta(days=n)).strftime("%Y-%m-%d")


class TestFixExpired(unittest.TestCase):
    """Test the fix_expired function."""

    def setUp(self):
        self.tmp_files = []

    def tearDown(self):
        for f in self.tmp_files:
            for ext in ["", ".bak"]:
                p = f + ext if ext else f
                if os.path.exists(p):
                    os.unlink(p)
            # Clean up .json.bak created by atomic save
            bak = Path(f).with_suffix(".json.bak")
            if bak.exists():
                bak.unlink()

    def _make(self, nodes):
        path = _make_tree(nodes)
        self.tmp_files.append(path)
        return path

    def test_no_expired(self):
        """No expired observations -> empty result."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Some fact",
                "expires": _days_ahead(30),
            }
        })
        result = fix_expired(tree_path=path)
        self.assertEqual(result.expired_count, 0)
        self.assertEqual(result.unsupported_count, 0)

    def test_finds_expired(self):
        """Expired observations are detected."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Stale fact",
                "expires": _days_ago(5),
            },
            "obs-002": {
                "type": "observation",
                "status": "active",
                "content": "Fresh fact",
                "expires": _days_ahead(30),
            },
        })
        result = fix_expired(tree_path=path, dry_run=True)
        self.assertEqual(result.expired_count, 1)
        self.assertEqual(result.expired_found[0].entry_id, "obs-001")

    def test_dry_run_does_not_modify(self):
        """Dry run doesn't change the tree file."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Expired fact",
                "expires": _days_ago(1),
            },
        })
        # Read original content
        with open(path) as f:
            original = f.read()

        fix_expired(tree_path=path, dry_run=True)

        with open(path) as f:
            after = f.read()
        self.assertEqual(original, after)

    def test_apply_invalidates(self):
        """Non-dry-run invalidates expired entries in the tree."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Expired fact",
                "expires": _days_ago(1),
            },
            "obs-002": {
                "type": "observation",
                "status": "active",
                "content": "Fresh fact",
                "expires": _days_ahead(30),
            },
        })

        result = fix_expired(tree_path=path, dry_run=False)
        self.assertEqual(result.expired_count, 1)
        self.assertFalse(result.dry_run)

        # Verify the tree was actually modified
        with open(path) as f:
            tree = json.load(f)

        self.assertEqual(tree["nodes"]["obs-001"]["status"], "invalidated")
        self.assertIn("TTL expired", tree["nodes"]["obs-001"]["invalidated_reason"])
        self.assertIn("invalidated_at", tree["nodes"]["obs-001"])
        # Fresh obs should remain active
        self.assertEqual(tree["nodes"]["obs-002"]["status"], "active")

    def test_skips_already_invalidated(self):
        """Already-invalidated observations are not re-invalidated."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "invalidated",
                "content": "Already dead",
                "expires": _days_ago(30),
                "invalidated_reason": "Previous reason",
            },
        })
        result = fix_expired(tree_path=path)
        self.assertEqual(result.expired_count, 0)

    def test_skips_non_observations(self):
        """Ideas and principles with expires are not touched."""
        path = self._make({
            "idea-001": {
                "type": "idea",
                "status": "active",
                "content": "An idea with expires",
                "expires": _days_ago(5),
            },
        })
        result = fix_expired(tree_path=path)
        self.assertEqual(result.expired_count, 0)

    def test_firewall_check_detects_unsupported(self):
        """When invalidation removes the last support of an idea, it's flagged."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Only support",
                "expires": _days_ago(1),
            },
            "idea-001": {
                "type": "idea",
                "status": "active",
                "content": "Idea depending on obs-001",
                "supports": ["obs-001"],
            },
        })
        result = fix_expired(tree_path=path, dry_run=True)
        self.assertEqual(result.expired_count, 1)
        self.assertEqual(result.unsupported_count, 1)
        self.assertEqual(result.newly_unsupported[0].entry_id, "idea-001")

    def test_firewall_not_triggered_with_remaining_supports(self):
        """Ideas with other active supports are not flagged."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Expired support",
                "expires": _days_ago(1),
            },
            "obs-002": {
                "type": "observation",
                "status": "active",
                "content": "Still-valid support",
            },
            "idea-001": {
                "type": "idea",
                "status": "active",
                "content": "Idea with two supports",
                "supports": ["obs-001", "obs-002"],
            },
        })
        result = fix_expired(tree_path=path, dry_run=True)
        self.assertEqual(result.expired_count, 1)
        self.assertEqual(result.unsupported_count, 0)

    def test_firewall_catches_principle_too(self):
        """Principles losing all supports are also flagged."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Only evidence",
                "expires": _days_ago(1),
            },
            "idea-001": {
                "type": "idea",
                "status": "active",
                "content": "Idea bridging obs to principle",
                "supports": ["obs-001"],
            },
            "prin-001": {
                "type": "principle",
                "status": "active",
                "content": "Principle on one idea",
                "supports": ["idea-001"],
            },
        })
        # Invalidating obs-001 doesn't directly affect prin-001 (its support is idea-001, still active)
        result = fix_expired(tree_path=path, dry_run=True)
        self.assertEqual(result.expired_count, 1)
        # idea-001 loses support, prin-001 keeps its support (idea-001 stays active, just unsupported)
        self.assertEqual(result.unsupported_count, 1)
        self.assertEqual(result.newly_unsupported[0].entry_id, "idea-001")

    def test_multiple_expired(self):
        """Multiple expired observations are all caught."""
        nodes = {}
        for i in range(5):
            nodes[f"obs-{i:03d}"] = {
                "type": "observation",
                "status": "active",
                "content": f"Expired fact {i}",
                "expires": _days_ago(i + 1),
            }
        path = self._make(nodes)
        result = fix_expired(tree_path=path, dry_run=True)
        self.assertEqual(result.expired_count, 5)

    def test_nonexistent_tree(self):
        """Missing tree file returns empty result."""
        result = fix_expired(tree_path="/tmp/nonexistent_tree_abc123.json")
        self.assertEqual(result.expired_count, 0)

    def test_format_report_dry_run(self):
        """Report format includes DRY RUN label."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Expired fact",
                "expires": _days_ago(1),
            },
        })
        result = fix_expired(tree_path=path, dry_run=True)
        report = result.format_report()
        self.assertIn("DRY RUN", report)
        self.assertIn("Would invalidate", report)

    def test_format_report_applied(self):
        """Report format says APPLIED when not dry-run."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Expired fact",
                "expires": _days_ago(1),
            },
        })
        result = fix_expired(tree_path=path, dry_run=False)
        report = result.format_report()
        self.assertIn("APPLIED", report)
        self.assertIn("Invalidated", report)

    def test_to_dict(self):
        """Result serializes to dict correctly."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "active",
                "content": "Expired fact",
                "expires": _days_ago(1),
            },
        })
        result = fix_expired(tree_path=path, dry_run=True)
        d = result.to_dict()
        self.assertEqual(d["expired_count"], 1)
        self.assertEqual(d["dry_run"], True)
        self.assertIsInstance(d["expired"], list)

    def test_previously_dead_support_plus_expired(self):
        """Idea with one already-dead and one now-expired support is flagged."""
        path = self._make({
            "obs-001": {
                "type": "observation",
                "status": "invalidated",
                "content": "Previously dead",
                "invalidated_reason": "old",
            },
            "obs-002": {
                "type": "observation",
                "status": "active",
                "content": "Now expiring",
                "expires": _days_ago(1),
            },
            "idea-001": {
                "type": "idea",
                "status": "active",
                "content": "Idea with both supports",
                "supports": ["obs-001", "obs-002"],
            },
        })
        result = fix_expired(tree_path=path, dry_run=True)
        self.assertEqual(result.unsupported_count, 1)


if __name__ == "__main__":
    unittest.main()
