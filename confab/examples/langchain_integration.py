"""Example: LangChain agent with confabulation detection.

Demonstrates how to use ConfabCallbackHandler to automatically verify
claims in LangChain agent outputs. The handler hooks into LangChain's
callback system and runs confab verification on every agent response.

Usage::

    pip install confab-framework[langchain] langchain-openai
    python examples/langchain_integration.py

The example works without an LLM by simulating agent outputs, so you can
run it immediately to see confab detection in action.
"""

from __future__ import annotations

import warnings
from typing import Any, Dict, List

# -- confab integration --
from confab.integrations.langchain import ConfabCallbackHandler
from confab.middleware import ConfabVerificationError


def simulate_agent_outputs() -> List[Dict[str, str]]:
    """Simulate agent outputs with mixed true/false claims.

    In a real setup, these would come from a LangChain agent. We simulate
    them here so the example runs without API keys or LLM providers.
    """
    return [
        {
            "label": "True claims (should pass)",
            "output": (
                "I checked the project configuration.\n"
                "- The pyproject.toml file exists and is valid\n"
                "- The README.md file is present\n"
            ),
        },
        {
            "label": "False claims (should fail)",
            "output": (
                "Deployment complete:\n"
                "- Wrote results to /tmp/confab_demo_output.json\n"
                "- Updated config at /opt/nonexistent/app.yaml\n"
                "- Pipeline output at /data/results/final.csv is ready\n"
            ),
        },
        {
            "label": "Mixed claims",
            "output": (
                "Status report:\n"
                "- The pyproject.toml configuration is valid\n"
                "- Database backup at /backups/latest.sql.gz is current\n"
                "- Environment requires CONFAB_EXAMPLE_SECRET to be set\n"
            ),
        },
    ]


# --- Example 1: Warn mode (default) ---

def example_warn_mode():
    """Warn on failures but continue execution."""
    print("\n--- Example 1: Warn mode ---")

    handler = ConfabCallbackHandler(on_fail="warn")

    for case in simulate_agent_outputs():
        print(f"\n  [{case['label']}]")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # Simulate what LangChain does on chain_end
            handler.on_chain_end({"output": case["output"]})
            if w:
                for warning in w:
                    print(f"  WARNING: {warning.message}")
            else:
                print("  No warnings (all claims verified)")

    print(f"\n  Overall: {handler.summary()}")


# --- Example 2: Raise mode (strict) ---

def example_raise_mode():
    """Raise an exception on the first failure."""
    print("\n--- Example 2: Raise mode (strict) ---")

    handler = ConfabCallbackHandler(on_fail="raise")

    # This should pass (references real files)
    print("\n  [True claims]")
    handler.on_chain_end({
        "output": "The pyproject.toml file has been verified."
    })
    print(f"  Passed: {handler.last_report.summary()}")

    # This should raise (references nonexistent file)
    print("\n  [False claims]")
    try:
        handler.on_chain_end({
            "output": "Data exported to /tmp/confab_nonexistent_export.csv"
        })
    except ConfabVerificationError as e:
        print(f"  Caught ConfabVerificationError ({len(e.failures)} failures)")

    print(f"\n  Overall: {handler.summary()}")


# --- Example 3: Log mode (production) ---

def example_log_mode():
    """Log failures quietly (for production pipelines)."""
    import logging

    print("\n--- Example 3: Log mode (production) ---")

    logging.basicConfig(level=logging.WARNING, format="  %(name)s: %(message)s")

    handler = ConfabCallbackHandler(on_fail="log")

    for case in simulate_agent_outputs():
        handler.on_chain_end({"output": case["output"]})

    print(f"  Reports collected: {len(handler.reports)}")
    print(f"  Overall: {handler.summary()}")


# --- Example 4: With AgentFinish (simulated) ---

def example_agent_finish():
    """Verify claims from AgentFinish events."""
    from langchain_core.agents import AgentFinish

    print("\n--- Example 4: AgentFinish verification ---")

    handler = ConfabCallbackHandler(on_fail="warn")

    # Simulate an agent that makes a false claim
    finish = AgentFinish(
        return_values={
            "output": (
                "Task complete. Results saved to /tmp/confab_agent_results.json. "
                "The pyproject.toml config was verified."
            ),
        },
        log="Agent completed task.",
    )

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        handler.on_agent_finish(finish)

    report = handler.last_report
    print(f"  Claims found: {report.claims_found}")
    print(f"  Passed: {report.passed}, Failed: {report.failed}")
    if not report.clean:
        print(f"  Failures:")
        for f in report.failures:
            print(f"    - {f.claim.text[:70]}...")
            print(f"      Evidence: {f.evidence[:100]}")


# --- Example 5: Real LangChain agent (requires API key) ---

def example_real_agent():
    """How to wire confab into a real LangChain agent.

    This example shows the pattern but doesn't execute, since it requires
    an API key. Uncomment and run with OPENAI_API_KEY set.
    """
    print("\n--- Example 5: Real agent pattern (not executed) ---")
    print("  To use with a real agent:")
    print()
    print("    from langchain_openai import ChatOpenAI")
    print("    from langchain.agents import AgentExecutor, create_tool_calling_agent")
    print("    from confab.integrations.langchain import ConfabCallbackHandler")
    print()
    print("    handler = ConfabCallbackHandler(on_fail='warn')")
    print("    llm = ChatOpenAI(model='gpt-4o')")
    print("    # ... create agent with tools ...")
    print("    result = agent_executor.invoke(")
    print("        {'input': 'Check system status'},")
    print("        config={'callbacks': [handler]},")
    print("    )")
    print("    if not handler.clean:")
    print("        print(handler.summary())")


# --- Run all examples ---

if __name__ == "__main__":
    print("=" * 60)
    print("Confab Framework — LangChain Integration Examples")
    print("=" * 60)

    example_warn_mode()
    example_raise_mode()
    example_log_mode()

    # Example 4 requires langchain_core to be installed
    try:
        example_agent_finish()
    except ImportError:
        print("\n--- Example 4: Skipped (langchain-core not installed) ---")

    example_real_agent()

    print("\n" + "=" * 60)
    print("Done. See confab.integrations.langchain for the full API.")
