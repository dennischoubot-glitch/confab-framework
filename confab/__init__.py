"""Confabulation Framework — structural detection and prevention for multi-agent systems.

Solves the cascade propagation problem: agents state falsehoods confidently,
other agents copy them forward indefinitely. This framework makes verification
structural (enforced by code) rather than aspirational (suggested by docs).

Quick start (CLI)::

    pip install confab-framework
    confab init                        # generate a confab.toml
    confab gate                        # run the cascade gate

Quick start (Python API)::

    from confab import ConfabGate

    gate = ConfabGate("confab.toml")
    report = gate.run()

    if report.has_failures:
        print(report.format_report())

See DESIGN.md for architecture.
"""

from .config import (
    ConfabConfig, get_config, load_config, set_config,
    parse_volatility, adjust_thresholds, VOLATILITY_PRESETS,
)
from .signals import compute_volatility_from_market_scan
from .gate import run_gate, quick_check, GateReport, ConfabGate
from .claims import extract_claims, extract_claims_from_file, Claim, ClaimType
from .verify import verify_claim, verify_all, VerificationResult, VerificationOutcome
from .middleware import (
    confab_gate, get_report, verify_text,
    ConfabVerificationError, VerificationReport,
)

__version__ = "1.4.0"

__all__ = [
    # High-level API
    "ConfabGate",
    # Decorator middleware
    "confab_gate", "get_report", "verify_text",
    "ConfabVerificationError", "VerificationReport",
    # Configuration
    "ConfabConfig", "get_config", "load_config", "set_config",
    "parse_volatility", "adjust_thresholds", "VOLATILITY_PRESETS",
    "compute_volatility_from_market_scan",
    # Gate (function-based)
    "run_gate", "quick_check", "GateReport",
    # Claims
    "extract_claims", "extract_claims_from_file", "Claim", "ClaimType",
    # Verification
    "verify_claim", "verify_all", "VerificationResult", "VerificationOutcome",
]
