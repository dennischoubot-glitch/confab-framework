"""Decorator middleware for adding confabulation verification to agent functions.

Usage::

    from confab import confab_gate

    @confab_gate(check_files=True, check_env=True, on_fail="warn")
    def my_agent_function(prompt):
        return "The config at /path/to/config.yaml has been updated..."

The decorator intercepts the function's return value, extracts claims
(file paths, env vars, counts) from the text output, verifies them against
reality, and either raises, warns, or logs based on on_fail.

This is the simplest possible API for the most common use case: gating
agent output with a single decorator.
"""

import functools
import logging
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, TypeVar, Union

from .claims import Claim, ClaimType, extract_claims
from .verify import (
    VerificationOutcome,
    VerificationResult,
    verify_claim,
)

logger = logging.getLogger("confab.middleware")

F = TypeVar("F", bound=Callable[..., Any])


class ConfabVerificationError(Exception):
    """Raised when confab verification fails and on_fail='raise'."""

    def __init__(self, message: str, failures: List[VerificationOutcome]):
        super().__init__(message)
        self.failures = failures


@dataclass
class VerificationReport:
    """Result of verifying an agent function's output."""

    output: str
    claims_found: int
    verified: int
    passed: int
    failed: int
    inconclusive: int
    skipped: int
    failures: List[VerificationOutcome] = field(default_factory=list)
    all_outcomes: List[VerificationOutcome] = field(default_factory=list)
    checked_at: str = ""

    @property
    def clean(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        if self.clean:
            return f"confab: CLEAN ({self.claims_found} claims, {self.passed} passed)"
        parts = []
        for f in self.failures:
            parts.append(f"  - {f.claim.text[:80]}: {f.evidence[:120]}")
        detail = "\n".join(parts)
        return (
            f"confab: {self.failed} FAILED of {self.claims_found} claims\n{detail}"
        )


def _extract_and_verify(
    text: str,
    *,
    check_files: bool = True,
    check_env: bool = True,
    check_counts: bool = False,
) -> VerificationReport:
    """Extract claims from text and verify them.

    Args:
        text: Agent output text to scan for claims.
        check_files: Verify file existence claims.
        check_env: Verify environment variable claims.
        check_counts: Verify count/quantity claims.
    """
    # Extract claims from the raw text (no source file context)
    claims = extract_claims(text)

    # Filter to requested check types
    allowed_types = set()
    if check_files:
        allowed_types.update({
            ClaimType.FILE_EXISTS,
            ClaimType.FILE_MISSING,
            ClaimType.SCRIPT_RUNS,
            ClaimType.SCRIPT_BROKEN,
            ClaimType.CONFIG_PRESENT,
        })
    if check_env:
        allowed_types.add(ClaimType.ENV_VAR)
    if check_counts:
        allowed_types.add(ClaimType.COUNT_CLAIM)

    # Always include these if any checking is enabled
    if allowed_types:
        allowed_types.update({
            ClaimType.PIPELINE_WORKS,
            ClaimType.PIPELINE_BLOCKED,
        })

    filtered = [c for c in claims if c.claim_type in allowed_types]

    # Verify
    outcomes = [verify_claim(c) for c in filtered]

    passed = sum(1 for o in outcomes if o.result == VerificationResult.PASSED)
    failed_list = [o for o in outcomes if o.result == VerificationResult.FAILED]
    inconclusive = sum(
        1 for o in outcomes if o.result == VerificationResult.INCONCLUSIVE
    )
    skipped = sum(1 for o in outcomes if o.result == VerificationResult.SKIPPED)

    return VerificationReport(
        output=text,
        claims_found=len(filtered),
        verified=len(outcomes),
        passed=passed,
        failed=len(failed_list),
        inconclusive=inconclusive,
        skipped=skipped,
        failures=failed_list,
        all_outcomes=outcomes,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


def confab_gate(
    _func: Optional[Callable] = None,
    *,
    check_files: bool = True,
    check_env: bool = True,
    check_counts: bool = False,
    on_fail: str = "warn",
) -> Union[Callable, Callable[[F], F]]:
    """Decorator that verifies confabulation claims in agent function output.

    Intercepts the function's return value (must be a string), extracts
    verifiable claims (file paths, env vars, counts), checks them against
    reality, and acts based on ``on_fail``.

    Args:
        check_files: Verify file existence claims (default True).
        check_env: Verify environment variable claims (default True).
        check_counts: Verify count/quantity claims (default False).
        on_fail: What to do when verification fails:
            - ``"raise"``: Raise :class:`ConfabVerificationError`
            - ``"warn"``: Issue a warning and return output normally
            - ``"log"``: Log failures at WARNING level, return output normally

    Returns:
        The original function's return value (string). The verification
        report is attached as a ``_confab_report`` attribute on the
        return value's wrapper, accessible via :func:`get_report`.

    Examples::

        @confab_gate
        def simple_agent(prompt):
            return "Updated /path/to/file.py successfully."

        @confab_gate(check_files=True, check_env=True, on_fail="raise")
        def strict_agent(prompt):
            return "Config at /etc/app.yaml has been applied."

        # Access the verification report after calling:
        result = strict_agent("do something")
        report = get_report(result)
        if report and not report.clean:
            print(report.summary())
    """

    _VALID_ON_FAIL = {"raise", "warn", "log"}
    if on_fail not in _VALID_ON_FAIL:
        raise ValueError(
            f"on_fail must be one of {_VALID_ON_FAIL!r}, got {on_fail!r}"
        )

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = func(*args, **kwargs)

            # Only verify string outputs
            if not isinstance(result, str):
                return result

            report = _extract_and_verify(
                result,
                check_files=check_files,
                check_env=check_env,
                check_counts=check_counts,
            )

            # Store report for retrieval
            _LAST_REPORTS[id(wrapper)] = report

            if not report.clean:
                msg = report.summary()
                if on_fail == "raise":
                    raise ConfabVerificationError(msg, report.failures)
                elif on_fail == "warn":
                    warnings.warn(msg, stacklevel=2)
                elif on_fail == "log":
                    logger.warning(msg)

            return result

        # Tag the wrapper so get_report can find it
        wrapper._confab_gated = True  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    # Support both @confab_gate and @confab_gate(...)
    if _func is not None:
        return decorator(_func)
    return decorator


# Store last verification report per decorated function
_LAST_REPORTS: dict = {}


def get_report(func_or_result: Any) -> Optional[VerificationReport]:
    """Retrieve the last verification report for a confab-gated function.

    Args:
        func_or_result: The decorated function itself.

    Returns:
        The :class:`VerificationReport` from the last call, or None if
        the function hasn't been called or isn't confab-gated.

    Example::

        @confab_gate
        def my_agent(prompt):
            return "Wrote /tmp/output.json"

        result = my_agent("go")
        report = get_report(my_agent)
        print(report.summary())
    """
    report = _LAST_REPORTS.get(id(func_or_result))
    return report


def verify_text(
    text: str,
    *,
    check_files: bool = True,
    check_env: bool = True,
    check_counts: bool = False,
) -> VerificationReport:
    """Verify claims in arbitrary text without using the decorator.

    Convenience function for one-off verification of agent output that
    wasn't produced by a decorated function.

    Args:
        text: Text to scan for verifiable claims.
        check_files: Verify file existence claims.
        check_env: Verify environment variable claims.
        check_counts: Verify count/quantity claims.

    Returns:
        A :class:`VerificationReport` with verification results.

    Example::

        report = verify_text("The database at /data/app.db has 500 entries.")
        if not report.clean:
            print(report.summary())
    """
    return _extract_and_verify(
        text,
        check_files=check_files,
        check_env=check_env,
        check_counts=check_counts,
    )
