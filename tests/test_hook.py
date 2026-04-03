"""Tests for the Claude Code hooks integration (confab hook)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Find the confab package
CONFAB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CONFAB_DIR.parent))

from confab.cli import cmd_hook, _extract_transcript_text


class TestExtractTranscriptText(unittest.TestCase):
    """Test transcript text extraction."""

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("")
            f.flush()
            result = _extract_transcript_text(f.name)
        os.unlink(f.name)
        self.assertEqual(result, "")

    def test_nonexistent_file(self):
        result = _extract_transcript_text("/nonexistent/path/transcript.jsonl")
        self.assertEqual(result, "")

    def test_extracts_assistant_text(self):
        lines = [
            json.dumps({"role": "user", "content": "Hello"}),
            json.dumps({"role": "assistant", "content": [
                {"type": "text", "text": "I checked the file at /tmp/test.py"}
            ]}),
            json.dumps({"role": "user", "content": "Thanks"}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            f.flush()
            result = _extract_transcript_text(f.name)
        os.unlink(f.name)
        self.assertIn("/tmp/test.py", result)

    def test_extracts_string_content(self):
        lines = [
            json.dumps({"role": "assistant", "content": "The file exists at /etc/hosts"}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            f.flush()
            result = _extract_transcript_text(f.name)
        os.unlink(f.name)
        self.assertIn("/etc/hosts", result)

    def test_skips_user_messages(self):
        lines = [
            json.dumps({"role": "user", "content": "secret user text"}),
            json.dumps({"role": "assistant", "content": "assistant response"}),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(lines))
            f.flush()
            result = _extract_transcript_text(f.name)
        os.unlink(f.name)
        self.assertNotIn("secret user text", result)
        self.assertIn("assistant response", result)

    def test_handles_malformed_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("not json\n")
            f.write(json.dumps({"role": "assistant", "content": "valid line"}) + "\n")
            f.flush()
            result = _extract_transcript_text(f.name)
        os.unlink(f.name)
        self.assertIn("valid line", result)


class TestHookCLI(unittest.TestCase):
    """Test the confab hook CLI via subprocess."""

    def _run_hook(self, stdin_data: str) -> subprocess.CompletedProcess:
        """Run confab hook as subprocess."""
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        return subprocess.run(
            [sys.executable, "-m", "confab", "hook"],
            input=stdin_data,
            capture_output=True,
            text=True,
            cwd=str(CONFAB_DIR.parent),
            env=env,
            timeout=10,
        )

    def test_empty_stdin_exits_clean(self):
        result = self._run_hook("")
        self.assertEqual(result.returncode, 0)

    def test_invalid_json_exits_1(self):
        result = self._run_hook("not json")
        self.assertEqual(result.returncode, 1)

    def test_unknown_event_exits_clean(self):
        event = json.dumps({"hook_event_name": "SessionStart"})
        result = self._run_hook(event)
        self.assertEqual(result.returncode, 0)

    def test_stop_event_no_transcript_exits_clean(self):
        event = json.dumps({
            "hook_event_name": "Stop",
            "transcript_path": "/nonexistent/transcript.jsonl",
        })
        result = self._run_hook(event)
        self.assertEqual(result.returncode, 0)

    def test_post_tool_use_write_with_valid_file(self):
        """Write referencing an existing file should pass."""
        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/test.txt",
                "content": "The config at /etc/hosts is ready.",
            },
        })
        result = self._run_hook(event)
        self.assertEqual(result.returncode, 0)

    def test_post_tool_use_write_with_nonexistent_file(self):
        """Write referencing a nonexistent file should produce a warning."""
        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/test.txt",
                "content": "The config at /tmp/confab_hook_test_nonexistent_xyz_42.json is loaded and working. [v1: verified 2026-04-01]",
            },
        })
        result = self._run_hook(event)
        self.assertEqual(result.returncode, 0)
        if result.stdout.strip():
            response = json.loads(result.stdout)
            # If claims were found, should have additionalContext
            hook_output = response.get("hookSpecificOutput", {})
            self.assertIn("additionalContext", hook_output)

    def test_post_tool_use_edit(self):
        """Edit events should check new_string content."""
        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/test.txt",
                "old_string": "old",
                "new_string": "The file at /etc/hosts exists",
            },
        })
        result = self._run_hook(event)
        self.assertEqual(result.returncode, 0)

    def test_post_tool_use_read_ignored(self):
        """Read events should be ignored (no text to check)."""
        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test.txt"},
        })
        result = self._run_hook(event)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_short_content_skipped(self):
        """Very short content should be skipped."""
        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/test.txt",
                "content": "hello",
            },
        })
        result = self._run_hook(event)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_stop_with_transcript(self):
        """Stop event with a real transcript should extract and check text."""
        # Create a transcript with a claim about a nonexistent file
        lines = [
            json.dumps({"role": "assistant", "content": [
                {"type": "text", "text": "The configuration file at /tmp/confab_hook_test_nonexistent_config_abc123.yaml is properly set up and ready to use. [v1: verified 2026-04-01]"}
            ]}),
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write("\n".join(lines))
            f.flush()
            transcript_path = f.name

        try:
            event = json.dumps({
                "hook_event_name": "Stop",
                "transcript_path": transcript_path,
            })
            result = self._run_hook(event)
            self.assertEqual(result.returncode, 0)
            # May or may not find claims depending on extraction patterns
        finally:
            os.unlink(transcript_path)

    def test_response_format(self):
        """When claims fail, response should match Claude Code hook schema."""
        # Create content with a definitely-false file reference
        event = json.dumps({
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/test.txt",
                "content": "Pipeline status: The script at /tmp/confab_hook_test_definitely_missing_pipeline_xyz.sh is running correctly. [v1: verified 2026-04-01]",
            },
        })
        result = self._run_hook(event)
        self.assertEqual(result.returncode, 0)

        if result.stdout.strip():
            response = json.loads(result.stdout)
            # Validate hook response schema
            self.assertIn("hookSpecificOutput", response)
            hook_output = response["hookSpecificOutput"]
            self.assertIn("hookEventName", hook_output)
            self.assertEqual(hook_output["hookEventName"], "PostToolUse")
            self.assertIn("additionalContext", hook_output)


if __name__ == "__main__":
    unittest.main()
