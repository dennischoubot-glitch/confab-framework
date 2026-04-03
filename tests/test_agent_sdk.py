"""Tests for confab Agent SDK integration.

Uses mock claude_agent_sdk classes since the SDK is an optional dependency.
Tests verify that ConfabPostToolUseHook and ConfabMessageVerifier correctly
extract text, run verification, and handle all on_fail modes.
"""

import asyncio
import os
import tempfile
import unittest
import warnings
from unittest.mock import MagicMock
import sys


# ---------------------------------------------------------------------------
# Mock claude_agent_sdk module before importing the integration
# ---------------------------------------------------------------------------

mock_sdk = MagicMock()
sys.modules["claude_agent_sdk"] = mock_sdk

from confab.integrations.agent_sdk import (
    ConfabPostToolUseHook,
    ConfabMessageVerifier,
)
from confab.middleware import ConfabVerificationError, VerificationReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_async(coro):
    """Run an async coroutine synchronously for testing."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Mock message types (simulate Agent SDK dataclasses)
# ---------------------------------------------------------------------------

class MockTextBlock:
    """Simulates claude_agent_sdk.TextBlock."""

    def __init__(self, text: str = ""):
        self.text = text


class MockToolUseBlock:
    """Simulates claude_agent_sdk.ToolUseBlock."""

    def __init__(self, id: str = "tu_1", name: str = "Read", input: dict = None):
        self.id = id
        self.name = name
        self.input = input or {}


class MockThinkingBlock:
    """Simulates claude_agent_sdk.ThinkingBlock (no .text, has .thinking)."""

    def __init__(self, thinking: str = ""):
        self.thinking = thinking


class MockAssistantMessage:
    """Simulates claude_agent_sdk.AssistantMessage."""

    def __init__(self, content: list = None, model: str = "claude-opus-4-6"):
        self.content = content or []
        self.model = model


class MockResultMessage:
    """Simulates claude_agent_sdk.ResultMessage."""

    def __init__(self, result: str = None, is_error: bool = False):
        self.result = result
        self.subtype = "error" if is_error else "final"
        self.is_error = is_error
        self.num_turns = 1
        self.session_id = "test-session"


class MockPostToolUseInput:
    """Simulates PostToolUseHookInput as a dict-like object."""

    def __init__(self, tool_name: str = "Read", tool_response: str = ""):
        self.tool_name = tool_name
        self.tool_response = tool_response
        self.tool_input = {}
        self.tool_use_id = "tu_1"
        self.hook_event_name = "PostToolUse"


# ---------------------------------------------------------------------------
# ConfabPostToolUseHook tests
# ---------------------------------------------------------------------------

class TestConfabPostToolUseHookInit(unittest.TestCase):
    """Test ConfabPostToolUseHook initialization and configuration."""

    def test_default_init(self):
        h = ConfabPostToolUseHook()
        self.assertTrue(h.check_files)
        self.assertTrue(h.check_env)
        self.assertFalse(h.check_counts)
        self.assertEqual(h.on_fail, "warn")
        self.assertEqual(h.reports, [])

    def test_custom_init(self):
        h = ConfabPostToolUseHook(
            check_files=False,
            check_env=False,
            check_counts=True,
            on_fail="log",
        )
        self.assertFalse(h.check_files)
        self.assertFalse(h.check_env)
        self.assertTrue(h.check_counts)
        self.assertEqual(h.on_fail, "log")

    def test_inject_mode_accepted(self):
        """Agent SDK supports 'inject' mode (unique to this integration)."""
        h = ConfabPostToolUseHook(on_fail="inject")
        self.assertEqual(h.on_fail, "inject")

    def test_invalid_on_fail_raises(self):
        with self.assertRaises(ValueError):
            ConfabPostToolUseHook(on_fail="explode")

    def test_drop_mode_rejected(self):
        """'drop' is AutoGen-specific, not valid for Agent SDK."""
        with self.assertRaises(ValueError):
            ConfabPostToolUseHook(on_fail="drop")

    def test_last_report_initially_none(self):
        h = ConfabPostToolUseHook()
        self.assertIsNone(h.last_report)


class TestConfabPostToolUseHookCall(unittest.TestCase):
    """Test the async __call__ hook with various inputs."""

    def test_clean_tool_output(self):
        """Tool output with no verifiable claims produces clean report."""
        h = ConfabPostToolUseHook()
        result = run_async(h(
            {"tool_name": "Read", "tool_response": "The system is healthy."},
        ))
        self.assertEqual(result, {})
        self.assertEqual(len(h.reports), 1)
        self.assertTrue(h.last_report.clean)

    def test_existing_file_passes(self):
        """Claims about files that exist should pass."""
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"# test")
            tmp = f.name
        try:
            h = ConfabPostToolUseHook()
            run_async(h(
                {"tool_name": "Read", "tool_response": f"Script at {tmp} is ready."},
            ))
            self.assertEqual(h.last_report.failed, 0)
        finally:
            os.unlink(tmp)

    def test_nonexistent_file_fails(self):
        """Claims about missing files should fail."""
        h = ConfabPostToolUseHook(on_fail="log")
        run_async(h(
            {
                "tool_name": "Read",
                "tool_response": "Config at /tmp/confab_sdk_test_missing_xyz.json is ready.",
            },
        ))
        self.assertGreater(h.last_report.failed, 0)
        self.assertFalse(h.last_report.clean)

    def test_empty_response_skipped(self):
        """Empty tool response should not produce a report."""
        h = ConfabPostToolUseHook()
        run_async(h({"tool_name": "Read", "tool_response": ""}))
        self.assertEqual(len(h.reports), 0)

    def test_whitespace_response_skipped(self):
        """Whitespace-only tool response should not produce a report."""
        h = ConfabPostToolUseHook()
        run_async(h({"tool_name": "Read", "tool_response": "   \n  "}))
        self.assertEqual(len(h.reports), 0)

    def test_dict_input_extracts_tool_name(self):
        """Dict input should extract tool_name for labeling."""
        h = ConfabPostToolUseHook(on_fail="log")
        run_async(h(
            {
                "tool_name": "Bash",
                "tool_response": "Config at /tmp/confab_sdk_toolname_xyz.toml deployed.",
            },
        ))
        self.assertEqual(len(h.reports), 1)

    def test_object_input_extracts_text(self):
        """Object with tool_response attribute should work."""
        h = ConfabPostToolUseHook()
        inp = MockPostToolUseInput(
            tool_name="Grep",
            tool_response="Found 5 matches. All systems nominal.",
        )
        run_async(h(inp))
        self.assertEqual(len(h.reports), 1)

    def test_list_tool_response(self):
        """List of content blocks in tool_response should be joined."""
        h = ConfabPostToolUseHook()
        run_async(h(
            {
                "tool_name": "Read",
                "tool_response": [
                    {"text": "Line 1: all good."},
                    {"text": "Line 2: no issues."},
                ],
            },
        ))
        self.assertEqual(len(h.reports), 1)
        self.assertTrue(h.last_report.clean)

    def test_dict_tool_response_with_text_key(self):
        """Dict tool_response with 'text' key should extract text."""
        h = ConfabPostToolUseHook()
        run_async(h(
            {
                "tool_name": "Read",
                "tool_response": {"text": "Everything is fine."},
            },
        ))
        self.assertEqual(len(h.reports), 1)

    def test_multiple_calls_accumulate(self):
        """Multiple hook calls should accumulate reports."""
        h = ConfabPostToolUseHook()
        for i in range(3):
            run_async(h(
                {"tool_name": "Read", "tool_response": f"Output {i}."},
            ))
        self.assertEqual(len(h.reports), 3)


class TestConfabPostToolUseHookOnFail(unittest.TestCase):
    """Test all four on_fail modes for ConfabPostToolUseHook."""

    def _failing_input(self):
        return {
            "tool_name": "Read",
            "tool_response": "Database at /tmp/confab_sdk_onfail_xyz.db is ready.",
        }

    def test_warn_mode_issues_warning(self):
        h = ConfabPostToolUseHook(on_fail="warn")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            run_async(h(self._failing_input()))
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertGreater(len(confab_warns), 0)

    def test_raise_mode_raises_error(self):
        h = ConfabPostToolUseHook(on_fail="raise")
        with self.assertRaises(ConfabVerificationError) as ctx:
            run_async(h(self._failing_input()))
        self.assertGreater(len(ctx.exception.failures), 0)

    def test_log_mode_no_raise_no_warn(self):
        h = ConfabPostToolUseHook(on_fail="log")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            run_async(h(self._failing_input()))
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertEqual(len(confab_warns), 0)
        self.assertFalse(h.last_report.clean)

    def test_inject_mode_returns_additional_context(self):
        """Inject mode should return hook output with additionalContext."""
        h = ConfabPostToolUseHook(on_fail="inject")
        result = run_async(h(self._failing_input()))
        self.assertIn("hookSpecificOutput", result)
        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PostToolUse")
        self.assertIn("CONFABULATION WARNING", output["additionalContext"])

    def test_inject_mode_passthrough_on_clean(self):
        """Inject mode should return empty dict on clean output."""
        h = ConfabPostToolUseHook(on_fail="inject")
        result = run_async(h(
            {"tool_name": "Read", "tool_response": "All systems nominal."},
        ))
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# ConfabMessageVerifier tests
# ---------------------------------------------------------------------------

class TestConfabMessageVerifierInit(unittest.TestCase):
    """Test ConfabMessageVerifier initialization."""

    def test_default_init(self):
        v = ConfabMessageVerifier()
        self.assertTrue(v.check_files)
        self.assertTrue(v.check_env)
        self.assertFalse(v.check_counts)
        self.assertEqual(v.on_fail, "warn")
        self.assertEqual(v.reports, [])

    def test_custom_init(self):
        v = ConfabMessageVerifier(
            check_files=False, check_env=False, check_counts=True, on_fail="log"
        )
        self.assertFalse(v.check_files)
        self.assertFalse(v.check_env)
        self.assertTrue(v.check_counts)

    def test_invalid_on_fail_raises(self):
        with self.assertRaises(ValueError):
            ConfabMessageVerifier(on_fail="inject")

    def test_last_report_initially_none(self):
        v = ConfabMessageVerifier()
        self.assertIsNone(v.last_report)


class TestConfabMessageVerifierVerify(unittest.TestCase):
    """Test message verification with mock Agent SDK messages."""

    def test_assistant_message_text_blocks(self):
        """AssistantMessage with TextBlock content should be verified."""
        v = ConfabMessageVerifier()
        msg = MockAssistantMessage(content=[
            MockTextBlock("The system is running fine."),
        ])
        report = v.verify(msg)
        self.assertIsNotNone(report)
        self.assertTrue(report.clean)

    def test_assistant_message_mixed_blocks(self):
        """Only TextBlock content should be extracted from AssistantMessage."""
        v = ConfabMessageVerifier()
        msg = MockAssistantMessage(content=[
            MockThinkingBlock("Let me think..."),
            MockTextBlock("Analysis complete. No issues found."),
            MockToolUseBlock(name="Read"),
        ])
        report = v.verify(msg)
        self.assertIsNotNone(report)
        self.assertEqual(len(v.reports), 1)

    def test_assistant_message_multiple_text_blocks(self):
        """Multiple TextBlocks should be joined."""
        v = ConfabMessageVerifier()
        msg = MockAssistantMessage(content=[
            MockTextBlock("First part."),
            MockTextBlock("Second part."),
        ])
        report = v.verify(msg)
        self.assertIsNotNone(report)

    def test_result_message(self):
        """ResultMessage with .result string should be verified."""
        v = ConfabMessageVerifier()
        msg = MockResultMessage(result="Task completed successfully.")
        report = v.verify(msg)
        self.assertIsNotNone(report)
        self.assertTrue(report.clean)

    def test_result_message_none_result(self):
        """ResultMessage with None result should be skipped."""
        v = ConfabMessageVerifier()
        msg = MockResultMessage(result=None)
        report = v.verify(msg)
        self.assertIsNone(report)
        self.assertEqual(len(v.reports), 0)

    def test_plain_string(self):
        """Plain string messages should work."""
        v = ConfabMessageVerifier()
        report = v.verify("The pipeline is healthy.")
        self.assertIsNotNone(report)

    def test_empty_content_skipped(self):
        """AssistantMessage with no text blocks should be skipped."""
        v = ConfabMessageVerifier()
        msg = MockAssistantMessage(content=[MockToolUseBlock()])
        report = v.verify(msg)
        self.assertIsNone(report)

    def test_empty_string_skipped(self):
        """Empty string should not produce a report."""
        v = ConfabMessageVerifier()
        report = v.verify("")
        self.assertIsNone(report)

    def test_existing_file_passes(self):
        """Claims about real files in messages should pass."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"data")
            tmp = f.name
        try:
            v = ConfabMessageVerifier()
            msg = MockAssistantMessage(content=[
                MockTextBlock(f"Output written to {tmp} successfully."),
            ])
            v.verify(msg)
            self.assertEqual(v.last_report.failed, 0)
        finally:
            os.unlink(tmp)

    def test_nonexistent_file_fails(self):
        """Claims about missing files should fail."""
        v = ConfabMessageVerifier(on_fail="log")
        msg = MockAssistantMessage(content=[
            MockTextBlock(
                "Config at /tmp/confab_sdk_msg_missing_xyz.json is deployed."
            ),
        ])
        v.verify(msg)
        self.assertGreater(v.last_report.failed, 0)

    def test_multiple_messages_accumulate(self):
        """Multiple verify calls should accumulate reports."""
        v = ConfabMessageVerifier()
        for i in range(3):
            v.verify(MockResultMessage(result=f"Output {i}."))
        self.assertEqual(len(v.reports), 3)


class TestConfabMessageVerifierOnFail(unittest.TestCase):
    """Test on_fail modes for ConfabMessageVerifier."""

    def _failing_msg(self):
        return MockAssistantMessage(content=[
            MockTextBlock(
                "Database at /tmp/confab_sdk_vermsg_xyz.db is ready."
            ),
        ])

    def test_warn_mode(self):
        v = ConfabMessageVerifier(on_fail="warn")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            v.verify(self._failing_msg())
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertGreater(len(confab_warns), 0)

    def test_raise_mode(self):
        v = ConfabMessageVerifier(on_fail="raise")
        with self.assertRaises(ConfabVerificationError) as ctx:
            v.verify(self._failing_msg())
        self.assertGreater(len(ctx.exception.failures), 0)

    def test_log_mode(self):
        v = ConfabMessageVerifier(on_fail="log")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            v.verify(self._failing_msg())
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertEqual(len(confab_warns), 0)
        self.assertFalse(v.last_report.clean)


# ---------------------------------------------------------------------------
# Convenience methods (shared interface)
# ---------------------------------------------------------------------------

class TestConvenienceMethodsHook(unittest.TestCase):
    """Test convenience properties on ConfabPostToolUseHook."""

    def test_clear(self):
        h = ConfabPostToolUseHook()
        run_async(h({"tool_name": "Read", "tool_response": "Output one."}))
        run_async(h({"tool_name": "Read", "tool_response": "Output two."}))
        self.assertEqual(len(h.reports), 2)
        h.clear()
        self.assertEqual(len(h.reports), 0)
        self.assertIsNone(h.last_report)

    def test_total_claims_zero(self):
        h = ConfabPostToolUseHook()
        run_async(h({"tool_name": "Read", "tool_response": "No claims here."}))
        self.assertEqual(h.total_claims, 0)

    def test_clean_property_all_clean(self):
        h = ConfabPostToolUseHook()
        run_async(h({"tool_name": "Read", "tool_response": "All good."}))
        self.assertTrue(h.clean)

    def test_clean_property_with_failure(self):
        h = ConfabPostToolUseHook(on_fail="log")
        run_async(h(
            {
                "tool_name": "Read",
                "tool_response": "Script at /tmp/confab_sdk_clean_xyz.py is ready.",
            },
        ))
        self.assertFalse(h.clean)

    def test_summary_no_reports(self):
        h = ConfabPostToolUseHook()
        self.assertIn("no verification runs", h.summary())

    def test_summary_all_clean(self):
        h = ConfabPostToolUseHook()
        run_async(h({"tool_name": "Read", "tool_response": "Clean output."}))
        self.assertIn("CLEAN", h.summary())

    def test_summary_with_failures(self):
        h = ConfabPostToolUseHook(on_fail="log")
        run_async(h(
            {
                "tool_name": "Read",
                "tool_response": "Config at /tmp/confab_sdk_summary_xyz.json deployed.",
            },
        ))
        self.assertIn("FAILED", h.summary())


class TestConvenienceMethodsVerifier(unittest.TestCase):
    """Test convenience properties on ConfabMessageVerifier."""

    def test_clear(self):
        v = ConfabMessageVerifier()
        v.verify(MockResultMessage(result="Output one."))
        v.verify(MockResultMessage(result="Output two."))
        self.assertEqual(len(v.reports), 2)
        v.clear()
        self.assertEqual(len(v.reports), 0)

    def test_total_failures_with_mixed(self):
        v = ConfabMessageVerifier(on_fail="log")
        v.verify(MockResultMessage(result="All good."))
        v.verify(MockAssistantMessage(content=[
            MockTextBlock(
                "Config at /tmp/confab_sdk_mixed_xyz.toml is ready."
            ),
        ]))
        self.assertGreater(v.total_failures, 0)

    def test_summary_no_reports(self):
        v = ConfabMessageVerifier()
        self.assertIn("no verification runs", v.summary())

    def test_summary_all_clean(self):
        v = ConfabMessageVerifier()
        v.verify(MockResultMessage(result="Clean output."))
        self.assertIn("CLEAN", v.summary())

    def test_summary_with_failures(self):
        v = ConfabMessageVerifier(on_fail="log")
        v.verify(MockAssistantMessage(content=[
            MockTextBlock(
                "Config at /tmp/confab_sdk_summ_xyz.json deployed."
            ),
        ]))
        self.assertIn("FAILED", v.summary())


# ---------------------------------------------------------------------------
# Text extraction edge cases
# ---------------------------------------------------------------------------

class TestExtractTextHook(unittest.TestCase):
    """Test _extract_text on ConfabPostToolUseHook."""

    def test_string_response(self):
        text = ConfabPostToolUseHook._extract_text(
            {"tool_name": "Read", "tool_response": "hello"}
        )
        self.assertEqual(text, "hello")

    def test_no_tool_response_key(self):
        text = ConfabPostToolUseHook._extract_text({"tool_name": "Read"})
        self.assertEqual(text, "")

    def test_object_with_tool_response(self):
        inp = MockPostToolUseInput(tool_response="from object")
        text = ConfabPostToolUseHook._extract_text(inp)
        self.assertEqual(text, "from object")

    def test_none_input(self):
        text = ConfabPostToolUseHook._extract_text(None)
        self.assertEqual(text, "")

    def test_numeric_input(self):
        text = ConfabPostToolUseHook._extract_text(42)
        self.assertEqual(text, "")

    def test_list_of_strings(self):
        text = ConfabPostToolUseHook._extract_text(
            {"tool_response": ["line 1", "line 2"]}
        )
        self.assertEqual(text, "line 1\nline 2")

    def test_list_of_text_objects(self):
        text = ConfabPostToolUseHook._extract_text(
            {"tool_response": [MockTextBlock("block 1"), MockTextBlock("block 2")]}
        )
        self.assertEqual(text, "block 1\nblock 2")


class TestExtractTextVerifier(unittest.TestCase):
    """Test _extract_text on ConfabMessageVerifier."""

    def test_assistant_message(self):
        msg = MockAssistantMessage(content=[MockTextBlock("hello")])
        text = ConfabMessageVerifier._extract_text(msg)
        self.assertEqual(text, "hello")

    def test_result_message(self):
        msg = MockResultMessage(result="final output")
        text = ConfabMessageVerifier._extract_text(msg)
        self.assertEqual(text, "final output")

    def test_plain_string(self):
        text = ConfabMessageVerifier._extract_text("direct string")
        self.assertEqual(text, "direct string")

    def test_empty_content_list(self):
        msg = MockAssistantMessage(content=[])
        text = ConfabMessageVerifier._extract_text(msg)
        self.assertEqual(text, "")

    def test_no_text_object(self):
        class NoText:
            pass
        text = ConfabMessageVerifier._extract_text(NoText())
        self.assertEqual(text, "")

    def test_text_attr_fallback(self):
        class TextAttr:
            text = "fallback text"
        text = ConfabMessageVerifier._extract_text(TextAttr())
        self.assertEqual(text, "fallback text")

    def test_result_preferred_over_content(self):
        """ResultMessage with .result should use .result, not .content."""

        class ResultAndContent:
            result = "from result"
            content = [MockTextBlock("from content")]

        text = ConfabMessageVerifier._extract_text(ResultAndContent())
        self.assertEqual(text, "from result")


# ---------------------------------------------------------------------------
# Realistic scenarios
# ---------------------------------------------------------------------------

class TestRealisticAgentScenarios(unittest.TestCase):
    """Test with text resembling real Agent SDK output."""

    def test_multi_turn_agent_mixed(self):
        """Simulate verifying multiple messages from an agent session."""
        v = ConfabMessageVerifier(on_fail="log")

        # Turn 1: agent reads a file (clean)
        v.verify(MockAssistantMessage(content=[
            MockTextBlock("I've read the configuration. Everything looks correct."),
        ]))

        # Turn 2: agent claims about a missing file (failing)
        v.verify(MockAssistantMessage(content=[
            MockTextBlock(
                "Config at /tmp/confab_sdk_realistic_xyz.json has been deployed."
            ),
        ]))

        # Turn 3: final result (clean)
        v.verify(MockResultMessage(result="Task completed. 2 files processed."))

        self.assertEqual(len(v.reports), 3)
        self.assertFalse(v.clean)
        self.assertIn("FAILED", v.summary())

    def test_hook_with_real_file(self):
        """Hook verifying tool output that references a real file."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"col1,col2\n1,2\n")
            tmp = f.name
        try:
            h = ConfabPostToolUseHook(on_fail="raise")
            run_async(h(
                {
                    "tool_name": "Write",
                    "tool_response": f"Successfully wrote data to {tmp}.",
                },
            ))
            self.assertTrue(h.last_report.clean)
        finally:
            os.unlink(tmp)

    def test_inject_mode_end_to_end(self):
        """Inject mode should return context only when claims fail."""
        h = ConfabPostToolUseHook(on_fail="inject")

        # Clean tool output
        result1 = run_async(h(
            {"tool_name": "Grep", "tool_response": "Found 3 matches."},
        ))
        self.assertEqual(result1, {})

        # Failing tool output
        result2 = run_async(h(
            {
                "tool_name": "Read",
                "tool_response": (
                    "Config at /tmp/confab_sdk_inject_xyz.yaml is deployed."
                ),
            },
        ))
        self.assertIn("hookSpecificOutput", result2)
        self.assertIn(
            "CONFABULATION WARNING",
            result2["hookSpecificOutput"]["additionalContext"],
        )


if __name__ == "__main__":
    unittest.main()
