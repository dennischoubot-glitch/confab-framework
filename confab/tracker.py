"""Staleness tracker — persistent claim tracking across gate runs.

The 16-build false blocker cascade (obs-3528) happened because claims
persisted without verification and no system tracked HOW LONG they'd
persisted. This module adds that memory.

Each gate run:
1. Extracts claims → hashes them for dedup
2. Looks up each claim in the tracker DB
3. New claims: inserted with run_count=1
4. Existing claims: run_count incremented
5. Verified claims: status updated with evidence
6. Claims at run_count >= threshold without verification → STALE

The tracker DB is the persistence layer that makes obs-3530
(two-agent verification) enforceable by code.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .claims import Claim
from .verify import VerificationResult

# Claims unseen for this many gate runs get auto-expired
EXPIRY_RUNS = 10

# Default staleness threshold
DEFAULT_STALE_THRESHOLD = 3


class TrackingStatus(Enum):
    """Verification status in the tracker."""
    NEW = "new"                  # Just seen for the first time
    UNVERIFIED = "unverified"    # Seen multiple times, never verified
    VERIFIED = "verified"        # Passed verification
    FAILED = "failed"            # Failed verification
    INCONCLUSIVE = "inconclusive"  # Couldn't determine
    STALE = "stale"              # Exceeded run threshold without verification
    EXPIRED = "expired"          # No longer appears in scanned files


@dataclass
class TrackedClaim:
    """A claim with persistence metadata."""
    claim_hash: str
    claim_text: str
    claim_type: str
    source_file: Optional[str]
    first_seen: str           # ISO timestamp
    last_seen: str            # ISO timestamp
    last_verified: Optional[str]  # ISO timestamp of last verification
    run_count: int            # How many gate runs this has appeared in
    status: str               # TrackingStatus value
    evidence: Optional[str]   # Last verification evidence
    verification_method: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hash": self.claim_hash,
            "text": self.claim_text,
            "type": self.claim_type,
            "source": self.source_file,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "last_verified": self.last_verified,
            "run_count": self.run_count,
            "status": self.status,
            "evidence": self.evidence,
        }


def _has_inline_verification(claim: Claim) -> bool:
    """Check if a claim carries an inline verification tag indicating it's been verified.

    Claims like 'Notes queue: healthy [v1: verified 2026-03-19]' carry manual
    verification tags that the auto-verifier can't check. The tracker should
    treat these as verified so they don't get flagged STALE.
    """
    tag = claim.verification_tag
    if not tag:
        return False
    tag_lower = tag.lower()
    # [unverified] and [FAILED: ...] are NOT positive verification
    if 'unverified' in tag_lower or 'failed' in tag_lower:
        return False
    # [v1: ...], [v2: ...], [verified ...] are positive verification
    return True


def _hash_claim(text: str) -> str:
    """Hash normalized claim text for deduplication.

    Normalizes whitespace and case so minor reformatting
    doesn't create duplicate entries.
    """
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _default_db_path() -> Path:
    """Get default DB path from config."""
    from .config import get_config
    return get_config().db_path


def _get_db(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get database connection, creating tables if needed."""
    path = db_path or _default_db_path()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_claims (
            claim_hash TEXT PRIMARY KEY,
            claim_text TEXT NOT NULL,
            claim_type TEXT NOT NULL,
            source_file TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            last_verified TEXT,
            run_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'new',
            evidence TEXT,
            verification_method TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            files_scanned TEXT,
            total_claims INTEGER,
            passed INTEGER,
            failed INTEGER,
            stale INTEGER,
            new_claims INTEGER,
            returning_claims INTEGER
        )
    """)
    # Cascade history — per-run claim appearances for lineage tracing.
    # Each row records that a specific claim appeared in a specific gate run
    # with a specific status. This is the data needed to measure cascade depth
    # (how many runs a false claim survived before being caught).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cascade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_hash TEXT NOT NULL,
            gate_run_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            source_file TEXT,
            FOREIGN KEY (claim_hash) REFERENCES tracked_claims(claim_hash),
            FOREIGN KEY (gate_run_id) REFERENCES gate_runs(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cascade_claim
        ON cascade_history(claim_hash)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cascade_run
        ON cascade_history(gate_run_id)
    """)
    conn.commit()
    return conn


def record_gate_run(
    claims: List[Claim],
    verification_results: Dict[str, "VerificationResult"],
    files_scanned: List[str],
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record a gate run, updating all claim tracking.

    Args:
        claims: Claims extracted from this gate run.
        verification_results: Map of claim_hash → VerificationResult.
        files_scanned: Files that were scanned.
        db_path: Optional override for DB path (for testing).

    Returns:
        Summary dict with new_claims, returning_claims, stale_claims counts.
    """
    conn = _get_db(db_path)
    now = datetime.now(timezone.utc).isoformat()

    new_count = 0
    returning_count = 0
    stale_count = 0
    passed_count = 0
    failed_count = 0

    seen_hashes = set()

    for claim in claims:
        h = _hash_claim(claim.text)
        seen_hashes.add(h)

        # Look up existing record
        row = conn.execute(
            "SELECT * FROM tracked_claims WHERE claim_hash = ?", (h,)
        ).fetchone()

        # Determine verification status
        vr = verification_results.get(h)
        if vr == VerificationResult.PASSED:
            status = TrackingStatus.VERIFIED.value
            passed_count += 1
        elif vr == VerificationResult.FAILED:
            status = TrackingStatus.FAILED.value
            failed_count += 1
        elif vr == VerificationResult.INCONCLUSIVE:
            status = TrackingStatus.INCONCLUSIVE.value
        else:
            status = TrackingStatus.UNVERIFIED.value

        # Check for inline verification tags — claims with [v1: ...],
        # [v2: ...], or [verified ...] tags should be treated as verified
        # even if the auto-verifier returned inconclusive/skipped.
        # Without this, manually-verified claims get flagged STALE.
        if status in (TrackingStatus.UNVERIFIED.value,
                      TrackingStatus.INCONCLUSIVE.value):
            if _has_inline_verification(claim):
                status = TrackingStatus.VERIFIED.value

        if row is None:
            # New claim
            conn.execute("""
                INSERT INTO tracked_claims
                    (claim_hash, claim_text, claim_type, source_file,
                     first_seen, last_seen, run_count, status,
                     last_verified, evidence, verification_method)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL, NULL)
            """, (
                h, claim.text[:500], claim.claim_type.value,
                claim.source_file, now, now, status,
                now if status == TrackingStatus.VERIFIED.value else None,
            ))
            new_count += 1
        else:
            # Existing claim — increment run_count
            new_run_count = row["run_count"] + 1

            # Update verification timestamp if verified
            last_verified = row["last_verified"]
            if status == TrackingStatus.VERIFIED.value:
                last_verified = now

            # Check if stale: high run count + never verified
            if (new_run_count >= DEFAULT_STALE_THRESHOLD
                    and status in (TrackingStatus.UNVERIFIED.value,
                                   TrackingStatus.INCONCLUSIVE.value)
                    and row["last_verified"] is None):
                status = TrackingStatus.STALE.value
                stale_count += 1

            conn.execute("""
                UPDATE tracked_claims
                SET last_seen = ?, run_count = ?, status = ?,
                    last_verified = ?, source_file = ?
                WHERE claim_hash = ?
            """, (now, new_run_count, status, last_verified,
                  claim.source_file, h))
            returning_count += 1

    # Record the gate run
    cursor = conn.execute("""
        INSERT INTO gate_runs
            (timestamp, files_scanned, total_claims, passed, failed,
             stale, new_claims, returning_claims)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now, json.dumps(files_scanned), len(claims),
        passed_count, failed_count, stale_count,
        new_count, returning_count,
    ))
    gate_run_id = cursor.lastrowid

    # Record cascade history — one row per claim per run
    for claim in claims:
        h = _hash_claim(claim.text)
        # Look up the current status for this claim
        row = conn.execute(
            "SELECT status FROM tracked_claims WHERE claim_hash = ?", (h,)
        ).fetchone()
        claim_status = row["status"] if row else "unknown"
        conn.execute("""
            INSERT INTO cascade_history
                (claim_hash, gate_run_id, status, source_file)
            VALUES (?, ?, ?, ?)
        """, (h, gate_run_id, claim_status, claim.source_file))

    conn.commit()
    conn.close()

    return {
        "new_claims": new_count,
        "returning_claims": returning_count,
        "stale_claims": stale_count,
        "passed": passed_count,
        "failed": failed_count,
    }


def get_stale_claims(
    threshold: int = DEFAULT_STALE_THRESHOLD,
    db_path: Optional[Path] = None,
) -> List[TrackedClaim]:
    """Get all claims that have exceeded the staleness threshold."""
    conn = _get_db(db_path)
    rows = conn.execute("""
        SELECT * FROM tracked_claims
        WHERE run_count >= ?
          AND (status IN ('unverified', 'inconclusive', 'stale'))
          AND last_verified IS NULL
        ORDER BY run_count DESC
    """, (threshold,)).fetchall()
    conn.close()
    return [_row_to_tracked(r) for r in rows]


def get_all_tracked(
    db_path: Optional[Path] = None,
) -> List[TrackedClaim]:
    """Get all tracked claims, ordered by staleness (most stale first)."""
    conn = _get_db(db_path)
    rows = conn.execute("""
        SELECT * FROM tracked_claims
        ORDER BY
            CASE status
                WHEN 'stale' THEN 0
                WHEN 'failed' THEN 1
                WHEN 'unverified' THEN 2
                WHEN 'inconclusive' THEN 3
                WHEN 'new' THEN 4
                WHEN 'verified' THEN 5
                WHEN 'expired' THEN 6
            END,
            run_count DESC
    """).fetchall()
    conn.close()
    return [_row_to_tracked(r) for r in rows]


def get_run_history(
    limit: int = 10,
    db_path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Get recent gate run history."""
    conn = _get_db(db_path)
    rows = conn.execute("""
        SELECT * FROM gate_runs
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_claims(
    claim_hashes: List[str],
    db_path: Optional[Path] = None,
) -> int:
    """Remove specific claims from tracking. Returns count removed."""
    conn = _get_db(db_path)
    removed = 0
    for h in claim_hashes:
        cur = conn.execute(
            "DELETE FROM tracked_claims WHERE claim_hash = ?", (h,)
        )
        removed += cur.rowcount
    conn.commit()
    conn.close()
    return removed


def remove_stale(
    threshold: int = DEFAULT_STALE_THRESHOLD,
    db_path: Optional[Path] = None,
) -> int:
    """Remove all stale claims. Returns count removed."""
    stale = get_stale_claims(threshold, db_path)
    if not stale:
        return 0
    return remove_claims([c.claim_hash for c in stale], db_path)


def get_stats(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """Get tracker statistics."""
    conn = _get_db(db_path)

    total = conn.execute("SELECT COUNT(*) FROM tracked_claims").fetchone()[0]
    by_status = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) as cnt FROM tracked_claims GROUP BY status"
    ).fetchall():
        by_status[row["status"]] = row["cnt"]

    run_count = conn.execute("SELECT COUNT(*) FROM gate_runs").fetchone()[0]

    latest_run = conn.execute(
        "SELECT timestamp FROM gate_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()

    conn.close()

    return {
        "total_tracked": total,
        "by_status": by_status,
        "total_gate_runs": run_count,
        "latest_run": latest_run["timestamp"] if latest_run else None,
    }


@dataclass
class CascadeEntry:
    """A single point in a claim's cascade history."""
    gate_run_id: int
    timestamp: str
    status: str
    source_file: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.gate_run_id,
            "timestamp": self.timestamp,
            "status": self.status,
            "source": self.source_file,
        }


def get_cascade_history(
    claim_hash: str,
    db_path: Optional[Path] = None,
) -> List[CascadeEntry]:
    """Get the full cascade history for a claim — every gate run it appeared in."""
    conn = _get_db(db_path)
    rows = conn.execute("""
        SELECT ch.gate_run_id, gr.timestamp, ch.status, ch.source_file
        FROM cascade_history ch
        JOIN gate_runs gr ON ch.gate_run_id = gr.id
        WHERE ch.claim_hash = ?
        ORDER BY gr.id ASC
    """, (claim_hash,)).fetchall()
    conn.close()
    return [
        CascadeEntry(
            gate_run_id=r["gate_run_id"],
            timestamp=r["timestamp"],
            status=r["status"],
            source_file=r["source_file"],
        )
        for r in rows
    ]


def get_cascade_stats(
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compute cascade depth statistics across all tracked claims.

    Returns:
        Dict with avg_depth, max_depth, total_cascaded, resolved_count,
        avg_time_to_resolution, and top_cascaders.
    """
    conn = _get_db(db_path)

    # Get cascade depth (number of runs) per claim
    rows = conn.execute("""
        SELECT
            ch.claim_hash,
            tc.claim_text,
            tc.status,
            tc.first_seen,
            tc.last_seen,
            COUNT(ch.id) as depth
        FROM cascade_history ch
        JOIN tracked_claims tc ON ch.claim_hash = tc.claim_hash
        GROUP BY ch.claim_hash
        ORDER BY depth DESC
    """).fetchall()

    if not rows:
        conn.close()
        return {
            "avg_depth": 0.0,
            "max_depth": 0,
            "total_cascaded": 0,
            "resolved_count": 0,
            "total_tracked": 0,
            "top_cascaders": [],
        }

    depths = [r["depth"] for r in rows]
    avg_depth = sum(depths) / len(depths)
    max_depth = max(depths)

    # Claims that cascaded (appeared 2+ times)
    cascaded = [r for r in rows if r["depth"] >= 2]

    # Claims that were eventually resolved (verified or expired)
    resolved = [r for r in rows if r["status"] in ("verified", "expired")]

    # Top cascaders (deepest propagation)
    top = []
    for r in rows[:10]:
        top.append({
            "hash": r["claim_hash"],
            "text": r["claim_text"][:100],
            "depth": r["depth"],
            "status": r["status"],
            "first_seen": r["first_seen"],
            "last_seen": r["last_seen"],
        })

    conn.close()

    return {
        "avg_depth": round(avg_depth, 1),
        "max_depth": max_depth,
        "total_cascaded": len(cascaded),
        "resolved_count": len(resolved),
        "total_tracked": len(rows),
        "top_cascaders": top,
    }


def trace_claim(
    search_text: str,
    db_path: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Trace a claim by searching for it in tracked claims.

    Args:
        search_text: Substring to search for in claim text.

    Returns:
        Dict with claim info and cascade history, or None if not found.
    """
    conn = _get_db(db_path)

    # Search by hash first (exact), then by substring
    row = conn.execute(
        "SELECT * FROM tracked_claims WHERE claim_hash = ?",
        (search_text,)
    ).fetchone()

    if row is None:
        # Substring search
        row = conn.execute(
            "SELECT * FROM tracked_claims WHERE claim_text LIKE ? LIMIT 1",
            (f"%{search_text}%",)
        ).fetchone()

    if row is None:
        conn.close()
        return None

    claim = _row_to_tracked(row)
    conn.close()

    history = get_cascade_history(claim.claim_hash, db_path)

    return {
        "claim": claim.to_dict(),
        "cascade": [e.to_dict() for e in history],
        "cascade_depth": len(history),
    }


def _row_to_tracked(row: sqlite3.Row) -> TrackedClaim:
    """Convert a database row to a TrackedClaim."""
    return TrackedClaim(
        claim_hash=row["claim_hash"],
        claim_text=row["claim_text"],
        claim_type=row["claim_type"],
        source_file=row["source_file"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        last_verified=row["last_verified"],
        run_count=row["run_count"],
        status=row["status"],
        evidence=row["evidence"],
        verification_method=row["verification_method"],
    )
