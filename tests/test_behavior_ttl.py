"""Tests for behavior claim TTL expiry."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from confab.claims import (
    Claim,
    ClaimType,
    VerifiabilityLevel,
    BEHAVIOR_CLAIM_TYPES,
    STATE_CLAIM_TYPES,
    is_behavior_claim,
    parse_vtag_timestamp,
)
from confab.config import ConfabConfig, set_config, reset_config
from confab.gate import run_gate, GateReport, _check_behavior_ttl


class TestClaimClassification(unittest.TestCase):
    """Test behavior vs state claim classification."""

    def test_behavior_types(self):
        """Pipeline, process, and script claims are behavior claims."""
        for ct in BEHAVIOR_CLAIM_TYPES:
            claim = Claim(text="test", claim_type=ct,
                         verifiability=VerifiabilityLevel.AUTO)
            self.assertTrue(is_behavior_claim(claim), f"{ct} should be behavior")

    def test_state_types(self):
        """File, env var, and config claims are NOT behavior claims."""
        for ct in STATE_CLAIM_TYPES:
            claim = Claim(text="test", claim_type=ct,
                         verifiability=VerifiabilityLevel.AUTO)
            self.assertFalse(is_behavior_claim(claim), f"{ct} should be state")

    def test_subjective_not_behavior(self):
        claim = Claim(text="test", claim_type=ClaimType.SUBJECTIVE,
                     verifiability=VerifiabilityLevel.MANUAL)
        self.assertFalse(is_behavior_claim(claim))

    def test_no_overlap(self):
        """Behavior and state sets should not overlap."""
        self.assertEqual(len(BEHAVIOR_CLAIM_TYPES & STATE_CLAIM_TYPES), 0)


class TestParseVtagTimestamp(unittest.TestCase):
    """Test verification tag timestamp extraction."""

    def test_v1_with_date_and_time(self):
        ts = parse_vtag_timestamp("[v1: verified 2026-03-21 8:22PM]")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.month, 3)
        self.assertEqual(ts.day, 21)
        self.assertEqual(ts.hour, 20)  # 8 PM = 20:00
        self.assertEqual(ts.minute, 22)

    def test_v1_with_date_only(self):
        ts = parse_vtag_timestamp("[v1: verified 2026-03-21]")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.month, 3)
        self.assertEqual(ts.day, 21)

    def test_v2_with_method_and_date(self):
        ts = parse_vtag_timestamp("[v2: checked via pip show 2026-03-22]")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.year, 2026)
        self.assertEqual(ts.month, 3)
        self.assertEqual(ts.day, 22)

    def test_verified_colon_date(self):
        ts = parse_vtag_timestamp("[verified: 2026-03-21]")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.day, 21)

    def test_unverified_returns_none(self):
        self.assertIsNone(parse_vtag_timestamp("[unverified]"))

    def test_failed_returns_none(self):
        self.assertIsNone(parse_vtag_timestamp("[FAILED: file not found]"))

    def test_none_returns_none(self):
        self.assertIsNone(parse_vtag_timestamp(None))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_vtag_timestamp(""))

    def test_no_date_returns_none(self):
        self.assertIsNone(parse_vtag_timestamp("[v1: verified]"))

    def test_time_with_space_before_am_pm(self):
        ts = parse_vtag_timestamp("[v1: verified 2026-03-21 8:22 PM]")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.hour, 20)

    def test_am_time(self):
        ts = parse_vtag_timestamp("[v1: verified 2026-03-21 11:30AM]")
        self.assertIsNotNone(ts)
        self.assertEqual(ts.hour, 11)
        self.assertEqual(ts.minute, 30)


class TestCheckBehaviorTtl(unittest.TestCase):
    """Test _check_behavior_ttl logic."""

    def _make_claim(self, claim_type, vtag, text="test claim"):
        return Claim(
            text=text,
            claim_type=claim_type,
            verifiability=VerifiabilityLevel.AUTO,
            source_file="test.md",
            source_line=1,
            verification_tag=vtag,
        )

    def test_expired_behavior_claim(self):
        """A behavior claim verified >6h ago should be flagged."""
        old_date = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime('%Y-%m-%d')
        claim = self._make_claim(
            ClaimType.PIPELINE_BLOCKED,
            f"[v1: verified {old_date}]",
            text="responder 403"
        )
        expired = _check_behavior_ttl([claim], ttl_hours=6.0)
        self.assertEqual(len(expired), 1)
        self.assertIn("responder 403", expired[0]["claim_text"])
        self.assertGreater(expired[0]["age_hours"], 6.0)

    def test_fresh_behavior_claim(self):
        """A behavior claim verified <6h ago should NOT be flagged."""
        now_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        claim = self._make_claim(
            ClaimType.PIPELINE_WORKS,
            f"[v1: verified {now_date}]",
        )
        # Use a TTL much larger than "since midnight" to ensure it's fresh
        expired = _check_behavior_ttl([claim], ttl_hours=48.0)
        self.assertEqual(len(expired), 0)

    def test_state_claim_not_subject_to_ttl(self):
        """A state claim (file_exists) should never be TTL-expired."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
        claim = self._make_claim(
            ClaimType.FILE_EXISTS,
            f"[v1: verified {old_date}]",
        )
        expired = _check_behavior_ttl([claim], ttl_hours=6.0)
        self.assertEqual(len(expired), 0)

    def test_unverified_claim_not_flagged(self):
        """Claims with [unverified] tag have no timestamp — skip them."""
        claim = self._make_claim(
            ClaimType.PIPELINE_BLOCKED,
            "[unverified]",
        )
        expired = _check_behavior_ttl([claim], ttl_hours=6.0)
        self.assertEqual(len(expired), 0)

    def test_no_vtag_not_flagged(self):
        """Claims with no verification tag are handled by stale detection, not TTL."""
        claim = self._make_claim(
            ClaimType.PROCESS_STATUS,
            None,
        )
        expired = _check_behavior_ttl([claim], ttl_hours=6.0)
        self.assertEqual(len(expired), 0)

    def test_ttl_zero_disables(self):
        """TTL of 0 should disable the check entirely."""
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).strftime('%Y-%m-%d')
        claim = self._make_claim(
            ClaimType.PIPELINE_BLOCKED,
            f"[v1: verified {old_date}]",
        )
        expired = _check_behavior_ttl([claim], ttl_hours=0)
        self.assertEqual(len(expired), 0)

    def test_process_status_ttl(self):
        """Process status claims should be subject to TTL."""
        old_date = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime('%Y-%m-%d')
        claim = self._make_claim(
            ClaimType.PROCESS_STATUS,
            f"[v1: verified {old_date}]",
            text="Weather rewards monitor: STOPPED"
        )
        expired = _check_behavior_ttl([claim], ttl_hours=6.0)
        self.assertEqual(len(expired), 1)

    def test_multiple_claims_mixed(self):
        """Only behavior claims past TTL should be flagged."""
        old_date = (datetime.now(timezone.utc) - timedelta(hours=10)).strftime('%Y-%m-%d')

        claims = [
            # Old behavior → should flag
            self._make_claim(ClaimType.PIPELINE_BLOCKED,
                           f"[v1: verified {old_date}]",
                           text="responder 403"),
            # Old state → should NOT flag (state claims exempt from TTL)
            self._make_claim(ClaimType.FILE_EXISTS,
                           f"[v1: verified {old_date}]",
                           text="config.py exists"),
            # Fresh behavior → should NOT flag (use large TTL to ensure freshness)
            self._make_claim(ClaimType.PIPELINE_WORKS,
                           f"[v1: verified {old_date}]",
                           text="pipeline OK"),
        ]
        # Only the old behavior claim should flag; state claim is exempt;
        # both behavior claims are old but we check all behavior types are detected
        expired = _check_behavior_ttl(claims, ttl_hours=6.0)
        self.assertEqual(len(expired), 2)  # Both behavior claims are old
        expired_texts = {e["claim_text"] for e in expired}
        self.assertIn("responder 403", expired_texts)
        self.assertIn("pipeline OK", expired_texts)
        # State claim should never appear
        self.assertNotIn("config.py exists", expired_texts)


class TestGateReportTtl(unittest.TestCase):
    """Test GateReport TTL-related properties and formatting."""

    def _make_report(self, **kwargs):
        defaults = dict(
            timestamp="2026-03-22T00:00:00",
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

    def test_clean_when_no_ttl(self):
        report = self._make_report()
        self.assertTrue(report.clean)
        self.assertFalse(report.has_ttl_expired)

    def test_not_clean_when_ttl_expired(self):
        report = self._make_report(
            ttl_expired=[{
                "claim_text": "responder 403",
                "claim_type": "pipeline_blocked",
                "source_file": "test.md",
                "source_line": 1,
                "verified_at": "2026-03-21 20:22 UTC",
                "age_hours": 10.5,
                "ttl_hours": 6.0,
            }],
        )
        self.assertFalse(report.clean)
        self.assertTrue(report.has_ttl_expired)

    def test_format_report_includes_ttl(self):
        report = self._make_report(
            ttl_expired=[{
                "claim_text": "responder 403",
                "claim_type": "pipeline_blocked",
                "source_file": "test.md",
                "source_line": 1,
                "verified_at": "2026-03-21 20:22 UTC",
                "age_hours": 10.5,
                "ttl_hours": 6.0,
            }],
        )
        text = report.format_report()
        self.assertIn("TTL-EXPIRED", text)
        self.assertIn("responder 403", text)
        self.assertIn("10.5h ago", text)

    def test_format_ci_includes_ttl(self):
        report = self._make_report(
            ttl_expired=[{
                "claim_text": "responder 403",
                "claim_type": "pipeline_blocked",
                "source_file": "test.md",
                "source_line": 1,
                "verified_at": "2026-03-21 20:22 UTC",
                "age_hours": 10.5,
                "ttl_hours": 6.0,
            }],
        )
        text = report.format_ci()
        self.assertIn("TTL-Expired", text)
        self.assertIn("responder 403", text)

    def test_format_slack_includes_ttl(self):
        report = self._make_report(
            ttl_expired=[{
                "claim_text": "responder 403",
                "claim_type": "pipeline_blocked",
                "source_file": "test.md",
                "source_line": 1,
                "verified_at": "2026-03-21 20:22 UTC",
                "age_hours": 10.5,
                "ttl_hours": 6.0,
            }],
        )
        text = report.format_slack()
        self.assertIn("TTL-expired", text)
        self.assertIn(":clock3:", text)

    def test_to_dict_includes_ttl(self):
        report = self._make_report(
            ttl_expired=[{"claim_text": "test", "age_hours": 10}],
        )
        d = report.to_dict()
        self.assertIn("ttl_expired", d)
        self.assertEqual(len(d["ttl_expired"]), 1)


class TestConfigTtl(unittest.TestCase):
    """Test behavior_ttl_hours config parsing."""

    def test_default_ttl(self):
        config = ConfabConfig(
            workspace_root=Path("/tmp"),
            files_to_scan=[],
        )
        self.assertEqual(config.behavior_ttl_hours, 6.0)

    def test_custom_ttl(self):
        config = ConfabConfig(
            workspace_root=Path("/tmp"),
            files_to_scan=[],
            behavior_ttl_hours=12.0,
        )
        self.assertEqual(config.behavior_ttl_hours, 12.0)


if __name__ == "__main__":
    unittest.main()
