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

| Command | Description |
|---------|-------------|
| `confab gate` | Run the full cascade gate |
| `confab check "text"` | Check inline text for claims |
| `confab extract file.md` | Extract claims without verifying |
| `confab quick` | One-line gate summary |
| `confab prune` | Identify stale build sections to remove |
| `confab sweep` | Show tracked claims by staleness |
| `confab sweep --stats` | Tracker statistics |
| `confab report` | System health dashboard (gate + supports + coverage) |
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
