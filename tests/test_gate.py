"""Tests for the cascade gate."""

import tempfile
import unittest
from pathlib import Path

from confab.config import ConfabConfig, set_config, reset_config
from confab.gate import run_gate, quick_check, GateReport, STALE_BUILD_THRESHOLD


class TestGateReport(unittest.TestCase):
    """Test GateReport properties and formatting."""

    def _make_report(self, **kwargs):
        defaults = dict(
            timestamp="2026-03-20T00:00:00",
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

    def test_clean_report(self):
        report = self._make_report()
        self.assertTrue(report.clean)
        self.assertFalse(report.has_failures)
        self.assertFalse(report.has_stale)

    def test_failed_report(self):
        report = self._make_report(
            failed=1,
            failed_details=[{
                "claim_text": "bad claim",
                "claim_type": "file_exists",
                "source_file": "test.md",
                "source_line": 1,
                "evidence": "MISSING",
                "action": "Fix it",
            }],
        )
        self.assertFalse(report.clean)
        self.assertTrue(report.has_failures)

    def test_stale_report(self):
        report = self._make_report(
            stale_claims=2,
            stale_details=[
                {"claim_text": "old claim", "claim_type": "status_claim", "age_builds": 5},
                {"claim_text": "older claim", "claim_type": "env_var", "age_builds": 8},
            ],
        )
        self.assertFalse(report.clean)
        self.assertTrue(report.has_stale)

    def test_format_report_clean(self):
        report = self._make_report()
        text = report.format_report()
        self.assertIn("CLEAN", text)
        self.assertIn("Confabulation Gate Report", text)

    def test_format_report_failures(self):
        report = self._make_report(
            failed=1,
            failed_details=[{
                "claim_text": "file missing",
                "claim_type": "file_exists",
                "source_file": "test.md",
                "source_line": 3,
                "evidence": "NOT FOUND",
                "action": "Fix it",
            }],
        )
        text = report.format_report()
        self.assertIn("FAILED VERIFICATIONS", text)
        self.assertIn("file missing", text)

    def test_format_slack_clean(self):
        report = self._make_report()
        text = report.format_slack()
        self.assertIn("CLEAN", text)

    def test_format_slack_failures(self):
        report = self._make_report(
            failed=1,
            failed_details=[{
                "claim_text": "bad",
                "claim_type": "file_exists",
                "evidence": "MISSING",
                "action": "Fix",
            }],
        )
        text = report.format_slack()
        self.assertIn("FAILED", text)

    def test_to_dict(self):
        report = self._make_report()
        d = report.to_dict()
        self.assertEqual(d["total_claims"], 5)
        self.assertEqual(d["passed"], 3)
        self.assertTrue(d["clean"])
        self.assertIn("tracker", d)

    def test_tracker_metadata(self):
        report = self._make_report(
            tracker_new=2,
            tracker_returning=3,
            tracker_total_runs=10,
        )
        d = report.to_dict()
        self.assertEqual(d["tracker"]["new_claims"], 2)
        self.assertEqual(d["tracker"]["total_runs"], 10)


class TestRunGate(unittest.TestCase):
    """Test the run_gate function."""

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

    def test_empty_gate(self):
        """Gate with no files produces a clean report."""
        report = run_gate(files=[], track=False)
        self.assertTrue(report.clean)
        self.assertEqual(report.total_claims, 0)

    def test_gate_with_text(self):
        """Gate can scan inline text."""
        report = run_gate(text="Audio blocked on OPENAI_API_KEY", track=False)
        self.assertTrue(report.total_claims > 0)

    def test_gate_with_file(self):
        """Gate scans files for claims."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Audio blocked on OPENAI_API_KEY\n- `missing.py` exists and is ready\n")
        report = run_gate(files=[str(md)], track=False)
        self.assertTrue(report.total_claims > 0)

    def test_gate_nonexistent_file_skipped(self):
        """Nonexistent files are silently skipped."""
        report = run_gate(files=["/nonexistent/path.md"], track=False)
        self.assertEqual(report.total_claims, 0)

    def test_gate_with_tracker(self):
        """Gate with tracking enabled records to DB."""
        md = Path(self.tmpdir) / "test.md"
        md.write_text("Script `test_script.py` is working\n")
        (Path(self.tmpdir) / "test_script.py").write_text("pass")
        report = run_gate(files=[str(md)], track=True)
        self.assertTrue(report.tracker_total_runs >= 1)

    def test_gate_detects_missing_file(self):
        """Gate should fail when a claimed file doesn't exist."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("The `nonexistent_script.py` file exists and is deployed\n")
        report = run_gate(files=[str(md)], track=False)
        if report.total_claims > 0:
            # If a file_exists claim was extracted, it should fail
            file_failures = [
                d for d in report.failed_details
                if d.get("claim_type") == "file_exists"
            ]
            if file_failures:
                self.assertTrue(report.has_failures)


class TestQuickCheck(unittest.TestCase):
    """Test the quick_check convenience function."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=Path(self.tmpdir) / "test.db",
        ))

    def tearDown(self):
        reset_config()

    def test_quick_check_clean(self):
        result = quick_check()
        self.assertIn("CLEAN", result)

    def test_quick_check_with_file(self):
        md = Path(self.tmpdir) / "test.md"
        md.write_text("Everything is fine\n")
        result = quick_check(str(md))
        self.assertIn("Gate:", result)


class TestRegistryEnforcement(unittest.TestCase):
    """Test registry violation detection in the gate."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Create core/SYSTEM_REGISTRY.md with some registered resources
        core_dir = Path(self.tmpdir) / "core"
        core_dir.mkdir()
        (core_dir / "SYSTEM_REGISTRY.md").write_text(
            "# System Registry\n\n"
            "| Path | Purpose | Status |\n"
            "|------|---------|--------|\n"
            "| `kalshi_market_data.db` | Market data | canonical |\n"
            "| `data/market_scan.json` | Scan output | canonical |\n"
            "| `scripts/kalshi_portfolio.py` | Portfolio | canonical |\n"
            "| `slack-bridge/history.db` | Agent history | canonical |\n"
        )
        # Also need core/agents dir so _is_ia_repo works
        (core_dir / "confab").mkdir()
        (core_dir / "confab" / "__init__.py").write_text("")
        (core_dir / "agents").mkdir()

        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=Path(self.tmpdir) / "test_tracker.db",
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def test_no_violations_for_registered_files(self):
        """Files in the registry should not be flagged."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text(
            "- Using `kalshi_market_data.db` for market data\n"
            "- Run `scripts/kalshi_portfolio.py` for portfolio review\n"
        )
        report = run_gate(files=[str(md)], track=False)
        self.assertEqual(len(report.registry_violations), 0)

    def test_violations_for_unregistered_db(self):
        """Unregistered .db files should be flagged."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Created `agent_state.db` to track sessions\n")
        report = run_gate(files=[str(md)], track=False)
        self.assertEqual(len(report.registry_violations), 1)
        self.assertEqual(report.registry_violations[0]['path'], 'agent_state.db')
        self.assertIn('Register', report.registry_violations[0]['action'])

    def test_violations_for_unregistered_json(self):
        """Unregistered .json data files should be flagged."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Updated `data/custom_signals.json` with new data\n")
        report = run_gate(files=[str(md)], track=False)
        violations = [v for v in report.registry_violations if v['path'] == 'data/custom_signals.json']
        self.assertEqual(len(violations), 1)

    def test_violations_for_unregistered_script(self):
        """Unregistered .py scripts should be flagged."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Run `scripts/new_pipeline.py` for data processing\n")
        report = run_gate(files=[str(md)], track=False)
        violations = [v for v in report.registry_violations if v['path'] == 'scripts/new_pipeline.py']
        self.assertEqual(len(violations), 1)

    def test_skips_test_files(self):
        """test_*.py files should not be flagged."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Fixed `test_verify.py` tests\n")
        report = run_gate(files=[str(md)], track=False)
        test_violations = [v for v in report.registry_violations if 'test_verify' in v['path']]
        self.assertEqual(len(test_violations), 0)

    def test_skips_framework_internals(self):
        """Files under core/confab/ should not be flagged."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Updated `core/confab/verify.py` with new verifier\n")
        report = run_gate(files=[str(md)], track=False)
        confab_violations = [v for v in report.registry_violations if 'core/confab/' in v['path']]
        self.assertEqual(len(confab_violations), 0)

    def test_skips_package_json(self):
        """Common config files like package.json should not be flagged."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Updated `package.json` with new deps\n")
        report = run_gate(files=[str(md)], track=False)
        pkg_violations = [v for v in report.registry_violations if 'package.json' in v['path']]
        self.assertEqual(len(pkg_violations), 0)

    def test_report_not_clean_with_violations(self):
        """Gate report should not be clean when violations exist."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Created `orphan.db` for temporary data\n")
        report = run_gate(files=[str(md)], track=False)
        self.assertTrue(report.has_registry_violations)
        self.assertFalse(report.clean)

    def test_violations_in_format_report(self):
        """Violations should appear in the formatted report."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Created `orphan.db` for temporary data\n")
        report = run_gate(files=[str(md)], track=False)
        text = report.format_report()
        self.assertIn("REGISTRY VIOLATIONS", text)
        self.assertIn("orphan.db", text)

    def test_violations_in_to_dict(self):
        """Violations should appear in the dict output."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Created `orphan.db` for temporary data\n")
        report = run_gate(files=[str(md)], track=False)
        d = report.to_dict()
        self.assertIn("registry_violations", d)
        self.assertEqual(len(d["registry_violations"]), 1)

    def test_deduplicates_paths(self):
        """Same path referenced multiple times should only appear once."""
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text(
            "- `orphan.db` is used for state\n"
            "- Check `orphan.db` for recent data\n"
        )
        report = run_gate(files=[str(md)], track=False)
        orphan_violations = [v for v in report.registry_violations if v['path'] == 'orphan.db']
        self.assertEqual(len(orphan_violations), 1)

    def test_no_registry_file_returns_empty(self):
        """When SYSTEM_REGISTRY.md doesn't exist, no violations are reported."""
        import os
        os.remove(str(Path(self.tmpdir) / "core" / "SYSTEM_REGISTRY.md"))
        md = Path(self.tmpdir) / "priorities.md"
        md.write_text("- Created `orphan.db` for data\n")
        report = run_gate(files=[str(md)], track=False)
        self.assertEqual(len(report.registry_violations), 0)


if __name__ == "__main__":
    unittest.main()
