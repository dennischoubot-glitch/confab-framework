"""Auto-quarantine for persistently unverified claims.

When claims survive N+ gate runs without verification, they are quarantined:
1. Moved to a ## QUARANTINED CLAIMS section in the source priority file
2. A Slack notification is posted with the claim, persistence count, and
   suggested verification action.

This makes propagation structurally impossible — quarantined claims are in a
labeled section that agents know not to propagate from. The 16-build false
blocker episode (obs-3528) proved agents don't act on stale warnings alone;
this module ensures they structurally can't.

Usage:
    from confab.quarantine import run_quarantine

    report = run_gate()
    result = run_quarantine(report, threshold=5, dry_run=False)
    print(result.format_report())
"""

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .claims import ClaimType


# Default: quarantine after 5 gate runs without verification
QUARANTINE_THRESHOLD = 5

# Section header written into priority files
QUARANTINE_SECTION = "## QUARANTINED CLAIMS"

# Claim type → human-readable verification suggestion
_VERIFICATION_SUGGESTIONS = {
    ClaimType.FILE_EXISTS.value: "Check: `ls {path}` or read the file",
    ClaimType.FILE_MISSING.value: "Check: `ls {path}` to confirm absence",
    ClaimType.ENV_VAR.value: "Check: `echo ${var}` in the runtime environment",
    ClaimType.PIPELINE_BLOCKED.value: "Test: run the pipeline script and check output",
    ClaimType.PIPELINE_WORKS.value: "Test: run the pipeline script and check output",
    ClaimType.SCRIPT_RUNS.value: "Test: `python {script}` and check exit code",
    ClaimType.SCRIPT_BROKEN.value: "Test: `python {script}` and check for errors",
    ClaimType.CONFIG_PRESENT.value: "Check: read the config file and verify keys",
}


@dataclass
class QuarantineAction:
    """Record of a single quarantine action."""
    claim_text: str
    claim_type: str
    source_file: Optional[str]
    run_count: int
    verification_hint: str
    moved: bool = False  # Whether the line was actually moved in the file


@dataclass
class QuarantineResult:
    """Result of a quarantine operation."""
    timestamp: str
    threshold: int
    candidates: int
    quarantined: List[QuarantineAction] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    slack_posted: bool = False
    dry_run: bool = False

    def format_report(self) -> str:
        """Human-readable report of quarantine actions."""
        lines = []
        prefix = "[DRY RUN] " if self.dry_run else ""
        lines.append(f"{prefix}Quarantine Report")
        lines.append(f"Threshold: {self.threshold} gate runs")
        lines.append(f"Candidates: {self.candidates}")
        lines.append(f"Quarantined: {len(self.quarantined)}")

        if self.quarantined:
            lines.append("")
            for action in self.quarantined:
                status = "MOVED" if action.moved else "NOTED"
                lines.append(f"  [{status}] [{action.run_count} runs] {action.claim_text[:100]}")
                lines.append(f"    Source: {action.source_file or '?'}")
                lines.append(f"    Verify: {action.verification_hint}")

        if self.skipped:
            lines.append(f"\nSkipped: {len(self.skipped)}")
            for skip in self.skipped:
                lines.append(f"  {skip.get('reason', '?')}: {skip.get('claim', '?')[:80]}")

        if self.slack_posted:
            lines.append("\nSlack notification posted.")

        return "\n".join(lines)

    def format_slack(self) -> str:
        """Slack-formatted quarantine notification."""
        if not self.quarantined:
            return ""

        lines = []
        lines.append(":rotating_light: *Confab Quarantine Alert*")
        lines.append(f"{len(self.quarantined)} claim(s) quarantined after {self.threshold}+ gate runs without verification:\n")

        for action in self.quarantined:
            lines.append(f":no_entry: *[{action.run_count} runs]* {action.claim_text[:120]}")
            if action.source_file:
                lines.append(f"  _Source:_ `{action.source_file}`")
            lines.append(f"  _Verify:_ {action.verification_hint}")
            lines.append("")

        lines.append("These claims have been moved to `## QUARANTINED CLAIMS` in their source files.")
        lines.append("To restore: verify the claim, then move it back with a `[v1: ...]` tag.")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "threshold": self.threshold,
            "candidates": self.candidates,
            "quarantined": [
                {
                    "claim_text": a.claim_text,
                    "claim_type": a.claim_type,
                    "source_file": a.source_file,
                    "run_count": a.run_count,
                    "verification_hint": a.verification_hint,
                    "moved": a.moved,
                }
                for a in self.quarantined
            ],
            "skipped": self.skipped,
            "slack_posted": self.slack_posted,
            "dry_run": self.dry_run,
        }


def _suggest_verification(claim_type: str, claim_text: str) -> str:
    """Suggest how to verify a claim based on its type."""
    suggestion = _VERIFICATION_SUGGESTIONS.get(claim_type)
    if suggestion:
        return suggestion
    # Generic fallback
    return "Manually verify this claim against current system state"


def _find_claim_line(content: str, claim_text: str) -> Optional[str]:
    """Find the full line in content that contains the claim text."""
    for line in content.splitlines():
        stripped = line.lstrip("- ").lstrip("0123456789.").strip()
        if claim_text in line or claim_text.strip() in stripped:
            return line

    # Try matching the core content (skip leading numbering)
    core = re.sub(r'^\d+\.\s*', '', claim_text).strip()
    if len(core) > 20:
        for line in content.splitlines():
            if core[:40] in line:
                return line

    return None


def _move_to_quarantine_section(
    file_path: str,
    claim_lines: List[str],
    run_counts: Dict[str, int],
) -> int:
    """Move claim lines to a QUARANTINED CLAIMS section in the file.

    Returns the number of lines actually moved.
    """
    path = Path(file_path)
    if not path.exists():
        return 0

    content = path.read_text()
    moved = 0

    # Remove each claim line from its current position
    for line in claim_lines:
        if line in content:
            # Remove the line (and its trailing newline)
            content = content.replace(line + "\n", "", 1)
            moved += 1

    if moved == 0:
        return 0

    # Build quarantine entries with metadata
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    quarantine_entries = []
    for line in claim_lines:
        count = run_counts.get(line, "?")
        quarantine_entries.append(
            f"{line.rstrip()}  *(quarantined {now}, {count} gate runs unverified)*"
        )

    quarantine_block = "\n".join(quarantine_entries)

    # Find or create the QUARANTINED CLAIMS section
    if QUARANTINE_SECTION in content:
        # Append to existing section — insert after the header line
        section_idx = content.index(QUARANTINE_SECTION)
        # Find the end of the header line
        newline_after = content.index("\n", section_idx)
        # Check if there's already a blank line after the header
        insert_pos = newline_after + 1
        content = (
            content[:insert_pos]
            + quarantine_block + "\n"
            + content[insert_pos:]
        )
    else:
        # Create the section at the end of the file
        # Ensure file ends with a newline before the section
        if not content.endswith("\n"):
            content += "\n"
        content += f"\n---\n\n{QUARANTINE_SECTION}\n\n"
        content += "Claims moved here have persisted 5+ gate runs without verification.\n"
        content += "To restore: verify the claim, then move it back with a `[v1: ...]` tag.\n\n"
        content += quarantine_block + "\n"

    path.write_text(content)
    return moved


def run_quarantine(
    gate_report: "GateReport",
    threshold: int = QUARANTINE_THRESHOLD,
    post_slack: bool = False,
    dry_run: bool = False,
) -> QuarantineResult:
    """Run quarantine on stale claims from a gate report.

    Args:
        gate_report: A GateReport from run_gate().
        threshold: Minimum run_count to trigger quarantine.
        post_slack: Whether to post a notification to Slack.
        dry_run: If True, report what would happen without modifying files.

    Returns:
        QuarantineResult with details of actions taken.
    """
    result = QuarantineResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        threshold=threshold,
        candidates=0,
        dry_run=dry_run,
    )

    # Filter stale claims that meet the quarantine threshold
    # stale_details may have 'tracker_run_count' (from tracker DB) or 'age_builds' (from in-file)
    candidates = []
    for detail in gate_report.stale_details:
        run_count = detail.get("tracker_run_count", detail.get("age_builds", 0))
        if run_count >= threshold:
            candidates.append((detail, run_count))

    result.candidates = len(candidates)

    if not candidates:
        return result

    # Group by source file for batch file modifications
    by_file: Dict[str, List[tuple]] = {}
    for detail, run_count in candidates:
        source = detail.get("source_file")
        if source:
            # Resolve to absolute path if relative
            source_path = Path(source)
            if not source_path.is_absolute():
                try:
                    from .config import get_config
                    config = get_config()
                    source_path = config.workspace_root / source
                except Exception:
                    pass
            source = str(source_path)
            by_file.setdefault(source, []).append((detail, run_count))
        else:
            result.skipped.append({
                "claim": detail["claim_text"][:80],
                "reason": "No source file — cannot quarantine",
            })

    # Process each file
    for source_file, file_candidates in by_file.items():
        path = Path(source_file)
        if not path.exists():
            for detail, run_count in file_candidates:
                result.skipped.append({
                    "claim": detail["claim_text"][:80],
                    "reason": f"Source file not found: {source_file}",
                })
            continue

        content = path.read_text()

        # Check if claim is already in quarantine section
        lines_to_move = []
        run_count_map = {}
        actions_this_file = []

        for detail, run_count in file_candidates:
            claim_text = detail["claim_text"]
            claim_line = _find_claim_line(content, claim_text)

            if claim_line is None:
                result.skipped.append({
                    "claim": claim_text[:80],
                    "reason": "Line not found in source file",
                })
                continue

            # Skip if already in quarantine section
            if QUARANTINE_SECTION in content:
                section_start = content.index(QUARANTINE_SECTION)
                line_start = content.find(claim_line)
                if line_start >= 0 and line_start > section_start:
                    result.skipped.append({
                        "claim": claim_text[:80],
                        "reason": "Already in quarantine section",
                    })
                    continue

            lines_to_move.append(claim_line)
            run_count_map[claim_line] = run_count

            verification_hint = _suggest_verification(
                detail.get("claim_type", ""),
                claim_text,
            )

            action = QuarantineAction(
                claim_text=claim_text,
                claim_type=detail.get("claim_type", "unknown"),
                source_file=detail.get("source_file"),
                run_count=run_count,
                verification_hint=verification_hint,
                moved=False,
            )
            result.quarantined.append(action)
            actions_this_file.append(action)

        # Move lines in the file
        if lines_to_move and not dry_run:
            moved_count = _move_to_quarantine_section(
                source_file, lines_to_move, run_count_map
            )
            # Update action records for this file
            for action in actions_this_file:
                action.moved = moved_count > 0

    # Update tracker status for quarantined claims
    if result.quarantined and not dry_run:
        try:
            from .tracker import update_claim_status, _hash_claim
            for action in result.quarantined:
                if action.moved:
                    claim_hash = _hash_claim(action.claim_text)
                    update_claim_status(
                        claim_hash, "quarantined",
                        evidence=f"Quarantined after {action.run_count} gate runs",
                        method="auto_quarantine",
                    )
        except Exception:
            pass  # Don't fail quarantine if tracker update fails

    # Post Slack notification
    if post_slack and result.quarantined and not dry_run:
        result.slack_posted = _post_quarantine_slack(result)

    return result


def _post_quarantine_slack(result: QuarantineResult) -> bool:
    """Post quarantine notification to Slack."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("SLACK_BOT_TOKEN not set — skipping Slack quarantine notification",
              file=sys.stderr)
        return False

    channel = os.environ.get("CONFAB_SLACK_CHANNEL")
    if not channel:
        from .config import load_ia_defaults_module
        ia = load_ia_defaults_module()
        if ia is not None and hasattr(ia, "SLACK_CHANNEL"):
            channel = ia.SLACK_CHANNEL
        else:
            print("CONFAB_SLACK_CHANNEL not set — skipping Slack notification",
                  file=sys.stderr)
            return False

    try:
        from slack_sdk import WebClient
        client = WebClient(token=token)
        message = result.format_slack()
        client.chat_postMessage(
            channel=channel,
            text=message,
        )
        return True
    except Exception as e:
        print(f"Slack quarantine notification failed: {e}", file=sys.stderr)
        return False
