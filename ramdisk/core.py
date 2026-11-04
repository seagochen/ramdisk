"""Shared paths, output, dry-run-aware filesystem actions and the symlink mechanism.

The ramdisk root is a directory on the per-user tmpfs that systemd-logind mounts at login
(/run/user/<uid>, size = RuntimeDirectorySize, default 10% of RAM). It is wiped at reboot and,
without lingering, at the last logout; ramdisk-dirs.service recreates everything at every login.
"""
from __future__ import annotations

import fnmatch
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

AGENTS = ("claude", "codex")


class Fail(Exception):
    """Abort the current command. A non-empty message is printed by the CLI."""

    def __init__(self, message: str = "", code: int = 1):
        super().__init__(message)
        self.code = code


def say(message: str = "") -> None:
    print(message, flush=True)


@dataclass
class Tally:
    ok: int = 0
    new: int = 0
    moved: int = 0
    skip: int = 0
    fail: int = 0


@dataclass
class Ctx:
    home: Path
    run_dir: Path
    state_dir: Path
    unit_dst: Path
    dry: bool = False
    force: bool = False
    stamp: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))
    n: Tally = field(default_factory=Tally)

    @classmethod
    def from_environ(cls, environ=None, **options) -> "Ctx":
        env = os.environ if environ is None else environ
        home = Path(env.get("HOME") or Path.home())
        run_dir = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        state = Path(env.get("XDG_STATE_HOME") or home / ".local/state") / "ramdisk"
        config = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
        unit_dst = config / "systemd/user" / "ramdisk-dirs.service"
        return cls(home=home, run_dir=run_dir, state_dir=state, unit_dst=unit_dst, **options)

    @property
    def root(self) -> Path:
        return self.run_dir / "ramdisk"

    def label(self, path: Path | str) -> str:
        s, h = str(path), str(self.home)
        return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s

    # --- agent on/off markers (the login unit re-applies exactly the agents marked on) ------

    def marker(self, agent: str) -> Path:
        return self.state_dir / f"{agent}.enabled"

    def enabled(self, agent: str) -> bool:
        return self.marker(agent).exists()

    def mark_enabled(self, agent: str, content: str = "") -> None:
        self.mkdir(self.state_dir)
        if self.dry:
            say(f"DRY  write {self.marker(agent)}")
        else:
            self.marker(agent).write_text(content)

    def mark_disabled(self, agent: str) -> None:
        self.remove(self.marker(agent))

    # --- dry-run-aware actions ---------------------------------------------------------------

    def _dry(self, *words) -> bool:
        if self.dry:
            say("DRY  " + " ".join(shlex.quote(str(w)) for w in words))
        return self.dry

    def mkdir(self, path: Path, private: bool = False) -> None:
        """mkdir -p; a directory this call creates gets mode 700 when private."""
        if path.is_dir() or self._dry("mkdir", "-p", *(["-m", "700"] if private else []), path):
            return
        path.mkdir(parents=True, exist_ok=True)
        if private:
            path.chmod(0o700)

    def rename(self, src: Path, dst: Path) -> None:
        if not self._dry("mv", src, dst):
            src.rename(dst)

    def symlink(self, points_to: Path, link: Path) -> None:
        # os.symlink never creates the link *inside* an existing directory (unlike plain ln -s).
        if not self._dry("ln", "-s", "-T", points_to, link):
            os.symlink(points_to, link)

    def remove(self, path: Path) -> None:
        if not self._dry("rm", "-f", path):
            path.unlink(missing_ok=True)

    def rmtree(self, path: Path) -> None:
        if self._dry("rm", "-rf", path):
            return
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)

    def copy_tree(self, src: Path, dst: Path) -> None:
        """cp -a src/. dst/ : merge src's content into dst, keeping symlinks, modes and times."""
        if not self._dry("cp", "-a", f"{src}/.", f"{dst}/"):
            shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)

    def copy_file(self, src: Path, dst: Path) -> None:
        if not self._dry("cp", "-a", src, dst):
            shutil.copy2(src, dst, follow_symlinks=False)

    def run(self, *cmd, check: bool = True, input: str | None = None) -> bool:
        """Run a command that changes something (skipped in dry run, where it counts as success)."""
        if self._dry(*cmd):
            return True
        result = subprocess.run(cmd, input=input, text=True,
                                stdout=subprocess.DEVNULL if input is not None else None)
        if check and result.returncode != 0:
            raise Fail(f"FAIL command failed ({result.returncode}): {shlex.join(map(str, cmd))}")
        return result.returncode == 0


def query(*cmd) -> bool:
    """Run a read-only command quietly; True on exit status 0."""
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except FileNotFoundError:
        return False


def is_empty_dir(path: Path) -> bool:
    return path.is_dir() and next(path.iterdir(), None) is None


# --- system inspection ------------------------------------------------------------------------

def fstype(path: Path) -> str | None:
    """Filesystem type of the mount that holds path (the longest matching mount point wins)."""
    target = os.path.realpath(path)
    best, kind = "", None
    try:
        lines = Path("/proc/self/mounts").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), parts[1])
        inside = target == mnt or target.startswith(mnt.rstrip("/") + "/")
        if inside and len(mnt) >= len(best):
            best, kind = mnt, parts[2]
    return kind


def processes_named(name: str) -> list[tuple[int, str]]:
    """This user's processes whose name (comm, as pgrep -x matches it) is exactly name."""
    uid, me, found = os.getuid(), os.getpid(), []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            if d.stat().st_uid != uid or (d / "comm").read_text().rstrip("\n") != name:
                continue
            args = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except OSError:
            continue  # exited meanwhile
        found.append((int(d.name), args or f"[{name}]"))
    return sorted(found)


def require_not_running(ctx: Ctx, proc: str, human: str, hint: str = "") -> None:
    """Refuse to swap directories under a live agent (shell snapshots, IDE locks, open sqlite).

    A service may be restarted during a live session, so running under systemd is no evidence
    that the agent is stopped; only --force skips this.
    """
    if ctx.force:
        return
    procs = processes_named(proc)
    if not procs:
        return
    say(f"FAIL {human} is running -- {len(procs)} process(es) named '{proc}':")
    for pid, args in procs:
        say(f"       {pid:>7}  {args[:150]}")
    say(f"     Quit every {human} process first{hint},")
    say(f"     or pass --force if you know these are not {human} sessions.")
    raise Fail()


def require_run_dir(ctx: Ctx) -> None:
    if not ctx.run_dir.is_dir():
        raise Fail(f"FAIL {ctx.run_dir} does not exist -- no user session tmpfs")


def require_tmpfs(ctx: Ctx) -> None:
    kind = fstype(ctx.run_dir)
    if kind != "tmpfs":
        raise Fail(f"FAIL {ctx.run_dir} is not a tmpfs (fstype={kind or 'missing'}); "
                   "refusing to call it a ramdisk")


# --- symlinked directories ---------------------------------------------------------------------

def link_one(ctx: Ctx, name: str, target: Path) -> None:
    """Make target a symlink to <root>/name.

    Pre-existing on-disk data is renamed aside to *.disk-<stamp>, never migrated or deleted
    (policy decided 2026-08-12).
    """
    rd, label = ctx.root / name, ctx.label(target)
    ctx.mkdir(rd, private=True)
    if target.is_symlink():
        cur = os.readlink(target)
        if cur == str(rd):
            say(f"OK   {label} -> {rd}")
            ctx.n.ok += 1
        else:
            # Retargeting a link we did not create is how you lose data you did not know was elsewhere.
            say(f"SKIP {label} is a symlink to {cur} (not ours) -- left alone")
            ctx.n.skip += 1
        return
    if target.exists():
        ctx.rename(target, target.with_name(f"{target.name}.disk-{ctx.stamp}"))
        say(f"MOVE {label} -> {label}.disk-{ctx.stamp} (on-disk data kept, not migrated)")
        ctx.n.moved += 1
    ctx.mkdir(target.parent)
    ctx.symlink(rd, target)
    say(f"NEW  {label} -> {rd}")
    ctx.n.new += 1


def unlink_one(ctx: Ctx, name: str, target: Path) -> None:
    """Undo link_one: our symlink becomes a real directory again, with the current ramdisk content."""
    rd, label = ctx.root / name, ctx.label(target)
    if not target.is_symlink():
        say(f"OK   {label} is not a symlink -- nothing to undo")
        ctx.n.ok += 1
        return
    cur = os.readlink(target)
    if cur != str(rd):
        say(f"SKIP {label} -> {cur} (not ours) -- left alone")
        ctx.n.skip += 1
        return
    had_content = rd.is_dir() and not is_empty_dir(rd)
    ctx.remove(target)
    ctx.mkdir(target)
    if had_content:
        ctx.copy_tree(rd, target)
    ctx.rmtree(rd)
    say(f"BACK {label} is a real directory again "
        f"({'ramdisk content copied back' if had_content else 'was empty'})")
    ctx.n.new += 1


def check_one(ctx: Ctx, name: str, target: Path) -> None:
    rd, label = ctx.root / name, ctx.label(target)
    if target.is_symlink():
        cur = os.readlink(target)
        if cur != str(rd):
            problem = f"{label} -> {cur} (expected {rd})"
        elif not rd.is_dir():
            problem = f"{label} -> {rd} (dangling: target missing -- writes will fail)"
        else:
            say(f"OK   {label} -> {rd}")
            return
    elif target.is_dir():
        problem = f"{label} is a real directory on disk, not a symlink"
    elif target.exists():
        problem = f"{label} is a real file on disk, not a symlink"
    else:
        problem = f"{label} does not exist (no symlink created)"
    say(f"FAIL {problem}")
    ctx.n.fail += 1


def report_leftovers(*dirs: Path) -> None:
    """Count leftovers of the rename-aside policy (hidden ones such as .tmp.disk-* included)."""
    n = 0
    for d in dirs:
        try:
            n += sum(fnmatch.fnmatch(e, "*.disk-*") for e in os.listdir(d))
        except OSError:
            pass
    if n:
        say(f"INFO {n} renamed-aside '*.disk-*' item(s) still on disk -- purge by hand when sure")
