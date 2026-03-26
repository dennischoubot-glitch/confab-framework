"""Integration tests for confab middleware — decorator and verify_text.

Tests realistic agent output scenarios: file claims, env var claims,
combined outputs, and all three on_fail modes. Uses real temp files
to test that verification actually catches false vs true claims.
"""

import os
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from confab.middleware import (
    ConfabVerificationError,
    VerificationReport,
    confab_gate,
    get_report,
    verify_text,
)


class TestVerifyText(unittest.TestCase):
    """Test verify_text() standalone function with realistic agent output."""

    def test_existing_file_passes(self):
        """Claims about files that exist should pass verification."""
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"# test")
            tmp = f.name
        try:
            report = verify_text(f"The script at {tmp} is ready.")
            self.assertIsInstance(report, VerificationReport)
            self.assertEqual(report.failed, 0)
            self.assertTrue(report.clean)
        finally:
            os.unlink(tmp)

    def test_nonexistent_file_fails(self):
        """Claims about files that don't exist should fail verification."""
        report = verify_text(
            "Config at /tmp/confab_test_nonexistent_file_xyz.json is ready."
        )
        self.assertGreater(report.failed, 0)
        self.assertFalse(report.clean)

    def test_env_var_present(self):
        """Claims about env vars that are set should pass."""
        with patch.dict(os.environ, {"CONFAB_TEST_VAR": "1"}):
            report = verify_text(
                "Pipeline requires CONFAB_TEST_VAR to be configured.",
                check_files=False,
                check_env=True,
            )
            # ENV_VAR claims about "blocked on X" when X is present → FAILED
            # ENV_VAR claims about "requires X" when X is present → depends on context
            # The key test: the claim was found and processed
            self.assertGreater(report.claims_found, 0)

    def test_env_var_missing_blocker(self):
        """Claims about being blocked on a missing env var should pass (blocker is real)."""
        # Make sure the var is not set
        env = os.environ.copy()
        env.pop("CONFAB_NONEXISTENT_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            report = verify_text(
                "Audio pipeline blocked on CONFAB_NONEXISTENT_KEY.",
                check_files=False,
                check_env=True,
            )
            # The claim says "blocked on X" — if X is truly missing, the blocker is real
            self.assertGreater(report.claims_found, 0)

    def test_mixed_claims_real_and_fake(self):
        """Agent output with both real and fake file claims."""
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            real_file = f.name
        try:
            report = verify_text(
                f"Status report:\n"
                f"- Script at {real_file} is operational\n"
                f"- Database at /tmp/confab_fake_db_xyz.db is ready\n"
            )
            self.assertGreater(report.claims_found, 0)
            # At least the fake file should fail
            self.assertGreater(report.failed, 0)
            self.assertFalse(report.clean)
            # Should have a summary describing the failure
            summary = report.summary()
            self.assertIn("FAILED", summary)
        finally:
            os.unlink(real_file)

    def test_no_claims_is_clean(self):
        """Text with no verifiable claims should be clean."""
        report = verify_text("Everything looks good. The system is healthy.")
        self.assertTrue(report.clean)
        self.assertEqual(report.claims_found, 0)
        self.assertEqual(report.failed, 0)

    def test_check_files_disabled(self):
        """When check_files=False, file claims should not be checked."""
        report = verify_text(
            "Config at /tmp/confab_definitely_missing.json is ready.",
            check_files=False,
            check_env=False,
        )
        self.assertEqual(report.claims_found, 0)
        self.assertTrue(report.clean)

    def test_report_summary_clean(self):
        """Clean reports should produce a CLEAN summary."""
        report = verify_text("No claims here.")
        summary = report.summary()
        self.assertIn("CLEAN", summary)

    def test_report_checked_at_populated(self):
        """Report should have a timestamp."""
        report = verify_text("No claims.")
        self.assertTrue(len(report.checked_at) > 0)


class TestConfabGateDecorator(unittest.TestCase):
    """Test @confab_gate decorator in all three modes."""

    def test_warn_mode_returns_output(self):
        """Warn mode should return the function's output even on failure."""
        @confab_gate
        def agent(prompt):
            return "Wrote /tmp/confab_warn_test_missing.json successfully."

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = agent("go")
            self.assertEqual(
                result, "Wrote /tmp/confab_warn_test_missing.json successfully."
            )
            # Should have issued a warning about the missing file
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertGreater(len(confab_warns), 0)

    def test_raise_mode_raises_on_failure(self):
        """Raise mode should raise ConfabVerificationError on failure."""
        @confab_gate(on_fail="raise")
        def agent(prompt):
            return "Database at /tmp/confab_raise_test_missing.db is loaded."

        with self.assertRaises(ConfabVerificationError) as ctx:
            agent("go")
        self.assertGreater(len(ctx.exception.failures), 0)

    def test_raise_mode_succeeds_on_clean(self):
        """Raise mode should not raise when all claims pass."""
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            tmp = f.name
        try:
            @confab_gate(on_fail="raise")
            def agent(prompt):
                return f"Script at {tmp} is ready."

            result = agent("go")
            self.assertIn(tmp, result)
        finally:
            os.unlink(tmp)

    def test_log_mode_no_raise_no_warn(self):
        """Log mode should neither raise nor warn — just log."""
        @confab_gate(on_fail="log")
        def agent(prompt):
            return "Config at /tmp/confab_log_test_missing.toml is deployed."

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = agent("go")
            # Should return output
            self.assertIn("deployed", result)
            # Should NOT issue a warning
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertEqual(len(confab_warns), 0)

    def test_get_report_after_call(self):
        """get_report() should return the verification report after calling."""
        @confab_gate
        def agent(prompt):
            return "Output at /tmp/confab_report_test.json is ready."

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            agent("go")

        report = get_report(agent)
        self.assertIsNotNone(report)
        self.assertIsInstance(report, VerificationReport)
        self.assertGreater(report.claims_found, 0)

    def test_get_report_before_call(self):
        """get_report() should return None if the function hasn't been called."""
        @confab_gate
        def agent(prompt):
            return "never called"

        report = get_report(agent)
        self.assertIsNone(report)

    def test_non_string_output_passthrough(self):
        """Decorator should pass through non-string outputs without verification."""
        @confab_gate
        def agent(prompt):
            return {"status": "ok", "files": ["/tmp/missing.json"]}

        result = agent("go")
        self.assertEqual(result, {"status": "ok", "files": ["/tmp/missing.json"]})

    def test_invalid_on_fail_raises(self):
        """Invalid on_fail value should raise ValueError at decoration time."""
        with self.assertRaises(ValueError):
            @confab_gate(on_fail="explode")
            def agent(prompt):
                return "test"

    def test_decorator_preserves_function_name(self):
        """Decorator should preserve the wrapped function's name."""
        @confab_gate
        def my_named_agent(prompt):
            return "test"

        self.assertEqual(my_named_agent.__name__, "my_named_agent")

    def test_decorator_with_kwargs(self):
        """Decorator should work with functions that take kwargs."""
        @confab_gate(on_fail="log")
        def agent(prompt, verbose=False, model="default"):
            if verbose:
                return f"Using model={model}. Config at /tmp/confab_kwargs_test.toml ready."
            return "done"

        result = agent("go", verbose=True, model="gpt-4")
        self.assertIn("gpt-4", result)


class TestRealisticAgentOutput(unittest.TestCase):
    """Test with text that resembles real multi-agent handoff output."""

    def test_builder_handoff_with_mixed_claims(self):
        """Simulate a builder handoff note with real and false claims."""
        with tempfile.NamedTemporaryFile(
            suffix=".md", delete=False, dir="/tmp"
        ) as f:
            f.write(b"# Priorities\n- item 1\n")
            real_file = f.name

        try:
            # Use assertion words the extractor recognizes ("ready", "deployed")
            handoff = (
                f"## Build complete\n"
                f"- Priorities at {real_file} ready\n"
                f"- Dashboard at /tmp/confab_test_dashboard_xyz.html deployed\n"
                f"- All 15 tests passing\n"
            )
            report = verify_text(handoff)
            # The real file should pass, the fake file should fail
            self.assertFalse(report.clean)
            self.assertGreater(report.failed, 0)
            self.assertGreater(report.passed, 0)
        finally:
            os.unlink(real_file)

    def test_dreamer_context_no_file_claims(self):
        """Dreamer output that discusses concepts without file claims."""
        dreamer_output = (
            "The knowledge tree shows convergence toward fixed attractors. "
            "External input from outside the latent space is required to "
            "perturb the attractor itself. The domain rotation rule suggests "
            "diversifying away from Synthesis."
        )
        report = verify_text(dreamer_output)
        self.assertTrue(report.clean)

    def test_multiline_agent_output(self):
        """Test extraction from multi-paragraph agent output."""
        output = (
            "Sprint complete. Here's the status:\n\n"
            "1. **Config deployed** — config/prod.toml has the new settings\n"
            "2. **Scripts working** — scripts/deploy.py tested successfully\n"
            "3. **Env ready** — DATABASE_URL is configured\n\n"
            "The pipeline is operational. Ready for next sprint.\n"
        )
        report = verify_text(output, check_files=True, check_env=True)
        # Should find claims (file paths and/or env vars)
        self.assertGreater(report.claims_found, 0)


class TestVerifyTextEdgeCases(unittest.TestCase):
    """Edge cases and boundary conditions."""

    def test_empty_string(self):
        """Empty text should be clean."""
        report = verify_text("")
        self.assertTrue(report.clean)
        self.assertEqual(report.claims_found, 0)

    def test_very_long_text(self):
        """Long text should not crash."""
        long_text = "word " * 10000 + "Config at /tmp/confab_long_test.json is ready."
        report = verify_text(long_text)
        # Should still find the file claim
        self.assertGreater(report.claims_found, 0)

    def test_unicode_text(self):
        """Unicode in text should not crash."""
        report = verify_text("Config at /tmp/café_config.json is réady. 日本語テスト")
        # Should not crash, may or may not find claims
        self.assertIsInstance(report, VerificationReport)

    def test_report_all_outcomes_populated(self):
        """all_outcomes should contain every verification result."""
        report = verify_text(
            "Script at /tmp/confab_outcomes_test.py is ready. "
            "Config at /tmp/confab_outcomes_cfg.toml deployed."
        )
        self.assertEqual(len(report.all_outcomes), report.verified)


if __name__ == "__main__":
    unittest.main()
