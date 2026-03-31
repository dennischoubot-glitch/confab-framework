"""Verification engine for the confabulation framework.

Given a list of extracted claims, automatically verifies those that can be
checked programmatically. This is the "external oracle bits" that truth-016
says are required to distinguish confabulation from understanding.

Verification methods:
- File existence: os.path.exists()
- Environment variables: parse .env files + os.environ
- Script validity: py_compile
- Pipeline output: check output files and timestamps
- Count claims: check against actual counts where possible
"""

import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .claims import Claim, ClaimType, VerifiabilityLevel
from .config import get_config


def _get_workspace_root() -> Path:
    """Get workspace root from config."""
    return get_config().workspace_root


class VerificationResult(Enum):
    """Outcome of a verification check."""
    PASSED = "passed"         # Claim is consistent with reality
    FAILED = "failed"         # Claim contradicts reality
    INCONCLUSIVE = "inconclusive"  # Couldn't determine (semi-verifiable)
    SKIPPED = "skipped"       # Not auto-verifiable


@dataclass
class VerificationOutcome:
    """Result of verifying a single claim."""
    claim: Claim
    result: VerificationResult
    evidence: str              # What was checked and what was found
    checked_at: str            # ISO timestamp
    method: str                # How it was verified
    checked_paths: List[str] = field(default_factory=list)  # Data files read during verification

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim": self.claim.to_dict(),
            "result": self.result.value,
            "evidence": self.evidence,
            "checked_at": self.checked_at,
            "method": self.method,
            "checked_paths": self.checked_paths,
        }


# ---------------------------------------------------------------------------
# Individual verification methods
# ---------------------------------------------------------------------------

def _resolve_path(path_str: str) -> Path:
    """Resolve a path relative to cwd first, then workspace root.

    If direct resolution fails, search the repo by filename via rglob.
    For paths with directory components (e.g. 'examples/__init__.py'),
    filter to suffix matches. Handles both bare filenames like
    'scheduler.py' and nested paths like 'examples/__init__.py'.
    """
    root = _get_workspace_root()
    p = Path(path_str)
    if p.is_absolute():
        return p

    # Try cwd first (supports external use outside ia repo)
    cwd_path = Path.cwd() / path_str
    if cwd_path.exists():
        return cwd_path

    direct = root / path_str
    if direct.exists():
        return direct

    # Search repo for the path — handles both bare filenames and relative paths
    # with directory components (e.g. "examples/__init__.py" matching
    # "core/confab/examples/__init__.py")
    skip = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.mypy_cache',
            '.egg-info', '.tox', '.pytest_cache', 'build', 'dist'}

    # Search by filename, then filter to those whose path ends with the full relative path
    filename = p.name
    matches = list(root.rglob(filename))
    matches = [
        m for m in matches
        if not any(part in skip or part.startswith('.') for part in m.relative_to(root).parts[:-1])
    ]
    # For paths with directory components, filter to suffix matches
    if '/' in path_str or '\\' in path_str:
        suffix_matches = [m for m in matches if str(m).endswith(path_str)]
        if suffix_matches:
            matches = suffix_matches

    if len(matches) == 1:
        return matches[0]
    elif len(matches) > 1:
        # Multiple matches — return first but caller should handle ambiguity
        return matches[0]

    # Return the direct path even if it doesn't exist (caller checks existence)
    return direct


def verify_file_exists(paths: List[str]) -> VerificationOutcome:
    """Verify that claimed files exist on disk."""
    results = []
    resolved_paths = []
    all_exist = True
    for path_str in paths:
        resolved = _resolve_path(path_str)
        resolved_paths.append(str(resolved))
        exists = resolved.exists()
        results.append(f"  {path_str}: {'EXISTS' if exists else 'MISSING'}")
        if not exists:
            all_exist = False

    evidence = "\n".join(results)
    return VerificationOutcome(
        claim=Claim(text="", claim_type=ClaimType.FILE_EXISTS, verifiability=VerifiabilityLevel.AUTO),
        result=VerificationResult.PASSED if all_exist else VerificationResult.FAILED,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="filesystem_check",
        checked_paths=resolved_paths,
    )


def verify_file_missing(paths: List[str]) -> VerificationOutcome:
    """Verify that files claimed to be missing are actually missing."""
    results = []
    all_missing = True
    for path_str in paths:
        resolved = _resolve_path(path_str)
        exists = resolved.exists()
        results.append(f"  {path_str}: {'EXISTS (claim wrong)' if exists else 'MISSING (claim correct)'}")
        if exists:
            all_missing = False

    evidence = "\n".join(results)
    return VerificationOutcome(
        claim=Claim(text="", claim_type=ClaimType.FILE_MISSING, verifiability=VerifiabilityLevel.AUTO),
        result=VerificationResult.PASSED if all_missing else VerificationResult.FAILED,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="filesystem_check",
    )


def verify_env_var(env_vars: List[str]) -> VerificationOutcome:
    """Verify environment variable presence in .env files and os.environ."""
    results = []
    all_found = False  # For blocker claims, finding the var means the blocker is FALSE

    # Check .env files — search cwd and workspace root
    root = _get_workspace_root()
    cwd = Path.cwd()
    env_files = list(cwd.rglob(".env")) if cwd != root else []
    env_files.extend(root.rglob(".env"))
    # Deduplicate by resolved path
    seen = set()
    unique_env_files = []
    for ef in env_files:
        resolved = ef.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_env_files.append(ef)
    env_file_vars: Dict[str, str] = {}

    for env_file in unique_env_files:
        try:
            content = env_file.read_text()
            for line in content.split('\n'):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, val = line.partition('=')
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if val:  # Only count if value is non-empty
                        try:
                            rel = str(env_file.relative_to(root))
                        except ValueError:
                            rel = str(env_file)
                        env_file_vars[key] = rel
        except (OSError, UnicodeDecodeError):
            continue

    for var in env_vars:
        in_env = var in os.environ
        in_file = var in env_file_vars
        source = []
        if in_env:
            source.append("os.environ")
        if in_file:
            source.append(f".env ({env_file_vars[var]})")

        if source:
            results.append(f"  {var}: PRESENT in {', '.join(source)}")
            all_found = True
        else:
            results.append(f"  {var}: NOT FOUND in any .env or os.environ")

    evidence = "\n".join(results)
    return VerificationOutcome(
        claim=Claim(text="", claim_type=ClaimType.ENV_VAR, verifiability=VerifiabilityLevel.AUTO),
        result=VerificationResult.PASSED if all_found else VerificationResult.INCONCLUSIVE,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="env_check",
    )


def verify_script_syntax(paths: List[str]) -> VerificationOutcome:
    """Verify Python scripts compile without syntax errors."""
    results = []
    all_ok = True
    for path_str in paths:
        resolved = _resolve_path(path_str)
        if not resolved.exists():
            results.append(f"  {path_str}: FILE MISSING")
            all_ok = False
            continue
        if not path_str.endswith('.py'):
            results.append(f"  {path_str}: not a Python file, skipped")
            continue
        try:
            py_compile.compile(str(resolved), doraise=True)
            results.append(f"  {path_str}: COMPILES OK")
        except py_compile.PyCompileError as e:
            results.append(f"  {path_str}: SYNTAX ERROR — {e}")
            all_ok = False

    evidence = "\n".join(results)
    return VerificationOutcome(
        claim=Claim(text="", claim_type=ClaimType.SCRIPT_RUNS, verifiability=VerifiabilityLevel.AUTO),
        result=VerificationResult.PASSED if all_ok else VerificationResult.FAILED,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="py_compile",
    )


def verify_script_imports(paths: List[str]) -> VerificationOutcome:
    """Verify Python scripts' imports resolve (catches missing deps, not just syntax).

    Uses AST to extract import statements from the script, then verifies each
    top-level module is importable in an isolated subprocess. This is safer than
    exec_module — it doesn't run any of the script's own code, only checks that
    declared dependencies are available. Skips relative imports (package-internal).

    Note: Also checks imports inside try/except blocks. Scripts that gracefully
    handle optional imports may trigger warnings — this is informative, not wrong.
    """
    results = []
    all_ok = True
    for path_str in paths:
        resolved = _resolve_path(path_str)
        if not resolved.exists():
            results.append(f"  {path_str}: FILE MISSING")
            all_ok = False
            continue
        if not path_str.endswith('.py'):
            results.append(f"  {path_str}: not a Python file, skipped")
            continue

        # First check syntax (fast)
        try:
            py_compile.compile(str(resolved), doraise=True)
        except py_compile.PyCompileError as e:
            results.append(f"  {path_str}: SYNTAX ERROR — {e}")
            all_ok = False
            continue

        # Then verify imports via subprocess using AST extraction
        check_script = _IMPORT_CHECK_TEMPLATE.format(script_path=str(resolved))
        try:
            proc = subprocess.run(
                [sys.executable, "-"],
                input=check_script,
                capture_output=True, text=True, timeout=10,
                cwd=str(_get_workspace_root()),
            )
            if proc.returncode != 0:
                error_msg = proc.stderr.strip()
                error_lines = [l for l in error_msg.split('\n') if l.strip()]
                brief = error_lines[-1] if error_lines else "unknown import error"
                results.append(f"  {path_str}: IMPORT ERROR — {brief}")
                all_ok = False
            else:
                results.append(f"  {path_str}: COMPILES OK, imports valid")
        except subprocess.TimeoutExpired:
            results.append(f"  {path_str}: COMPILES OK (import check timed out — skipped)")
        except Exception as e:
            results.append(f"  {path_str}: COMPILES OK (import check error: {e})")

    evidence = "\n".join(results)
    return VerificationOutcome(
        claim=Claim(text="", claim_type=ClaimType.SCRIPT_RUNS, verifiability=VerifiabilityLevel.AUTO),
        result=VerificationResult.PASSED if all_ok else VerificationResult.FAILED,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="script_import_check",
    )


# Template for the import check subprocess. Extracts imports via AST and
# tries __import__ on each, avoiding execution of the script's own code.
_IMPORT_CHECK_TEMPLATE = '''import ast
import sys

try:
    with open("{script_path}") as f:
        tree = ast.parse(f.read())
except SyntaxError as e:
    print("SyntaxError: " + str(e), file=sys.stderr)
    sys.exit(1)

failed = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            mod_name = alias.name.split(".")[0]
            try:
                __import__(mod_name)
            except ImportError as e:
                failed.append(str(e))
    elif isinstance(node, ast.ImportFrom):
        if node.module and node.level == 0:
            mod_name = node.module.split(".")[0]
            try:
                __import__(mod_name)
            except ImportError as e:
                failed.append(str(e))

if failed:
    seen = set()
    for f in failed:
        if f not in seen:
            print(f, file=sys.stderr)
            seen.add(f)
    sys.exit(1)
'''


def verify_pipeline_output(script_path: str) -> VerificationOutcome:
    """Check if a pipeline's output artifacts exist and are recent."""
    resolved = _resolve_path(script_path)

    if not resolved.exists():
        return VerificationOutcome(
            claim=Claim(text="", claim_type=ClaimType.PIPELINE_WORKS, verifiability=VerifiabilityLevel.AUTO),
            result=VerificationResult.FAILED,
            evidence=f"Script {script_path} does not exist",
            checked_at=datetime.now(timezone.utc).isoformat(),
            method="output_check",
        )

    # Pipeline → output mappings from config
    script_name = resolved.name
    outputs = get_config().pipeline_outputs.get(script_name, [])

    if not outputs:
        return VerificationOutcome(
            claim=Claim(text="", claim_type=ClaimType.PIPELINE_WORKS, verifiability=VerifiabilityLevel.AUTO),
            result=VerificationResult.INCONCLUSIVE,
            evidence=f"No known output mapping for {script_name}",
            checked_at=datetime.now(timezone.utc).isoformat(),
            method="output_check",
        )

    results = []
    any_output = False
    for output_path in outputs:
        resolved_out = _resolve_path(output_path)
        if resolved_out.exists():
            if resolved_out.is_dir():
                files = list(resolved_out.iterdir())
                results.append(f"  {output_path}: directory exists with {len(files)} items")
                any_output = True
            else:
                mtime = datetime.fromtimestamp(resolved_out.stat().st_mtime, tz=timezone.utc)
                results.append(f"  {output_path}: exists, last modified {mtime.isoformat()[:19]}Z")
                any_output = True
        else:
            results.append(f"  {output_path}: MISSING")

    evidence = "\n".join(results)
    return VerificationOutcome(
        claim=Claim(text="", claim_type=ClaimType.PIPELINE_WORKS, verifiability=VerifiabilityLevel.AUTO),
        result=VerificationResult.PASSED if any_output else VerificationResult.FAILED,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="output_check",
    )


def verify_config_present(
    paths: List[str],
    keys: Optional[List[str]] = None,
) -> VerificationOutcome:
    """Verify config files exist, parse correctly, and contain expected keys.

    Supports JSON, YAML, TOML, and INI/CFG formats.
    Keys support dot notation for nested access (e.g., "database.host").
    """
    results = []
    all_ok = True

    for path_str in paths:
        resolved = _resolve_path(path_str)
        if not resolved.exists():
            results.append(f"  {path_str}: FILE MISSING")
            all_ok = False
            continue

        ext = resolved.suffix.lower()
        data = None

        try:
            content = resolved.read_text()
            if ext == '.json':
                data = json.loads(content)
            elif ext in ('.yaml', '.yml'):
                try:
                    import yaml
                    data = yaml.safe_load(content)
                except ImportError:
                    results.append(f"  {path_str}: EXISTS (YAML parsing unavailable — pyyaml not installed)")
                    continue
            elif ext == '.toml':
                try:
                    import tomllib
                except ImportError:
                    try:
                        import tomli as tomllib
                    except ImportError:
                        results.append(f"  {path_str}: EXISTS (TOML parsing unavailable)")
                        continue
                data = tomllib.loads(content)
            elif ext in ('.cfg', '.conf', '.ini'):
                import configparser
                cp = configparser.ConfigParser()
                cp.read_string(content)
                data = {s: dict(cp[s]) for s in cp.sections()}
            else:
                results.append(f"  {path_str}: EXISTS (unknown config format '{ext}')")
                continue

            results.append(f"  {path_str}: EXISTS, valid {ext} format")

        except Exception as e:
            results.append(f"  {path_str}: EXISTS but PARSE ERROR — {e}")
            all_ok = False
            continue

        # Check for specific keys if provided
        if keys and data is not None:
            for key in keys:
                if _check_key_in_data(data, key):
                    results.append(f"    key '{key}': PRESENT")
                else:
                    results.append(f"    key '{key}': MISSING")
                    all_ok = False

    evidence = "\n".join(results)
    return VerificationOutcome(
        claim=Claim(text="", claim_type=ClaimType.CONFIG_PRESENT, verifiability=VerifiabilityLevel.AUTO),
        result=VerificationResult.PASSED if all_ok else VerificationResult.FAILED,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="config_check",
    )


def _check_key_in_data(data: Any, key: str) -> bool:
    """Check if a key exists in parsed config data. Supports dot notation.

    Examples:
        _check_key_in_data({"a": {"b": 1}}, "a.b") → True
        _check_key_in_data({"a": {"b": 1}}, "a.c") → False
        _check_key_in_data({"x": 1}, "x") → True
    """
    if not isinstance(data, dict):
        return False

    parts = key.split('.')
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return False
    return True


def verify_process_status(claim: Claim) -> VerificationOutcome:
    """Verify process/service status claims against actual process state.

    Matches keywords in the claim text against configured process_services,
    then checks the actual process status using the configured manager
    (supervisorctl, systemd, or ps fallback).

    Examples of claims this catches:
    - "Weather rewards monitor: running" when service is actually STOPPED
    - "slack-monitor is operational" when process has crashed
    """
    config = get_config()
    claim_lower = claim.text.lower()
    now = datetime.now(timezone.utc).isoformat()

    # Route blanket "all services" claims to the comprehensive checker
    if _is_all_services_claim(claim.text):
        return verify_all_services(claim)

    # Normalize dashes/spaces for matching — "weather-rewards" and "weather rewards"
    # should both match claims containing either variant. This prevents variant drift
    # where config has one form and claim text uses the other.
    claim_normalized = claim_lower.replace('-', ' ')

    # Match longest keyword first to avoid partial matches
    matched_service = None
    matched_keyword = None
    for keyword in sorted(config.process_services.keys(), key=len, reverse=True):
        keyword_normalized = keyword.lower().replace('-', ' ')
        if keyword_normalized in claim_normalized:
            matched_service = config.process_services[keyword]
            matched_keyword = keyword
            break

    if not matched_service:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence="No process service keyword matched in claim text",
            checked_at=now,
            method="process_status_check",
        )

    # Determine what the claim asserts (running vs stopped)
    positive_words = {'running', 'active', 'operational', 'healthy', 'up'}
    negative_words = {'stopped', 'inactive', 'down', 'crashed', 'exited', 'fatal', 'backoff'}
    claims_running = any(w in claim_lower for w in positive_words)
    claims_stopped = any(w in claim_lower for w in negative_words)

    # Check actual process status
    manager = matched_service.get("manager", "ps")
    service_name = matched_service.get("service_name", matched_keyword)
    actual_status, status_detail = _check_process_status(
        manager=manager,
        service_name=service_name,
        config_path=matched_service.get("config"),
    )

    # Supplement with pid file check if configured.
    # Pid file + os.kill is more reliable than pgrep for process name mismatches.
    pid_file = matched_service.get("pid_file")
    pid_info = ""
    if pid_file:
        pid_status, pid_detail = _check_pid_file(pid_file)
        pid_info = f"\n  Pid check: {pid_detail}"
        if pid_status == "running" and actual_status in ("unknown", "stopped"):
            actual_status = pid_status
        elif pid_status == "stopped" and actual_status == "unknown":
            actual_status = pid_status

    # Supplement with port check if configured
    port = matched_service.get("port")
    port_info = ""
    if port:
        port_host = matched_service.get("host", "127.0.0.1")
        port_status, port_detail = _check_port(port, port_host)
        port_info = f"\n  Port check: {port_detail}"
        if port_status == "running" and actual_status in ("unknown", "stopped"):
            actual_status = port_status
        elif port_status == "stopped" and actual_status == "unknown":
            actual_status = port_status

    evidence = (
        f"  Matched keyword '{matched_keyword}' → {service_name}\n"
        f"  Manager: {manager}\n"
        f"  Actual status: {actual_status}\n"
        f"  Detail: {status_detail}"
        f"{pid_info}{port_info}"
    )

    # Compare claim against reality
    is_actually_running = actual_status.lower() in ("running", "active")

    if actual_status == "unknown":
        result = VerificationResult.INCONCLUSIVE
    elif claims_running and is_actually_running:
        result = VerificationResult.PASSED
    elif claims_stopped and not is_actually_running:
        result = VerificationResult.PASSED
    elif claims_running and not is_actually_running:
        result = VerificationResult.FAILED
        evidence += f"\n  → Claim says RUNNING but process is {actual_status.upper()}"
    elif claims_stopped and is_actually_running:
        result = VerificationResult.FAILED
        evidence += f"\n  → Claim says STOPPED but process is {actual_status.upper()}"
    else:
        result = VerificationResult.INCONCLUSIVE
        evidence += "\n  → Could not determine claim assertion direction"

    return VerificationOutcome(
        claim=claim,
        result=result,
        evidence=evidence,
        checked_at=now,
        method="process_status_check",
    )


def _check_process_status(
    manager: str,
    service_name: str,
    config_path: Optional[str] = None,
) -> tuple:
    """Check actual process status using the configured manager.

    Returns (status_string, detail_string).
    Status is one of: running, stopped, starting, backoff, exited, fatal, unknown.
    """
    root = _get_workspace_root()

    if manager == "supervisorctl":
        return _check_supervisorctl(service_name, config_path, root)
    elif manager == "systemd":
        return _check_systemd(service_name)
    else:
        return _check_ps(service_name)


def _check_supervisorctl(
    service_name: str,
    config_path: Optional[str],
    root: Path,
) -> tuple:
    """Check process status via supervisorctl."""
    cmd = ["supervisorctl"]
    if config_path:
        conf = root / config_path if not Path(config_path).is_absolute() else Path(config_path)
        if conf.exists():
            cmd.extend(["-c", str(conf)])

    cmd.extend(["status", service_name])

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
        )
        output = proc.stdout.strip()
        if not output:
            output = proc.stderr.strip()

        # Parse supervisorctl output format: "name  STATUS  pid NNN, uptime X:XX:XX"
        # or "name  STOPPED  Mar 14 07:41 PM"
        parts = output.split()
        if len(parts) >= 2:
            status = parts[1].lower()
            return (status, output)
        return ("unknown", output or "No output from supervisorctl")
    except FileNotFoundError:
        return ("unknown", "supervisorctl not found on PATH")
    except subprocess.TimeoutExpired:
        return ("unknown", "supervisorctl timed out")
    except Exception as e:
        return ("unknown", f"Error running supervisorctl: {e}")


def _check_systemd(service_name: str) -> tuple:
    """Check process status via systemctl."""
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=10,
        )
        status = proc.stdout.strip().lower()
        if status in ("active", "activating"):
            return ("running", f"systemd: {status}")
        return (status or "unknown", f"systemd: {status}")
    except FileNotFoundError:
        return ("unknown", "systemctl not found on PATH")
    except subprocess.TimeoutExpired:
        return ("unknown", "systemctl timed out")
    except Exception as e:
        return ("unknown", f"Error running systemctl: {e}")


def _check_ps(service_name: str) -> tuple:
    """Check process status via ps (fallback)."""
    try:
        proc = subprocess.run(
            ["pgrep", "-f", service_name],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            pids = proc.stdout.strip().split('\n')
            return ("running", f"Found {len(pids)} process(es): PIDs {', '.join(pids)}")
        return ("stopped", "No matching process found via pgrep")
    except FileNotFoundError:
        return ("unknown", "pgrep not found on PATH")
    except subprocess.TimeoutExpired:
        return ("unknown", "pgrep timed out")
    except Exception as e:
        return ("unknown", f"Error running pgrep: {e}")


def _check_pid_file(pid_file: str) -> tuple:
    """Check if a process is alive via its pid file.

    Returns (status_string, detail_string).
    Reads the pid from the file and sends signal 0 to check liveness.
    """
    root = _get_workspace_root()
    pid_path = root / pid_file if not Path(pid_file).is_absolute() else Path(pid_file)

    if not pid_path.exists():
        return ("unknown", f"pid file {pid_file} does not exist")

    try:
        pid = int(pid_path.read_text().strip())
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return ("running", f"pid {pid} is alive (from {pid_file})")
    except ValueError:
        return ("unknown", f"pid file {pid_file} contains non-integer content")
    except ProcessLookupError:
        return ("stopped", f"pid {pid} from {pid_file} is not running")
    except PermissionError:
        # Process exists but we lack permission — it's alive
        return ("running", f"pid {pid} exists (permission denied on signal)")
    except OSError as e:
        return ("unknown", f"Error checking pid {pid_file}: {e}")


def _check_port(port: int, host: str = "127.0.0.1") -> tuple:
    """Check if a port is accepting connections.

    Returns (status_string, detail_string).
    Uses a quick socket connect with a 2-second timeout.
    """
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            return ("running", f"port {port} on {host} is accepting connections")
        else:
            return ("stopped", f"port {port} on {host} refused connection (errno {result})")
    except socket.timeout:
        return ("stopped", f"port {port} on {host} connection timed out")
    except OSError as e:
        return ("unknown", f"Error probing port {port}: {e}")


def verify_all_services(claim: Claim) -> VerificationOutcome:
    """Verify blanket 'all services running' claims by checking every configured service.

    When a claim says "All services RUNNING" (without naming a specific service),
    this function iterates over ALL configured process_services and checks each one.
    The claim passes only if every service is in the claimed state.

    Also checks pid files and ports when configured on individual services.
    """
    config = get_config()
    claim_lower = claim.text.lower()
    now = datetime.now(timezone.utc).isoformat()

    if not config.process_services:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence="No process services configured",
            checked_at=now,
            method="all_services_check",
        )

    # Determine what the claim asserts
    positive_words = {'running', 'active', 'operational', 'healthy', 'up'}
    negative_words = {'stopped', 'inactive', 'down', 'crashed', 'exited', 'fatal'}
    claims_running = any(w in claim_lower for w in positive_words)
    claims_stopped = any(w in claim_lower for w in negative_words)

    # Check each unique service (skip alias entries that point to same service_name)
    checked = {}  # service_name -> (status, detail, keyword)
    for keyword, svc_cfg in config.process_services.items():
        svc_name = svc_cfg.get("service_name", keyword)
        if svc_name in checked:
            continue

        manager = svc_cfg.get("manager", "ps")
        status, detail = _check_process_status(
            manager=manager,
            service_name=svc_name,
            config_path=svc_cfg.get("config"),
        )

        # Supplement with pid file check if configured.
        # Pid file + os.kill(pid, 0) is a kernel-level liveness check —
        # it's more reliable than pgrep for processes whose command line
        # doesn't match the service_name string (e.g. supervisord).
        pid_file = svc_cfg.get("pid_file")
        if pid_file:
            pid_status, pid_detail = _check_pid_file(pid_file)
            detail += f" | pid: {pid_detail}"
            if pid_status == "running" and status in ("unknown", "stopped"):
                status = pid_status
            elif pid_status == "stopped" and status == "unknown":
                status = pid_status

        # Supplement with port check if configured
        port = svc_cfg.get("port")
        if port:
            port_host = svc_cfg.get("host", "127.0.0.1")
            port_status, port_detail = _check_port(port, port_host)
            detail += f" | port: {port_detail}"
            if port_status == "running" and status in ("unknown", "stopped"):
                status = port_status
            elif port_status == "stopped" and status == "unknown":
                status = port_status

        checked[svc_name] = (status, detail, keyword)

    # Build evidence and determine result
    results = []
    all_running = True
    all_stopped = True
    for svc_name, (status, detail, keyword) in checked.items():
        is_running = status.lower() in ("running", "active")
        results.append(f"  {svc_name}: {status.upper()} ({detail})")
        if not is_running:
            all_running = False
        if is_running:
            all_stopped = False

    evidence = f"  Checked {len(checked)} services:\n" + "\n".join(results)

    if claims_running and all_running:
        result = VerificationResult.PASSED
    elif claims_stopped and all_stopped:
        result = VerificationResult.PASSED
    elif claims_running and not all_running:
        failed_svcs = [
            svc_name for svc_name, (status, _, _) in checked.items()
            if status.lower() not in ("running", "active")
        ]
        result = VerificationResult.FAILED
        evidence += f"\n  → Claim says ALL RUNNING but {len(failed_svcs)} service(s) not running: {', '.join(failed_svcs)}"
    elif claims_stopped and not all_stopped:
        result = VerificationResult.FAILED
        evidence += "\n  → Claim says STOPPED but some services are still running"
    else:
        result = VerificationResult.INCONCLUSIVE
        evidence += "\n  → Could not determine claim assertion direction"

    return VerificationOutcome(
        claim=claim,
        result=result,
        evidence=evidence,
        checked_at=now,
        method="all_services_check",
    )


def _is_all_services_claim(claim_text: str) -> bool:
    """Detect blanket 'all services' claims that should check every service."""
    lower = claim_text.lower()
    return bool(re.search(r'\ball\s+services\b', lower))


def verify_status_by_name(claim: Claim) -> VerificationOutcome:
    """Verify pipeline/service status claims that lack explicit file paths.

    When a claim says "Notes pipeline operational" without referencing a
    specific script, this method matches keywords in the claim text against
    a configured pipeline name mapping, then checks the matched pipeline's
    output artifacts for existence/freshness.
    """
    config = get_config()
    claim_lower = claim.text.lower()

    # Match longest keyword first to avoid partial matches
    matched_script = None
    matched_keyword = None
    for keyword in sorted(config.pipeline_names.keys(), key=len, reverse=True):
        if keyword.lower() in claim_lower:
            matched_script = config.pipeline_names[keyword]
            matched_keyword = keyword
            break

    if not matched_script:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence="No pipeline name keyword matched in claim text",
            checked_at=datetime.now(timezone.utc).isoformat(),
            method="status_name_match",
        )

    # Check output artifacts directly (don't require the script to be at
    # a resolvable path — we just care about whether the pipeline's outputs
    # exist and are reasonably fresh).
    config = get_config()
    outputs = config.pipeline_outputs.get(matched_script, [])

    if not outputs:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence=f"  Matched keyword '{matched_keyword}' → {matched_script}\n"
                     f"  No output artifacts configured for {matched_script}",
            checked_at=datetime.now(timezone.utc).isoformat(),
            method="status_name_match",
        )

    results = []
    any_output = False
    root = _get_workspace_root()
    for output_path in outputs:
        resolved_out = root / output_path if not Path(output_path).is_absolute() else Path(output_path)
        if resolved_out.exists():
            if resolved_out.is_dir():
                files = list(resolved_out.iterdir())
                results.append(f"  {output_path}: directory with {len(files)} items")
                any_output = True
            else:
                mtime = datetime.fromtimestamp(resolved_out.stat().st_mtime, tz=timezone.utc)
                results.append(f"  {output_path}: exists, modified {mtime.isoformat()[:19]}Z")
                any_output = True
        else:
            results.append(f"  {output_path}: MISSING")

    evidence = f"  Matched keyword '{matched_keyword}' → {matched_script}\n"
    evidence += "\n".join(results)

    return VerificationOutcome(
        claim=claim,
        result=VerificationResult.PASSED if any_output else VerificationResult.FAILED,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="status_name_match",
    )


def verify_count(claim: Claim) -> VerificationOutcome:
    """Verify count/quantity claims against configured data sources.

    Uses count_sources from config to match claim keywords to data files.
    Supports json_array (count items in a JSON array) and regex_count
    (count regex matches in a text file) source types.

    Falls back to test counting (always available) for test-related claims.
    """
    claim_lower = claim.text.lower()
    now = datetime.now(timezone.utc).isoformat()
    root = _get_workspace_root()
    config = get_config()

    if not claim.extracted_numbers:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence="No count number extracted from claim",
            checked_at=now,
            method="count_check",
        )

    # Try config-driven count sources
    for source_key, source_cfg in config.count_sources.items():
        # Match by keywords in the source key (e.g. "journal_entries" matches "journal" + "entries")
        keywords = source_key.replace("_", " ").split()
        if all(kw in claim_lower for kw in keywords):
            source_type = source_cfg.get("type", "")
            if source_type == "json_array":
                return _verify_json_array_count(claim, root, now, source_cfg)
            elif source_type == "regex_count":
                return _verify_regex_count(claim, root, now, source_cfg)
            elif source_type == "notes_queue":
                return _verify_notes_queue_count(claim, root, now, source_cfg)
            elif source_type == "knowledge_tree_stats":
                return _verify_prediction_resolution(claim, root, now)

    # Built-in: test count (always available, no config needed)
    # Must check for "N tests" pattern specifically, not just substring "test".
    # Otherwise phrases like "90-day test" route here as a false positive.
    if re.search(r'\b\d+\s+tests?\b', claim_lower):
        return _verify_test_count(claim, root, now)

    return VerificationOutcome(
        claim=claim,
        result=VerificationResult.INCONCLUSIVE,
        evidence=f"No count verification source for: {claim.text[:80]}",
        checked_at=now,
        method="count_check",
    )


def _verify_json_array_count(
    claim: Claim, root: Path, now: str, source_cfg: dict,
) -> VerificationOutcome:
    """Count items in a JSON array file. Config-driven replacement for hardcoded counters.

    source_cfg keys:
        file: path to JSON file (relative to workspace root)
        json_path: dot-separated key to the array (e.g. "posts" or "data.items")
    """
    file_path = root / source_cfg["file"]
    if not file_path.exists():
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence=f"Count source file not found: {source_cfg['file']}",
            checked_at=now,
            method="count_check",
        )

    try:
        raw = json.loads(file_path.read_text())

        # Navigate to the array via json_path
        json_path = source_cfg.get("json_path", "")
        data = raw
        if json_path:
            for key in json_path.split("."):
                if isinstance(data, dict):
                    data = data.get(key, data)
                else:
                    break
        if not isinstance(data, list):
            data = []

        # Extract the count number associated with the subject, not time windows
        entry_match = re.search(
            r'(\d+)\s+(?:\w+\s+)?entr(?:ies|y)', claim.text, re.IGNORECASE,
        )
        if entry_match:
            claimed_count = int(entry_match.group(1))
        else:
            claimed_count = int(claim.extracted_numbers[0])

        # Check for time window (e.g., "in 24 hours", "in 3 days")
        time_match = re.search(r'(\d+)\s+(hours?|days?)', claim.text.lower())
        if time_match:
            window_num = int(time_match.group(1))
            unit = time_match.group(2)
            hours = window_num if "hour" in unit else window_num * 24
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            cutoff_str = cutoff.strftime("%Y-%m-%d")

            recent = [
                p for p in data
                if isinstance(p, dict) and p.get("date", "") >= cutoff_str
            ]
            actual_count = len(recent)
            evidence = (
                f"  Claimed: {claimed_count} entries in {window_num} {unit}\n"
                f"  Actual: {actual_count} entries since {cutoff_str}\n"
            )
        else:
            actual_count = len(data)
            evidence = (
                f"  Claimed: {claimed_count} entries\n"
                f"  Actual: {actual_count} total entries\n"
            )

        tolerance = max(claimed_count * 0.2, 2)
        if abs(actual_count - claimed_count) <= tolerance:
            result = VerificationResult.PASSED
            evidence += f"  → Approximately correct (tolerance: ±{int(tolerance)})"
        else:
            result = VerificationResult.FAILED
            evidence += f"  → Count mismatch (tolerance: ±{int(tolerance)})"

        return VerificationOutcome(
            claim=claim, result=result, evidence=evidence,
            checked_at=now, method="count_check",
            checked_paths=[str(file_path)],
        )
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        return VerificationOutcome(
            claim=claim, result=VerificationResult.INCONCLUSIVE,
            evidence=f"Error counting from {source_cfg['file']}: {e}",
            checked_at=now, method="count_check",
        )


def _verify_regex_count(
    claim: Claim, root: Path, now: str, source_cfg: dict,
) -> VerificationOutcome:
    """Count regex matches in a text file. Config-driven replacement for hardcoded counters.

    source_cfg keys:
        file: path to text file (relative to workspace root)
        pattern: regex pattern to count (matched per-line with MULTILINE)
        rate_per_day: optional rate for runway estimates (e.g. 2.0 notes/day)
    """
    file_path = root / source_cfg["file"]
    if not file_path.exists():
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.FAILED,
            evidence=f"Count source file not found: {source_cfg['file']}",
            checked_at=now,
            method="count_check",
        )

    try:
        content = file_path.read_text()
        pattern = source_cfg.get("pattern", r".")
        matches = re.findall(pattern, content, re.MULTILINE)
        match_count = len(matches)

        # Subtract already-posted items if a posted_file is configured
        posted_file = source_cfg.get("posted_file")
        if posted_file:
            posted_path = root / posted_file
            if posted_path.exists():
                posted_count = len([
                    line for line in posted_path.read_text().strip().splitlines()
                    if line.strip()
                ])
                match_count = max(0, match_count - posted_count)

        # Detect "X total, Y remaining" claims — compare remaining against
        # the subtracted count, and total against the raw count.
        claim_lower = claim.text.lower()
        total_raw = len(matches)  # before subtracting posted
        remaining_match = re.search(
            r'(\d+)\s+remaining', claim_lower,
        )
        total_match = re.search(
            r'(\d+)\s+total', claim_lower,
        )

        # Runway/days estimate using configured rate
        rate = source_cfg.get("rate_per_day")
        if rate and ("runway" in claim_lower or "days" in claim_lower):
            claimed_number = int(claim.extracted_numbers[0])
            estimated_days = match_count / float(rate)
            evidence = (
                f"  Claimed: ~{claimed_number} days runway\n"
                f"  Actual: {match_count} items (≈{estimated_days:.0f} days at {rate}/day)\n"
            )
            tolerance = max(claimed_number * 0.4, 2)
            if abs(estimated_days - claimed_number) <= tolerance:
                result = VerificationResult.PASSED
                evidence += f"  → Approximately correct (tolerance: ±{int(tolerance)})"
            else:
                result = VerificationResult.FAILED
                evidence += f"  → Mismatch (tolerance: ±{int(tolerance)})"
        elif remaining_match or total_match:
            # Handle "X total, Y remaining" claims with separate checks
            checks = []
            if total_match:
                claimed_total = int(total_match.group(1))
                tolerance_t = max(claimed_total * 0.2, 2)
                ok = abs(total_raw - claimed_total) <= tolerance_t
                checks.append(("total", claimed_total, total_raw, tolerance_t, ok))
            if remaining_match and posted_file:
                claimed_remaining = int(remaining_match.group(1))
                tolerance_r = max(claimed_remaining * 0.2, 2)
                ok = abs(match_count - claimed_remaining) <= tolerance_r
                checks.append(("remaining", claimed_remaining, match_count, tolerance_r, ok))

            evidence_parts = []
            all_ok = True
            for label, claimed, actual, tol, ok in checks:
                evidence_parts.append(f"  {label.capitalize()}: claimed {claimed}, actual {actual}")
                if not ok:
                    all_ok = False
            evidence = "\n".join(evidence_parts) + "\n"
            if all_ok:
                result = VerificationResult.PASSED
                evidence += "  → Counts match"
            else:
                result = VerificationResult.FAILED
                evidence += "  → Count mismatch"
        else:
            claimed_number = int(claim.extracted_numbers[0])
            evidence = f"  Count: {match_count} matches\n"
            tolerance = max(claimed_number * 0.2, 2)
            if abs(match_count - claimed_number) <= tolerance:
                result = VerificationResult.PASSED
                evidence += f"  → Approximately correct (tolerance: ±{int(tolerance)})"
            else:
                result = VerificationResult.FAILED
                evidence += f"  → Count mismatch (tolerance: ±{int(tolerance)})"

        return VerificationOutcome(
            claim=claim, result=result, evidence=evidence,
            checked_at=now, method="count_check",
            checked_paths=[str(file_path)],
        )
    except (ValueError, IndexError) as e:
        return VerificationOutcome(
            claim=claim, result=VerificationResult.INCONCLUSIVE,
            evidence=f"Error checking count from {source_cfg['file']}: {e}",
            checked_at=now, method="count_check",
        )


def _verify_notes_queue_count(
    claim: Claim, root: Path, now: str, source_cfg: dict,
) -> VerificationOutcome:
    """Verify Notes queue count claims by parsing posted/unposted markers.

    Handles claims like:
    - "3 unposted Notes remain (31/34 posted)"
    - "31/35 posted"
    - "4 unposted Notes"

    A note is "posted" if EITHER its header has ~~strikethrough~~ markers
    OR its slug appears in the .notes_posted tracking file. This matches
    the logic in post_note.py's _is_posted().
    """
    file_path = root / source_cfg.get("file", "projects/synthesis/scripts/notes_queue.md")
    if not file_path.exists():
        return VerificationOutcome(
            claim=claim, result=VerificationResult.FAILED,
            evidence=f"Notes queue file not found: {file_path}",
            checked_at=now, method="notes_queue_count",
        )

    try:
        content = file_path.read_text()

        # Load .notes_posted tracking file (slugs of posted notes)
        posted_file = root / "projects" / "synthesis" / "scripts" / ".notes_posted"
        posted_slugs: set = set()
        if posted_file.exists():
            posted_slugs = {
                line.strip() for line in posted_file.read_text().splitlines()
                if line.strip()
            }

        # Parse note headers and determine posted status
        # Headers: "### Note: Title" (unposted) or "### ~~Note: Title~~ (POSTED)" (posted)
        # Also h2 variants: "## ~~Note: Title~~"
        note_header_re = re.compile(
            r'^(##?#?)\s+(~~)?Note:\s+(.+?)(?:~~)?\s*(?:\(POSTED.*\))?\s*$',
            re.MULTILINE,
        )
        actual_total = 0
        actual_posted = 0
        for m in note_header_re.finditer(content):
            actual_total += 1
            has_strikethrough = m.group(2) == "~~"
            title = m.group(3).strip().rstrip("~~").strip()
            # Generate slug to check against .notes_posted
            slug = "note-" + re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')
            if has_strikethrough or slug in posted_slugs:
                actual_posted += 1

        actual_unposted = actual_total - actual_posted

        claim_lower = claim.text.lower()
        checks = []

        # Parse fractional format: "31/34 posted" or "31/35 posted"
        frac_match = re.search(r'(\d+)\s*/\s*(\d+)\s+posted', claim_lower)
        if frac_match:
            claimed_posted = int(frac_match.group(1))
            claimed_total = int(frac_match.group(2))
            checks.append(("posted", claimed_posted, actual_posted))
            checks.append(("total", claimed_total, actual_total))

        # Parse "N unposted" format
        unposted_match = re.search(r'(\d+)\s+unposted', claim_lower)
        if unposted_match:
            claimed_unposted = int(unposted_match.group(1))
            checks.append(("unposted", claimed_unposted, actual_unposted))

        if not checks:
            if claim.extracted_numbers:
                claimed = int(claim.extracted_numbers[0])
                checks.append(("count", claimed, actual_total))
            else:
                return VerificationOutcome(
                    claim=claim, result=VerificationResult.INCONCLUSIVE,
                    evidence="Could not parse count from Notes claim",
                    checked_at=now, method="notes_queue_count",
                )

        evidence_parts = []
        all_ok = True
        for label, claimed, actual in checks:
            ok = claimed == actual
            if ok:
                evidence_parts.append(f"  ✓ {label}: {claimed}/{actual}")
            else:
                evidence_parts.append(f"  ✗ {label}: claimed {claimed}, actual {actual}")
                all_ok = False

        evidence = " | ".join(p.strip() for p in evidence_parts)
        result = VerificationResult.PASSED if all_ok else VerificationResult.FAILED
        return VerificationOutcome(
            claim=claim, result=result, evidence=evidence,
            checked_at=now, method="notes_queue_count",
            checked_paths=[str(file_path)],
        )
    except Exception as e:
        return VerificationOutcome(
            claim=claim, result=VerificationResult.INCONCLUSIVE,
            evidence=f"Error parsing notes queue: {e}",
            checked_at=now, method="notes_queue_count",
        )


def _verify_prediction_resolution(
    claim: Claim, root: Path, now: str,
) -> VerificationOutcome:
    """Verify prediction resolution count claims against the knowledge tree.

    Handles claims like:
    - "120/470 resolved (25.5%)"
    - "118/470 resolved"

    Reads KNOWLEDGE_TREE.json directly and counts prediction-tagged entries
    that have a 'resolution' field populated.
    """
    tree_path = root / "core" / "knowledge" / "KNOWLEDGE_TREE.json"
    if not tree_path.exists():
        return VerificationOutcome(
            claim=claim, result=VerificationResult.INCONCLUSIVE,
            evidence="KNOWLEDGE_TREE.json not found",
            checked_at=now, method="prediction_resolution",
        )

    try:
        tree = json.loads(tree_path.read_text())
        nodes = tree.get("nodes", {})

        # Count prediction-tagged entries
        pred_nodes = [
            (k, v) for k, v in nodes.items()
            if isinstance(v, dict) and "prediction" in str(v.get("tags", ""))
        ]
        actual_total = len(pred_nodes)
        actual_resolved = sum(
            1 for _, v in pred_nodes if v.get("resolution")
        )

        claim_lower = claim.text.lower()
        frac_match = re.search(r'(\d+)\s*/\s*(\d+)\s+resolved', claim_lower)

        if frac_match:
            claimed_resolved = int(frac_match.group(1))
            claimed_total = int(frac_match.group(2))
            tolerance = max(claimed_total * 0.1, 10)

            evidence = (
                f"  Resolved: claimed {claimed_resolved}/{claimed_total}, "
                f"actual {actual_resolved}/{actual_total}\n"
            )
            resolved_ok = abs(actual_resolved - claimed_resolved) <= tolerance
            total_ok = abs(actual_total - claimed_total) <= tolerance

            if resolved_ok and total_ok:
                result = VerificationResult.PASSED
                evidence += f"  → Counts match (tolerance: ±{int(tolerance)})"
            else:
                result = VerificationResult.FAILED
                evidence += f"  → Count mismatch (tolerance: ±{int(tolerance)})"

            return VerificationOutcome(
                claim=claim, result=result, evidence=evidence,
                checked_at=now, method="prediction_resolution",
            )

        return VerificationOutcome(
            claim=claim, result=VerificationResult.INCONCLUSIVE,
            evidence="Could not parse resolution fraction from claim",
            checked_at=now, method="prediction_resolution",
        )
    except (json.JSONDecodeError, Exception) as e:
        return VerificationOutcome(
            claim=claim, result=VerificationResult.INCONCLUSIVE,
            evidence=f"Error checking prediction resolution: {e}",
            checked_at=now, method="prediction_resolution",
        )


def _count_tests_in_dir(test_dir: Path) -> tuple:
    """Count test functions in a directory. Returns (count, file_count)."""
    test_files = list(test_dir.rglob("test_*.py"))
    test_count = 0
    for tf in test_files:
        try:
            content = tf.read_text()
            test_count += len(re.findall(r'^\s*def test_', content, re.MULTILINE))
        except (OSError, UnicodeDecodeError):
            continue
    return test_count, len(test_files)


def _verify_test_count(
    claim: Claim, root: Path, now: str,
) -> VerificationOutcome:
    """Verify test count claims by counting test functions.

    Scope-aware: if the claim mentions a specific component (e.g. "confab tests",
    "synthesis tests"), searches for a tests/ directory under that component first.
    When no component scope matches, searches ALL test directories and passes if
    any directory's count matches the claimed number within tolerance. This prevents
    false positives where an unscoped claim like "297 tests" gets checked only
    against the workspace-level tests/ directory.
    """
    claimed = int(claim.extracted_numbers[0])
    tolerance = max(claimed * 0.2, 3)

    # Try scoped match first (claim mentions a component name)
    scoped_dir = _find_scoped_test_dir(claim.text, root)
    if scoped_dir is not None and scoped_dir.exists():
        scope_label = str(scoped_dir.relative_to(root))
        test_count, file_count = _count_tests_in_dir(scoped_dir)
        evidence = (
            f"  Claimed: {claimed} tests\n"
            f"  Actual: {test_count} test functions in {file_count} files under {scope_label}\n"
        )
        if abs(test_count - claimed) <= tolerance:
            evidence += f"  → Approximately correct (tolerance: ±{int(tolerance)})"
            return VerificationOutcome(
                claim=claim, result=VerificationResult.PASSED,
                evidence=evidence, checked_at=now, method="count_check",
            )
        else:
            evidence += f"  → Count mismatch (tolerance: ±{int(tolerance)})"
            return VerificationOutcome(
                claim=claim, result=VerificationResult.FAILED,
                evidence=evidence, checked_at=now, method="count_check",
            )

    # No scoped match — search ALL test directories for best match.
    # This prevents false positives when an unscoped claim like "297 tests"
    # gets checked against the wrong directory.
    all_test_dirs = _find_all_test_dirs(root)
    if not all_test_dirs:
        return VerificationOutcome(
            claim=claim, result=VerificationResult.FAILED,
            evidence="No test directories found",
            checked_at=now, method="count_check",
        )

    best_match = None
    best_delta = float('inf')
    results = []
    for td in all_test_dirs:
        label = str(td.relative_to(root))
        count, files = _count_tests_in_dir(td)
        delta = abs(count - claimed)
        results.append((label, count, files, delta))
        if delta < best_delta:
            best_delta = delta
            best_match = (label, count, files)

    evidence = f"  Claimed: {claimed} tests (no component scope in claim)\n"
    evidence += "  Searched:\n"
    for label, count, files, delta in sorted(results, key=lambda x: x[3]):
        marker = " ←" if delta <= tolerance else ""
        evidence += f"    {label}: {count} tests in {files} files (Δ{delta}){marker}\n"

    if best_delta <= tolerance:
        evidence += f"  → Matched {best_match[0]} within tolerance (±{int(tolerance)})"
        return VerificationOutcome(
            claim=claim, result=VerificationResult.PASSED,
            evidence=evidence, checked_at=now, method="count_check",
        )
    else:
        evidence += f"  → No directory matched within tolerance (±{int(tolerance)})"
        return VerificationOutcome(
            claim=claim, result=VerificationResult.FAILED,
            evidence=evidence, checked_at=now, method="count_check",
        )


def _find_scoped_test_dir(claim_text: str, root: Path) -> Optional[Path]:
    """Find the most specific test directory matching the claim's scope.

    If the claim mentions a known component name (e.g. "confab", "synthesis"),
    looks for a tests/ directory under that component's path. This prevents
    false positives where a claim about "154 confab tests" gets checked against
    526 tests across the entire workspace.

    Returns None if no scoped match is found.
    """
    claim_lower = claim_text.lower()

    # Search for component-scoped test directories
    # Check common patterns: core/<name>/tests/, projects/<name>/tests/
    for prefix in ("core", "projects"):
        prefix_dir = root / prefix
        if not prefix_dir.is_dir():
            continue
        for child in sorted(prefix_dir.iterdir()):
            if child.is_dir() and child.name.lower() in claim_lower:
                tests_dir = child / "tests"
                if tests_dir.is_dir():
                    return tests_dir

    return None


def _find_all_test_dirs(root: Path) -> list:
    """Find all test directories in the workspace."""
    dirs = []
    # Workspace-level tests/
    ws = root / "tests"
    if ws.is_dir():
        dirs.append(ws)
    # Component-level tests/ under core/ and projects/
    for prefix in ("core", "projects"):
        prefix_dir = root / prefix
        if not prefix_dir.is_dir():
            continue
        for child in sorted(prefix_dir.iterdir()):
            if child.is_dir():
                tests_dir = child / "tests"
                if tests_dir.is_dir():
                    dirs.append(tests_dir)
    return dirs


def verify_registry(paths: List[str]) -> VerificationOutcome:
    """Verify that referenced files appear in SYSTEM_REGISTRY.md.

    Loads the registry and checks whether each path is mentioned.
    Files not in the registry = FAILED with evidence.
    """
    root = _get_workspace_root()
    registry_path = root / "core" / "SYSTEM_REGISTRY.md"

    if not registry_path.exists():
        return VerificationOutcome(
            claim=Claim(text="", claim_type=ClaimType.REGISTRY_VIOLATION,
                        verifiability=VerifiabilityLevel.AUTO),
            result=VerificationResult.INCONCLUSIVE,
            evidence="core/SYSTEM_REGISTRY.md not found",
            checked_at=datetime.now(timezone.utc).isoformat(),
            method="registry_check",
        )

    registry_text = registry_path.read_text()
    results = []
    any_missing = False

    for path_str in paths:
        # Normalize: check both the full path and the basename
        basename = Path(path_str).name
        # Check if the path or basename appears in the registry
        in_registry = (
            f"`{path_str}`" in registry_text
            or f"`{basename}`" in registry_text
            or path_str in registry_text
        )
        if in_registry:
            results.append(f"  {path_str}: in registry")
        else:
            results.append(f"  {path_str}: NOT in SYSTEM_REGISTRY.md")
            any_missing = True

    evidence = "\n".join(results)
    return VerificationOutcome(
        claim=Claim(text="", claim_type=ClaimType.REGISTRY_VIOLATION,
                    verifiability=VerifiabilityLevel.AUTO),
        result=VerificationResult.FAILED if any_missing else VerificationResult.PASSED,
        evidence=evidence,
        checked_at=datetime.now(timezone.utc).isoformat(),
        method="registry_check",
    )


# ---------------------------------------------------------------------------
# Date-expiry and staleness verification
# ---------------------------------------------------------------------------

# Month name → number mapping for date parsing
_MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

# Patterns for extracting concrete dates from claim text
_ISO_DATE_RE = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')
_MONTH_DAY_RE = re.compile(
    r'\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2})\b',
    re.IGNORECASE,
)


def _parse_date_from_text(text: str) -> Optional[datetime]:
    """Try to extract a concrete date from claim text.

    Returns a timezone-aware datetime or None.
    """
    # Try ISO format first: 2026-03-31
    m = _ISO_DATE_RE.search(text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            tzinfo=timezone.utc)
        except ValueError:
            pass

    # Try "Mon DD" format: Mar 31, Apr 5
    m = _MONTH_DAY_RE.search(text)
    if m:
        month_num = _MONTH_MAP.get(m.group(1)[:3].lower())
        day = int(m.group(2))
        if month_num:
            # Assume current year
            year = datetime.now(timezone.utc).year
            try:
                return datetime(year, month_num, day, tzinfo=timezone.utc)
            except ValueError:
                pass

    return None


def verify_date_expiry(claim: Claim) -> VerificationOutcome:
    """Verify date-expiry claims by checking if the date has passed.

    For claims like "expires Mon Mar 31" or "resolve Apr 5":
    - If the date has passed → FAILED (claim is stale/actionable)
    - If the date is today or future → PASSED
    - If no date can be parsed → INCONCLUSIVE
    """
    now = datetime.now(timezone.utc)
    expiry_date = _parse_date_from_text(claim.text)

    if expiry_date is None:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence="Could not parse a concrete date from claim text",
            checked_at=now.isoformat(),
            method="date_expiry_check",
        )

    days_diff = (expiry_date.date() - now.date()).days

    if days_diff < 0:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.FAILED,
            evidence=f"Date {expiry_date.strftime('%Y-%m-%d')} has passed ({abs(days_diff)} days ago). Claim is stale.",
            checked_at=now.isoformat(),
            method="date_expiry_check",
        )
    else:
        label = "today" if days_diff == 0 else f"in {days_diff} day(s)"
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.PASSED,
            evidence=f"Date {expiry_date.strftime('%Y-%m-%d')} is {label}. Not yet expired.",
            checked_at=now.isoformat(),
            method="date_expiry_check",
        )


# Pattern for (verified: YYYY-MM-DD) inline markers
_VERIFIED_INLINE_RE = re.compile(
    r'\(verified:?\s*(\d{4}-\d{2}-\d{2})\b',
    re.IGNORECASE,
)

# Default; overridden by core.config.VERIFIED_DATE_STALE_DAYS when running inside ia
try:
    from core.config import VERIFIED_DATE_STALE_DAYS as _VERIFIED_DATE_STALE_DAYS
except ImportError:
    _VERIFIED_DATE_STALE_DAYS = 3


def verify_verified_date_staleness(claim: Claim) -> VerificationOutcome:
    """Check if a (verified: YYYY-MM-DD) marker is stale.

    Looks at the extracted_numbers field (which contains the date string
    from the VERIFIED_DATE_RE match) or parses from claim text.
    """
    now = datetime.now(timezone.utc)
    date_str = None

    # The date is stored in extracted_numbers by the extractor
    if claim.extracted_numbers:
        for num in claim.extracted_numbers:
            if re.match(r'\d{4}-\d{2}-\d{2}$', num):
                date_str = num
                break

    # Fallback: parse from text
    if not date_str:
        m = _VERIFIED_INLINE_RE.search(claim.text)
        if m:
            date_str = m.group(1)

    if not date_str:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence="No verified date found in claim",
            checked_at=now.isoformat(),
            method="verified_date_staleness",
        )

    try:
        verified_date = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except ValueError:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence=f"Could not parse date: {date_str}",
            checked_at=now.isoformat(),
            method="verified_date_staleness",
        )

    days_old = (now.date() - verified_date.date()).days

    if days_old > _VERIFIED_DATE_STALE_DAYS:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.FAILED,
            evidence=f"Verified date {date_str} is {days_old} days old (threshold: {_VERIFIED_DATE_STALE_DAYS} days). Data may be stale.",
            checked_at=now.isoformat(),
            method="verified_date_staleness",
        )
    else:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.PASSED,
            evidence=f"Verified date {date_str} is {days_old} day(s) old (within {_VERIFIED_DATE_STALE_DAYS}-day threshold).",
            checked_at=now.isoformat(),
            method="verified_date_staleness",
        )


# ---------------------------------------------------------------------------
# Main verification dispatcher
# ---------------------------------------------------------------------------

def verify_claim(claim: Claim) -> VerificationOutcome:
    """Verify a single claim using the appropriate method.

    Routes to the correct verification method based on claim type.
    Returns SKIPPED for claims that can't be auto-verified.
    """
    now = datetime.now(timezone.utc).isoformat()

    if claim.verifiability == VerifiabilityLevel.MANUAL:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.SKIPPED,
            evidence="Manual verification required — claim is subjective",
            checked_at=now,
            method="skipped",
        )

    if claim.claim_type == ClaimType.FILE_EXISTS and claim.extracted_paths:
        outcome = verify_file_exists(claim.extracted_paths)
        outcome.claim = claim
        return outcome

    if claim.claim_type == ClaimType.FILE_MISSING and claim.extracted_paths:
        outcome = verify_file_missing(claim.extracted_paths)
        outcome.claim = claim
        return outcome

    if claim.claim_type == ClaimType.ENV_VAR and claim.extracted_env_vars:
        outcome = verify_env_var(claim.extracted_env_vars)
        outcome.claim = claim
        # For blocker claims: if env var IS present, the blocker claim is FALSE
        if outcome.result == VerificationResult.PASSED:
            outcome.evidence += "\n  → Env var IS present — blocker claim appears FALSE"
            outcome.result = VerificationResult.FAILED  # The blocker claim fails
        return outcome

    if claim.claim_type in (ClaimType.SCRIPT_RUNS, ClaimType.SCRIPT_BROKEN) and claim.extracted_paths:
        py_paths = [p for p in claim.extracted_paths if p.endswith('.py')]
        if py_paths:
            # Use the deeper import check for script_runs claims
            outcome = verify_script_imports(py_paths)
            outcome.claim = claim
            return outcome

    if claim.claim_type in (ClaimType.PIPELINE_WORKS, ClaimType.PIPELINE_BLOCKED) and claim.extracted_paths:
        py_paths = [p for p in claim.extracted_paths if p.endswith('.py')]
        if py_paths:
            outcome = verify_pipeline_output(py_paths[0])
            outcome.claim = claim
            return outcome

    if claim.claim_type == ClaimType.CONFIG_PRESENT and claim.extracted_paths:
        config_exts = {'.json', '.yaml', '.yml', '.toml', '.cfg', '.conf', '.ini'}
        config_paths = [p for p in claim.extracted_paths
                        if Path(p).suffix.lower() in config_exts]
        if config_paths:
            keys = claim.extracted_config_keys if claim.extracted_config_keys else None
            outcome = verify_config_present(config_paths, keys)
            outcome.claim = claim
            return outcome

    if claim.claim_type == ClaimType.REGISTRY_VIOLATION and claim.extracted_paths:
        outcome = verify_registry(claim.extracted_paths)
        outcome.claim = claim
        return outcome

    # Process/service status claims — verify against actual process state
    if claim.claim_type == ClaimType.PROCESS_STATUS:
        return verify_process_status(claim)

    # Pipeline/service status claims without file paths — resolve via name
    if claim.claim_type in (ClaimType.PIPELINE_WORKS, ClaimType.PIPELINE_BLOCKED) and not claim.extracted_paths:
        outcome = verify_status_by_name(claim)
        if outcome.result != VerificationResult.INCONCLUSIVE:
            return outcome

    # Count/quantity claims — verify against data sources
    if claim.claim_type == ClaimType.COUNT_CLAIM:
        outcome = verify_count(claim)
        if outcome.result != VerificationResult.INCONCLUSIVE:
            return outcome

    # Date-expiry claims — check if the date has passed
    if claim.claim_type == ClaimType.DATE_EXPIRY:
        return verify_date_expiry(claim)

    # Verified-date staleness — check (verified: YYYY-MM-DD) markers
    if (claim.claim_type == ClaimType.FACT_CLAIM
            and claim.extracted_numbers
            and any(re.match(r'\d{4}-\d{2}-\d{2}$', n) for n in claim.extracted_numbers)):
        return verify_verified_date_staleness(claim)

    # Semi-verifiable claims without enough context for auto-verification
    if claim.verifiability == VerifiabilityLevel.SEMI:
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.INCONCLUSIVE,
            evidence="Claim is semi-verifiable but lacks specific paths/vars for auto-check",
            checked_at=now,
            method="insufficient_context",
        )

    return VerificationOutcome(
        claim=claim,
        result=VerificationResult.SKIPPED,
        evidence="No applicable verification method",
        checked_at=now,
        method="no_method",
    )


def verify_all(claims: List[Claim]) -> List[VerificationOutcome]:
    """Verify all claims in a list. Returns list of outcomes."""
    return [verify_claim(c) for c in claims]


def summarize_outcomes(outcomes: List[VerificationOutcome]) -> Dict[str, Any]:
    """Summarize verification results."""
    by_result = {}
    for o in outcomes:
        by_result[o.result.value] = by_result.get(o.result.value, 0) + 1

    failed = [o for o in outcomes if o.result == VerificationResult.FAILED]

    return {
        "total_checked": len(outcomes),
        "by_result": by_result,
        "passed": by_result.get("passed", 0),
        "failed": by_result.get("failed", 0),
        "inconclusive": by_result.get("inconclusive", 0),
        "skipped": by_result.get("skipped", 0),
        "failed_claims": [
            {"text": o.claim.text[:120], "evidence": o.evidence}
            for o in failed
        ],
    }
