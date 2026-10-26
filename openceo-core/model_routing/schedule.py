"""Refresh scheduling: one user-crontab source (TASK-2026-00292).

A single marked block in the user crontab owns both refresh jobs
(Cron B every 6 h, Cron A daily); reinstalling replaces the block
instead of duplicating it. Dry-run is the default; ``--install`` and
``--uninstall`` rewrite the crontab. Activating the jobs on the host
belongs to TASK-2026-00299.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

if __name__ == "__main__":
    _CORE = str(Path(__file__).resolve().parents[1])
    if _CORE not in sys.path:
        sys.path.insert(0, _CORE)

from model_routing.paths import UPDATER_LOG_FILENAME  # noqa: E402
from model_routing.paths import state_dir as default_state_dir  # noqa: E402

__all__ = [
    "CRON_MARK_BEGIN",
    "CRON_MARK_END",
    "current_crontab",
    "install_block",
    "main",
    "remove_block",
    "render_crontab_block",
    "save_crontab",
]

CRON_MARK_BEGIN = "# BEGIN free-router refresh (model_routing)"
CRON_MARK_END = "# END free-router refresh"


def render_crontab_block(python_exe: str, cli_path: str, state: str) -> str:
    """Two jobs logging to the state directory.

    No outer ``flock`` wrapper: ``run_update`` already takes the
    non-blocking in-process lock, and a wrapper on the same file would
    self-lock every scheduled run.
    """
    log = str(Path(state) / UPDATER_LOG_FILENAME)
    lines = [
        CRON_MARK_BEGIN,
        "# Host-local times; confirm the zone with `timedatectl` at activation.",
        "# Mutual exclusion lives in-process (run_update flock on update.lock).",
        f"15 2 * * * {python_exe} {cli_path} update --benchmarks >> {log} 2>&1",
        f"20 */6 * * * {python_exe} {cli_path} update --providers >> {log} 2>&1",
        CRON_MARK_END,
    ]
    return "\n".join(lines) + "\n"


def _split_around(text: str) -> tuple[str, str, str] | None:
    begin = text.find(CRON_MARK_BEGIN)
    end = text.find(CRON_MARK_END)
    if begin < 0 or end < 0 or end < begin:
        return None
    tail_start = end + len(CRON_MARK_END)
    if tail_start < len(text) and text[tail_start] == "\n":
        tail_start += 1
    return text[:begin], text[begin:tail_start], text[tail_start:]


def install_block(crontab_text: str, block: str) -> str:
    """Add the refresh block, replacing any previous one (idempotent)."""
    found = _split_around(crontab_text)
    if found is None:
        base = crontab_text if crontab_text.endswith("\n") or not crontab_text else crontab_text + "\n"
        return base + block
    head, _old, tail = found
    return head + block + tail


def remove_block(crontab_text: str) -> str:
    """Drop the refresh block; text without it is unchanged."""
    found = _split_around(crontab_text)
    if found is None:
        return crontab_text
    head, _old, tail = found
    return head + tail


def current_crontab(reader: Callable[[], str] | None = None) -> str:
    """Current user crontab, empty when none is installed."""
    if reader is not None:
        return reader()
    try:
        completed = subprocess.run(
            ["crontab", "-l"], check=True, text=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""
    return completed.stdout


def save_crontab(text: str, writer: Callable[[str], None] | None = None) -> None:
    """Replace the user crontab with ``text``."""
    if writer is not None:
        writer(text)
        return
    subprocess.run(["crontab", "-"], input=text, check=True, text=True)


def build_parser() -> argparse.ArgumentParser:
    """Installer parser; dry-run stays the default."""
    parser = argparse.ArgumentParser(prog="free-router-schedule")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true",
                        help="Show the block and the planned change (default)")
    action.add_argument("--install", action="store_true",
                        help="Write the refresh block into the user crontab")
    action.add_argument("--uninstall", action="store_true",
                        help="Remove the refresh block from the user crontab")
    parser.add_argument("--python", default=sys.executable, help="Python running the CLI")
    parser.add_argument("--cli", default=str(Path(__file__).resolve().parent / "cli.py"),
                        help="free-router CLI path")
    parser.add_argument("--state-dir", default=str(default_state_dir()),
                        help="State directory for the lock and the log")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dry-run, install or uninstall the refresh block."""
    args = build_parser().parse_args(argv)
    block = render_crontab_block(args.python, args.cli, args.state_dir)
    if not args.install and not args.uninstall:
        print(block, end="")
        print("dry-run: no crontab change (use --install to apply)")
        return 0
    current = current_crontab()
    if args.install:
        save_crontab(install_block(current, block))
        print("installed free-router refresh block in the user crontab")
        return 0
    if _split_around(current) is None:
        print("no free-router refresh block installed; nothing to remove")
        return 0
    save_crontab(remove_block(current))
    print("removed free-router refresh block from the user crontab")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
