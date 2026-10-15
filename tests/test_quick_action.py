from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import venv

ROOT = Path(__file__).resolve().parents[1]

HELPER = '''import json, os, sys, time
from pathlib import Path

def record(event, **fields):
    with open(os.environ["TEST_EVENTS"], "a") as stream:
        stream.write(json.dumps(dict(event=event, time=time.monotonic(), **fields)) + "\\n")

name = Path(sys.argv[0]).name
if name == "selection_capture":
    record("capture_start", python_prefix=sys.prefix)
    time.sleep(0.25)
    record("capture_end")
    print("Captured selection.", end="")
elif name == "paragraph_translator.py":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", type=Path)
    parser.add_argument("--gesture-file", type=Path)
    parser.add_argument("--stream-file", type=Path)
    args = parser.parse_args()
    record("engine_start", config=os.environ.get("TRANSLATOR_CONFIG"), python_prefix=sys.prefix)
    source = sys.stdin.read()
    record("request_start", source=source)
    if os.environ.get("TEST_ERROR"):
        print("Expected test failure", file=sys.stderr)
        sys.exit(1)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        gesture = args.gesture_file.read_text().strip()
        if gesture in ("normal", "preserve"):
            break
        time.sleep(0.01)
    record("format", gesture=gesture)
    args.status_file.write_text(json.dumps(dict(mode=os.environ.get("TEST_MODE", "单段落快速翻译")), ensure_ascii=False, separators=(",", ":")))
    print("Test translation", end="")
else:
    record("window", mode=sys.argv[1], python_prefix=sys.prefix)
    path = Path(sys.argv[2])
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and path.exists():
        try:
            status = json.loads(path.read_text())
        except (ValueError, OSError):
            time.sleep(0.01)
            continue
        if "completed" in status:
            payload = Path(status["path"])
            record("completed", mode=status["completed"], text=payload.read_text(), layout=status["layout"])
            payload.unlink()
            path.unlink()
            break
        time.sleep(0.01)
'''


class QuickActionPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Translator project 'quoted'"
        self.root.mkdir()
        self.log = self.root / "events.jsonl"
        self.env = dict(os.environ, TEST_EVENTS=str(self.log))
        for key in tuple(self.env):
            if key.startswith("TRANSLATOR_"):
                self.env.pop(key)
        self.env["TRANSLATOR_PYTHON"] = sys.executable
        self.env["PATH"] = os.pathsep.join((str(Path(sys.executable).parent), self.env.get("PATH", "")))
        self.env["TRANSLATOR_LOG_DIR"] = str(self.root / "logs")
        script = (ROOT / "run_quick_action.sh").read_text()
        script = script.replace('GESTURE_STATE_DIR="${TMPDIR:-/tmp}/com.zhaoxun.paragraph-translator.shortcut-${UID}"', f'GESTURE_STATE_DIR="{self.root / "gesture-state"}"')
        (self.root / "run_quick_action.sh").write_text(script)
        shutil.copy2(ROOT / "shortcut_gesture.py", self.root)
        for name in ("paragraph_translator.py", "build/selection_capture", "build/多段落翻译窗口.app/Contents/MacOS/translation_result_window"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/usr/bin/env python3\n" + HELPER)
            path.chmod(0o755)

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def wait_for(self, name):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            events = [event for event in self.events() if event["event"] == name]
            if events:
                return events
            time.sleep(0.01)
        self.fail(f"Missing {name}: {self.events()}")

    def run_action(self, source=""):
        return subprocess.run(["/bin/zsh", str(self.root / "run_quick_action.sh")], input=source, text=True, capture_output=True, env=self.env, timeout=5)

    def test_popup_and_engine_do_not_wait_for_capture_or_double_press(self):
        result = self.run_action()
        self.assertEqual(result.returncode, 0, result.stderr)
        completed = self.wait_for("completed")[0]
        events = {event["event"]: event for event in self.events()}
        self.assertLess(events["window"]["time"], events["capture_end"]["time"])
        self.assertLess(events["engine_start"]["time"], events["capture_end"]["time"])
        self.assertLess(events["request_start"]["time"], events["format"]["time"])
        self.assertEqual(events["request_start"]["source"], "Captured selection.")
        self.assertEqual(completed["text"], "Test translation")
        self.assertEqual(completed["layout"], "single")
        self.assertEqual(len([e for e in self.events() if e["event"] == "window"]), 1)

    def test_background_pipeline_preserves_explicit_stdin(self):
        result = self.run_action("Explicit input.\nSecond paragraph.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for("completed")
        self.assertFalse(any(e["event"] == "capture_start" for e in self.events()))
        request = self.wait_for("request_start")[0]
        self.assertEqual(request["source"], "Explicit input.\nSecond paragraph.")
        self.assertEqual(self.wait_for("format")[0]["gesture"], "preserve")

    def test_second_press_changes_format_without_duplicate_window_or_request(self):
        first = subprocess.Popen(["/bin/zsh", str(self.root / "run_quick_action.sh")], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)

        def cleanup_process():
            if first.poll() is None:
                first.kill()
            first.communicate(timeout=5)

        self.addCleanup(cleanup_process)
        self.wait_for("window")
        second = self.run_action()
        _, error = first.communicate(timeout=5)
        self.assertEqual(first.returncode, 0, error)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.wait_for("completed")
        self.assertEqual(self.wait_for("format")[0]["gesture"], "normal")
        for name in ("window", "engine_start", "request_start"):
            self.assertEqual(len([e for e in self.events() if e["event"] == name]), 1)

    def test_preserved_paragraph_layout_survives_completion_handoff(self):
        self.env["TEST_MODE"] = "多段落综合翻译（保留原段落）"
        result = self.run_action("First paragraph.\nSecond paragraph.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wait_for("completed")[0]["layout"], "paragraphs")

    def test_sentence_list_layout_survives_completion_handoff(self):
        self.env["TEST_MODE"] = "多段落综合翻译"
        result = self.run_action("First paragraph.\nSecond paragraph.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.wait_for("completed")[0]["layout"], "multi")

    def test_error_reuses_progress_window(self):
        self.env["TEST_ERROR"] = "1"
        result = self.run_action("Explicit input.")
        self.assertEqual(result.returncode, 1, result.stderr)
        completed = self.wait_for("completed")[0]
        self.assertEqual(completed["mode"], "error")
        self.assertIn("Expected test failure", completed["text"])
        self.assertEqual(len([e for e in self.events() if e["event"] == "window"]), 1)

    def test_installed_runtime_paths_preserve_spaces_and_quotes(self):
        self.env.pop("TRANSLATOR_PYTHON")
        config = self.root / "custom config's.json"
        (self.root / "build/runtime.env").write_text(
            f"_TRANSLATOR_INSTALLED_CONFIG={shlex.quote(str(config))}\n"
            f"_TRANSLATOR_INSTALLED_PYTHON={shlex.quote(sys.executable)}\n"
        )
        result = self.run_action("Explicit input.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for("completed")
        self.assertEqual(self.wait_for("engine_start")[0]["config"], str(config.resolve()))

    def test_relative_python_override_uses_selected_config_directory(self):
        config = self.root / "configs/selected.json"
        config.parent.mkdir()
        binary = self.root / "bin/python"
        binary.parent.mkdir()
        binary.symlink_to(sys.executable)
        self.env["TRANSLATOR_CONFIG"] = str(config)
        self.env["TRANSLATOR_PYTHON"] = "../bin/python"
        result = self.run_action("Explicit input.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for("completed")
        self.assertEqual(self.wait_for("engine_start")[0]["config"], str(config.resolve()))

    def test_default_python_uses_the_project_virtual_environment(self):
        self.env.pop("TRANSLATOR_PYTHON")
        binary = self.root / ".venv/bin/python"
        binary.parent.mkdir(parents=True)
        binary.symlink_to(sys.executable)
        result = self.run_action("Explicit input.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for("completed")

    def test_explicit_overrides_win_over_installed_runtime_paths(self):
        config = self.root / "selected config.json"
        self.env["TRANSLATOR_CONFIG"] = str(config)
        (self.root / "build/runtime.env").write_text(
            "_TRANSLATOR_INSTALLED_CONFIG=/unused/config.json\n"
            "_TRANSLATOR_INSTALLED_PYTHON=/unused/python\n"
        )
        result = self.run_action("Explicit input.")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for("completed")
        self.assertEqual(self.wait_for("engine_start")[0]["config"], str(config.resolve()))

    def test_missing_python_fails_before_capture_or_window(self):
        self.env["TRANSLATOR_PYTHON"] = str(self.root / "missing python")
        result = self.run_action("Explicit input.")
        self.assertEqual(result.returncode, 1)
        self.assertIn("TRANSLATOR_PYTHON", result.stderr)
        self.assertEqual(self.events(), [])

    def test_virtual_environment_python_keeps_its_site_packages_context(self):
        environment = self.root / "isolated python"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
        self.env["TRANSLATOR_PYTHON"] = str(environment / "bin/python")
        self.env["PATH"] = os.pathsep.join((str(environment / "bin"), self.env["PATH"]))
        result = self.run_action()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for("completed")
        for event in ("engine_start", "capture_start", "window"):
            with self.subTest(event=event):
                prefix = Path(self.wait_for(event)[0]["python_prefix"])
                self.assertEqual(prefix.resolve(), environment.resolve())


if __name__ == "__main__":
    unittest.main()
