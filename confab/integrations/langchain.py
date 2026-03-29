"""LangChain callback handler for confabulation detection.

Automatically extracts and verifies claims from LLM and agent outputs
using confab-framework's claim extraction and verification pipeline.

Usage::

    from confab.integrations.langchain import ConfabCallbackHandler

    handler = ConfabCallbackHandler(on_fail="warn")

    # With a chain or agent:
    chain.invoke({"input": "..."}, config={"callbacks": [handler]})

    # Check results:
    for report in handler.reports:
        if not report.clean:
            print(report.summary())

Requires ``langchain-core`` to be installed::

    pip install confab-framework[langchain]
"""

from __future__ import annotations

import logging
import warnings
from typing import Any, Dict, List, Optional

try:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.outputs import LLMResult
    from langchain_core.agents import AgentFinish
except ImportError:
    raise ImportError(
        "langchain-core is required for the LangChain integration. "
        "Install it with: pip install confab-framework[langchain]"
    )

from confab.middleware import (
    VerificationReport,
    ConfabVerificationError,
    verify_text,
)

logger = logging.getLogger("confab.integrations.langchain")


class ConfabCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that detects confabulated claims.

    Hooks into LLM completions and agent finish events. When text output
    is produced, it extracts verifiable claims (file paths, env vars,
    pipeline statuses, counts) and checks them against reality.

    Args:
        check_files: Verify file existence claims. Default True.
        check_env: Verify environment variable claims. Default True.
        check_counts: Verify count/quantity claims. Default False.
        on_fail: Action when verification fails:
            - ``"warn"``: Issue a Python warning (default).
            - ``"raise"``: Raise :class:`ConfabVerificationError`.
            - ``"log"``: Log at WARNING level.
        verify_llm: Run verification on raw LLM outputs. Default False.
            Set to True if you want to catch claims at the LLM level,
            not just the final agent/chain output.
        verify_agent: Run verification on agent finish. Default True.
        verify_chain: Run verification on chain outputs. Default True.

    Attributes:
        reports: List of all :class:`VerificationReport` objects generated.
        last_report: The most recent report, or None.

    Example::

        from langchain_openai import ChatOpenAI
        from langchain.agents import AgentExecutor, create_tool_calling_agent
        from confab.integrations.langchain import ConfabCallbackHandler

        handler = ConfabCallbackHandler(on_fail="warn")
        agent_executor.invoke(
            {"input": "Check if /tmp/data.csv exists"},
            config={"callbacks": [handler]},
        )

        if handler.last_report and not handler.last_report.clean:
            print(handler.last_report.summary())
    """

    def __init__(
        self,
        *,
        check_files: bool = True,
        check_env: bool = True,
        check_counts: bool = False,
        on_fail: str = "warn",
        verify_llm: bool = False,
        verify_agent: bool = True,
        verify_chain: bool = True,
    ) -> None:
        super().__init__()
        valid = {"warn", "raise", "log"}
        if on_fail not in valid:
            raise ValueError(f"on_fail must be one of {valid!r}, got {on_fail!r}")

        self.check_files = check_files
        self.check_env = check_env
        self.check_counts = check_counts
        self.on_fail = on_fail
        self.verify_llm = verify_llm
        self.verify_agent = verify_agent
        self.verify_chain = verify_chain
        self.reports: List[VerificationReport] = []

    @property
    def last_report(self) -> Optional[VerificationReport]:
        """The most recent verification report, or None."""
        return self.reports[-1] if self.reports else None

    def _verify_text(self, text: str, source: str) -> VerificationReport:
        """Run claim extraction and verification on text.

        Args:
            text: The text to verify.
            source: Label for log messages (e.g. "llm_end", "agent_finish").

        Returns:
            A VerificationReport with results.
        """
        report = verify_text(
            text,
            check_files=self.check_files,
            check_env=self.check_env,
            check_counts=self.check_counts,
        )
        self.reports.append(report)

        if not report.clean:
            msg = f"[confab/{source}] {report.summary()}"
            if self.on_fail == "raise":
                raise ConfabVerificationError(msg, report.failures)
            elif self.on_fail == "warn":
                # stacklevel=4: caller → on_*_end → _verify_text → warnings.warn
                warnings.warn(msg, stacklevel=4)
            elif self.on_fail == "log":
                logger.warning(msg)

        return report

    # -- LangChain callback hooks --

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Verify claims in LLM output text.

        Only runs if ``verify_llm=True`` (default False), since most users
        want verification at the agent/chain level, not per-LLM-call.
        """
        if not self.verify_llm:
            return

        for generation_list in response.generations:
            for generation in generation_list:
                text = generation.text
                if text and text.strip():
                    self._verify_text(text, "llm_end")

    def on_agent_finish(self, finish: AgentFinish, **kwargs: Any) -> None:
        """Verify claims in the agent's final answer."""
        if not self.verify_agent:
            return

        # AgentFinish.return_values is a dict, typically {"output": "..."}
        for value in finish.return_values.values():
            if isinstance(value, str) and value.strip():
                self._verify_text(value, "agent_finish")

    def on_chain_end(self, outputs: Dict[str, Any], **kwargs: Any) -> None:
        """Verify claims in chain outputs."""
        if not self.verify_chain:
            return

        for value in outputs.values():
            if isinstance(value, str) and value.strip():
                self._verify_text(value, "chain_end")

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        """No-op. Tool outputs are not verified by default.

        Tool outputs come from real execution, not from LLM generation,
        so confabulation detection is less relevant here.
        """

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
