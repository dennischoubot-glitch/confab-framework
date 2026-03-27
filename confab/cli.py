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

    # Lint priority files for claim hygiene
    confab lint
    confab lint path/to/file.md
    confab lint --json

    # Show tracked claims by staleness (persistent across gate runs)
    confab sweep
    confab sweep --remove-stale  # remove stale claims
    confab sweep --stats          # tracker statistics
    confab sweep --history        # gate run history

    # System health dashboard (gate + supports + coverage)
    confab report
    confab report --json
    confab report --slack   # concise Slack-friendly output

    # Trace the propagation path of a specific claim
    confab trace "OPENAI_API_KEY"
    confab trace abc123def456    # by hash
    confab trace "audio" --json

    # Cascade depth statistics across all claims
    confab cascade
    confab cascade --json

    # Check knowledge tree structural integrity (zombie/weakened entries)
    confab check-supports
    confab check-supports --json
    confab check-supports --slack

    # Scan knowledge tree for factual health (expired/perishable/unverified)
    confab tree
    confab tree --json
    confab tree --slack
    confab tree --stale-days 7

    # Auto-fix stale and failed claims
    confab fix
    confab fix --dry-run   # preview without modifying
    confab fix --file path/to/priorities.md

    # Fix perishable observations (add expires dates to entries with dates/prices/%)
    confab fix --perishable              # preview table (dry-run by default)
    confab fix --perishable --apply      # write changes to KNOWLEDGE_TREE.json
    confab fix --perishable --json       # JSON output

    # Auto-quarantine claims persisting 5+ gate runs
    confab quarantine                    # quarantine + Slack notification
    confab quarantine --dry-run          # preview without modifying files
    confab quarantine --threshold 10     # custom threshold
    confab gate --quarantine             # run gate with quarantine in one step

    # Triage — unified severity-ranked remediation view
    confab triage                        # rank all issues, suggest fixes
    confab triage --source gate          # gate issues only
    confab triage --source tree          # tree issues only
    confab triage --category tree_no_ttl # filter to one category
    confab triage --limit 5              # show top 5 only
    confab triage --slack                # concise Slack output
    confab triage --json                 # machine-readable output

    # Comprehensive audit report (tracker DB summary)
    confab audit
    confab audit --json

    # CI mode — markdown output, proper exit codes for pipelines
    confab ci
    confab ci --strict               # exit 2 on stale claims
    confab ci --output report.md     # write markdown to file (for PR comments)
    confab ci --no-track             # don't persist to tracker DB

    # Full JSON output
    confab gate --json

    # Use a specific config file
    confab gate --config /path/to/confab.toml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Dual import: works both as pip-installed package and as direct script invocation.
try:
    from .claims import extract_claims, extract_claims_from_file, summarize_claims, BUILD_HEADER_RE, FILE_PATH_RE
    from .config import get_config, load_config, set_config, parse_volatility
    from .gate import run_gate, quick_check
    from .tracker import (
        get_all_tracked, get_stale_claims, get_run_history,
        get_stats, remove_stale, remove_claims,
        get_cascade_stats, trace_claim, get_audit_data,
        record_fix_action, get_fix_history, update_claim_status,
        _hash_claim,
    )
    from .verify import verify_claim, verify_all, verify_file_exists, summarize_outcomes
    from .lint import run_lint
except ImportError:
    # Running as script directly (python core/confab/cli.py) — add parent dirs to path
    _script_dir = Path(__file__).resolve().parent
    # Prefer the local source over any installed package
    _parent = str(_script_dir.parent)
    if _parent in sys.path:
        sys.path.remove(_parent)
    sys.path.insert(0, _parent)
    from confab.claims import extract_claims, extract_claims_from_file, summarize_claims, BUILD_HEADER_RE, FILE_PATH_RE
    from confab.config import get_config, load_config, set_config, parse_volatility
    from confab.gate import run_gate, quick_check
    from confab.tracker import (
        get_all_tracked, get_stale_claims, get_run_history,
        get_stats, remove_stale, remove_claims,
        get_cascade_stats, trace_claim, get_audit_data,
        record_fix_action, get_fix_history, update_claim_status,
        _hash_claim,
    )
    from confab.verify import verify_claim, verify_all, verify_file_exists, summarize_outcomes
    from confab.lint import run_lint

# Lazy import for supports (avoids loading tree JSON at module import time)
def _get_check_supports():
    try:
        from .supports import check_supports
    except ImportError:
        from confab.supports import check_supports
    return check_supports


def _get_check_tree():
    try:
        from .tree import check_tree
    except ImportError:
        from confab.tree import check_tree
    return check_tree


def cmd_gate(args):
    """Run the cascade gate."""
    files = [args.file] if args.file else None
    vol = parse_volatility(getattr(args, 'volatility', None))
    report = run_gate(files=files, volatility=vol)

    if args.json and not getattr(args, 'quarantine', False):
        print(json.dumps(report.to_dict(), indent=2))
    elif not getattr(args, 'quarantine', False):
        print(report.format_report())
        # Helpful hint when nothing was scanned
        if not report.files_scanned and not args.file:
            print("\nNo files configured to scan.")
            config = get_config()
            if not (config.workspace_root / "confab.toml").exists():
                print("Run `confab init` to create a confab.toml, then configure your scan targets.")
            else:
                print("Edit confab.toml and uncomment or add files to `files_to_scan`.")

    # Run quarantine if requested
    if getattr(args, 'quarantine', False):
        try:
            from .quarantine import run_quarantine
        except ImportError:
            from confab.quarantine import run_quarantine
        threshold = getattr(args, 'quarantine_threshold', 5)
        dry_run = getattr(args, 'dry_run', False)
        q_result = run_quarantine(
            report,
            threshold=threshold,
            post_slack=not dry_run,
            dry_run=dry_run,
        )
        if args.json:
            output = report.to_dict()
            output["quarantine"] = q_result.to_dict()
            print(json.dumps(output, indent=2))
        else:
            print(report.format_report())
            if q_result.quarantined:
                print("\n" + q_result.format_report())
            elif q_result.candidates == 0:
                print(f"\nNo claims meet quarantine threshold ({threshold} runs).")

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
    """Print a system health dashboard combining gate + supports + tree analysis."""
    check_supports = _get_check_supports()
    check_tree = _get_check_tree()

    # Run gate
    files = [args.file] if args.file else None
    gate_report = run_gate(files=files)

    # Run supports check
    try:
        supports_report = check_supports()
    except Exception as e:
        supports_report = None
        supports_error = str(e)

    # Run tree health check
    try:
        tree_report = check_tree()
    except Exception as e:
        tree_report = None
        tree_error = str(e)

    if args.json:
        result = {
            "gate": gate_report.to_dict(),
            "supports": supports_report.to_dict() if supports_report else {"error": supports_error},
            "tree": tree_report.to_dict() if tree_report else {"error": tree_error},
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
        print(_format_health_slack(gate_report, supports_report, tree_report))
        if gate_report.has_failures or (supports_report and supports_report.has_zombies):
            sys.exit(1)
        return

    # Terminal dashboard
    print(_format_health_dashboard(gate_report, supports_report, tree_report))
    if gate_report.has_failures or (supports_report and supports_report.has_zombies):
        sys.exit(1)


def _format_health_dashboard(gate_report, supports_report, tree_report=None):
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

    # --- Cascade section ---
    try:
        cascade_stats = get_cascade_stats()
        if cascade_stats["total_tracked"] > 0:
            lines.append("")
            lines.append(f"  Cascade: avg depth {cascade_stats['avg_depth']} | "
                         f"max depth {cascade_stats['max_depth']} | "
                         f"{cascade_stats['total_cascaded']} propagated | "
                         f"{cascade_stats['resolved_count']} resolved")
    except Exception:
        pass

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
                     f"Degraded: {len(supports_report.degraded)}  |  "
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

        if supports_report.degraded:
            lines.append(f"  Degraded: {len(supports_report.degraded)} entries with some dead supports (run check-supports for details)")

    # --- Tree factual health section ---
    lines.append("")
    lines.append("-" * 52)
    lines.append("")
    lines.append("KNOWLEDGE TREE FACTUAL HEALTH")

    if tree_report is None:
        lines.append("  (unavailable -- knowledge tree not found)")
    else:
        lines.append(f"  Observations: {tree_report.total_observations}  |  "
                     f"Expired: {len(tree_report.expired)}  |  "
                     f"Stale-unverified: {len(tree_report.stale_unverified)}  |  "
                     f"No-TTL: {len(tree_report.perishable_no_ttl)}")
        lines.append(f"  TTL coverage: {tree_report.ttl_coverage:.1f}%  |  "
                     f"Verified coverage: {tree_report.verified_coverage:.1f}%")

        if tree_report.expired:
            lines.append("")
            lines.append(f"  EXPIRED ({len(tree_report.expired)}):")
            for e in tree_report.expired[:5]:
                lines.append(f"    x  {e.entry_id} (expired {e.expires}) -- {e.content[:60]}")
            if len(tree_report.expired) > 5:
                lines.append(f"    ...and {len(tree_report.expired) - 5} more")

        if tree_report.stale_unverified:
            lines.append("")
            lines.append(f"  STALE UNVERIFIED ({len(tree_report.stale_unverified)}):")
            for s in tree_report.stale_unverified[:5]:
                lines.append(f"    ~  {s.entry_id} -- {s.content[:70]}")
            if len(tree_report.stale_unverified) > 5:
                lines.append(f"    ...and {len(tree_report.stale_unverified) - 5} more")

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

    if tree_report:
        lines.append(f"  Tree TTL coverage: {tree_report.ttl_coverage:.1f}%")

    # --- Overall status ---
    lines.append("")
    lines.append("=" * 52)

    has_critical = gate_report.has_failures or (supports_report and supports_report.has_zombies)
    has_warning = (gate_report.has_stale
                   or (supports_report and supports_report.has_issues and not supports_report.has_zombies)
                   or (tree_report and tree_report.has_expired))

    if has_critical:
        status = "CRITICAL"
    elif has_warning:
        status = "WARNING"
    else:
        status = "HEALTHY"

    lines.append(f"  STATUS: {status}")
    lines.append("=" * 52)

    return "\n".join(lines)


def _format_health_slack(gate_report, supports_report, tree_report=None):
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

    # Tree factual health
    if tree_report:
        if not tree_report.has_issues:
            lines.append(f":white_check_mark: Tree CLEAN — {tree_report.total_observations} obs, {tree_report.ttl_coverage:.0f}% TTL")
        else:
            parts = []
            if tree_report.expired:
                parts.append(f":x: {len(tree_report.expired)} expired")
            if tree_report.stale_unverified:
                parts.append(f":warning: {len(tree_report.stale_unverified)} stale-unverified")
            if tree_report.perishable_no_ttl:
                parts.append(f":hourglass: {len(tree_report.perishable_no_ttl)} no-TTL")
            lines.append(" | ".join(parts))
            lines.append(f"TTL coverage: {tree_report.ttl_coverage:.0f}%")

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
  confab lint
  confab sweep --stats
  confab ci
  confab ci --strict --output report.md
        """,
    )
    parser.add_argument("--config", "-c", help="Path to confab.toml config file")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # gate
    gate_parser = subparsers.add_parser("gate", help="Run the cascade gate")
    gate_parser.add_argument("--file", "-f", help="Specific file to scan")
    gate_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    gate_parser.add_argument(
        "--volatility", "-V",
        help="Environmental volatility: low/medium/high or 0.0-1.0. "
             "High = looser thresholds (faster adaptation), low = tighter (integrity).",
    )
    gate_parser.add_argument(
        "--quarantine", action="store_true",
        help="Auto-quarantine claims persisting 5+ gate runs: move to QUARANTINED "
             "section in source files and post Slack notification.",
    )
    gate_parser.add_argument(
        "--quarantine-threshold", type=int, default=5,
        help="Run count threshold for quarantine (default: 5).",
    )
    gate_parser.add_argument(
        "--dry-run", action="store_true",
        help="With --quarantine: preview quarantine actions without modifying files.",
    )

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

    # lint
    lint_parser = subparsers.add_parser("lint", help="Check claim hygiene in priority/handoff files")
    lint_parser.add_argument("file", nargs="?", help="Specific file to lint (default: files_to_scan from confab.toml)")
    lint_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    lint_parser.add_argument("--threshold", "-t", type=int, help="Staleness threshold for [unverified] claims (default: 3)")

    # check-supports
    supports_parser = subparsers.add_parser(
        "check-supports",
        help="Check knowledge tree for zombie/weakened entries (degraded supports)",
    )
    supports_parser.add_argument("--tree", "-t", help="Path to KNOWLEDGE_TREE.json (default: auto-detect)")
    supports_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    supports_parser.add_argument("--slack", action="store_true", help="Slack-friendly output")
    supports_parser.add_argument("--fix", action="store_true", help="Auto-invalidate zombie entries (all supports dead)")
    supports_parser.add_argument("--dry-run", action="store_true", help="With --fix: show what would be invalidated without modifying")

    # tree — knowledge tree factual health scan
    tree_parser = subparsers.add_parser(
        "tree",
        help="Scan knowledge tree for factual health issues (expired, perishable, unverified)",
    )
    tree_parser.add_argument("--tree", "-t", help="Path to KNOWLEDGE_TREE.json (default: auto-detect)")
    tree_parser.add_argument("--stale-days", "-s", type=int, help="Days before unverified obs are flagged stale (default: 14)")
    tree_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    tree_parser.add_argument("--slack", action="store_true", help="Slack-friendly output")

    # trace — trace the propagation of a specific claim
    trace_parser = subparsers.add_parser("trace", help="Trace propagation path of a specific claim")
    trace_parser.add_argument("query", help="Claim hash or text substring to search for")
    trace_parser.add_argument("--json", "-j", action="store_true", help="JSON output")

    # cascade — cascade depth statistics
    cascade_parser = subparsers.add_parser("cascade", help="Show cascade depth statistics")
    cascade_parser.add_argument("--json", "-j", action="store_true", help="JSON output")

    # audit — comprehensive audit report from tracker DB
    audit_parser = subparsers.add_parser("audit", help="Comprehensive audit summary: claims, cascades, resolution rate")
    audit_parser.add_argument("--json", "-j", action="store_true", help="JSON output")

    # ci — CI-friendly gate with exit codes and markdown output
    ci_parser = subparsers.add_parser("ci", help="Run gate for CI pipelines (markdown output, exit codes)")
    ci_parser.add_argument("--file", "-f", help="Specific file to scan")
    ci_parser.add_argument("--json", "-j", action="store_true", help="JSON output instead of markdown")
    ci_parser.add_argument("--output", "-o", help="Write markdown report to file (for PR comments)")
    ci_parser.add_argument("--stale-threshold", type=int, help="Staleness threshold (default: 3 runs)")
    ci_parser.add_argument("--strict", action="store_true", help="Exit 2 on stale claims (default: only exit 1 on failures)")
    ci_parser.add_argument("--no-track", action="store_true", help="Don't record this run in the tracker DB")
    ci_parser.add_argument(
        "--volatility", "-V",
        help="Environmental volatility: low/medium/high or 0.0-1.0.",
    )

    # fix — automated stale/failed claim remediation
    fix_parser = subparsers.add_parser("fix", help="Auto-fix stale and failed claims (re-verify, update tags, delete dead lines)")
    fix_parser.add_argument("--file", "-f", help="Specific file to scan and fix")
    fix_parser.add_argument("--dry-run", action="store_true", help="Preview fixes without modifying files or DB")
    fix_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    fix_parser.add_argument("--perishable", action="store_true",
                            help="Fix perishable observations: add expires dates to tree entries with dates/prices/%% but no TTL")
    fix_parser.add_argument("--apply", action="store_true",
                            help="With --perishable: actually write changes (default is dry-run preview)")
    fix_parser.add_argument("--tree", "-t", help="Path to KNOWLEDGE_TREE.json (for --perishable)")

    # quarantine — standalone quarantine subcommand
    quarantine_parser = subparsers.add_parser(
        "quarantine",
        help="Auto-quarantine claims persisting 5+ gate runs without verification",
    )
    quarantine_parser.add_argument("--file", "-f", help="Specific file to scan")
    quarantine_parser.add_argument("--threshold", "-t", type=int, default=5,
                                   help="Run count threshold for quarantine (default: 5)")
    quarantine_parser.add_argument("--dry-run", action="store_true",
                                   help="Preview without modifying files or posting to Slack")
    quarantine_parser.add_argument("--no-slack", action="store_true",
                                   help="Skip Slack notification")
    quarantine_parser.add_argument("--json", "-j", action="store_true", help="JSON output")

    # triage — unified severity-ranked remediation view
    triage_parser = subparsers.add_parser(
        "triage",
        help="Rank all confab issues by severity, suggest fixes, enable batch operations",
    )
    triage_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    triage_parser.add_argument("--slack", action="store_true", help="Concise Slack-friendly output")
    triage_parser.add_argument("--limit", "-n", type=int, default=20,
                               help="Max items to show (default: 20)")
    triage_parser.add_argument("--source", "-s", choices=["gate", "tree", "supports", "all"],
                               default="all", help="Which data source to triage (default: all)")
    triage_parser.add_argument("--category", help="Filter to specific category (e.g. tree_no_ttl, gate_stale)")

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
    elif args.command == "lint":
        cmd_lint(args)
    elif args.command == "check-supports":
        cmd_check_supports(args)
    elif args.command == "tree":
        cmd_tree(args)
    elif args.command == "trace":
        cmd_trace(args)
    elif args.command == "cascade":
        cmd_cascade(args)
    elif args.command == "audit":
        cmd_audit(args)
    elif args.command == "ci":
        cmd_ci(args)
    elif args.command == "fix":
        if getattr(args, 'perishable', False):
            cmd_fix_perishable(args)
        else:
            cmd_fix(args)
    elif args.command == "quarantine":
        cmd_quarantine(args)
    elif args.command == "triage":
        cmd_triage(args)
    elif args.command == "init":
        cmd_init(args)
    else:
        parser.print_help()


def cmd_quarantine(args):
    """Standalone quarantine: run gate then quarantine persistent stale claims."""
    try:
        from .quarantine import run_quarantine
    except ImportError:
        from confab.quarantine import run_quarantine

    files = [args.file] if args.file else None
    report = run_gate(files=files)

    q_result = run_quarantine(
        report,
        threshold=args.threshold,
        post_slack=not args.dry_run and not args.no_slack,
        dry_run=args.dry_run,
    )

    if args.json:
        output = report.to_dict()
        output["quarantine"] = q_result.to_dict()
        print(json.dumps(output, indent=2))
    else:
        if q_result.quarantined:
            print(q_result.format_report())
        else:
            print(f"No claims meet quarantine threshold ({args.threshold} runs).")
            if report.has_stale:
                print(f"\n{report.stale_claims} stale claim(s) exist but below threshold.")
                for d in report.stale_details[:5]:
                    rc = d.get("tracker_run_count", d.get("age_builds", "?"))
                    print(f"  [{rc} runs] {d['claim_text'][:80]}")


def cmd_triage(args):
    """Unified triage: rank all confab issues by severity, suggest fixes, batch."""
    try:
        from .triage import run_triage
    except ImportError:
        from confab.triage import run_triage

    gate_report = None
    tree_report = None
    supports_report = None
    source = getattr(args, 'source', 'all')

    # Run requested data sources
    if source in ("all", "gate"):
        gate_report = run_gate()

    if source in ("all", "tree"):
        check_tree = _get_check_tree()
        tree_report = check_tree()

    if source in ("all", "supports"):
        check_supports = _get_check_supports()
        supports_report = check_supports()

    limit = getattr(args, 'limit', 20)
    report = run_triage(
        gate_report=gate_report,
        tree_report=tree_report,
        supports_report=supports_report,
        limit=limit,
    )

    # Filter by category if specified
    category_filter = getattr(args, 'category', None)
    if category_filter:
        report.items = [i for i in report.items if i.category == category_filter]
        report.batches = [b for b in report.batches if b.category == category_filter]

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif getattr(args, 'slack', False):
        print(report.format_slack())
    else:
        print(report.format_report(limit=limit))


def cmd_ci(args):
    """Run the gate in CI mode with proper exit codes and markdown output.

    Exit codes:
        0 — clean (no failures, no stale claims)
        1 — failures found (claims contradict reality)
        2 — stale claims found (no failures, but unverified claims persist)
    """
    files = [args.file] if args.file else None
    stale_threshold = args.stale_threshold or 3
    vol = parse_volatility(getattr(args, 'volatility', None))
    report = run_gate(files=files, stale_threshold=stale_threshold, track=not args.no_track, volatility=vol)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_ci())

    # Write markdown to file if requested (for GitHub Actions PR comments)
    if args.output:
        Path(args.output).write_text(report.format_ci())

    # Exit codes: 1 = failures, 2 = stale only, 0 = clean
    if report.has_failures:
        sys.exit(1)
    elif report.has_stale and args.strict:
        sys.exit(2)


def cmd_fix(args):
    """Fix stale and failed claims automatically.

    For stale claims: re-runs verification, then either updates the verification
    tag to [v1: verified] or deletes the claim line from the source file.

    For file_exists failures: uses git log --diff-filter=R to find renames and
    updates the path in-place.

    All actions are logged to the fix_actions audit table in confab_tracker.db.
    """
    import re
    import subprocess
    from datetime import datetime, timezone

    dry_run = args.dry_run
    files = [args.file] if args.file else None

    # Run the gate to get current state
    report = run_gate(files=files, track=True)

    actions_taken = []

    # --- Fix stale claims ---
    for stale in report.stale_details:
        claim_text = stale["claim_text"]
        source_file = stale.get("source_file")
        claim_type = stale.get("claim_type", "unknown")

        # Find the matching outcome to get the Claim object for re-verification
        matching_outcome = None
        for outcome in report.all_outcomes:
            if outcome.claim.text == claim_text:
                matching_outcome = outcome
                break

        if matching_outcome is None:
            # No matching claim in outcomes — can't re-verify, just note it
            action = {
                "claim": claim_text[:80],
                "action": "skipped",
                "detail": "No matching claim found in gate outcomes for re-verification",
                "file": source_file,
            }
            actions_taken.append(action)
            continue

        # Re-verify the claim
        reverify = verify_claim(matching_outcome.claim)

        if reverify.result.value == "passed":
            # Claim is actually valid — update the verification tag in the source file
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            new_tag = f"[v1: {reverify.method} {today}]"

            if source_file and Path(source_file).exists():
                content = Path(source_file).read_text()
                # Find the line containing this claim text and add/update the verification tag
                old_line = _find_claim_line(content, claim_text)
                if old_line:
                    updated_line = _update_verification_tag(old_line, new_tag)
                    if updated_line != old_line:
                        if not dry_run:
                            content = content.replace(old_line, updated_line, 1)
                            Path(source_file).write_text(content)
                            claim_hash = _hash_claim(claim_text)
                            update_claim_status(claim_hash, "verified",
                                                evidence=reverify.evidence,
                                                method=reverify.method)
                            record_fix_action(
                                claim_text=claim_text,
                                action="verified_and_tagged",
                                file_modified=source_file,
                                detail=f"Re-verified PASSED ({reverify.method}). Added {new_tag}",
                                claim_hash=claim_hash,
                            )
                        action = {
                            "claim": claim_text[:80],
                            "action": "verified_and_tagged",
                            "detail": f"PASSED — added {new_tag}",
                            "file": source_file,
                        }
                        actions_taken.append(action)
                        continue

            # Passed but couldn't update file — just update tracker
            if not dry_run:
                claim_hash = _hash_claim(claim_text)
                update_claim_status(claim_hash, "verified",
                                    evidence=reverify.evidence,
                                    method=reverify.method)
                record_fix_action(
                    claim_text=claim_text,
                    action="verified_tracker_only",
                    file_modified=source_file,
                    detail=f"Re-verified PASSED ({reverify.method}). Source file not updated.",
                    claim_hash=claim_hash,
                )
            action = {
                "claim": claim_text[:80],
                "action": "verified_tracker_only",
                "detail": f"PASSED — tracker updated (couldn't modify source)",
                "file": source_file,
            }
            actions_taken.append(action)

        elif reverify.result.value == "failed":
            # Claim contradicts reality — delete the line from the source file
            if source_file and Path(source_file).exists():
                content = Path(source_file).read_text()
                old_line = _find_claim_line(content, claim_text)
                if old_line:
                    if not dry_run:
                        content = content.replace(old_line + "\n", "", 1)
                        Path(source_file).write_text(content)
                        claim_hash = _hash_claim(claim_text)
                        update_claim_status(claim_hash, "failed",
                                            evidence=reverify.evidence,
                                            method=reverify.method)
                        record_fix_action(
                            claim_text=claim_text,
                            action="deleted_failed_claim",
                            file_modified=source_file,
                            detail=f"Re-verified FAILED ({reverify.evidence}). Line removed.",
                            claim_hash=claim_hash,
                        )
                    action = {
                        "claim": claim_text[:80],
                        "action": "deleted_failed_claim",
                        "detail": f"FAILED — line removed ({reverify.evidence[:60]})",
                        "file": source_file,
                    }
                    actions_taken.append(action)
                    continue

            # Failed but couldn't modify source
            if not dry_run:
                claim_hash = _hash_claim(claim_text)
                update_claim_status(claim_hash, "failed",
                                    evidence=reverify.evidence,
                                    method=reverify.method)
                record_fix_action(
                    claim_text=claim_text,
                    action="marked_failed",
                    file_modified=source_file,
                    detail=f"Re-verified FAILED. Source not modified.",
                    claim_hash=claim_hash,
                )
            action = {
                "claim": claim_text[:80],
                "action": "marked_failed",
                "detail": "FAILED — tracker updated (couldn't modify source)",
                "file": source_file,
            }
            actions_taken.append(action)

        else:
            # Inconclusive or skipped — delete stale claims that can't be verified
            if source_file and Path(source_file).exists():
                content = Path(source_file).read_text()
                old_line = _find_claim_line(content, claim_text)
                if old_line:
                    if not dry_run:
                        content = content.replace(old_line + "\n", "", 1)
                        Path(source_file).write_text(content)
                        claim_hash = _hash_claim(claim_text)
                        remove_claims([claim_hash])
                        record_fix_action(
                            claim_text=claim_text,
                            action="deleted_unverifiable",
                            file_modified=source_file,
                            detail=f"Cannot auto-verify ({reverify.result.value}). "
                                   f"Stale for {stale.get('tracker_run_count', '?')} runs. Line removed.",
                            claim_hash=claim_hash,
                        )
                    action = {
                        "claim": claim_text[:80],
                        "action": "deleted_unverifiable",
                        "detail": f"Unverifiable + stale ({stale.get('tracker_run_count', '?')} runs) — removed",
                        "file": source_file,
                    }
                    actions_taken.append(action)
                    continue

            action = {
                "claim": claim_text[:80],
                "action": "skipped",
                "detail": f"Cannot verify ({reverify.result.value}), no source to modify",
                "file": source_file,
            }
            actions_taken.append(action)

    # --- Fix failed FILE_EXISTS claims via git rename detection ---
    for failed in report.failed_details:
        claim_text = failed["claim_text"]
        source_file = failed.get("source_file")
        claim_type = failed.get("claim_type", "unknown")

        if claim_type != "file_exists":
            continue

        # Find the Claim object to get extracted_paths
        matching_outcome = None
        for outcome in report.all_outcomes:
            if outcome.claim.text == claim_text and outcome.result.value == "failed":
                matching_outcome = outcome
                break

        if not matching_outcome or not matching_outcome.claim.extracted_paths:
            continue

        # Try git rename detection for each missing path
        for missing_path in matching_outcome.claim.extracted_paths:
            if Path(missing_path).exists():
                continue  # Not actually missing

            new_path = _find_git_rename(missing_path)
            if new_path and source_file and Path(source_file).exists():
                content = Path(source_file).read_text()
                if missing_path in content:
                    if not dry_run:
                        content = content.replace(missing_path, new_path)
                        Path(source_file).write_text(content)
                        claim_hash = _hash_claim(claim_text)
                        record_fix_action(
                            claim_text=claim_text,
                            action="updated_renamed_path",
                            file_modified=source_file,
                            detail=f"Git rename detected: {missing_path} → {new_path}",
                            claim_hash=claim_hash,
                        )
                    action = {
                        "claim": claim_text[:80],
                        "action": "updated_renamed_path",
                        "detail": f"{missing_path} → {new_path}",
                        "file": source_file,
                    }
                    actions_taken.append(action)

    # --- Output ---
    if not actions_taken:
        print("No fixable claims found. Gate is clean." if report.clean
              else "No auto-fixable claims found. Manual review needed for remaining issues.")
        return

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}# Confab Fix Report\n")
    print(f"Actions: {len(actions_taken)}\n")

    for i, act in enumerate(actions_taken, 1):
        icon = {"verified_and_tagged": "+", "deleted_failed_claim": "-",
                "deleted_unverifiable": "-", "updated_renamed_path": "~",
                "verified_tracker_only": "+", "marked_failed": "!",
                "skipped": "?"}.get(act["action"], " ")
        print(f"  [{icon}] {act['claim']}")
        print(f"      Action: {act['action']}")
        print(f"      Detail: {act['detail']}")
        if act.get("file"):
            print(f"      File: {act['file']}")
        print()

    if dry_run:
        print("(Dry run — no files or database were modified)")

    if args.json:
        print(json.dumps(actions_taken, indent=2))


def _propose_expires(content: str, created: Optional[str], matched_patterns: List[str]) -> tuple:
    """Propose an expires date for a perishable observation.

    Rules:
        1. If content contains ISO dates (YYYY-MM-DD), use the latest date + 1 day.
        2. If content contains month-day dates (Feb 26, Mar 15 2026), parse and use latest + 1 day.
        3. If content has prices ($) or percentages (%), use created + 30 days.
        4. If content has financial terms only, use created + 60 days.
        5. Fallback: created + 60 days.

    Returns:
        (proposed_date: str, rule: str) — YYYY-MM-DD date and which rule matched.
    """
    import re
    from datetime import datetime, timedelta, timezone

    # Try to extract explicit dates from content
    iso_dates = re.findall(r'\b(\d{4}-\d{2}-\d{2})\b', content)
    month_dates = re.findall(
        r'\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*)\s+(\d{1,2})(?:,?\s*(\d{4}))?',
        content
    )

    latest_date = None

    # Parse ISO dates
    for d in iso_dates:
        try:
            parsed = datetime.strptime(d, "%Y-%m-%d")
            if latest_date is None or parsed > latest_date:
                latest_date = parsed
        except ValueError:
            continue

    # Parse month-day dates
    current_year = datetime.now(timezone.utc).year
    for month_str, day_str, year_str in month_dates:
        year = int(year_str) if year_str else current_year
        try:
            parsed = datetime.strptime(f"{month_str[:3]} {day_str} {year}", "%b %d %Y")
            if latest_date is None or parsed > latest_date:
                latest_date = parsed
        except ValueError:
            continue

    if latest_date is not None:
        expires = (latest_date + timedelta(days=1)).strftime("%Y-%m-%d")
        return expires, "event_date+1d"

    # No explicit dates — use created date + offset based on pattern type
    base_date = None
    if created and len(created) >= 10:
        try:
            base_date = datetime.strptime(created[:10], "%Y-%m-%d")
        except ValueError:
            pass
    if base_date is None:
        base_date = datetime.now(timezone.utc)

    if "price" in matched_patterns or "percentage" in matched_patterns:
        expires = (base_date + timedelta(days=30)).strftime("%Y-%m-%d")
        return expires, "price_or_pct+30d"

    if "financial-term" in matched_patterns:
        expires = (base_date + timedelta(days=60)).strftime("%Y-%m-%d")
        return expires, "financial_term+60d"

    # Fallback
    expires = (base_date + timedelta(days=60)).strftime("%Y-%m-%d")
    return expires, "fallback+60d"


def cmd_fix_perishable(args):
    """Fix perishable observations: add expires dates to tree entries with dates/prices/% but no TTL.

    Default behavior: prints a preview table (dry-run).
    With --apply: writes changes to KNOWLEDGE_TREE.json.
    """
    try:
        from .tree import check_tree
    except ImportError:
        from confab.tree import check_tree

    tree_path = getattr(args, 'tree', None)
    apply = getattr(args, 'apply', False)
    json_output = getattr(args, 'json', False)

    # Scan tree for perishable observations without TTL
    report = check_tree(tree_path=tree_path)

    if not report.perishable_no_ttl:
        print("No perishable observations without TTL found. Tree is clean.")
        return

    # Resolve the actual tree file path for writing
    if tree_path:
        tree_file = Path(tree_path)
        if not tree_file.is_absolute():
            tree_file = Path.cwd() / tree_path
    else:
        from confab.tree import DEFAULT_TREE_PATH
        tree_file = Path.cwd() / DEFAULT_TREE_PATH

    # Load tree data for reading created dates and (if applying) writing
    tree_data = json.loads(tree_file.read_text())
    nodes = tree_data.get("nodes", {})

    # Build proposals
    proposals = []
    for issue in report.perishable_no_ttl:
        node = nodes.get(issue.entry_id, {})
        created = node.get("created", node.get("timestamp", ""))
        full_content = node.get("content", issue.content)

        proposed_date, rule = _propose_expires(
            full_content, created, issue.matched_patterns
        )
        proposals.append({
            "id": issue.entry_id,
            "content": issue.content[:100],
            "domain": issue.domain or "unset",
            "patterns": issue.matched_patterns,
            "proposed_expires": proposed_date,
            "rule": rule,
        })

    # Output
    if json_output:
        result = {
            "total_perishable_no_ttl": len(proposals),
            "mode": "apply" if apply else "dry-run",
            "tree_path": str(tree_file),
            "proposals": proposals,
        }
        print(json.dumps(result, indent=2))
        return

    # Table output
    mode_label = "APPLYING" if apply else "DRY RUN (use --apply to write)"
    print(f"# Perishable Fix — {mode_label}\n")
    print(f"Tree: {tree_file}")
    print(f"Entries to fix: {len(proposals)}\n")

    # Group by rule for summary
    rule_counts = {}
    for p in proposals:
        rule_counts[p["rule"]] = rule_counts.get(p["rule"], 0) + 1

    print("Rules applied:")
    for rule, count in sorted(rule_counts.items(), key=lambda x: -x[1]):
        print(f"  {rule}: {count}")
    print()

    # Show preview table (first 30 entries, then summary)
    shown = proposals[:30]
    print(f"{'ID':<10} {'Expires':<12} {'Rule':<22} {'Content'}")
    print(f"{'─'*10} {'─'*12} {'─'*22} {'─'*50}")
    for p in shown:
        content_snippet = p["content"][:50].replace("\n", " ")
        print(f"{p['id']:<10} {p['proposed_expires']:<12} {p['rule']:<22} {content_snippet}")
    if len(proposals) > 30:
        print(f"\n  ...and {len(proposals) - 30} more")

    # Apply changes
    if apply:
        modified = 0
        for p in proposals:
            nid = p["id"]
            if nid in nodes:
                nodes[nid]["expires"] = p["proposed_expires"]
                modified += 1

        tree_file.write_text(json.dumps(tree_data, indent=2, ensure_ascii=False) + "\n")
        print(f"\nWrote {modified} expires dates to {tree_file}")
    else:
        print(f"\n(Dry run — no files modified. Use --apply to write changes.)")


def _find_claim_line(content: str, claim_text: str) -> Optional[str]:
    """Find the full line in content that contains the claim text."""
    # The claim text might be a substring of the full line (numbering stripped, etc.)
    # Try increasingly aggressive matching
    for line in content.splitlines():
        # Strip common markdown prefixes for comparison
        stripped = line.lstrip("- ").lstrip("0123456789.").strip()
        if claim_text in line or claim_text.strip() in stripped:
            return line

    # Try matching just the substantive part (skip numbers/bullets)
    import re
    # Extract the core content from the claim text (skip leading "N. ")
    core = re.sub(r'^\d+\.\s*', '', claim_text).strip()
    if len(core) > 20:
        for line in content.splitlines():
            if core[:40] in line:
                return line

    return None


def _update_verification_tag(line: str, new_tag: str) -> str:
    """Update or append a verification tag on a line."""
    import re
    # Replace existing verification tags
    tag_pattern = r'\[(?:v[12]:\s*[^\]]*|unverified|FAILED:\s*[^\]]*)\]'
    if re.search(tag_pattern, line):
        return re.sub(tag_pattern, new_tag, line, count=1)
    # No existing tag — append
    return line.rstrip() + f" {new_tag}"


def _find_git_rename(missing_path: str) -> Optional[str]:
    """Use git log --diff-filter=R to find if a file was renamed."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "--diff-filter=R", "--summary", "--all",
             "-n", "5", "--", missing_path],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        # Parse rename lines: " rename X => Y (NNN%)"
        import re
        for line in result.stdout.splitlines():
            line = line.strip()
            # Match patterns like "rename old/path => new/path (100%)"
            m = re.search(r'rename\s+(.+?)\s*=>\s*(.+?)\s*\(', line)
            if m:
                # The new path is the destination of the rename
                new_path = m.group(2).strip()
                if Path(new_path).exists():
                    return new_path

            # Also match "{old => new}/rest" brace format
            m = re.search(r'\{(.+?)\s*=>\s*(.+?)\}', line)
            if m:
                # Need the full path context around the braces
                full_match = re.search(r'rename\s+(.*?\{.+?\s*=>\s*.+?\}.*?)\s*\(', line)
                if full_match:
                    rename_expr = full_match.group(1)
                    # Expand {old => new} into the new path
                    new_expr = re.sub(r'\{[^}]*=>\s*([^}]*)\}', r'\1', rename_expr)
                    new_expr = new_expr.replace('//', '/')
                    if Path(new_expr).exists():
                        return new_expr

        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def cmd_lint(args):
    """Lint priority files for claim hygiene issues."""
    files = [args.file] if args.file else None
    threshold = args.threshold or 3
    report = run_lint(files=files, stale_threshold=threshold)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_report())

    # Exit code: 1 if any errors or warnings found
    if report.error_count > 0 or report.warning_count > 0:
        sys.exit(1)


def cmd_check_supports(args):
    """Check knowledge tree for entries with degraded support structures."""
    if args.fix:
        try:
            from .supports import fix_zombies
        except ImportError:
            from confab.supports import fix_zombies

        dry_run = args.dry_run
        result = fix_zombies(tree_path=args.tree, dry_run=dry_run)
        report = result["report"]

        if not result["fixed"]:
            print("No zombie entries to fix.")
            return

        if dry_run:
            print(f"DRY RUN — would invalidate {len(result['fixed'])} zombie entries:")
            for entry_id in result["fixed"]:
                zombie = next((z for z in report.zombies if z.entry_id == entry_id), None)
                if zombie:
                    print(f"  {entry_id} ({zombie.entry_type}) — {zombie.content[:80]}")
            print(f"\nRun without --dry-run to apply.")
        else:
            print(f"Fixed {len(result['fixed'])} zombie entries:")
            for entry_id in result["fixed"]:
                print(f"  invalidated {entry_id}")
            if result["skipped"]:
                print(f"Skipped {len(result['skipped'])}: {', '.join(result['skipped'])}")

        return

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


def cmd_tree(args):
    """Scan knowledge tree for factual health issues (expired, perishable, unverified)."""
    check_tree = _get_check_tree()
    stale_days = args.stale_days or 14
    report = check_tree(tree_path=args.tree, stale_days=stale_days)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.slack:
        print(report.format_slack())
    else:
        print(report.format_report())

    if report.has_expired:
        sys.exit(1)


def cmd_trace(args):
    """Trace the propagation path of a specific claim."""
    result = trace_claim(args.query)

    if result is None:
        print(f"No claim found matching: {args.query}")
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    claim = result["claim"]
    cascade = result["cascade"]
    depth = result["cascade_depth"]

    print("# Claim Trace\n")
    print(f"**Text:** {claim['text'][:200]}")
    print(f"**Type:** {claim['type']}")
    print(f"**Status:** {claim['status']}")
    print(f"**Source:** {claim.get('source', 'unknown')}")
    print(f"**First seen:** {claim['first_seen'][:19]}")
    print(f"**Last seen:** {claim['last_seen'][:19]}")
    print(f"**Run count:** {claim['run_count']}")
    print(f"**Cascade depth:** {depth}")

    if not cascade:
        print("\nNo cascade history recorded (tracking started after this claim).")
        return

    print(f"\n## Propagation Timeline ({len(cascade)} appearances)\n")
    for i, entry in enumerate(cascade):
        marker = "  "
        if entry["status"] == "verified":
            marker = "ok"
        elif entry["status"] == "failed":
            marker = "xx"
        elif entry["status"] == "stale":
            marker = "!!"
        elif entry["status"] in ("new", "unverified"):
            marker = ".."

        ts = entry["timestamp"][:19]
        print(f"  {marker} Run #{entry['run_id']:3d}  {ts}  [{entry['status']}]"
              + (f"  <- {entry['source']}" if entry.get("source") else ""))

    # Show cascade analysis
    statuses = [e["status"] for e in cascade]
    unverified_streak = 0
    max_streak = 0
    for s in statuses:
        if s in ("new", "unverified", "inconclusive", "stale"):
            unverified_streak += 1
            max_streak = max(max_streak, unverified_streak)
        else:
            unverified_streak = 0

    if max_streak > 1:
        print(f"\n  Longest unverified streak: {max_streak} consecutive runs")

    if "verified" in statuses:
        first_verified = next(i for i, s in enumerate(statuses) if s == "verified")
        print(f"  Runs before first verification: {first_verified}")


def cmd_cascade(args):
    """Show cascade depth statistics across all tracked claims."""
    stats = get_cascade_stats()

    if args.json:
        print(json.dumps(stats, indent=2))
        return

    print("# Cascade Statistics\n")

    if stats["total_tracked"] == 0:
        print("No cascade data yet. Run `confab gate` to start tracking.")
        return

    print(f"Total claims tracked: {stats['total_tracked']}")
    print(f"Claims that cascaded (2+ runs): {stats['total_cascaded']}")
    print(f"Claims resolved: {stats['resolved_count']}")
    print(f"Average cascade depth: {stats['avg_depth']} runs")
    print(f"Maximum cascade depth: {stats['max_depth']} runs")

    if stats["total_cascaded"] > 0 and stats["total_tracked"] > 0:
        cascade_rate = stats["total_cascaded"] / stats["total_tracked"] * 100
        print(f"Cascade rate: {cascade_rate:.0f}% of claims propagated 2+ runs")

    if stats["top_cascaders"]:
        print(f"\n## Deepest Cascaders\n")
        for c in stats["top_cascaders"][:5]:
            status_icon = {
                "verified": "ok", "failed": "xx", "stale": "!!",
                "expired": "~~", "unverified": "..",
            }.get(c["status"], "??")
            print(f"  [{status_icon}] depth={c['depth']:3d}  {c['text'][:80]}")


def cmd_audit(args):
    """Print a comprehensive audit summary from the tracker database."""
    data = get_audit_data()

    if args.json:
        print(json.dumps(data, indent=2))
        return

    summary = data["claims_summary"]
    dist = data["depth_distribution"]
    res = data["resolution"]
    cascaders = data["unresolved_cascaders"]
    runs = data["recent_runs"]

    lines = []
    lines.append("=" * 56)
    lines.append("  CONFAB AUDIT REPORT")
    lines.append("=" * 56)

    # --- Claims summary ---
    lines.append("")
    lines.append("CLAIMS TRACKED")
    lines.append(f"  Total: {summary['total_tracked']}  |  Gate runs: {summary['total_gate_runs']}")
    if summary["latest_run"]:
        lines.append(f"  Latest run: {summary['latest_run'][:19]}")
    lines.append("")
    lines.append("  By status:")
    status_order = ["verified", "unverified", "stale", "failed", "new", "inconclusive", "expired"]
    for s in status_order:
        count = summary["by_status"].get(s, 0)
        if count > 0:
            lines.append(f"    {s:14s} {count:4d}")

    # --- Resolution rate ---
    lines.append("")
    lines.append("-" * 56)
    lines.append("")
    lines.append("RESOLUTION RATE")
    lines.append(f"  {res['rate_pct']:.1f}% ({res['resolved']}/{res['total']} claims resolved)")

    # --- Cascade depth distribution ---
    lines.append("")
    lines.append("-" * 56)
    lines.append("")
    lines.append("CASCADE DEPTH DISTRIBUTION")
    total_depth_claims = sum(dist.values())
    if total_depth_claims > 0:
        max_bar = max(dist.values()) if dist.values() else 1
        for bucket, count in dist.items():
            pct = count / total_depth_claims * 100
            bar_len = int(count / max_bar * 20) if max_bar > 0 else 0
            bar = "#" * bar_len
            lines.append(f"  {bucket:>5s}: {bar:<20s} {count:3d} ({pct:.0f}%)")
    else:
        lines.append("  No cascade data yet.")

    # --- Top unresolved cascaders ---
    lines.append("")
    lines.append("-" * 56)
    lines.append("")
    lines.append("TOP UNRESOLVED CASCADERS")
    if cascaders:
        for i, c in enumerate(cascaders, 1):
            status_icon = {
                "stale": "!!", "failed": "xx", "unverified": "..",
                "inconclusive": "??", "new": "++",
            }.get(c["status"], "  ")
            lines.append(f"  {i}. [{status_icon}] depth={c['depth']:3d}  runs={c['run_count']:3d}")
            lines.append(f"     {c['text']}")
            if c.get("source"):
                lines.append(f"     src: {c['source']}")
    else:
        lines.append("  All claims resolved.")

    # --- Recent runs ---
    lines.append("")
    lines.append("-" * 56)
    lines.append("")
    lines.append("RECENT GATE RUNS")
    if runs:
        for run in runs:
            ts = run["timestamp"][:19] if run.get("timestamp") else "?"
            lines.append(
                f"  #{run['id']:3d}  {ts}  "
                f"claims={run['total_claims']}  "
                f"pass={run['passed']}  "
                f"fail={run['failed']}  "
                f"stale={run['stale']}"
            )
    else:
        lines.append("  No gate runs recorded yet.")

    lines.append("")
    lines.append("=" * 56)

    print("\n".join(lines))


def cmd_init(args):
    """Generate a starter confab.toml in the current directory.

    Auto-detects markdown files in the project and suggests them as scan targets.
    """
    target = Path.cwd() / "confab.toml"
    if target.exists():
        print(f"confab.toml already exists at {target}")
        sys.exit(1)

    # Auto-detect markdown files that look like priority/handoff files
    cwd = Path.cwd()
    md_files = sorted(cwd.rglob("*.md"))

    # Filter to likely scan targets (priority files, handoffs, notes)
    # Skip common non-claim files (README, LICENSE, CHANGELOG, etc.)
    skip_names = {
        'readme', 'license', 'changelog', 'contributing', 'code_of_conduct',
        'security', 'design', 'architecture',
    }
    skip_dirs = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', '.tox', 'dist', 'build'}

    candidates = []
    for md in md_files:
        # Skip files in ignored directories
        if any(part in skip_dirs for part in md.parts):
            continue
        # Skip common non-claim files
        if md.stem.lower() in skip_names:
            continue
        # Prefer files that look like priorities, handoffs, or notes
        rel = md.relative_to(cwd)
        candidates.append(str(rel))

    # Build the files_to_scan section
    if candidates:
        # Show up to 10 auto-detected files, commented out for user to pick
        file_lines = []
        for c in candidates[:10]:
            file_lines.append(f'    # "{c}",')
        if len(candidates) > 10:
            file_lines.append(f"    # ... and {len(candidates) - 10} more .md files found")
        files_block = "\n".join(file_lines)
    else:
        files_block = '    # "docs/priorities.md",\n    # "notes/handoff.md",'

    content = f"""\
[confab]
# Files to scan for carry-forward claims (relative to workspace root)
# Uncomment the files you want confab to monitor for stale claims.
files_to_scan = [
{files_block}
]

# How many gate runs before unverified claims are flagged stale
stale_threshold = 3

# Where to store the tracker database (relative to workspace root)
db_path = "confab_tracker.db"

# Sections to skip during claim extraction (regex patterns matched against headings).
# Lines under these headings are treated as knowledge notes, not system state claims.
# This prevents false positives from sections that contain ideas or strategic context.
exclude_sections = [
    # "Germinating threads",
    # "Strategic Context",
]

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
"""
    target.write_text(content)
    print(f"Created {target}")

    if candidates:
        print(f"Auto-detected {len(candidates)} markdown file(s) — uncomment the ones to scan.")
        print("Edit confab.toml to configure your scan targets, then run: confab gate")
    else:
        # No markdown files found — create a sample so the user can test immediately
        sample_dir = cwd / "docs"
        sample_dir.mkdir(exist_ok=True)
        sample_file = sample_dir / "priorities.md"
        if not sample_file.exists():
            sample_file.write_text("""\
# Priorities

## Current Sprint

- Feature X depends on `src/config.py` being configured
- Deploy blocked on DATABASE_URL not being set
- The data pipeline output at `output/results.json` is stale [unverified]

## Completed

- Set up CI/CD pipeline [v2: checked tests 2026-03-20]
""")
            # Update confab.toml to scan the sample file
            content = content.replace(
                '    # "docs/priorities.md",\n    # "notes/handoff.md",',
                '    "docs/priorities.md",',
            )
            target.write_text(content)
            print(f"Created sample file: {sample_file}")

        print("Try it now: confab gate")


if __name__ == "__main__":
    main()
