"""AutoGen integration for confabulation detection.

Provides a runtime-level intervention handler that intercepts agent
responses and verifies claims using confab-framework's extraction and
verification pipeline.

Usage::

    from confab.integrations.autogen import ConfabInterventionHandler

    handler = ConfabInterventionHandler(on_fail="warn")

    # Add to runtime:
    from autogen_core import SingleThreadedAgentRuntime

    runtime = SingleThreadedAgentRuntime(
        intervention_handlers=[handler],
    )

    # After execution, check results:
    for report in handler.reports:
        if not report.clean:
            print(report.summary())

Requires ``autogen-agentchat`` to be installed::

    pip install confab-framework[autogen]
"""

from __future__ import annotations

import logging
import warnings
from typing import Any, List, Optional

try:
    from autogen_core import DefaultInterventionHandler, AgentId, DropMessage
except ImportError:
    raise ImportError(
        "autogen-core is required for the AutoGen integration. "
        "Install it with: pip install confab-framework[autogen]"
    )

from confab.middleware import (
    VerificationReport,
    ConfabVerificationError,
    verify_text,
)

logger = logging.getLogger("confab.integrations.autogen")


class ConfabInterventionHandler(DefaultInterventionHandler):
    """AutoGen intervention handler that detects confabulated claims.

    Intercepts agent responses at the runtime level. When an agent
    produces a text response, it extracts verifiable claims (file paths,
    env vars, pipeline statuses, counts) and checks them against reality.

    This handler is designed for AutoGen v0.4+ (``autogen-agentchat`` and
    ``autogen-core`` packages). The older ``pyautogen`` 0.2.x API
    (``ConversableAgent.register_reply``) is not supported.

    Args:
        check_files: Verify file existence claims. Default True.
        check_env: Verify environment variable claims. Default True.
        check_counts: Verify count/quantity claims. Default False.
        on_fail: Action when verification fails:
            - ``"warn"``: Issue a Python warning (default).
            - ``"raise"``: Raise :class:`ConfabVerificationError`.
            - ``"log"``: Log at WARNING level.
            - ``"drop"``: Drop the message via AutoGen's DropMessage.

    Attributes:
        reports: List of all :class:`VerificationReport` objects generated.
        last_report: The most recent report, or None.

    Example::

        from autogen_core import SingleThreadedAgentRuntime
        from confab.integrations.autogen import ConfabInterventionHandler

        handler = ConfabInterventionHandler(on_fail="warn")
        runtime = SingleThreadedAgentRuntime(
            intervention_handlers=[handler],
        )

        # ... register agents and run ...

        if not handler.clean:
            print(handler.summary())
    """

    def __init__(
        self,
        *,
        check_files: bool = True,
        check_env: bool = True,
        check_counts: bool = False,
        on_fail: str = "warn",
    ) -> None:
        valid = {"warn", "raise", "log", "drop"}
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
        """Extract text content from an AutoGen message object.

        Handles multiple message types:
        - Plain strings
        - Objects with ``.content`` (TextMessage, StopMessage, etc.)
        - Objects with ``.to_model_text()`` (ChatMessage protocol)
        - Objects with ``.text``
        """
        if isinstance(message, str):
            return message
        # autogen_agentchat message types have .content
        if hasattr(message, "content") and isinstance(message.content, str):
            return message.content
        # ChatMessage protocol
        if hasattr(message, "to_model_text") and callable(message.to_model_text):
            try:
                result = message.to_model_text()
                if isinstance(result, str):
                    return result
            except Exception:
                pass
        # Fallback: .text attribute
        if hasattr(message, "text") and isinstance(message.text, str):
            return message.text
        return ""

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
            msg = f"[confab/autogen/{source}] {report.summary()}"
            if self.on_fail == "raise":
                raise ConfabVerificationError(msg, report.failures)
            elif self.on_fail == "warn":
                warnings.warn(msg, stacklevel=3)
            elif self.on_fail == "log":
                logger.warning(msg)
            # "drop" is handled by the caller (on_response)

        return report

    async def on_response(
        self,
        message: Any,
        *,
        sender: AgentId,
        recipient: AgentId | None,
    ) -> Any:
        """Intercept agent responses for claim verification.

        Called by the AutoGen runtime when an agent produces a response.
        Extracts text, runs verification, and either passes through or
        drops the message based on the ``on_fail`` setting.

        Args:
            message: The response message object.
            sender: The agent that produced the response.
            recipient: The intended recipient (may be None for broadcasts).

        Returns:
            The original message (pass-through) or ``DropMessage`` if
            ``on_fail="drop"`` and verification failed.
        """
        text = self._extract_text(message)
        report = self._verify(text, f"response/{sender}")

        if report and not report.clean and self.on_fail == "drop":
            return DropMessage

        return message

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
