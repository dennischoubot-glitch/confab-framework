"""Claude Agent SDK integration for confabulation detection.

Provides a PostToolUse hook callback and a message verifier for detecting
confabulated claims in agent tool outputs and responses using
confab-framework's extraction and verification pipeline.

Usage with hooks::

    from confab.integrations.agent_sdk import ConfabPostToolUseHook
    from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher

    hook = ConfabPostToolUseHook(on_fail="warn")

    options = ClaudeAgentOptions(
        hooks={
            "PostToolUse": [HookMatcher(hooks=[hook])],
        },
    )

    async for message in query(prompt="...", options=options):
        print(message)

    # After execution:
    if not hook.clean:
        print(hook.summary())

Usage with message verification::

    from confab.integrations.agent_sdk import ConfabMessageVerifier

    verifier = ConfabMessageVerifier(on_fail="warn")

    async for message in query(prompt="...", options=options):
        verifier.verify(message)

    if not verifier.clean:
        print(verifier.summary())

Requires ``claude-agent-sdk`` to be installed::

    pip install confab-framework[agent-sdk]
"""

from __future__ import annotations

import logging
import warnings
from typing import Any, Dict, List, Optional

try:
    import claude_agent_sdk  # noqa: F401
except ImportError:
    raise ImportError(
        "claude-agent-sdk is required for the Agent SDK integration. "
        "Install it with: pip install confab-framework[agent-sdk]"
    )

from confab.middleware import (
    VerificationReport,
    ConfabVerificationError,
    verify_text,
)

logger = logging.getLogger("confab.integrations.agent_sdk")


class ConfabPostToolUseHook:
    """Agent SDK PostToolUse hook that detects confabulated claims.

    An async callable for use with the Claude Agent SDK's hooks system.
    When a tool produces output, this hook extracts verifiable claims
    (file paths, env vars, pipeline statuses, counts) and checks them
    against reality.

    Args:
        check_files: Verify file existence claims. Default True.
        check_env: Verify environment variable claims. Default True.
        check_counts: Verify count/quantity claims. Default False.
        on_fail: Action when verification fails:
            - ``"warn"``: Issue a Python warning (default).
            - ``"raise"``: Raise :class:`ConfabVerificationError`.
            - ``"log"``: Log at WARNING level.
            - ``"inject"``: Inject confabulation warning into agent
              context via ``additionalContext`` in hook output. The
              agent sees the warning and can self-correct.

    Attributes:
        reports: List of all :class:`VerificationReport` objects generated.
        last_report: The most recent report, or None.

    Example::

        from claude_agent_sdk import query, ClaudeAgentOptions, HookMatcher
        from confab.integrations.agent_sdk import ConfabPostToolUseHook

        hook = ConfabPostToolUseHook(on_fail="inject")
        options = ClaudeAgentOptions(
            hooks={
                "PostToolUse": [HookMatcher(hooks=[hook])],
            },
        )

        async for message in query(prompt="...", options=options):
            pass

        if not hook.clean:
            print(hook.summary())
    """

    def __init__(
        self,
        *,
        check_files: bool = True,
        check_env: bool = True,
        check_counts: bool = False,
        on_fail: str = "warn",
    ) -> None:
        valid = {"warn", "raise", "log", "inject"}
        if on_fail not in valid:
            raise ValueError(f"on_fail must be one of {valid!r}, got {on_fail!r}")

        self.check_files = check_files
        self.check_env = check_env
        self.check_counts = check_counts
        self.on_fail = on_fail
        self.reports: List[VerificationReport] = []

    @property
    def last_report(self) -> Optional[VerificationReport]:
        """The most recent verification report, or None."""
        return self.reports[-1] if self.reports else None

    @staticmethod
    def _extract_text(input_data: Any) -> str:
        """Extract text from a PostToolUse hook input.

        The hook input contains ``tool_response`` which may be a string,
        a dict with text content, or a list of content blocks.
        """
        if isinstance(input_data, dict):
            response = input_data.get("tool_response", "")
        elif hasattr(input_data, "tool_response"):
            response = input_data.tool_response
        else:
            return ""

        if isinstance(response, str):
            return response
        if isinstance(response, dict):
            return response.get("text", response.get("content", ""))
        if isinstance(response, list):
            parts = []
            for block in response:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    parts.append(block.get("text", ""))
                elif hasattr(block, "text"):
                    parts.append(block.text)
            return "\n".join(parts)
        return str(response) if response else ""

    def _verify(self, text: str, source: str) -> Optional[VerificationReport]:
        """Run verification on text and handle the result.

        Args:
            text: The text to verify.
            source: Label for log/warning messages.

        Returns:
            A VerificationReport, or None if text was empty.
        """
        if not text or not text.strip():
            return None

        report = verify_text(
            text,
            check_files=self.check_files,
            check_env=self.check_env,
            check_counts=self.check_counts,
        )
        self.reports.append(report)

        if not report.clean:
            msg = f"[confab/agent-sdk/{source}] {report.summary()}"
            if self.on_fail == "raise":
                raise ConfabVerificationError(msg, report.failures)
            elif self.on_fail == "warn":
                warnings.warn(msg, stacklevel=3)
            elif self.on_fail == "log":
                logger.warning(msg)
            # "inject" is handled by __call__ (returns additionalContext)

        return report

    async def __call__(
        self,
        input_data: Any,
        tool_use_id: Optional[str] = None,
        context: Any = None,
    ) -> Dict[str, Any]:
        """Hook callback invoked by Agent SDK after tool execution.

        Args:
            input_data: PostToolUseHookInput with tool_response.
            tool_use_id: Correlates with the PreToolUse event.
            context: Reserved for future use.

        Returns:
            Hook output dict. If ``on_fail="inject"`` and verification
            fails, includes ``additionalContext`` with the warning.
        """
        text = self._extract_text(input_data)

        tool_name = ""
        if isinstance(input_data, dict):
            tool_name = input_data.get("tool_name", "unknown")
        elif hasattr(input_data, "tool_name"):
            tool_name = input_data.tool_name

        report = self._verify(text, f"tool/{tool_name}")
        if not report:
            return {}

        if not report.clean and self.on_fail == "inject":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": (
                        f"CONFABULATION WARNING: {report.summary()}"
                    ),
                }
            }

        return {}

    # -- Convenience methods --

    def clear(self) -> None:
        """Clear all stored reports."""
        self.reports.clear()

    @property
    def total_claims(self) -> int:
        """Total claims found across all reports."""
        return sum(r.claims_found for r in self.reports)

    @property
    def total_failures(self) -> int:
        """Total failed verifications across all reports."""
        return sum(r.failed for r in self.reports)

    @property
    def clean(self) -> bool:
        """True if no verification failures in any report."""
        return all(r.clean for r in self.reports)

    def summary(self) -> str:
        """Summary of all verification activity."""
        if not self.reports:
            return "confab: no verification runs yet"
        total_c = self.total_claims
        total_f = self.total_failures
        if total_f == 0:
            return (
                f"confab: CLEAN across {len(self.reports)} checks "
                f"({total_c} claims verified)"
            )
        return (
            f"confab: {total_f} FAILED across {len(self.reports)} checks "
            f"({total_c} total claims)"
        )


class ConfabMessageVerifier:
    """Verifies claims in Agent SDK messages during iteration.

    Use this to check AssistantMessage and ResultMessage content for
    confabulated claims as you iterate over ``query()`` output.

    Args:
        check_files: Verify file existence claims. Default True.
        check_env: Verify environment variable claims. Default True.
        check_counts: Verify count/quantity claims. Default False.
        on_fail: Action when verification fails:
            - ``"warn"``: Issue a Python warning (default).
            - ``"raise"``: Raise :class:`ConfabVerificationError`.
            - ``"log"``: Log at WARNING level.

    Example::

        from confab.integrations.agent_sdk import ConfabMessageVerifier

        verifier = ConfabMessageVerifier(on_fail="warn")

        async for message in query(prompt="Check system", options=options):
            verifier.verify(message)

        if not verifier.clean:
            print(verifier.summary())
    """

    def __init__(
        self,
        *,
        check_files: bool = True,
        check_env: bool = True,
        check_counts: bool = False,
        on_fail: str = "warn",
    ) -> None:
        valid = {"warn", "raise", "log"}
        if on_fail not in valid:
            raise ValueError(f"on_fail must be one of {valid!r}, got {on_fail!r}")

        self.check_files = check_files
        self.check_env = check_env
        self.check_counts = check_counts
        self.on_fail = on_fail
        self.reports: List[VerificationReport] = []

    @property
    def last_report(self) -> Optional[VerificationReport]:
        """The most recent verification report, or None."""
        return self.reports[-1] if self.reports else None

    @staticmethod
    def _extract_text(message: Any) -> str:
        """Extract text content from an Agent SDK message.

        Handles:
        - AssistantMessage: iterates .content for TextBlock instances
        - ResultMessage: uses .result string
        - Plain strings
        - Objects with .text or .content attributes
        """
        if isinstance(message, str):
            return message

        # ResultMessage: has .result (final output string)
        if hasattr(message, "result") and isinstance(
            getattr(message, "result", None), str
        ):
            return message.result

        # AssistantMessage: has .content (list of blocks)
        if hasattr(message, "content") and isinstance(message.content, list):
            parts = []
            for block in message.content:
                if hasattr(block, "text") and isinstance(block.text, str):
                    parts.append(block.text)
            return "\n".join(parts)

        # Fallback: .text attribute
        if hasattr(message, "text") and isinstance(message.text, str):
            return message.text

        return ""

    def verify(self, message: Any) -> Optional[VerificationReport]:
        """Verify claims in a single message.

        Args:
            message: An Agent SDK message (AssistantMessage, ResultMessage,
                or any object with text content).

        Returns:
            A VerificationReport if text was found, None otherwise.
        """
        text = self._extract_text(message)
        if not text or not text.strip():
            return None

        msg_type = type(message).__name__

        report = verify_text(
            text,
            check_files=self.check_files,
            check_env=self.check_env,
            check_counts=self.check_counts,
        )
        self.reports.append(report)

        if not report.clean:
            msg = f"[confab/agent-sdk/{msg_type}] {report.summary()}"
            if self.on_fail == "raise":
                raise ConfabVerificationError(msg, report.failures)
            elif self.on_fail == "warn":
                warnings.warn(msg, stacklevel=2)
            elif self.on_fail == "log":
                logger.warning(msg)

        return report

    # -- Convenience methods --

    def clear(self) -> None:
        """Clear all stored reports."""
        self.reports.clear()

    @property
    def total_claims(self) -> int:
        """Total claims found across all reports."""
        return sum(r.claims_found for r in self.reports)

    @property
    def total_failures(self) -> int:
        """Total failed verifications across all reports."""
        return sum(r.failed for r in self.reports)

    @property
    def clean(self) -> bool:
        """True if no verification failures in any report."""
        return all(r.clean for r in self.reports)

    def summary(self) -> str:
        """Summary of all verification activity."""
        if not self.reports:
            return "confab: no verification runs yet"
        total_c = self.total_claims
        total_f = self.total_failures
        if total_f == 0:
            return (
                f"confab: CLEAN across {len(self.reports)} checks "
                f"({total_c} claims verified)"
            )
        return (
            f"confab: {total_f} FAILED across {len(self.reports)} checks "
            f"({total_c} total claims)"
        )
