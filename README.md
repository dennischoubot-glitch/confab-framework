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

### CLI

```bash
# Generate a config file
confab init

# Run the cascade gate (extracts claims, verifies them, reports failures)
confab gate

# Check a single claim inline
confab check "Audio is blocked on OPENAI_API_KEY"

# One-line summary for embedding in prompts
confab quick
```

### Python API

```python
from confab import ConfabGate

# Load config from file
gate = ConfabGate("confab.toml")
report = gate.run()

if report.has_failures:
    print(report.format_report())
elif report.has_stale:
    print(f"{report.stale_claims} stale claims need verification")
else:
    print(f"Clean: {report.passed} claims verified")

# Check specific files
report = gate.run(files=["docs/handoff.md"])

# Check inline text
outcomes = gate.check("Pipeline is blocked on OPENAI_API_KEY")
for outcome in outcomes:
    print(f"{outcome.result.value}: {outcome.claim.text}")

# One-line summary
print(gate.quick())
```

Or use the lower-level function API:

```python
from confab import load_config, set_config, run_gate

config = load_config(config_path=Path("confab.toml"))
set_config(config)
report = run_gate()
```

## How It Works

1. **Extract** -- Scans priority files and handoff text for carry-forward claims (file exists, env var present, pipeline works/blocked, counts)
2. **Classify** -- Each claim gets a type and verifiability level (auto, semi, manual)
3. **Verify** -- Auto-verifiable claims are checked against reality (filesystem, env vars, script syntax, config parsing, pipeline outputs)
4. **Track** -- A persistent SQLite tracker records how many gate runs each claim has survived without verification
5. **Report** -- Gate reports flag failures (claim contradicts reality) and stale claims (persisted too long without verification)

## Commands

| Command | Description |
|---------|-------------|
| `confab gate` | Run the full cascade gate |
| `confab check "text"` | Check inline text for claims |
| `confab extract file.md` | Extract claims without verifying |
| `confab quick` | One-line gate summary |
| `confab prune` | Identify stale build sections to remove |
| `confab sweep` | Show tracked claims by staleness |
| `confab sweep --stats` | Tracker statistics |
| `confab report` | Slack-friendly concise report |
| `confab init` | Generate a starter `confab.toml` |

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

## Architecture

See [DESIGN.md](DESIGN.md) for the full architecture, including the cascade propagation problem, verification methods, and the gate's role at agent handoff points.

## License

MIT
