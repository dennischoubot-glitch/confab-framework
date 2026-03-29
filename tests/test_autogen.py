"""Tests for confab AutoGen integration.

Uses mock autogen_core classes since autogen is an optional dependency.
Tests verify that ConfabInterventionHandler correctly extracts text,
runs verification, and handles all four on_fail modes (warn, raise, log, drop).
"""

import asyncio
import os
import tempfile
import unittest
import warnings
from unittest.mock import MagicMock
import sys


# ---------------------------------------------------------------------------
# Mock autogen_core module before importing the integration
# ---------------------------------------------------------------------------

mock_autogen_core = MagicMock()


class MockAgentId:
    """Simulates autogen_core.AgentId."""

    def __init__(self, type: str = "default", key: str = "default"):
        self.type = type
        self.key = key

    def __str__(self) -> str:
        return f"{self.type}/{self.key}"


class _DropMessageSentinel:
    """Simulates autogen_core.DropMessage sentinel."""
    pass


DropMessage = _DropMessageSentinel


class MockDefaultInterventionHandler:
    """Simulates autogen_core.DefaultInterventionHandler."""

    async def on_send(self, message, *, message_context, recipient):
        return message

    async def on_publish(self, message, *, message_context):
        return message

    async def on_response(self, message, *, sender, recipient):
        return message


mock_autogen_core.AgentId = MockAgentId
mock_autogen_core.DropMessage = DropMessage
mock_autogen_core.DefaultInterventionHandler = MockDefaultInterventionHandler

sys.modules["autogen_core"] = mock_autogen_core

from confab.integrations.autogen import ConfabInterventionHandler
from confab.middleware import ConfabVerificationError, VerificationReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_async(coro):
    """Run an async coroutine synchronously for testing."""
    return asyncio.run(coro)


def make_sender(name: str = "assistant") -> MockAgentId:
    return MockAgentId(type=name, key="default")


# ---------------------------------------------------------------------------
# Mock message types
# ---------------------------------------------------------------------------

class MockTextMessage:
    """Simulates autogen_agentchat TextMessage (has .content)."""

    def __init__(self, content: str = "", source: str = "assistant"):
        self.content = content
        self.source = source


class MockModelTextMessage:
    """Simulates a message with to_model_text() method."""

    def __init__(self, text: str = ""):
        self._text = text

    def to_model_text(self) -> str:
        return self._text


class MockTextAttrMessage:
    """Simulates a message with .text attribute."""

    def __init__(self, text: str = ""):
        self.text = text


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestConfabInterventionHandlerInit(unittest.TestCase):
    """Test ConfabInterventionHandler initialization and configuration."""

    def test_default_init(self):
        h = ConfabInterventionHandler()
        self.assertTrue(h.check_files)
        self.assertTrue(h.check_env)
        self.assertFalse(h.check_counts)
        self.assertEqual(h.on_fail, "warn")
        self.assertEqual(h.reports, [])

    def test_custom_init(self):
        h = ConfabInterventionHandler(
            check_files=False,
            check_env=False,
            check_counts=True,
            on_fail="log",
        )
        self.assertFalse(h.check_files)
        self.assertFalse(h.check_env)
        self.assertTrue(h.check_counts)
        self.assertEqual(h.on_fail, "log")

    def test_drop_mode_accepted(self):
        """AutoGen supports 'drop' mode (unlike LangChain/CrewAI)."""
        h = ConfabInterventionHandler(on_fail="drop")
        self.assertEqual(h.on_fail, "drop")

    def test_invalid_on_fail_raises(self):
        with self.assertRaises(ValueError):
            ConfabInterventionHandler(on_fail="explode")

    def test_last_report_initially_none(self):
        h = ConfabInterventionHandler()
        self.assertIsNone(h.last_report)


class TestConfabInterventionHandlerOnResponse(unittest.TestCase):
    """Test the on_response async handler with mock messages."""

    def test_clean_text_message(self):
        """TextMessage with no verifiable claims should produce clean report."""
        h = ConfabInterventionHandler()
        msg = MockTextMessage(content="The system is healthy. All good.")
        result = run_async(
            h.on_response(msg, sender=make_sender(), recipient=None)
        )
        self.assertIs(result, msg)  # pass-through
        self.assertEqual(len(h.reports), 1)
        self.assertTrue(h.last_report.clean)

    def test_existing_file_passes(self):
        """Claims about files that exist should pass."""
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"# test")
            tmp = f.name
        try:
            h = ConfabInterventionHandler()
            msg = MockTextMessage(content=f"The script at {tmp} is ready.")
            run_async(h.on_response(msg, sender=make_sender(), recipient=None))
            self.assertEqual(h.last_report.failed, 0)
        finally:
            os.unlink(tmp)

    def test_nonexistent_file_fails(self):
        """Claims about missing files should fail."""
        h = ConfabInterventionHandler(on_fail="log")
        msg = MockTextMessage(
            content="Config at /tmp/confab_autogen_test_missing_xyz.json is ready."
        )
        run_async(h.on_response(msg, sender=make_sender(), recipient=None))
        self.assertGreater(h.last_report.failed, 0)
        self.assertFalse(h.last_report.clean)

    def test_empty_content_skipped(self):
        """Empty message content should not produce a report."""
        h = ConfabInterventionHandler()
        msg = MockTextMessage(content="")
        run_async(h.on_response(msg, sender=make_sender(), recipient=None))
        self.assertEqual(len(h.reports), 0)

    def test_whitespace_content_skipped(self):
        """Whitespace-only content should not produce a report."""
        h = ConfabInterventionHandler()
        msg = MockTextMessage(content="   \n  ")
        run_async(h.on_response(msg, sender=make_sender(), recipient=None))
        self.assertEqual(len(h.reports), 0)

    def test_plain_string_message(self):
        """Plain string messages should work."""
        h = ConfabInterventionHandler()
        result = run_async(
            h.on_response(
                "The system is running fine.",
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertEqual(result, "The system is running fine.")
        self.assertEqual(len(h.reports), 1)

    def test_model_text_message(self):
        """Messages with to_model_text() should have text extracted."""
        h = ConfabInterventionHandler()
        msg = MockModelTextMessage(text="Analysis complete.")
        run_async(h.on_response(msg, sender=make_sender(), recipient=None))
        self.assertEqual(len(h.reports), 1)
        self.assertTrue(h.last_report.clean)

    def test_text_attr_message(self):
        """Messages with .text attribute should have text extracted."""
        h = ConfabInterventionHandler()
        msg = MockTextAttrMessage(text="Report generated.")
        run_async(h.on_response(msg, sender=make_sender(), recipient=None))
        self.assertEqual(len(h.reports), 1)

    def test_multiple_responses_accumulate(self):
        """Multiple on_response calls should accumulate reports."""
        h = ConfabInterventionHandler()
        for i in range(3):
            run_async(
                h.on_response(
                    MockTextMessage(content=f"Output {i}."),
                    sender=make_sender(),
                    recipient=None,
                )
            )
        self.assertEqual(len(h.reports), 3)

    def test_passthrough_on_clean(self):
        """Clean messages should be returned unchanged."""
        h = ConfabInterventionHandler()
        msg = MockTextMessage(content="All systems nominal.")
        result = run_async(
            h.on_response(msg, sender=make_sender(), recipient=None)
        )
        self.assertIs(result, msg)


class TestConfabInterventionHandlerOnFail(unittest.TestCase):
    """Test all four on_fail modes."""

    def _make_failing_msg(self):
        return MockTextMessage(
            content="Database at /tmp/confab_autogen_onfail_test_xyz.db is ready."
        )

    def test_warn_mode_issues_warning(self):
        h = ConfabInterventionHandler(on_fail="warn")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            run_async(
                h.on_response(
                    self._make_failing_msg(),
                    sender=make_sender(),
                    recipient=None,
                )
            )
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertGreater(len(confab_warns), 0)

    def test_raise_mode_raises_error(self):
        h = ConfabInterventionHandler(on_fail="raise")
        with self.assertRaises(ConfabVerificationError) as ctx:
            run_async(
                h.on_response(
                    self._make_failing_msg(),
                    sender=make_sender(),
                    recipient=None,
                )
            )
        self.assertGreater(len(ctx.exception.failures), 0)

    def test_log_mode_no_raise_no_warn(self):
        h = ConfabInterventionHandler(on_fail="log")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            run_async(
                h.on_response(
                    self._make_failing_msg(),
                    sender=make_sender(),
                    recipient=None,
                )
            )
            confab_warns = [x for x in w if "confab" in str(x.message).lower()]
            self.assertEqual(len(confab_warns), 0)
        self.assertFalse(h.last_report.clean)

    def test_drop_mode_returns_drop_message(self):
        """Drop mode should return DropMessage sentinel on failure."""
        h = ConfabInterventionHandler(on_fail="drop")
        result = run_async(
            h.on_response(
                self._make_failing_msg(),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertIs(result, DropMessage)

    def test_drop_mode_passthrough_on_clean(self):
        """Drop mode should pass through clean messages normally."""
        h = ConfabInterventionHandler(on_fail="drop")
        msg = MockTextMessage(content="All systems nominal.")
        result = run_async(
            h.on_response(msg, sender=make_sender(), recipient=None)
        )
        self.assertIs(result, msg)


class TestConfabInterventionHandlerConvenience(unittest.TestCase):
    """Test convenience properties and methods."""

    def test_clear(self):
        h = ConfabInterventionHandler()
        run_async(
            h.on_response(
                MockTextMessage(content="Output one."),
                sender=make_sender(),
                recipient=None,
            )
        )
        run_async(
            h.on_response(
                MockTextMessage(content="Output two."),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertEqual(len(h.reports), 2)
        h.clear()
        self.assertEqual(len(h.reports), 0)
        self.assertIsNone(h.last_report)

    def test_total_claims(self):
        h = ConfabInterventionHandler()
        run_async(
            h.on_response(
                MockTextMessage(content="No claims here."),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertEqual(h.total_claims, 0)

    def test_total_failures_with_mixed(self):
        h = ConfabInterventionHandler(on_fail="log")
        # Clean
        run_async(
            h.on_response(
                MockTextMessage(content="All good."),
                sender=make_sender(),
                recipient=None,
            )
        )
        # Failing
        run_async(
            h.on_response(
                MockTextMessage(
                    content="Config at /tmp/confab_autogen_failures_xyz.toml is ready."
                ),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertGreater(h.total_failures, 0)

    def test_clean_property_all_clean(self):
        h = ConfabInterventionHandler()
        run_async(
            h.on_response(
                MockTextMessage(content="Clean output."),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertTrue(h.clean)

    def test_clean_property_with_failure(self):
        h = ConfabInterventionHandler(on_fail="log")
        run_async(
            h.on_response(
                MockTextMessage(
                    content="Script at /tmp/confab_autogen_clean_xyz.py is ready."
                ),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertFalse(h.clean)

    def test_summary_no_reports(self):
        h = ConfabInterventionHandler()
        self.assertIn("no verification runs", h.summary())

    def test_summary_all_clean(self):
        h = ConfabInterventionHandler()
        run_async(
            h.on_response(
                MockTextMessage(content="Clean output."),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertIn("CLEAN", h.summary())

    def test_summary_with_failures(self):
        h = ConfabInterventionHandler(on_fail="log")
        run_async(
            h.on_response(
                MockTextMessage(
                    content="Config at /tmp/confab_autogen_summary_xyz.json is deployed."
                ),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertIn("FAILED", h.summary())


class TestExtractText(unittest.TestCase):
    """Test _extract_text handles various input types."""

    def test_text_message_content(self):
        text = ConfabInterventionHandler._extract_text(
            MockTextMessage(content="hello world")
        )
        self.assertEqual(text, "hello world")

    def test_plain_string(self):
        text = ConfabInterventionHandler._extract_text("direct string")
        self.assertEqual(text, "direct string")

    def test_model_text_method(self):
        text = ConfabInterventionHandler._extract_text(
            MockModelTextMessage(text="from model text")
        )
        self.assertEqual(text, "from model text")

    def test_text_attr(self):
        text = ConfabInterventionHandler._extract_text(
            MockTextAttrMessage(text="from text attr")
        )
        self.assertEqual(text, "from text attr")

    def test_empty_returns_empty(self):
        """Non-text objects return empty string."""

        class NoTextObj:
            pass

        text = ConfabInterventionHandler._extract_text(NoTextObj())
        self.assertEqual(text, "")

    def test_numeric_returns_empty(self):
        """Numeric values return empty string (not str(42))."""
        text = ConfabInterventionHandler._extract_text(42)
        self.assertEqual(text, "")

    def test_content_preferred_over_text(self):
        """When both .content and .text exist, .content wins."""

        class BothAttrs:
            content = "from content"
            text = "from text"

        text = ConfabInterventionHandler._extract_text(BothAttrs())
        self.assertEqual(text, "from content")


class TestRealisticAutoGenScenarios(unittest.TestCase):
    """Test with text resembling real AutoGen agent output."""

    def test_multi_agent_conversation_mixed(self):
        """Simulate runtime intercepting multiple agent responses."""
        h = ConfabInterventionHandler(on_fail="log")

        # Agent 1: research (clean)
        run_async(
            h.on_response(
                MockTextMessage(content="The market analysis shows strong fundamentals."),
                sender=MockAgentId(type="researcher"),
                recipient=MockAgentId(type="coordinator"),
            )
        )

        # Agent 2: file operation (failing)
        run_async(
            h.on_response(
                MockTextMessage(
                    content="Config at /tmp/confab_autogen_report_xyz.json is ready."
                ),
                sender=MockAgentId(type="writer"),
                recipient=MockAgentId(type="coordinator"),
            )
        )

        # Agent 3: summary (clean)
        run_async(
            h.on_response(
                MockTextMessage(content="Summary: research complete."),
                sender=MockAgentId(type="summarizer"),
                recipient=MockAgentId(type="coordinator"),
            )
        )

        self.assertEqual(len(h.reports), 3)
        self.assertFalse(h.clean)
        self.assertIn("FAILED", h.summary())

    def test_real_file_in_response(self):
        """Response referencing a real file should pass."""
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            f.write(b"col1,col2\n1,2\n")
            tmp = f.name
        try:
            h = ConfabInterventionHandler(on_fail="raise")
            run_async(
                h.on_response(
                    MockTextMessage(content=f"Data exported to {tmp} successfully."),
                    sender=make_sender(),
                    recipient=None,
                )
            )
            self.assertTrue(h.last_report.clean)
        finally:
            os.unlink(tmp)

    def test_broadcast_response_no_recipient(self):
        """Broadcast responses (recipient=None) should still be verified."""
        h = ConfabInterventionHandler()
        run_async(
            h.on_response(
                MockTextMessage(content="Broadcast update."),
                sender=make_sender(),
                recipient=None,
            )
        )
        self.assertEqual(len(h.reports), 1)


if __name__ == "__main__":
    unittest.main()
