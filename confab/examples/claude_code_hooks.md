# Confab + Claude Code Hooks

Real-time confabulation detection for Claude Code sessions.

## Quick Setup

Add to your `.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "confab hook",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

This checks Claude's output for confabulated claims (false file references,
non-existent env vars, incorrect counts) every time Claude finishes a response.

## What It Checks

The hook extracts verifiable claims from Claude's output and checks them:

- **File existence** — References to files that don't exist on disk
- **Environment variables** — Claims about env vars that aren't set
- **Counts and statistics** — Numeric claims that can be verified

When failures are found, the hook injects a warning into Claude's context
so it can self-correct.

## Hook Events

### Stop (recommended)

Fires when Claude finishes responding. Reads the session transcript and
checks the final output for claims. Best for catching confabulation before
the user acts on it.

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "confab hook",
          "timeout": 10
        }]
      }
    ]
  }
}
```

### PostToolUse on Write/Edit

Fires after Claude writes or edits a file. Checks the content being
written for claims. Good for catching false references in documentation.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [{
          "type": "command",
          "command": "confab hook",
          "timeout": 10
        }]
      }
    ]
  }
}
```

### Both Together

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "confab hook",
          "timeout": 10
        }]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [{
          "type": "command",
          "command": "confab hook",
          "timeout": 10
        }]
      }
    ]
  }
}
```

## Installation

```bash
pip install confab-framework
```

The `confab` CLI is then available system-wide for hooks.

## How It Works

1. Claude Code fires a hook event (Stop or PostToolUse)
2. Hook JSON is piped to `confab hook` on stdin
3. Confab extracts text from the event payload
4. Claims are extracted and verified against the filesystem
5. If failures are found, a warning is returned as `additionalContext`
6. Claude sees the warning and can self-correct

## Custom Configuration

Use a `confab.toml` in your project root to customize claim extraction:

```toml
[extraction]
check_files = true
check_env = true
check_counts = false

[verification]
timeout = 5
```

Generate a starter config:

```bash
confab init
```
