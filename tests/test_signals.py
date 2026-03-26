"""Tests for the market scan signal integration (--volatility auto)."""

import json
import tempfile
import unittest
from pathlib import Path

from confab.signals import compute_volatility_from_market_scan, STRESS_REGIMES, GROWTH_REGIMES
from confab.config import parse_volatility, adjust_thresholds, ConfabConfig, set_config, reset_config
from confab.gate import run_gate


class TestComputeVolatilityFromMarketScan(unittest.TestCase):
    """Test the composite volatility signal computation."""

    def _write_scan(self, regime_weights):
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        json.dump({"regime_weights": regime_weights}, f)
        f.close()
        return Path(f.name)

    def test_high_stress_produces_high_volatility(self):
        """Current market scan (high stress, no growth) → ~0.6+."""
        path = self._write_scan({
            "stagflation": 0.667,
            "macro_volatility": 0.6,
            "credit_crisis": 0.429,
            "bull_expansion": 0.0,
        })
        vol = compute_volatility_from_market_scan(path)
        self.assertGreaterEqual(vol, 0.6)
        self.assertLessEqual(vol, 1.0)

    def test_bull_market_produces_low_volatility(self):
        """Growth-dominant environment → low volatility."""
        path = self._write_scan({
            "stagflation": 0.1,
            "macro_volatility": 0.1,
            "credit_crisis": 0.05,
            "bull_expansion": 0.8,
        })
        vol = compute_volatility_from_market_scan(path)
        self.assertLess(vol, 0.15)

    def test_balanced_produces_medium_volatility(self):
        """Mixed stress and growth → medium volatility."""
        path = self._write_scan({
            "stagflation": 0.4,
            "macro_volatility": 0.3,
            "credit_crisis": 0.2,
            "bull_expansion": 0.5,
        })
        vol = compute_volatility_from_market_scan(path)
        self.assertGreater(vol, 0.15)
        self.assertLess(vol, 0.5)

    def test_extreme_crisis_near_max(self):
        """All stress at 1.0, no growth → near 1.0."""
        path = self._write_scan({
            "stagflation": 1.0,
            "macro_volatility": 1.0,
            "credit_crisis": 1.0,
            "bull_expansion": 0.0,
        })
        vol = compute_volatility_from_market_scan(path)
        self.assertGreaterEqual(vol, 0.9)

    def test_no_stress_no_growth_neutral(self):
        """All zeros → 0.0."""
        path = self._write_scan({
            "stagflation": 0.0,
            "macro_volatility": 0.0,
            "credit_crisis": 0.0,
            "bull_expansion": 0.0,
        })
        vol = compute_volatility_from_market_scan(path)
        self.assertAlmostEqual(vol, 0.0)

    def test_missing_file_returns_neutral(self):
        """Missing file → 0.5 neutral default."""
        vol = compute_volatility_from_market_scan(Path("/nonexistent/scan.json"))
        self.assertAlmostEqual(vol, 0.5)

    def test_invalid_json_returns_neutral(self):
        """Corrupt file → 0.5 neutral default."""
        f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
        f.write("not valid json {{{")
        f.close()
        vol = compute_volatility_from_market_scan(Path(f.name))
        self.assertAlmostEqual(vol, 0.5)

    def test_empty_regime_weights_returns_neutral(self):
        """Empty regime_weights dict → 0.5."""
        path = self._write_scan({})
        vol = compute_volatility_from_market_scan(path)
        self.assertAlmostEqual(vol, 0.5)

    def test_missing_some_regimes_still_works(self):
        """Only some regimes present → uses what's available."""
        path = self._write_scan({
            "stagflation": 0.8,
            # no macro_volatility, credit_crisis, or bull_expansion
        })
        vol = compute_volatility_from_market_scan(path)
        # stagflation 0.8, others default to 0.0
        # stress_max=0.8, stress_mean=0.8/3≈0.267, signal≈0.533
        self.assertGreater(vol, 0.3)
        self.assertLess(vol, 0.8)

    def test_result_always_in_range(self):
        """Output is always clamped to [0, 1]."""
        for stress in [0.0, 0.5, 1.0]:
            for growth in [0.0, 0.5, 1.0]:
                path = self._write_scan({
                    "stagflation": stress,
                    "macro_volatility": stress,
                    "credit_crisis": stress,
                    "bull_expansion": growth,
                })
                vol = compute_volatility_from_market_scan(path)
                self.assertGreaterEqual(vol, 0.0)
                self.assertLessEqual(vol, 1.0)


class TestParseVolatilityAuto(unittest.TestCase):
    """Test that parse_volatility('auto') calls the market scan reader."""

    def test_auto_returns_float(self):
        """'auto' produces a float (from the actual market_scan.json)."""
        vol = parse_volatility("auto")
        self.assertIsInstance(vol, float)
        self.assertGreaterEqual(vol, 0.0)
        self.assertLessEqual(vol, 1.0)

    def test_auto_case_insensitive(self):
        vol = parse_volatility("AUTO")
        self.assertIsInstance(vol, float)
        vol2 = parse_volatility("Auto")
        self.assertIsInstance(vol2, float)


class TestAutoVolatilityThresholdEffect(unittest.TestCase):
    """Verify that auto volatility from current market scan loosens thresholds."""

    def test_current_scan_adjusts_stale_threshold(self):
        """Auto volatility adjusts stale threshold in the expected direction."""
        vol = parse_volatility("auto")
        self.assertIsNotNone(vol)
        stale, ttl = adjust_thresholds(3, 6.0, vol)
        if vol > 0.5:
            # High volatility → looser thresholds
            self.assertGreaterEqual(stale, 3, f"Expected stale >= 3 at volatility {vol:.3f}, got {stale}")
            self.assertGreater(ttl, 6.0)
        else:
            # Low/neutral volatility → tighter or same thresholds
            self.assertLessEqual(stale, 3, f"Expected stale <= 3 at volatility {vol:.3f}, got {stale}")
            self.assertLessEqual(ttl, 6.0)


class TestGateAutoVolatilityIntegration(unittest.TestCase):
    """End-to-end: confab gate --volatility auto works."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def test_run_gate_with_auto_volatility(self):
        """run_gate with auto volatility doesn't error."""
        vol = parse_volatility("auto")
        report = run_gate(files=[], volatility=vol, track=False)
        self.assertIsNotNone(report)
        self.assertTrue(report.clean)


if __name__ == "__main__":
    unittest.main()
