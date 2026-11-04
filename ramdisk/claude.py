"""Claude Code's (and the VSCode server's) scratch/session data on the ramdisk.

Policy: ~/.claude/projects -- transcripts (/resume) AND memory/ -- lives on RAM and is lost at
every reboot. Switching on does NOT migrate: existing directories are renamed aside to
`*.disk-<stamp>`; nothing is ever deleted.

Must run with Claude Code and the VSCode server stopped: shell-snapshots holds the snapshot a
live session sources for every command and ide/ holds its lock files. After `on`, the login unit
re-applies this at every login (before any session starts), which also repairs a symlink an
update clobbered back into a directory.
"""
from __future__ import annotations

from pathlib import Path

from . import unit
from .core import (Ctx, Fail, check_one, link_one, report_leftovers, require_not_running,
                   require_run_dir, say, unlink_one)

AGENT = "claude"

# ramdisk directory -> path under $HOME that should point at it.
# "(absent today)": no counterpart on disk yet; linked anyway so a future write lands on RAM.
# Deliberately NOT here (stay on disk):
#   ~/.claude.json, ~/.claude/history.jsonl  single files rewritten via temp+rename, which would
#                                            replace a symlink with a real file on the next save
#   settings.json, plugins/, skills/, .credentials.json   configuration, not scratch
MAP = (
    ("claude-projects", ".claude/projects"),              # transcripts + memory/
    ("claude-shell-snapshots", ".claude/shell-snapshots"),
    ("claude-sessions", ".claude/sessions"),
    ("claude-ide", ".claude/ide"),
    ("claude-session-env", ".claude/session-env"),
    ("claude-file-history", ".claude/file-history"),
    ("claude-backups", ".claude/backups"),
    ("claude-cache", ".claude/cache"),
    ("claude-downloads", ".claude/downloads"),
    ("claude-state", ".claude/state"),                    # (absent today)
    ("claude-todos", ".claude/todos"),                    # (absent today)
    ("claude-tasks", ".claude/tasks"),                    # (absent today)
    ("claude-plans", ".claude/plans"),                    # (absent today)
    ("claude-debug", ".claude/debug"),                    # (absent today)
    ("claude-paste-cache", ".claude/paste-cache"),        # (absent today)
    ("claude-statsig", ".claude/statsig"),                # (absent today)
    ("claude-telemetry", ".claude/telemetry"),            # (absent today)
    ("vscode-logs", ".vscode-server/data/logs"),
)
LEFTOVER_DIRS = (".claude", ".vscode-server/data")


def _entries(ctx: Ctx) -> list[tuple[str, Path]]:
    return [(name, ctx.home / rel) for name, rel in MAP]


def _guard(ctx: Ctx) -> None:
    require_not_running(ctx, "claude", "Claude Code")


def on(ctx: Ctx) -> None:
    say(f"ramdisk root: {ctx.root}")
    require_run_dir(ctx)
    _guard(ctx)
    unit.install(ctx)
    for name, target in _entries(ctx):
        link_one(ctx, name, target)
    ctx.mark_enabled(AGENT)
    n = ctx.n
    say()
    say(f"already linked: {n.ok}   new links: {n.new}   moved aside: {n.moved}   skipped: {n.skip}")
    if n.moved and not ctx.dry:
        report_leftovers(*(ctx.home / d for d in LEFTOVER_DIRS))


def off(ctx: Ctx) -> None:
    say(f"ramdisk root: {ctx.root}")
    _guard(ctx)
    for name, target in _entries(ctx):
        unlink_one(ctx, name, target)
    ctx.mark_disabled(AGENT)
    n = ctx.n
    say()
    say(f"restored: {n.new}   already on disk: {n.ok}   skipped: {n.skip}")
    say("(the login unit stays for other agents; ramdisk off removes everything)")


def check(ctx: Ctx) -> None:
    say(f"ramdisk root: {ctx.root}")
    if not ctx.enabled(AGENT):
        say("INFO claude is not switched on (ramdisk claude on)")
    for name, target in _entries(ctx):
        check_one(ctx, name, target)
    unit.check(ctx)
    report_leftovers(*(ctx.home / d for d in LEFTOVER_DIRS))
    say()
    if ctx.n.fail:
        raise Fail(f"RESULT: FAIL ({ctx.n.fail} problem(s))")
    say("RESULT: OK -- Claude Code's scratch data is on the ramdisk")
