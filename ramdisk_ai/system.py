"""The ramdisk itself: the per-user tmpfs, its size, and the login unit that rebuilds it.

Size: the tmpfs is /run/user/<uid>, mounted by systemd-logind with RuntimeDirectorySize
(default 10% of RAM). `size` writes a logind drop-in, which applies from the next login, and
also remounts the live tmpfs so the new size takes effect immediately.
"""
from __future__ import annotations

import os
import re
import traceback
from pathlib import Path

from . import claude, codex, unit
from .core import AGENTS, Ctx, Fail, Tally, fstype, is_empty_dir, require_run_dir, say

DROPIN = Path("/etc/systemd/logind.conf.d/50-ramdisk-ai.conf")


def codex_home_for_login(ctx: Ctx) -> Path:
    """The CODEX_HOME recorded when codex was switched on (falls back to the default)."""
    try:
        saved = ctx.marker("codex").read_text().splitlines()
    except OSError:
        saved = []
    return Path(saved[0]) if saved and saved[0] else codex.default_home()


def run_agent(ctx: Ctx, agent: str, command: str) -> None:
    """One agent command with its own counters, as if it had been run on its own."""
    say()
    say(f"---- {agent}: {command} ----")
    ctx.n = Tally()
    if agent == "claude":
        getattr(claude, command)(ctx)
    else:
        getattr(codex, command)(ctx, codex_home_for_login(ctx))


def _disk_usage(path: Path) -> int:
    """Bytes allocated under path, like du -s."""
    total = 0
    for top, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                total += os.lstat(os.path.join(top, name)).st_blocks * 512
            except OSError:
                pass
    return total


def _human(n: float) -> str:
    for unit_ in "BKMG":
        if n < 1024 or unit_ == "G":
            return f"{n:.0f}{unit_}" if unit_ == "B" else f"{n:.1f}{unit_}"
        n /= 1024
    return str(n)


def status(ctx: Ctx) -> None:
    kind = fstype(ctx.run_dir)
    if kind != "tmpfs":
        say(f"FAIL {ctx.run_dir} is not a tmpfs (fstype={kind or 'missing'})")
        ctx.n.fail += 1
    else:
        st = os.statvfs(ctx.run_dir)
        mib = 1024 * 1024
        size = st.f_blocks * st.f_frsize // mib
        used = (st.f_blocks - st.f_bfree) * st.f_frsize // mib
        avail = st.f_bavail * st.f_frsize // mib
        pct = used * 100 // size if size else 0
        say(f"OK   {ctx.run_dir} is tmpfs: {size}M total, {used}M used ({pct}%), {avail}M free")
        if pct >= 80:
            say(f"WARN tmpfs is {pct}% full -- writes fail with ENOSPC when it fills; purge old")
            say(f"     transcripts under {ctx.root}, or ./ramdisk.py size <bigger>")
    if DROPIN.is_file():
        lines = [l for l in DROPIN.read_text().splitlines() if l.startswith("RuntimeDirectorySize")]
        say(f"INFO size configured in {DROPIN}: {lines[0] if lines else '?'}")
    else:
        say("INFO size: logind default (RuntimeDirectorySize=10%)")
    if ctx.root.is_dir():
        say(f"OK   ramdisk root {ctx.root} exists ({_human(_disk_usage(ctx.root))})")
    else:
        say(f"INFO ramdisk root {ctx.root} does not exist yet")
    if any(ctx.enabled(a) for a in AGENTS):
        unit.check(ctx)
    elif unit.is_installed():
        say(f"INFO login unit installed (no agent is on, so it only recreates {ctx.root})")
    else:
        say("INFO login unit not installed (fine: no agent is on)")
    for a in AGENTS:
        say(f"ON   {a}  (details: ./ramdisk.py {a} check)" if ctx.enabled(a) else f"OFF  {a}")
    if ctx.n.fail:
        raise Fail()


def on(ctx: Ctx) -> None:
    require_run_dir(ctx)
    ctx.mkdir(ctx.root, private=True)
    say(f"OK   ramdisk root: {ctx.root}")
    unit.install(ctx)
    say("next: ./ramdisk.py claude on   and/or   ./ramdisk.py codex on")


def login(ctx: Ctx) -> None:
    """What the login unit runs. One agent's problem never stops the others; the unit's exit
    status still reports it."""
    ctx.mkdir(ctx.root, private=True)
    failed = False
    for a in AGENTS:
        if not ctx.enabled(a):
            continue
        try:
            run_agent(ctx, a, "on")
        except Fail as failure:
            if str(failure):
                say(str(failure))
            failed = True
        except Exception:
            say(f"FAIL {a}: unexpected error")
            say(traceback.format_exc().rstrip())
            failed = True
    if failed:
        raise Fail()


def off(ctx: Ctx) -> None:
    for a in AGENTS:
        if ctx.enabled(a):
            run_agent(ctx, a, "off")
    say()
    unit.uninstall(ctx)
    if ctx.root.is_dir():
        if is_empty_dir(ctx.root):
            if not ctx._dry("rmdir", ctx.root):
                ctx.root.rmdir()
            say(f"GONE {ctx.root}")
        else:
            left = " ".join(sorted(p.name for p in ctx.root.iterdir()))
            say(f"WARN {ctx.root} is not empty (left by hand?) -- not removed: {left}")
    if DROPIN.is_file():
        say("INFO size drop-in kept; './ramdisk.py size default' removes it")


def set_size(ctx: Ctx, size: str | None) -> None:
    if size == "default":
        if DROPIN.is_file():
            ctx.run("sudo", "rm", "-f", DROPIN)
        value = "10%"
    elif size and re.fullmatch(r"\d+%|\d+[KMG]", size):
        value = size
        ctx.run("sudo", "mkdir", "-p", DROPIN.parent)
        text = f"# written by {Path(__file__).resolve()}\n[Login]\nRuntimeDirectorySize={value}\n"
        ctx.run("sudo", "tee", DROPIN, input=text)
    else:
        raise Fail("usage: ramdisk.py size <10%|4G|4096M|default>", code=2)
    # logind reads the drop-in at the next login; remount so the change is live now as well.
    # Shrinking below what is in use fails, which leaves the old size in place -- harmless.
    if ctx.run("sudo", "mount", "-o", f"remount,size={value}", ctx.run_dir, check=False):
        say(f"NEW  size {value} (live now; persistent via {DROPIN})")
    else:
        say(f"WARN remount failed (more data in use than {value}?) -- applies from the next login")
