"""Confabulation triage — rank, suggest, batch.

The gate detects issues. The tree scanner finds stale facts. The supports
checker finds zombies. Triage unifies all three into a single ranked view
with actionable remediation commands.

Three operations:
1. **Rank** — Score all issues by severity (impact × urgency × effort-to-fix)
2. **Suggest** — Generate specific verification/fix commands per issue type
3. **Batch** — Group similar issues for bulk remediation

Motivated by the gap between detection (mature) and remediation (missing).
Agents see "14 stale claims" but get no help prioritizing or fixing them.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Severity weights by issue source
SEVERITY_WEIGHTS = {
    # Gate issues — directly affect agent handoffs
    "gate_failed": 10,       # Contradicts reality — highest priority
    "gate_stale": 5,         # Persisting without verification
    "gate_ttl_expired": 4,   # Behavior claim past TTL
    "registry_violation": 3, # File not in system registry
    # Tree issues — affect knowledge quality
    "tree_expired": 8,       # Past expiry date, actively wrong
    "tree_stale_unverified": 3,  # Never verified, aging
    "tree_no_ttl": 1,        # Missing TTL — systemic but low per-item urgency
    # Supports issues — affect knowledge structure
    "supports_zombie": 6,    # All supports dead
    "supports_weakened": 2,  # >50% supports dead
}

# Effort estimates (lower = easier to fix)
EFFORT = {
    "gate_failed": 2,        # Investigate + fix or delete
    "gate_stale": 1,         # Verify or delete
    "gate_ttl_expired": 1,   # Re-verify
    "registry_violation": 2, # Add to registry or investigate
    "tree_expired": 1,       # Invalidate or update expires
    "tree_stale_unverified": 2,  # Need web search or file check
    "tree_no_ttl": 1,        # Add --expires flag (bulk-able)
    "supports_zombie": 2,    # Review + invalidate or re-link
    "supports_weakened": 2,  # Review remaining supports
}


@dataclass
class TriageItem:
    """A single triaged issue with severity score and suggested fix."""
    source: str              # "gate", "tree", "supports"
    category: str            # e.g. "gate_failed", "tree_expired"
    severity: float          # Computed score (higher = fix first)
    entry_id: str            # Claim hash, obs-NNN, or idea-NNN
    summary: str             # One-line description
    detail: str              # Full text (truncated)
    suggested_cmd: str       # Exact command to fix
    effort: int              # 1=easy, 2=moderate, 3=hard
    source_file: Optional[str] = None
    domain: Optional[str] = None
    run_count: int = 0       # For gate claims: how many gate runs

    @property
    def priority_score(self) -> float:
        """Higher = fix first. Severity / effort = best bang for buck."""
        return self.severity / max(self.effort, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "category": self.category,
            "severity": self.severity,
            "priority_score": round(self.priority_score, 2),
            "entry_id": self.entry_id,
            "summary": self.summary,
            "detail": self.detail[:200],
            "suggested_cmd": self.suggested_cmd,
            "effort": self.effort,
            "source_file": self.source_file,
            "domain": self.domain,
            "run_count": self.run_count,
        }


@dataclass
class BatchGroup:
    """A group of similar issues that can be fixed together."""
    category: str
    count: int
    items: List[TriageItem]
    batch_cmd: str           # Command that fixes all items in this group
    total_severity: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category,
            "count": self.count,
            "total_severity": round(self.total_severity, 1),
            "batch_cmd": self.batch_cmd,
            "item_ids": [i.entry_id for i in self.items],
        }


@dataclass
class TriageReport:
    """Complete triage report with ranked items and batch groups."""
    timestamp: str
    total_issues: int
    items: List[TriageItem]           # Sorted by priority_score desc
    batches: List[BatchGroup]         # Grouped for bulk operations
    summary: Dict[str, int]           # category -> count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "total_issues": self.total_issues,
            "items": [i.to_dict() for i in self.items],
            "batches": [b.to_dict() for b in self.batches],
            "summary": self.summary,
        }

    def format_report(self, limit: int = 20) -> str:
        """Human-readable triage report."""
        lines = []
        lines.append("# Confabulation Triage Report")
        lines.append(f"\nTotal issues: {self.total_issues}")
        lines.append("")

        # Summary by category
        lines.append("## Summary")
        for cat, count in sorted(self.summary.items(), key=lambda x: -SEVERITY_WEIGHTS.get(x[0], 0)):
            sev = SEVERITY_WEIGHTS.get(cat, 0)
            lines.append(f"  {cat}: {count} (severity weight: {sev})")
        lines.append("")

        # Top items by priority
        if self.items:
            shown = self.items[:limit]
            lines.append(f"## Top {len(shown)} Issues (by priority score)")
            lines.append("")
            for i, item in enumerate(shown, 1):
                icon = _severity_icon(item.category)
                lines.append(f"{i}. {icon} [{item.category}] {item.summary}")
                if item.run_count > 1:
                    lines.append(f"   {item.run_count} gate runs | severity {item.severity:.0f} | effort {item.effort}")
                else:
                    lines.append(f"   severity {item.severity:.0f} | effort {item.effort}")
                lines.append(f"   Fix: {item.suggested_cmd}")
                lines.append("")

        # Batch operations
        if self.batches:
            lines.append("## Batch Operations")
            lines.append("")
            for batch in self.batches:
                lines.append(f"**{batch.category}** ({batch.count} items, total severity {batch.total_severity:.0f})")
                lines.append(f"  {batch.batch_cmd}")
                lines.append("")

        return "\n".join(lines)

    def format_slack(self) -> str:
        """Concise Slack-friendly output."""
        if self.total_issues == 0:
            return ":white_check_mark: Triage CLEAN — no issues found"

        parts = []
        for cat, count in sorted(self.summary.items(), key=lambda x: -SEVERITY_WEIGHTS.get(x[0], 0)):
            if count > 0:
                icon = _severity_icon(cat)
                parts.append(f"{icon} {count} {cat.replace('_', ' ')}")

        lines = [" | ".join(parts)]
        # Top 3 actions
        for item in self.items[:3]:
            lines.append(f"  `{item.suggested_cmd}`")

        return "\n".join(lines)


def _severity_icon(category: str) -> str:
    icons = {
        "gate_failed": "🔴",
        "gate_stale": "🟡",
        "gate_ttl_expired": "🟠",
        "registry_violation": "⚠️",
        "tree_expired": "🔴",
        "tree_stale_unverified": "🟡",
        "tree_no_ttl": "⚪",
        "supports_zombie": "💀",
        "supports_weakened": "🟡",
    }
    return icons.get(category, "•")


def _suggest_gate_fix(category: str, detail: Dict[str, Any]) -> str:
    """Generate fix command for a gate issue."""
    claim_text = detail.get("claim_text", "")[:60]
    source = detail.get("source_file", "")

    if category == "gate_failed":
        return f'python core/confab/cli.py fix --file "{source}"'
    elif category == "gate_stale":
        return f'python core/confab/cli.py fix --file "{source}"'
    elif category == "gate_ttl_expired":
        return f"# Re-verify: check the claim's current state, update [v1: ...] tag"
    elif category == "registry_violation":
        return "# Add file to core/SYSTEM_REGISTRY.md or investigate"
    return "# Manual review needed"


def _suggest_tree_fix(category: str, issue: Any) -> str:
    """Generate fix command for a tree issue."""
    eid = issue.entry_id if hasattr(issue, 'entry_id') else issue.get("id", "???")

    if category == "tree_expired":
        return f'python core/knowledge.py invalidate {eid} --reason "Expired"'
    elif category == "tree_stale_unverified":
        return f'python core/knowledge.py invalidate {eid} --reason "Stale unverified"'
    elif category == "tree_no_ttl":
        return f'python core/knowledge.py set-field {eid} expires YYYY-MM-DD'
    return f"# Review {eid}"


def _suggest_supports_fix(entry: Any) -> str:
    """Generate fix command for a supports issue."""
    eid = entry.entry_id if hasattr(entry, 'entry_id') else entry.get("id", "???")
    if hasattr(entry, 'is_zombie') and entry.is_zombie:
        return f'python core/knowledge.py invalidate {eid} --reason "All supports dead"'
    return f"# Review supports for {eid} — >50% dead"


def run_triage(
    gate_report=None,
    tree_report=None,
    supports_report=None,
    limit: int = 50,
) -> TriageReport:
    """Run triage across all confab data sources.

    Pass in existing reports or None to skip that source.
    For a full triage, run gate, tree, and supports first.
    """
    items: List[TriageItem] = []
    summary: Dict[str, int] = {}
    now = datetime.now(timezone.utc).isoformat()

    # --- Gate issues ---
    if gate_report:
        # Failed claims
        for d in gate_report.failed_details:
            cat = "gate_failed"
            summary[cat] = summary.get(cat, 0) + 1
            rc = d.get("tracker_run_count", 1)
            sev = SEVERITY_WEIGHTS[cat] * max(1, rc)
            items.append(TriageItem(
                source="gate",
                category=cat,
                severity=sev,
                entry_id=d.get("claim_hash", d.get("claim_text", "?")[:16]),
                summary=d.get("claim_text", "")[:100],
                detail=d.get("evidence", ""),
                suggested_cmd=_suggest_gate_fix(cat, d),
                effort=EFFORT[cat],
                source_file=d.get("source_file"),
                run_count=rc,
            ))

        # Stale claims
        for d in gate_report.stale_details:
            cat = "gate_stale"
            summary[cat] = summary.get(cat, 0) + 1
            rc = d.get("tracker_run_count", d.get("age_builds", 3))
            sev = SEVERITY_WEIGHTS[cat] * max(1, rc // 3)
            items.append(TriageItem(
                source="gate",
                category=cat,
                severity=sev,
                entry_id=d.get("claim_hash", d.get("claim_text", "?")[:16]),
                summary=d.get("claim_text", "")[:100],
                detail=d.get("claim_text", ""),
                suggested_cmd=_suggest_gate_fix(cat, d),
                effort=EFFORT[cat],
                source_file=d.get("source_file"),
                run_count=rc,
            ))

        # TTL-expired behavior claims
        for d in gate_report.ttl_expired:
            cat = "gate_ttl_expired"
            summary[cat] = summary.get(cat, 0) + 1
            items.append(TriageItem(
                source="gate",
                category=cat,
                severity=SEVERITY_WEIGHTS[cat],
                entry_id=d.get("claim_hash", "?"),
                summary=d.get("claim_text", "")[:100],
                detail=d.get("claim_text", ""),
                suggested_cmd=_suggest_gate_fix(cat, d),
                effort=EFFORT[cat],
                source_file=d.get("source_file"),
            ))

        # Registry violations
        for d in gate_report.registry_violations:
            cat = "registry_violation"
            summary[cat] = summary.get(cat, 0) + 1
            items.append(TriageItem(
                source="gate",
                category=cat,
                severity=SEVERITY_WEIGHTS[cat],
                entry_id=d.get("path", "?"),
                summary=f"File not in SYSTEM_REGISTRY: {d.get('path', '?')}",
                detail=str(d),
                suggested_cmd=_suggest_gate_fix(cat, d),
                effort=EFFORT[cat],
            ))

    # --- Tree issues ---
    if tree_report:
        for issue in tree_report.expired:
            cat = "tree_expired"
            summary[cat] = summary.get(cat, 0) + 1
            items.append(TriageItem(
                source="tree",
                category=cat,
                severity=SEVERITY_WEIGHTS[cat],
                entry_id=issue.entry_id,
                summary=f"{issue.entry_id}: {issue.content[:80]}",
                detail=issue.content,
                suggested_cmd=_suggest_tree_fix(cat, issue),
                effort=EFFORT[cat],
                domain=issue.domain,
            ))

        for issue in tree_report.stale_unverified:
            cat = "tree_stale_unverified"
            summary[cat] = summary.get(cat, 0) + 1
            items.append(TriageItem(
                source="tree",
                category=cat,
                severity=SEVERITY_WEIGHTS[cat],
                entry_id=issue.entry_id,
                summary=f"{issue.entry_id}: {issue.content[:80]}",
                detail=issue.content,
                suggested_cmd=_suggest_tree_fix(cat, issue),
                effort=EFFORT[cat],
                domain=issue.domain,
            ))

        # No-TTL: only include a sample (there can be hundreds)
        no_ttl_sample = tree_report.perishable_no_ttl[:limit]
        for issue in no_ttl_sample:
            cat = "tree_no_ttl"
            summary[cat] = summary.get(cat, 0) + 1
            items.append(TriageItem(
                source="tree",
                category=cat,
                severity=SEVERITY_WEIGHTS[cat],
                entry_id=issue.entry_id,
                summary=f"{issue.entry_id}: {issue.content[:80]}",
                detail=issue.content,
                suggested_cmd=_suggest_tree_fix(cat, issue),
                effort=EFFORT[cat],
                domain=issue.domain,
            ))
        # Record the full count even though we limited the items
        remaining = len(tree_report.perishable_no_ttl) - len(no_ttl_sample)
        if remaining > 0:
            summary["tree_no_ttl"] = summary.get("tree_no_ttl", 0) + remaining

    # --- Supports issues ---
    if supports_report:
        for entry in supports_report.zombies:
            cat = "supports_zombie"
            summary[cat] = summary.get(cat, 0) + 1
            items.append(TriageItem(
                source="supports",
                category=cat,
                severity=SEVERITY_WEIGHTS[cat],
                entry_id=entry.entry_id,
                summary=f"{entry.entry_id} ({entry.entry_type}): {entry.content[:60]}",
                detail=entry.content,
                suggested_cmd=_suggest_supports_fix(entry),
                effort=EFFORT[cat],
                domain=entry.domain,
            ))

        for entry in supports_report.weakened:
            cat = "supports_weakened"
            summary[cat] = summary.get(cat, 0) + 1
            items.append(TriageItem(
                source="supports",
                category=cat,
                severity=SEVERITY_WEIGHTS[cat],
                entry_id=entry.entry_id,
                summary=f"{entry.entry_id} ({entry.entry_type}): {entry.content[:60]}",
                detail=entry.content,
                suggested_cmd=_suggest_supports_fix(entry),
                effort=EFFORT[cat],
                domain=entry.domain,
            ))

    # Sort by priority score (severity / effort), highest first
    items.sort(key=lambda x: -x.priority_score)

    # Build batch groups
    batches = _build_batches(items)

    return TriageReport(
        timestamp=now,
        total_issues=sum(summary.values()),
        items=items[:limit],
        batches=batches,
        summary=summary,
    )


def _build_batches(items: List[TriageItem]) -> List[BatchGroup]:
    """Group items by category for bulk operations."""
    by_cat: Dict[str, List[TriageItem]] = {}
    for item in items:
        by_cat.setdefault(item.category, []).append(item)

    batches = []
    for cat, cat_items in sorted(by_cat.items(), key=lambda x: -SEVERITY_WEIGHTS.get(x[0], 0)):
        if len(cat_items) < 2:
            continue  # No point batching a single item

        batch_cmd = _batch_command(cat, cat_items)
        total_sev = sum(i.severity for i in cat_items)

        batches.append(BatchGroup(
            category=cat,
            count=len(cat_items),
            items=cat_items,
            batch_cmd=batch_cmd,
            total_severity=total_sev,
        ))

    return batches


def _batch_command(category: str, items: List[TriageItem]) -> str:
    """Generate a batch command for a group of similar issues."""
    ids = [i.entry_id for i in items]

    if category == "tree_expired":
        id_list = " ".join(ids[:20])
        return f"for id in {id_list}; do python core/knowledge.py invalidate $id --reason 'Expired'; done"

    elif category == "tree_stale_unverified":
        id_list = " ".join(ids[:20])
        return f"for id in {id_list}; do python core/knowledge.py invalidate $id --reason 'Stale unverified'; done"

    elif category == "tree_no_ttl":
        # Group by domain for more targeted batch
        domains = {}
        for item in items:
            d = item.domain or "unset"
            domains.setdefault(d, []).append(item.entry_id)
        top_domain = max(domains, key=lambda d: len(domains[d]))
        count = len(domains[top_domain])
        return (f"# {len(items)} obs without TTL ({count} in '{top_domain}' domain). "
                f"Use: python core/knowledge.py set-field OBS_ID expires YYYY-MM-DD")

    elif category == "gate_stale":
        return f'python core/confab/cli.py fix  # auto-fix {len(items)} stale claims'

    elif category == "gate_failed":
        return f'python core/confab/cli.py fix  # investigate {len(items)} failed claims'

    elif category == "supports_zombie":
        id_list = " ".join(ids[:20])
        return f"for id in {id_list}; do python core/knowledge.py invalidate $id --reason 'All supports dead'; done"

    elif category == "supports_weakened":
        return f"# Review {len(items)} weakened entries — check remaining supports"

    return f"# {len(items)} {category} issues — manual review needed"
