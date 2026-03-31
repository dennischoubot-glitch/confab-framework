#!/usr/bin/env python3
"""Gate an agent handoff — verify claims before passing work to the next agent.

Shows the core confab pattern: agent A produces output with claims,
the gate checks those claims against reality, and only passes verified
output to agent B. False claims are caught at the boundary instead of
cascading through the system.

Usage:
    pip install confab-framework
    python ci_gate_handoff.py
"""

import tempfile
from pathlib import Path

from confab import (
    ConfabConfig,
    ConfabGate,
    extract_claims,
    verify_all,
    VerificationResult,
)


def simulate_builder_output(workspace: Path) -> str:
    """Simulate an agent that produces output with verifiable claims.

    In a real system, this would be the LLM's response text.
    Some claims are true (files that exist), some are false.
    """
    # Create some real artifacts the "builder" references
    (workspace / "src").mkdir(exist_ok=True)
    (workspace / "src" / "app.py").write_text("def main(): pass\n")
    (workspace / "config.toml").write_text("[app]\nname = 'demo'\n")

    return f"""\
## Builder Handoff

Sprint work complete. Handing off to reviewer.

### What was built
- Main application at `{workspace}/src/app.py` is ready for review
- Config at `{workspace}/config.toml` has been updated with new settings
- Test output at `{workspace}/test_results.json` shows all tests passing [unverified]
- Migration script at `{workspace}/scripts/migrate.py` needs to be run before deploy [unverified]

### Blockers
- Deployment requires DATABASE_URL to be set
- CI pipeline needs CONFAB_EXAMPLE_API_KEY configured

@critic
"""


def gate_the_handoff(handoff_text: str, workspace: Path) -> bool:
    """Run the confab gate on handoff text. Returns True if clean."""

    print("=" * 60)
    print("CONFAB GATE: Builder -> Critic")
    print("=" * 60)

    # Step 1: Extract claims
    claims = extract_claims(handoff_text, source_file="builder_handoff.md")
    print(f"\nExtracted {len(claims)} verifiable claims:")
    for i, c in enumerate(claims, 1):
        tag = f" {c.verification_tag}" if c.verification_tag else ""
        print(f"  {i}. [{c.claim_type.value}]{tag} {c.text[:70]}")

    # Step 2: Verify against reality
    outcomes = verify_all(claims)
    print(f"\nVerification results:")

    all_clean = True
    for o in outcomes:
        icon = {
            "passed": "PASS",
            "failed": "FAIL",
            "inconclusive": "????",
            "skipped": "SKIP",
        }[o.result.value]
        print(f"  [{icon}] {o.claim.text[:60]}")
        print(f"         {o.evidence}")
        if o.result == VerificationResult.FAILED:
            all_clean = False

    # Step 3: Decision
    print(f"\n{'=' * 60}")
    if all_clean:
        print("GATE: CLEAN — safe to hand off to critic")
    else:
        print("GATE: ISSUES DETECTED — investigate before handing off")
        print("\nFailed claims would have cascaded to the next agent")
        print("without the gate. The critic would trust claims that")
        print("the builder stated but never verified.")
    print("=" * 60)

    return all_clean


def gate_with_high_level_api(workspace: Path) -> None:
    """Same gate using the ConfabGate class API."""

    print("\n\n--- Alternative: ConfabGate class API ---\n")

    # Write handoff to a file (as it would be in a real project)
    handoff_file = workspace / "handoff.md"
    handoff_file.write_text(simulate_builder_output(workspace))

    config = ConfabConfig(
        workspace_root=workspace,
        files_to_scan=[str(handoff_file)],
    )
    gate = ConfabGate(config=config)
    report = gate.run(track=False)

    # The report has everything you need
    print(f"Claims: {report.total_claims}")
    print(f"Passed: {report.passed}")
    print(f"Failed: {report.failed}")
    print(f"Clean:  {report.clean}")

    if report.has_failures:
        print("\nFailed claims:")
        for d in report.failed_details:
            print(f"  - {d.get('claim_text', '')[:70]}")
            print(f"    {d.get('evidence', '')}")


def main():
    with tempfile.TemporaryDirectory(prefix="confab_gate_") as tmpdir:
        workspace = Path(tmpdir)

        # Simulate builder output
        handoff = simulate_builder_output(workspace)

        # Gate the handoff
        gate_the_handoff(handoff, workspace)

        # Show the alternative API
        gate_with_high_level_api(workspace)


if __name__ == "__main__":
    main()
