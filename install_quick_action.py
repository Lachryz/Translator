#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from translator_runtime import resolve_runtime_paths

ROOT = Path(__file__).resolve().parent
SERVICE_NAME = "多段落翻译"
RESULT_APP_NAME = "多段落翻译窗口.app"
RESULT_BUNDLE_ID = "com.zhaoxun.paragraph-translator.window"
SELECTION_CAPTURE_NAME = "selection_capture"
HOTKEY_DAEMON_NAME = "translator_hotkey_daemon"
HOTKEY_APP_NAME = "Translator Hotkey.app"
HOTKEY_AGENT_LABEL = "com.zhaoxun.paragraph-translator.hotkey"
HOTKEY_SIGNING_ID = "com.zhaoxun.paragraph-translator.hotkey"
CAPTURE_SIGNING_ID = "com.zhaoxun.paragraph-translator.capture"
RESULT_EXECUTABLE_NAME = "translation_result_window"
SWIFTC = "/usr/bin/xcrun"
CODESIGN = "/usr/bin/codesign"


def action_document() -> dict:
    input_uuid = str(uuid.uuid4()).upper()
    output_uuid = str(uuid.uuid4()).upper()
    action_uuid = str(uuid.uuid4()).upper()
    command = shlex.quote(str(ROOT / "run_quick_action.sh"))
    return {
        "actions": [{
            "action": {
                "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
                "ActionName": "运行Shell脚本",
                "ActionParameters": {
                    "CheckedForUserDefaultShell": True,
                    "COMMAND_STRING": command,
                    "inputMethod": 0,
                    "shell": "/bin/zsh",
                    "source": "",
                },
                "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.string"]},
                "AMActionVersion": "2.0.3",
                "AMApplication": ["自动操作"],
                "AMParameterProperties": {
                    "CheckedForUserDefaultShell": {}, "COMMAND_STRING": {},
                    "inputMethod": {}, "shell": {}, "source": {},
                },
                "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.string"]},
                "arguments": {
                    "0": {"default value": 0, "name": "inputMethod", "required": "0", "type": "0", "uuid": "0"},
                    "1": {"default value": False, "name": "CheckedForUserDefaultShell", "required": "0", "type": "0", "uuid": "1"},
                    "2": {"default value": "", "name": "source", "required": "0", "type": "0", "uuid": "2"},
                    "3": {"default value": "", "name": "COMMAND_STRING", "required": "0", "type": "0", "uuid": "3"},
                    "4": {"default value": "/bin/sh", "name": "shell", "required": "0", "type": "0", "uuid": "4"},
                },
                "BundleIdentifier": "com.apple.RunShellScript",
                "CanShowSelectedItemsWhenRun": False,
                "CanShowWhenRun": True,
                "Category": ["AMCategoryUtilities"],
                "CFBundleVersion": "2.0.3",
                "Class Name": "RunShellScriptAction",
                "InputUUID": input_uuid,
                "isViewVisible": 1,
                "Keywords": ["Shell", "脚本", "命令", "运行", "Unix"],
                "location": "309.000000:305.000000",
                "nibPath": "/System/Library/Automator/Run Shell Script.action/Contents/Resources/Base.lproj/main.nib",
                "OutputUUID": output_uuid,
                "UnlocalizedApplications": ["Automator"],
                "UUID": action_uuid,
            },
            "isViewVisible": 1,
        }],
        "AMApplicationBuild": "534",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "connectors": {},
        "workflowMetaData": {
            "applicationBundleIDsByPath": {},
            "applicationPaths": [],
            "inputTypeIdentifier": "com.apple.Automator.nothing",
            "outputTypeIdentifier": "com.apple.Automator.nothing",
            "presentationMode": 11,
            "processesInput": False,
            "serviceInputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": False,
            "systemImageName": "NSActionTemplate",
            "useAutomaticInputType": False,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }


def info_plist() -> dict:
    return {
        "NSServices": [{
            "NSBackgroundColorName": "background",
            "NSIconName": "NSActionTemplate",
            "NSMenuItem": {"default": SERVICE_NAME},
            "NSMessage": "runWorkflowAsService",
        }]
    }


def result_app_info_plist() -> dict:
    return {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": "多段落翻译窗口",
        "CFBundleExecutable": RESULT_EXECUTABLE_NAME,
        "CFBundleIdentifier": RESULT_BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "多段落翻译窗口",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }


def hotkey_app_info_plist() -> dict:
    return {
        "CFBundleDevelopmentRegion": "zh_CN",
        "CFBundleDisplayName": "Translator Hotkey",
        "CFBundleExecutable": HOTKEY_DAEMON_NAME,
        "CFBundleIdentifier": HOTKEY_SIGNING_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "Translator Hotkey",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSBackgroundOnly": True,
        "LSUIElement": True,
        "NSAccessibilityUsageDescription": (
            "Read the current text selection and copy it when an app "
            "does not expose selected text."
        ),
    }


def write_plist(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump(payload, stream)


def compile_swift(
    source: Path,
    output: Path,
    frameworks: tuple[str, ...],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [SWIFTC, "swiftc", "-O"]
    for framework in frameworks:
        command.extend(("-framework", framework))
    command.extend((str(source), "-o", str(output)))
    subprocess.run(command, check=True)


def sign_adhoc(path: Path, identifier: str) -> None:
    subprocess.run(
        [
            CODESIGN,
            "--force",
            "--sign",
            "-",
            "--identifier",
            identifier,
            "--requirements",
            f'=designated => identifier "{identifier}"',
            str(path),
        ],
        check=True,
    )


def replace_artifact(staged: Path, destination: Path) -> None:
    """Replace one build artifact while retaining the old copy on failure."""
    backup = destination.with_name(f".{destination.name}.install-backup")
    if backup.is_dir():
        shutil.rmtree(backup)
    elif backup.exists():
        backup.unlink()
    if destination.exists():
        os.replace(destination, backup)
    try:
        os.replace(staged, destination)
    except Exception:
        if backup.exists():
            os.replace(backup, destination)
        raise
    if backup.is_dir():
        shutil.rmtree(backup)
    elif backup.exists():
        backup.unlink()


def build_artifacts(build_dir: Path) -> Path:
    """Build and sign everything in staging before replacing working artifacts."""
    build_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=build_dir) as temporary:
        stage = Path(temporary)
        result_app = stage / RESULT_APP_NAME
        result_executable = result_app / "Contents" / "MacOS" / RESULT_EXECUTABLE_NAME
        compile_swift(
            ROOT / "result_window.swift",
            result_executable,
            ("AppKit",),
        )
        write_plist(result_app / "Contents" / "Info.plist", result_app_info_plist())
        sign_adhoc(result_app, RESULT_BUNDLE_ID)

        hotkey_app = stage / HOTKEY_APP_NAME
        hotkey_executable = (
            hotkey_app / "Contents" / "MacOS" / HOTKEY_DAEMON_NAME
        )
        compile_swift(
            ROOT / "hotkey_daemon.swift",
            hotkey_executable,
            ("AppKit", "ApplicationServices", "Carbon"),
        )
        write_plist(hotkey_app / "Contents" / "Info.plist", hotkey_app_info_plist())
        sign_adhoc(hotkey_app, HOTKEY_SIGNING_ID)

        selection_capture = stage / SELECTION_CAPTURE_NAME
        compile_swift(
            ROOT / "selection_capture.swift",
            selection_capture,
            ("AppKit", "ApplicationServices"),
        )
        sign_adhoc(selection_capture, CAPTURE_SIGNING_ID)

        installed_result_app = build_dir / RESULT_APP_NAME
        installed_hotkey_app = build_dir / HOTKEY_APP_NAME
        installed_selection_capture = build_dir / SELECTION_CAPTURE_NAME
        replace_artifact(result_app, installed_result_app)
        replace_artifact(hotkey_app, installed_hotkey_app)
        replace_artifact(selection_capture, installed_selection_capture)

    legacy_hotkey_binary = build_dir / HOTKEY_DAEMON_NAME
    if legacy_hotkey_binary.is_file() or legacy_hotkey_binary.is_symlink():
        legacy_hotkey_binary.unlink()

    return installed_hotkey_app / "Contents" / "MacOS" / HOTKEY_DAEMON_NAME


def write_runtime_environment(
    build_dir: Path,
    *,
    config_path: Path,
    python_path: Path,
) -> Path:
    """Cache resolved paths so each shortcut avoids an extra Python startup."""
    destination = build_dir / "runtime.env"
    with tempfile.TemporaryDirectory(prefix=".runtime-", dir=build_dir) as temporary:
        staged = Path(temporary) / "runtime.env"
        staged.write_text(
            "# Generated by install_quick_action.py; contains no API credentials.\n"
            f"_TRANSLATOR_INSTALLED_CONFIG={shlex.quote(str(config_path))}\n"
            f"_TRANSLATOR_INSTALLED_PYTHON={shlex.quote(str(python_path))}\n",
            encoding="utf-8",
        )
        replace_artifact(staged, destination)
    return destination


def hotkey_launch_agent(
    daemon_path: Path,
    launcher_path: Path,
    log_path: Path,
) -> dict:
    return {
        "Label": HOTKEY_AGENT_LABEL,
        "ProgramArguments": [str(daemon_path), str(launcher_path)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def install_service() -> Path:
    service_dir = Path.home() / "Library/Services" / f"{SERVICE_NAME}.workflow"
    service_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".translator-", dir=service_dir.parent) as temporary:
        staged = Path(temporary) / service_dir.name
        contents = staged / "Contents"
        write_plist(contents / "document.wflow", action_document())
        write_plist(contents / "Info.plist", info_plist())
        replace_artifact(staged, service_dir)

    service_key = f"(null) - {SERVICE_NAME} - runWorkflowAsService"
    # The background agent owns the shortcut. Leaving the same key equivalent on
    # the Services item could launch a duplicate request in apps that support it.
    shortcut_value = (
        '{ "presentation_modes" = { '
        '"ContextMenu" = 1; "ServicesMenu" = 1; "TouchBar" = 1; }; }'
    )
    subprocess.run(
        [
            "/usr/bin/defaults", "write", "pbs", "NSServicesStatus", "-dict-add",
            f'"{service_key}"', shortcut_value,
        ],
        check=True,
    )
    subprocess.run(["/System/Library/CoreServices/pbs", "-flush"], check=True)
    subprocess.run(["/System/Library/CoreServices/pbs", "-update"], check=True)
    return service_dir


def install_hotkey_agent(hotkey_daemon_path: Path) -> Path:
    logs_dir = Path.home() / "Library/Logs/Translator"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "hotkey.log"
    launch_agents_dir = Path.home() / "Library/LaunchAgents"
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    launch_agent_path = launch_agents_dir / f"{HOTKEY_AGENT_LABEL}.plist"
    write_plist(
        launch_agent_path,
        hotkey_launch_agent(
            hotkey_daemon_path,
            ROOT / "run_quick_action.sh",
            log_path,
        ),
    )

    domain = f"gui/{os.getuid()}"
    subprocess.run(
        ["/bin/launchctl", "bootout", domain, str(launch_agent_path)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(launch_agent_path)],
        check=True,
    )
    return launch_agent_path


def print_summary(rows: tuple[tuple[str, str], ...]) -> None:
    """Keep ASCII field labels and value starts aligned across the whole block."""
    label_width = max(len(label) for label, _ in rows)
    separator = "═" * max(1, shutil.get_terminal_size((80, 24)).columns)
    print(separator)
    for label, value in rows:
        print(f"{label:<{label_width}} : {value}")
    print(separator)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build and install the macOS Translator shortcut.")
    parser.add_argument("--config", type=Path, help="path to translator_config.json")
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="build and sign artifacts without registering Services or restarting the hotkey agent",
    )
    args = parser.parse_args(argv)
    paths = resolve_runtime_paths(args.config)
    if not args.build_only and not (
        paths.python_path.is_file() and os.access(paths.python_path, os.X_OK)
    ):
        parser.error(
            f"Python interpreter is not executable: {paths.python_path}. "
            "Run uv sync, set TRANSLATOR_PYTHON, or use --build-only."
        )
    build_dir = ROOT / "build"
    build_dir.mkdir(exist_ok=True)
    hotkey_daemon_path = build_artifacts(build_dir)
    runtime_path = write_runtime_environment(
        build_dir,
        config_path=paths.config_path,
        python_path=paths.python_path,
    )
    if args.build_only:
        print_summary((("built", str(build_dir)), ("runtime paths", str(runtime_path))))
        return
    service_dir = install_service()
    launch_agent_path = install_hotkey_agent(hotkey_daemon_path)

    print_summary((
        ("built", str(build_dir)),
        ("installed", str(service_dir)),
        ("hotkey agent", str(launch_agent_path)),
        ("global shortcut", "Control+Option+Command+T"),
        ("input", "explicit stdin; accessibility selection; Command+C after no selection"),
        ("runtime paths", str(runtime_path)),
    ))


if __name__ == "__main__":
    main()
