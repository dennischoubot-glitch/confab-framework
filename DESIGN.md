# Confabulation Framework — Design

> **Problem:** Agents state falsehoods confidently. Other agents treat these as fact. Errors cascade — 17x amplification in mesh topologies (idea-359). The 16-build false blocker episode (obs-3528) proved this isn't theoretical.

## Conceptual Foundation

Harry Frankfurt's *On Bullshit* distinguishes three failure modes: **hallucination** (generating fiction — opposed to truth), **lying** (aware of truth, choosing falsehood), and **bullshit** (indifferent to truth — generating plausible claims with no relationship to verification). Rob Nelson's ["On Confabulation"](https://www.ailog.blog/p/on-confabulation) (AI Log, Feb 2025) maps the third mode to the psychiatric definition of **confabulation**: false memories produced without awareness of their falsity.

This is the exact failure mode of stateless agents. They don't lie about blockers — they confabulate. They inherit a claim ("audio blocked on OPENAI_API_KEY"), propagate it across 16 builds, and never develop a verification relationship with it. The claim feels true because it was written confidently by a prior agent. This framework targets confabulation specifically: claims that persist not through malice or ignorance, but through *indifference to whether they are true*.

## Design Principle

**Make verification structural, not aspirational.** The manual verification protocol (obs-3530) works when agents follow it. They don't always follow it. The framework makes verification happen automatically at cascade propagation points.

## What Would Have Caught the False Blockers?

The two false claims that persisted for 16+ builds:
1. "Audio still blocked on OPENAI_API_KEY" — testable by checking env/running script
2. "Substack publishing needs cookie" — testable by checking the script's own load_env()

Both were **verifiable claims about system state** that persisted because no agent checked reality. The fix: automatically verify claims that CAN be verified, and flag the rest.

## Architecture

```
                    ┌─────────────────────┐
                    │   Priority Files    │  ← carry-forward claims live here
                    │  (builder_priorities │
                    │   dreamer_priorities)│
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Claim Extractor   │  ← parse claims from text
                    │   (claims.py)       │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │ Verification Engine │  ← auto-verify what's testable
                    │   (verify.py)       │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Cascade Gate     │  ← block/flag unverified claims
                    │    (gate.py)        │
                    └──────────┬──────────┘
                               │
                    ┌──────────▼──────────┐
                    │      Report         │  ← what's verified, what's not,
                    │                     │     what failed verification
                    └─────────────────────┘
```

## Claim Types

| Type | Auto-verifiable? | How |
|------|-----------------|-----|
| `file_exists` | Yes | Check filesystem |
| `file_missing` | Yes | Check filesystem |
| `env_var` | Yes | Check .env files |
| `pipeline_works` | Yes | Run with --dry-run or check recent output |
| `pipeline_blocked` | Yes | Test the blocker condition |
| `script_runs` | Yes | Import check or syntax check |
| `config_present` | Yes | Parse JSON/YAML/TOML/INI, check key existence (dot notation) |
| `fact_claim` | Partially | Check knowledge tree, git history |
| `status_claim` | Partially | Check git status, process status |
| `subjective` | No | Requires human/agent judgment |

## Four Threads

### 1. Detection — Distinguish confabulation from understanding

**Approach:** Pattern-match priority file text for verifiable claims. A claim about file existence, env vars, or pipeline status is testable. A claim about design quality is not.

The key insight from truth-016: confabulation is indistinguishable from understanding *without external oracle bits*. The framework's job is to supply those oracle bits automatically where possible.

### 2. Prevention — Structural gates that stop cascade propagation

**Approach:** At every handoff point (builder → next builder, dreamer → builder), run the gate. Claims that fail auto-verification get stripped or flagged. Claims that pass get promoted. Claims that can't be auto-verified get tagged with age.

**Stale claim rule:** Unverified non-auto-verifiable claims older than 3 builds get flagged for manual verification or deletion. This is the existing obs-3530 rule, now enforced by code.

### 3. Verification — Automated fact-checking against ground truth

**Approach:** For each auto-verifiable claim type, implement a checker:
- File existence → `os.path.exists()`
- Env var → parse `.env` files and check `os.environ`
- Pipeline status → check output files, timestamps, exit codes
- Script validity → `py_compile` or import check
- Git state → `git` commands

### 4. Protocol — Confabulation-resistant multi-agent coordination

**Status:** Implemented March 19, 2026.

**Approach:** Three components make the confab framework operational at agent handoff boundaries:

#### 4a. Claim Format Spec

Claims in priority files follow this format:
```
- [claim: TYPE] description [STATUS]
```

**TYPE** must be one of the auto-verifiable claim types from the table above:
`file_exists`, `file_missing`, `env_var`, `pipeline_works`, `pipeline_blocked`, `script_runs`, `config_present`

**STATUS** is one of:
- `[unverified]` — No agent has tested this claim. MUST be verified before propagating.
- `[v1: METHOD DATE]` — One agent verified. Next agent should independently verify.
- `[v2: METHOD DATE]` — Two independent agents confirmed. Trusted — propagate without re-checking.
- `[FAILED: reason]` — Auto-verification contradicted this claim. Evidence attached.

**METHOD** is how verification was performed:
`file_read`, `script_run`, `env_check`, `web_search`, `git_check`, `manual_test`

**Examples:**
```
- [claim: pipeline_works] Audio generation pipeline operational [v2: script_run 2026-03-14]
- [claim: env_var] OPENAI_API_KEY present in environment [v1: env_check 2026-03-19]
- [claim: pipeline_blocked] Substack publishing needs cookie [FAILED: script loads env vars directly, no cookie needed]
- [claim: file_exists] Weather rewards monitor config at scripts/weather_config.json [unverified]
```

**Rules for agents writing claims:**
1. Any carry-forward statement about system state SHOULD use claim format.
2. Plain-text statements (without `[claim:]` prefix) are still scanned by the gate's pattern matcher, but claim-formatted entries get stronger verification routing.
3. When a claim FAILS verification, don't silently remove it — mark it `[FAILED: evidence]` so the next agent sees both the original claim AND the counter-evidence.
4. Claims at `[unverified]` after 3+ gate runs get flagged `STALE` by the tracker. Verify or delete.
5. Subjective assessments ("code quality is good", "design is clean") are NOT claims. Don't force them into claim format.

#### 4b. Gate Integration into Agent Lifecycle

The gate runs at two points in the agent lifecycle:

1. **Builder pre-flight (MANDATORY):** Before acting on carry-forward claims, run:
   ```bash
   python core/confab/cli.py gate
   ```
   If FAILED claims exist, investigate before trusting them. If STALE claims exist, verify or delete before propagating.

2. **System health report (post-build):** After completing work, the health dashboard can be viewed:
   ```bash
   python core/confab/cli.py report           # terminal dashboard
   python core/confab/cli.py report --slack    # concise Slack-friendly output
   ```
   This combines gate results with knowledge tree supports analysis and verification coverage.

The gate is non-blocking — it produces a report, not a hard stop. This preserves the "wrong > blocked" principle while making verification structural rather than aspirational.

#### 4c. System Health Report

`python core/confab/cli.py report` outputs a comprehensive health dashboard combining:
- Gate results (claims extracted, verified, failed, stale)
- Knowledge tree supports analysis (zombie/weakened entries)
- Verification coverage percentage

Use `--slack` for a concise Slack-friendly version, `--json` for machine-readable output.

Example terminal output:
```
====================================================
  CONFAB SYSTEM HEALTH REPORT
====================================================

CLAIMS
  Total: 4  |  Verified: 2  |  Failed: 0  |  Stale: 0
  Inconclusive: 2  |  Skipped: 0
  Pass rate: 50% (2/4 auto-verified)
  Files: builder_priorities.md, dreamer_priorities.md

----------------------------------------------------

KNOWLEDGE TREE SUPPORTS
  Entries checked: 589  |  Zombies: 14  |  Weakened: 13  |  Healthy: 562
  No supports: 1  |  Invalidated: 518  |  Total tree: 4524

----------------------------------------------------

VERIFICATION COVERAGE
  Claims verified: 2/4
  Tree entries healthy: 562/589
  Combined coverage: 95.1% (564/593)

====================================================
  STATUS: CRITICAL
====================================================
```

## Integration Points

1. **Builder pre-flight** — `python core/confab/cli.py gate` before starting work (MANDATORY in builder checklist)
2. **Builder post-flight** — Verify claims in priorities before handoff
3. **CLI commands:**
   - `gate` — Full gate report (for agent consumption in prompts)
   - `report` — System health dashboard (gate + supports + coverage); `--slack` for concise output
   - `check` — On-demand verification of inline text
   - `quick` — One-line gate summary (for embedding in prompts)
   - `sweep` — Tracked claims by staleness (persistent across runs)
   - `extract` — Extract claims from a file without verifying
   - `prune` — Identify stale build sections to remove
4. **Cron** — Optional periodic sweep of priority files

## Dog-food Results (March 19, 2026)

First real-world application of the framework on its own system:

**Before:** builder_priorities.md had 714 lines, 40+ historical build sections. Gate found:
- 80 claims scanned, 4 passed (5%), 15 failed, 61 inconclusive, 46 stale
- All 15 failures: dead file paths in old build sections (FOMC templates, Koo framework docs, queue files, edugame paths, etc.)
- Root cause: historical build sections accumulate references to files that existed at build time but were later moved/renamed/deleted. No agent ever cleaned up.

**After:** Pruned to ~120 lines (3 recent builds + structural sections). Gate: CLEAN — 0 failures, 0 stale.

**What the framework caught that agents didn't:**
- `KOO_FRAMEWORK_BASELINE.md` referenced but doesn't exist
- `projects/synthesis/drafts/fomc-scenario-*.md` — 4 separate references to templates that were consumed
- `notes_queue.md`, `synthesis.json` — relative paths that only work from a specific directory
- `PROJECT_STATE.md` for edugame — wrong project path persisting across builds

**Lesson:** The gate diagnosed; the builder treated. This is the correct separation — auto-detection + human (agent) judgment on what to fix.

### Hardening Sprint (March 19 ~1PM)

**Bug fix:** `verify_script_imports` was broken — used `module_from_spec(spec)` without `exec_module()`, so a script with `import nonexistent_module` silently passed. Subprocess return code was never checked. Fixed: now uses AST-based import extraction + individual `__import__()` checks in isolated subprocess. Catches real missing dependencies, skips relative imports (package-internal), checks return code.

**New verification type:** `config_present` — parses JSON/YAML/TOML/INI config files, verifies they exist and parse correctly, optionally checks for specific keys using dot notation (e.g., `database.host`). Extends the framework from 4 to 5 verification types. Claim extraction auto-routes config file references with assertion context words ("configured", "key", "setting") to this type, extracting key names from backticked identifiers.

**Verification types now:** file_exists, file_missing, env_var, script_runs (AST import check), pipeline_output, config_present.

## Staleness Tracking (Thread 2: Prevention)

Added March 19, 2026. The gate now persists claim tracking across runs in a SQLite database (`confab_tracker.db`).

### How it works

Each `gate` run:
1. Extracts claims from priority files (same as before)
2. Runs verification (same as before)
3. **NEW:** Hashes each claim text (normalized) and looks it up in the tracker DB
4. New claims → inserted with `run_count=1`
5. Returning claims → `run_count` incremented
6. Claims that pass verification → status updated to `verified` with timestamp
7. Claims at `run_count >= 3` without verification → flagged `stale`

### CLI commands

```bash
# Gate now automatically tracks (adds --no-track to disable)
python core/confab/cli.py gate

# View all tracked claims by staleness
python core/confab/cli.py sweep

# Remove stale claims from tracker
python core/confab/cli.py sweep --remove-stale

# Tracker statistics
python core/confab/cli.py sweep --stats

# Gate run history
python core/confab/cli.py sweep --history
```

### Why this catches the 16-build cascade

The false blockers ("Audio blocked on OPENAI_API_KEY") persisted because:
1. Each builder read the claim, trusted it, and propagated it
2. No system tracked *how many times* the claim had been seen without verification
3. There was no structural escalation for old unverified claims

The tracker adds that missing memory. After 3 gate runs where a claim appears but is never verified, it gets flagged as `STALE` — the signal that this claim needs human or agent attention before propagating further.

### Database schema

**`tracked_claims`**: claim_hash (PK), claim_text, claim_type, source_file, first_seen, last_seen, last_verified, run_count, status, evidence, verification_method

**`gate_runs`**: id, timestamp, files_scanned, total_claims, passed, failed, stale, new_claims, returning_claims

### Design decisions

- **Hash-based dedup:** Claims are normalized (lowercase, whitespace collapsed) and SHA-256 hashed. Minor reformatting doesn't create duplicates.
- **Non-blocking:** The tracker records but doesn't block execution. The gate report flags stale claims; agents decide what to do.
- **Staleness threshold = 3:** Matches the existing obs-3530 two-agent verification rule. Three gate runs without verification is the signal.
- **Separate from in-file staleness:** The original `age_builds` counted build sections within a single file. The tracker counts across separate gate invocations — a fundamentally different (and more useful) staleness signal.

## What This Does NOT Do

- Replace human judgment for subjective claims
- Guarantee zero confabulation (truth-016 says this is impossible)
- Auto-fix false claims (it flags them; agents fix them)
- Handle novel claim types it hasn't seen before

The framework reduces cascade propagation by catching the LOW-HANGING claims that are objectively testable. Per the false blocker episode, these are the majority of claims that actually propagate.
