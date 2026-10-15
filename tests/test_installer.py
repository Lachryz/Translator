from __future__ import annotations

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import install_quick_action as installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Translator project's files"
        self.root.mkdir()

    def test_workflow_quotes_launcher_path_as_one_shell_command(self):
        root = self.root / "special $(no-command)"
        with mock.patch.object(installer, "ROOT", root):
            command = installer.action_document()["actions"][0]["action"]["ActionParameters"]["COMMAND_STRING"]
        self.assertEqual(shlex.split(command), [str(root / "run_quick_action.sh")])

    def test_summary_aligns_fields_punctuation_paths_and_full_width_rules(self):
        output = io.StringIO()
        with (
            mock.patch.object(installer.shutil, "get_terminal_size", return_value=os.terminal_size((72, 24))),
            redirect_stdout(output),
        ):
            installer.print_summary((
                ("built", str(self.root / "build")),
                ("runtime paths", str(self.root / "build/runtime.env")),
            ))
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "═" * 72)
        self.assertEqual(lines[-1], lines[0])
        self.assertEqual([line.index(":") for line in lines[1:-1]], [14, 14])
        self.assertEqual([line.index(str(self.root)) for line in lines[1:-1]], [16, 16])
        self.assertNotIn("\t", output.getvalue())

    def test_runtime_environment_round_trips_shell_metacharacters(self):
        config = self.root / "config's $(not-executed).json"
        python = self.root / "python path; 'quoted'"
        runtime = installer.write_runtime_environment(
            self.root, config_path=config, python_path=python,
        )
        result = subprocess.run(
            [
                "/bin/zsh", "-c",
                'source "$1"; printf "%s\\0%s" "$_TRANSLATOR_INSTALLED_CONFIG" "$_TRANSLATOR_INSTALLED_PYTHON"',
                "runtime-test", str(runtime),
            ],
            check=True, capture_output=True,
        )
        self.assertEqual(result.stdout.split(b"\0"), [os.fsencode(config), os.fsencode(python)])

    def test_build_only_does_not_require_python_or_register_system_services(self):
        paths = SimpleNamespace(
            config_path=self.root / "custom.json",
            python_path=self.root / "missing-python",
        )
        with (
            mock.patch.object(installer, "ROOT", self.root),
            mock.patch.object(installer, "resolve_runtime_paths", return_value=paths) as resolve,
            mock.patch.object(installer, "build_artifacts") as build,
            mock.patch.object(installer, "install_service") as service,
            mock.patch.object(installer, "install_hotkey_agent") as agent,
            redirect_stdout(io.StringIO()),
        ):
            installer.main(["--build-only", "--config", str(paths.config_path)])
        resolve.assert_called_once_with(paths.config_path)
        build.assert_called_once_with(self.root / "build")
        service.assert_not_called()
        agent.assert_not_called()
        self.assertTrue((self.root / "build/runtime.env").is_file())

    def test_install_rejects_missing_python_before_building(self):
        paths = SimpleNamespace(python_path=self.root / "missing-python")
        with (
            mock.patch.object(installer, "resolve_runtime_paths", return_value=paths),
            mock.patch.object(installer, "build_artifacts") as build,
            redirect_stdout(io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            installer.main([])
        self.assertEqual(raised.exception.code, 2)
        build.assert_not_called()

    def test_install_summary_aligns_paths_and_reports_installed_artifacts(self):
        paths = SimpleNamespace(
            config_path=self.root / "custom.json", python_path=Path(sys.executable),
        )
        daemon = self.root / "build/hotkey"
        service_path = self.root / "Library/Services/翻译.workflow"
        agent_path = self.root / "Library/LaunchAgents/hotkey.plist"
        output = io.StringIO()
        with (
            mock.patch.object(installer, "ROOT", self.root),
            mock.patch.object(installer, "resolve_runtime_paths", return_value=paths),
            mock.patch.object(installer, "build_artifacts", return_value=daemon),
            mock.patch.object(installer, "install_service", return_value=service_path) as service,
            mock.patch.object(installer, "install_hotkey_agent", return_value=agent_path) as agent,
            redirect_stdout(output),
        ):
            installer.main([])
        service.assert_called_once_with()
        agent.assert_called_once_with(daemon)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], lines[-1])
        fields = [line.split(" : ", 1) for line in lines[1:-1]]
        self.assertEqual(
            [label.rstrip() for label, _ in fields],
            ["built", "installed", "hotkey agent", "global shortcut", "input", "runtime paths"],
        )
        self.assertEqual(len({line.index(" : ") for line in lines[1:-1]}), 1)
        self.assertEqual(fields[1][1], str(service_path))
        self.assertEqual(fields[2][1], str(agent_path))
        self.assertEqual(fields[-1][1], str(self.root / "build/runtime.env"))

    def test_compile_failure_preserves_all_existing_artifacts(self):
        build_dir = self.root / "build"
        original = {
            installer.RESULT_APP_NAME: "result-window",
            installer.HOTKEY_APP_NAME: "hotkey-app",
            installer.SELECTION_CAPTURE_NAME: "selection-helper",
        }
        for name, content in original.items():
            destination = build_dir / name
            if name.endswith(".app"):
                destination /= "original.txt"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)

        def compile_or_fail(source, output, frameworks):
            if source.name == "selection_capture.swift":
                raise RuntimeError("test compile failure")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("compiled")

        with (
            mock.patch.object(installer, "compile_swift", side_effect=compile_or_fail),
            mock.patch.object(installer, "sign_adhoc"),
            self.assertRaisesRegex(RuntimeError, "test compile failure"),
        ):
            installer.build_artifacts(build_dir)
        for name, content in original.items():
            destination = build_dir / name
            if name.endswith(".app"):
                destination /= "original.txt"
            self.assertEqual(destination.read_text(), content)
        self.assertEqual(list(build_dir.glob(".install-*")), [])

    def test_artifact_replacement_restores_original_after_rename_failure(self):
        destination = self.root / "installed"
        staged = self.root / "staged"
        destination.write_text("original")
        staged.write_text("replacement")
        replace = os.replace
        calls = 0

        def move(source, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("test replacement failure")
            return replace(source, target)

        with (
            mock.patch.object(installer.os, "replace", side_effect=move),
            self.assertRaisesRegex(OSError, "test replacement failure"),
        ):
            installer.replace_artifact(staged, destination)
        self.assertEqual(destination.read_text(), "original")
        self.assertEqual(staged.read_text(), "replacement")

    def test_service_staging_failure_preserves_existing_workflow(self):
        service = self.root / "Library/Services" / f"{installer.SERVICE_NAME}.workflow"
        original = service / "Contents/document.wflow"
        original.parent.mkdir(parents=True)
        original.write_text("original workflow")
        with (
            mock.patch.object(Path, "home", return_value=self.root),
            mock.patch.object(installer, "write_plist", side_effect=OSError("test write failure")),
            mock.patch.object(installer.subprocess, "run") as run,
            self.assertRaisesRegex(OSError, "test write failure"),
        ):
            installer.install_service()
        self.assertEqual(original.read_text(), "original workflow")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
