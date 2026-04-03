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
    COMMIT_EXISTS = "commit_exists"      # "commit abc1234", "see abc1234a"
    DATE_EXPIRY = "date_expiry"              # "expires Mon", "resolve Apr 5"
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
    extracted_commits: List[str] = field(default_factory=list)  # Git commit hashes
    context: str = ""                  # Surrounding text for context
    age_builds: int = 0                # How many builds this has persisted
    confidence: float = 1.0            # 0.0-1.0 extraction confidence score

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
            "commits": self.extracted_commits,
            "age_builds": self.age_builds,
            "confidence": self.confidence,
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
# Handles both "pipeline is working" and "pipeline: WORKING" and "pipeline: **WORKING**"
PIPELINE_STATUS_RE = re.compile(
    r'(?:pipeline|script|cron|process|service)[\s:]+(?:is\s+)?\*{0,2}'
    r'(?:working|running|operational|active|healthy|broken|failing|down|blocked|stopped)'
    r'\*{0,2}',
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

# Directive/constraint patterns — lines that prescribe limits or rules, not factual
# count assertions. e.g. "1-2 entries per day maximum" is a limit, not a count claim.
DIRECTIVE_RE = re.compile(
    r'\b(?:maximum|minimum|max|min|limit|per\s+(?:day|hour|week|sprint|session)'
    r'|at\s+most|at\s+least|no\s+more\s+than|up\s+to|cap\s+(?:of|at)'
    r'|should\s+(?:not\s+exceed|be\s+(?:under|below|above))'
    r'|(?:redirect|redirect\s+to|when\s+today)'
    r'|already\s+published\s+today'
    r'|well\s+over\s+the)\b',
    re.IGNORECASE,
)

# Narrative context patterns — lines that describe what happened (past tense),
# not assertions about current system state. These should not trigger claim extraction.
NARRATIVE_RE = re.compile(
    r'^\*{0,2}(?:What happened|This build|Domain note|Domain)\*{0,2}\s*[:—]',
    re.IGNORECASE,
)

# Build section header pattern (to track claim age)
# Matches both builder-style "## Previous Build (date)" and
# dreamer-style "## Previous session — date — context" headers.
BUILD_HEADER_RE = re.compile(
    r'^##\s+(?:Latest|Previous|Current|Last)\s+(?:Build|session)\b',
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

# Env var natural-language status — "KEY is not set", "KEY missing", "KEY absent"
# Catches env var claims OUTSIDE blocker context (blocker context handled separately).
ENV_VAR_STATUS_RE = re.compile(
    r'\b([A-Z][A-Z0-9_]{2,})\b'
    r'\s+(?:is\s+)?'
    r'(?:not\s+(?:set|configured|found|available|defined|present)'
    r'|missing|absent|unset|unavailable)',
    re.IGNORECASE,
)

# Status claims with error codes or expiry — "returned 403", "cookie expired"
STATUS_ERROR_RE = re.compile(
    r'(?:returned|got|received|threw|raised|hit)\s+(?:a\s+)?(\d{3})\s*(?:error|status|response)?'
    r'|(?:has\s+)?(?:expired|timed?\s*out)'
    r'|(?:returned|got|received)\s+(?:an?\s+)?error',
    re.IGNORECASE,
)

# Standing item status pattern — catches "Label: STATUS" format in priority file bullet points.
# These are common in the "Standing Items" section: "Substack cookie: **WORKING**",
# "Substack responder: RE-ENABLED", "Notes posting: DISABLED".
# Requires a bullet-point context (- or *) to avoid matching table rows or paragraphs.
STANDING_STATUS_RE = re.compile(
    r'^\s*[-*]\s+\*{0,2}[^:\n]{2,50}\*{0,2}\s*:\s*\*{0,2}'
    r'(WORKING|RUNNING|BLOCKED|STOPPED|DISABLED|ENABLED|RE-ENABLED|OPERATIONAL|PARTIAL|ACTIVE|INACTIVE|PAUSED|COMPLETED|FAILED|BROKEN|DOWN|HEALTHY)'
    r'\*{0,2}',
    re.IGNORECASE,
)

# Fractional count pattern — "118/470 resolved", "30/33 posted"
FRAC_COUNT_RE = re.compile(
    r'(\d+)\s*/\s*(\d+)\s+(?:resolved|completed|done|passed|failed|processed|posted|published|remaining)',
    re.IGNORECASE,
)

# Factual claims with specific numbers — "CPI at 3.5%", "Brent at $107", "rate is 4.2%"
FACT_CLAIM_RE = re.compile(
    r'(?:is\s+(?:at\s+)?|at\s+|was\s+(?:at\s+)?|hit\s+|reached\s+|stands?\s+at\s+|rose\s+to\s+|fell\s+to\s+|dropped\s+to\s+)'
    r'(?:\$\s*)?\d+(?:[.,]\d+)?(?:\s*%)',
    re.IGNORECASE,
)

# Date-verified staleness markers — "(verified: 2026-03-26 ...)" in section headers or inline.
# These tag when data was last refreshed; the gate checks if they're stale (> threshold days).
VERIFIED_DATE_RE = re.compile(
    r'\(verified:?\s*(\d{4}-\d{2}-\d{2})\b[^)]*\)',
    re.IGNORECASE,
)

# Portfolio monetary claims — "Cash: $200.91", "Total value: $475.28", "P&L: +$43.45"
# Matches common portfolio-table patterns with dollar amounts.
PORTFOLIO_VALUE_RE = re.compile(
    r'(?:Cash|Total\s+value|Unrealized\s+P&?L|P&?L|Cost\s+basis|Balance)'
    r'\s*[:=]\s*[+\-]?\$[\d,]+(?:\.\d{1,2})?',
    re.IGNORECASE,
)

# Project pipeline counts — "11 leads", "5 modules", "3 contracts", "22 curated authors"
# Catches domain-specific counts that go stale (e.g., Sentinel leads, Confab modules).
PIPELINE_COUNT_RE = re.compile(
    r'(?<!\w)(\d+)\s+(?:leads?|modules?|contracts?|positions?|dossiers?|authors?|targets?'
    r'|candidates?|referrals?|signals?|monitors?|detections?|providers?)',
    re.IGNORECASE,
)

# Git commit reference pattern — matches commit hashes in backticks or after "commit"
# Catches: `abc1234a`, `abc12345`, commit abc1234, Commits: abc1234
COMMIT_REF_RE = re.compile(
    r'(?:commit\s+|Commits?:\s*)([0-9a-f]{7,40})\b'
    r'|`([0-9a-f]{7,40})`',
    re.IGNORECASE,
)

# Date-expiry claims — lines containing BOTH expiry language AND a date reference.
# Catches "expires Mon", "EXPIRY MON.", "resolve Apr 5", "Mon Mar 31: Gas contracts expire".
# These are time-sensitive claims that become actionable (or stale) on a specific date.
DATE_EXPIRY_WORD_RE = re.compile(
    r'\b(?:expir(?:es?|y|ation|ing)|resolves?|deadline|due\b)',
    re.IGNORECASE,
)

# Date references: day names, month+day, ISO dates, relative dates
DATE_REF_RE = re.compile(
    r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\b'
    r'|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}\b'
    r'|\b\d{4}-\d{2}-\d{2}\b'
    r'|\b(?:TOMORROW|TODAY|YESTERDAY|NEXT\s+WEEK|THIS\s+WEEK)\b',
    re.IGNORECASE,
)

# Approximate count pattern — "~65 published", "~320 entries"
# These are explicitly excluded from COUNT_RE but still go stale.
# Semi-verifiable: the exact number can be checked but tolerance is wider.
APPROX_COUNT_RE = re.compile(
    r'~(\d+)\s+(?:entries|items|posts|notes|files|tests|builds|sprints|days|hours|commits|'
    r'observations|ideas|principles|scripts|databases|subscribers|views|published|posted|'
    r'leads|modules|contracts|positions|dossiers|authors|targets|curated)',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Generic agent output patterns (for non-ia agent text)
# ---------------------------------------------------------------------------

# Generic fractional count — "(42/42)", "42/42", "Tests pass (42/42)"
# Broader than FRAC_COUNT_RE: doesn't require a trailing word like "passed/resolved".
# Catches test results, progress counts, and inline ratios in prose.
GENERIC_FRAC_RE = re.compile(
    r'(\d+)\s*/\s*(\d+)',
)

# Positive env var status — "DATABASE_URL is set", "ENV var X is configured"
# Complements ENV_VAR_STATUS_RE which only catches negative status ("not set", "missing").
ENV_VAR_POSITIVE_RE = re.compile(
    r'(?:ENV\s+(?:var(?:iable)?)\s+)?'
    r'([A-Z][A-Z0-9_]{2,})'
    r'\s+(?:(?:environment\s+)?(?:var(?:iable)?\s+)?)?'
    r'(?:is\s+)?(?:set|configured|defined|present|available|exists)',
    re.IGNORECASE,
)

# Inline file path in prose — "/tmp/test.yaml", "/etc/nginx/conf.d/app.conf"
# Broader than FILE_PATH_RE: catches absolute paths without backticks in prose,
# even without ia-specific directory structures like core/ or projects/.
INLINE_PATH_RE = re.compile(
    r'(?:^|[\s(])(/(?:[\w./-]+/)*[\w.-]+\.[\w]+)',
)

# Sentence boundary pattern for splitting multi-sentence lines.
# Splits on ". " followed by an uppercase letter (new sentence), preserving
# each sentence as a standalone unit for independent claim extraction.
_SENTENCE_SPLIT_RE = re.compile(r'(?<=\.)\s+(?=[A-Z])')

# ---------------------------------------------------------------------------
# Generic claim patterns (for arbitrary agent output, not just ia files)
# ---------------------------------------------------------------------------

# Generic quantitative claims — "improved 40%", "handles 10K qps", "reduced by 50ms"
# Catches change/measurement verbs paired with numbers (optionally suffixed with
# %, K, M, B, x, ms, qps, etc.). Two forms:
#   verb + [by/to] + number[suffix]  — "improved 40%", "handles 10K qps"
#   number[suffix] + context_word    — "40% improvement", "3x faster"
GENERIC_QUANTITATIVE_RE = re.compile(
    r'(?:'
    # Form 1: verb + number+suffix
    r'(?:improv(?:ed|es?|ing)|reduc(?:ed|es?|ing)|increas(?:ed|es?|ing)|decreas(?:ed|es?|ing)'
    r'|grew|dropped|surged|declined|rose|fell|cut|doubled|tripled|halved'
    r'|handles?|processes?|serves?|supports?|achieves?|delivers?|generates?|produces?'
    r'|takes?|costs?|saves?|consumes?|runs?\b|hits?|reaches?|exceeds?)'
    r'\s+(?:by\s+|to\s+|at\s+|about\s+|approximately\s+|roughly\s+|nearly\s+|over\s+|under\s+|up\s+to\s+)?'
    r'(?:\$\s*)?'
    r'\d[\d,]*(?:\.\d+)?'
    r'(?:\s*(?:%|percent|x|ms|seconds?|minutes?|hours?|K|M|B|k|m|b|GB|MB|TB|qps|rps|tps|ops(?:/s(?:ec)?)?))?'
    r'|'
    # Form 2: number+suffix + context word
    r'(?:\$\s*)?'
    r'\d[\d,]*(?:\.\d+)?'
    r'\s*(?:%|percent|x|ms|seconds?|minutes?|hours?|K|M|B|k|m|b|GB|MB|TB|qps|rps|tps|ops(?:/s(?:ec)?)?)'
    r'\s+(?:improv|reduc|increas|decreas|fast|slow|more|less|better|worse|higher|lower|gain|loss|drop|growth|decline)'
    r')',
    re.IGNORECASE,
)

# Generic temporal claims — "completed yesterday", "ships Friday", "deployed last week"
# Catches action verbs paired with temporal references (day names, relative dates).
GENERIC_TEMPORAL_RE = re.compile(
    r'(?:'
    # Form 1: verb + [prep] + time reference
    r'(?:complet(?:ed|es?|ing)|deploy(?:ed|s|ing)?|shipp?(?:ed|s|ing)?|launch(?:ed|es|ing)?'
    r'|releas(?:ed|es|ing)|migrat(?:ed|es|ing)|finish(?:ed|es|ing)|start(?:ed|s|ing)?'
    r'|submitt?(?:ed|s|ing)?|deliver(?:ed|s|ing)?|publish(?:ed|es|ing)?'
    r'|push(?:ed|es|ing)?|merg(?:ed|es|ing)|remov(?:ed|es|ing)|delet(?:ed|es|ing)'
    r'|add(?:ed|s|ing)?|creat(?:ed|es|ing)|updat(?:ed|es|ing)|resolv(?:ed|es|ing)|fix(?:ed|es|ing)?)'
    r'\s+(?:on\s+|by\s+|before\s+|after\s+|since\s+|until\s+)?'
    r'(?:yesterday|today|tomorrow'
    r'|last\s+(?:week|month|night|(?:Mon|Tue|Wednes|Thurs|Fri|Satur|Sun)day)'
    r'|next\s+(?:week|month|(?:Mon|Tue|Wednes|Thurs|Fri|Satur|Sun)day)'
    r'|(?:Mon|Tue|Wednes|Thurs|Fri|Satur|Sun)day'
    r'|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}'
    r'|\d{4}-\d{2}-\d{2})'
    r'|'
    # Form 2: time reference + verb
    r'(?:yesterday|today|tomorrow'
    r'|last\s+(?:week|month|night|(?:Mon|Tue|Wednes|Thurs|Fri|Satur|Sun)day)'
    r'|next\s+(?:week|month|(?:Mon|Tue|Wednes|Thurs|Fri|Satur|Sun)day)'
    r'|(?:Mon|Tue|Wednes|Thurs|Fri|Satur|Sun)day'
    r'|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+\d{1,2}'
    r'|\d{4}-\d{2}-\d{2})'
    r'\s*[,:]?\s*'
    r'(?:complet(?:ed|es?|ing)|deploy(?:ed|s|ing)?|shipp?(?:ed|s|ing)?|launch(?:ed|es|ing)?'
    r'|releas(?:ed|es|ing)|migrat(?:ed|es|ing)|finish(?:ed|es|ing)|start(?:ed|s|ing)?'
    r'|submitt?(?:ed|s|ing)?|deliver(?:ed|s|ing)?|publish(?:ed|es|ing)?'
    r'|push(?:ed|es|ing)?|merg(?:ed|es|ing)|remov(?:ed|es|ing)|delet(?:ed|es|ing)'
    r'|add(?:ed|s|ing)?|creat(?:ed|es|ing)|updat(?:ed|es|ing)|resolv(?:ed|es|ing)|fix(?:ed|es|ing)?)'
    r')',
    re.IGNORECASE,
)

# Generic status assertions — "Migration completed", "server is operational", "bug fixed"
# Catches subject + status-verb patterns without requiring ia-specific context.
# Only matches when the status word is the MAIN assertion, not incidental.
GENERIC_STATUS_RE = re.compile(
    r'(?:^|[.!]\s+)'  # Start of line or new sentence
    r'(?:\w+\s+){0,4}'  # Up to 4 words of subject
    r'(?:is\s+|was\s+|has\s+been\s+|got\s+)?'
    r'(?:completed?|deployed?|shipped|launched|released|migrated'
    r'|operational|functional|live|working|running|serving|available'
    r'|fixed|resolved|implemented|installed|configured|enabled|disabled'
    r'|broken|down|failing|crashed|offline|degraded|unavailable)'
    r'\s*[.!]?\s*$',  # End of line or sentence
    re.IGNORECASE | re.MULTILINE,
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

    # Pre-process: expand multi-sentence lines into separate entries.
    # Generic agent output often packs multiple claims into one line:
    #   "File deployed. Tests pass (42/42). Server running."
    # Each sentence may contain a distinct claim that would be masked by
    # the first-match-then-continue logic. We split BEFORE the main loop
    # so each sentence is processed independently.
    expanded_lines: List[Tuple[int, str]] = []  # (original_line_num, text)
    for line_num, line in enumerate(lines, 1):
        stripped_check = line.strip()
        # Only split non-heading, non-empty, non-bullet lines with multiple sentences
        if (stripped_check
            and not stripped_check.startswith('#')
            and not stripped_check.startswith('|')
            and '.' in stripped_check):
            subs = _SENTENCE_SPLIT_RE.split(stripped_check)
            if len(subs) > 1:
                for sub in subs:
                    # Re-apply leading whitespace style from original for bullet detection
                    if line.lstrip().startswith(('-', '*')):
                        expanded_lines.append((line_num, sub))
                    else:
                        expanded_lines.append((line_num, sub))
                continue
        expanded_lines.append((line_num, line))

    for line_num, line in expanded_lines:
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

        # Skip empty lines and table formatting
        if not stripped or stripped.startswith('|---'):
            continue

        # Extract verified-date markers from headings before skipping them.
        # Headings like "## Portfolio Status (verified: 2026-03-26 ...)" carry
        # staleness signals that the gate should track.
        if stripped.startswith('#'):
            verified_heading_match = VERIFIED_DATE_RE.search(line)
            if verified_heading_match:
                vtag_match = VERIFICATION_TAG_RE.search(line)
                claims.append(Claim(
                    text=stripped.lstrip('#').strip(),
                    claim_type=ClaimType.FACT_CLAIM,
                    verifiability=VerifiabilityLevel.SEMI,
                    source_file=source_file,
                    source_line=line_num,
                    verification_tag=vtag_match.group(0) if vtag_match else None,
                    extracted_numbers=[verified_heading_match.group(1)],
                    age_builds=current_build_idx,
                ))
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

        # --- Standing item status claims ("Label: STATUS" in bullet points) ---
        standing_match = STANDING_STATUS_RE.match(line)
        if standing_match and not _is_process_status_claim(line):
            # Determine if it's pipeline-related or general status
            lower = line.lower()
            if 'pipeline' in lower:
                claim = _classify_status_claim(
                    line, source_file, line_num, vtag, current_build_idx
                )
            else:
                claim = Claim(
                    text=stripped,
                    claim_type=ClaimType.STATUS_CLAIM,
                    verifiability=VerifiabilityLevel.SEMI,
                    source_file=source_file,
                    source_line=line_num,
                    verification_tag=vtag,
                    extracted_paths=_extract_file_paths(line),
                    age_builds=current_build_idx,
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

        # --- Env var status claims (standalone, outside blocker context) ---
        env_status_match = ENV_VAR_STATUS_RE.search(line)
        if env_status_match:
            var_name = env_status_match.group(1)
            all_known = _get_all_known_env_vars()
            # Only extract if it looks like a real env var (known, or ends with common suffix)
            if var_name in all_known or var_name.endswith(
                ('_KEY', '_TOKEN', '_SECRET', '_COOKIE', '_URL', '_PATH', '_API', '_ID', '_PASSWORD')
            ):
                claims.append(Claim(
                    text=stripped,
                    claim_type=ClaimType.ENV_VAR,
                    verifiability=VerifiabilityLevel.AUTO,
                    source_file=source_file,
                    source_line=line_num,
                    verification_tag=vtag,
                    extracted_env_vars=[var_name],
                    age_builds=current_build_idx,
                ))
                continue

        # --- Positive env var status: "ENV var X is set", "DATABASE_URL is configured" ---
        env_pos_match = ENV_VAR_POSITIVE_RE.search(line)
        if env_pos_match:
            var_name = env_pos_match.group(1)
            all_known = _get_all_known_env_vars()
            if var_name in all_known or var_name.endswith(
                ('_KEY', '_TOKEN', '_SECRET', '_COOKIE', '_URL', '_PATH', '_API', '_ID', '_PASSWORD')
            ):
                claims.append(Claim(
                    text=stripped,
                    claim_type=ClaimType.ENV_VAR,
                    verifiability=VerifiabilityLevel.AUTO,
                    source_file=source_file,
                    source_line=line_num,
                    verification_tag=vtag,
                    extracted_env_vars=[var_name],
                    age_builds=current_build_idx,
                ))
                continue

        # --- Git commit reference claims ---
        commit_matches = COMMIT_REF_RE.findall(line)
        if commit_matches:
            # Each match is a tuple (group1, group2) — one will be non-empty
            hashes = [g1 or g2 for g1, g2 in commit_matches if g1 or g2]
            if hashes:
                claims.append(Claim(
                    text=stripped,
                    claim_type=ClaimType.COMMIT_EXISTS,
                    verifiability=VerifiabilityLevel.AUTO,
                    source_file=source_file,
                    source_line=line_num,
                    verification_tag=vtag,
                    extracted_commits=hashes,
                    age_builds=current_build_idx,
                ))
                continue

        # --- Status/error claims (error codes, expiry, timeouts) ---
        if STATUS_ERROR_RE.search(line):
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.STATUS_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                age_builds=current_build_idx,
            ))
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
        frac_match = FRAC_COUNT_RE.search(line)
        if (count_matches or frac_match) and _is_assertion_context(line) and not _is_directive_context(line):
            numbers = list(count_matches)
            if frac_match:
                numbers.extend([frac_match.group(1), frac_match.group(2)])
            # Auto-verifiable if claim matches a configured count_source
            is_auto = _matches_count_source(stripped)
            claim = Claim(
                text=stripped,
                claim_type=ClaimType.COUNT_CLAIM,
                verifiability=VerifiabilityLevel.AUTO if is_auto else VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                extracted_numbers=numbers,
                age_builds=current_build_idx,
            )
            claims.append(claim)
            continue

        # --- Generic fractional counts: "(42/42)", "Tests pass (10/10)" ---
        # Catches test results and progress ratios that FRAC_COUNT_RE misses
        # (because FRAC_COUNT_RE requires trailing words like "passed/resolved").
        # No assertion context gate: a bare N/M ratio IS the assertion.
        generic_frac_match = GENERIC_FRAC_RE.search(line)
        if generic_frac_match and not _is_directive_context(line):
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.COUNT_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                extracted_numbers=[generic_frac_match.group(1), generic_frac_match.group(2)],
                age_builds=current_build_idx,
            ))
            continue

        # --- Fact claims with specific numbers (percentages, prices) ---
        if FACT_CLAIM_RE.search(line):
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.FACT_CLAIM,
                verifiability=VerifiabilityLevel.MANUAL,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                age_builds=current_build_idx,
            ))
            continue

        # --- Date-verified staleness markers: (verified: YYYY-MM-DD) ---
        verified_match = VERIFIED_DATE_RE.search(line)
        if verified_match:
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.FACT_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                extracted_numbers=[verified_match.group(1)],
                age_builds=current_build_idx,
            ))
            continue

        # --- Portfolio monetary claims: Cash: $NNN, Total value: $NNN ---
        if PORTFOLIO_VALUE_RE.search(line):
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.FACT_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                age_builds=current_build_idx,
            ))
            continue

        # --- Date-expiry claims: "expires Mon", "resolve Apr 5", "EXPIRY MON." ---
        has_expiry_word = DATE_EXPIRY_WORD_RE.search(line)
        has_date_ref = DATE_REF_RE.search(line)
        if has_expiry_word and has_date_ref:
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.DATE_EXPIRY,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                age_builds=current_build_idx,
            ))
            continue

        # --- Pipeline/project counts: N leads, N modules, N contracts ---
        pipeline_count_matches = PIPELINE_COUNT_RE.findall(line)
        if pipeline_count_matches and _is_assertion_context(line) and not _is_directive_context(line):
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.COUNT_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                extracted_numbers=list(pipeline_count_matches),
                age_builds=current_build_idx,
            ))
            continue

        # --- Approximate count claims: ~65 published, ~320 entries ---
        approx_matches = APPROX_COUNT_RE.findall(line)
        if approx_matches and _is_assertion_context(line) and not _is_directive_context(line):
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.COUNT_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                extracted_numbers=list(approx_matches),
                confidence=0.7,  # Lower confidence for approximate claims
                age_builds=current_build_idx,
            ))
            continue

        # =================================================================
        # Generic agent output fallbacks
        # These catch claims in arbitrary text that doesn't use ia-specific
        # formatting (bullet-point status, priority file structure, etc.).
        # Placed last so ia-specific patterns take priority.
        # =================================================================

        # --- Generic quantitative claims: "improved 40%", "handles 10K qps" ---
        if GENERIC_QUANTITATIVE_RE.search(line):
            # Extract any numbers for the claim record
            numbers = re.findall(r'\d[\d,]*(?:\.\d+)?(?:\s*[%KMBkmb])?', line)
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.FACT_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                extracted_numbers=numbers,
                age_builds=current_build_idx,
            ))
            continue

        # --- Generic temporal claims: "completed yesterday", "ships Friday" ---
        if GENERIC_TEMPORAL_RE.search(line):
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.STATUS_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                age_builds=current_build_idx,
            ))
            continue

        # --- Generic status assertions: "Migration completed.", "Server is operational." ---
        if GENERIC_STATUS_RE.search(line):
            claims.append(Claim(
                text=stripped,
                claim_type=ClaimType.STATUS_CLAIM,
                verifiability=VerifiabilityLevel.SEMI,
                source_file=source_file,
                source_line=line_num,
                verification_tag=vtag,
                age_builds=current_build_idx,
            ))
            continue

    # Score confidence for each claim
    for claim in claims:
        claim.confidence = score_confidence(claim)

    # Sort: auto-verifiable first, then semi, then manual; within each level, by confidence desc
    priority = {VerifiabilityLevel.AUTO: 0, VerifiabilityLevel.SEMI: 1, VerifiabilityLevel.MANUAL: 2}
    claims.sort(key=lambda c: (priority[c.verifiability], -c.confidence, -c.age_builds))

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
        'blocked', 'broken', 'failing', 'failed', 'missing', 'needs', 'requires',
        'present', 'configured', 'deployed', 'operational', 'healthy',
        'queued', 'pending', 'complete', 'completed', 'done', 'fixed',
        'added', 'created', 'updated', 'verified', 'confirmed',
        'status', 'still', 'not', 'should', 'must',
        'passing', 'passed', 'set', 'expired', 'returned', 'error',
        'resolved', 'posted', 'published', 'remaining',
    }
    lower = line.lower()
    return any(word in lower for word in assertion_words)


def _is_directive_context(line: str) -> bool:
    """Check if a line is a directive/constraint rather than a factual count assertion.

    Lines like "1-2 journal entries per day maximum" prescribe a limit — they are
    not asserting the current count of entries. Similarly, "7 entries already
    published today" is a time-windowed status that shouldn't be compared against
    the total entry count without proper time filtering.

    Returns True if the line looks like a directive, constraint, or limit.
    """
    return bool(DIRECTIVE_RE.search(line))


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


def _matches_count_source(text: str) -> bool:
    """Check if claim text matches a configured count_source keyword set.

    If so, the verifier can check the count against a data source — making
    it auto-verifiable instead of semi.
    """
    try:
        from .config import get_config
        lower = text.lower()
        for source_key in get_config().count_sources:
            keywords = source_key.replace("_", " ").split()
            if all(kw in lower for kw in keywords):
                return True
    except Exception:
        pass
    return False


def _matches_pipeline_name(text: str) -> bool:
    """Check if claim text matches a configured pipeline_names keyword.

    If so, the verifier can resolve the pipeline status by name — making
    it auto-verifiable even without explicit file paths in the claim.
    """
    try:
        from .config import get_config
        lower = text.lower()
        for keyword in get_config().pipeline_names:
            if keyword.lower() in lower:
                return True
    except Exception:
        pass
    return False


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

    # Auto-verifiable if file paths found OR if claim matches a configured pipeline name
    is_auto = bool(file_paths) or _matches_pipeline_name(line)

    return Claim(
        text=line.strip(),
        claim_type=claim_type,
        verifiability=VerifiabilityLevel.AUTO if is_auto else VerifiabilityLevel.SEMI,
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


def score_confidence(claim: Claim) -> float:
    """Score extraction confidence for a claim (0.0-1.0).

    Confidence reflects how certain the extractor is that:
    1. This text IS a verifiable claim (not noise)
    2. The classification is correct
    3. The extracted artifacts (paths, env vars, etc.) are accurate

    Scoring factors:
    - Specificity: concrete artifacts (paths, env vars) > vague assertions
    - Verifiability: AUTO > SEMI > MANUAL
    - Pattern strength: blocker/env var patterns are high-signal
    - Verification tags: tagged claims have human confirmation of claim-ness
    - Age: older unverified claims decay in confidence
    """
    score = 0.5  # Base: the extractor matched a pattern

    # Specificity bonus: concrete extracted artifacts
    if claim.extracted_paths:
        score += 0.2
    if claim.extracted_env_vars:
        score += 0.2
    if claim.extracted_config_keys:
        score += 0.1
    if claim.extracted_numbers:
        score += 0.1

    # Verifiability bonus: auto-verifiable = higher confidence
    if claim.verifiability == VerifiabilityLevel.AUTO:
        score += 0.15
    elif claim.verifiability == VerifiabilityLevel.SEMI:
        score += 0.05
    # MANUAL gets no bonus

    # Claim type signal strength
    high_signal_types = {
        ClaimType.FILE_EXISTS, ClaimType.FILE_MISSING,
        ClaimType.ENV_VAR, ClaimType.CONFIG_PRESENT,
    }
    medium_signal_types = {
        ClaimType.PIPELINE_WORKS, ClaimType.PIPELINE_BLOCKED,
        ClaimType.SCRIPT_RUNS, ClaimType.SCRIPT_BROKEN,
        ClaimType.PROCESS_STATUS, ClaimType.COUNT_CLAIM,
    }
    if claim.claim_type in high_signal_types:
        score += 0.1
    elif claim.claim_type in medium_signal_types:
        score += 0.05

    # Verification tag bonus: someone already tagged this
    if claim.verification_tag:
        if 'v2' in (claim.verification_tag or ''):
            score += 0.1  # Two agents confirmed
        elif 'v1' in (claim.verification_tag or ''):
            score += 0.05  # One agent verified
        elif 'FAILED' in (claim.verification_tag or ''):
            score += 0.05  # Tagged as failed = confirmed as claim

    # Age penalty: older unverified claims are less trustworthy
    if claim.age_builds > 0 and not claim.verification_tag:
        score -= min(0.15, claim.age_builds * 0.03)

    return max(0.0, min(1.0, round(score, 2)))


def flag_stale_vtags(
    claims: List[Claim],
    max_age_hours: float = 24.0,
    now: Optional[datetime] = None,
) -> List[Tuple[Claim, float]]:
    """Identify claims with verification tags older than a threshold.

    Returns a list of (claim, age_hours) tuples for claims whose vtag
    timestamp is older than max_age_hours. Behavior claims (pipeline
    status, process state) use a shorter default TTL because they
    represent transient runtime state.

    Args:
        claims: Claims to check.
        max_age_hours: Threshold in hours. Claims verified more recently
            than this are not flagged. Default: 24h.
        now: Current time (for testing). Defaults to utcnow.

    Returns:
        List of (claim, age_hours) tuples, sorted by age descending.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    stale = []
    for claim in claims:
        if not claim.verification_tag:
            continue
        ts = parse_vtag_timestamp(claim.verification_tag)
        if ts is None:
            continue
        age_hours = (now - ts).total_seconds() / 3600.0
        # Behavior claims get a tighter TTL (half the threshold)
        threshold = max_age_hours / 2 if is_behavior_claim(claim) else max_age_hours
        if age_hours > threshold:
            stale.append((claim, round(age_hours, 1)))

    stale.sort(key=lambda x: -x[1])
    return stale


def summarize_claims(claims: List[Claim]) -> Dict[str, Any]:
    """Generate a summary of extracted claims."""
    by_type = {}
    by_verifiability = {}
    for c in claims:
        by_type[c.claim_type.value] = by_type.get(c.claim_type.value, 0) + 1
        by_verifiability[c.verifiability.value] = by_verifiability.get(c.verifiability.value, 0) + 1

    confidences = [c.confidence for c in claims]
    return {
        "total": len(claims),
        "by_type": by_type,
        "by_verifiability": by_verifiability,
        "auto_verifiable": by_verifiability.get("auto", 0),
        "oldest_build_age": max((c.age_builds for c in claims), default=0),
        "untagged": sum(1 for c in claims if c.verification_tag is None),
        "avg_confidence": round(sum(confidences) / len(confidences), 2) if confidences else 0.0,
        "high_confidence": sum(1 for c in confidences if c >= 0.8),
        "low_confidence": sum(1 for c in confidences if c < 0.5),
    }
