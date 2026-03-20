#!/usr/bin/env python3
"""CLI for the confabulation framework.

Usage:
    # Run the cascade gate on default files (configured or auto-detected)
    python core/confab/cli.py gate
    confab gate                          # when pip-installed

    # Run gate on specific file
    confab gate --file path/to/priorities.md

    # Check inline text for claims
    confab check "Audio is blocked on OPENAI_API_KEY"

    # Extract claims from a file (without verifying)
    confab extract path/to/priorities.md

    # Quick one-line summary (for embedding in prompts)
    confab quick

    # Identify stale build sections to prune
    confab prune
    confab prune --verbose  # show dead file references

    # Show tracked claims by staleness (persistent across gate runs)
    confab sweep
    confab sweep --remove-stale  # remove stale claims
    confab sweep --stats          # tracker statistics
    confab sweep --history        # gate run history

    # System health dashboard (gate + supports + coverage)
    confab report
    confab report --json
    confab report --slack   # concise Slack-friendly output

    # Check knowledge tree structural integrity (zombie/weakened entries)
    confab check-supports
    confab check-supports --json
    confab check-supports --slack

    # Full JSON output
    confab gate --json

    # Use a specific config file
    confab gate --config /path/to/confab.toml
"""

import argparse
import json
import sys
from pathlib import Path

# Dual import: works both as pip-installed package and as direct script invocation.
try:
    from .claims import extract_claims, extract_claims_from_file, summarize_claims, BUILD_HEADER_RE, FILE_PATH_RE
    from .config import get_config, load_config, set_config
    from .gate import run_gate, quick_check
    from .tracker import (
        get_all_tracked, get_stale_claims, get_run_history,
        get_stats, remove_stale, remove_claims,
    )
    from .verify import verify_claim, verify_all, verify_file_exists, summarize_outcomes
except ImportError:
    # Running as script directly (python core/confab/cli.py)
    _script_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(_script_dir.parent.parent))
    from core.confab.claims import extract_claims, extract_claims_from_file, summarize_claims, BUILD_HEADER_RE, FILE_PATH_RE
    from core.confab.config import get_config, load_config, set_config
    from core.confab.gate import run_gate, quick_check
    from core.confab.tracker import (
        get_all_tracked, get_stale_claims, get_run_history,
        get_stats, remove_stale, remove_claims,
    )
    from core.confab.verify import verify_claim, verify_all, verify_file_exists, summarize_outcomes

# Lazy import for supports (avoids loading tree JSON at module import time)
def _get_check_supports():
    try:
        from .supports import check_supports
    except ImportError:
        from core.confab.supports import check_supports
    return check_supports


def cmd_gate(args):
    """Run the cascade gate."""
    files = [args.file] if args.file else None
    report = run_gate(files=files)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_report())

    # Exit code: 1 if failures found
    if report.has_failures:
        sys.exit(1)


def cmd_check(args):
    """Check inline text for claims and verify them."""
    text = args.text
    claims = extract_claims(text, source_file="<cli>")

    if not claims:
        print("No verifiable claims found in text.")
        return

    print(f"Found {len(claims)} claim(s):\n")

    outcomes = verify_all(claims)
    for outcome in outcomes:
        status_icon = {
            "passed": "  ",
            "failed": "  ",
            "inconclusive": "  ?",
            "skipped": "  -",
        }.get(outcome.result.value, "  ?")

        print(f"{status_icon} [{outcome.claim.claim_type.value}] {outcome.claim.text[:100]}")
        if outcome.result.value in ("failed", "passed"):
            for line in outcome.evidence.strip().split('\n'):
                print(f"    {line.strip()}")
        print()


def cmd_extract(args):
    """Extract claims from a file without verifying."""
    claims = extract_claims_from_file(args.file)

    if not claims:
        print(f"No claims found in {args.file}")
        return

    summary = summarize_claims(claims)

    if args.json:
        print(json.dumps({
            "summary": summary,
            "claims": [c.to_dict() for c in claims],
        }, indent=2))
        return

    print(f"# Claims in {args.file}")
    print(f"\nTotal: {summary['total']}")
    print(f"Auto-verifiable: {summary['auto_verifiable']}")
    print(f"Untagged: {summary['untagged']}")
    print(f"\nBy type: {json.dumps(summary['by_type'], indent=2)}")
    print(f"\nBy verifiability: {json.dumps(summary['by_verifiability'], indent=2)}")

    print("\n## Claims\n")
    for claim in claims:
        vtag = f" {claim.verification_tag}" if claim.verification_tag else ""
        age = f" (age: {claim.age_builds} builds)" if claim.age_builds > 0 else ""
        print(f"[{claim.verifiability.value}] [{claim.claim_type.value}]{vtag}{age}")
        print(f"  {claim.text[:120]}")
        if claim.extracted_paths:
            print(f"  paths: {', '.join(claim.extracted_paths)}")
        if claim.extracted_env_vars:
            print(f"  env_vars: {', '.join(claim.extracted_env_vars)}")
        print()


def cmd_quick(args):
    """Print a one-line gate summary."""
    file_path = args.file if args.file else None
    print(quick_check(file_path))


def cmd_prune(args):
    """Identify stale build sections that should be pruned.

    Analyzes priority files and reports which build sections contain
    dead references, how old they are, and recommends which to remove.
    """
    config = get_config()

    default_files = [
        str(config.workspace_root / f) for f in config.files_to_scan
    ]
    files = [args.file] if args.file else default_files

    for file_path in files:
        path = Path(file_path)
        if not path.exists():
            continue

        text = path.read_text()
        lines = text.split('\n')

        # Find build section boundaries
        sections = []
        for i, line in enumerate(lines):
            match = BUILD_HEADER_RE.match(line)
            if match:
                sections.append((i, match.group(0), match.group(1)))

        if not sections:
            print(f"\n{path.name}: No build sections found.")
            continue

        print(f"\n# Prune Report: {path.name}")
        print(f"Build sections: {len(sections)}")
        print(f"Total lines: {len(lines)}")

        if len(sections) <= 3:
            print("  Only 3 or fewer sections — nothing to prune.")
            continue

        # Analyze each section beyond the first 3
        prunable = []
        for idx, (line_num, header, date_str) in enumerate(sections):
            if idx < 3:
                continue  # Keep the 3 most recent

            # Find section end (next header or EOF)
            end_line = sections[idx + 1][0] if idx + 1 < len(sections) else len(lines)
            section_text = '\n'.join(lines[line_num:end_line])
            section_lines = end_line - line_num

            # Check for dead file references in this section
            file_refs = []
            for match in FILE_PATH_RE.finditer(section_text):
                p = match.group(1) or match.group(2)
                if p:
                    resolved = config.workspace_root / p
                    if not resolved.exists():
                        file_refs.append(p)

            prunable.append({
                'header': header,
                'date': date_str,
                'line_start': line_num + 1,
                'line_end': end_line,
                'lines': section_lines,
                'dead_refs': file_refs,
            })

        if not prunable:
            print("  No prunable sections found.")
            continue

        total_prunable_lines = sum(s['lines'] for s in prunable)
        total_dead_refs = sum(len(s['dead_refs']) for s in prunable)

        print(f"\n## Prunable sections: {len(prunable)} ({total_prunable_lines} lines, {total_dead_refs} dead references)")
        print()

        for s in prunable:
            dead_count = len(s['dead_refs'])
            dead_marker = f" \u26a0\ufe0f {dead_count} dead file refs" if dead_count > 0 else ""
            print(f"  [{s['lines']:3d} lines] {s['header']}{dead_marker}")
            if dead_count > 0 and args.verbose:
                for ref in s['dead_refs'][:5]:
                    print(f"             \u2717 {ref}")
                if dead_count > 5:
                    print(f"             ... and {dead_count - 5} more")

        print(f"\n  **Recommendation:** Remove {len(prunable)} sections ({total_prunable_lines} lines).")
        print(f"  Keep latest 3 builds + non-build sections (strategic direction, portfolio, dates, standing items).")


def cmd_report(args):
    """Print a system health dashboard combining gate + supports analysis."""
    check_supports = _get_check_supports()

    # Run gate
    files = [args.file] if args.file else None
    gate_report = run_gate(files=files)

    # Run supports check
    try:
        supports_report = check_supports()
    except Exception as e:
        supports_report = None
        supports_error = str(e)

    if args.json:
        result = {
            "gate": gate_report.to_dict(),
            "supports": supports_report.to_dict() if supports_report else {"error": supports_error},
        }
        if supports_report:
            total = gate_report.total_claims + supports_report.checked_entries
            verified = gate_report.passed + supports_report.healthy
            result["coverage"] = {
                "total_checked": total,
                "verified_healthy": verified,
                "percentage": round(verified / total * 100, 1) if total > 0 else 100.0,
            }
        print(json.dumps(result, indent=2))
        if gate_report.has_failures or (supports_report and supports_report.has_zombies):
            sys.exit(1)
        return

    if args.slack:
        print(_format_health_slack(gate_report, supports_report))
        if gate_report.has_failures or (supports_report and supports_report.has_zombies):
            sys.exit(1)
        return

    # Terminal dashboard
    print(_format_health_dashboard(gate_report, supports_report))
    if gate_report.has_failures or (supports_report and supports_report.has_zombies):
        sys.exit(1)


def _format_health_dashboard(gate_report, supports_report):
    """Format a comprehensive terminal health dashboard."""
    lines = []
    lines.append("=" * 52)
    lines.append("  CONFAB SYSTEM HEALTH REPORT")
    lines.append("=" * 52)

    # --- Claims section ---
    lines.append("")
    lines.append("CLAIMS")
    lines.append(f"  Total: {gate_report.total_claims}  |  "
                 f"Verified: {gate_report.passed}  |  "
                 f"Failed: {gate_report.failed}  |  "
                 f"Stale: {gate_report.stale_claims}")
    lines.append(f"  Inconclusive: {gate_report.inconclusive}  |  "
                 f"Skipped: {gate_report.skipped}")

    if gate_report.auto_verified > 0:
        claim_pct = gate_report.passed / gate_report.auto_verified * 100
        lines.append(f"  Pass rate: {claim_pct:.0f}% ({gate_report.passed}/{gate_report.auto_verified} auto-verified)")
    lines.append(f"  Files: {', '.join(gate_report.files_scanned) or '(none)'}")

    if gate_report.has_failures:
        lines.append("")
        lines.append(f"  FAILURES ({gate_report.failed}):")
        for d in gate_report.failed_details[:5]:
            lines.append(f"    x  {d['claim_text'][:80]}")
            ev = d['evidence'].split('\n')[0][:70]
            lines.append(f"       {ev}")
        if len(gate_report.failed_details) > 5:
            lines.append(f"    ...and {len(gate_report.failed_details) - 5} more")

    if gate_report.has_stale:
        lines.append("")
        lines.append(f"  STALE ({gate_report.stale_claims}):")
        for d in gate_report.stale_details[:3]:
            age = d.get('age_builds', '?')
            lines.append(f"    ~  [{age} runs] {d['claim_text'][:70]}")
        if len(gate_report.stale_details) > 3:
            lines.append(f"    ...and {len(gate_report.stale_details) - 3} more")

    # --- Supports section ---
    lines.append("")
    lines.append("-" * 52)
    lines.append("")
    lines.append("KNOWLEDGE TREE SUPPORTS")

    if supports_report is None:
        lines.append("  (unavailable — knowledge tree not found)")
    else:
        lines.append(f"  Entries checked: {supports_report.checked_entries}  |  "
                     f"Zombies: {len(supports_report.zombies)}  |  "
                     f"Weakened: {len(supports_report.weakened)}  |  "
                     f"Healthy: {supports_report.healthy}")
        lines.append(f"  No supports: {supports_report.no_supports}  |  "
                     f"Invalidated: {supports_report.invalidated_count}  |  "
                     f"Total tree: {supports_report.total_entries}")

        if supports_report.zombies:
            zombie_ids = [z.entry_id for z in supports_report.zombies[:10]]
            lines.append(f"  Zombie IDs: {', '.join(zombie_ids)}"
                         + (f" ...+{len(supports_report.zombies) - 10}" if len(supports_report.zombies) > 10 else ""))

        if supports_report.weakened:
            lines.append(f"  Weakened IDs: "
                         + ", ".join(w.entry_id for w in supports_report.weakened[:5])
                         + (f" ...+{len(supports_report.weakened) - 5}" if len(supports_report.weakened) > 5 else ""))

    # --- Coverage section ---
    lines.append("")
    lines.append("-" * 52)
    lines.append("")
    lines.append("VERIFICATION COVERAGE")

    if supports_report:
        total = gate_report.total_claims + supports_report.checked_entries
        verified = gate_report.passed + supports_report.healthy
        pct = verified / total * 100 if total > 0 else 100.0
        lines.append(f"  Claims verified: {gate_report.passed}/{gate_report.total_claims}")
        lines.append(f"  Tree entries healthy: {supports_report.healthy}/{supports_report.checked_entries}")
        lines.append(f"  Combined coverage: {pct:.1f}% ({verified}/{total})")
    else:
        if gate_report.total_claims > 0:
            pct = gate_report.passed / gate_report.total_claims * 100
            lines.append(f"  Claims verified: {gate_report.passed}/{gate_report.total_claims} ({pct:.0f}%)")
        else:
            lines.append(f"  No claims to verify")

    # --- Overall status ---
    lines.append("")
    lines.append("=" * 52)

    has_critical = gate_report.has_failures or (supports_report and supports_report.has_zombies)
    has_warning = gate_report.has_stale or (supports_report and supports_report.has_issues and not supports_report.has_zombies)

    if has_critical:
        status = "CRITICAL"
    elif has_warning:
        status = "WARNING"
    else:
        status = "HEALTHY"

    lines.append(f"  STATUS: {status}")
    lines.append("=" * 52)

    return "\n".join(lines)


def _format_health_slack(gate_report, supports_report):
    """Format a concise Slack-friendly health report."""
    lines = []

    # Gate status
    if gate_report.clean:
        lines.append(f":white_check_mark: Gate CLEAN — {gate_report.total_claims} claims, {gate_report.passed} verified")
    else:
        parts = []
        if gate_report.failed > 0:
            parts.append(f":x: {gate_report.failed} failed")
        if gate_report.stale_claims > 0:
            parts.append(f":hourglass: {gate_report.stale_claims} stale")
        if gate_report.passed > 0:
            parts.append(f":white_check_mark: {gate_report.passed} passed")
        lines.append(" | ".join(parts))

    # Supports status
    if supports_report:
        if not supports_report.has_issues:
            lines.append(f":white_check_mark: Supports CLEAN — {supports_report.checked_entries} entries healthy")
        else:
            parts = []
            if supports_report.zombies:
                parts.append(f":skull: {len(supports_report.zombies)} zombie")
            if supports_report.weakened:
                parts.append(f":warning: {len(supports_report.weakened)} weakened")
            parts.append(f":white_check_mark: {supports_report.healthy} healthy")
            lines.append(" | ".join(parts))

    # Coverage
    if supports_report:
        total = gate_report.total_claims + supports_report.checked_entries
        verified = gate_report.passed + supports_report.healthy
        pct = verified / total * 100 if total > 0 else 100.0
        lines.append(f"Coverage: {pct:.0f}%")

    return "\n".join(lines)


def cmd_sweep(args):
    """Show all tracked claims by staleness. Optionally remove stale ones."""
    if args.stats:
        stats = get_stats()
        print("# Tracker Statistics\n")
        print(f"Total tracked claims: {stats['total_tracked']}")
        print(f"Total gate runs: {stats['total_gate_runs']}")
        if stats['latest_run']:
            print(f"Latest run: {stats['latest_run']}")
        print(f"\nBy status:")
        for status, count in sorted(stats['by_status'].items()):
            print(f"  {status}: {count}")
        return

    if args.history:
        runs = get_run_history(limit=args.history)
        if not runs:
            print("No gate runs recorded yet.")
            return
        print("# Gate Run History\n")
        for run in runs:
            files = json.loads(run['files_scanned']) if run['files_scanned'] else []
            print(f"Run #{run['id']} \u2014 {run['timestamp']}")
            print(f"  Claims: {run['total_claims']} | Passed: {run['passed']} | "
                  f"Failed: {run['failed']} | Stale: {run['stale']}")
            print(f"  New: {run['new_claims']} | Returning: {run['returning_claims']}")
            if files:
                print(f"  Files: {', '.join(files)}")
            print()
        return

    if args.remove_stale:
        threshold = args.threshold or 3
        stale = get_stale_claims(threshold)
        if not stale:
            print(f"No stale claims (threshold: {threshold} runs).")
            return
        print(f"Removing {len(stale)} stale claims:\n")
        for tc in stale:
            print(f"  [{tc.run_count} runs] {tc.claim_text[:100]}")
        removed = remove_stale(threshold)
        print(f"\nRemoved {removed} stale claims from tracker.")
        return

    # Default: show all tracked claims
    threshold = args.threshold or 3
    tracked = get_all_tracked()

    if not tracked:
        print("No claims tracked yet. Run `gate` to start tracking.")
        return

    if args.json:
        print(json.dumps([tc.to_dict() for tc in tracked], indent=2))
        return

    # Group by status
    by_status = {}
    for tc in tracked:
        by_status.setdefault(tc.status, []).append(tc)

    print("# Claim Sweep Report\n")

    stats = get_stats()
    print(f"Total tracked: {stats['total_tracked']} | "
          f"Gate runs: {stats['total_gate_runs']} | "
          f"Stale threshold: {threshold} runs\n")

    status_order = ['stale', 'failed', 'unverified', 'inconclusive', 'new', 'verified', 'expired']
    status_icons = {
        'stale': '\u23f0', 'failed': '\u274c', 'unverified': '\u2753',
        'inconclusive': '\u2754', 'new': '\U0001f195', 'verified': '\u2705', 'expired': '\U0001f480',
    }

    for status in status_order:
        claims = by_status.get(status, [])
        if not claims:
            continue

        icon = status_icons.get(status, '\u2022')
        print(f"## {icon} {status.upper()} ({len(claims)})\n")

        for tc in claims:
            run_info = f"[{tc.run_count} runs]"
            source = f" \u2014 {tc.source_file}" if tc.source_file else ""
            verified = ""
            if tc.last_verified:
                verified = f" (last verified: {tc.last_verified[:10]})"
            print(f"  {run_info} [{tc.claim_type}]{source}{verified}")
            print(f"    {tc.claim_text[:120]}")
            print()


def main():
    parser = argparse.ArgumentParser(
        description="Confabulation Framework \u2014 structural detection and prevention for multi-agent systems",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  confab gate
  confab gate --file path/to/priorities.md
  confab check "Audio blocked on OPENAI_API_KEY"
  confab extract path/to/priorities.md
  confab quick
  confab prune
  confab sweep --stats
        """,
    )
    parser.add_argument("--config", "-c", help="Path to confab.toml config file")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # gate
    gate_parser = subparsers.add_parser("gate", help="Run the cascade gate")
    gate_parser.add_argument("--file", "-f", help="Specific file to scan")
    gate_parser.add_argument("--json", "-j", action="store_true", help="JSON output")

    # check
    check_parser = subparsers.add_parser("check", help="Check inline text")
    check_parser.add_argument("text", help="Text to check for claims")

    # extract
    extract_parser = subparsers.add_parser("extract", help="Extract claims from a file")
    extract_parser.add_argument("file", help="File to extract claims from")
    extract_parser.add_argument("--json", "-j", action="store_true", help="JSON output")

    # quick
    quick_parser = subparsers.add_parser("quick", help="One-line gate summary")
    quick_parser.add_argument("--file", "-f", help="Specific file to check")

    # prune
    prune_parser = subparsers.add_parser("prune", help="Identify stale build sections to remove")
    prune_parser.add_argument("--file", "-f", help="Specific file to analyze")
    prune_parser.add_argument("--verbose", "-v", action="store_true", help="Show dead file references")

    # report
    report_parser = subparsers.add_parser("report", help="System health dashboard (gate + supports + coverage)")
    report_parser.add_argument("--file", "-f", help="Specific file to scan")
    report_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    report_parser.add_argument("--slack", action="store_true", help="Concise Slack-friendly output")

    # sweep
    sweep_parser = subparsers.add_parser("sweep", help="Show tracked claims by staleness")
    sweep_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    sweep_parser.add_argument("--remove-stale", action="store_true", help="Remove stale claims from tracker")
    sweep_parser.add_argument("--threshold", "-t", type=int, help="Staleness threshold (default: 3 runs)")
    sweep_parser.add_argument("--stats", action="store_true", help="Show tracker statistics only")
    sweep_parser.add_argument("--history", type=int, nargs="?", const=10, help="Show gate run history (default: 10)")

    # check-supports
    supports_parser = subparsers.add_parser(
        "check-supports",
        help="Check knowledge tree for zombie/weakened entries (degraded supports)",
    )
    supports_parser.add_argument("--tree", "-t", help="Path to KNOWLEDGE_TREE.json (default: auto-detect)")
    supports_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    supports_parser.add_argument("--slack", action="store_true", help="Slack-friendly output")

    # init — generate a starter confab.toml
    init_parser = subparsers.add_parser("init", help="Generate a starter confab.toml in the current directory")

    args = parser.parse_args()

    # Apply config override if specified
    if args.config:
        config = load_config(config_path=Path(args.config))
        set_config(config)

    if args.command == "gate":
        cmd_gate(args)
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "extract":
        cmd_extract(args)
    elif args.command == "quick":
        cmd_quick(args)
    elif args.command == "prune":
        cmd_prune(args)
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "sweep":
        cmd_sweep(args)
    elif args.command == "check-supports":
        cmd_check_supports(args)
    elif args.command == "init":
        cmd_init(args)
    else:
        parser.print_help()


def cmd_check_supports(args):
    """Check knowledge tree for entries with degraded support structures."""
    check_supports = _get_check_supports()
    report = check_supports(tree_path=args.tree)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.slack:
        print(report.format_slack())
    else:
        print(report.format_report())

    if report.has_zombies:
        sys.exit(1)


def cmd_init(args):
    """Generate a starter confab.toml in the current directory."""
    target = Path.cwd() / "confab.toml"
    if target.exists():
        print(f"confab.toml already exists at {target}")
        sys.exit(1)

    target.write_text("""\
[confab]
# Files to scan for carry-forward claims (relative to workspace root)
files_to_scan = [
    # "docs/priorities.md",
    # "notes/handoff.md",
]

# How many gate runs before unverified claims are flagged stale
stale_threshold = 3

# Where to store the tracker database (relative to workspace root)
db_path = "confab_tracker.db"

# Known environment variable names to detect in claims
[confab.env_vars]
known = [
    # "OPENAI_API_KEY",
    # "DATABASE_URL",
]

# Pipeline output mappings: script name -> expected output paths
# [confab.pipelines]
# "my_pipeline.py" = ["output/data/", "output/report.json"]

# Name-based pipeline matching for status claims without explicit paths
# [confab.pipeline_names]
# "data pipeline" = "my_pipeline.py"

# Count verification sources — verify numeric claims against data files
# [confab.count_sources.my_entries]
# file = "data/entries.json"
# type = "json_array"       # count items in a JSON array
# json_path = "entries"     # key to the array
#
# [confab.count_sources.task_queue]
# file = "queue.md"
# type = "regex_count"      # count regex matches
# pattern = "^###\\\\s+Task\\\\s+\\\\d+"
# rate_per_day = 3.0        # for runway estimates
""")
    print(f"Created {target}")
    print("Edit the file to configure your scan targets, then run: confab gate")


if __name__ == "__main__":
    main()
