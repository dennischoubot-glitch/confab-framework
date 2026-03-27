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
    verify_process_status,
    verify_all_services,
    verify_registry,
    verify_claim,
    verify_all,
    verify_count,
    summarize_outcomes,
    _resolve_path,
    _check_key_in_data,
    _check_supervisorctl,
    _check_systemd,
    _check_ps,
    _check_pid_file,
    _check_port,
    _is_all_services_claim,
    _verify_test_count,
    _find_scoped_test_dir,
    _find_all_test_dirs,
    _count_tests_in_dir,
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


class TestVerifyRegistry(unittest.TestCase):
    """Test registry verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        core_dir = Path(self.tmpdir) / "core"
        core_dir.mkdir()
        (core_dir / "SYSTEM_REGISTRY.md").write_text(
            "# System Registry\n\n"
            "| Path | Purpose | Status |\n"
            "| `kalshi_market_data.db` | Market data | canonical |\n"
            "| `scripts/kalshi_portfolio.py` | Portfolio | canonical |\n"
        )
        set_config(ConfabConfig(workspace_root=Path(self.tmpdir), files_to_scan=[]))

    def tearDown(self):
        reset_config()

    def test_registered_file_passes(self):
        outcome = verify_registry(["kalshi_market_data.db"])
        self.assertEqual(outcome.result, VerificationResult.PASSED)

    def test_unregistered_file_fails(self):
        outcome = verify_registry(["orphan_data.db"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)
        self.assertIn("NOT in SYSTEM_REGISTRY.md", outcome.evidence)

    def test_mixed_registered_and_unregistered(self):
        outcome = verify_registry(["kalshi_market_data.db", "unknown.db"])
        self.assertEqual(outcome.result, VerificationResult.FAILED)

    def test_no_registry_file(self):
        import os
        os.remove(str(Path(self.tmpdir) / "core" / "SYSTEM_REGISTRY.md"))
        outcome = verify_registry(["anything.db"])
        self.assertEqual(outcome.result, VerificationResult.INCONCLUSIVE)


class TestVerifyProcessStatus(unittest.TestCase):
    """Test process/service status verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            process_services={
                "weather-rewards": {
                    "manager": "supervisorctl",
                    "config": "slack-bridge/supervisord.conf",
                    "service_name": "ia-services:weather-rewards",
                },
                "weather rewards": {
                    "manager": "supervisorctl",
                    "config": "slack-bridge/supervisord.conf",
                    "service_name": "ia-services:weather-rewards",
                },
                "slack-monitor": {
                    "manager": "ps",
                    "service_name": "slack-monitor",
                },
            },
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def test_no_keyword_match_inconclusive(self):
        """Claims about unknown services should be INCONCLUSIVE."""
        claim = Claim(
            text="database server is running",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_process_status(claim)
        self.assertEqual(outcome.result, VerificationResult.INCONCLUSIVE)

    @patch('confab.verify.subprocess.run')
    def test_claims_running_actually_running_passes(self, mock_run):
        """Claim says running, process IS running → PASSED."""
        mock_run.return_value = type('', (), {
            'stdout': 'ia-services:weather-rewards   RUNNING   pid 12345, uptime 1:00:00',
            'stderr': '',
            'returncode': 0,
        })()
        claim = Claim(
            text="Weather rewards monitor: running [v1: verified 2026-03-14]",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_process_status(claim)
        self.assertEqual(outcome.result, VerificationResult.PASSED)

    @patch('confab.verify.subprocess.run')
    def test_dash_space_normalization(self, mock_run):
        """Config key 'weather-rewards' should match claim text 'weather rewards' (space)."""
        mock_run.return_value = type('', (), {
            'stdout': 'ia-services:weather-rewards   RUNNING   pid 12345, uptime 1:00:00',
            'stderr': '',
            'returncode': 0,
        })()
        claim = Claim(
            text="Weather rewards monitor: RUNNING (pid 65048)",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_process_status(claim)
        # Should match via normalized dash/space — not INCONCLUSIVE
        self.assertEqual(outcome.result, VerificationResult.PASSED)
        self.assertIn("weather", outcome.evidence.lower())

    @patch('confab.verify.subprocess.run')
    def test_claims_running_actually_stopped_fails(self, mock_run):
        """Claim says running, process is STOPPED → FAILED."""
        mock_run.return_value = type('', (), {
            'stdout': 'ia-services:weather-rewards   STOPPED   Mar 14 07:41 PM',
            'stderr': '',
            'returncode': 0,
        })()
        claim = Claim(
            text="Weather rewards monitor: running [v1: verified 2026-03-14]",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_process_status(claim)
        self.assertEqual(outcome.result, VerificationResult.FAILED)
        self.assertIn("STOPPED", outcome.evidence)

    @patch('confab.verify.subprocess.run')
    def test_claims_stopped_actually_stopped_passes(self, mock_run):
        """Claim says stopped, process IS stopped → PASSED."""
        mock_run.return_value = type('', (), {
            'stdout': 'ia-services:weather-rewards   STOPPED   Mar 14 07:41 PM',
            'stderr': '',
            'returncode': 0,
        })()
        claim = Claim(
            text="weather-rewards: stopped since March 14",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_process_status(claim)
        self.assertEqual(outcome.result, VerificationResult.PASSED)

    @patch('confab.verify.subprocess.run')
    def test_supervisorctl_not_found(self, mock_run):
        """Missing supervisorctl should be INCONCLUSIVE, not crash."""
        mock_run.side_effect = FileNotFoundError("supervisorctl not found")
        claim = Claim(
            text="Weather rewards monitor: running",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_process_status(claim)
        self.assertEqual(outcome.result, VerificationResult.INCONCLUSIVE)

    def test_check_supervisorctl_parsing(self):
        """Test supervisorctl output parsing."""
        with patch('confab.verify.subprocess.run') as mock_run:
            mock_run.return_value = type('', (), {
                'stdout': 'ia-services:weather-rewards   RUNNING   pid 83794, uptime 6 days, 20:43:02',
                'stderr': '',
                'returncode': 0,
            })()
            status, detail = _check_supervisorctl(
                "ia-services:weather-rewards", None, Path(self.tmpdir)
            )
            self.assertEqual(status, "running")

    def test_check_supervisorctl_stopped(self):
        with patch('confab.verify.subprocess.run') as mock_run:
            mock_run.return_value = type('', (), {
                'stdout': 'ia-services:weather-rewards   STOPPED   Mar 14 07:41 PM',
                'stderr': '',
                'returncode': 0,
            })()
            status, detail = _check_supervisorctl(
                "ia-services:weather-rewards", None, Path(self.tmpdir)
            )
            self.assertEqual(status, "stopped")

    def test_check_ps_running(self):
        with patch('confab.verify.subprocess.run') as mock_run:
            mock_run.return_value = type('', (), {
                'stdout': '12345\n',
                'stderr': '',
                'returncode': 0,
            })()
            status, detail = _check_ps("slack-monitor")
            self.assertEqual(status, "running")
            self.assertIn("12345", detail)

    def test_check_ps_stopped(self):
        with patch('confab.verify.subprocess.run') as mock_run:
            mock_run.return_value = type('', (), {
                'stdout': '',
                'stderr': '',
                'returncode': 1,
            })()
            status, detail = _check_ps("nonexistent-process")
            self.assertEqual(status, "stopped")

    def test_dispatcher_routes_process_status(self):
        """verify_claim should route PROCESS_STATUS to verify_process_status."""
        with patch('confab.verify.subprocess.run') as mock_run:
            mock_run.return_value = type('', (), {
                'stdout': 'ia-services:weather-rewards   STOPPED   Mar 14',
                'stderr': '',
                'returncode': 0,
            })()
            claim = Claim(
                text="Weather rewards monitor: running",
                claim_type=ClaimType.PROCESS_STATUS,
                verifiability=VerifiabilityLevel.AUTO,
            )
            outcome = verify_claim(claim)
            self.assertEqual(outcome.result, VerificationResult.FAILED)
            self.assertEqual(outcome.method, "process_status_check")


class TestIsAllServicesClaim(unittest.TestCase):
    """Test the all-services detection helper."""

    def test_detects_all_services(self):
        self.assertTrue(_is_all_services_claim("All services RUNNING"))
        self.assertTrue(_is_all_services_claim("**All services RUNNING** [v1: verified 2026-03-25]"))
        self.assertTrue(_is_all_services_claim("all services operational"))

    def test_rejects_specific_service(self):
        self.assertFalse(_is_all_services_claim("Weather rewards monitor: running"))
        self.assertFalse(_is_all_services_claim("slack-monitor is running"))


class TestCheckPidFile(unittest.TestCase):
    """Test pid file verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        set_config(ConfabConfig(workspace_root=Path(self.tmpdir), files_to_scan=[]))

    def tearDown(self):
        reset_config()

    def test_missing_pid_file(self):
        status, detail = _check_pid_file("nonexistent.pid")
        self.assertEqual(status, "unknown")
        self.assertIn("does not exist", detail)

    def test_pid_alive(self):
        pid_file = Path(self.tmpdir) / "test.pid"
        pid_file.write_text(str(os.getpid()))  # Current process is alive
        status, detail = _check_pid_file(str(pid_file))
        self.assertEqual(status, "running")
        self.assertIn("alive", detail)

    def test_pid_dead(self):
        pid_file = Path(self.tmpdir) / "test.pid"
        pid_file.write_text("999999999")  # Very unlikely to be alive
        status, detail = _check_pid_file(str(pid_file))
        self.assertEqual(status, "stopped")

    def test_invalid_pid(self):
        pid_file = Path(self.tmpdir) / "test.pid"
        pid_file.write_text("not-a-number")
        status, detail = _check_pid_file(str(pid_file))
        self.assertEqual(status, "unknown")
        self.assertIn("non-integer", detail)


class TestCheckPort(unittest.TestCase):
    """Test port checking."""

    def test_closed_port(self):
        # Port 19999 is very unlikely to be in use
        status, detail = _check_port(19999, "127.0.0.1")
        self.assertIn(status, ("stopped", "unknown"))

    @patch('socket.socket')
    def test_open_port(self, mock_socket_cls):
        mock_sock = mock_socket_cls.return_value
        mock_sock.connect_ex.return_value = 0
        status, detail = _check_port(8080)
        self.assertEqual(status, "running")
        self.assertIn("accepting connections", detail)


class TestVerifyAllServices(unittest.TestCase):
    """Test blanket 'all services' verification."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config = ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            process_services={
                "svc-a": {
                    "manager": "ps",
                    "service_name": "svc-a",
                },
                "svc-b": {
                    "manager": "ps",
                    "service_name": "svc-b",
                },
            },
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    @patch('confab.verify.subprocess.run')
    def test_all_running_passes(self, mock_run):
        """All services running + claim says running → PASSED."""
        mock_run.return_value = type('', (), {
            'stdout': '12345\n',
            'stderr': '',
            'returncode': 0,
        })()
        claim = Claim(
            text="All services RUNNING",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_all_services(claim)
        self.assertEqual(outcome.result, VerificationResult.PASSED)
        self.assertEqual(outcome.method, "all_services_check")

    @patch('confab.verify.subprocess.run')
    def test_one_stopped_fails(self, mock_run):
        """One service stopped + claim says all running → FAILED."""
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return type('', (), {
                    'stdout': '12345\n', 'stderr': '', 'returncode': 0,
                })()
            else:
                return type('', (), {
                    'stdout': '', 'stderr': '', 'returncode': 1,
                })()
        mock_run.side_effect = side_effect
        claim = Claim(
            text="All services RUNNING",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_all_services(claim)
        self.assertEqual(outcome.result, VerificationResult.FAILED)
        self.assertIn("not running", outcome.evidence)

    def test_no_services_configured(self):
        """No process_services → INCONCLUSIVE."""
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            process_services={},
        ))
        claim = Claim(
            text="All services RUNNING",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_all_services(claim)
        self.assertEqual(outcome.result, VerificationResult.INCONCLUSIVE)

    @patch('confab.verify.subprocess.run')
    def test_routes_through_verify_process_status(self, mock_run):
        """verify_process_status should detect 'all services' and route correctly."""
        mock_run.return_value = type('', (), {
            'stdout': '12345\n', 'stderr': '', 'returncode': 0,
        })()
        claim = Claim(
            text="**All services RUNNING** [v1: verified 2026-03-25 6:00AM]",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_process_status(claim)
        self.assertEqual(outcome.method, "all_services_check")

    @patch('confab.verify.subprocess.run')
    def test_with_pid_file_supplement(self, mock_run):
        """Pid file supplements manager check when configured."""
        # Manager returns unknown
        mock_run.return_value = type('', (), {
            'stdout': '', 'stderr': '', 'returncode': 1,
        })()
        # Configure service with pid file
        pid_file = Path(self.tmpdir) / "test.pid"
        pid_file.write_text(str(os.getpid()))
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            process_services={
                "svc-a": {
                    "manager": "ps",
                    "service_name": "svc-a",
                    "pid_file": str(pid_file),
                },
            },
        ))
        claim = Claim(
            text="All services RUNNING",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_all_services(claim)
        self.assertIn("pid", outcome.evidence.lower())

    @patch('confab.verify.subprocess.run')
    def test_deduplicates_aliases(self, mock_run):
        """Alias entries pointing to same service_name should only be checked once."""
        mock_run.return_value = type('', (), {
            'stdout': '12345\n', 'stderr': '', 'returncode': 0,
        })()
        set_config(ConfabConfig(
            workspace_root=Path(self.tmpdir),
            files_to_scan=[],
            process_services={
                "weather-rewards": {
                    "manager": "ps",
                    "service_name": "ia-services:weather-rewards",
                },
                "weather monitor": {
                    "manager": "ps",
                    "service_name": "ia-services:weather-rewards",
                },
                "slack-monitor": {
                    "manager": "ps",
                    "service_name": "ia-services:slack-monitor",
                },
            },
        ))
        claim = Claim(
            text="All services RUNNING",
            claim_type=ClaimType.PROCESS_STATUS,
            verifiability=VerifiabilityLevel.AUTO,
        )
        outcome = verify_all_services(claim)
        # Should check 2 unique services, not 3
        self.assertIn("2 services", outcome.evidence)
        self.assertEqual(outcome.result, VerificationResult.PASSED)


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


class TestVerifyTestCount(unittest.TestCase):
    """Test the test count verification logic.

    Reproduces the false positive: an unscoped claim like "297 tests passing"
    was checked against the workspace-level tests/ directory (547 tests) instead
    of searching all directories to find the matching core/confab/tests/ (297).
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.root = Path(self.tmpdir)
        # Create a workspace-level tests/ with 5 test functions
        ws_tests = self.root / "tests"
        ws_tests.mkdir()
        (ws_tests / "test_main.py").write_text(
            "def test_a(): pass\n"
            "def test_b(): pass\n"
            "def test_c(): pass\n"
            "def test_d(): pass\n"
            "def test_e(): pass\n"
        )
        # Create a component tests/ with 10 test functions
        comp = self.root / "core" / "mylib"
        comp.mkdir(parents=True)
        comp_tests = comp / "tests"
        comp_tests.mkdir()
        (comp_tests / "test_core.py").write_text(
            "\n".join(f"def test_func_{i}(): pass" for i in range(10))
        )
        self.config = ConfabConfig(
            workspace_root=self.root,
            files_to_scan=[],
        )
        set_config(self.config)

    def tearDown(self):
        reset_config()

    def _make_claim(self, text, numbers):
        return Claim(
            text=text,
            source_file="test.md",
            source_line=1,
            claim_type=ClaimType.COUNT_CLAIM,
            verifiability=VerifiabilityLevel.AUTO,
            extracted_paths=[],
            extracted_numbers=[str(n) for n in numbers],
        )

    def test_scoped_claim_matches_component(self):
        """Claim mentioning 'mylib' should scope to core/mylib/tests/."""
        claim = self._make_claim("mylib has 10 tests", [10])
        result = _verify_test_count(claim, self.root, "now")
        self.assertEqual(result.result, VerificationResult.PASSED)
        self.assertIn("core/mylib/tests", result.evidence)

    def test_scoped_claim_wrong_count_fails(self):
        """Scoped claim with wrong count should fail."""
        claim = self._make_claim("mylib has 50 tests", [50])
        result = _verify_test_count(claim, self.root, "now")
        self.assertEqual(result.result, VerificationResult.FAILED)

    def test_unscoped_claim_finds_best_match(self):
        """Unscoped '10 tests passing' should find core/mylib/tests/ (10),
        not workspace tests/ (5). This is the false positive regression test."""
        claim = self._make_claim("10 tests passing", [10])
        result = _verify_test_count(claim, self.root, "now")
        self.assertEqual(result.result, VerificationResult.PASSED)
        self.assertIn("core/mylib/tests", result.evidence)

    def test_unscoped_claim_matches_workspace(self):
        """Unscoped '5 tests' should match workspace tests/ (5)."""
        claim = self._make_claim("5 tests pass", [5])
        result = _verify_test_count(claim, self.root, "now")
        self.assertEqual(result.result, VerificationResult.PASSED)

    def test_unscoped_claim_no_match_fails(self):
        """Unscoped claim with count matching no directory should fail."""
        claim = self._make_claim("999 tests", [999])
        result = _verify_test_count(claim, self.root, "now")
        self.assertEqual(result.result, VerificationResult.FAILED)

    def test_find_all_test_dirs(self):
        """Should find both workspace and component test dirs."""
        dirs = _find_all_test_dirs(self.root)
        labels = [str(d.relative_to(self.root)) for d in dirs]
        self.assertIn("tests", labels)
        self.assertIn("core/mylib/tests", labels)

    def test_find_scoped_returns_none_when_unscoped(self):
        """Unscoped claim should return None from _find_scoped_test_dir."""
        result = _find_scoped_test_dir("297 tests passing", self.root)
        self.assertIsNone(result)

    def test_count_tests_in_dir(self):
        """Should count test functions correctly."""
        count, files = _count_tests_in_dir(self.root / "core" / "mylib" / "tests")
        self.assertEqual(count, 10)
        self.assertEqual(files, 1)


if __name__ == "__main__":
    unittest.main()
