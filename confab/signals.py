"""Environmental signal sources for the Fidelity Thermostat.

Reads external data (market scan, etc.) to compute the volatility signal
that drives adaptive verification thresholds.
"""

import json
from pathlib import Path
from typing import Optional


# Regimes that indicate environmental stress → higher volatility
STRESS_REGIMES = {'credit_crisis', 'stagflation', 'macro_volatility'}

# Regimes that indicate stability/growth → lower volatility
GROWTH_REGIMES = {'bull_expansion'}


def compute_volatility_from_market_scan(scan_path: Optional[Path] = None) -> float:
    """Compute a composite volatility signal (0.0–1.0) from market scan regime weights.

    Reads data/market_scan.json and blends stress regime weights (higher = more volatile)
    against growth regime weights (dampening effect).

    Args:
        scan_path: Path to market_scan.json. If None, auto-detects from workspace root.

    Returns:
        Float 0.0–1.0. Returns 0.5 (neutral) if the file is missing or unparseable.
    """
    if scan_path is None:
        scan_path = _find_market_scan()
    if scan_path is None or not scan_path.exists():
        return 0.5  # Neutral default when no signal available

    try:
        with open(scan_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0.5

    weights = data.get('regime_weights', {})
    if not weights:
        return 0.5

    stress_vals = [weights.get(r, 0.0) for r in STRESS_REGIMES]
    growth_vals = [weights.get(r, 0.0) for r in GROWTH_REGIMES]

    # Blend max and mean of stress signals — max captures acute stress,
    # mean captures breadth of stress across regimes
    stress_max = max(stress_vals) if stress_vals else 0.0
    stress_mean = sum(stress_vals) / len(stress_vals) if stress_vals else 0.0
    stress_signal = 0.5 * stress_max + 0.5 * stress_mean

    # Growth dampens the stress signal
    growth_signal = sum(growth_vals) / len(growth_vals) if growth_vals else 0.0
    composite = stress_signal * (1 - 0.3 * growth_signal)

    return max(0.0, min(1.0, composite))


def _find_market_scan() -> Optional[Path]:
    """Find data/market_scan.json relative to the workspace root."""
    from .config import get_config
    candidate = get_config().workspace_root / "data" / "market_scan.json"
    if candidate.exists():
        return candidate
    return None
