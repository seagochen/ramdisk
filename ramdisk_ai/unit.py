"""The systemd user unit that recreates the ramdisk directories at every login."""
from __future__ import annotations

import filecmp
import os

from .core import PROJECT, Ctx, Fail, query, say

NAME = "ramdisk-dirs.service"
SRC = PROJECT / "configs" / NAME


def _systemctl_ok(*args) -> bool:
    return query("systemctl", "--user", *args, NAME)


def is_installed() -> bool:
    return _systemctl_ok("cat")


def install(ctx: Ctx) -> None:
    """Install as a real COPY (not a symlink), so moving the canonical file can never leave
    systemd with a dangling link; every run compares the two and refreshes a stale copy."""
    if not SRC.is_file():
        raise Fail(f"FAIL unit source missing: {SRC} -- nothing will recreate the ramdisk after reboot")
    dst = ctx.unit_dst
    was_link = dst.is_symlink()
    if was_link:
        ctx.remove(dst)
        say("MOVE unit was a symlink -- replacing it with a real copy")
    elif dst.is_file() and filecmp.cmp(SRC, dst, shallow=False) and _systemctl_ok("is-enabled"):
        say("OK   login unit up to date and enabled")
        return
    ctx.mkdir(dst.parent)
    ctx.copy_file(SRC, dst)
    say(f"NEW  login unit installed/refreshed: {dst}")
    if os.environ.get("INVOCATION_ID"):
        # Running AS that unit: no daemon-reload from inside its own job; read at next login anyway.
        say("     (running under systemd -- daemon-reload skipped, effective next login)")
    else:
        ctx.run("systemctl", "--user", "daemon-reload")
        ctx.run("systemctl", "--user", "reenable", NAME)


def uninstall(ctx: Ctx) -> None:
    dst = ctx.unit_dst
    if not (is_installed() or dst.exists() or dst.is_symlink()):
        say("OK   login unit not installed")
        return
    ctx.run("systemctl", "--user", "disable", NAME, check=False)
    ctx.remove(dst)
    ctx.run("systemctl", "--user", "daemon-reload")
    say("GONE login unit removed")


def check(ctx: Ctx) -> None:
    if not is_installed():
        say(f"FAIL {NAME} is not installed -- after the next reboot the ramdisk targets are gone")
        say("     and Claude Code / Codex cannot write their files  -> ./ramdisk.py install-unit")
        ctx.n.fail += 1
        return
    if _systemctl_ok("is-enabled"):
        say(f"OK   {NAME} is enabled")
    else:
        say(f"FAIL {NAME} is installed but not enabled  -> ./ramdisk.py install-unit")
        ctx.n.fail += 1
    if _systemctl_ok("is-failed"):
        say(f"FAIL {NAME} failed at the last login -- journalctl --user -u {NAME.removesuffix('.service')}")
        ctx.n.fail += 1
    dst = ctx.unit_dst
    if dst.is_symlink():
        say("WARN installed unit is a symlink (older setup)  -> ./ramdisk.py install-unit")
    elif SRC.is_file() and dst.is_file() and not filecmp.cmp(SRC, dst, shallow=False):
        say(f"WARN installed unit differs from configs/{NAME} (stale copy)  -> ./ramdisk.py install-unit")
