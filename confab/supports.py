"""Knowledge tree structural integrity check — upward invalidation detection.

Scans ideas, principles, and truths for references to invalidated supports.
Reports "zombie" entries (ALL supports dead) and "weakened" entries (>50% dead).

This is a read-only diagnostic. It doesn't modify the tree — the dreamer
decides what to invalidate. It surfaces the structural debt so the dreamer
can act on it.

IMPORTANT: Ideas don't need observations to remain valid. Observations are
fleeting facts; ideas are durable patterns that outlive their source evidence.
An idea with all-dead observation supports is NOT a zombie — it's normal
lifecycle. Ideas are only flagged as zombie/weakened when their non-observation
supports (other ideas, principles) are dead. (Dennis directive, Mar 20 2026)

Motivated by idea-489 (The Firewall): the tree's type hierarchy blocks
upward invalidation. Observations get invalidated but ideas standing on
those observations persist indefinitely.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Default path relative to workspace root
DEFAULT_TREE_PATH = "core/knowledge/KNOWLEDGE_TREE.json"

# Entry types that have supports pointing downward
SUPPORTED_TYPES = {"idea", "principle", "truth"}



@dataclass
class WeakEntry:
    """An entry with degraded support structure."""
    entry_id: str
    entry_type: str
    content: str
    domain: Optional[str]
    total_supports: int
    dead_supports: int
    dead_ids: List[str]
    missing_ids: List[str]  # supports referencing non-existent entries
    # Effective counts for zombie/weakened classification.
    # For ideas, these exclude dead observation supports (observations are fleeting).
    # For principles/truths, these equal the raw counts.
    effective_dead: int = 0
    effective_total: int = 0

    @property
    def dead_ratio(self) -> float:
        if self.effective_total == 0:
            return 0.0
        return self.effective_dead / self.effective_total

    @property
    def is_zombie(self) -> bool:
        """All effective supports are dead or missing."""
        return self.effective_total > 0 and self.effective_dead >= self.effective_total

    @property
    def is_weakened(self) -> bool:
        """More than 50% of effective supports are dead or missing, but not all."""
        return not self.is_zombie and self.dead_ratio > 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.entry_id,
            "type": self.entry_type,
            "content": self.content[:200],
            "domain": self.domain,
            "total_supports": self.total_supports,
            "dead_supports": self.dead_supports,
            "missing_supports": len(self.missing_ids),
            "effective_dead": self.effective_dead,
            "effective_total": self.effective_total,
            "dead_ratio": round(self.dead_ratio, 3),
            "zombie": self.is_zombie,
            "dead_ids": self.dead_ids,
            "missing_ids": self.missing_ids,
        }


@dataclass
class SupportsReport:
    """Result of the structural integrity check."""
    tree_path: str
    total_entries: int
    checked_entries: int  # entries with supports (ideas, principles, truths)
    total_supports_checked: int
    zombies: List[WeakEntry]
    weakened: List[WeakEntry]
    healthy: int
    no_supports: int  # entries in supported types that have empty supports
    invalidated_count: int  # total invalidated entries in tree
    by_type: Dict[str, Dict[str, int]]  # type -> {checked, zombie, weakened, healthy}
    by_domain: Dict[str, Dict[str, int]]  # domain -> {checked, zombie, weakened}

    @property
    def has_zombies(self) -> bool:
        return len(self.zombies) > 0

    @property
    def has_issues(self) -> bool:
        return len(self.zombies) > 0 or len(self.weakened) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tree_path": self.tree_path,
            "total_entries": self.total_entries,
            "checked_entries": self.checked_entries,
            "total_supports_checked": self.total_supports_checked,
            "zombies": len(self.zombies),
            "weakened": len(self.weakened),
            "healthy": self.healthy,
            "no_supports": self.no_supports,
            "invalidated_count": self.invalidated_count,
            "by_type": self.by_type,
            "by_domain": self.by_domain,
            "zombie_details": [z.to_dict() for z in self.zombies],
            "weakened_details": [w.to_dict() for w in self.weakened],
        }

    def format_report(self) -> str:
        """Human-readable report matching gate output style."""
        lines = []
        lines.append("# Knowledge Tree Supports Check")
        lines.append(f"\nTree: {self.tree_path}")
        lines.append(f"Total entries: {self.total_entries} ({self.invalidated_count} invalidated)")
        lines.append(f"Entries with supports: {self.checked_entries}")
        lines.append(f"Support references checked: {self.total_supports_checked}")

        if not self.has_issues:
            lines.append("\n**SUPPORTS: CLEAN** — No zombie or weakened entries found.")
            return "\n".join(lines)

        if self.zombies:
            lines.append(f"\n## ZOMBIE ENTRIES ({len(self.zombies)})")
            lines.append("")
            lines.append("ALL supports are dead or missing — these entries stand on nothing:")
            lines.append("")
            for z in self.zombies:
                lines.append(f"**{z.entry_id}** ({z.entry_type}, {z.domain or 'unset'})")
                lines.append(f"  {z.content[:120]}")
                lines.append(f"  Supports: {z.total_supports} total, {z.dead_supports} invalidated, {len(z.missing_ids)} missing")
                if z.dead_ids:
                    lines.append(f"  Dead: {', '.join(z.dead_ids[:5])}{' ...' if len(z.dead_ids) > 5 else ''}")
                if z.missing_ids:
                    lines.append(f"  Missing: {', '.join(z.missing_ids[:5])}{' ...' if len(z.missing_ids) > 5 else ''}")
                lines.append(f"  Fix: `python core/knowledge.py invalidate {z.entry_id} --reason \"All supports dead\"`")
                lines.append("")

            lines.append(f"Auto-fix all: `confab check-supports --fix`")
            lines.append(f"Preview first: `confab check-supports --fix --dry-run`")
            lines.append("")

        if self.weakened:
            lines.append(f"\n## WEAKENED ENTRIES ({len(self.weakened)})")
            lines.append("")
            lines.append(">50% of supports are dead or missing:")
            lines.append("")
            for w in self.weakened:
                pct = int(w.dead_ratio * 100)
                lines.append(f"**{w.entry_id}** ({w.entry_type}, {w.domain or 'unset'}) — {pct}% degraded")
                lines.append(f"  {w.content[:120]}")
                lines.append(f"  Supports: {w.total_supports} total, {w.dead_supports} invalidated, {len(w.missing_ids)} missing")
                lines.append("")

        # Summary by type
        lines.append("## Summary by Type")
        for entry_type in ["idea", "principle", "truth"]:
            if entry_type in self.by_type:
                t = self.by_type[entry_type]
                lines.append(f"- **{entry_type}**: {t['checked']} checked, {t['zombie']} zombie, {t['weakened']} weakened, {t['healthy']} healthy")

        # Summary by domain (top offenders)
        if self.by_domain:
            lines.append("\n## Zombie Rate by Domain")
            domain_items = sorted(
                self.by_domain.items(),
                key=lambda x: (x[1]["zombie"] + x[1]["weakened"]) / max(x[1]["checked"], 1),
                reverse=True,
            )
            for domain, counts in domain_items[:10]:
                if counts["checked"] == 0:
                    continue
                rate = (counts["zombie"] + counts["weakened"]) / counts["checked"] * 100
                lines.append(f"- {domain or 'unset'}: {rate:.0f}% degraded ({counts['zombie']} zombie, {counts['weakened']} weakened / {counts['checked']} checked)")

        return "\n".join(lines)

    def format_slack(self) -> str:
        """Concise Slack-friendly output."""
        if not self.has_issues:
            return f":white_check_mark: Supports CLEAN — {self.checked_entries} entries checked, all healthy"

        parts = []
        if self.zombies:
            parts.append(f":skull: {len(self.zombies)} zombie")
        if self.weakened:
            parts.append(f":warning: {len(self.weakened)} weakened")
        parts.append(f":white_check_mark: {self.healthy} healthy")

        lines = [" | ".join(parts)]
        lines.append(f"{self.checked_entries} entries checked, {self.total_supports_checked} support refs")

        if self.zombies:
            lines.append("")
            for z in self.zombies[:5]:
                lines.append(f":skull: {z.entry_id} ({z.entry_type}) — {z.content[:60]}")
            if len(self.zombies) > 5:
                lines.append(f"  ...and {len(self.zombies) - 5} more zombies")

        return "\n".join(lines)


def _looks_like_observation(entry_id: str) -> bool:
    """Heuristic: does this ID look like an observation?

    Handles standard (obs-NNN) and legacy prefixed (sc-obs-NNN, ph-obs-NNN) formats.
    Used when the entry doesn't exist in the tree and we need to infer type from ID.
    """
    return bool(re.search(r"obs-\d+", entry_id))


def check_supports(
    tree_path: Optional[str] = None,
    workspace_root: Optional[Path] = None,
) -> SupportsReport:
    """Check the knowledge tree for entries with degraded support structures.

    Args:
        tree_path: Path to KNOWLEDGE_TREE.json (absolute or relative to workspace).
        workspace_root: Workspace root for resolving relative paths.

    Returns:
        SupportsReport with zombie and weakened entries.
    """
    if workspace_root is None:
        try:
            from .config import get_config
            workspace_root = get_config().workspace_root
        except Exception:
            workspace_root = Path.cwd()

    if tree_path is None:
        resolved = workspace_root / DEFAULT_TREE_PATH
    else:
        resolved = Path(tree_path)
        if not resolved.is_absolute():
            resolved = workspace_root / tree_path

    tree_data = json.loads(resolved.read_text())
    nodes = tree_data.get("nodes", {})

    # Index: which entries are invalidated?
    invalidated_ids = set()
    for entry_id, entry in nodes.items():
        if entry.get("status") == "invalidated":
            invalidated_ids.add(entry_id)

    all_node_ids = set(nodes.keys())

    zombies: List[WeakEntry] = []
    weakened: List[WeakEntry] = []
    healthy_count = 0
    no_supports_count = 0
    total_supports_checked = 0
    by_type: Dict[str, Dict[str, int]] = {}
    by_domain: Dict[str, Dict[str, int]] = {}

    # Check every entry that has supports
    for entry_id, entry in nodes.items():
        entry_type = entry.get("type", "")
        if entry_type not in SUPPORTED_TYPES:
            continue
        if entry.get("status") == "invalidated":
            continue  # Skip already-invalidated entries

        supports = entry.get("supports", [])
        if not supports:
            no_supports_count += 1
            continue

        total_supports_checked += len(supports)

        dead_ids = [s for s in supports if s in invalidated_ids]
        missing_ids = [s for s in supports if s not in all_node_ids]
        dead_count = len(dead_ids)

        # For ideas: observations are fleeting — dead obs supports are normal
        # lifecycle, not structural debt. Only count non-observation dead
        # supports for zombie/weakened classification.
        # Missing entries with observation-like IDs (obs-NNN, sc-obs-NNN)
        # are treated as missing observations — also excluded.
        if entry_type == "idea":
            non_obs_dead = [s for s in dead_ids
                           if nodes.get(s, {}).get("type") != "observation"]
            non_obs_missing = [s for s in missing_ids
                               if not _looks_like_observation(s)]
            effective_dead = len(non_obs_dead) + len(non_obs_missing)
            non_obs_supports = [s for s in supports
                                if nodes.get(s, {}).get("type", "observation") != "observation"
                                and s not in missing_ids]
            # Add back non-obs missing (they count as supports we need to track)
            effective_total = len(non_obs_supports) + len(non_obs_missing)
        else:
            effective_dead = dead_count + len(missing_ids)
            effective_total = len(supports)

        domain = entry.get("domain", "unset") or "unset"

        # Initialize type/domain counters
        if entry_type not in by_type:
            by_type[entry_type] = {"checked": 0, "zombie": 0, "weakened": 0, "healthy": 0}
        if domain not in by_domain:
            by_domain[domain] = {"checked": 0, "zombie": 0, "weakened": 0}

        by_type[entry_type]["checked"] += 1
        by_domain[domain]["checked"] += 1

        weak = WeakEntry(
            entry_id=entry_id,
            entry_type=entry_type,
            content=entry.get("content", ""),
            domain=domain,
            total_supports=len(supports),
            dead_supports=dead_count,
            dead_ids=dead_ids,
            missing_ids=missing_ids,
            effective_dead=effective_dead,
            effective_total=effective_total,
        )

        if weak.is_zombie:
            zombies.append(weak)
            by_type[entry_type]["zombie"] += 1
            by_domain[domain]["zombie"] += 1
        elif weak.is_weakened:
            weakened.append(weak)
            by_type[entry_type]["weakened"] += 1
            by_domain[domain]["weakened"] += 1
        else:
            healthy_count += 1
            by_type[entry_type]["healthy"] += 1

    # Sort zombies and weakened by dead ratio descending
    zombies.sort(key=lambda w: (-w.dead_ratio, w.entry_id))
    weakened.sort(key=lambda w: (-w.dead_ratio, w.entry_id))

    checked = sum(t["checked"] for t in by_type.values())

    return SupportsReport(
        tree_path=str(resolved),
        total_entries=len(nodes),
        checked_entries=checked,
        total_supports_checked=total_supports_checked,
        zombies=zombies,
        weakened=weakened,
        healthy=healthy_count,
        no_supports=no_supports_count,
        invalidated_count=len(invalidated_ids),
        by_type=by_type,
        by_domain=by_domain,
    )


def fix_zombies(
    tree_path: Optional[str] = None,
    workspace_root: Optional[Path] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Auto-invalidate zombie entries where ALL supports are dead.

    Args:
        tree_path: Path to KNOWLEDGE_TREE.json.
        workspace_root: Workspace root for resolving relative paths.
        dry_run: If True, report what would be fixed without modifying the tree.

    Returns:
        Dict with 'fixed' (list of invalidated IDs) and 'report' (SupportsReport).
    """
    report = check_supports(tree_path=tree_path, workspace_root=workspace_root)

    if not report.zombies:
        return {"fixed": [], "skipped": [], "report": report}

    if workspace_root is None:
        try:
            from .config import get_config
            workspace_root = get_config().workspace_root
        except Exception:
            workspace_root = Path.cwd()

    resolved = Path(report.tree_path)
    tree_data = json.loads(resolved.read_text())
    nodes = tree_data.get("nodes", {})

    fixed = []
    skipped = []

    for zombie in report.zombies:
        entry = nodes.get(zombie.entry_id)
        if not entry:
            skipped.append(zombie.entry_id)
            continue

        if dry_run:
            fixed.append(zombie.entry_id)
            continue

        # Invalidate the entry
        entry["status"] = "invalidated"
        entry["invalidated_reason"] = (
            f"Auto-invalidated by supports checker: all {zombie.effective_total} "
            f"effective supports are dead or missing "
            f"({zombie.dead_supports} invalidated, {len(zombie.missing_ids)} missing)."
        )
        fixed.append(zombie.entry_id)

    if not dry_run and fixed:
        resolved.write_text(json.dumps(tree_data, indent=2, ensure_ascii=False) + "\n")

    return {"fixed": fixed, "skipped": skipped, "report": report}
