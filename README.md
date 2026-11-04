# ramdisk

Keep the scratch data of AI coding agents — **Claude Code** and **OpenAI Codex CLI** — in RAM
instead of on disk.

Agents write a steady stream of transcripts, shell snapshots, caches, logs and SQLite databases.
`ramdisk.py` moves that churn onto the per-user tmpfs that systemd-logind already mounts at
`/run/user/<uid>`, which saves SSD writes and keeps throwaway data from piling up. Configuration,
credentials, plugins and skills stay on disk.

## How it works

- **Directories become symlinks** into `/run/user/<uid>/ramdisk/`. Existing data is never migrated
  or deleted: it is renamed aside to `*.disk-<timestamp>` for you to purge by hand. Symlinks that
  point somewhere else are left alone.
- **Codex databases and logs** cannot be symlinked safely (SQLite WAL/SHM files), so they are
  redirected with Codex's own `sqlite_home` / `log_dir` keys in a managed block at the top of
  `~/.codex/config.toml` (backed up first; refuses if you already set those keys yourself).
- **A systemd user unit** (`ramdisk-dirs.service`) runs `ramdisk.py login` at every login, before
  any session starts, so the tmpfs targets exist again after a reboot.
- **Safety guards**: refuses to touch anything while the agent is running (and lists the offending
  processes), refuses to call a non-tmpfs a ramdisk, and every command supports `--dry-run`.

> **Trade-off:** everything on the ramdisk — including Claude Code's `/resume` transcripts and
> memory, and Codex's sessions and databases — is **lost at every reboot**. Use `off` to copy it
> back to disk.

## Requirements

Linux with a systemd user session, Python 3.11+ at `/usr/bin/python3` (standard library only),
and Codex CLI 0.166+ for the Codex part. The project is expected at `~/projects/ramdisk`
(the path is baked into `configs/ramdisk-dirs.service`).

## Usage

Quit every session of the agent first — for Codex, that includes the background
`codex app-server` daemon.

```bash
./ramdisk.py                     # status: tmpfs usage, login unit, which agents are on
./ramdisk.py on                  # create the ramdisk root, install the login unit

./ramdisk.py claude on --dry-run # preview
./ramdisk.py claude on           # move Claude Code (+ VS Code server logs) to RAM
./ramdisk.py claude check        # verify; exits 1 on any problem (also the default)
./ramdisk.py claude off          # copy the RAM content back, restore real directories

./ramdisk.py codex on
./ramdisk.py codex check
./ramdisk.py codex run [args]    # launch Codex with TMPDIR/TMP/TEMP on RAM as well
./ramdisk.py codex off

./ramdisk.py size 15%            # resize the tmpfs (sudo): 10% | 4G | 4096M | default
./ramdisk.py off                 # switch every agent off, remove the unit and the root
```

Options: `--dry-run` prints what would change and changes nothing; `--force` skips the
"agent is running" guard — use it only when the listed processes are not agent sessions.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Tests run in an isolated `HOME`/XDG tree with a fake `systemctl`; your real setup is never touched.

| Module | Role |
|---|---|
| `ramdisk_ai/core.py` | paths, dry-run-aware file operations, symlink logic, process/tmpfs checks |
| `ramdisk_ai/claude.py` | Claude Code directory map and `on` / `off` / `check` |
| `ramdisk_ai/codex.py` | Codex directory map, `config.toml` block, `on` / `off` / `check` / `run` |
| `ramdisk_ai/system.py` | `status`, `on`, `off`, `size`, `login` |
| `ramdisk_ai/unit.py` | login unit install / check |
| `ramdisk_ai/cli.py` | argument parsing and dispatch |

## License

[MIT](LICENSE)
