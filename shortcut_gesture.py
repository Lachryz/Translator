#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _read_owner(owner_file: Path) -> int | None:
    try:
        return int(owner_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def _remove_state(state_dir: Path, owner_pid: int | None = None) -> None:
    owner_file = state_dir / "owner"
    if owner_pid is not None and _read_owner(owner_file) != owner_pid:
        return
    for name in ("double", "owner"):
        try:
            (state_dir / name).unlink()
        except FileNotFoundError:
            pass
    try:
        state_dir.rmdir()
    except (FileNotFoundError, OSError):
        pass


def detect_gesture(
    state_dir: Path, window_seconds: float, ready_file: Path | None = None,
) -> str:
    """Preserve paragraphs on one press; use sentence lists on a rapid double press."""
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(window_seconds, 0.05)
    while True:
        try:
            state_dir.mkdir()
        except FileExistsError:
            owner_file = state_dir / "owner"
            owner_pid = _read_owner(owner_file)
            try:
                age = time.time() - state_dir.stat().st_mtime
            except FileNotFoundError:
                continue
            if owner_pid is not None and age <= 5 and _process_is_alive(owner_pid):
                (state_dir / "double").touch(exist_ok=True)
                if ready_file is not None:
                    ready_file.write_text("secondary", encoding="ascii")
                return "secondary"
            if owner_pid is None and time.monotonic() < deadline:
                time.sleep(0.01)
                continue
            _remove_state(state_dir)
            if time.monotonic() >= deadline:
                if ready_file is not None:
                    ready_file.write_text("leader", encoding="ascii")
                return "preserve"
            continue

        owner_pid = os.getpid()
        (state_dir / "owner").write_text(str(owner_pid), encoding="ascii")
        try:
            if ready_file is not None:
                ready_file.write_text("leader", encoding="ascii")
            time.sleep(max(window_seconds, 0.05))
            return "normal" if (state_dir / "double").exists() else "preserve"
        finally:
            _remove_state(state_dir, owner_pid)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect a rapid double invocation")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--window", type=float, default=0.45)
    parser.add_argument("--ready-file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(detect_gesture(args.state_dir, args.window, args.ready_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
