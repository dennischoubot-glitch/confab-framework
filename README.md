# confab-framework

Structural confabulation detection and prevention for multi-agent systems.

Agents state falsehoods confidently. Other agents copy them forward indefinitely. This framework makes verification structural (enforced by code) rather than aspirational (suggested by docs).

## Install

```bash
pip install confab-framework
```

Or from source:

```bash
pip install -e ./core/confab
```

## Quick Start

```bash
pip install confab-framework
cd your-project/
confab init                        # generate a confab.toml
# Edit confab.toml — add your priority/handoff files to files_to_scan
confab gate                        # verify carry-forward claims against reality
```

### Python API

```python
from confab import ConfabGate

# From a config file
gate = ConfabGate("confab.toml")
report = gate.run()

if report.has_failures:
    print(report.format_report())
elif report.has_stale:
    print(f"{report.stale_claims} stale claims need verification")
else:
    print(f"Clean: {report.passed} claims verified")

# Check inline text directly
outcomes = gate.check("Audio pipeline blocked on OPENAI_API_KEY")
for o in outcomes:
    print(f"{o.result.value}: {o.evidence}")
```

## How It Works

Agents in multi-agent systems pass claims forward at handoff points — "the pipeline is blocked on X," "file Y exists," "the config is ready." When an agent states a falsehood confidently, the next agent copies it forward. The confab framework breaks this cascade by extracting claims from handoff text, auto-verifying them against reality (filesystem, environment variables, script syntax, config parsing, pipeline outputs), and tracking how long unverified claims persist. Claims that fail verification get flagged; claims that linger without verification get marked stale. The gate runs at every agent handoff point, supplying the oracle bits that distinguish confabulation from understanding.

The pipeline: **Extract** (scan for claims) → **Classify** (type + verifiability) → **Verify** (check against ground truth) → **Track** (SQLite persistence across runs) → **Report** (failures + staleness + tree health).

## Commands

### Core

| Command | Description |
|---------|-------------|
| `confab gate` | Run the full cascade gate — extract, verify, track, report |
| `confab check "text"` | Check inline text for claims |
| `confab extract file.md` | Extract claims without verifying |
| `confab quick` | One-line gate summary (for scripts and prompts) |
| `confab init` | Generate a starter `confab.toml` in the current directory |

### Hygiene

| Command | Description |
|---------|-------------|
| `confab lint [file]` | Check claim hygiene — missing verification tags, stale `[unverified]` claims |
| `confab sweep` | Show tracked claims sorted by staleness |
| `confab sweep --stats` | Tracker statistics |
| `confab prune` | Identify stale build sections to remove |

### Diagnostics (Knowledge Tree)

| Command | Description |
|---------|-------------|
| `confab tree` | Scan knowledge tree for factual health — expired, perishable, unverified observations |
| `confab check-supports` | Check for zombie/weakened entries (all or most supports invalidated) |
| `confab report` | System health dashboard combining gate + supports + coverage |

### Tracing & Audit

| Command | Description |
|---------|-------------|
| `confab trace "text"` | Trace propagation path of a specific claim across gate runs |
| `confab cascade` | Show cascade depth statistics — how far claims propagate |
| `confab audit` | Comprehensive audit: claims, cascades, resolution rate |

### CI

| Command | Description |
|---------|-------------|
| `confab ci` | CI-friendly gate with markdown output and exit codes |
| `confab ci --strict` | Also fail on stale claims (exit code 2) |

## Configuration

`confab.toml` in your workspace root:

```toml
[confab]
files_to_scan = ["docs/priorities.md", "notes/handoff.md"]
stale_threshold = 3
db_path = "confab_tracker.db"

[confab.env_vars]
known = ["OPENAI_API_KEY", "DATABASE_URL"]

[confab.pipelines]
"my_pipeline.py" = ["output/data/", "output/report.json"]

# Optional: name-based pipeline matching for status claims
[confab.pipeline_names]
"data pipeline" = "my_pipeline.py"

# Optional: count verification sources
[confab.count_sources.my_entries]
file = "data/entries.json"
type = "json_array"
json_path = "entries"

[confab.count_sources.task_queue]
file = "queue.md"
type = "regex_count"
pattern = "^###\\s+Task\\s+\\d+"
rate_per_day = 3.0
```

Without a config file, the framework auto-detects context and uses sensible defaults.

## Claim Types

| Type | Example | Verification |
|------|---------|-------------|
| `file_exists` | "config.json is ready" | `os.path.exists()` |
| `file_missing` | "output.csv doesn't exist" | `os.path.exists()` |
| `env_var` | "blocked on OPENAI_API_KEY" | `.env` files + `os.environ` |
| `pipeline_works` | "audio pipeline operational" | Output artifact check |
| `pipeline_blocked` | "publishing blocked" | Output artifact check |
| `script_runs` | "generate.py works" | `py_compile` + import check |
| `config_present` | "settings.toml configured" | Parse + key check |
| `count_claim` | "144 tests passing" | Source-specific count |

## Diagnostics

### `confab lint`

Scans priority and handoff files for claim hygiene issues: missing verification tags (`[unverified]`, `[v1: ...]`, `[v2: ...]`), claims that have lingered at `[unverified]` past the staleness threshold, and formatting problems.

```bash
confab lint                          # lint all files_to_scan from confab.toml
confab lint notes/handoff.md         # lint a specific file
confab lint --threshold 5            # flag [unverified] claims after 5 runs
confab lint --json                   # machine-readable output
```

### `confab tree`

Scans a knowledge tree (JSON) for factual health: expired observations past their TTL, perishable facts missing an `expires` date, and unverified observations older than a threshold.

```bash
confab tree                          # scan with defaults (14-day stale window)
confab tree --stale-days 7           # tighter window
confab tree --tree path/to/tree.json # explicit tree path
```

### `confab check-supports`

Checks knowledge tree entries whose supporting evidence has degraded — entries where most or all supports have been invalidated.

```bash
confab check-supports                # list weakened entries
confab check-supports --fix --dry-run  # preview auto-invalidation of zombies
confab check-supports --fix          # auto-invalidate entries with all supports dead
```

## Tracing & Audit

### `confab trace`

Traces a specific claim across every gate run it appeared in — when it was first seen, how many runs it persisted, and its verification status at each checkpoint.

```bash
confab trace "pipeline"              # search by text substring
confab trace "abc123"                # search by claim hash
confab trace "OPENAI_API_KEY" --json # machine-readable
```

### `confab cascade`

Shows how far claims propagate before being verified or removed. High cascade depth means claims are being copied forward without verification — the exact failure mode this framework prevents.

```bash
confab cascade                       # cascade depth statistics
confab cascade --json                # machine-readable
```

### `confab audit`

Comprehensive summary combining claim tracking, cascade analysis, and resolution rates into a single report.

```bash
confab audit                         # full audit report
confab audit --json                  # machine-readable
```

## Examples

### Checking claims in a handoff file

An agent writes a handoff note for the next agent. Before the next agent acts on those claims, the gate checks them against reality:

```bash
# The handoff file says: "Config deployed at config/prod.toml"
# and "Blocked on DATABASE_URL"
confab gate --file notes/handoff.md
```

If `config/prod.toml` doesn't exist, the gate flags it as `FAILED`. If `DATABASE_URL` is set in the environment, the "blocked" claim is contradicted. The next agent sees the failures before acting on bad information.

### Linting for claim hygiene

Enforce verification discipline across your team's handoff files:

```bash
confab lint docs/priorities.md
```

Output flags claims without verification tags:

```
CONFAB LINT REPORT
====================================================
Files scanned: 1
Claims found:  5
Issues:        2 (0 errors, 2 warnings, 0 info)

--- docs/priorities.md
  W line 12: [no-tag] Claim has no verification tag
  W line 31: [no-tag] Claim has no verification tag
```

### Monitoring claim propagation over time

Run the gate at every handoff (or on a schedule) and use `cascade` and `audit` to see how claims age:

```bash
# After several gate runs, check how claims are propagating
confab cascade

# See the full picture: resolution rate, depth distribution, unresolved claims
confab audit
```

A healthy system has a high resolution rate and low cascade depth. Deep cascaders are claims being copied forward without anyone checking them.

### Knowledge tree health check

For systems using a JSON knowledge tree with observations, check factual freshness:

```bash
# Find expired and unverified observations
confab tree

# Find entries whose evidence base has eroded
confab check-supports

# Combined dashboard
confab report
```

## CI Integration

### `confab ci` command

Run the gate in CI pipelines with proper exit codes and markdown output:

```bash
# Basic — exits 1 on failures, 0 otherwise
confab ci

# Strict — also exits 2 on stale claims
confab ci --strict

# Write markdown report to file (for PR comments)
confab ci --output report.md

# Skip tracker DB persistence (stateless CI runs)
confab ci --no-track
```

Exit codes:
- `0` — clean (all claims verified, no stale)
- `1` — failures (claims contradict reality)
- `2` — stale claims only (with `--strict`)

### GitHub Action

Add confab to your CI pipeline with the GitHub Action:

```yaml
name: Confab Gate
on:
  pull_request:
    paths:
      - 'docs/**'
      - 'notes/**'

jobs:
  confab:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run confab gate
        uses: dennischoubot-glitch/confab-framework@v0.4.0
        with:
          config: confab.toml        # optional, auto-detected
          strict: true               # fail on stale claims too
          stale-threshold: 3         # runs before flagging stale
```

The action installs confab-framework from PyPI, runs `confab ci`, and posts the markdown report as a PR comment.

### Generic CI (GitLab, CircleCI, etc.)

```yaml
# .gitlab-ci.yml
confab:
  image: python:3.12
  script:
    - pip install confab-framework
    - confab ci --strict
  artifacts:
    when: always
    paths:
      - confab-report.md
```

## Architecture

See [DESIGN.md](DESIGN.md) for the full architecture, including the cascade propagation problem, verification methods, and the gate's role at agent handoff points.

## License

MIT
