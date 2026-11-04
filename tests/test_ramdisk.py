"""End-to-end tests in an isolated HOME/XDG tree; systemctl and sudo are replaced by stand-ins
and the tmpfs/process checks are patched, so the real user setup is never touched.

Run: python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ramdisk import cli, codex, core, unit  # noqa: E402

# systemctl stand-in: records calls; `cat` succeeds when the unit file is installed,
# `is-enabled` after `reenable`, `is-failed` never.
FAKE_SYSTEMCTL = """#!/usr/bin/python3
import os, sys
from pathlib import Path
log = Path(os.environ["FAKE_LOG"])
with log.open("a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
args = [a for a in sys.argv[1:] if a != "--user"]
unit = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd/user/ramdisk-dirs.service"
flag = log.with_name("enabled")
if args[0] == "cat":
    sys.exit(0 if unit.exists() else 1)
if args[0] == "is-enabled":
    sys.exit(0 if flag.exists() else 1)
if args[0] == "is-failed":
    sys.exit(1)
if args[0] == "reenable":
    flag.touch()
if args[0] == "disable":
    flag.unlink(missing_ok=True)
sys.exit(0)
"""


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name)
        self.t = t
        self.home = t / "home"
        self.run_dir = t / "run"
        self.bin = t / "bin"
        for d in (self.home, self.run_dir, self.bin):
            d.mkdir()
        self.log = t / "systemctl.log"
        stub = self.bin / "systemctl"
        stub.write_text(FAKE_SYSTEMCTL)
        stub.chmod(0o755)
        env = {
            "HOME": str(self.home),
            "XDG_RUNTIME_DIR": str(self.run_dir),
            "XDG_STATE_HOME": str(t / "state"),
            "XDG_CONFIG_HOME": str(t / "config"),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "FAKE_LOG": str(self.log),
        }
        patches = [
            mock.patch.dict(os.environ, env),
            mock.patch.object(core, "fstype", return_value="tmpfs"),
            mock.patch.object(core, "processes_named", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("INVOCATION_ID", None)
        os.environ.pop("CODEX_HOME", None)
        self.root = self.run_dir / "ramdisk"
        self.state = t / "state" / "ramdisk"

    def tearDown(self):
        self._tmp.cleanup()

    def cmd(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def tree(self):
        """Every path under the sandbox (for 'nothing changed' assertions)."""
        return sorted(str(p.relative_to(self.t)) for p in self.t.rglob("*") if p != self.log)


class ClaudeTest(Sandbox):
    def test_on_check_off_roundtrip(self):
        projects = self.home / ".claude/projects"
        projects.mkdir(parents=True)
        (projects / "old.jsonl").write_text("old")

        code, out = self.cmd("claude", "on")
        self.assertEqual(code, 0, out)
        self.assertTrue(projects.is_symlink())
        self.assertEqual(os.readlink(projects), str(self.root / "claude-projects"))
        aside = [p for p in (self.home / ".claude").iterdir() if p.name.startswith("projects.disk-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / "old.jsonl").read_text(), "old")
        self.assertTrue((self.state / "claude.enabled").exists())
        self.assertIn("reenable ramdisk-dirs.service", self.log.read_text())
        self.assertIn("INFO 1 renamed-aside", out)

        code, out = self.cmd("claude", "check")
        self.assertEqual(code, 0, out)
        self.assertIn("RESULT: OK", out)

        code, out = self.cmd("claude", "on")  # idempotent
        self.assertEqual(code, 0, out)
        self.assertIn("new links: 0", out)

        (projects / "new.jsonl").write_text("new")  # written to RAM
        code, out = self.cmd("claude", "off")
        self.assertEqual(code, 0, out)
        self.assertFalse(projects.is_symlink())
        self.assertEqual((projects / "new.jsonl").read_text(), "new")
        self.assertFalse((self.root / "claude-projects").exists())
        self.assertFalse((self.state / "claude.enabled").exists())

    def test_check_reports_clobbered_symlink(self):
        self.assertEqual(self.cmd("claude", "on")[0], 0)
        cache = self.home / ".claude/cache"
        cache.unlink()
        cache.mkdir()
        code, out = self.cmd("claude", "check")
        self.assertEqual(code, 1)
        self.assertIn("FAIL ~/.claude/cache is a real directory on disk", out)
        self.assertIn("RESULT: FAIL (1 problem(s))", out)

    def test_foreign_symlink_left_alone(self):
        elsewhere = self.t / "elsewhere"
        elsewhere.mkdir()
        (self.home / ".claude").mkdir()
        (self.home / ".claude/todos").symlink_to(elsewhere)
        code, out = self.cmd("claude", "on")
        self.assertEqual(code, 0, out)
        self.assertIn("SKIP ~/.claude/todos is a symlink to", out)
        self.assertEqual(os.readlink(self.home / ".claude/todos"), str(elsewhere))

    def test_guard_and_force(self):
        with mock.patch.object(core, "processes_named", return_value=[(4242, "claude --resume")]):
            code, out = self.cmd("claude", "on")
            self.assertEqual(code, 1)
            self.assertIn("4242  claude --resume", out)
            self.assertFalse((self.home / ".claude/projects").exists())
            code, out = self.cmd("claude", "on", "--force")
            self.assertEqual(code, 0, out)

    def test_dry_run_changes_nothing(self):
        (self.home / ".claude/projects").mkdir(parents=True)
        before = self.tree()
        code, out = self.cmd("claude", "on", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("DRY  ln -s -T", out)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.log.exists())


class CodexConfigTest(unittest.TestCase):
    S, L = "/run/user/1/ramdisk/codex-sqlite", "/run/user/1/ramdisk/codex-log"

    def test_block_added_and_removed(self):
        base, result, present, _ = codex.prepare('model = "x"\n', self.S, self.L)
        self.assertFalse(present)
        self.assertTrue(result.startswith(codex.BEGIN + "\n"))
        self.assertTrue(result.endswith('model = "x"\n'))
        base2, _, present2, parsed = codex.prepare(result, self.S, self.L)
        self.assertTrue(present2)
        self.assertEqual(base2, 'model = "x"\n')
        self.assertEqual(parsed["sqlite_home"], self.S)

    def test_legacy_marker_recognized(self):
        old = f'{codex.LEGACY_BEGINS[1]}\nsqlite_home = "{self.S}"\nlog_dir = "{self.L}"\n{codex.LEGACY_ENDS[0]}\nmodel = "x"\n'
        base, result, present, _ = codex.prepare(old, self.S, self.L)
        self.assertTrue(present)
        self.assertEqual(base, 'model = "x"\n')
        self.assertTrue(result.startswith(codex.BEGIN))

    def test_user_setting_conflicts(self):
        with self.assertRaisesRegex(ValueError, "profiles.fast.log_dir"):
            codex.prepare('[profiles.fast]\nlog_dir = "/x"\n', self.S, self.L)

    def test_malformed_block(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            codex.prepare(f'model = "x"\n{codex.BEGIN}\n{codex.END}\n', self.S, self.L)

    def test_unexpected_key_in_block(self):
        text = f'{codex.BEGIN}\nsqlite_home = "a"\nlog_dir = "b"\nmodel = "x"\n{codex.END}\n'
        with self.assertRaisesRegex(ValueError, "unexpected settings"):
            codex.prepare(text, self.S, self.L)


class UnitTest(Sandbox):
    def test_render_runs_this_package_with_given_interpreter(self):
        text = unit.render("/opt/my env/bin/python3")
        self.assertIn('ExecStart="/opt/my env/bin/python3" -m ramdisk login', text)
        self.assertIn(f"WorkingDirectory={unit.PACKAGE_DIR.parent}", text)
        self.assertIn(f"ConditionPathExists={unit.PACKAGE_DIR / '__main__.py'}", text)

    def test_install_is_idempotent_and_refreshes_stale_copy(self):
        code, out = self.cmd("install-unit")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.cmd("install-unit")[1].strip(), "OK   login unit up to date and enabled")
        (self.t / "config/systemd/user/ramdisk-dirs.service").write_text("stale")
        code, out = self.cmd("install-unit")
        self.assertIn("NEW  login unit installed/refreshed", out)


class CodexTest(Sandbox):
    def setUp(self):
        super().setUp()
        self.codex = self.home / ".codex"
        self.codex.mkdir()
        (self.codex / "config.toml").write_text('model = "x"\n')
        (self.codex / "state_5.sqlite").write_text("disk-db")
        (self.codex / "sessions").mkdir()

    def test_on_check_off_roundtrip(self):
        code, out = self.cmd("codex", "on")
        self.assertEqual(code, 0, out)
        cfg = (self.codex / "config.toml").read_text()
        self.assertIn(f'sqlite_home = "{self.root}/codex-sqlite"', cfg)
        self.assertTrue((self.codex / "sessions").is_symlink())
        self.assertEqual((self.state / "codex.enabled").read_text(), f"{self.codex}\n")
        self.assertTrue(list(self.codex.glob("config.toml.bak-*")))

        code, out = self.cmd("codex", "check")
        self.assertEqual(code, 0, out)

        (self.root / "codex-sqlite/state_5.sqlite").write_text("ram-db")
        (self.root / "codex-log/codex-tui.log").write_text("log")
        code, out = self.cmd("codex", "off")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.codex / "state_5.sqlite").read_text(), "ram-db")
        aside = next(self.codex.glob("sqlite.disk-*"))
        self.assertEqual((aside / "state_5.sqlite").read_text(), "disk-db")
        self.assertEqual((self.codex / "log/codex-tui.log").read_text(), "log")
        self.assertEqual((self.codex / "config.toml").read_text(), 'model = "x"\n')
        self.assertFalse((self.codex / "sessions").is_symlink())
        self.assertFalse((self.state / "codex.enabled").exists())

    def test_conflicting_user_setting_refuses_before_changing_anything(self):
        (self.codex / "config.toml").write_text('log_dir = "/var/log/codex"\n')
        before = self.tree()
        code, out = self.cmd("codex", "on")
        self.assertEqual(code, 1)
        self.assertIn("FAIL config.toml: user setting log_dir conflicts", out)
        self.assertEqual(self.tree(), before)

    def test_not_tmpfs_refuses(self):
        with mock.patch.object(core, "fstype", return_value="ext4"):
            code, out = self.cmd("codex", "on")
        self.assertEqual(code, 1)
        self.assertIn("is not a tmpfs (fstype=ext4)", out)

    def test_missing_codex_home_is_skipped(self):
        os.environ["CODEX_HOME"] = str(self.t / "nope")
        code, out = self.cmd("codex", "on")
        self.assertEqual(code, 0, out)
        self.assertIn("Codex is not installed", out)


class SystemTest(Sandbox):
    def test_login_reapplies_enabled_agents_and_custom_codex_home(self):
        custom = self.t / "codex-home"
        custom.mkdir()
        os.environ["CODEX_HOME"] = str(custom)
        self.assertEqual(self.cmd("claude", "on")[0], 0)
        self.assertEqual(self.cmd("codex", "on")[0], 0)
        del os.environ["CODEX_HOME"]
        # Reboot: the tmpfs is empty again.
        import shutil
        shutil.rmtree(self.root)
        code, out = self.cmd("login")
        self.assertEqual(code, 0, out)
        self.assertIn("---- claude: on ----", out)
        self.assertIn("---- codex: on ----", out)
        self.assertTrue((self.root / "claude-projects").is_dir())
        self.assertTrue((self.root / "codex-sessions").is_dir())
        self.assertEqual(os.readlink(custom / "sessions"), str(self.root / "codex-sessions"))

    def test_off_everything(self):
        self.assertEqual(self.cmd("claude", "on")[0], 0)
        code, out = self.cmd("off")
        self.assertEqual(code, 0, out)
        self.assertIn("GONE login unit removed", out)
        self.assertFalse(self.root.exists())
        self.assertFalse((self.home / ".claude/projects").is_symlink())

    def test_usage_errors(self):
        self.assertEqual(self.cmd("size", "huge")[0], 2)
        self.assertEqual(self.cmd("claude", "explode")[0], 2)
        self.assertEqual(self.cmd("--bogus")[0], 2)
        self.assertEqual(self.cmd("nonsense")[0], 2)


if __name__ == "__main__":
    unittest.main()
