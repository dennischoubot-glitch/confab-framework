"""CrewAI integration for confabulation detection.

Provides task and crew-level callbacks that automatically verify claims
in agent outputs using confab-framework's extraction and verification
pipeline.

Usage::

    from confab.integrations.crewai import ConfabTaskCallback

    cb = ConfabTaskCallback(on_fail="warn")

    # Per-task callback:
    task = Task(
        description="...",
        agent=my_agent,
        callback=cb,
    )

    # Or crew-level (verifies all task outputs):
    crew = Crew(
        agents=[...],
        tasks=[...],
        task_callback=cb,
    )

    # After execution, check results:
    for report in cb.reports:
        if not report.clean:
            print(report.summary())

Requires ``crewai`` to be installed::

    pip install confab-framework[crewai]
"""

from __future__ import annotations

import logging
import warnings
from typing import Any, List, Optional

try:
    from crewai.tasks.task_output import TaskOutput
except ImportError:
    raise ImportError(
        "crewai is required for the CrewAI integration. "
        "Install it with: pip install confab-framework[crewai]"
    )

from confab.middleware import (
    VerificationReport,
    ConfabVerificationError,
    verify_text,
)

logger = logging.getLogger("confab.integrations.crewai")


class ConfabTaskCallback:
    """CrewAI task callback that detects confabulated claims.

    Designed to be passed as either a per-task ``callback`` or as the
    crew-level ``task_callback``. When a task completes, its raw output
    text is scanned for verifiable claims (file paths, env vars,
    pipeline statuses, counts) and checked against reality.

    Args:
        check_files: Verify file existence claims. Default True.
        check_env: Verify environment variable claims. Default True.
        check_counts: Verify count/quantity claims. Default False.
        on_fail: Action when verification fails:
            - ``"warn"``: Issue a Python warning (default).
            - ``"raise"``: Raise :class:`ConfabVerificationError`.
            - ``"log"``: Log at WARNING level.

    Attributes:
        reports: List of all :class:`VerificationReport` objects generated.
        last_report: The most recent report, or None.

    Example::

        from crewai import Agent, Task, Crew
        from confab.integrations.crewai import ConfabTaskCallback

        cb = ConfabTaskCallback(on_fail="warn")

        agent = Agent(role="analyst", goal="...", backstory="...")
        task = Task(
            description="Check system status",
            agent=agent,
            callback=cb,
        )
        crew = Crew(agents=[agent], tasks=[task])
        crew.kickoff()

        if cb.last_report and not cb.last_report.clean:
            print(cb.last_report.summary())
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

    def __call__(self, output: Any) -> None:
        """Called by CrewAI when a task completes.

        Args:
            output: A :class:`TaskOutput` instance from CrewAI.
        """
        text = self._extract_text(output)
        if not text or not text.strip():
            return

        report = verify_text(
            text,
            check_files=self.check_files,
            check_env=self.check_env,
            check_counts=self.check_counts,
        )
        self.reports.append(report)

        if not report.clean:
            desc = getattr(output, "description", "task")
            msg = f"[confab/crewai/{desc[:40]}] {report.summary()}"
            if self.on_fail == "raise":
                raise ConfabVerificationError(msg, report.failures)
            elif self.on_fail == "warn":
                warnings.warn(msg, stacklevel=2)
            elif self.on_fail == "log":
                logger.warning(msg)

    @staticmethod
    def _extract_text(output: Any) -> str:
        """Extract text content from a TaskOutput or compatible object.

        Handles TaskOutput objects (via .raw attribute), plain strings,
        and objects with a .text attribute.
        """
        if isinstance(output, str):
            return output
        if hasattr(output, "raw") and isinstance(output.raw, str):
            return output.raw
        if hasattr(output, "text") and isinstance(output.text, str):
            return output.text
        return str(output)

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
