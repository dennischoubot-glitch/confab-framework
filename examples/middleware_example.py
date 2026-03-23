"""Example: using the @confab_gate decorator to verify agent output.

This demonstrates the simplest way to add confabulation detection to
an agent function. The decorator intercepts the return value, extracts
verifiable claims (file paths, env vars), and checks them against reality.

Usage::

    pip install confab-framework
    python examples/middleware_example.py
"""

from confab import confab_gate, get_report
from confab.middleware import verify_text, ConfabVerificationError


# --- Example 1: Basic decorator (warn mode) ---

@confab_gate
def summarize_config(prompt: str) -> str:
    """An agent that reports on config files."""
    # In practice this would call an LLM. For demo purposes, return
    # a string that references real and fake files.
    return (
        "Configuration summary:\n"
        "- Main config at pyproject.toml is valid\n"
        "- Database at /tmp/nonexistent_database.db is ready\n"
        "- Environment requires CONFAB_DEMO_KEY to be set\n"
    )


# --- Example 2: Strict mode (raises on failure) ---

@confab_gate(check_files=True, check_env=False, on_fail="raise")
def deploy_agent(prompt: str) -> str:
    """An agent that claims to have deployed files."""
    return (
        "Deployment complete:\n"
        "- Wrote output to /tmp/deploy_output.json\n"
        "- Updated /tmp/deploy_manifest.yaml\n"
    )


# --- Example 3: Log mode (quiet, for production) ---

@confab_gate(check_files=True, check_env=True, on_fail="log")
def audit_agent(prompt: str) -> str:
    """An agent that audits system state."""
    return (
        "Audit complete. All systems operational.\n"
        "- Config at pyproject.toml verified\n"
        "- README.md present\n"
    )


# --- Example 4: verify_text() without decorator ---

def standalone_verification():
    """Verify arbitrary text without decorating a function."""
    agent_output = "The model weights at /models/latest.bin are loaded."
    report = verify_text(agent_output)
    print(f"\nStandalone: {report.summary()}")


# --- Run all examples ---

if __name__ == "__main__":
    print("=" * 60)
    print("Confab Middleware Examples")
    print("=" * 60)

    # Example 1: warn mode — prints a warning but returns output
    print("\n--- Example 1: @confab_gate (warn mode) ---")
    import warnings
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        result = summarize_config("check configs")
        print(f"Output: {result[:80]}...")
        report = get_report(summarize_config)
        if report:
            print(f"Report: {report.summary()}")
        if w:
            print(f"Warning issued: {w[0].message}")

    # Example 2: strict mode — raises on failure
    print("\n--- Example 2: @confab_gate (raise mode) ---")
    try:
        deploy_agent("deploy now")
    except ConfabVerificationError as e:
        print(f"Caught ConfabVerificationError: {e}")
        print(f"  Failures: {len(e.failures)}")

    # Example 3: log mode — logs quietly
    print("\n--- Example 3: @confab_gate (log mode) ---")
    import logging
    logging.basicConfig(level=logging.WARNING)
    result = audit_agent("run audit")
    report = get_report(audit_agent)
    if report:
        print(f"Report: {report.summary()}")

    # Example 4: standalone text verification
    print("\n--- Example 4: verify_text() ---")
    standalone_verification()

    print("\n" + "=" * 60)
    print("Done. See confab.middleware for the full API.")
