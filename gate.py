"""Cascade gate — structural enforcement at handoff points.

This is the core prevention mechanism. At every agent handoff point
(builder → next builder, dreamer → builder), the gate:

1. Extracts claims from the handoff text / priority file
2. Runs auto-verification on verifiable claims
3. Produces a gate report: what passed, what failed, what needs manual check
4. Flags stale unverified claims (age > threshold)

The gate doesn't block execution — it produces a report that the receiving
agent MUST acknowledge before proceeding. This makes verification structural
without making it blocking (which would violate the "wrong > blocked" principle).

Design principle from truth-016: confabulation is indistinguishable from
understanding without external oracle bits. The gate supplies those oracle
bits at the point where cascade propagation happens.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .claims import (
    Claim,
    ClaimType,
    VerifiabilityLevel,
    extract_claims,
    extract_claims_from_file,
    summarize_claims,
)
from .tracker import (
    TrackedClaim,
    record_gate_run,
    get_stale_claims,
    _hash_claim,
    DEFAULT_STALE_THRESHOLD as TRACKER_STALE_THRESHOLD,
)
from .config import get_config
from .verify import (
    VerificationOutcome,
    VerificationResult,
    verify_all,
    summarize_outcomes,
)

# Staleness threshold: claims unverified after this many build sections
STALE_BUILD_THRESHOLD = 3


@dataclass
class GateReport:
    """Result of running the cascade gate."""
    timestamp: str
    files_scanned: List[str]
    total_claims: int
    auto_verified: int
    passed: int
    failed: int
    inconclusive: int
    skipped: int
    stale_claims: int              # Unverified claims older than threshold
    failed_details: List[Dict[str, Any]]
    stale_details: List[Dict[str, Any]]
    all_outcomes: List[VerificationOutcome]
    # Tracker metadata (populated when tracker is enabled)
    tracker_new: int = 0           # Claims seen for the first time
    tracker_returning: int = 0     # Claims seen before
    tracker_total_runs: int = 0    # Total gate runs recorded

    @property
    def has_failures(self) -> bool:
        return self.failed > 0

    @property
    def has_stale(self) -> bool:
        return self.stale_claims > 0

    @property
    def clean(self) -> bool:
        return not self.has_failures and not self.has_stale

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "files_scanned": self.files_scanned,
            "total_claims": self.total_claims,
            "auto_verified": self.auto_verified,
            "passed": self.passed,
            "failed": self.failed,
            "inconclusive": self.inconclusive,
            "skipped": self.skipped,
            "stale_claims": self.stale_claims,
            "clean": self.clean,
            "failed_details": self.failed_details,
            "stale_details": self.stale_details,
            "tracker": {
                "new_claims": self.tracker_new,
                "returning_claims": self.tracker_returning,
                "total_runs": self.tracker_total_runs,
            },
        }

    def format_report(self) -> str:
        """Format as human-readable report for agent consumption."""
        lines = []
        lines.append("# Confabulation Gate Report")
        lines.append(f"\nScanned: {', '.join(self.files_scanned)}")
        lines.append(f"Claims found: {self.total_claims}")
        lines.append(f"Auto-verified: {self.auto_verified}")

        if self.clean:
            lines.append("\n**GATE: CLEAN** — No failed verifications or stale claims.")
            return "\n".join(lines)

        if self.has_failures:
            lines.append(f"\n## FAILED VERIFICATIONS ({self.failed})")
            lines.append("")
            lines.append("These claims contradict observable reality:")
            lines.append("")
            for detail in self.failed_details:
                lines.append(f"**Claim:** {detail['claim_text']}")
                if detail.get('source_file'):
                    lines.append(f"  Source: {detail['source_file']}:{detail.get('source_line', '?')}")
                lines.append(f"  Evidence: {detail['evidence']}")
                lines.append(f"  Action: {detail['action']}")
                lines.append("")

        if self.has_stale:
            lines.append(f"\n## STALE CLAIMS ({self.stale_claims})")
            lines.append("")
            lines.append(f"These claims have persisted for {STALE_BUILD_THRESHOLD}+ build sections without verification:")
            lines.append("")
            for detail in self.stale_details:
                lines.append(f"**Claim:** {detail['claim_text']}")
                lines.append(f"  Age: {detail['age_builds']} build sections")
                lines.append(f"  Action: Verify or delete before propagating.")
                lines.append("")

        # Summary
        passed_pct = (self.passed / self.auto_verified * 100) if self.auto_verified > 0 else 0
        lines.append("## Summary")
        lines.append(f"- Passed: {self.passed}/{self.auto_verified} auto-verified ({passed_pct:.0f}%)")
        lines.append(f"- Failed: {self.failed}")
        lines.append(f"- Inconclusive: {self.inconclusive}")
        lines.append(f"- Stale: {self.stale_claims}")
        lines.append(f"- Manual: {self.skipped}")

        # Tracker info
        if self.tracker_total_runs > 0:
            lines.append(f"\n## Tracker (run #{self.tracker_total_runs})")
            lines.append(f"- New claims this run: {self.tracker_new}")
            lines.append(f"- Returning claims: {self.tracker_returning}")

        return "\n".join(lines)

    def format_slack(self) -> str:
        """Format as concise Slack-friendly report.

        No markdown tables, no headers, just emoji status indicators
        and short lines suitable for Slack message display.
        """
        lines = []

        if self.clean:
            lines.append(f":white_check_mark: Gate CLEAN — {self.total_claims} claims, {self.passed} verified")
            if self.tracker_total_runs > 0:
                lines.append(f"Run #{self.tracker_total_runs} | {self.tracker_new} new, {self.tracker_returning} returning")
            return "\n".join(lines)

        # Status line
        status_parts = []
        if self.failed > 0:
            status_parts.append(f":x: {self.failed} FAILED")
        if self.stale_claims > 0:
            status_parts.append(f":hourglass: {self.stale_claims} stale")
        if self.passed > 0:
            status_parts.append(f":white_check_mark: {self.passed} passed")

        lines.append(" | ".join(status_parts))
        lines.append(f"{self.total_claims} claims scanned, {self.inconclusive} inconclusive")

        # Failed details (compact)
        if self.has_failures:
            lines.append("")
            for detail in self.failed_details:
                claim_short = detail['claim_text'][:80]
                lines.append(f":x: {claim_short}")
                evidence_short = detail['evidence'].split('\n')[0][:80]
                lines.append(f"  {evidence_short}")

        # Stale details (compact, max 3)
        if self.has_stale:
            lines.append("")
            shown = self.stale_details[:3]
            for detail in shown:
                claim_short = detail['claim_text'][:80]
                age = detail.get('age_builds', '?')
                lines.append(f":hourglass: [{age} runs] {claim_short}")
            remaining = len(self.stale_details) - len(shown)
            if remaining > 0:
                lines.append(f"  ...and {remaining} more stale claims")

        # Tracker
        if self.tracker_total_runs > 0:
            lines.append(f"\nRun #{self.tracker_total_runs} | {self.tracker_new} new, {self.tracker_returning} returning")

        return "\n".join(lines)


def run_gate(
    files: Optional[List[str]] = None,
    text: Optional[str] = None,
    stale_threshold: int = STALE_BUILD_THRESHOLD,
    track: bool = True,
) -> GateReport:
    """Run the cascade gate on specified files and/or text.

    Args:
        files: List of file paths to scan (relative to workspace or absolute).
               Defaults to builder_priorities.md and dreamer_priorities.md.
        text: Additional text to scan for claims.
        stale_threshold: Number of build sections after which unverified claims are flagged.
        track: Whether to record this run in the persistent tracker DB.

    Returns:
        GateReport with verification results.
    """
    config = get_config()

    if files is None:
        files = config.files_to_scan

    all_claims: List[Claim] = []
    scanned_files = []

    # Extract claims from files
    for file_path in files:
        resolved = Path(file_path)
        if not resolved.is_absolute():
            resolved = config.workspace_root / file_path
        if resolved.exists():
            claims = extract_claims_from_file(str(resolved))
            all_claims.extend(claims)
            try:
                scanned_files.append(str(resolved.relative_to(config.workspace_root)))
            except ValueError:
                scanned_files.append(str(resolved))

    # Extract claims from text
    if text:
        text_claims = extract_claims(text, source_file="<inline>")
        all_claims.extend(text_claims)

    # Run verification
    outcomes = verify_all(all_claims)

    # Identify failed verifications
    failed_details = []
    for outcome in outcomes:
        if outcome.result == VerificationResult.FAILED:
            failed_details.append({
                "claim_text": outcome.claim.text[:200],
                "claim_type": outcome.claim.claim_type.value,
                "source_file": outcome.claim.source_file,
                "source_line": outcome.claim.source_line,
                "evidence": outcome.evidence,
                "action": _suggest_action(outcome),
            })

    # Identify stale claims from in-file build section counting (original method)
    stale_details = []
    for claim in all_claims:
        if (claim.verification_tag is None
                and claim.age_builds >= stale_threshold
                and claim.verifiability != VerifiabilityLevel.MANUAL):
            stale_details.append({
                "claim_text": claim.text[:200],
                "claim_type": claim.claim_type.value,
                "age_builds": claim.age_builds,
                "source_file": claim.source_file,
                "source_line": claim.source_line,
            })

    # Record in persistent tracker and merge stale claims from tracker DB
    tracker_new = 0
    tracker_returning = 0
    tracker_total_runs = 0

    if track:
        # Build verification results map: claim_hash → result
        vr_map: Dict[str, VerificationResult] = {}
        for outcome in outcomes:
            h = _hash_claim(outcome.claim.text)
            vr_map[h] = outcome.result

        tracker_summary = record_gate_run(
            claims=all_claims,
            verification_results=vr_map,
            files_scanned=scanned_files,
        )
        tracker_new = tracker_summary["new_claims"]
        tracker_returning = tracker_summary["returning_claims"]

        # Pull stale claims from tracker DB (persistent across runs)
        # Only include claims that were also extracted this run — old DB
        # records for claims that are no longer being extracted (e.g. because
        # they were filtered out as meta-rules) should not appear as stale.
        current_hashes = {_hash_claim(c.text) for c in all_claims}
        tracker_stale = get_stale_claims(stale_threshold)
        seen_texts = {d["claim_text"] for d in stale_details}
        for tc in tracker_stale:
            if tc.claim_hash not in current_hashes:
                continue  # Not extracted this run — skip
            if tc.claim_text[:200] not in seen_texts:
                stale_details.append({
                    "claim_text": tc.claim_text[:200],
                    "claim_type": tc.claim_type,
                    "age_builds": tc.run_count,
                    "source_file": tc.source_file,
                    "source_line": None,
                    "tracker_run_count": tc.run_count,
                })

        # Get total run count
        from .tracker import get_stats
        stats = get_stats()
        tracker_total_runs = stats["total_gate_runs"]

    # Count results
    result_counts = {}
    for o in outcomes:
        result_counts[o.result.value] = result_counts.get(o.result.value, 0) + 1

    auto_count = sum(1 for o in outcomes if o.result != VerificationResult.SKIPPED)

    return GateReport(
        timestamp=datetime.now(timezone.utc).isoformat(),
        files_scanned=scanned_files,
        total_claims=len(all_claims),
        auto_verified=auto_count,
        passed=result_counts.get("passed", 0),
        failed=result_counts.get("failed", 0),
        inconclusive=result_counts.get("inconclusive", 0),
        skipped=result_counts.get("skipped", 0),
        stale_claims=len(stale_details),
        failed_details=failed_details,
        stale_details=stale_details,
        all_outcomes=outcomes,
        tracker_new=tracker_new,
        tracker_returning=tracker_returning,
        tracker_total_runs=tracker_total_runs,
    )


def _suggest_action(outcome: VerificationOutcome) -> str:
    """Suggest what to do about a failed verification."""
    ct = outcome.claim.claim_type

    if ct == ClaimType.ENV_VAR:
        return "Env var exists — remove the blocker claim or update to reflect the actual issue."

    if ct == ClaimType.FILE_EXISTS:
        return "File(s) not found — update the path or remove the claim."

    if ct == ClaimType.FILE_MISSING:
        return "File(s) actually exist — the 'missing' claim is wrong. Remove it."

    if ct == ClaimType.PIPELINE_BLOCKED:
        return "Evidence suggests pipeline is not blocked. Test it and update the claim."

    if ct == ClaimType.PIPELINE_WORKS:
        return "Pipeline output missing or stale. The 'working' claim may be wrong."

    if ct in (ClaimType.SCRIPT_RUNS, ClaimType.SCRIPT_BROKEN):
        return "Script has issues. Check the syntax error and fix or update the claim."

    if ct == ClaimType.CONFIG_PRESENT:
        return "Config file missing, invalid, or missing expected keys. Check the file and update the claim."

    return "Claim contradicts evidence. Investigate and correct."


class ConfabGate:
    """High-level API for running the confabulation gate.

    Encapsulates configuration and gate execution in a single object,
    suitable for programmatic use in external projects.

    Usage::

        from confab import ConfabGate

        # From a config file
        gate = ConfabGate("confab.toml")
        report = gate.run()

        # With explicit config
        from confab import ConfabConfig
        config = ConfabConfig(
            workspace_root=Path("."),
            files_to_scan=["docs/handoff.md"],
        )
        gate = ConfabGate(config=config)
        report = gate.run()

        # Check specific files or text
        report = gate.run(files=["notes/priorities.md"])
        report = gate.run(text="Pipeline is blocked on OPENAI_API_KEY")

        # One-line summary
        print(gate.quick())

        # Access results
        if report.has_failures:
            print(report.format_report())
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        *,
        config: Optional["ConfabConfig"] = None,
        workspace_root: Optional[str] = None,
    ):
        """Initialize the gate with configuration.

        Args:
            config_path: Path to a confab.toml file.
            config: A pre-built ConfabConfig object (takes precedence).
            workspace_root: Override workspace root directory.
        """
        from .config import ConfabConfig as _ConfabConfig, load_config, set_config

        if config is not None:
            self._config = config
        elif config_path is not None:
            ws = Path(workspace_root) if workspace_root else None
            self._config = load_config(config_path=Path(config_path), workspace_root=ws)
        elif workspace_root is not None:
            self._config = load_config(workspace_root=Path(workspace_root))
        else:
            self._config = load_config()

        # Set as active config so internal modules use it
        set_config(self._config)

    @property
    def config(self) -> "ConfabConfig":
        """The active configuration."""
        return self._config

    def run(
        self,
        files: Optional[List[str]] = None,
        text: Optional[str] = None,
        stale_threshold: Optional[int] = None,
        track: bool = True,
    ) -> GateReport:
        """Run the cascade gate.

        Args:
            files: Files to scan. Defaults to configured files_to_scan.
            text: Additional inline text to scan.
            stale_threshold: Override stale threshold from config.
            track: Whether to record in persistent tracker DB.

        Returns:
            GateReport with verification results.
        """
        threshold = stale_threshold if stale_threshold is not None else self._config.stale_threshold
        return run_gate(files=files, text=text, stale_threshold=threshold, track=track)

    def quick(self, file_path: Optional[str] = None) -> str:
        """One-line gate summary for embedding in prompts."""
        return quick_check(file_path)

    def extract(self, file_path: str) -> List[Claim]:
        """Extract claims from a file without verifying."""
        return extract_claims_from_file(file_path)

    def check(self, text: str) -> List["VerificationOutcome"]:
        """Check inline text for claims and verify them."""
        from .verify import verify_all as _verify_all
        claims = extract_claims(text, source_file="<api>")
        return _verify_all(claims)


def quick_check(file_path: Optional[str] = None) -> str:
    """Run a quick gate check and return a one-line summary.

    Useful for embedding in agent prompts or pre-flight checks.
    """
    files = [file_path] if file_path else None
    report = run_gate(files=files)

    if report.clean:
        return f"Gate: CLEAN ({report.total_claims} claims, {report.passed} verified)"

    parts = []
    if report.failed > 0:
        parts.append(f"{report.failed} FAILED")
    if report.stale_claims > 0:
        parts.append(f"{report.stale_claims} STALE")

    return f"Gate: {'|'.join(parts)} ({report.total_claims} claims, {report.passed} passed)"
