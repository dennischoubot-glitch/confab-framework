"""Tests for confab CrewAI integration.

Uses mock TaskOutput objects since crewai is an optional dependency.
Tests verify that ConfabTaskCallback correctly extracts text, runs
verification, and handles all three on_fail modes.
"""

import os
import tempfile
import unittest
import warnings
from unittest.mock import patch

# Mock crewai.tasks.task_output before importing the integration
from unittest.mock import MagicMock
import sys

# Create a mock crewai module with TaskOutput
mock_crewai = MagicMock()
mock_task_output_mod = MagicMock()


class MockTaskOutput:
    """Simulates crewai.tasks.task_output.TaskOutput."""

    def __init__(self, raw: str = "", description: str = "test task"):
        self.raw = raw
        self.description = description
        self.summary = raw[:100] if raw else ""
        self.json_dict = {}
        self.pydantic = None


mock_task_output_mod.TaskOutput = MockTaskOutput
mock_crewai.tasks = MagicMock()
mock_crewai.tasks.task_output = mock_task_output_mod

sys.modules["crewai"] = mock_crewai
sys.modules["crewai.tasks"] = mock_crewai.tasks
sys.modules["crewai.tasks.task_output"] = mock_task_output_mod

from confab.integrations.crewai import ConfabTaskCallback
from confab.middleware import ConfabVerificationError, VerificationReport


class TestConfabTaskCallbackInit(unittest.TestCase):
    """Test ConfabTaskCallback initialization and configuration."""

    def test_default_init(self):
        cb = ConfabTaskCallback()
        self.assertTrue(cb.check_files)
        self.assertTrue(cb.check_env)
        self.assertFalse(cb.check_counts)
        self.assertEqual(cb.on_fail, "warn")
        self.assertEqual(cb.reports, [])

    def test_custom_init(self):
        cb = ConfabTaskCallback(
            check_files=False,
            check_env=False,
            check_counts=True,
            on_fail="log",
        )
        self.assertFalse(cb.check_files)
        self.assertFalse(cb.check_env)
        self.assertTrue(cb.check_counts)
        self.assertEqual(cb.on_fail, "log")

    def test_invalid_on_fail_raises(self):
        with self.assertRaises(ValueError):
            ConfabTaskCallback(on_fail="explode")

    def test_last_report_initially_none(self):
        cb = ConfabTaskCallback()
        self.assertIsNone(cb.last_report)


class TestConfabTaskCallbackCall(unittest.TestCase):
    """Test the __call__ interface with mock TaskOutput objects."""

    def test_clean_output_from_task_output(self):
        """TaskOutput with no verifiable claims should produce clean report."""
        cb = ConfabTaskCallback()
        output = MockTaskOutput(raw="The system is healthy. All good.")
        cb(output)
        self.assertEqual(len(cb.reports), 1)
        self.assertTrue(cb.last_report.clean)

    def test_existing_file_passes(self):
        """Claims about files that exist should pass."""
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"# test")
            tmp = f.name
        try:
            cb = ConfabTaskCallback()
            output = MockTaskOutput(raw=f"The script at {tmp} is ready.")
            cb(output)
            self.assertEqual(cb.last_report.failed, 0)
        finally:
            os.unlink(tmp)

    def test_nonexistent_file_fails(self):
        """Claims about missing files should fail."""
        cb = ConfabTaskCallback()
        output = MockTaskOutput(
            raw="Config at /tmp/confab_crewai_test_missing_xyz.json is ready."
        )
        cb(output)
        self.assertGreater(cb.last_report.failed, 0)
        self.assertFalse(cb.last_report.clean)

    def test_empty_output_skipped(self):
        """Empty task output should not produce a report."""
        cb = ConfabTaskCallback()
        output = MockTaskOutput(raw="")
        cb(output)
        self.assertEqual(len(cb.reports), 0)

    def test_whitespace_output_skipped(self):
        """Whitespace-only output should not produce a report."""
        cb = ConfabTaskCallback()
        output = MockTaskOutput(raw="   \n  ")
        cb(output)
        self.assertEqual(len(cb.reports), 0)

    def test_string_input_accepted(self):
        """Plain string input should work (for flexible usage)."""
        cb = ConfabTaskCallback()
        cb("The system is running fine.")
        self.assertEqual(len(cb.reports), 1)
        self.assertTrue(cb.last_report.clean)

    def test_multiple_calls_accumulate_reports(self):
        """Multiple task completions should accumulate reports."""
        cb = ConfabTaskCallback()
        cb(MockTaskOutput(raw="First output."))
        cb(MockTaskOutput(raw="Second output."))
        cb(MockTaskOutput(raw="Third output."))
        self.assertEqual(len(cb.reports), 3)


class TestConfabTaskCallbackOnFail(unittest.TestCase):
    """Test all three on_fail modes."""

    def _make_failing_output(self):
        return MockTaskOutput(
            raw="Database at /tmp/confab_crewai_onfail_test_xyz.db is ready.",
            description="check db",
        )

    def test_warn_mode_issues_warning(self):
        cb = ConfabTaskCallback(on_fail="warn")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cb(self._make_failing_output())
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertGreater(len(confab_warns), 0)

    def test_raise_mode_raises_error(self):
        cb = ConfabTaskCallback(on_fail="raise")
        with self.assertRaises(ConfabVerificationError) as ctx:
            cb(self._make_failing_output())
        self.assertGreater(len(ctx.exception.failures), 0)

    def test_log_mode_no_raise_no_warn(self):
        cb = ConfabTaskCallback(on_fail="log")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            cb(self._make_failing_output())
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertEqual(len(confab_warns), 0)
        # Report should still be recorded
        self.assertFalse(cb.last_report.clean)


class TestConfabTaskCallbackConvenience(unittest.TestCase):
    """Test convenience properties and methods."""

    def test_clear(self):
        cb = ConfabTaskCallback()
        cb(MockTaskOutput(raw="Output one."))
        cb(MockTaskOutput(raw="Output two."))
        self.assertEqual(len(cb.reports), 2)
        cb.clear()
        self.assertEqual(len(cb.reports), 0)
        self.assertIsNone(cb.last_report)

    def test_total_claims(self):
        cb = ConfabTaskCallback()
        cb(MockTaskOutput(raw="No claims here."))
        cb(MockTaskOutput(raw="No claims either."))
        self.assertEqual(cb.total_claims, 0)

    def test_total_failures_with_mixed(self):
        """Should count failures across multiple reports."""
        cb = ConfabTaskCallback(on_fail="log")
        # Clean output
        cb(MockTaskOutput(raw="All good."))
        # Failing output
        cb(MockTaskOutput(
            raw="Config at /tmp/confab_crewai_failures_test_xyz.toml is ready."
        ))
        self.assertGreater(cb.total_failures, 0)

    def test_clean_property_all_clean(self):
        cb = ConfabTaskCallback()
        cb(MockTaskOutput(raw="Clean output."))
        cb(MockTaskOutput(raw="Also clean."))
        self.assertTrue(cb.clean)

    def test_clean_property_with_failure(self):
        cb = ConfabTaskCallback(on_fail="log")
        cb(MockTaskOutput(raw="Clean output."))
        cb(MockTaskOutput(
            raw="Script at /tmp/confab_crewai_clean_prop_xyz.py is ready."
        ))
        self.assertFalse(cb.clean)

    def test_summary_no_reports(self):
        cb = ConfabTaskCallback()
        self.assertIn("no verification runs", cb.summary())

    def test_summary_all_clean(self):
        cb = ConfabTaskCallback()
        cb(MockTaskOutput(raw="Clean output."))
        self.assertIn("CLEAN", cb.summary())

    def test_summary_with_failures(self):
        cb = ConfabTaskCallback(on_fail="log")
        cb(MockTaskOutput(
            raw="Config at /tmp/confab_crewai_summary_xyz.json is deployed."
        ))
        self.assertIn("FAILED", cb.summary())


class TestExtractText(unittest.TestCase):
    """Test _extract_text handles various input types."""

    def test_task_output_raw(self):
        text = ConfabTaskCallback._extract_text(
            MockTaskOutput(raw="hello world")
        )
        self.assertEqual(text, "hello world")

    def test_plain_string(self):
        text = ConfabTaskCallback._extract_text("direct string")
        self.assertEqual(text, "direct string")

    def test_object_with_text_attr(self):
        class TextObj:
            text = "from text attr"

        text = ConfabTaskCallback._extract_text(TextObj())
        self.assertEqual(text, "from text attr")

    def test_fallback_to_str(self):
        text = ConfabTaskCallback._extract_text(42)
        self.assertEqual(text, "42")


class TestRealisticCrewAIScenarios(unittest.TestCase):
    """Test with text resembling real CrewAI agent output."""

    def test_multi_task_crew_with_mixed_results(self):
        """Simulate a crew with multiple tasks, some clean, some not."""
        cb = ConfabTaskCallback(on_fail="log")

        # Task 1: research (clean)
        cb(MockTaskOutput(
            raw="The market analysis shows strong fundamentals.",
            description="market research",
        ))

        # Task 2: file operation (failing — file doesn't exist)
        cb(MockTaskOutput(
            raw="Config at /tmp/confab_crewai_report_xyz.json is ready.",
            description="write report",
        ))

        # Task 3: summary (clean)
        cb(MockTaskOutput(
            raw="Summary: research complete, report generated.",
            description="summarize",
        ))

        self.assertEqual(len(cb.reports), 3)
        # At least task 2 should have failures
        self.assertFalse(cb.clean)
        self.assertIn("FAILED", cb.summary())

    def test_real_file_in_task_output(self):
        """Task output referencing a real file should pass."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"col1,col2\n1,2\n")
            tmp = f.name
        try:
            cb = ConfabTaskCallback(on_fail="raise")
            cb(MockTaskOutput(
                raw=f"Data exported to {tmp} successfully.",
                description="export data",
            ))
            self.assertTrue(cb.last_report.clean)
        finally:
            os.unlink(tmp)


if __name__ == "__main__":
    unittest.main()
