"""Codex CLI's scratch/session data on the ramdisk.

Two mechanisms:
  1. directories -> symlinks into the ramdisk (same as for Claude Code)
  2. Codex's sqlite databases (state, thread history, memories, goals, queue) and its log sink
     cannot be symlinked safely (WAL/SHM side files, rename-on-write), so they are moved with
     Codex's own config keys `sqlite_home` and `log_dir` (codex-cli >= 0.166), written as a
     managed block at the top of config.toml. The explicit log directory also enables plaintext
     TUI logging.

Policy: sessions, memories and thread history live on RAM and are lost at every reboot.
Switching on does NOT migrate: existing directories are renamed aside to `*.disk-<stamp>` and
the old databases in ~/.codex are simply no longer used; nothing is ever deleted.

Must run with every Codex process stopped -- including the background `codex app-server`
daemon, which keeps the databases open.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import tomllib
from pathlib import Path

from . import unit
from .core import (Ctx, Fail, check_one, link_one, report_leftovers, require_not_running,
                   require_run_dir, require_tmpfs, say, unlink_one)

AGENT = "codex"

# ramdisk directory -> path under CODEX_HOME that should point at it.
# Deliberately NOT here (stay on disk): config.toml, auth.json, history.jsonl,
# session_index.jsonl (single files rewritten in place), packages/ (the installed binaries),
# plugins/, skills/, app-server-* (daemon sockets/control).
MAP = (
    ("codex-memories", "memories"),
    ("codex-sessions", "sessions"),
    ("codex-archived-sessions", "archived_sessions"),     # (absent today)
    ("codex-shell-snapshots", "shell_snapshots"),
    ("codex-tmp", "tmp"),
    ("codex-dot-tmp", ".tmp"),
    ("codex-cache", "cache"),
    ("codex-thread-writer-locks", "thread-writer-locks"),
    ("codex-tui-thread-reference-capabilities", "tui-thread-reference-capabilities"),
)

BEGIN = "# >>> ramdisk-ai (managed by ramdisk.py -- do not edit) >>>"
BEGIN_LEGACY = "# >>> ramdisk-ai (managed by codex_ramdisk.sh -- do not edit) >>>"
END = "# <<< ramdisk-ai <<<"
KEYS = {"sqlite_home", "log_dir"}


def default_home(environ=None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex")


class Codex:
    """Paths for one CODEX_HOME."""

    def __init__(self, ctx: Ctx, home: Path):
        self.ctx, self.home = ctx, home
        self.config = home / "config.toml"
        self.sqlite_rd = ctx.root / "codex-sqlite"
        self.log_rd = ctx.root / "codex-log"
        self.process_tmp = ctx.root / "codex-process-tmp"

    def entries(self) -> list[tuple[str, Path]]:
        return [(name, self.home / rel) for name, rel in MAP]


# --- config.toml managed block ----------------------------------------------------------------

def _is_begin(line: str) -> bool:
    return line.rstrip("\r\n") in (BEGIN, BEGIN_LEGACY)


def prepare(text: str, sqlite: str, log: str) -> tuple[str, str, bool, dict]:
    """Split config text into (base without our block, base with a fresh block, block present,
    parsed full text). Never silently overrides user settings: raises ValueError instead."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if _is_begin(line)]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] != 0 or ends[0] <= starts[0]:
            raise ValueError("malformed managed block; repair config.toml manually")
        if set(tomllib.loads("".join(lines[1:ends[0]]))) != KEYS:
            raise ValueError("unexpected settings in managed block; refusing to remove them")
        base = "".join(lines[ends[0] + 1:])
    else:
        base = text
    parsed = tomllib.loads(text)

    def conflicts(table: dict, location: str = "") -> None:
        for key, value in table.items():
            if key in KEYS:
                raise ValueError(f"user setting {location}{key} conflicts with ramdisk setup")
            if isinstance(value, dict):
                conflicts(value, f"{location}{key}.")

    conflicts(tomllib.loads(base))
    block = (f"{BEGIN}\n"
             f"sqlite_home = {json.dumps(sqlite, ensure_ascii=False)}\n"
             f"log_dir = {json.dumps(log, ensure_ascii=False)}\n"
             f"{END}\n")
    result = block + base
    tomllib.loads(result)
    return base, result, bool(starts), parsed


def has_block(path: Path) -> bool:
    try:
        return any(_is_begin(line) for line in path.read_text().splitlines())
    except FileNotFoundError:
        return False


def _write_atomic(path: Path, text: str, stamp: str) -> None:
    if path.exists():
        # Unique backups even when two invocations share a timestamp.
        fd, backup = tempfile.mkstemp(prefix=f"{path.name}.bak-{stamp}-", dir=path.parent)
        os.close(fd)
        shutil.copy2(path, backup)
    fd, temporary = tempfile.mkstemp(prefix=".ramdisk-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def config(cx: Codex, action: str) -> None:
    """action: validate-on | validate | check | on | off. Raises Fail on any problem."""
    ctx, sqlite, log = cx.ctx, str(cx.sqlite_rd), str(cx.log_rd)
    path = cx.config
    try:
        if path.is_symlink():
            path = path.resolve(strict=True)
        text = path.read_text() if path.exists() else ""
        base, result, present, parsed = prepare(text, sqlite, log)
        points_here = parsed.get("sqlite_home") == sqlite and parsed.get("log_dir") == log
        if action == "validate" and present and not points_here:
            raise ValueError("managed paths differ from this runtime; "
                             "refusing to restore from the wrong directories")
        if action == "check" and not (present and points_here):
            raise ValueError("sqlite_home/log_dir do not point at the current ramdisk")
        if action in ("on", "off"):
            output = result if action == "on" else base
            if output != text:
                if ctx.dry:
                    say(f"DRY  rewrite {path}")
                else:
                    _write_atomic(path, output, ctx.stamp)
    except (ValueError, OSError) as error:
        raise Fail(f"FAIL config.toml: {error}") from None
    say(f"OK   config.toml: {action}")


def _config_check(cx: Codex) -> None:
    try:
        config(cx, "check")
    except Fail as failure:
        say(str(failure))
        cx.ctx.n.fail += 1
    for rd in (cx.sqlite_rd, cx.log_rd):
        if not rd.is_dir():
            say(f"FAIL missing directory: {rd}")
            cx.ctx.n.fail += 1


def _config_off(cx: Codex) -> None:
    ctx = cx.ctx
    if not has_block(cx.config):
        say("OK   config.toml has no ramdisk block")
        ctx.n.ok += 1
        return
    # Keep the redirect active until every copy has succeeded.
    if cx.sqlite_rd.is_dir() and any(cx.sqlite_rd.glob("*.sqlite*")):
        aside = cx.home / f"sqlite.disk-{ctx.stamp}"
        ctx.mkdir(aside)
        for f in sorted(cx.home.glob("*.sqlite*")):
            ctx.rename(f, aside / f.name)
        ctx.copy_tree(cx.sqlite_rd, cx.home)
        say(f"BACK ramdisk databases copied back (previous on-disk ones -> {ctx.label(aside)})")
    if cx.log_rd.is_dir() and any(cx.log_rd.iterdir()):
        ctx.mkdir(cx.home / "log")
        ctx.copy_tree(cx.log_rd, cx.home / "log")
        # Versions that put SQLite logs under log_dir must restore those to the default
        # sqlite_home, alongside the state databases, including WAL/SHM files.
        for f in sorted(cx.log_rd.glob("*.sqlite*")):
            ctx.copy_file(f, cx.home / f.name)
    config(cx, "off")
    ctx.rmtree(cx.sqlite_rd)
    ctx.rmtree(cx.log_rd)
    ctx.n.new += 1


# --- commands -----------------------------------------------------------------------------------

def _guard(ctx: Ctx) -> None:
    require_not_running(ctx, "codex", "Codex", hint=" (including the background `codex app-server` daemon)")


def on(ctx: Ctx, home: Path) -> None:
    cx = Codex(ctx, home)
    say(f"ramdisk root: {ctx.root}")
    require_run_dir(ctx)
    if not home.is_dir():
        say(f"SKIP {ctx.label(home)} does not exist -- Codex is not installed")
        return
    _guard(ctx)
    require_tmpfs(ctx)
    config(cx, "validate-on")
    unit.install(ctx)
    for name, target in cx.entries():
        link_one(ctx, name, target)
    config(cx, "on")
    ctx.mkdir(cx.sqlite_rd, private=True)
    ctx.mkdir(cx.log_rd, private=True)
    ctx.n.new += 1
    if ctx.n.skip:
        raise Fail("FAIL some paths were skipped; setup is incomplete")
    ctx.mark_enabled(AGENT, f"{home}\n")  # the login unit reads CODEX_HOME back from here
    n = ctx.n
    say()
    say(f"already set: {n.ok}   new: {n.new}   moved aside: {n.moved}   skipped: {n.skip}")
    if n.moved and not ctx.dry:
        report_leftovers(home)


def off(ctx: Ctx, home: Path) -> None:
    cx = Codex(ctx, home)
    say(f"ramdisk root: {ctx.root}")
    _guard(ctx)
    config(cx, "validate")
    _config_off(cx)
    for name, target in cx.entries():
        unlink_one(ctx, name, target)
    ctx.rmtree(cx.process_tmp)
    ctx.mark_disabled(AGENT)
    n = ctx.n
    say()
    say(f"restored: {n.new}   already on disk: {n.ok}   skipped: {n.skip}")
    say("(the login unit stays for other agents; ./ramdisk.py off removes everything)")


def check(ctx: Ctx, home: Path) -> None:
    cx = Codex(ctx, home)
    say(f"ramdisk root: {ctx.root}")
    try:
        require_tmpfs(ctx)
    except Fail as failure:
        say(str(failure))
        ctx.n.fail += 1
    if not ctx.enabled(AGENT):
        say("INFO codex is not switched on (./ramdisk.py codex on)")
    for name, target in cx.entries():
        check_one(ctx, name, target)
    _config_check(cx)
    unit.check(ctx)
    report_leftovers(home)
    say()
    if ctx.n.fail:
        raise Fail(f"RESULT: FAIL ({ctx.n.fail} problem(s))")
    say("RESULT: OK -- Codex's scratch data is on the ramdisk")


def run(ctx: Ctx, home: Path, args: list[str]) -> None:
    """Launch Codex with TMPDIR/TMP/TEMP on the ramdisk as well: config.toml cannot redirect
    Rust/libc temporary files or the tools Codex runs that honor TMPDIR."""
    cx = Codex(ctx, home)
    require_tmpfs(ctx)
    _config_check(cx)
    for name, target in cx.entries():
        check_one(ctx, name, target)
    if ctx.n.fail:
        raise Fail(f"FAIL setup incomplete ({ctx.n.fail} problem(s)) -- ./ramdisk.py codex check")
    ctx.mkdir(cx.process_tmp, private=True)
    tmp = str(cx.process_tmp)
    env = dict(os.environ, TMPDIR=tmp, TMP=tmp, TEMP=tmp)
    try:
        os.execvpe("codex", ["codex", *args], env)
    except FileNotFoundError:
        raise Fail("FAIL codex not found on PATH", code=127) from None
