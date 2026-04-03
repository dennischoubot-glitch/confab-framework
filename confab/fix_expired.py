"""Fix expired knowledge tree observations — batch invalidation with Firewall check.

Scans the knowledge tree for active observations past their `expires` date
and invalidates them in batch. After invalidation, checks whether any
idea/principle/truth lost ALL its active supports (the Firewall check
from idea-489) and reports newly-unsupported entries.

This automates what dreamers do manually every sprint: run `knowledge.py verify`,
see 13 expired observations, invalidate a few if time allows. Now it's one command.

Usage:
    confab fix-expired              # invalidate all expired observations
    confab fix-expired --dry-run    # preview without modifying the tree
    confab fix-expired --json       # JSON output
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _default_tree_path() -> str:
    from .config import load_ia_defaults_module
    ia = load_ia_defaults_module()
    if ia is not None and hasattr(ia, "TREE_PATH"):
        return ia.TREE_PATH
    return "knowledge_tree.json"


@dataclass
class ExpiredEntry:
    """An observation that has expired."""
    entry_id: str
    content: str
    expires: str
    domain: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.entry_id,
            "content": self.content[:200],
            "expires": self.expires,
            "domain": self.domain,
        }


@dataclass
class UnsupportedEntry:
    """An idea/principle/truth that lost all active supports after invalidation."""
    entry_id: str
    entry_type: str
    content: str
    dead_supports: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.entry_id,
            "type": self.entry_type,
            "content": self.content[:200],
            "dead_supports": self.dead_supports,
        }


@dataclass
class FixExpiredResult:
    """Result of the fix-expired operation."""
    tree_path: str
    scan_date: str
    dry_run: bool
    expired_found: List[ExpiredEntry]
    newly_unsupported: List[UnsupportedEntry]

    @property
    def expired_count(self) -> int:
        return len(self.expired_found)

    @property
    def unsupported_count(self) -> int:
        return len(self.newly_unsupported)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tree_path": self.tree_path,
            "scan_date": self.scan_date,
            "dry_run": self.dry_run,
            "expired_count": self.expired_count,
            "unsupported_count": self.unsupported_count,
            "expired": [e.to_dict() for e in self.expired_found],
            "newly_unsupported": [u.to_dict() for u in self.newly_unsupported],
        }

    def format_report(self) -> str:
        lines = []
        mode = "DRY RUN" if self.dry_run else "APPLIED"
        lines.append(f"# Fix Expired — {mode}")
        lines.append(f"Scanned: {self.tree_path}")
        lines.append(f"Date: {self.scan_date}\n")

        if not self.expired_found:
            lines.append("No expired observations found.")
            return "\n".join(lines)

        action = "Would invalidate" if self.dry_run else "Invalidated"
        lines.append(f"## {action} {self.expired_count} expired observation(s)\n")
        for e in self.expired_found:
            lines.append(f"  {e.entry_id} (expired {e.expires})")
            lines.append(f"    {e.content[:120]}\n")

        if self.newly_unsupported:
            lines.append(f"## {self.unsupported_count} newly-unsupported entry/entries (Firewall check)\n")
            lines.append("These entries now have NO active supports. Review for invalidation.\n")
            for u in self.newly_unsupported:
                lines.append(f"  {u.entry_id} ({u.entry_type})")
                lines.append(f"    {u.content[:120]}")
                lines.append(f"    Dead supports: {', '.join(u.dead_supports)}\n")
        else:
            lines.append("\nNo entries lost all supports.")

        lines.append(f"\nSummary: {self.expired_count} expired invalidated, "
                      f"{self.unsupported_count} newly-unsupported flagged")
        return "\n".join(lines)

    def format_slack(self) -> str:
        if not self.expired_found:
            return "fix-expired: 0 expired observations found"
        mode = "DRY RUN" if self.dry_run else "APPLIED"
        parts = [f"fix-expired ({mode}): {self.expired_count} expired invalidated"]
        if self.newly_unsupported:
            ids = ", ".join(u.entry_id for u in self.newly_unsupported)
            parts.append(f"{self.unsupported_count} newly-unsupported: {ids}")
        return " | ".join(parts)


def fix_expired(
    tree_path: Optional[str] = None,
    dry_run: bool = False,
) -> FixExpiredResult:
    """Find and invalidate expired observations, then check for Firewall violations.

    Args:
        tree_path: Path to KNOWLEDGE_TREE.json. None = auto-detect.
        dry_run: If True, report what would happen without modifying the tree.

    Returns:
        FixExpiredResult with all findings.
    """
    # Resolve tree path
    if tree_path:
        path = Path(tree_path)
    else:
        path = Path(_default_tree_path())

    if not path.exists():
        return FixExpiredResult(
            tree_path=str(path),
            scan_date=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            dry_run=dry_run,
            expired_found=[],
            newly_unsupported=[],
        )

    with open(path) as f:
        tree = json.load(f)

    nodes = tree.get("nodes", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Step 1: Find expired active observations
    expired_entries = []
    for nid, node in nodes.items():
        if node.get("status") != "active":
            continue
        if node.get("type") != "observation":
            continue
        exp = node.get("expires")
        if exp and exp <= today:
            expired_entries.append(ExpiredEntry(
                entry_id=nid,
                content=node.get("content", ""),
                expires=exp,
                domain=node.get("domain"),
            ))

    # Sort by expiry date (oldest first)
    expired_entries.sort(key=lambda e: e.expires)

    # Step 2: Invalidate (if not dry run)
    invalidated_ids = set()
    if not dry_run and expired_entries:
        for entry in expired_entries:
            nid = entry.entry_id
            node = nodes[nid]
            node["status"] = "invalidated"
            node["invalidated_reason"] = f"TTL expired [{entry.expires}], not re-verified"
            node["invalidated_at"] = datetime.now(timezone.utc).isoformat()
            invalidated_ids.add(nid)

    # For dry-run, simulate which IDs would be invalidated
    if dry_run:
        invalidated_ids = {e.entry_id for e in expired_entries}

    # Step 3: Firewall check — find entries that lost ALL active supports
    # Check ideas, principles, truths whose supports include any newly-invalidated entry
    newly_unsupported = []
    if invalidated_ids:
        for nid, node in nodes.items():
            if node.get("status") != "active":
                continue
            if node.get("type") not in ("idea", "principle", "truth"):
                continue

            supports = node.get("supports", [])
            if not supports:
                continue

            # Check if any support was just invalidated
            affected = any(s in invalidated_ids for s in supports)
            if not affected:
                continue

            # Count active supports (considering our new invalidations)
            active_supports = []
            dead_supports = []
            for s in supports:
                s_node = nodes.get(s)
                if s_node is None:
                    dead_supports.append(s)
                    continue
                # In dry-run mode, treat would-be-invalidated as dead
                if s in invalidated_ids:
                    dead_supports.append(s)
                elif s_node.get("status") != "active":
                    dead_supports.append(s)
                else:
                    active_supports.append(s)

            if not active_supports and dead_supports:
                newly_unsupported.append(UnsupportedEntry(
                    entry_id=nid,
                    entry_type=node.get("type", "unknown"),
                    content=node.get("content", ""),
                    dead_supports=dead_supports,
                ))

    newly_unsupported.sort(key=lambda u: u.entry_id)

    # Step 4: Save tree (if not dry run and we made changes)
    if not dry_run and expired_entries:
        # Atomic save with backup (same pattern as knowledge.py)
        import tempfile
        import shutil
        backup = path.with_suffix(".json.bak")
        if path.exists():
            shutil.copy2(path, backup)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".json.tmp")
        try:
            import os
            with os.fdopen(fd, "w") as f:
                json.dump(tree, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(path))
        except Exception:
            import os
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    return FixExpiredResult(
        tree_path=str(path),
        scan_date=now_str,
        dry_run=dry_run,
        expired_found=expired_entries,
        newly_unsupported=newly_unsupported,
    )
