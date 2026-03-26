"""Knowledge tree factual health scanner — perishable fact detection.

Scans the knowledge tree for confabulation-prone entries:

1. **Expired** — observations past their `expires` date
2. **Perishable-without-TTL** — observations containing dates, prices, or
   percentages but no `expires` field (the largest category)
3. **Stale-unverified** — observations with `verified: "unverified"` older
   than a configurable threshold (default 14 days)

This complements `supports.py` (structural integrity — zombie/weakened ideas)
with factual integrity (stale/unverified observations). Together they give
a full picture of tree health.

The pattern detection regex is adapted from `core/agents/dreamer/preflight.py`
(already working and tested) but integrated into the confab framework
architecture for use as a general-purpose tool.

Motivated by the dreamer's observation: 582 perishable observations with no
`expires` field, 7 expired, 4 unverified. The preflight script catches some
of this, but it's dreamer-specific — the confab framework should eat its
own dog food on ia's most important data store.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


# Default path relative to workspace root — overridden by ia_defaults when in ia repo
def _default_tree_path() -> str:
    try:
        from . import _ia_defaults as ia
        return ia.TREE_PATH
    except ImportError:
        return "knowledge_tree.json"

DEFAULT_TREE_PATH = _default_tree_path()

# Default staleness threshold for unverified observations
DEFAULT_STALE_DAYS = 14

# Patterns that indicate time-sensitive content (from preflight.py)
TIME_SENSITIVE_PATTERNS = [
    re.compile(r'\b\d{4}-\d{2}-\d{2}\b'),                                    # ISO dates
    re.compile(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2}'),  # "Feb 26"
    re.compile(r'\$\d+'),                                                      # Dollar amounts
    re.compile(r'\d+\.?\d*%'),                                                 # Percentages
    re.compile(r'\b(?:price|earnings|rate|CPI|GDP|YoY|QoQ|revenue)\b', re.IGNORECASE),
]


@dataclass
class TreeIssue:
    """A single factual health issue in the tree."""
    entry_id: str
    content: str
    category: str       # "expired", "perishable_no_ttl", "stale_unverified"
    domain: Optional[str]
    source: Optional[str]
    expires: Optional[str] = None
    verified: Optional[str] = None
    created: Optional[str] = None
    matched_patterns: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "id": self.entry_id,
            "content": self.content[:200],
            "category": self.category,
            "domain": self.domain,
            "source": self.source,
        }
        if self.expires:
            d["expires"] = self.expires
        if self.verified:
            d["verified"] = self.verified
        if self.created:
            d["created"] = self.created
        if self.matched_patterns:
            d["matched_patterns"] = self.matched_patterns
        return d


@dataclass
class TreeHealthReport:
    """Result of the tree factual health scan."""
    tree_path: str
    scan_date: str
    total_observations: int
    expired: List[TreeIssue]
    perishable_no_ttl: List[TreeIssue]
    stale_unverified: List[TreeIssue]
    ttl_coverage: float        # % of perishable observations that have expires set
    verified_coverage: float   # % of observations with verified != None
    stale_threshold_days: int

    @property
    def total_issues(self) -> int:
        return len(self.expired) + len(self.perishable_no_ttl) + len(self.stale_unverified)

    @property
    def has_issues(self) -> bool:
        return self.total_issues > 0

    @property
    def has_expired(self) -> bool:
        return len(self.expired) > 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tree_path": self.tree_path,
            "scan_date": self.scan_date,
            "total_observations": self.total_observations,
            "expired": len(self.expired),
            "perishable_no_ttl": len(self.perishable_no_ttl),
            "stale_unverified": len(self.stale_unverified),
            "total_issues": self.total_issues,
            "ttl_coverage": round(self.ttl_coverage, 1),
            "verified_coverage": round(self.verified_coverage, 1),
            "stale_threshold_days": self.stale_threshold_days,
            "expired_details": [e.to_dict() for e in self.expired],
            "perishable_no_ttl_details": [p.to_dict() for p in self.perishable_no_ttl[:20]],
            "stale_unverified_details": [s.to_dict() for s in self.stale_unverified],
        }

    def format_report(self) -> str:
        """Human-readable report matching gate/supports output style."""
        lines = []
        lines.append("# Knowledge Tree Factual Health")
        lines.append(f"\nTree: {self.tree_path}")
        lines.append(f"Active observations: {self.total_observations}")
        lines.append(f"TTL coverage: {self.ttl_coverage:.1f}% of perishable observations have `expires`")
        lines.append(f"Verification coverage: {self.verified_coverage:.1f}% of observations have `verified`")

        if not self.has_issues:
            lines.append("\n**TREE HEALTH: CLEAN** -- No expired, unverified, or untagged perishable facts.")
            return "\n".join(lines)

        if self.expired:
            lines.append(f"\n## EXPIRED ({len(self.expired)})")
            lines.append("")
            lines.append("Observations past their `expires` date:")
            lines.append("")
            for e in self.expired:
                lines.append(f"**{e.entry_id}** (expired {e.expires}, {e.domain or 'unset'})")
                lines.append(f"  {e.content[:120]}")
                lines.append(f"  Fix: `python core/knowledge.py invalidate {e.entry_id} --reason \"Expired\"`")
                lines.append("")

        if self.stale_unverified:
            lines.append(f"\n## STALE UNVERIFIED ({len(self.stale_unverified)})")
            lines.append("")
            lines.append(f"Observations marked `unverified` older than {self.stale_threshold_days} days:")
            lines.append("")
            for s in self.stale_unverified:
                lines.append(f"**{s.entry_id}** ({s.domain or 'unset'})")
                lines.append(f"  {s.content[:120]}")
                lines.append(f"  Source: {s.source or 'unknown'} | Created: {s.created or 'unknown'}")
                lines.append(f"  Fix: verify and update, or `python core/knowledge.py invalidate {s.entry_id} --reason \"Stale unverified\"`")
                lines.append("")

        if self.perishable_no_ttl:
            lines.append(f"\n## PERISHABLE WITHOUT TTL ({len(self.perishable_no_ttl)})")
            lines.append("")
            lines.append("Observations containing dates/prices/% but no `expires` field.")
            lines.append("These are confabulation-prone: they'll become stale invisibly.")
            lines.append("")
            shown = self.perishable_no_ttl[:15]
            for p in shown:
                patterns = ", ".join(p.matched_patterns[:3]) if p.matched_patterns else ""
                lines.append(f"**{p.entry_id}** ({p.domain or 'unset'}) [{patterns}]")
                lines.append(f"  {p.content[:120]}")
            if len(self.perishable_no_ttl) > 15:
                lines.append(f"\n  ...and {len(self.perishable_no_ttl) - 15} more")
            lines.append("")
            lines.append("Bulk fix: `python core/knowledge.py verify` to see the full list,")
            lines.append("then add `--expires YYYY-MM-DD` to each perishable observation.")

        # Summary
        lines.append("\n## Summary")
        lines.append(f"- Expired: {len(self.expired)}")
        lines.append(f"- Stale unverified: {len(self.stale_unverified)}")
        lines.append(f"- Perishable without TTL: {len(self.perishable_no_ttl)}")
        lines.append(f"- TTL coverage: {self.ttl_coverage:.1f}%")
        lines.append(f"- Verification coverage: {self.verified_coverage:.1f}%")

        return "\n".join(lines)

    def format_slack(self) -> str:
        """Concise Slack-friendly output."""
        if not self.has_issues:
            return f":white_check_mark: Tree CLEAN -- {self.total_observations} observations, all healthy"

        parts = []
        if self.expired:
            parts.append(f":x: {len(self.expired)} expired")
        if self.stale_unverified:
            parts.append(f":warning: {len(self.stale_unverified)} stale-unverified")
        if self.perishable_no_ttl:
            parts.append(f":hourglass: {len(self.perishable_no_ttl)} no-TTL")

        lines = [" | ".join(parts)]
        lines.append(f"TTL coverage: {self.ttl_coverage:.0f}% | Verified: {self.verified_coverage:.0f}%")

        if self.expired:
            lines.append("")
            for e in self.expired[:5]:
                lines.append(f":x: {e.entry_id} (expired {e.expires}) -- {e.content[:60]}")
            if len(self.expired) > 5:
                lines.append(f"  ...and {len(self.expired) - 5} more expired")

        return "\n".join(lines)

    def format_summary_line(self) -> str:
        """One-line summary for embedding in report dashboards."""
        if not self.has_issues:
            return f"Tree: CLEAN ({self.total_observations} obs, {self.ttl_coverage:.0f}% TTL coverage)"

        parts = []
        if self.expired:
            parts.append(f"{len(self.expired)} expired")
        if self.stale_unverified:
            parts.append(f"{len(self.stale_unverified)} stale-unverified")
        if self.perishable_no_ttl:
            parts.append(f"{len(self.perishable_no_ttl)} no-TTL")

        return f"Tree: {' | '.join(parts)} (TTL coverage: {self.ttl_coverage:.0f}%)"


def _classify_pattern(pattern: re.Pattern) -> str:
    """Return a human-readable label for a time-sensitive pattern."""
    src = pattern.pattern
    if '\\d{4}-\\d{2}-\\d{2}' in src:
        return "date"
    if '(?:Jan|Feb|Mar' in src:
        return "date"
    if '\\$' in src:
        return "price"
    if '%' in src:
        return "percentage"
    if 'price|earnings|rate' in src.lower():
        return "financial-term"
    return "time-sensitive"


def check_tree(
    tree_path: Optional[str] = None,
    workspace_root: Optional[Path] = None,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> TreeHealthReport:
    """Scan the knowledge tree for factual health issues.

    Args:
        tree_path: Path to KNOWLEDGE_TREE.json (absolute or relative to workspace).
        workspace_root: Workspace root for resolving relative paths.
        stale_days: Days after which unverified observations are flagged stale.

    Returns:
        TreeHealthReport with expired, perishable, and stale-unverified entries.
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

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).strftime("%Y-%m-%d")

    expired: List[TreeIssue] = []
    perishable_no_ttl: List[TreeIssue] = []
    stale_unverified: List[TreeIssue] = []

    total_observations = 0
    observations_with_expires = 0
    observations_with_verified = 0
    perishable_with_expires = 0
    perishable_total = 0

    for nid, node in nodes.items():
        if node.get("status") != "active":
            continue
        if node.get("type") != "observation":
            continue

        total_observations += 1
        content = node.get("content", "")
        exp = node.get("expires")
        verified = node.get("verified")
        source = node.get("source")
        domain = node.get("domain")
        created = node.get("created", node.get("timestamp", ""))

        if exp:
            observations_with_expires += 1
        if verified:
            observations_with_verified += 1

        # Check 1: Expired
        if exp and exp <= today:
            expired.append(TreeIssue(
                entry_id=nid,
                content=content[:200],
                category="expired",
                domain=domain,
                source=source,
                expires=exp,
                verified=verified,
                created=created,
            ))
            continue

        # Check 2: Explicitly unverified and old enough to be stale
        if verified == "unverified":
            # Use created/timestamp to determine age
            is_stale = False
            if created and len(created) >= 10:
                created_date = created[:10]
                if created_date <= stale_cutoff:
                    is_stale = True
            else:
                # No created date — conservative: flag it
                is_stale = True

            if is_stale:
                stale_unverified.append(TreeIssue(
                    entry_id=nid,
                    content=content[:200],
                    category="stale_unverified",
                    domain=domain,
                    source=source,
                    verified=verified,
                    created=created,
                ))
            continue

        # Check 3: Time-sensitive content without TTL
        matched = []
        for pattern in TIME_SENSITIVE_PATTERNS:
            if pattern.search(content):
                matched.append(_classify_pattern(pattern))

        if matched:
            perishable_total += 1
            if exp:
                perishable_with_expires += 1
            elif not exp:
                # Deduplicate pattern labels
                unique_patterns = list(dict.fromkeys(matched))
                perishable_no_ttl.append(TreeIssue(
                    entry_id=nid,
                    content=content[:200],
                    category="perishable_no_ttl",
                    domain=domain,
                    source=source,
                    created=created,
                    matched_patterns=unique_patterns,
                ))

    # Sort for consistent output
    expired.sort(key=lambda x: x.expires or "")
    stale_unverified.sort(key=lambda x: x.entry_id)
    perishable_no_ttl.sort(key=lambda x: x.entry_id)

    # Coverage metrics
    ttl_coverage = (perishable_with_expires / perishable_total * 100) if perishable_total > 0 else 100.0
    verified_coverage = (observations_with_verified / total_observations * 100) if total_observations > 0 else 100.0

    try:
        tree_rel = str(resolved.relative_to(workspace_root))
    except ValueError:
        tree_rel = str(resolved)

    return TreeHealthReport(
        tree_path=tree_rel,
        scan_date=today,
        total_observations=total_observations,
        expired=expired,
        perishable_no_ttl=perishable_no_ttl,
        stale_unverified=stale_unverified,
        ttl_coverage=ttl_coverage,
        verified_coverage=verified_coverage,
        stale_threshold_days=stale_days,
    )
