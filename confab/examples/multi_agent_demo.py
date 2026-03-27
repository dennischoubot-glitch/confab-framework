#!/usr/bin/env python3
"""Multi-agent cascade detection demo for confab-framework.

Simulates a three-agent sprint cycle where claims propagate through handoffs.
Agent 1 (Dreamer) generates claims — some true, some false.
Agent 2 (Builder) receives the handoff and propagates claims.
The confab gate runs between each handoff, catching false claims before
they cascade further.

Usage:
    pip install confab-framework
    python -m confab.examples.multi_agent_demo

No external dependencies beyond confab-framework itself.
"""

import os
import tempfile
import textwrap
from pathlib import Path

from confab import (
    ConfabConfig,
    ConfabGate,
    extract_claims,
    verify_all,
)


def banner(title: str) -> None:
    width = 60
    print(f"\n{'=' * width}")
    print(f"  {title}")
    print(f"{'=' * width}\n")


def step(label: str) -> None:
    print(f"\n--- {label} ---\n")


def agent_dreamer(workspace: Path) -> str:
    """Agent 1: Dreamer generates a handoff with mixed true/false claims."""

    # Create some real files the dreamer references
    config_dir = workspace / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "settings.toml").write_text('[app]\nname = "demo"\n')
    (workspace / "pipeline.py").write_text("def run(): return True\n")

    # The dreamer's handoff text — contains both true and false claims
    handoff = textwrap.dedent(f"""\
        ## Dreamer Handoff

        System state assessment for this sprint:

        - Config is ready at `{config_dir}/settings.toml` [unverified]
        - Data pipeline script at `{workspace}/pipeline.py` works [unverified]
        - Output report at `{workspace}/output/report.json` is generated [unverified]
        - Deployment blocked on CONFAB_DEMO_SECRET_KEY [unverified]
        - Model weights cached at `{workspace}/models/weights.bin` [unverified]
        - Test database at `{workspace}/test.db` needs migration [unverified]

        @builder

        Build the next feature based on the above system state.
    """)

    return handoff


def agent_builder(handoff_text: str, workspace: Path) -> str:
    """Agent 2: Builder receives handoff and propagates claims forward."""

    # Builder adds its own claims on top of what it received
    builder_additions = textwrap.dedent(f"""\
        ## Builder Handoff

        Completed sprint work. Propagating system state:

        - Config at `{workspace}/config/settings.toml` confirmed ready
        - Pipeline at `{workspace}/pipeline.py` is operational
        - Output report at `{workspace}/output/report.json` ready for review
        - Still blocked on CONFAB_DEMO_SECRET_KEY — cannot deploy
        - Model weights at `{workspace}/models/weights.bin` loaded successfully
        - Built new feature using `{workspace}/src/feature.py`

        @critic

        Review the above and deploy if clean.
    """)

    return builder_additions


def run_gate_between_agents(
    sender: str,
    receiver: str,
    handoff_text: str,
    workspace: Path,
) -> None:
    """Run the confab gate at the handoff boundary between two agents."""

    step(f"CONFAB GATE: {sender} -> {receiver}")
    print(f"Scanning {sender}'s handoff for verifiable claims...\n")

    # Extract claims from the handoff text
    claims = extract_claims(handoff_text, source_file=f"{sender}_handoff.md")

    if not claims:
        print("  No verifiable claims found.\n")
        return

    print(f"  Extracted {len(claims)} claims:\n")
    for i, claim in enumerate(claims, 1):
        tag = f" {claim.verification_tag}" if claim.verification_tag else ""
        print(f"    {i}. [{claim.claim_type.value}]{tag}")
        print(f"       \"{claim.text[:80]}{'...' if len(claim.text) > 80 else ''}\"")
        if claim.extracted_paths:
            print(f"       Paths: {claim.extracted_paths}")
        if claim.extracted_env_vars:
            print(f"       Env vars: {claim.extracted_env_vars}")
        print()

    # Verify all claims against reality
    outcomes = verify_all(claims)

    passed = sum(1 for o in outcomes if o.result.value == "passed")
    failed = sum(1 for o in outcomes if o.result.value == "failed")
    inconclusive = sum(1 for o in outcomes if o.result.value == "inconclusive")
    skipped = sum(1 for o in outcomes if o.result.value == "skipped")

    print(f"  Verification results:")
    print(f"    PASSED:       {passed}")
    print(f"    FAILED:       {failed}")
    print(f"    INCONCLUSIVE: {inconclusive}")
    print(f"    SKIPPED:      {skipped}")
    print()

    # Show details for failures
    failures = [o for o in outcomes if o.result.value == "failed"]
    if failures:
        print(f"  FAILURES (would have cascaded without the gate):\n")
        for o in failures:
            print(f"    FAILED: \"{o.claim.text[:70]}...\"")
            print(f"      Evidence: {o.evidence}")
            print(f"      Method:   {o.method}")
            print()

    # Show what passed
    passes = [o for o in outcomes if o.result.value == "passed"]
    if passes:
        print(f"  VERIFIED (safe to propagate):\n")
        for o in passes:
            print(f"    PASSED: \"{o.claim.text[:70]}{'...' if len(o.claim.text) > 70 else ''}\"")
            print(f"      Evidence: {o.evidence}")
            print()


def demonstrate_cascade_tracking(workspace: Path) -> None:
    """Show how unverified claims age across multiple gate runs."""

    step("CASCADE TRACKING: Claims aging across runs")

    # Simulate the same claim persisting across 4 build cycles
    claim_text = f"Output report at `{workspace}/output/report.json` is generated"
    print(f"  Tracking claim: \"{claim_text}\"\n")

    for build in range(1, 5):
        claims = extract_claims(
            f"- {claim_text} [unverified]",
            source_file=f"build_{build}_priorities.md",
        )
        if claims:
            outcomes = verify_all(claims)
            for o in outcomes:
                status = o.result.value.upper()
                marker = "!!" if build >= 3 else "  "
                print(f"  {marker} Build {build}: {status} — {o.evidence}")

    print()
    print("  After 3+ builds at [unverified], the claim should be")
    print("  verified or deleted — this is how false blockers propagate.")


def demonstrate_full_gate(workspace: Path) -> None:
    """Show the high-level ConfabGate API with a config object."""

    step("HIGH-LEVEL API: ConfabGate with config")

    # Write a handoff file to disk
    handoff_file = workspace / "handoff.md"
    handoff_file.write_text(textwrap.dedent(f"""\
        ## Sprint Handoff

        - Config deployed at `{workspace}/config/settings.toml`
        - Blocked on CONFAB_DEMO_NONEXISTENT_VAR
        - Missing output at `{workspace}/does_not_exist.json`
    """))

    config = ConfabConfig(
        workspace_root=workspace,
        files_to_scan=[str(handoff_file)],
    )

    gate = ConfabGate(config=config)
    report = gate.run(track=False)

    print(f"  Files scanned:  {len(report.files_scanned)}")
    print(f"  Total claims:   {report.total_claims}")
    print(f"  Passed:         {report.passed}")
    print(f"  Failed:         {report.failed}")
    print(f"  Gate status:    {'CLEAN' if report.clean else 'ISSUES DETECTED'}")

    if report.failed_details:
        print(f"\n  Failed claims:")
        for d in report.failed_details:
            print(f"    - {d.get('text', d.get('claim', ''))[:70]}")
            print(f"      Evidence: {d.get('evidence', '')}")


def main() -> None:
    banner("CONFAB FRAMEWORK — Multi-Agent Cascade Detection Demo")

    print("This demo simulates a three-agent sprint cycle (dreamer -> builder")
    print("-> critic) and runs the confab gate at each handoff boundary.")
    print()
    print("The gate catches false claims BEFORE they cascade to the next agent,")
    print("preventing the propagation problem where one agent's confabulation")
    print("becomes the next agent's trusted input.")

    # Create a temporary workspace with some real files
    with tempfile.TemporaryDirectory(prefix="confab_demo_") as tmpdir:
        workspace = Path(tmpdir)

        # --- Phase 1: Dreamer generates handoff ---
        banner("Phase 1: Dreamer generates handoff")
        dreamer_handoff = agent_dreamer(workspace)
        print("Dreamer's handoff text:")
        for line in dreamer_handoff.strip().split("\n"):
            print(f"  {line}")

        # --- Gate check: Dreamer -> Builder ---
        banner("Phase 2: Gate checks dreamer's claims")
        run_gate_between_agents("Dreamer", "Builder", dreamer_handoff, workspace)

        # --- Phase 3: Builder propagates claims ---
        banner("Phase 3: Builder propagates claims (cascade risk)")
        builder_handoff = agent_builder(dreamer_handoff, workspace)
        print("Builder's handoff text (note: propagates unchecked claims):")
        for line in builder_handoff.strip().split("\n"):
            print(f"  {line}")

        # --- Gate check: Builder -> Critic ---
        banner("Phase 4: Gate checks builder's claims")
        run_gate_between_agents("Builder", "Critic", builder_handoff, workspace)

        # --- Cascade tracking ---
        banner("Phase 5: Cascade depth tracking")
        demonstrate_cascade_tracking(workspace)

        # --- High-level API demo ---
        banner("Phase 6: High-level ConfabGate API")
        demonstrate_full_gate(workspace)

    # --- Summary ---
    banner("Summary")
    print("The confab gate caught false claims at BOTH handoff points:")
    print()
    print("  1. Files that don't exist (output/report.json) were flagged")
    print("     before the next agent could act on them.")
    print()
    print("  2. Env var claims (CONFAB_DEMO_SECRET_KEY) were checked against")
    print("     the actual environment — 'blocked on X' is only true if X")
    print("     is actually missing.")
    print()
    print("  3. The builder propagated the dreamer's false claims forward —")
    print("     exactly the cascade pattern. Without the gate, the critic")
    print("     would have trusted claims that originated 2 agents back")
    print("     and were never verified.")
    print()
    print("  4. Cascade tracking shows claims aging across build cycles.")
    print("     After 3+ builds at [unverified], they're flagged as stale.")
    print()
    print("Install: pip install confab-framework")
    print("Docs:    https://github.com/dennischoubot-glitch/confab-framework")
    print()


if __name__ == "__main__":
    main()
