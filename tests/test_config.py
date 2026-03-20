"""Tests for configuration loading."""

import tempfile
import unittest
from pathlib import Path

from confab.config import (
    ConfabConfig,
    load_config,
    get_config,
    set_config,
    reset_config,
    _is_ia_repo,
    _detect_workspace_root,
    _load_toml,
    _config_from_toml,
    CONFIG_FILENAME,
)


class TestConfabConfig(unittest.TestCase):
    """Test ConfabConfig dataclass."""

    def test_default_db_path(self):
        """db_path defaults to workspace_root / confab_tracker.db."""
        config = ConfabConfig(
            workspace_root=Path("/tmp/test"),
            files_to_scan=["a.md"],
        )
        self.assertEqual(config.db_path, Path("/tmp/test/confab_tracker.db"))

    def test_explicit_db_path(self):
        config = ConfabConfig(
            workspace_root=Path("/tmp/test"),
            files_to_scan=["a.md"],
            db_path=Path("/tmp/custom.db"),
        )
        self.assertEqual(config.db_path, Path("/tmp/custom.db"))

    def test_default_stale_threshold(self):
        config = ConfabConfig(
            workspace_root=Path("/tmp"),
            files_to_scan=[],
        )
        self.assertEqual(config.stale_threshold, 3)

    def test_custom_stale_threshold(self):
        config = ConfabConfig(
            workspace_root=Path("/tmp"),
            files_to_scan=[],
            stale_threshold=5,
        )
        self.assertEqual(config.stale_threshold, 5)


class TestLoadToml(unittest.TestCase):
    """Test TOML file loading."""

    def test_valid_toml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[confab]\nfiles_to_scan = ["a.md"]\nstale_threshold = 5\n')
            f.flush()
            data = _load_toml(Path(f.name))
        self.assertIsNotNone(data)
        self.assertEqual(data["confab"]["stale_threshold"], 5)

    def test_invalid_toml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write("this is not valid toml {{{}}")
            f.flush()
            data = _load_toml(Path(f.name))
        self.assertIsNone(data)

    def test_nonexistent_file(self):
        data = _load_toml(Path("/nonexistent/path.toml"))
        self.assertIsNone(data)


class TestConfigFromToml(unittest.TestCase):
    """Test config creation from TOML data."""

    def test_full_config(self):
        data = {
            "confab": {
                "files_to_scan": ["priorities.md", "notes.md"],
                "stale_threshold": 5,
                "db_path": "custom.db",
                "pipelines": {
                    "build.py": ["output/"],
                },
                "pipeline_names": {
                    "build pipeline": "build.py",
                },
                "env_vars": {
                    "known": ["MY_API_KEY", "MY_SECRET"],
                },
            }
        }
        root = Path("/tmp/workspace")
        config = _config_from_toml(data, root)

        self.assertEqual(config.files_to_scan, ["priorities.md", "notes.md"])
        self.assertEqual(config.stale_threshold, 5)
        self.assertEqual(config.db_path, root / "custom.db")
        self.assertEqual(config.pipeline_outputs, {"build.py": ["output/"]})
        self.assertEqual(config.pipeline_names, {"build pipeline": "build.py"})
        self.assertEqual(config.known_env_vars, {"MY_API_KEY", "MY_SECRET"})

    def test_minimal_config(self):
        data = {"confab": {}}
        config = _config_from_toml(data, Path("/tmp"))
        self.assertEqual(config.files_to_scan, [])
        self.assertEqual(config.stale_threshold, 3)
        self.assertEqual(config.known_env_vars, set())

    def test_empty_toml(self):
        data = {}
        config = _config_from_toml(data, Path("/tmp"))
        self.assertEqual(config.files_to_scan, [])


class TestLoadConfig(unittest.TestCase):
    """Test load_config with different workspace states."""

    def test_explicit_config_path(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
            f.write('[confab]\nfiles_to_scan = ["custom.md"]\n')
            f.flush()
            config = load_config(
                config_path=Path(f.name),
                workspace_root=Path("/tmp"),
            )
        self.assertEqual(config.files_to_scan, ["custom.md"])

    def test_standalone_defaults(self):
        """Non-ia workspace with no config file gets empty defaults."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config = load_config(workspace_root=Path(tmpdir))
            self.assertEqual(config.files_to_scan, [])
            self.assertEqual(config.known_env_vars, set())


class TestConfigSingleton(unittest.TestCase):
    """Test config singleton behavior."""

    def setUp(self):
        reset_config()

    def tearDown(self):
        reset_config()

    def test_set_and_get(self):
        custom = ConfabConfig(
            workspace_root=Path("/tmp/test"),
            files_to_scan=["test.md"],
        )
        set_config(custom)
        result = get_config()
        self.assertEqual(result.files_to_scan, ["test.md"])

    def test_reset_clears(self):
        custom = ConfabConfig(
            workspace_root=Path("/tmp/test"),
            files_to_scan=["test.md"],
        )
        set_config(custom)
        reset_config()
        # After reset, get_config will reload from disk
        # (may differ from our custom config)
        result = get_config()
        self.assertNotEqual(result.files_to_scan, ["test.md"])


class TestIsIaRepo(unittest.TestCase):
    """Test ia repo detection."""

    def test_ia_repo_detected(self):
        # The actual ia repo root
        ia_root = Path(__file__).resolve().parent.parent.parent.parent
        if (ia_root / "core" / "confab" / "__init__.py").exists():
            self.assertTrue(_is_ia_repo(ia_root))

    def test_random_dir_not_ia(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(_is_ia_repo(Path(tmpdir)))


if __name__ == "__main__":
    unittest.main()
