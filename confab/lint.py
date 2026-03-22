"""Claim hygiene linter for the confabulation framework.

Scans priority/handoff files and flags claims with poor hygiene:
- Claims without verification tags ([v1], [v2], [unverified], [FAILED])
- Claims with [unverified] tag seen in 3+ gate runs (stale)
- Claims with [FAILED] tag that still persist (should be fixed or removed)
- Approximate counts without sources (~N items)

This is the PREVENTION side of the confab framework: structural pressure
to write claims in documented format with verification tags.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .claims import (
    Claim,
    ClaimType,
    VERIFICATION_TAG_RE,
    extract_claims_from_file,
)
from .config import get_config
from .tracker import get_stale_claims, _hash_claim, _get_db, DEFAULT_STALE_THRESHOLD


class LintSeverity:
    ERROR = "error"      # Must fix: FAILED claims persisting, etc.
    WARNING = "warning"  # Should fix: untagged claims, stale claims
    INFO = "info"        # Advisory: approximate counts without sources


@dataclass
class LintIssue:
    """A single lint issue found in a file."""
    file: str
    line: int
    severity: str          # LintSeverity value
    rule: str              # Rule ID (e.g., "no-tag", "stale", "failed-persists", "approx-no-source")
    message: str           # Human-readable description
    claim_text: str        # The offending claim text

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "claim_text": self.claim_text,
        }


@dataclass
class LintReport:
    """Result of a lint run."""
    files_scanned: List[str]
    issues: List[LintIssue] = field(default_factory=list)
    total_claims: int = 0

    @property
    def clean(self) -> bool:
        return len(self.issues) == 0

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == LintSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == LintSeverity.WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == LintSeverity.INFO)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "files_scanned": self.files_scanned,
            "total_claims": self.total_claims,
            "issues": [i.to_dict() for i in self.issues],
            "summary": {
                "total": len(self.issues),
                "errors": self.error_count,
                "warnings": self.warning_count,
                "info": self.info_count,
            },
            "clean": self.clean,
        }

    def format_report(self) -> str:
        """Format a human-readable lint report."""
        lines = []
        lines.append("CONFAB LINT REPORT")
        lines.append("=" * 52)
        lines.append(f"Files scanned: {len(self.files_scanned)}")
        lines.append(f"Claims found:  {self.total_claims}")
        lines.append(f"Issues:        {len(self.issues)} "
                     f"({self.error_count} errors, {self.warning_count} warnings, {self.info_count} info)")
        lines.append("")

        if self.clean:
            lines.append("All claims have proper hygiene. No issues found.")
            return "\n".join(lines)

        # Group by file
        by_file: Dict[str, List[LintIssue]] = {}
        for issue in self.issues:
            by_file.setdefault(issue.file, []).append(issue)

        severity_icon = {
            LintSeverity.ERROR: "E",
            LintSeverity.WARNING: "W",
            LintSeverity.INFO: "I",
        }

        for filepath, file_issues in by_file.items():
            lines.append(f"--- {filepath}")
            for issue in sorted(file_issues, key=lambda i: i.line):
                icon = severity_icon.get(issue.severity, "?")
                lines.append(f"  {icon} line {issue.line}: [{issue.rule}] {issue.message}")
                lines.append(f"    {issue.claim_text[:100]}")
            lines.append("")

        # Summary
        lines.append("=" * 52)
        if self.error_count > 0:
            lines.append(f"FAILED — {self.error_count} error(s) require attention")
        elif self.warning_count > 0:
            lines.append(f"WARNINGS — {self.warning_count} claim(s) need verification tags")
        else:
            lines.append(f"OK — {self.info_count} advisory note(s)")

        return "\n".join(lines)


# Approximate count pattern: ~N followed by a noun
APPROX_COUNT_RE = re.compile(
    r'~\s*(\d+)\s+(?:entries|items|posts|notes|files|tests|builds|sprints|days|hours|'
    r'commits|observations|ideas|principles|scripts|databases|subscribers|views|published|'
    r'journal|agents|records|sessions|runs)',
    re.IGNORECASE,
)

# Source citation patterns that indicate a count has been grounded
SOURCE_CITATION_RE = re.compile(
    r'\((?:per|from|via|source|checked|counted|verified|as of)\b'
    r'|\[v[12]:'
    r'|\[verified'
    r'|(?:per|from|via|source:)\s+`[^`]+`'
    r'|(?:per|from|via)\s+\S+\.(?:json|md|py|db|csv)',
    re.IGNORECASE,
)


def run_lint(
    files: Optional[List[str]] = None,
    stale_threshold: int = DEFAULT_STALE_THRESHOLD,
) -> LintReport:
    """Run the claim hygiene linter.

    Args:
        files: Files to scan. If None, uses files_to_scan from confab.toml.
        stale_threshold: How many gate runs before [unverified] is flagged stale.

    Returns:
        LintReport with all issues found.
    """
    config = get_config()

    if files is None:
        scan_files = [
            str(config.workspace_root / f) for f in config.files_to_scan
        ]
    else:
        scan_files = files

    report = LintReport(files_scanned=scan_files)

    # Build a lookup of stale claim hashes from the tracker DB
    stale_hashes = set()
    try:
        stale_claims = get_stale_claims(threshold=stale_threshold)
        stale_hashes = {c.claim_hash for c in stale_claims}
    except Exception:
        pass  # Tracker DB may not exist yet

    # Also build a lookup of run counts for unverified claims
    unverified_run_counts: Dict[str, int] = {}
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT claim_hash, run_count FROM tracked_claims "
            "WHERE status IN ('unverified', 'stale', 'inconclusive') "
            "AND last_verified IS NULL"
        ).fetchall()
        for row in rows:
            unverified_run_counts[row["claim_hash"]] = row["run_count"]
        db.close()
    except Exception:
        pass

    for filepath in scan_files:
        path = Path(filepath)
        if not path.exists():
            continue

        # Extract claims using the existing engine
        claims = extract_claims_from_file(filepath)
        report.total_claims += len(claims)

        # Also scan raw lines for approximate counts
        text = path.read_text()
        _check_approx_counts(text, filepath, report)

        # Check each extracted claim
        for claim in claims:
            _check_claim(claim, stale_hashes, unverified_run_counts,
                        stale_threshold, report)

    return report


def _check_claim(
    claim: Claim,
    stale_hashes: set,
    unverified_run_counts: Dict[str, int],
    stale_threshold: int,
    report: LintReport,
) -> None:
    """Check a single claim for lint issues."""
    source = claim.source_file or "<unknown>"
    line = claim.source_line or 0

    # Rule: failed-persists — [FAILED] claims should be fixed or removed
    if claim.verification_tag and "FAILED" in claim.verification_tag.upper():
        report.issues.append(LintIssue(
            file=source,
            line=line,
            severity=LintSeverity.ERROR,
            rule="failed-persists",
            message="Claim marked [FAILED] still present — fix the issue or remove the claim",
            claim_text=claim.text,
        ))
        return  # Don't double-flag

    # Rule: stale-unverified — [unverified] claims seen in 3+ gate runs
    if claim.verification_tag and "unverified" in claim.verification_tag.lower():
        claim_hash = _hash_claim(claim.text)
        run_count = unverified_run_counts.get(claim_hash, 0)
        if run_count >= stale_threshold or claim_hash in stale_hashes:
            report.issues.append(LintIssue(
                file=source,
                line=line,
                severity=LintSeverity.ERROR,
                rule="stale-unverified",
                message=f"Claim tagged [unverified] for {run_count} gate runs — verify or delete",
                claim_text=claim.text,
            ))
            return  # Don't double-flag

    # Rule: no-tag — auto/semi-verifiable claims without any verification tag
    # Skip subjective claims — they don't need verification tags
    if claim.claim_type == ClaimType.SUBJECTIVE:
        return

    if claim.verification_tag is None:
        # Only flag auto and semi-verifiable claims (not manual-only)
        from .claims import VerifiabilityLevel
        if claim.verifiability in (VerifiabilityLevel.AUTO, VerifiabilityLevel.SEMI):
            report.issues.append(LintIssue(
                file=source,
                line=line,
                severity=LintSeverity.WARNING,
                rule="no-tag",
                message="Claim has no verification tag — add [unverified], [v1: ...], or [v2: ...]",
                claim_text=claim.text,
            ))


def _check_approx_counts(text: str, filepath: str, report: LintReport) -> None:
    """Check for approximate counts without source citations."""
    lines = text.split('\n')
    for line_num, line in enumerate(lines, 1):
        if APPROX_COUNT_RE.search(line) and not SOURCE_CITATION_RE.search(line):
            report.issues.append(LintIssue(
                file=filepath,
                line=line_num,
                severity=LintSeverity.INFO,
                rule="approx-no-source",
                message="Approximate count without source — add a citation or verify the number",
                claim_text=line.strip(),
            ))
