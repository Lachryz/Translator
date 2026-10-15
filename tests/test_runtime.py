from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import translator_runtime
from translator_runtime import DEFAULT_CONFIG, resolve_config_path, resolve_runtime_paths


class RuntimePathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="translator runtime ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / "translator_config.json"
        self.config.write_text(json.dumps({"env_file": None}))
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_defaults_use_local_credentials_and_project_environment(self):
        paths = resolve_runtime_paths(self.config)
        self.assertEqual(paths.env_file, self.root / ".env")
        self.assertEqual(paths.python_path, translator_runtime.ROOT / ".venv/bin/python")

    def test_omitted_env_file_is_beside_config(self):
        self.assertEqual(resolve_runtime_paths(self.config, config={}).env_file, self.root / ".env")

    def test_explicit_env_file_is_relative_to_config(self):
        paths = resolve_runtime_paths(self.config, config={"env_file": "private/.env"})
        self.assertEqual(paths.env_file, self.root / "private/.env")

    def test_overrides_win_without_reading_credentials(self):
        with patch.dict(os.environ, {
            "TRANSLATOR_CONFIG": str(self.config),
            "TRANSLATOR_ENV_FILE": "private/key.env",
            "TRANSLATOR_PYTHON": "python/bin/python",
        }):
            paths = resolve_runtime_paths()
        self.assertEqual(paths.config_path, self.config)
        self.assertEqual(paths.env_file, self.root / "private/key.env")
        self.assertEqual(paths.python_path, self.root / "python/bin/python")

    def test_explicit_config_beats_environment(self):
        with patch.dict(os.environ, {"TRANSLATOR_CONFIG": "/unused/config.json"}):
            self.assertEqual(resolve_config_path(self.config), self.config)
        self.assertEqual(resolve_config_path(), DEFAULT_CONFIG)

    def test_home_paths_are_expanded(self):
        paths = resolve_runtime_paths(self.config, config={"env_file": "~/private/.env"})
        self.assertEqual(paths.env_file, Path.home() / "private/.env")

    def test_python_override_preserves_virtualenv_symlink(self):
        executable = self.root / "python"
        executable.symlink_to("/usr/bin/python3")
        with patch.dict(os.environ, {"TRANSLATOR_PYTHON": str(executable)}):
            self.assertEqual(resolve_runtime_paths(self.config).python_path, executable)

    def test_malformed_or_empty_paths_fail_clearly(self):
        for value in ([], "", 42):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_runtime_paths(self.config, config={"env_file": value})
        with self.assertRaisesRegex(ValueError, "JSON object"):
            resolve_runtime_paths(self.config, config=[])


if __name__ == "__main__":
    unittest.main()
