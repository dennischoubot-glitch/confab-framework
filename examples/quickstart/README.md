# Confab Quickstart

A self-contained example showing how `confab` catches stale and false claims in agent handoff files.

## What's here

| File | Purpose |
|------|---------|
| `handoff.md` | A sample agent handoff file with carry-forward claims |
| `confab.toml` | Configuration telling confab which files to scan |

## Setup

Install confab (if not already):

```bash
pip install confab-framework
# or run from source:
cd /path/to/confab && pip install -e .
```

## Workflow

### 1. Extract claims

See what confab detects in the handoff file:

```bash
confab extract handoff.md
```

Output shows each claim's type (`file_exists`, `env_var`, `pipeline_works`, etc.),
verification tag (`[unverified]`, `[v1: ...]`, `[v2: ...]`), and extracted evidence paths.

### 2. Run the gate

The gate auto-verifies claims against reality:

```bash
confab gate
```

Expected output for this example:

- **FAILED:** `scripts/migrate_v3.py` — the handoff says it exists, but it doesn't
  (because this is a demo directory, not the real project)
- **FAILED:** `src/middleware/auth_v1.py` — detected as a file reference
- **Inconclusive:** `AWS_ACCESS_KEY_ID` — env var not found (expected in a demo)

In a real project, FAILED claims mean the handoff file is lying to the next agent.
The agent should investigate before trusting the claim.

### 3. Check inline text

Verify a single claim without scanning files:

```bash
confab check "Audio pipeline is blocked on OPENAI_API_KEY"
```

### 4. Lint for claim hygiene

Check that claims follow verification tag conventions:

```bash
confab lint handoff.md
```

Flags claims missing `[unverified]`/`[v1: ...]`/`[v2: ...]` tags, stale verification
dates, and claims that have persisted too long without re-verification.

### 5. CI integration

For CI pipelines (GitHub Actions, etc.), use the `ci` subcommand:

```bash
confab ci                    # markdown output, exit code 1 on failures
confab ci --strict           # exit code 2 on stale claims too
confab ci --output report.md # write report to file (for PR comments)
```

## Making it real

To use confab in your own project:

```bash
# 1. Initialize config (auto-detects markdown files)
cd your-project/
confab init

# 2. Edit confab.toml — uncomment the files you want scanned
#    Add env vars and pipeline mappings relevant to your project

# 3. Run the gate before every agent handoff
confab gate

# 4. Add to CI
#    See action.yml in the confab repo for a GitHub Action
```

## Claim types confab detects

| Type | Auto-verifiable | What it checks |
|------|----------------|----------------|
| `file_exists` | Yes | File/directory exists on disk |
| `env_var` | Yes | Environment variable is set |
| `pipeline_works` | Yes | Script runs or recent output exists |
| `config_present` | Yes | Key exists in JSON/YAML/TOML config |
| `status_claim` | Partially | Git status, process status |
| `fact_claim` | Partially | Cross-reference with knowledge base |
| `subjective` | No | Requires human judgment |

## Why this matters

Stateless agents inherit claims from previous agents without questioning them.
A single false claim ("audio blocked on OPENAI_API_KEY") propagated through 16+
agent invocations over 3 days — every agent trusted the last agent's notes without
checking. The confab gate makes verification structural: claims that CAN be checked
ARE checked, automatically, at every handoff.
