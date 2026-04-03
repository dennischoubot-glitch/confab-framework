"""OpenAI Agents SDK integration for confabulation detection.

Provides an output guardrail and a run verifier for detecting confabulated
claims in agent outputs using confab-framework's extraction and verification
pipeline.

Usage with output guardrail::

    from confab.integrations.openai_agents import ConfabOutputGuardrail

    guardrail = ConfabOutputGuardrail(on_fail="tripwire")

    from agents import Agent

    agent = Agent(
        name="my_agent",
        instructions="...",
        output_guardrails=[guardrail],
    )

    # After execution:
    if not guardrail.clean:
        print(guardrail.summary())

Usage with run verifier::

    from confab.integrations.openai_agents import ConfabRunVerifier
    from agents import Agent, Runner

    verifier = ConfabRunVerifier(on_fail="warn")

    agent = Agent(name="my_agent", instructions="...")
    result = await Runner.run(agent, "Check system status")

    verifier.verify(result)

    if not verifier.clean:
        print(verifier.summary())

Requires ``openai-agents`` to be installed::

    pip install confab-framework[openai-agents]
"""

from __future__ import annotations

import logging
import warnings
from typing import Any, List, Optional

try:
    from agents import (
        Agent,
        GuardrailFunctionOutput,
        OutputGuardrail,
        RunContextWrapper,
    )
except ImportError:
    raise ImportError(
        "openai-agents is required for the OpenAI Agents SDK integration. "
        "Install it with: pip install confab-framework[openai-agents]"
    )

from confab.middleware import (
    VerificationReport,
    ConfabVerificationError,
    verify_text,
)

logger = logging.getLogger("confab.integrations.openai_agents")


class ConfabOutputGuardrail(OutputGuardrail):
    """OpenAI Agents SDK output guardrail that detects confabulated claims.

    When an agent produces output, this guardrail extracts verifiable claims
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
            - ``"tripwire"``: Trigger the guardrail tripwire, which
              raises ``OutputGuardrailTripwireTriggered`` in the
              Agents SDK runtime.

    Attributes:
        reports: List of all :class:`VerificationReport` objects generated.
        last_report: The most recent report, or None.

    Example::

        from agents import Agent, Runner
        from confab.integrations.openai_agents import ConfabOutputGuardrail

        guardrail = ConfabOutputGuardrail(on_fail="tripwire")
        agent = Agent(
            name="analyst",
            instructions="Check system health",
            output_guardrails=[guardrail],
        )

        result = await Runner.run(agent, "Check status")

        if not guardrail.clean:
            print(guardrail.summary())
    """

    def __init__(
        self,
        *,
        check_files: bool = True,
        check_env: bool = True,
        check_counts: bool = False,
        on_fail: str = "warn",
    ) -> None:
        valid = {"warn", "raise", "log", "tripwire"}
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
    def _extract_text(output: Any) -> str:
        """Extract text from an agent output.

        Handles:
        - Plain strings (most common for str-typed agent output)
        - Objects with ``.model_dump()`` (Pydantic models)
        - Objects with ``.text`` or ``.content`` attributes
        """
        if isinstance(output, str):
            return output
        # Pydantic model output (structured output agents)
        if hasattr(output, "model_dump") and callable(output.model_dump):
            try:
                dumped = output.model_dump()
                if isinstance(dumped, dict):
                    parts = []
                    for v in dumped.values():
                        if isinstance(v, str):
                            parts.append(v)
                    return "\n".join(parts)
            except Exception:
                pass
        if hasattr(output, "text") and isinstance(output.text, str):
            return output.text
        if hasattr(output, "content") and isinstance(output.content, str):
            return output.content
        return str(output) if output else ""

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
            msg = f"[confab/openai-agents/{source}] {report.summary()}"
            if self.on_fail == "raise":
                raise ConfabVerificationError(msg, report.failures)
            elif self.on_fail == "warn":
                warnings.warn(msg, stacklevel=3)
            elif self.on_fail == "log":
                logger.warning(msg)
            # "tripwire" is handled by run() (sets tripwire_triggered=True)

        return report

    async def run(
        self,
        context: RunContextWrapper,
        agent: Agent,
        output: Any,
    ) -> GuardrailFunctionOutput:
        """Guardrail callback invoked by the Agents SDK after agent output.

        Args:
            context: The run context wrapper.
            agent: The agent that produced the output.
            output: The agent's output (string or structured).

        Returns:
            GuardrailFunctionOutput with verification info. If
            ``on_fail="tripwire"`` and verification fails, sets
            ``tripwire_triggered=True``.
        """
        text = self._extract_text(output)
        report = self._verify(text, f"output/{agent.name}")

        if not report:
            return GuardrailFunctionOutput(
                output_info={"confab": "skipped", "reason": "empty output"},
                tripwire_triggered=False,
            )

        if not report.clean and self.on_fail == "tripwire":
            return GuardrailFunctionOutput(
                output_info={
                    "confab": "failed",
                    "claims_found": report.claims_found,
                    "failed": report.failed,
                    "summary": report.summary(),
                },
                tripwire_triggered=True,
            )

        return GuardrailFunctionOutput(
            output_info={
                "confab": "clean" if report.clean else "failed",
                "claims_found": report.claims_found,
                "passed": report.passed,
                "failed": report.failed,
            },
            tripwire_triggered=False,
        )

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


class ConfabRunVerifier:
    """Verifies claims in OpenAI Agents SDK run results.

    Use this to check RunResult or RunResultStreaming output for
    confabulated claims after agent execution completes.

    Args:
        check_files: Verify file existence claims. Default True.
        check_env: Verify environment variable claims. Default True.
        check_counts: Verify count/quantity claims. Default False.
        on_fail: Action when verification fails:
            - ``"warn"``: Issue a Python warning (default).
            - ``"raise"``: Raise :class:`ConfabVerificationError`.
            - ``"log"``: Log at WARNING level.

    Example::

        from agents import Agent, Runner
        from confab.integrations.openai_agents import ConfabRunVerifier

        verifier = ConfabRunVerifier(on_fail="warn")

        agent = Agent(name="analyst", instructions="...")
        result = await Runner.run(agent, "Check if /tmp/data.csv exists")

        verifier.verify(result)

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
    def _extract_text(result: Any) -> str:
        """Extract text content from a RunResult or compatible object.

        Handles:
        - RunResult: uses ``.final_output`` (str or structured)
        - Objects with ``.final_output`` attribute
        - Objects with ``.output`` attribute
        - Plain strings
        """
        if isinstance(result, str):
            return result

        # RunResult: .final_output is the primary output
        if hasattr(result, "final_output"):
            output = result.final_output
            if isinstance(output, str):
                return output
            # Structured output (Pydantic model)
            if hasattr(output, "model_dump") and callable(output.model_dump):
                try:
                    dumped = output.model_dump()
                    if isinstance(dumped, dict):
                        parts = []
                        for v in dumped.values():
                            if isinstance(v, str):
                                parts.append(v)
                        return "\n".join(parts)
                except Exception:
                    pass
            if output is not None:
                return str(output)

        # Fallback: .output attribute
        if hasattr(result, "output") and isinstance(
            getattr(result, "output", None), str
        ):
            return result.output

        # Fallback: .text attribute
        if hasattr(result, "text") and isinstance(result.text, str):
            return result.text

        return ""

    def verify(self, result: Any) -> Optional[VerificationReport]:
        """Verify claims in a run result.

        Args:
            result: A RunResult, RunResultStreaming, or any object with
                a final_output attribute.

        Returns:
            A VerificationReport if text was found, None otherwise.
        """
        text = self._extract_text(result)
        if not text or not text.strip():
            return None

        result_type = type(result).__name__

        report = verify_text(
            text,
            check_files=self.check_files,
            check_env=self.check_env,
            check_counts=self.check_counts,
        )
        self.reports.append(report)

        if not report.clean:
            msg = f"[confab/openai-agents/{result_type}] {report.summary()}"
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
