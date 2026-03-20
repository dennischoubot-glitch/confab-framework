"""Tests for the verification engine."""

import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from confab.claims import Claim, ClaimType, VerifiabilityLevel
from confab.config import ConfabConfig, set_config, reset_config
from confab.verify import (
    VerificationResult,
    VerificationOutcome,
    verify_file_exists,
    verify_file_missing,
    verify_env_var,
    verify_script_syntax,
    verify_config_present,
    verify_claim,
    verify_all,
    summarize_outcomes,
    _resolve_path,
    _check_key_in_data,
)


class TestResolvePath(unittest.TestCase):
    """Test path resolution logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def test_absolute_path(self):
        p = _resolve_path("/usr/bin/python3")
        self.assertEqual(p, Path("/usr/bin/python3"))

    def test_relative_path(self):
        # Create a file in tmpdir
        (Path(self.tmpdir) / "test.py").write_text("pass")
        p = _resolve_path("test.py")
        self.assertTrue(p.exists())

    def test_subdirectory_search(self):
        """Bare filenames should be found in first-level subdirectories."""
        subdir = Path(self.tmpdir) / "subdir"
        subdir.mkdir()
        (subdir / "script.py").write_text("pass")
        p = _resolve_path("script.py")
        self.assertTrue(p.exists())

    def test_nonexistent_returns_direct(self):
        p = _resolve_path("nonexistent.py")
        self.assertEqual(p, Path(self.tmpdir) / "nonexistent.py")


class TestVerifyFileExists(unittest.TestCase):
    """Test file existence verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def test_existing_file(self):
        (Path(self.tmpdir) / "exists.py").write_text("pass")
        outcome = verify_file_exists(["exists.py"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)
        self.assertIn("EXISTS", outcome.evidence)

    def test_missing_file(self):
        outcome = verify_file_exists(["missing.py"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)
        self.assertIn("MISSING", outcome.evidence)

    def test_mixed_files(self):
        (Path(self.tmpdir) / "exists.py").write_text("pass")
        outcome = verify_file_exists(["exists.py", "missing.py"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)


class TestVerifyFileMissing(unittest.TestCase):
    """Test file-missing verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        set_config(ConfabConfig(workspace_root=Path(self.tmpdir), files_to_scan=[]))

    def tearDown(self):
        reset_config()

    def test_actually_missing(self):
        outcome = verify_file_missing(["gone.py"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)

    def test_actually_exists(self):
        (Path(self.tmpdir) / "here.py").write_text("pass")
        outcome = verify_file_missing(["here.py"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)


class TestVerifyEnvVar(unittest.TestCase):
    """Test environment variable verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        set_config(ConfabConfig(workspace_root=Path(self.tmpdir), files_to_scan=[]))

    def tearDown(self):
        reset_config()

    def test_env_var_in_environ(self):
        with patch.dict(os.environ, {"TEST_CONFAB_VAR": "value"}):
            outcome = verify_env_var(["TEST_CONFAB_VAR"])
            self.assertEqual(outcome.result, VerificationResult.PASSED)
            self.assertIn("os.environ", outcome.evidence)

    def test_env_var_missing(self):
        # Ensure it's not set
        os.environ.pop("CONFAB_NONEXISTENT_VAR", None)
        outcome = verify_env_var(["CONFAB_NONEXISTENT_VAR"])
        self.assertEqual(outcome.result, VerificationResult.INCONCLUSIVE)

    def test_env_var_in_dotenv(self):
        env_file = Path(self.tmpdir) / ".env"
        env_file.write_text("MY_SECRET_KEY=abc123\n")
        os.environ.pop("MY_SECRET_KEY", None)
        outcome = verify_env_var(["MY_SECRET_KEY"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)
        self.assertIn(".env", outcome.evidence)


class TestVerifyScriptSyntax(unittest.TestCase):
    """Test Python script syntax verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        set_config(ConfabConfig(workspace_root=Path(self.tmpdir), files_to_scan=[]))

    def tearDown(self):
        reset_config()

    def test_valid_script(self):
        (Path(self.tmpdir) / "good.py").write_text("x = 1\nprint(x)\n")
        outcome = verify_script_syntax(["good.py"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)

    def test_syntax_error(self):
        (Path(self.tmpdir) / "bad.py").write_text("def f(\n")
        outcome = verify_script_syntax(["bad.py"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)

    def test_missing_script(self):
        outcome = verify_script_syntax(["nonexistent.py"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)

    def test_non_python_skipped(self):
        (Path(self.tmpdir) / "readme.md").write_text("# Hello")
        outcome = verify_script_syntax(["readme.md"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)  # skipped = ok


class TestVerifyConfigPresent(unittest.TestCase):
    """Test config file verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        set_config(ConfabConfig(workspace_root=Path(self.tmpdir), files_to_scan=[]))

    def tearDown(self):
        reset_config()

    def test_valid_json_config(self):
        (Path(self.tmpdir) / "config.json").write_text('{"key": "value"}')
        outcome = verify_config_present(["config.json"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)

    def test_json_with_key_check(self):
        (Path(self.tmpdir) / "config.json").write_text('{"database": {"host": "localhost"}}')
        outcome = verify_config_present(["config.json"], keys=["database.host"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)

    def test_json_missing_key(self):
        (Path(self.tmpdir) / "config.json").write_text('{"database": {}}')
        outcome = verify_config_present(["config.json"], keys=["database.host"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)

    def test_invalid_json(self):
        (Path(self.tmpdir) / "bad.json").write_text("{not valid json")
        outcome = verify_config_present(["bad.json"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)

    def test_missing_config_file(self):
        outcome = verify_config_present(["missing.json"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)

    def test_valid_toml_config(self):
        (Path(self.tmpdir) / "config.toml").write_text('[section]\nkey = "val"\n')
        outcome = verify_config_present(["config.toml"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)


class TestCheckKeyInData(unittest.TestCase):
    """Test nested key checking in config data."""

    def test_simple_key(self):
        self.assertTrue(_check_key_in_data({"x": 1}, "x"))

    def test_nested_key(self):
        self.assertTrue(_check_key_in_data({"a": {"b": {"c": 1}}}, "a.b.c"))

    def test_missing_key(self):
        self.assertFalse(_check_key_in_data({"a": 1}, "b"))

    def test_missing_nested_key(self):
        self.assertFalse(_check_key_in_data({"a": {"b": 1}}, "a.c"))

    def test_non_dict_data(self):
        self.assertFalse(_check_key_in_data("not a dict", "key"))

    def test_intermediate_non_dict(self):
        self.assertFalse(_check_key_in_data({"a": "string"}, "a.b"))


class TestVerifyClaim(unittest.TestCase):
    """Test the main verify_claim dispatcher."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        set_config(ConfabConfig(workspace_root=Path(self.tmpdir), files_to_scan=[]))

    def tearDown(self):
        reset_config()

    def test_manual_claim_skipped(self):
        claim = Claim(
            text="the code quality is excellent",
            claim_type=ClaimType.SUBJECTIVE,
            verifiability=VerifiabilityLevel.MANUAL,
        )
        outcome = verify_claim(claim)
        self.assertEqual(outcome.result, VerificationResult.SKIPPED)

    def test_file_exists_dispatch(self):
        (Path(self.tmpdir) / "exists.py").write_text("pass")
        claim = Claim(
            text="`exists.py` is ready",
            claim_type=ClaimType.FILE_EXISTS,
            verifiability=VerifiabilityLevel.AUTO,
            extracted_paths=["exists.py"],
        )
        outcome = verify_claim(claim)
        self.assertEqual(outcome.result, VerificationResult.PASSED)

    def test_env_var_blocker_logic(self):
        """When an env var IS present, a blocker claim should FAIL."""
        with patch.dict(os.environ, {"TEST_KEY": "value"}):
            claim = Claim(
                text="blocked on TEST_KEY",
                claim_type=ClaimType.ENV_VAR,
                verifiability=VerifiabilityLevel.AUTO,
                extracted_env_vars=["TEST_KEY"],
            )
            outcome = verify_claim(claim)
            # Blocker claims FAIL when the var exists (blocker is false)
            self.assertEqual(outcome.result, VerificationResult.FAILED)


class TestVerifyAll(unittest.TestCase):
    """Test batch verification."""

    def test_empty_list(self):
        self.assertEqual(verify_all([]), [])

    def test_multiple_claims(self):
        claims = [
            Claim(text="a", claim_type=ClaimType.SUBJECTIVE, verifiability=VerifiabilityLevel.MANUAL),
            Claim(text="b", claim_type=ClaimType.SUBJECTIVE, verifiability=VerifiabilityLevel.MANUAL),
        ]
        outcomes = verify_all(claims)
        self.assertEqual(len(outcomes), 2)


class TestSummarizeOutcomes(unittest.TestCase):
    """Test outcome summarization."""

    def test_empty(self):
        summary = summarize_outcomes([])
        self.assertEqual(summary["total_checked"], 0)

    def test_mixed_results(self):
        outcomes = [
            VerificationOutcome(
                claim=Claim(text="a", claim_type=ClaimType.FILE_EXISTS, verifiability=VerifiabilityLevel.AUTO),
                result=VerificationResult.PASSED,
                evidence="ok",
                checked_at="2026-01-01",
                method="test",
            ),
            VerificationOutcome(
                claim=Claim(text="b", claim_type=ClaimType.FILE_EXISTS, verifiability=VerifiabilityLevel.AUTO),
                result=VerificationResult.FAILED,
                evidence="missing",
                checked_at="2026-01-01",
                method="test",
            ),
        ]
        summary = summarize_outcomes(outcomes)
        self.assertEqual(summary["passed"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(len(summary["failed_claims"]), 1)


if __name__ == "__main__":
    unittest.main()
