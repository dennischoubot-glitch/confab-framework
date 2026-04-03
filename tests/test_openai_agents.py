"""Tests for confab OpenAI Agents SDK integration.

Uses mock agents module classes since the SDK is an optional dependency.
Tests verify that ConfabOutputGuardrail and ConfabRunVerifier correctly
extract text, run verification, and handle all on_fail modes.
"""

import asyncio
import os
import tempfile
import unittest
import warnings
from dataclasses import dataclass
from unittest.mock import MagicMock
import sys


# ---------------------------------------------------------------------------
# Mock agents module before importing the integration
# ---------------------------------------------------------------------------

mock_agents = MagicMock()


@dataclass
class MockGuardrailFunctionOutput:
    """Simulates agents.GuardrailFunctionOutput."""
    output_info: dict = None
    tripwire_triggered: bool = False


class MockOutputGuardrail:
    """Simulates agents.OutputGuardrail base class."""
    pass


class MockAgent:
    """Simulates agents.Agent."""
    def __init__(self, name: str = "test_agent"):
        self.name = name


class MockRunContextWrapper:
    """Simulates agents.RunContextWrapper."""
    pass


mock_agents.GuardrailFunctionOutput = MockGuardrailFunctionOutput
mock_agents.OutputGuardrail = MockOutputGuardrail
mock_agents.Agent = MockAgent
mock_agents.RunContextWrapper = MockRunContextWrapper

sys.modules["agents"] = mock_agents

from confab.integrations.openai_agents import (
    ConfabOutputGuardrail,
    ConfabRunVerifier,
)
from confab.middleware import ConfabVerificationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_async(coro):
    """Run an async coroutine synchronously for testing."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Mock result types (simulate OpenAI Agents SDK dataclasses)
# ---------------------------------------------------------------------------

class MockRunResult:
    """Simulates agents.RunResult."""
    def __init__(self, final_output=None):
        self.final_output = final_output
        self.input = "test input"
        self.new_items = []


class MockPydanticOutput:
    """Simulates a Pydantic model output from a structured agent."""
    def __init__(self, **kwargs):
        self._data = kwargs

    def model_dump(self):
        return self._data


# ---------------------------------------------------------------------------
# ConfabOutputGuardrail tests
# ---------------------------------------------------------------------------

class TestConfabOutputGuardrailInit(unittest.TestCase):
    """Test ConfabOutputGuardrail initialization and configuration."""

    def test_default_init(self):
        g = ConfabOutputGuardrail()
        self.assertTrue(g.check_files)
        self.assertTrue(g.check_env)
        self.assertFalse(g.check_counts)
        self.assertEqual(g.on_fail, "warn")
        self.assertEqual(g.reports, [])

    def test_custom_init(self):
        g = ConfabOutputGuardrail(
            check_files=False,
            check_env=False,
            check_counts=True,
            on_fail="log",
        )
        self.assertFalse(g.check_files)
        self.assertFalse(g.check_env)
        self.assertTrue(g.check_counts)
        self.assertEqual(g.on_fail, "log")

    def test_tripwire_mode_accepted(self):
        """OpenAI Agents SDK supports 'tripwire' mode (unique to this integration)."""
        g = ConfabOutputGuardrail(on_fail="tripwire")
        self.assertEqual(g.on_fail, "tripwire")

    def test_invalid_on_fail_raises(self):
        with self.assertRaises(ValueError):
            ConfabOutputGuardrail(on_fail="explode")

    def test_inject_mode_rejected(self):
        """'inject' is Agent SDK-specific, not valid for OpenAI."""
        with self.assertRaises(ValueError):
            ConfabOutputGuardrail(on_fail="inject")

    def test_drop_mode_rejected(self):
        """'drop' is AutoGen-specific, not valid for OpenAI."""
        with self.assertRaises(ValueError):
            ConfabOutputGuardrail(on_fail="drop")

    def test_last_report_initially_none(self):
        g = ConfabOutputGuardrail()
        self.assertIsNone(g.last_report)


class TestConfabOutputGuardrailRun(unittest.TestCase):
    """Test the async run() guardrail with various inputs."""

    def _run_guardrail(self, guardrail, output, agent_name="test_agent"):
        ctx = MockRunContextWrapper()
        agent = MockAgent(name=agent_name)
        return run_async(guardrail.run(ctx, agent, output))

    def test_clean_output(self):
        """Output with no verifiable claims produces clean report."""
        g = ConfabOutputGuardrail()
        result = self._run_guardrail(g, "The system is healthy.")
        self.assertFalse(result.tripwire_triggered)
        self.assertEqual(result.output_info["confab"], "clean")
        self.assertEqual(len(g.reports), 1)
        self.assertTrue(g.last_report.clean)

    def test_existing_file_passes(self):
        """Claims about files that exist should pass."""
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"# test")
            tmp = f.name
        try:
            g = ConfabOutputGuardrail()
            result = self._run_guardrail(g, f"Script at {tmp} is ready.")
            self.assertEqual(g.last_report.failed, 0)
            self.assertFalse(result.tripwire_triggered)
        finally:
            os.unlink(tmp)

    def test_nonexistent_file_fails(self):
        """Claims about missing files should fail."""
        g = ConfabOutputGuardrail(on_fail="log")
        result = self._run_guardrail(
            g, "Config at /tmp/confab_oai_test_missing_xyz.json is ready."
        )
        self.assertGreater(g.last_report.failed, 0)
        self.assertFalse(g.last_report.clean)
        self.assertEqual(result.output_info["confab"], "failed")

    def test_empty_output_skipped(self):
        """Empty output should not produce a report."""
        g = ConfabOutputGuardrail()
        result = self._run_guardrail(g, "")
        self.assertEqual(len(g.reports), 0)
        self.assertEqual(result.output_info["confab"], "skipped")

    def test_whitespace_output_skipped(self):
        """Whitespace-only output should not produce a report."""
        g = ConfabOutputGuardrail()
        result = self._run_guardrail(g, "   \n  ")
        self.assertEqual(len(g.reports), 0)

    def test_string_output(self):
        """Plain string output should be verified."""
        g = ConfabOutputGuardrail()
        result = self._run_guardrail(g, "Analysis complete. No issues found.")
        self.assertEqual(len(g.reports), 1)
        self.assertFalse(result.tripwire_triggered)

    def test_pydantic_output(self):
        """Pydantic model output should extract string fields."""
        g = ConfabOutputGuardrail()
        output = MockPydanticOutput(
            summary="Analysis complete.",
            status="All systems operational.",
        )
        result = self._run_guardrail(g, output)
        self.assertEqual(len(g.reports), 1)

    def test_multiple_calls_accumulate(self):
        """Multiple guardrail runs should accumulate reports."""
        g = ConfabOutputGuardrail()
        for i in range(3):
            self._run_guardrail(g, f"Output {i}.")
        self.assertEqual(len(g.reports), 3)

    def test_agent_name_in_source(self):
        """Agent name should be used in source label."""
        g = ConfabOutputGuardrail(on_fail="log")
        self._run_guardrail(
            g,
            "Config at /tmp/confab_oai_name_xyz.toml deployed.",
            agent_name="my_custom_agent",
        )
        self.assertEqual(len(g.reports), 1)


class TestConfabOutputGuardrailOnFail(unittest.TestCase):
    """Test all four on_fail modes for ConfabOutputGuardrail."""

    def _run_failing(self, guardrail):
        ctx = MockRunContextWrapper()
        agent = MockAgent(name="test")
        return run_async(guardrail.run(
            ctx, agent,
            "Database at /tmp/confab_oai_onfail_xyz.db is ready.",
        ))

    def test_warn_mode_issues_warning(self):
        g = ConfabOutputGuardrail(on_fail="warn")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            self._run_failing(g)
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertGreater(len(confab_warns), 0)

    def test_raise_mode_raises_error(self):
        g = ConfabOutputGuardrail(on_fail="raise")
        with self.assertRaises(ConfabVerificationError) as ctx:
            self._run_failing(g)
        self.assertGreater(len(ctx.exception.failures), 0)

    def test_log_mode_no_raise_no_warn(self):
        g = ConfabOutputGuardrail(on_fail="log")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            self._run_failing(g)
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertEqual(len(confab_warns), 0)
        self.assertFalse(g.last_report.clean)

    def test_tripwire_mode_triggers(self):
        """Tripwire mode should set tripwire_triggered=True on failure."""
        g = ConfabOutputGuardrail(on_fail="tripwire")
        result = self._run_failing(g)
        self.assertTrue(result.tripwire_triggered)
        self.assertEqual(result.output_info["confab"], "failed")
        self.assertIn("summary", result.output_info)

    def test_tripwire_mode_passthrough_on_clean(self):
        """Tripwire mode should not trigger on clean output."""
        g = ConfabOutputGuardrail(on_fail="tripwire")
        ctx = MockRunContextWrapper()
        agent = MockAgent(name="test")
        result = run_async(g.run(ctx, agent, "All systems nominal."))
        self.assertFalse(result.tripwire_triggered)


# ---------------------------------------------------------------------------
# ConfabRunVerifier tests
# ---------------------------------------------------------------------------

class TestConfabRunVerifierInit(unittest.TestCase):
    """Test ConfabRunVerifier initialization."""

    def test_default_init(self):
        v = ConfabRunVerifier()
        self.assertTrue(v.check_files)
        self.assertTrue(v.check_env)
        self.assertFalse(v.check_counts)
        self.assertEqual(v.on_fail, "warn")
        self.assertEqual(v.reports, [])

    def test_custom_init(self):
        v = ConfabRunVerifier(
            check_files=False, check_env=False, check_counts=True, on_fail="log"
        )
        self.assertFalse(v.check_files)
        self.assertFalse(v.check_env)
        self.assertTrue(v.check_counts)

    def test_invalid_on_fail_raises(self):
        with self.assertRaises(ValueError):
            ConfabRunVerifier(on_fail="tripwire")

    def test_last_report_initially_none(self):
        v = ConfabRunVerifier()
        self.assertIsNone(v.last_report)


class TestConfabRunVerifierVerify(unittest.TestCase):
    """Test run result verification with mock RunResult objects."""

    def test_string_final_output(self):
        """RunResult with string final_output should be verified."""
        v = ConfabRunVerifier()
        result = MockRunResult(final_output="The system is running fine.")
        report = v.verify(result)
        self.assertIsNotNone(report)
        self.assertTrue(report.clean)

    def test_pydantic_final_output(self):
        """RunResult with Pydantic model output should extract strings."""
        v = ConfabRunVerifier()
        result = MockRunResult(
            final_output=MockPydanticOutput(
                analysis="Analysis complete.",
                recommendation="No issues found.",
            ),
        )
        report = v.verify(result)
        self.assertIsNotNone(report)

    def test_none_final_output(self):
        """RunResult with None final_output should be skipped."""
        v = ConfabRunVerifier()
        result = MockRunResult(final_output=None)
        report = v.verify(result)
        self.assertIsNone(report)
        self.assertEqual(len(v.reports), 0)

    def test_plain_string(self):
        """Plain string messages should work."""
        v = ConfabRunVerifier()
        report = v.verify("The pipeline is healthy.")
        self.assertIsNotNone(report)

    def test_empty_string_skipped(self):
        """Empty string should not produce a report."""
        v = ConfabRunVerifier()
        report = v.verify("")
        self.assertIsNone(report)

    def test_existing_file_passes(self):
        """Claims about real files should pass."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"data")
            tmp = f.name
        try:
            v = ConfabRunVerifier()
            result = MockRunResult(
                final_output=f"Output written to {tmp} successfully.",
            )
            v.verify(result)
            self.assertEqual(v.last_report.failed, 0)
        finally:
            os.unlink(tmp)

    def test_nonexistent_file_fails(self):
        """Claims about missing files should fail."""
        v = ConfabRunVerifier(on_fail="log")
        result = MockRunResult(
            final_output="Config at /tmp/confab_oai_msg_missing_xyz.json is deployed.",
        )
        v.verify(result)
        self.assertGreater(v.last_report.failed, 0)

    def test_multiple_results_accumulate(self):
        """Multiple verify calls should accumulate reports."""
        v = ConfabRunVerifier()
        for i in range(3):
            v.verify(MockRunResult(final_output=f"Output {i}."))
        self.assertEqual(len(v.reports), 3)


class TestConfabRunVerifierOnFail(unittest.TestCase):
    """Test on_fail modes for ConfabRunVerifier."""

    def _failing_result(self):
        return MockRunResult(
            final_output="Database at /tmp/confab_oai_vermsg_xyz.db is ready.",
        )

    def test_warn_mode(self):
        v = ConfabRunVerifier(on_fail="warn")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            v.verify(self._failing_result())
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertGreater(len(confab_warns), 0)

    def test_raise_mode(self):
        v = ConfabRunVerifier(on_fail="raise")
        with self.assertRaises(ConfabVerificationError) as ctx:
            v.verify(self._failing_result())
        self.assertGreater(len(ctx.exception.failures), 0)

    def test_log_mode(self):
        v = ConfabRunVerifier(on_fail="log")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            v.verify(self._failing_result())
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertEqual(len(confab_warns), 0)
        self.assertFalse(v.last_report.clean)


# ---------------------------------------------------------------------------
# Convenience methods
# ---------------------------------------------------------------------------

class TestConvenienceMethodsGuardrail(unittest.TestCase):
    """Test convenience properties on ConfabOutputGuardrail."""

    def _run(self, g, output):
        ctx = MockRunContextWrapper()
        agent = MockAgent(name="test")
        return run_async(g.run(ctx, agent, output))

    def test_clear(self):
        g = ConfabOutputGuardrail()
        self._run(g, "Output one.")
        self._run(g, "Output two.")
        self.assertEqual(len(g.reports), 2)
        g.clear()
        self.assertEqual(len(g.reports), 0)
        self.assertIsNone(g.last_report)

    def test_total_claims_zero(self):
        g = ConfabOutputGuardrail()
        self._run(g, "No claims here.")
        self.assertEqual(g.total_claims, 0)

    def test_clean_property_all_clean(self):
        g = ConfabOutputGuardrail()
        self._run(g, "All good.")
        self.assertTrue(g.clean)

    def test_clean_property_with_failure(self):
        g = ConfabOutputGuardrail(on_fail="log")
        self._run(g, "Script at /tmp/confab_oai_clean_xyz.py is ready.")
        self.assertFalse(g.clean)

    def test_summary_no_reports(self):
        g = ConfabOutputGuardrail()
        self.assertIn("no verification runs", g.summary())

    def test_summary_all_clean(self):
        g = ConfabOutputGuardrail()
        self._run(g, "Clean output.")
        self.assertIn("CLEAN", g.summary())

    def test_summary_with_failures(self):
        g = ConfabOutputGuardrail(on_fail="log")
        self._run(g, "Config at /tmp/confab_oai_summary_xyz.json deployed.")
        self.assertIn("FAILED", g.summary())


class TestConvenienceMethodsVerifier(unittest.TestCase):
    """Test convenience properties on ConfabRunVerifier."""

    def test_clear(self):
        v = ConfabRunVerifier()
        v.verify(MockRunResult(final_output="Output one."))
        v.verify(MockRunResult(final_output="Output two."))
        self.assertEqual(len(v.reports), 2)
        v.clear()
        self.assertEqual(len(v.reports), 0)

    def test_total_failures_with_mixed(self):
        v = ConfabRunVerifier(on_fail="log")
        v.verify(MockRunResult(final_output="All good."))
        v.verify(MockRunResult(
            final_output="Config at /tmp/confab_oai_mixed_xyz.toml is ready.",
        ))
        self.assertGreater(v.total_failures, 0)

    def test_summary_no_reports(self):
        v = ConfabRunVerifier()
        self.assertIn("no verification runs", v.summary())

    def test_summary_all_clean(self):
        v = ConfabRunVerifier()
        v.verify(MockRunResult(final_output="Clean output."))
        self.assertIn("CLEAN", v.summary())

    def test_summary_with_failures(self):
        v = ConfabRunVerifier(on_fail="log")
        v.verify(MockRunResult(
            final_output="Config at /tmp/confab_oai_summ_xyz.json deployed.",
        ))
        self.assertIn("FAILED", v.summary())


# ---------------------------------------------------------------------------
# Text extraction edge cases
# ---------------------------------------------------------------------------

class TestExtractTextGuardrail(unittest.TestCase):
    """Test _extract_text on ConfabOutputGuardrail."""

    def test_string_output(self):
        text = ConfabOutputGuardrail._extract_text("hello")
        self.assertEqual(text, "hello")

    def test_pydantic_output(self):
        output = MockPydanticOutput(summary="result", detail="more info")
        text = ConfabOutputGuardrail._extract_text(output)
        self.assertIn("result", text)
        self.assertIn("more info", text)

    def test_text_attr_fallback(self):
        class TextAttr:
            text = "fallback text"
        text = ConfabOutputGuardrail._extract_text(TextAttr())
        self.assertEqual(text, "fallback text")

    def test_content_attr_fallback(self):
        class ContentAttr:
            content = "content text"
        text = ConfabOutputGuardrail._extract_text(ContentAttr())
        self.assertEqual(text, "content text")

    def test_none_returns_empty(self):
        text = ConfabOutputGuardrail._extract_text(None)
        self.assertEqual(text, "")

    def test_numeric_returns_str(self):
        text = ConfabOutputGuardrail._extract_text(42)
        self.assertEqual(text, "42")


class TestExtractTextVerifier(unittest.TestCase):
    """Test _extract_text on ConfabRunVerifier."""

    def test_run_result_string(self):
        result = MockRunResult(final_output="final output")
        text = ConfabRunVerifier._extract_text(result)
        self.assertEqual(text, "final output")

    def test_run_result_pydantic(self):
        result = MockRunResult(
            final_output=MockPydanticOutput(answer="the answer"),
        )
        text = ConfabRunVerifier._extract_text(result)
        self.assertIn("the answer", text)

    def test_run_result_none(self):
        result = MockRunResult(final_output=None)
        text = ConfabRunVerifier._extract_text(result)
        self.assertEqual(text, "")

    def test_plain_string(self):
        text = ConfabRunVerifier._extract_text("direct string")
        self.assertEqual(text, "direct string")

    def test_output_attr_fallback(self):
        class OutputAttr:
            output = "output text"
        text = ConfabRunVerifier._extract_text(OutputAttr())
        self.assertEqual(text, "output text")

    def test_text_attr_fallback(self):
        class TextAttr:
            text = "text fallback"
        text = ConfabRunVerifier._extract_text(TextAttr())
        self.assertEqual(text, "text fallback")

    def test_no_attrs_returns_empty(self):
        class Empty:
            pass
        text = ConfabRunVerifier._extract_text(Empty())
        self.assertEqual(text, "")


# ---------------------------------------------------------------------------
# Realistic scenarios
# ---------------------------------------------------------------------------

class TestRealisticScenarios(unittest.TestCase):
    """Test with text resembling real OpenAI Agents SDK output."""

    def test_multi_run_verification(self):
        """Simulate verifying multiple run results."""
        v = ConfabRunVerifier(on_fail="log")

        # Run 1: clean output
        v.verify(MockRunResult(
            final_output="I've analyzed the data. Everything looks correct.",
        ))

        # Run 2: failing claim
        v.verify(MockRunResult(
            final_output="Config at /tmp/confab_oai_realistic_xyz.json has been deployed.",
        ))

        # Run 3: clean output
        v.verify(MockRunResult(
            final_output="Task completed. 2 files processed.",
        ))

        self.assertEqual(len(v.reports), 3)
        self.assertFalse(v.clean)
        self.assertIn("FAILED", v.summary())

    def test_guardrail_with_real_file(self):
        """Guardrail verifying output that references a real file."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"col1,col2\n1,2\n")
            tmp = f.name
        try:
            g = ConfabOutputGuardrail(on_fail="raise")
            ctx = MockRunContextWrapper()
            agent = MockAgent(name="writer")
            result = run_async(g.run(
                ctx, agent, f"Successfully wrote data to {tmp}.",
            ))
            self.assertTrue(g.last_report.clean)
            self.assertFalse(result.tripwire_triggered)
        finally:
            os.unlink(tmp)

    def test_tripwire_mode_end_to_end(self):
        """Tripwire mode should trigger only when claims fail."""
        g = ConfabOutputGuardrail(on_fail="tripwire")
        ctx = MockRunContextWrapper()
        agent = MockAgent(name="checker")

        # Clean output
        result1 = run_async(g.run(ctx, agent, "Found 3 matches."))
        self.assertFalse(result1.tripwire_triggered)

        # Failing output
        result2 = run_async(g.run(
            ctx, agent,
            "Config at /tmp/confab_oai_tripwire_xyz.yaml is deployed.",
        ))
        self.assertTrue(result2.tripwire_triggered)
        self.assertIn("confab", str(result2.output_info))

    def test_structured_output_agent(self):
        """Simulate a structured output agent with Pydantic model."""
        g = ConfabOutputGuardrail(on_fail="log")
        ctx = MockRunContextWrapper()
        agent = MockAgent(name="structured_agent")

        output = MockPydanticOutput(
            summary="The deployment is complete.",
            config_path="/tmp/confab_oai_struct_xyz.yaml is ready.",
            status="operational",
        )
        result = run_async(g.run(ctx, agent, output))
        self.assertEqual(len(g.reports), 1)


if __name__ == "__main__":
    unittest.main()
