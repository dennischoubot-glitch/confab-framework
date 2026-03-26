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

# Environmental volatility: adjusts stale_threshold and behavior_ttl_hours.
# Accepts "low", "medium", "high", or a float 0.0–1.0.
# High volatility = looser thresholds (faster adaptation).
# Low volatility = tighter thresholds (integrity preservation).
# volatility = "medium"

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
from typing import Any, Dict, List, Optional, Set


CONFIG_FILENAME = "confab.toml"


# Named volatility presets → numeric values (0.0–1.0)
VOLATILITY_PRESETS = {
    "low": 0.2,
    "medium": 0.5,
    "high": 0.8,
}


def parse_volatility(value: Any) -> Optional[float]:
    """Parse a volatility value from string or numeric input.

    Accepts:
        - Named presets: "low", "medium", "high"
        - "auto": compute from market scan regime weights (data/market_scan.json)
        - Numeric: 0.0 to 1.0 (float or string)
        - None: no adjustment

    Returns:
        Float 0.0–1.0 or None.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        value_lower = value.strip().lower()
        if value_lower == "auto":
            from .signals import compute_volatility_from_market_scan
            return compute_volatility_from_market_scan()
        if value_lower in VOLATILITY_PRESETS:
            return VOLATILITY_PRESETS[value_lower]
        try:
            return max(0.0, min(1.0, float(value_lower)))
        except ValueError:
            return None
    return None


def adjust_thresholds(
    stale_threshold: int,
    behavior_ttl_hours: float,
    volatility: float,
) -> tuple:
    """Adjust gate thresholds based on volatility.

    Volatility 0.0 (stable) → tighter: lower stale threshold, shorter TTL.
    Volatility 0.5 (neutral) → no change.
    Volatility 1.0 (volatile) → looser: higher stale threshold, longer TTL.

    The multiplier scales linearly from 0.5× at volatility=0 to 2.0× at volatility=1.

    Returns:
        (adjusted_stale_threshold: int, adjusted_ttl_hours: float)
    """
    # Linear scale: 0.0→0.5x, 0.5→1.0x, 1.0→2.0x
    # Piecewise to keep 0.5 as the identity point:
    if volatility <= 0.5:
        # 0.0→0.5, 0.5→1.0
        multiplier = 0.5 + volatility
    else:
        # 0.5→1.0, 1.0→2.0
        multiplier = 1.0 + 2.0 * (volatility - 0.5)

    adjusted_stale = max(1, round(stale_threshold * multiplier))
    adjusted_ttl = behavior_ttl_hours * multiplier

    return adjusted_stale, adjusted_ttl


@dataclass
class ConfabConfig:
    """Configuration for the confabulation framework."""
    workspace_root: Path
    files_to_scan: List[str]
    stale_threshold: int = 3
    behavior_ttl_hours: float = 6.0    # TTL for behavior claims (hours)
    volatility: Optional[float] = None  # 0.0–1.0, adjusts thresholds
    db_path: Optional[Path] = None
    pipeline_outputs: Dict[str, List[str]] = field(default_factory=dict)
    pipeline_names: Dict[str, str] = field(default_factory=dict)
    known_env_vars: Set[str] = field(default_factory=set)
    count_sources: Dict[str, Dict] = field(default_factory=dict)
    process_services: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    exclude_sections: List[str] = field(default_factory=list)

    def __post_init__(self):
        # Coerce workspace_root to Path for developer convenience
        if isinstance(self.workspace_root, str):
            self.workspace_root = Path(self.workspace_root)
        if self.db_path is None:
            self.db_path = self.workspace_root / "confab_tracker.db"

    @property
    def effective_stale_threshold(self) -> int:
        """Stale threshold adjusted for volatility."""
        if self.volatility is None:
            return self.stale_threshold
        return adjust_thresholds(
            self.stale_threshold, self.behavior_ttl_hours, self.volatility
        )[0]

    @property
    def effective_behavior_ttl(self) -> float:
        """Behavior TTL adjusted for volatility."""
        if self.volatility is None:
            return self.behavior_ttl_hours
        return adjust_thresholds(
            self.stale_threshold, self.behavior_ttl_hours, self.volatility
        )[1]


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


def _load_ia_defaults(workspace_root: Path) -> "ConfabConfig":
    """Load ia-specific defaults. Only called when inside the ia repo."""
    try:
        from . import _ia_defaults as ia
    except ImportError:
        # _ia_defaults not available (standalone install) — empty config
        return ConfabConfig(workspace_root=workspace_root, files_to_scan=[])
    return ConfabConfig(
        workspace_root=workspace_root,
        files_to_scan=list(ia.SCAN_FILES),
        pipeline_outputs=dict(ia.PIPELINE_OUTPUTS),
        pipeline_names=dict(ia.PIPELINE_NAMES),
        known_env_vars=set(ia.KNOWN_ENV_VARS),
        count_sources=dict(ia.COUNT_SOURCES),
        process_services=dict(ia.PROCESS_SERVICES),
        exclude_sections=list(ia.EXCLUDE_SECTIONS),
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
        return _load_ia_defaults(workspace_root)

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
    behavior_ttl = confab.get("behavior_ttl_hours", 6.0)
    volatility = parse_volatility(confab.get("volatility"))
    db = confab.get("db_path", "confab_tracker.db")

    pipelines = confab.get("pipelines", {})
    pipeline_names = confab.get("pipeline_names", {})

    env_section = confab.get("env_vars", {})
    known = set(env_section.get("known", []))

    count_sources = confab.get("count_sources", {})
    process_services = confab.get("process_services", {})
    exclude_sections = confab.get("exclude_sections", [])

    return ConfabConfig(
        workspace_root=workspace_root,
        files_to_scan=files,
        stale_threshold=threshold,
        behavior_ttl_hours=behavior_ttl,
        volatility=volatility,
        db_path=workspace_root / db,
        pipeline_outputs=pipelines,
        pipeline_names=pipeline_names,
        known_env_vars=known,
        count_sources=count_sources,
        process_services=process_services,
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
