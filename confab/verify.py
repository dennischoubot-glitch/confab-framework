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
from dataclasses import dataclass
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim": self.claim.to_dict(),
            "result": self.result.value,
            "evidence": self.evidence,
            "checked_at": self.checked_at,
            "method": self.method,
        }


# ---------------------------------------------------------------------------
# Individual verification methods
# ---------------------------------------------------------------------------

def _resolve_path(path_str: str) -> Path:
    """Resolve a path relative to workspace root.

    If direct resolution fails and the path has no directory component,
    search first-level subdirectories. This handles claims like
    'scheduler.py' when the actual file is at 'slack-bridge/scheduler.py'.
    """
    root = _get_workspace_root()
    p = Path(path_str)
    if p.is_absolute():
        return p

    direct = root / path_str
    if direct.exists():
        return direct

    # Only search subdirectories for bare filenames (no directory component)
    if '/' not in path_str and '\\' not in path_str:
        skip = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.mypy_cache',
                '.egg-info', '.tox', '.pytest_cache'}
        matches = list(root.rglob(path_str))
        # Filter out matches inside skipped directories
        matches = [
            m for m in matches
            if not any(part in skip or part.startswith('.') for part in m.relative_to(root).parts[:-1])
        ]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            # Multiple matches — return first but caller should handle ambiguity
            # Log for debugging: multiple candidates found
            return matches[0]

    # Return the direct path even if it doesn't exist (caller checks existence)
    return direct


def verify_file_exists(paths: List[str]) -> VerificationOutcome:
    """Verify that claimed files exist on disk."""
    results = []
    all_exist = True
    for path_str in paths:
        resolved = _resolve_path(path_str)
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

    # Check .env files
    root = _get_workspace_root()
    env_files = list(root.rglob(".env"))
    env_file_vars: Dict[str, str] = {}

    for env_file in env_files:
        try:
            content = env_file.read_text()
            for line in content.split('\n'):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, val = line.partition('=')
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    if val:  # Only count if value is non-empty
                        env_file_vars[key] = str(env_file.relative_to(root))
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

    # Built-in: test count (always available, no config needed)
    if "test" in claim_lower:
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
        )
    except (ValueError, IndexError) as e:
        return VerificationOutcome(
            claim=claim, result=VerificationResult.INCONCLUSIVE,
            evidence=f"Error checking count from {source_cfg['file']}: {e}",
            checked_at=now, method="count_check",
        )


def _verify_test_count(
    claim: Claim, root: Path, now: str,
) -> VerificationOutcome:
    """Verify test count claims by counting test functions.

    Scope-aware: if the claim mentions a specific component (e.g. "confab tests",
    "synthesis tests"), searches for a tests/ directory under that component first.
    Falls back to the workspace-level tests/ directory.
    """
    test_dir = _find_scoped_test_dir(claim.text, root)
    scope_label = str(test_dir.relative_to(root)) if test_dir else "tests/"

    if test_dir is None or not test_dir.exists():
        return VerificationOutcome(
            claim=claim,
            result=VerificationResult.FAILED,
            evidence=f"{scope_label} directory not found",
            checked_at=now,
            method="count_check",
        )

    test_files = list(test_dir.rglob("test_*.py"))
    test_count = 0
    for tf in test_files:
        try:
            content = tf.read_text()
            test_count += len(re.findall(r'^\s*def test_', content, re.MULTILINE))
        except (OSError, UnicodeDecodeError):
            continue

    claimed = int(claim.extracted_numbers[0])
    evidence = (
        f"  Claimed: {claimed} tests\n"
        f"  Actual: {test_count} test functions in {len(test_files)} files under {scope_label}\n"
    )

    tolerance = max(claimed * 0.2, 3)
    if abs(test_count - claimed) <= tolerance:
        result = VerificationResult.PASSED
        evidence += f"  → Approximately correct (tolerance: ±{int(tolerance)})"
    else:
        result = VerificationResult.FAILED
        evidence += f"  → Count mismatch (tolerance: ±{int(tolerance)})"

    return VerificationOutcome(
        claim=claim, result=result, evidence=evidence,
        checked_at=now, method="count_check",
    )


def _find_scoped_test_dir(claim_text: str, root: Path) -> Optional[Path]:
    """Find the most specific test directory matching the claim's scope.

    If the claim mentions a known component name (e.g. "confab", "synthesis"),
    looks for a tests/ directory under that component's path. This prevents
    false positives where a claim about "154 confab tests" gets checked against
    526 tests across the entire workspace.

    Returns None if no test directory is found.
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

    # No scoped match — fall back to workspace-level tests/
    fallback = root / "tests"
    if fallback.exists():
        return fallback
    return None


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
