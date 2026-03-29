#!/usr/bin/env python3
"""Scan any markdown file for unverified claims and check them against reality.

This is the simplest way to use confab programmatically: point it at a file,
get back a list of what's true, what's false, and what couldn't be checked.

Usage:
    pip install confab-framework
    python standalone_scan.py path/to/notes.md
    python standalone_scan.py --text "Config at /etc/app.conf is ready"
"""

import argparse
import sys
import tempfile
from pathlib import Path

from confab import (
    ConfabConfig,
    ConfabGate,
    extract_claims,
    extract_claims_from_file,
    verify_all,
)


def scan_file(filepath: str) -> int:
    """Scan a file for claims and verify them. Returns exit code."""
    path = Path(filepath)
    if not path.exists():
        print(f"Error: {filepath} not found")
        return 1

    print(f"Scanning {filepath}...\n")

    # Extract claims
    claims = extract_claims_from_file(str(path))
    if not claims:
        print("No verifiable claims found.")
        return 0

    print(f"Found {len(claims)} claims:\n")

    # Verify each claim against reality
    outcomes = verify_all(claims)

    passed = failed = inconclusive = skipped = 0
    for outcome in outcomes:
        status = outcome.result.value
        icon = {"passed": "+", "failed": "!", "inconclusive": "?", "skipped": "-"}[status]

        print(f"  [{icon}] {outcome.claim.text[:80]}")
        print(f"      {status.upper()}: {outcome.evidence}")
        print()

        if status == "passed":
            passed += 1
        elif status == "failed":
            failed += 1
        elif status == "inconclusive":
            inconclusive += 1
        else:
            skipped += 1

    # Summary
    print("---")
    print(f"Total: {len(claims)}  |  Passed: {passed}  |  Failed: {failed}  "
          f"|  Inconclusive: {inconclusive}  |  Skipped: {skipped}")

    if failed:
        print(f"\n{failed} claim(s) contradict reality — investigate before trusting.")
        return 1

    return 0


def scan_text(text: str) -> int:
    """Scan inline text for claims and verify them. Returns exit code."""
    print(f"Scanning text: \"{text[:60]}{'...' if len(text) > 60 else ''}\"\n")

    claims = extract_claims(text)
    if not claims:
        print("No verifiable claims found.")
        return 0

    outcomes = verify_all(claims)
    failed = 0

    for outcome in outcomes:
        status = outcome.result.value.upper()
        print(f"  [{status}] {outcome.claim.text[:80]}")
        print(f"    Evidence: {outcome.evidence}")
        if outcome.result.value == "failed":
            failed += 1

    return 1 if failed else 0


def scan_with_gate(filepath: str) -> int:
    """Use the high-level ConfabGate API for a full report."""
    path = Path(filepath)
    if not path.exists():
        print(f"Error: {filepath} not found")
        return 1

    # Configure the gate for the file's directory
    config = ConfabConfig(
        workspace_root=path.parent,
        files_to_scan=[str(path)],
    )
    gate = ConfabGate(config=config)

    # Run without tracking (no persistent DB in this example)
    report = gate.run(track=False)

    # Print the formatted report
    print(report.format_report())

    if report.has_failures:
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Scan markdown for unverified claims and check them against reality."
    )
    parser.add_argument("file", nargs="?", help="Markdown file to scan")
    parser.add_argument("--text", help="Inline text to scan (instead of a file)")
    parser.add_argument(
        "--full-report", action="store_true",
        help="Use ConfabGate API for a full report (includes staleness tracking)"
    )
    args = parser.parse_args()

    if args.text:
        sys.exit(scan_text(args.text))
    elif args.file:
        if args.full_report:
            sys.exit(scan_with_gate(args.file))
        else:
            sys.exit(scan_file(args.file))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
