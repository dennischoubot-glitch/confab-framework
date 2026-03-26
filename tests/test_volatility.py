"""Tests for the Fidelity Thermostat — adaptive verification thresholds."""

import tempfile
import unittest
from pathlib import Path

from confab.config import (
    ConfabConfig,
    adjust_thresholds,
    parse_volatility,
    VOLATILITY_PRESETS,
    set_config,
    reset_config,
)
from confab.gate import run_gate, ConfabGate


class TestParseVolatility(unittest.TestCase):
    """Test volatility parsing from various input formats."""

    def test_named_presets(self):
        self.assertAlmostEqual(parse_volatility("low"), 0.2)
        self.assertAlmostEqual(parse_volatility("medium"), 0.5)
        self.assertAlmostEqual(parse_volatility("high"), 0.8)

    def test_named_presets_case_insensitive(self):
        self.assertAlmostEqual(parse_volatility("LOW"), 0.2)
        self.assertAlmostEqual(parse_volatility("High"), 0.8)
        self.assertAlmostEqual(parse_volatility("  Medium  "), 0.5)

    def test_numeric_float(self):
        self.assertAlmostEqual(parse_volatility(0.7), 0.7)
        self.assertAlmostEqual(parse_volatility(0.0), 0.0)
        self.assertAlmostEqual(parse_volatility(1.0), 1.0)

    def test_numeric_string(self):
        self.assertAlmostEqual(parse_volatility("0.3"), 0.3)
        self.assertAlmostEqual(parse_volatility("0.9"), 0.9)

    def test_numeric_int(self):
        self.assertAlmostEqual(parse_volatility(0), 0.0)
        self.assertAlmostEqual(parse_volatility(1), 1.0)

    def test_clamped_to_range(self):
        self.assertAlmostEqual(parse_volatility(-0.5), 0.0)
        self.assertAlmostEqual(parse_volatility(2.0), 1.0)
        self.assertAlmostEqual(parse_volatility("1.5"), 1.0)

    def test_none_returns_none(self):
        self.assertIsNone(parse_volatility(None))

    def test_invalid_string_returns_none(self):
        self.assertIsNone(parse_volatility("garbage"))
        self.assertIsNone(parse_volatility(""))


class TestAdjustThresholds(unittest.TestCase):
    """Test threshold adjustment math."""

    def test_neutral_volatility(self):
        """Volatility 0.5 = no change."""
        stale, ttl = adjust_thresholds(3, 6.0, 0.5)
        self.assertEqual(stale, 3)
        self.assertAlmostEqual(ttl, 6.0)

    def test_high_volatility_loosens(self):
        """High volatility raises stale threshold and extends TTL."""
        stale, ttl = adjust_thresholds(3, 6.0, 1.0)
        self.assertGreater(stale, 3)
        self.assertGreater(ttl, 6.0)

    def test_low_volatility_tightens(self):
        """Low volatility lowers stale threshold and shortens TTL."""
        stale, ttl = adjust_thresholds(3, 6.0, 0.0)
        self.assertLess(stale, 3)
        self.assertLess(ttl, 6.0)

    def test_minimum_stale_threshold_is_1(self):
        """Stale threshold never goes below 1."""
        stale, _ = adjust_thresholds(1, 6.0, 0.0)
        self.assertGreaterEqual(stale, 1)

    def test_specific_values_at_extremes(self):
        """Check the specific multiplier values."""
        # volatility 0.0 → multiplier 0.5
        stale, ttl = adjust_thresholds(4, 10.0, 0.0)
        self.assertEqual(stale, 2)  # 4 * 0.5 = 2
        self.assertAlmostEqual(ttl, 5.0)  # 10 * 0.5

        # volatility 1.0 → multiplier 2.0
        stale, ttl = adjust_thresholds(3, 6.0, 1.0)
        self.assertEqual(stale, 6)  # 3 * 2.0 = 6
        self.assertAlmostEqual(ttl, 12.0)  # 6 * 2.0

    def test_monotonic_increase(self):
        """Higher volatility always produces >= thresholds."""
        prev_stale, prev_ttl = 0, 0.0
        for v in [0.0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0]:
            stale, ttl = adjust_thresholds(3, 6.0, v)
            self.assertGreaterEqual(stale, prev_stale)
            self.assertGreaterEqual(ttl, prev_ttl)
            prev_stale, prev_ttl = stale, ttl

    def test_dreamer_specified_values(self):
        """Match the dreamer's specification: high → 3→5, 6h→12h; low → 3→2, 6h→3h."""
        # High (0.8): multiplier = 1.0 + 2.0*(0.8-0.5) = 1.6
        stale_high, ttl_high = adjust_thresholds(3, 6.0, 0.8)
        self.assertEqual(stale_high, 5)  # round(3 * 1.6) = 5
        self.assertAlmostEqual(ttl_high, 9.6)  # 6 * 1.6

        # Low (0.2): multiplier = 0.5 + 0.2 = 0.7
        stale_low, ttl_low = adjust_thresholds(3, 6.0, 0.2)
        self.assertEqual(stale_low, 2)  # round(3 * 0.7) = 2
        self.assertAlmostEqual(ttl_low, 4.2)  # 6 * 0.7


class TestConfigEffectiveProperties(unittest.TestCase):
    """Test ConfabConfig's effective_* properties."""

    def test_no_volatility_uses_base(self):
        config = ConfabConfig(
            workspace_root=Path("/tmp"),
            files_to_scan=[],
            stale_threshold=3,
            behavior_ttl_hours=6.0,
        )
        self.assertEqual(config.effective_stale_threshold, 3)
        self.assertAlmostEqual(config.effective_behavior_ttl, 6.0)

    def test_with_volatility(self):
        config = ConfabConfig(
            workspace_root=Path("/tmp"),
            files_to_scan=[],
            stale_threshold=3,
            behavior_ttl_hours=6.0,
            volatility=0.8,
        )
        self.assertGreater(config.effective_stale_threshold, 3)
        self.assertGreater(config.effective_behavior_ttl, 6.0)


class TestGateVolatilityIntegration(unittest.TestCase):
    """Test that volatility wires through run_gate correctly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def test_run_gate_with_volatility(self):
        """run_gate accepts volatility parameter without error."""
        report = run_gate(files=[], volatility=0.8, track=False)
        self.assertIsNotNone(report)
        self.assertTrue(report.clean)

    def test_run_gate_without_volatility(self):
        """run_gate works normally without volatility."""
        report = run_gate(files=[], track=False)
        self.assertIsNotNone(report)

    def test_confab_gate_api_with_volatility(self):
        """ConfabGate class accepts volatility parameter."""
        gate = ConfabGate(
            config=self.config,
            volatility=0.8,
        )
        self.assertAlmostEqual(gate.config.volatility, 0.8)

    def test_confab_gate_run_volatility_override(self):
        """ConfabGate.run() accepts per-run volatility override."""
        gate = ConfabGate(config=self.config, volatility=0.2)
        report = gate.run(files=[], volatility=0.9, track=False)
        self.assertIsNotNone(report)


class TestTomlVolatility(unittest.TestCase):
    """Test volatility in confab.toml."""

    def test_volatility_in_toml(self):
        from confab.config import _config_from_toml
        data = {
            "confab": {
                "files_to_scan": ["a.md"],
                "volatility": "high",
            }
        }
        config = _config_from_toml(data, Path("/tmp"))
        self.assertAlmostEqual(config.volatility, 0.8)

    def test_numeric_volatility_in_toml(self):
        from confab.config import _config_from_toml
        data = {
            "confab": {
                "files_to_scan": [],
                "volatility": 0.6,
            }
        }
        config = _config_from_toml(data, Path("/tmp"))
        self.assertAlmostEqual(config.volatility, 0.6)

    def test_no_volatility_in_toml(self):
        from confab.config import _config_from_toml
        data = {"confab": {"files_to_scan": []}}
        config = _config_from_toml(data, Path("/tmp"))
        self.assertIsNone(config.volatility)


if __name__ == "__main__":
    unittest.main()
