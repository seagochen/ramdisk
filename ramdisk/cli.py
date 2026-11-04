"""Command line: ramdisk <command> [--dry-run] [--force]."""
from __future__ import annotations

import sys

from . import __version__, claude, codex, system, unit
from .core import Ctx, Fail, say

USAGE = """\
usage: ramdisk <command> [--dry-run] [--force]

the ramdisk itself
  status                    tmpfs size/usage, login unit, which agents are on (default)
  on                        create the ramdisk root and install the login unit
  off                       switch EVERY agent off (data copied back to disk),
                            remove the login unit and the ramdisk root
  size <10%|4G|4096M|default>   set the tmpfs size (sudo; live now and from next login)
  install-unit              install/refresh the login unit only
  login                     what the login unit runs: recreate dirs, re-apply enabled agents

agents
  claude [check|on|off]     Claude Code + VSCode server scratch data (default: check)
  codex  [check|on|off]     Codex scratch data, sqlite and logs (default: check)
  codex run [codex args...] launch Codex with TMPDIR/TMP/TEMP on the ramdisk

options
  --dry-run                 print what would change, change nothing
  --force                   skip the "agent is running" guard
  --version                 print the version
"""

SYSTEM = {"status": system.status, "on": system.on, "off": system.off,
          "install-unit": unit.install, "login": system.login}
AGENT_COMMANDS = ("check", "on", "off")


def _usage_error(message: str) -> Fail:
    return Fail(f"{message}\n\n{USAGE}".rstrip(), code=2)


def dispatch(argv: list[str]) -> None:
    if argv[:2] == ["codex", "run"]:  # everything after `run` belongs to codex
        ctx = Ctx.from_environ()
        return codex.run(ctx, codex.default_home(), argv[2:])

    dry = force = False
    words = []
    for a in argv:
        if a == "--dry-run":
            dry = True
        elif a == "--force":
            force = True
        elif a == "--version":
            say(f"ramdisk {__version__}")
            return
        elif a in ("-h", "--help", "help"):
            say(USAGE.rstrip())
            return
        elif a.startswith("-"):
            raise _usage_error(f"unknown option: {a}")
        else:
            words.append(a)

    ctx = Ctx.from_environ(dry=dry, force=force)
    if dry:
        say("== dry run: nothing will be changed ==")
    cmd, rest = (words[0], words[1:]) if words else ("status", [])

    if cmd in ("claude", "codex"):
        sub = rest[0] if rest else "check"
        if sub not in AGENT_COMMANDS or len(rest) > 1:
            raise _usage_error(f"unknown {cmd} command: {' '.join(rest)}")
        if cmd == "claude":
            getattr(claude, sub)(ctx)
        else:
            getattr(codex, sub)(ctx, codex.default_home())
    elif cmd == "size":
        if len(rest) > 1:
            raise _usage_error("size takes one argument")
        system.set_size(ctx, rest[0] if rest else None)
    elif cmd in SYSTEM and not rest:
        SYSTEM[cmd](ctx)
    else:
        raise _usage_error(f"unknown command: {' '.join(words)}")


def main(argv: list[str] | None = None) -> int:
    try:
        dispatch(sys.argv[1:] if argv is None else argv)
    except Fail as failure:
        if str(failure):
            say(str(failure))
        return failure.code
    except KeyboardInterrupt:
        return 130
    return 0
