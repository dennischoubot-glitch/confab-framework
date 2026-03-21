"""Configuration for the confabulation framework.

Loads settings from confab.toml if present, otherwise uses sensible defaults.
When running inside the ia repository, defaults to ia-specific paths.
When running standalone (pip-installed), defaults to current working directory.

## confab.toml schema

```toml
[confab]
# Files to scan for carry-forward claims (relative to workspace root)
files_to_scan = [
    "docs/priorities.md",
    "notes/handoff.md",
]

# How many gate runs before unverified claims are flagged stale
stale_threshold = 3

# Where to store the tracker database (relative to workspace root)
db_path = "confab_tracker.db"

# Known environment variable names to detect in claims
[confab.env_vars]
known = ["OPENAI_API_KEY", "DATABASE_URL"]

# Pipeline output mappings: script name -> expected output paths
[confab.pipelines]
"my_pipeline.py" = ["output/data/", "output/report.json"]

# Sections to skip during claim extraction (regex patterns matched against headings)
# Lines under these headings are knowledge notes, not system state claims
exclude_sections = [
    "Germinating threads",
    "For Next Dreamer",
]
```
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set


CONFIG_FILENAME = "confab.toml"

# ia-specific defaults (used when running from the ia repo with no config file)
_IA_SCAN_FILES = [
    "core/agents/builder/builder_priorities.md",
    "core/agents/dreamer/dreamer_priorities.md",
]

_IA_PIPELINE_OUTPUTS = {
    "generate_audio.py": ["projects/synthesis/audio/"],
    "publish_substack.py": ["projects/synthesis/scripts/.substack_drafted"],
    "notes_cron.py": ["projects/synthesis/scripts/.notes_posted"],
}

# Pipeline name keywords → script names. Used by verify_status_by_name()
# to resolve status claims like "Notes pipeline operational" that lack
# explicit file paths.
_IA_PIPELINE_NAMES = {
    "audio pipeline": "generate_audio.py",
    "audio": "generate_audio.py",
    "substack pipeline": "publish_substack.py",
    "notes pipeline": "notes_cron.py",
    "notes queue": "notes_cron.py",
    "substack responder": "substack_responder.py",
    "weather monitor": "kalshi_weather_mm.py",
    "weather rewards": "kalshi_weather_mm.py",
}

_IA_KNOWN_ENV_VARS = {
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CLAUDE_API_KEY',
    'KALSHI_API_KEY', 'KALSHI_KEY_ID', 'KALSHI_PRIVATE_KEY',
    'SUBSTACK_COOKIE', 'SUBSTACK_TOKEN', 'SUBSTACK_SID',
    'SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'SLACK_WEBHOOK',
    'DEVTO_API_KEY', 'GITHUB_TOKEN', 'DATABASE_URL',
    'SECRET_KEY', 'API_KEY', 'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY', 'GOOGLE_API_KEY',
}

# Sections to skip during claim extraction (regex patterns matched against headings).
# These sections contain knowledge notes, germinating ideas, or strategic context
# that are NOT system state claims and should not trigger stale warnings.
_IA_EXCLUDE_SECTIONS = [
    r"Germinating threads",
    r"For Next Dreamer",
    r"Active Tensions",
    r"Settled Stances",
]

# Count verification sources: keyword pattern -> {file, type, count_pattern}
# Used by verify.py to check count claims against actual data.
_IA_COUNT_SOURCES = {
    "journal_entries": {
        "file": "projects/synthesis/data/posts.json",
        "type": "json_array",           # count items in a JSON array
        "json_path": "posts",           # key to the array (top-level)
    },
    "notes_queue": {
        "file": "projects/synthesis/scripts/notes_queue.md",
        "type": "regex_count",          # count regex matches
        "pattern": r"^###\s+Note\s+\d+",
        "rate_per_day": 3.0,            # 3x/day cron (9am, 1pm, 5pm PST)
        "posted_file": "projects/synthesis/scripts/.notes_posted",  # subtract posted count
    },
}


@dataclass
class ConfabConfig:
    """Configuration for the confabulation framework."""
    workspace_root: Path
    files_to_scan: List[str]
    stale_threshold: int = 3
    db_path: Optional[Path] = None
    pipeline_outputs: Dict[str, List[str]] = field(default_factory=dict)
    pipeline_names: Dict[str, str] = field(default_factory=dict)
    known_env_vars: Set[str] = field(default_factory=set)
    count_sources: Dict[str, Dict] = field(default_factory=dict)
    exclude_sections: List[str] = field(default_factory=list)

    def __post_init__(self):
        if self.db_path is None:
            self.db_path = self.workspace_root / "confab_tracker.db"


def _detect_workspace_root() -> Path:
    """Detect workspace root.

    Priority order:
    1. If cwd has a confab.toml, use cwd (external project with config)
    2. If running from within the ia repo, use the ia repo root
    3. Otherwise use cwd
    """
    cwd = Path.cwd()
    # If cwd has its own confab.toml, it's the workspace root
    if (cwd / CONFIG_FILENAME).exists():
        return cwd
    # Check if running from within the ia repo
    module_dir = Path(__file__).resolve().parent
    candidate = module_dir.parent.parent  # core/confab -> core -> repo root
    if _is_ia_repo(candidate):
        return candidate
    if _is_ia_repo(cwd):
        return cwd
    return cwd


def _is_ia_repo(root: Path) -> bool:
    """Check if a directory is the ia repository.

    Looks for ia-specific markers (not just confab's own files).
    """
    return (
        (root / "core" / "confab" / "__init__.py").exists()
        and (root / "core" / "agents").is_dir()
    )


def load_config(
    config_path: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> ConfabConfig:
    """Load configuration from confab.toml or use defaults.

    Search order for confab.toml:
    1. Explicit config_path if provided
    2. workspace_root / confab.toml
    3. Current directory / confab.toml (only when workspace_root was auto-detected)

    If no config file found:
    - Inside ia repo: use ia-specific defaults
    - Otherwise: use minimal defaults (empty files_to_scan)
    """
    explicit_root = workspace_root is not None
    if workspace_root is None:
        workspace_root = _detect_workspace_root()

    # Find and load config file
    toml_data = None
    if config_path and config_path.exists():
        toml_data = _load_toml(config_path)
    elif (workspace_root / CONFIG_FILENAME).exists():
        toml_data = _load_toml(workspace_root / CONFIG_FILENAME)
    elif not explicit_root and Path.cwd() != workspace_root and (Path.cwd() / CONFIG_FILENAME).exists():
        toml_data = _load_toml(Path.cwd() / CONFIG_FILENAME)

    if toml_data is not None:
        return _config_from_toml(toml_data, workspace_root)

    # No config file — use defaults based on context
    if _is_ia_repo(workspace_root):
        return ConfabConfig(
            workspace_root=workspace_root,
            files_to_scan=list(_IA_SCAN_FILES),
            pipeline_outputs=dict(_IA_PIPELINE_OUTPUTS),
            pipeline_names=dict(_IA_PIPELINE_NAMES),
            known_env_vars=set(_IA_KNOWN_ENV_VARS),
            count_sources=dict(_IA_COUNT_SOURCES),
            exclude_sections=list(_IA_EXCLUDE_SECTIONS),
        )

    # Standalone with no config — empty scan list
    return ConfabConfig(
        workspace_root=workspace_root,
        files_to_scan=[],
        known_env_vars=set(),
    )


def _load_toml(path: Path) -> Optional[dict]:
    """Load a TOML file. Returns None on failure."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return None


def _config_from_toml(data: dict, workspace_root: Path) -> ConfabConfig:
    """Create ConfabConfig from parsed TOML data."""
    confab = data.get("confab", {})

    files = confab.get("files_to_scan", [])
    threshold = confab.get("stale_threshold", 3)
    db = confab.get("db_path", "confab_tracker.db")

    pipelines = confab.get("pipelines", {})
    pipeline_names = confab.get("pipeline_names", {})

    env_section = confab.get("env_vars", {})
    known = set(env_section.get("known", []))

    count_sources = confab.get("count_sources", {})
    exclude_sections = confab.get("exclude_sections", [])

    return ConfabConfig(
        workspace_root=workspace_root,
        files_to_scan=files,
        stale_threshold=threshold,
        db_path=workspace_root / db,
        pipeline_outputs=pipelines,
        pipeline_names=pipeline_names,
        known_env_vars=known,
        count_sources=count_sources,
        exclude_sections=exclude_sections,
    )


# Singleton config — loaded once per process
_config: Optional[ConfabConfig] = None


def get_config() -> ConfabConfig:
    """Get the current configuration (lazy-loaded singleton)."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config: ConfabConfig) -> None:
    """Override the current configuration (useful for testing)."""
    global _config
    _config = config


def reset_config() -> None:
    """Reset the singleton so next get_config() reloads from disk."""
    global _config
    _config = None
