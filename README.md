# confab-framework

Structural confabulation detection and prevention for multi-agent systems.

Agents state falsehoods confidently. Other agents copy them forward indefinitely. This framework makes verification structural (enforced by code) rather than aspirational (suggested by docs).

## Why

Multi-agent systems have a confabulation cascade problem. Agent A states a falsehood with full confidence — "the config is deployed," "blocked on API key," "tests are passing." Agent B reads A's output, trusts it, and copies the claim into its own handoff. Agent C does the same. By the time a human notices, the false claim has propagated through dozens of builds.

This isn't hypothetical. In one production system, two false claims — "audio pipeline blocked on OPENAI_API_KEY" and "Substack publishing needs cookie refresh" — propagated through **16 consecutive agent builds over 3 days**. Every agent trusted the last agent's notes. Both pipelines worked perfectly the entire time. No agent checked.

The fix isn't better instructions. Agents ignore instructions the same way they ignore documentation. The fix is a **verification gate** that runs at every handoff point, extracts claims from agent output, and checks them against reality — filesystem, environment variables, running processes, pipeline outputs. Claims that contradict reality get flagged before the next agent sees them.

## What It Catches

Run `confab gate` at any agent handoff point:

```
$ confab gate

# Confabulation Gate Report

Scanned: docs/builder_priorities.md, docs/handoff.md
Claims found: 5
Auto-verified: 5

  [FAIL] Config at deploy/config.toml is ready
         deploy/config.toml: FILE MISSING

  [FAIL] Output at results/report.json verified clean
         results/report.json: MISSING

  [PASS] Migration at scripts/migrate.py needs review
         scripts/migrate.py: EXISTS

  [????] Blocked on DATABASE_URL environment variable
         DATABASE_URL: NOT FOUND in any .env or os.environ

  [????] Data pipeline is working [v1: verified 2026-03-20]
         Claim is semi-verifiable but lacks specific paths/vars for auto-check

## Summary
- Passed: 1/5 auto-verified (20%)
- Failed: 2
- Inconclusive: 2
```

The two FAILED claims would have cascaded to the next agent without the gate. The INCONCLUSIVE claims are flagged for manual verification.

## Examples

Working examples in [`examples/`](examples/):

- **[`standalone_scan.py`](examples/standalone_scan.py)** — Scan any markdown file for unverified claims via the Python API or CLI
- **[`ci_gate_handoff.py`](examples/ci_gate_handoff.py)** — Gate an agent handoff: verify claims before passing work to the next agent
- **[`github_actions.yml`](examples/github_actions.yml)** — Copy-paste GitHub Actions workflow for CI integration

See also [`examples/multi_agent_demo.py`](examples/multi_agent_demo.py) for a self-contained three-agent cascade simulation.

## Install

```bash
pip install confab-framework
```

Or from source:

```bash
git clone https://github.com/dennischoubot-glitch/confab-framework.git
pip install -e ./confab-framework
```

## Quick Start

### Scan any markdown (no config needed)

```bash
pip install confab-framework
confab scan path/to/handoff.md     # extract + verify claims in any file
confab scan docs/                  # scan all .md files in a directory
```

This works on any markdown — no `confab.toml` required. Claims referencing files are resolved relative to your current directory.

### Full project setup

```bash
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

### Decorator Middleware

Wrap any agent function with `@confab_gate` to auto-verify its output:

```python
from confab import confab_gate

@confab_gate
def my_agent(prompt: str) -> str:
    return "Config at config/prod.toml is ready. Blocked on DATABASE_URL."

# On call, the decorator extracts claims from the return value,
# verifies them against reality, and warns on failures.
result = my_agent("check status")
```

Modes:
- `@confab_gate` — warn on failures (default)
- `@confab_gate(on_fail="raise")` — raise `ConfabVerificationError` on failures
- `@confab_gate(on_fail="log")` — log quietly, for production

Options: `check_files=True`, `check_env=True` to control what gets verified.

See `examples/middleware_example.py` for a complete walkthrough.

### Framework Integrations

Install with the integration you need:

```bash
pip install confab-framework[langchain]   # LangChain
pip install confab-framework[crewai]      # CrewAI
pip install confab-framework[autogen]     # AutoGen v0.4+
```

**LangChain** — callback handler that verifies agent output at each step:

```python
from confab.integrations.langchain import ConfabCallbackHandler

handler = ConfabCallbackHandler(on_fail="warn")
llm = ChatOpenAI(callbacks=[handler])

# After execution:
print(handler.summary())   # "confab: 0 failures in 3 claims"
assert handler.clean       # True if no failures
```

**CrewAI** — task callback that checks output after each task:

```python
from confab.integrations.crewai import ConfabTaskCallback

cb = ConfabTaskCallback(on_fail="warn")
task = Task(description="...", agent=agent, callback=cb)

# After crew runs:
print(cb.summary())
```

**AutoGen** — intervention handler that intercepts agent responses at runtime:

```python
from confab.integrations.autogen import ConfabInterventionHandler

handler = ConfabInterventionHandler(on_fail="drop")  # drop = filter bad claims
runtime = SingleThreadedAgentRuntime(
    intervention_handlers=[handler]
)

# After execution:
print(handler.summary())
```

All integrations share the same interface: `check_files`, `check_env`, `check_counts` toggles, `on_fail` mode (`"warn"`, `"raise"`, `"log"`, and `"drop"` for AutoGen), plus `.reports`, `.last_report`, `.clean`, `.summary()`, and `.clear()`.

### Standalone text verification

```python
from confab import verify_text

report = verify_text("The model at /models/latest.bin is ready.")
print(report.summary())
# → confab: 1 FAILED of 1 claims
#     - The model at /models/latest.bin is ready.: File not found: /models/latest.bin
```

## How It Works

Agents in multi-agent systems pass claims forward at handoff points — "the pipeline is blocked on X," "file Y exists," "the config is ready." When an agent states a falsehood confidently, the next agent copies it forward. The confab framework breaks this cascade by extracting claims from handoff text, auto-verifying them against reality (filesystem, environment variables, script syntax, config parsing, pipeline outputs), and tracking how long unverified claims persist. Claims that fail verification get flagged; claims that linger without verification get marked stale. The gate runs at every agent handoff point, supplying the oracle bits that distinguish confabulation from understanding.

The pipeline: **Extract** (scan for claims) → **Classify** (type + verifiability) → **Score** (confidence 0.0–1.0) → **Verify** (check against ground truth) → **Track** (SQLite persistence across runs) → **Report** (failures + staleness + tree health).

### Confidence Scoring

Every extracted claim gets a confidence score (0.0–1.0) reflecting how certain the extractor is that the text is a verifiable claim and that the classification is correct:

```python
from confab import extract_claims

claims = extract_claims(agent_output)
for c in claims:
    print(f"[{c.confidence:.2f}] {c.claim_type.value}: {c.text[:60]}")
```

Scoring factors: specificity of extracted artifacts (paths, env vars), verifiability level (AUTO > SEMI > MANUAL), claim type signal strength, existing verification tags, and age penalty for stale unverified claims. Use confidence to prioritize which claims to verify first or to filter low-confidence noise.

### Assertion Context Detection

The extractor only flags lines that contain assertion language — words like *ready*, *deployed*, *working*, *blocked*, *missing*, *configured*, *operational*, etc. A line that merely references a file path (e.g., "see config/app.toml for details") is not treated as a claim. A line that asserts something about a file (e.g., "config/app.toml is ready") is.

This avoids false positives from documentation, comments, and references while catching the actual assertions that propagate through agent handoffs.

## Commands

### Core

| Command | Description |
|---------|-------------|
| `confab gate` | Run the full cascade gate — extract, verify, track, report |
| `confab check "text"` | Check inline text for claims |
| `confab extract file.md` | Extract claims without verifying |
| `confab quick` | One-line gate summary (for scripts and prompts) |
| `confab scan <files>` | Scan arbitrary markdown files for claims — extract + verify |
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
# volatility = "medium"  # Adaptive thresholds: low/medium/high or 0.0-1.0
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
| `process_status` | "monitor STOPPED since Mar 14" | `supervisorctl` / `systemd` / `pgrep` |

### Behavior Claim TTL

Transient claims about runtime state (API responses, process status, pipeline outputs) go stale faster than structural claims. The gate auto-expires behavior claims after 6 hours — if a claim's verification tag is older than the TTL, it's flagged for re-verification rather than trusted blindly.

This catches the pattern where "pipeline is working [v1: verified yesterday]" persists in a handoff file long after the pipeline broke.

### Adaptive Thresholds (Volatility)

The gate's thresholds can adapt to environmental conditions. In volatile periods (market regime changes, geopolitical shifts, rapid deployment), the gate loosens — faster adaptation matters more than rigorous verification. In stable periods, the gate tightens — integrity preservation matters more.

```bash
confab gate --volatility high       # looser: stale threshold ↑, TTL ↑
confab gate --volatility low        # tighter: stale threshold ↓, TTL ↓
confab gate --volatility 0.8        # numeric: 0.0 (tightest) to 1.0 (loosest)
confab ci --volatility medium       # works in CI mode too
```

Python API:

```python
from confab import ConfabGate

# Set volatility at init (persists for all runs)
gate = ConfabGate("confab.toml", volatility=0.8)
report = gate.run()

# Or override per-run
report = gate.run(volatility=0.3)
```

Configure a default in `confab.toml`:

```toml
[confab]
volatility = "medium"   # or 0.0–1.0
```

Named presets: `low` (0.2), `medium` (0.5), `high` (0.8). At `medium`, thresholds are unchanged. At `high`, the stale threshold rises ~60% and TTL doubles. At `low`, thresholds drop ~30%.

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

## Usage Examples

### Multi-agent cascade demo

A self-contained demo simulating a three-agent sprint cycle with the confab gate running at each handoff:

```bash
pip install confab-framework
python -m confab.examples.multi_agent_demo
```

The demo shows:
1. **Claim extraction** from natural language handoff text
2. **Auto-verification** catching false file/env claims before they cascade
3. **Cascade tracking** — how unverified claims age across builds
4. **High-level API** usage with `ConfabGate` and `ConfabConfig`

### Scanning any markdown file

Use `confab scan` to extract and verify claims from any markdown file — not limited to files in your `confab.toml`:

```bash
confab scan docs/README.md notes/handoff.md    # scan multiple files
confab scan docs/*.md                          # glob patterns work
confab scan docs/priorities.md --no-verify     # extract claims only, skip verification
confab scan docs/priorities.md --json          # machine-readable output
```

This is useful for one-off checks on files outside your normal gate pipeline.

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

Add claim verification to your CI in 5 minutes.

### Option 1: Copy-paste workflow (simplest)

Copy this into `.github/workflows/confab-gate.yml` in your repo:

```yaml
name: Confab Gate
on:
  pull_request:
    paths: ['docs/**', 'notes/**', '**/*.md']

permissions:
  contents: read
  pull-requests: write

jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install confab-framework
      - name: Run confab gate
        run: confab ci --no-track --strict --output report.md
      - name: Post PR comment
        if: always() && github.event_name == 'pull_request'
        uses: marocchino/sticky-pull-request-comment@v2
        with:
          path: report.md
```

That's it. Every PR that touches markdown files gets claim verification with results posted as a comment.

### Option 2: Reusable workflow (multi-repo)

Reference the workflow directly — no file to copy or maintain:

```yaml
name: Confab Gate
on:
  pull_request:
    paths: ['docs/**', '**/*.md']

jobs:
  confab:
    uses: dennischoubot-glitch/confab-framework/.github/workflows/confab-gate.yml@main
    with:
      strict: true
```

### Option 3: Composite action (custom integration)

Use the action directly for more control over the pipeline:

```yaml
jobs:
  confab:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run confab gate
        id: gate
        uses: dennischoubot-glitch/confab-framework@v0.8.0
        with:
          config: confab.toml
          strict: true
          stale-threshold: 3
      - name: Use results
        if: always()
        run: echo "Status=${{ steps.gate.outputs.status }} Failed=${{ steps.gate.outputs.failed }}"
```

The action installs confab-framework from PyPI, runs `confab ci`, and posts the markdown report as a PR comment.

### `confab ci` command

Run the gate directly in any CI pipeline:

```bash
confab ci                        # exits 1 on failures, 0 otherwise
confab ci --strict               # also exits 2 on stale claims
confab ci --output report.md     # write markdown report for PR comments
confab ci --no-track             # skip tracker DB (stateless CI runs)
```

Exit codes:
- `0` — clean (all claims verified, no stale)
- `1` — failures (claims contradict reality)
- `2` — stale claims only (with `--strict`)

### Generic CI (GitLab, CircleCI, etc.)

```yaml
# .gitlab-ci.yml
confab:
  image: python:3.12
  script:
    - pip install confab-framework
    - confab ci --strict --output confab-report.md
  artifacts:
    when: always
    paths:
      - confab-report.md
```

## Releasing

Releases are automated via GitHub Actions. When a version tag is pushed, the workflow runs tests, builds the package, and publishes to PyPI using Trusted Publishers (OIDC).

```bash
# 1. Update version in pyproject.toml
# 2. Commit the version bump
git add pyproject.toml
git commit -m "Bump version to 0.9.0"

# 3. Tag and push
git tag v0.9.0
git push && git push --tags
```

The workflow verifies the tag version matches `pyproject.toml` before publishing.

### Trusted Publisher Setup (one-time)

Configure PyPI to trust this GitHub repo — no API tokens needed:

1. Go to [pypi.org/manage/project/confab-framework/settings/publishing/](https://pypi.org/manage/project/confab-framework/settings/publishing/)
2. Under **Add a new pending publisher**, enter:
   - **Owner:** `dennischoubot-glitch`
   - **Repository name:** `confab-framework`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
3. Click **Add**

After this, any tag push matching `v*.*.*` will auto-publish.

## Architecture

See [DESIGN.md](DESIGN.md) for the full architecture, including the cascade propagation problem, verification methods, and the gate's role at agent handoff points.

## License

MIT
