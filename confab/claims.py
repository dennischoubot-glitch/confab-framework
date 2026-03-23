"""Claim extraction and classification for the confabulation framework.

Parses agent priority files and handoff text to identify carry-forward claims,
classify them by type, and determine which are auto-verifiable.

The key insight: most cascade-propagating confabulations in the ia system are
verifiable claims about system state (file exists, env var present, pipeline
works/blocked) that persist because no agent checks reality. This module
extracts those claims so the verification engine can test them.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ClaimType(Enum):
    """Types of claims agents make in priority files."""
    FILE_EXISTS = "file_exists"          # "X file exists / is ready"
    FILE_MISSING = "file_missing"        # "X file is missing / doesn't exist"
    ENV_VAR = "env_var"                  # "needs ENV_VAR" / "ENV_VAR is set"
    PIPELINE_WORKS = "pipeline_works"    # "pipeline X is working"
    PIPELINE_BLOCKED = "pipeline_blocked" # "X is blocked on Y"
    SCRIPT_RUNS = "script_runs"          # "script X works / runs"
    SCRIPT_BROKEN = "script_broken"      # "script X fails / is broken"
    CONFIG_PRESENT = "config_present"    # "config X is present / configured"
    PROCESS_STATUS = "process_status"    # "X is running / stopped / operational"
    COUNT_CLAIM = "count_claim"          # "X entries / N items / count of Y"
    STATUS_CLAIM = "status_claim"        # general status assertions
    FACT_CLAIM = "fact_claim"            # factual claims (dates, numbers)
    REGISTRY_VIOLATION = "registry_violation"  # file/db not in SYSTEM_REGISTRY.md
    SUBJECTIVE = "subjective"            # opinions, assessments


# Behavior claim types — transient runtime state that can change without file edits.
# Claims of these types represent point-in-time observations (API responses, process
# status, pipeline health) that go stale quickly. Verification tags on behavior claims
# should be subject to TTL expiry — a [v1: verified 10 hours ago] on "responder 403"
# tells you almost nothing about right now.
BEHAVIOR_CLAIM_TYPES = {
    ClaimType.PIPELINE_WORKS,
    ClaimType.PIPELINE_BLOCKED,
    ClaimType.PROCESS_STATUS,
    ClaimType.STATUS_CLAIM,
    ClaimType.SCRIPT_RUNS,
    ClaimType.SCRIPT_BROKEN,
}

# State claim types — durable system state that changes only via explicit action.
# Files don't spontaneously appear, env vars don't reset themselves.
# No TTL needed — the gate's static verification is sufficient.
STATE_CLAIM_TYPES = {
    ClaimType.FILE_EXISTS,
    ClaimType.FILE_MISSING,
    ClaimType.ENV_VAR,
    ClaimType.CONFIG_PRESENT,
    ClaimType.COUNT_CLAIM,
}


class VerifiabilityLevel(Enum):
    """How automatically verifiable a claim is."""
    AUTO = "auto"           # Can be verified by code right now
    SEMI = "semi"           # Partially verifiable (needs some context)
    MANUAL = "manual"       # Requires human/agent judgment


@dataclass
class Claim:
    """A single extracted claim from agent text."""
    text: str                          # Original text of the claim
    claim_type: ClaimType              # Classification
    verifiability: VerifiabilityLevel  # How verifiable
    source_file: Optional[str] = None  # File the claim was extracted from
    source_line: Optional[int] = None  # Line number
    verification_tag: Optional[str] = None  # Existing [v1]/[v2]/[unverified] tag
    extracted_paths: List[str] = field(default_factory=list)  # File paths mentioned
    extracted_env_vars: List[str] = field(default_factory=list)  # Env vars mentioned
    extracted_numbers: List[str] = field(default_factory=list)  # Numbers/counts
    extracted_config_keys: List[str] = field(default_factory=list)  # Config keys to check
    context: str = ""                  # Surrounding text for context
    age_builds: int = 0                # How many builds this has persisted

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "type": self.claim_type.value,
            "verifiability": self.verifiability.value,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "verification_tag": self.verification_tag,
            "paths": self.extracted_paths,
            "env_vars": self.extracted_env_vars,
            "numbers": self.extracted_numbers,
            "config_keys": self.extracted_config_keys,
            "age_builds": self.age_builds,
        }


# ---------------------------------------------------------------------------
# Pattern definitions for claim extraction
# ---------------------------------------------------------------------------

# File path pattern (matches common project paths)
FILE_PATH_RE = re.compile(
    r'`([^`]+\.(?:py|md|json|html|txt|yaml|yml|sh|js|ts|swift|css|db|conf|env|toml|cfg))`'
    r'|(?:^|\s)((?:[\w./-]+/)+[\w.-]+\.(?:py|md|json|html|txt|yaml|yml|sh|js|ts|swift|css|db|conf|env|toml|cfg))',
)

# Environment variable pattern
ENV_VAR_RE = re.compile(
    r'\b([A-Z][A-Z0-9_]{2,}(?:_KEY|_TOKEN|_SECRET|_URL|_PATH|_API|_ID|_PASSWORD|_COOKIE)?)\b'
)

# Default known env var names (always checked).
# These are common env vars found in most projects.
# Extended at runtime with project-specific env vars via _get_all_known_env_vars()
# (from confab.toml or ia-repo defaults).
_DEFAULT_KNOWN_ENV_VARS = {
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CLAUDE_API_KEY',
    'GITHUB_TOKEN', 'DATABASE_URL',
    'SECRET_KEY', 'API_KEY', 'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY', 'GOOGLE_API_KEY',
}

# Backwards-compatible alias
KNOWN_ENV_VARS = _DEFAULT_KNOWN_ENV_VARS


def _get_all_known_env_vars() -> set:
    """Merge default known env vars with any configured extras."""
    try:
        from .config import get_config
        return _DEFAULT_KNOWN_ENV_VARS | get_config().known_env_vars
    except Exception:
        return _DEFAULT_KNOWN_ENV_VARS

# Verification tag patterns
VERIFICATION_TAG_RE = re.compile(
    r'\[(v[12]):\s*(?:checked\s+)?(.+?)(?:\s+\d{4}-\d{2}-\d{2})?\]'
    r'|\[(unverified)\]'
    r'|\[(verified(?::\s*\d{4}-\d{2}-\d{2})?)\]'
    r'|\[FAILED:\s*(.+?)\]'
)

# Blocker/blocked patterns
BLOCKER_RE = re.compile(
    r'(?:blocked\s+(?:on|by)|waiting\s+(?:on|for)|needs|requires|depends\s+on|missing)\s+(.+?)(?:\.|$|\n|—)',
    re.IGNORECASE,
)

# Pipeline/script status patterns
PIPELINE_STATUS_RE = re.compile(
    r'(?:pipeline|script|cron|process|service)\s+(?:is\s+)?'
    r'(?:working|running|operational|active|healthy|broken|failing|down|blocked|stopped)',
    re.IGNORECASE,
)

# Process/service status claims — matches "X service is running", "weather-rewards: RUNNING"
# More specific than PIPELINE_STATUS_RE: requires a named service/process/monitor
PROCESS_STATUS_RE = re.compile(
    r'(?:^|\s)(\S+(?:\s+\S+)?)\s*(?:service|monitor|process|daemon|worker)?\s*'
    r'(?::|is\s+|:\s+)'
    r'(?:running|stopped|active|inactive|down|crashed|starting|backoff|exited|fatal|operational|RUNNING|STOPPED)',
    re.IGNORECASE,
)

# Count/quantity claims — excludes approximate language (prefixed with ~)
COUNT_RE = re.compile(
    r'(?<!~)(?<!\w)(\d+)\s+(?:entries|items|posts|notes|files|tests|builds|sprints|days|hours|commits|'
    r'observations|ideas|principles|scripts|databases|subscribers|views)',
    re.IGNORECASE,
)

# Narrative context patterns — lines that describe what happened (past tense),
# not assertions about current system state. These should not trigger claim extraction.
NARRATIVE_RE = re.compile(
    r'^\*{0,2}(?:What happened|This build|Domain note|Domain)\*{0,2}\s*[:—]',
    re.IGNORECASE,
)

# Build section header pattern (to track claim age)
BUILD_HEADER_RE = re.compile(
    r'^##\s+(?:Latest|Previous|Current)\s+Build\s+\((.+?)\)',
    re.MULTILINE,
)

# Meta-rule pattern — lines that describe how to handle claims, not claims themselves.
# e.g. "**Staleness rule:** ...", "**Rules:** ...", "**Size rule:** ..."
META_RULE_RE = re.compile(
    r'^\w[\w\s]*rules?\s*:',
    re.IGNORECASE,
)

# Config file detection
CONFIG_FILE_EXTS = {'.json', '.yaml', '.yml', '.toml', '.cfg', '.conf', '.ini'}

CONFIG_ASSERTION_RE = re.compile(
    r'\b(?:config(?:ured|uration)?|setting|key\b|has\s+key|contains?\s+key)\b',
    re.IGNORECASE,
)

# Config key pattern: backtick-enclosed identifiers that aren't file paths
CONFIG_KEY_RE = re.compile(r'`([a-zA-Z_][a-zA-Z0-9_.]*)`')

# Optional/conditional language — when present, file references are not existence assertions.
# e.g. "loads confab.toml or falls back to defaults", "reads config.yaml if present"
OPTIONAL_FILE_RE = re.compile(
    r'\b(?:if\s+(?:\w+\s+)?(?:present|exists?|found|available)'
    r'|or\s+(?:falls?\s+back|defaults?\s+to|uses?\s+defaults?)'
    r'|optional(?:ly)?'
    r'|when\s+(?:present|available|found)'
    r'|(?:falls?\s+back|defaults?)\s+(?:to|if))\b',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

def _get_exclude_patterns() -> List[re.Pattern]:
    """Load section exclusion patterns from config."""
    try:
        from .config import get_config
        patterns = get_config().exclude_sections
    except Exception:
        patterns = []
    return [re.compile(p, re.IGNORECASE) for p in patterns] if patterns else []


# Markdown heading pattern for section tracking
_HEADING_RE = re.compile(r'^(#{1,6})\s+(.+)')


def extract_claims(
    text: str,
    source_file: Optional[str] = None,
    exclude_sections: Optional[List[str]] = None,
) -> List[Claim]:
    """Extract verifiable claims from agent text.

    Scans text for patterns that indicate testable assertions:
    - File existence/absence claims
    - Environment variable requirements
    - Pipeline/script status claims
    - Blocker assertions
    - Count/quantity claims
    - General status claims with verification tags

    Args:
        text: The text to scan for claims.
        source_file: Path to the source file (for reporting).
        exclude_sections: Optional list of regex patterns for section headings
            to skip during extraction. If None, loads from config.

    Returns a list of Claim objects, sorted by verifiability (auto first).
    """
    claims = []
    lines = text.split('\n')

    # Build section exclusion patterns
    if exclude_sections is not None:
        excl_patterns = [re.compile(p, re.IGNORECASE) for p in exclude_sections]
    else:
        excl_patterns = _get_exclude_patterns()

    # Track which section we're in for exclusion filtering
    in_excluded_section = False
    excluded_heading_level = 0  # depth of the heading that started the exclusion

    # Track build sections for age estimation
    build_sections = list(BUILD_HEADER_RE.finditer(text))
    current_build_idx = 0

    for line_num, line in enumerate(lines, 1):
        # Update build section tracking
        for i, m in enumerate(build_sections):
            if m.start() <= sum(len(l) + 1 for l in lines[:line_num - 1]):
                current_build_idx = i

        # Check if this line is a heading — update section tracking
        stripped = line.strip()
        heading_match = _HEADING_RE.match(stripped)
        if heading_match:
            heading_level = len(heading_match.group(1))
            heading_text = heading_match.group(2).strip()

            if in_excluded_section:
                # A heading at the same or higher level ends the exclusion
                if heading_level <= excluded_heading_level:
                    in_excluded_section = False
                    # Fall through to check if THIS heading starts a new exclusion

            if not in_excluded_section:
                # Check if this heading matches an exclusion pattern
                for pattern in excl_patterns:
                    if pattern.search(heading_text):
                        in_excluded_section = True
                        excluded_heading_level = heading_level
                        break

        # Skip lines in excluded sections
        if in_excluded_section:
            continue

        # Skip headers, empty lines, and table formatting
        if not stripped or stripped.startswith('#') or stripped.startswith('|---'):
            continue

        # Skip meta-rules about claim handling (e.g. "**Staleness rule:** ...")
        # These describe how to process claims, not assertions about system state.
        clean_for_rule_check = stripped.replace('*', '').strip()
        if META_RULE_RE.match(clean_for_rule_check):
            continue

        # Skip narrative/retrospective lines (e.g. "**What happened:** ...")
        # These describe past events, not current system state assertions.
        if NARRATIVE_RE.match(clean_for_rule_check):
            continue

        # Extract existing verification tags
        vtag_match = VERIFICATION_TAG_RE.search(line)
        vtag = None
        if vtag_match:
            vtag = vtag_match.group(0)

        # --- Blocker claims (highest priority — these are the cascade propagators) ---
        blocker_matches = BLOCKER_RE.findall(line)
        if blocker_matches:
            for blocker_text in blocker_matches:
                claim = _classify_blocker_claim(
                    line, blocker_text.strip(), source_file, line_num, vtag, current_build_idx
                )
                if claim:
                    claims.append(claim)
            continue  # Don't double-count

        # --- Process/service status claims (check before pipeline) ---
        if _is_process_status_claim(line):
            claim = _classify_process_status_claim(
                line, source_file, line_num, vtag, current_build_idx
            )
            if claim:
                claims.append(claim)
            continue

        # --- Pipeline/script status claims ---
        if PIPELINE_STATUS_RE.search(line):
            claim = _classify_status_claim(
                line, source_file, line_num, vtag, current_build_idx
            )
            if claim:
                claims.append(claim)
            continue

        # --- File path and config file references in assertion context ---
        file_paths = _extract_file_paths(line)
        if file_paths and _is_assertion_context(line) and not _is_optional_reference(line):
            if _is_config_assertion(line, file_paths):
                config_keys = _extract_config_keys(line, file_paths)
                claim = Claim(
                    text=stripped,
                    claim_type=ClaimType.CONFIG_PRESENT,
                    verifiability=VerifiabilityLevel.AUTO,
                    source_file=source_file,
                    source_line=line_num,
                    verification_tag=vtag,
                    extracted_paths=file_paths,
                    extracted_config_keys=config_keys,
                    age_builds=current_build_idx,
                )
            else:
                claim = Claim(
                    text=stripped,
                    claim_type=ClaimType.FILE_EXISTS,
                    verifiability=VerifiabilityLevel.AUTO,
                    source_file=source_file,
                    source_line=line_num,
                    verification_tag=vtag,
                    extracted_paths=file_paths,
                    age_builds=current_build_idx,
                )
            claims.append(claim)
            continue

        # --- Count/quantity claims ---
        count_matches = COUNT_RE.findall(line)
        if count_matches and _is_assertion_context(line):
            claim = Claim(
                text=stripped,
                claim_type=ClaimType.COUNT_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                extracted_numbers=count_matches,
                age_builds=current_build_idx,
            )
            claims.append(claim)

    # Sort: auto-verifiable first, then semi, then manual
    priority = {VerifiabilityLevel.AUTO: 0, VerifiabilityLevel.SEMI: 1, VerifiabilityLevel.MANUAL: 2}
    claims.sort(key=lambda c: (priority[c.verifiability], -c.age_builds))

    return claims


def _extract_file_paths(text: str) -> List[str]:
    """Extract file paths from text."""
    paths = []
    for match in FILE_PATH_RE.finditer(text):
        path = match.group(1) or match.group(2)
        if path:
            paths.append(path)
    return paths


def _is_assertion_context(line: str) -> bool:
    """Check if a line contains an assertion (not just a reference)."""
    assertion_words = {
        'exists', 'ready', 'working', 'works', 'runs', 'running',
        'blocked', 'broken', 'failing', 'missing', 'needs', 'requires',
        'present', 'configured', 'deployed', 'operational', 'healthy',
        'queued', 'pending', 'complete', 'completed', 'done', 'fixed',
        'added', 'created', 'updated', 'verified', 'confirmed',
        'status', 'still', 'not', 'should', 'must',
    }
    lower = line.lower()
    return any(word in lower for word in assertion_words)


def _is_optional_reference(line: str) -> bool:
    """Check if a line describes a file as optional/conditional.

    Lines like "loads confab.toml or falls back to ia defaults" should not
    be treated as assertions that the file must exist. The file reference
    is conditional — the system works without it.
    """
    return bool(OPTIONAL_FILE_RE.search(line))


def _is_config_assertion(line: str, file_paths: List[str]) -> bool:
    """Check if a line is a config-related assertion about config files."""
    has_config_file = any(
        Path(p).suffix.lower() in CONFIG_FILE_EXTS for p in file_paths
    )
    has_config_words = bool(CONFIG_ASSERTION_RE.search(line))
    return has_config_file and has_config_words


def _extract_config_keys(line: str, file_paths: List[str]) -> List[str]:
    """Extract config key names from backticked text in a line.

    Returns identifiers in backticks that aren't file paths. These are
    candidate config keys to verify in the referenced config file.
    """
    keys = []
    file_path_set = set(file_paths)
    file_exts = {'.py', '.md', '.json', '.yaml', '.yml', '.toml', '.html',
                 '.js', '.ts', '.css', '.sh', '.swift', '.txt', '.db',
                 '.conf', '.env', '.cfg', '.ini'}
    for match in CONFIG_KEY_RE.finditer(line):
        candidate = match.group(1)
        # Skip if it's a detected file path
        if candidate in file_path_set:
            continue
        # Skip if it looks like a file path
        if '/' in candidate:
            continue
        # Skip if it looks like a file with extension
        if '.' in candidate:
            suffix = '.' + candidate.rsplit('.', 1)[-1]
            if suffix.lower() in file_exts:
                continue
        keys.append(candidate)
    return keys


def _is_process_status_claim(line: str) -> bool:
    """Check if a line is a process/service status claim.

    Matches patterns like:
    - "Weather rewards monitor: running"
    - "weather-rewards: STOPPED"
    - "slack-monitor is running"
    - "service X is operational"
    """
    lower = line.lower()
    # Must mention a service/process/monitor concept
    service_words = {'monitor', 'service', 'process', 'daemon', 'worker', 'rewards', 'server'}
    status_words = {'running', 'stopped', 'active', 'inactive', 'down',
                    'crashed', 'starting', 'backoff', 'exited', 'fatal', 'operational'}
    has_service = any(w in lower for w in service_words)
    has_status = any(w in lower for w in status_words)
    if not (has_service and has_status):
        return False
    # Exclude lines that are clearly about pipelines (pipeline output checks)
    pipeline_words = {'pipeline', 'script', 'cron'}
    if any(w in lower for w in pipeline_words):
        return False
    return True


def _classify_process_status_claim(
    line: str,
    source_file: Optional[str],
    line_num: int,
    vtag: Optional[str],
    build_idx: int,
) -> Optional[Claim]:
    """Classify a process/service status claim."""
    stripped = line.strip()
    return Claim(
        text=stripped,
        claim_type=ClaimType.PROCESS_STATUS,
        verifiability=VerifiabilityLevel.AUTO,
        source_file=source_file,
        source_line=line_num,
        verification_tag=vtag,
        extracted_paths=_extract_file_paths(line),
        age_builds=build_idx,
    )


def _classify_blocker_claim(
    line: str,
    blocker_text: str,
    source_file: Optional[str],
    line_num: int,
    vtag: Optional[str],
    build_idx: int,
) -> Optional[Claim]:
    """Classify a blocker claim by what it's blocked on."""
    # Scan the FULL LINE for env vars, not just the blocker capture group.
    # The regex captures up to the first delimiter, but env var names often
    # appear later in the line (e.g., "needs cookie — fails without SUBSTACK_COOKIE").
    scan_text = line

    # Check for env var blockers
    env_vars = []
    all_known = _get_all_known_env_vars()
    for var in all_known:
        if var.lower() in scan_text.lower() or var in scan_text:
            env_vars.append(var)

    # Check for generic env var pattern in full line
    for match in ENV_VAR_RE.finditer(scan_text):
        candidate = match.group(1)
        if candidate in all_known or candidate.endswith(('_KEY', '_TOKEN', '_SECRET', '_COOKIE')):
            if candidate not in env_vars:
                env_vars.append(candidate)

    if env_vars:
        return Claim(
            text=line.strip(),
            claim_type=ClaimType.ENV_VAR,
            verifiability=VerifiabilityLevel.AUTO,
            source_file=source_file,
            source_line=line_num,
            verification_tag=vtag,
            extracted_env_vars=env_vars,
            age_builds=build_idx,
        )

    # Check for file-based blockers
    file_paths = _extract_file_paths(blocker_text)
    if file_paths:
        return Claim(
            text=line.strip(),
            claim_type=ClaimType.FILE_MISSING,
            verifiability=VerifiabilityLevel.AUTO,
            source_file=source_file,
            source_line=line_num,
            verification_tag=vtag,
            extracted_paths=file_paths,
            age_builds=build_idx,
        )

    # General blocker — semi-verifiable
    return Claim(
        text=line.strip(),
        claim_type=ClaimType.PIPELINE_BLOCKED,
        verifiability=VerifiabilityLevel.SEMI,
        source_file=source_file,
        source_line=line_num,
        verification_tag=vtag,
        age_builds=build_idx,
    )


def _classify_status_claim(
    line: str,
    source_file: Optional[str],
    line_num: int,
    vtag: Optional[str],
    build_idx: int,
) -> Optional[Claim]:
    """Classify a pipeline/script status claim."""
    lower = line.lower()

    # Determine if it's a "works" or "broken" claim
    positive_words = {'working', 'running', 'operational', 'active', 'healthy'}
    negative_words = {'broken', 'failing', 'down', 'blocked', 'stopped'}

    is_positive = any(w in lower for w in positive_words)
    is_negative = any(w in lower for w in negative_words)

    claim_type = ClaimType.PIPELINE_BLOCKED if is_negative else ClaimType.PIPELINE_WORKS

    # Extract any file paths (scripts being referenced)
    file_paths = _extract_file_paths(line)

    return Claim(
        text=line.strip(),
        claim_type=claim_type,
        verifiability=VerifiabilityLevel.AUTO if file_paths else VerifiabilityLevel.SEMI,
        source_file=source_file,
        source_line=line_num,
        verification_tag=vtag,
        extracted_paths=file_paths,
        age_builds=build_idx,
    )


def extract_claims_from_file(
    file_path: str,
    exclude_sections: Optional[List[str]] = None,
) -> List[Claim]:
    """Extract claims from a file on disk."""
    path = Path(file_path)
    if not path.exists():
        return []
    text = path.read_text()
    return extract_claims(text, source_file=str(path), exclude_sections=exclude_sections)


def is_behavior_claim(claim: Claim) -> bool:
    """Check if a claim represents transient runtime state subject to TTL.

    Behavior claims (pipeline status, process state, API responses) are
    point-in-time observations that go stale quickly. State claims (file
    exists, env var set) are durable and don't need TTL.
    """
    return claim.claim_type in BEHAVIOR_CLAIM_TYPES


# Pattern to extract dates from verification tags: YYYY-MM-DD with optional time
_VTAG_DATE_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2})'           # Date: 2026-03-21
    r'(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?' # Optional time: 8:22PM or 8:22 PM
)

# Pattern to extract date from [verified: YYYY-MM-DD] format
_VTAG_VERIFIED_DATE_RE = re.compile(r'verified(?::\s*(\d{4}-\d{2}-\d{2}))')


def parse_vtag_timestamp(vtag: str) -> Optional[datetime]:
    """Extract a datetime from a verification tag string.

    Handles formats:
    - [v1: verified 2026-03-21 8:22PM]
    - [v1: verified 2026-03-21]
    - [v2: checked via pip show 2026-03-22]
    - [verified: 2026-03-21]

    Returns None if no date can be extracted (e.g. [unverified], [FAILED: ...]).
    """
    if not vtag:
        return None

    # Skip tags that aren't positive verification
    vtag_lower = vtag.lower()
    if 'unverified' in vtag_lower or 'failed' in vtag_lower:
        return None

    # Try [verified: YYYY-MM-DD] format first
    vm = _VTAG_VERIFIED_DATE_RE.search(vtag)
    if vm and vm.group(1):
        try:
            return datetime.strptime(vm.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # Try general date pattern
    dm = _VTAG_DATE_RE.search(vtag)
    if dm:
        date_str = dm.group(1)
        time_str = dm.group(2)
        try:
            if time_str:
                # Normalize: "8:22PM" -> "8:22 PM"
                time_clean = time_str.strip().upper()
                if time_clean[-2:] in ('AM', 'PM') and time_clean[-3] != ' ':
                    time_clean = time_clean[:-2] + ' ' + time_clean[-2:]
                return datetime.strptime(
                    f"{date_str} {time_clean}", '%Y-%m-%d %I:%M %p'
                ).replace(tzinfo=timezone.utc)
            else:
                return datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def summarize_claims(claims: List[Claim]) -> Dict[str, Any]:
    """Generate a summary of extracted claims."""
    by_type = {}
    by_verifiability = {}
    for c in claims:
        by_type[c.claim_type.value] = by_type.get(c.claim_type.value, 0) + 1
        by_verifiability[c.verifiability.value] = by_verifiability.get(c.verifiability.value, 0) + 1

    return {
        "total": len(claims),
        "by_type": by_type,
        "by_verifiability": by_verifiability,
        "auto_verifiable": by_verifiability.get("auto", 0),
        "oldest_build_age": max((c.age_builds for c in claims), default=0),
        "untagged": sum(1 for c in claims if c.verification_tag is None),
    }
