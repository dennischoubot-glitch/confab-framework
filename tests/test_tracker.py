"""Tests for the staleness tracker."""

import tempfile
import unittest
from pathlib import Path

from confab.claims import Claim, ClaimType, VerifiabilityLevel
from confab.config import ConfabConfig, set_config, reset_config
from confab.tracker import (
    _hash_claim,
    _has_inline_verification,
    record_gate_run,
    get_stale_claims,
    get_all_tracked,
    get_run_history,
    get_stats,
    remove_claims,
    remove_stale,
    get_cascade_history,
    get_cascade_stats,
    trace_claim,
    TrackedClaim,
    CascadeEntry,
    TrackingStatus,
    DEFAULT_STALE_THRESHOLD,
)
from confab.verify import VerificationResult


class TestHashClaim(unittest.TestCase):
    """Test claim text hashing."""

    def test_deterministic(self):
        h1 = _hash_claim("test claim")
        h2 = _hash_claim("test claim")
        self.assertEqual(h1, h2)

    def test_whitespace_normalized(self):
        h1 = _hash_claim("test  claim")
        h2 = _hash_claim("test claim")
        self.assertEqual(h1, h2)

    def test_case_insensitive(self):
        h1 = _hash_claim("Test Claim")
        h2 = _hash_claim("test claim")
        self.assertEqual(h1, h2)

    def test_different_text_different_hash(self):
        h1 = _hash_claim("claim one")
        h2 = _hash_claim("claim two")
        self.assertNotEqual(h1, h2)

    def test_hash_length(self):
        h = _hash_claim("test")
        self.assertEqual(len(h), 16)


class TestHasInlineVerification(unittest.TestCase):
    """Test inline verification tag detection."""

    def test_v1_is_verified(self):
        claim = Claim(
            text="test", claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
            verification_tag="[v1: checked file_read 2026-03-19]",
        )
        self.assertTrue(_has_inline_verification(claim))

    def test_v2_is_verified(self):
        claim = Claim(
            text="test", claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
            verification_tag="[v2: checked web_search 2026-03-20]",
        )
        self.assertTrue(_has_inline_verification(claim))

    def test_unverified_is_not_verified(self):
        claim = Claim(
            text="test", claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
            verification_tag="[unverified]",
        )
        self.assertFalse(_has_inline_verification(claim))

    def test_failed_is_not_verified(self):
        claim = Claim(
            text="test", claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
            verification_tag="[FAILED: not found]",
        )
        self.assertFalse(_has_inline_verification(claim))

    def test_no_tag(self):
        claim = Claim(
            text="test", claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
        )
        self.assertFalse(_has_inline_verification(claim))


class TestRecordGateRun(unittest.TestCase):
    """Test gate run recording."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_tracker.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def _make_claim(self, text, ctype=ClaimType.FILE_EXISTS):
        return Claim(
            text=text,
            claim_type=ctype,
            verifiability=VerifiabilityLevel.AUTO,
        )

    def test_new_claims_counted(self):
        claims = [self._make_claim("file A exists")]
        result = record_gate_run(
            claims=claims,
            verification_results={},
            files_scanned=["test.md"],
            db_path=self.db_path,
        )
        self.assertEqual(result["new_claims"], 1)
        self.assertEqual(result["returning_claims"], 0)

    def test_returning_claims_counted(self):
        claims = [self._make_claim("file A exists")]
        # First run
        record_gate_run(claims=claims, verification_results={},
                        files_scanned=["test.md"], db_path=self.db_path)
        # Second run
        result = record_gate_run(claims=claims, verification_results={},
                                 files_scanned=["test.md"], db_path=self.db_path)
        self.assertEqual(result["returning_claims"], 1)
        self.assertEqual(result["new_claims"], 0)

    def test_verified_claim_tracked(self):
        claim = self._make_claim("file B exists")
        h = _hash_claim(claim.text)
        result = record_gate_run(
            claims=[claim],
            verification_results={h: VerificationResult.PASSED},
            files_scanned=["test.md"],
            db_path=self.db_path,
        )
        self.assertEqual(result["passed"], 1)

    def test_stale_after_threshold(self):
        claim = self._make_claim("persistent claim")
        # Run enough times to exceed threshold
        for _ in range(DEFAULT_STALE_THRESHOLD + 1):
            record_gate_run(
                claims=[claim],
                verification_results={},
                files_scanned=["test.md"],
                db_path=self.db_path,
            )
        stale = get_stale_claims(DEFAULT_STALE_THRESHOLD, self.db_path)
        self.assertTrue(len(stale) > 0)


class TestGetStaleClams(unittest.TestCase):
    """Test stale claim retrieval."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def test_no_stale_initially(self):
        stale = get_stale_claims(3, self.db_path)
        self.assertEqual(len(stale), 0)


class TestGetAllTracked(unittest.TestCase):
    """Test retrieving all tracked claims."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def test_empty_initially(self):
        tracked = get_all_tracked(self.db_path)
        self.assertEqual(len(tracked), 0)

    def test_returns_after_recording(self):
        claim = Claim(
            text="test claim",
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        record_gate_run(
            claims=[claim],
            verification_results={},
            files_scanned=["t.md"],
            db_path=self.db_path,
        )
        tracked = get_all_tracked(self.db_path)
        self.assertEqual(len(tracked), 1)
        self.assertEqual(tracked[0].claim_text, "test claim")


class TestRunHistory(unittest.TestCase):
    """Test gate run history retrieval."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def test_empty_history(self):
        history = get_run_history(db_path=self.db_path)
        self.assertEqual(len(history), 0)

    def test_history_after_runs(self):
        record_gate_run(claims=[], verification_results={},
                        files_scanned=[], db_path=self.db_path)
        record_gate_run(claims=[], verification_results={},
                        files_scanned=[], db_path=self.db_path)
        history = get_run_history(db_path=self.db_path)
        self.assertEqual(len(history), 2)


class TestRemoveClaims(unittest.TestCase):
    """Test claim removal."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def test_remove_specific_claim(self):
        claim = Claim(
            text="removable claim",
            claim_type=ClaimType.STATUS_CLAIM,
            verifiability=VerifiabilityLevel.SEMI,
        )
        record_gate_run(claims=[claim], verification_results={},
                        files_scanned=["t.md"], db_path=self.db_path)
        h = _hash_claim(claim.text)
        removed = remove_claims([h], self.db_path)
        self.assertEqual(removed, 1)
        self.assertEqual(len(get_all_tracked(self.db_path)), 0)


class TestGetStats(unittest.TestCase):
    """Test tracker statistics."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def test_empty_stats(self):
        stats = get_stats(self.db_path)
        self.assertEqual(stats["total_tracked"], 0)
        self.assertEqual(stats["total_gate_runs"], 0)
        self.assertIsNone(stats["latest_run"])

    def test_stats_after_run(self):
        claim = Claim(
            text="stats test",
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        record_gate_run(claims=[claim], verification_results={},
                        files_scanned=["t.md"], db_path=self.db_path)
        stats = get_stats(self.db_path)
        self.assertEqual(stats["total_tracked"], 1)
        self.assertEqual(stats["total_gate_runs"], 1)
        self.assertIsNotNone(stats["latest_run"])


class TestTrackedClaimToDict(unittest.TestCase):
    """Test TrackedClaim serialization."""

    def test_to_dict(self):
        tc = TrackedClaim(
            claim_hash="abc123",
            claim_text="test",
            claim_type="file_exists",
            source_file="test.md",
            first_seen="2026-01-01",
            last_seen="2026-03-20",
            last_verified=None,
            run_count=5,
            status="stale",
            evidence=None,
            verification_method=None,
        )
        d = tc.to_dict()
        self.assertEqual(d["hash"], "abc123")
        self.assertEqual(d["run_count"], 5)
        self.assertEqual(d["status"], "stale")


class TestCascadeHistory(unittest.TestCase):
    """Test cascade history recording and retrieval."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_cascade.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def _make_claim(self, text, ctype=ClaimType.FILE_EXISTS):
        return Claim(
            text=text,
            claim_type=ctype,
            verifiability=VerifiabilityLevel.AUTO,
        )

    def test_cascade_recorded_on_gate_run(self):
        """Each gate run records a cascade_history entry per claim."""
        claim = self._make_claim("cascade test claim")
        record_gate_run(
            claims=[claim], verification_results={},
            files_scanned=["t.md"], db_path=self.db_path,
        )
        h = _hash_claim(claim.text)
        history = get_cascade_history(h, self.db_path)
        self.assertEqual(len(history), 1)
        self.assertIsInstance(history[0], CascadeEntry)

    def test_cascade_depth_grows_with_runs(self):
        """Multiple gate runs produce multiple cascade entries."""
        claim = self._make_claim("persistent claim for cascade")
        for _ in range(5):
            record_gate_run(
                claims=[claim], verification_results={},
                files_scanned=["t.md"], db_path=self.db_path,
            )
        h = _hash_claim(claim.text)
        history = get_cascade_history(h, self.db_path)
        self.assertEqual(len(history), 5)

    def test_cascade_records_status_at_each_run(self):
        """Cascade history captures the status at each gate run."""
        claim = self._make_claim("status tracking claim")
        h = _hash_claim(claim.text)

        # Run 1: new
        record_gate_run(
            claims=[claim], verification_results={},
            files_scanned=["t.md"], db_path=self.db_path,
        )
        # Run 2: unverified
        record_gate_run(
            claims=[claim], verification_results={},
            files_scanned=["t.md"], db_path=self.db_path,
        )
        # Run 3: verified
        record_gate_run(
            claims=[claim],
            verification_results={h: VerificationResult.PASSED},
            files_scanned=["t.md"], db_path=self.db_path,
        )
        history = get_cascade_history(h, self.db_path)
        self.assertEqual(len(history), 3)
        # After recording, statuses reflect the claim's state at that run
        statuses = [e.status for e in history]
        # The last entry should be "verified" since it passed
        self.assertEqual(statuses[-1], "verified")


class TestCascadeStats(unittest.TestCase):
    """Test cascade depth statistics."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_stats.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def _make_claim(self, text):
        return Claim(
            text=text,
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
        )

    def test_empty_stats(self):
        stats = get_cascade_stats(self.db_path)
        self.assertEqual(stats["avg_depth"], 0.0)
        self.assertEqual(stats["max_depth"], 0)
        self.assertEqual(stats["total_cascaded"], 0)

    def test_stats_with_data(self):
        c1 = self._make_claim("claim one stats")
        c2 = self._make_claim("claim two stats")

        # c1 appears 5 times, c2 appears 2 times
        for i in range(5):
            claims = [c1] if i < 3 else [c1, c2]
            if i >= 3:
                claims = [c1, c2]
            else:
                claims = [c1]
            record_gate_run(
                claims=claims, verification_results={},
                files_scanned=["t.md"], db_path=self.db_path,
            )

        stats = get_cascade_stats(self.db_path)
        self.assertEqual(stats["max_depth"], 5)  # c1 appeared 5 times
        self.assertGreater(stats["avg_depth"], 0)
        self.assertGreater(stats["total_tracked"], 0)

    def test_top_cascaders_limited(self):
        """Top cascaders list is capped at 10."""
        claims = [self._make_claim(f"claim {i}") for i in range(15)]
        record_gate_run(
            claims=claims, verification_results={},
            files_scanned=["t.md"], db_path=self.db_path,
        )
        stats = get_cascade_stats(self.db_path)
        self.assertLessEqual(len(stats["top_cascaders"]), 10)


class TestTraceClaim(unittest.TestCase):
    """Test claim tracing by text search."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_trace.db"
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            db_path=self.db_path,
        ))

    def tearDown(self):
        reset_config()

    def test_trace_not_found(self):
        result = trace_claim("nonexistent claim", self.db_path)
        self.assertIsNone(result)

    def test_trace_by_substring(self):
        claim = Claim(
            text="OPENAI_API_KEY is missing",
            claim_type=ClaimType.ENV_VAR,
            verifiability=VerifiabilityLevel.AUTO,
        )
        record_gate_run(
            claims=[claim], verification_results={},
            files_scanned=["t.md"], db_path=self.db_path,
        )
        result = trace_claim("OPENAI_API_KEY", self.db_path)
        self.assertIsNotNone(result)
        self.assertIn("claim", result)
        self.assertIn("cascade", result)
        self.assertEqual(result["cascade_depth"], 1)

    def test_trace_by_hash(self):
        claim = Claim(
            text="trace by hash test",
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        h = _hash_claim(claim.text)
        record_gate_run(
            claims=[claim], verification_results={},
            files_scanned=["t.md"], db_path=self.db_path,
        )
        result = trace_claim(h, self.db_path)
        self.assertIsNotNone(result)
        self.assertEqual(result["claim"]["hash"], h)


if __name__ == "__main__":
    unittest.main()
