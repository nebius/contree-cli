# contree-cli

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Zero Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](#zero-dependencies)
[![PyPI](https://img.shields.io/pypi/v/contree-cli.svg)](https://pypi.org/project/contree-cli/)

Command-line client for the [ConTree](https://contree.dev) sandboxing platform — secure, VM-isolated sandboxes with git-like branching for AI agents and developers.

```bash
eval $(contree use tag:ubuntu:latest)   # pick a base image for current session
contree run apt-get update -qq          # each run snapshots the result
contree run apt-get install -y curl     # builds on the previous snapshot
contree session branch experiment       # branch the sandbox state
contree run -- make test                # experiment freely
contree session checkout main           # switch back instantly
contree session rollback 2              # or rewind two steps
```

## What is ConTree?

[ConTree](https://contree.dev) is a secure sandbox API that runs every command inside a VM-isolated instance and snapshots the full filesystem after each execution. These snapshots (called **images**) form a tree — branch from any checkpoint, explore paths in parallel, and roll back on failure.

**Built for AI agents that think ahead:**

- **Tree-search execution** — branch sandbox state so an agent can explore multiple solution paths in parallel and keep the best one
- **Instant rollback** — backtrack to any previous checkpoint without rebuilding from scratch
- **Safe code execution** — run untrusted or LLM-generated code inside VM-level isolation; crashes and side effects stay in the sandbox
- **Session continuity** — rewind and resume long-running agent workflows with full filesystem context preserved

`contree-cli` talks to the ConTree API. Install it, authenticate with your project token, and create sandboxes, run commands, inspect filesystems, and manage sessions — all from your terminal, shell scripts, or agent toolchains.

## Install

```bash
pip install contree-cli
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install contree-cli
```

<details>
<summary>More options (pipx, from source)</summary>

```bash
# pipx
pipx install contree-cli

# From source
git clone https://github.com/nebius/contree-cli.git
cd contree-cli
pip install .
```

</details>

Verify:

```bash
contree --help
```

**Requirements:** Python 3.10+ and nothing else. Zero external dependencies — stdlib only.

## Quick Start

### 1. Authenticate

```bash
contree auth
```

You'll be prompted to enter your API token and project ID. The CLI verifies the token and saves credentials to `~/.config/contree/auth.ini` (override the data directory via `CONTREE_HOME`).

If `--token`/`--url`/`--project` flags are omitted, `contree auth` reads `CONTREE_TOKEN` (or `NEBIUS_API_KEY`), `CONTREE_URL`, and `CONTREE_PROJECT` (or `NEBIUS_AI_PROJECT`) from the environment instead of prompting. These variables are read only during registration; runtime commands use the saved profile only.

### 2. Install agent skills (optional)

```bash
contree skill install
```

Autodetects installed agents (Claude Code, Codex, OpenCode, Cline, Amp) and installs ConTree skill files into their skill directories. Use `contree skill install -F` to force-overwrite.

### 3. Start a session

```bash
eval $(contree use tag:ubuntu:latest)
```

This picks a base image and creates a session. The `eval` wrapper exports the session variable so subsequent commands share the same state.

### 4. Run commands

```bash
contree run uname -a                  # direct execution
contree run apt-get install -y curl   # installs persist to next run
contree run -s -- 'echo $PATH'        # shell mode for expansions
```

Each non-disposable `run` produces a new image — a full filesystem checkpoint.

### 5. Inspect without spawning

```bash
contree ls /usr/bin          # list files (no VM needed)
contree cat /etc/os-release  # read files (no VM needed)
contree cp /app/output.log . # download to local machine
```

### 6. Branch and roll back

```bash
contree session branch experiment     # create a branch
contree run -- make test              # experiment on it
contree session checkout main         # switch back
contree session rollback 1            # undo last run
```

## Interactive Shell

`contree shell` starts a REPL where bare commands run in the sandbox automatically:

```
$ contree shell
contree:/> apt-get update -qq
...
contree:/> apt-get install -y curl
...
contree:/> curl -sI https://example.com
HTTP/2 200
...
contree:/> cd /etc
contree:/etc> cat os-release
PRETTY_NAME="Ubuntu 24.04 LTS"
...
contree:/etc> contree session branch experiment
Created branch 'experiment'
contree:/etc> exit
```

The shell provides tab completion for commands, paths, image tags, and operation IDs. `ls` and `cat` map to the fast API inspection commands by default. `vim`/`vi`/`nano` open `contree file edit` with your local `$EDITOR`.

## Commands

| Command | Aliases | Description |
|---|---|---|
| `use IMAGE` | `ci` | Set or show current session image |
| `run [-- CMD]` | `r` | Spawn a sandbox instance, execute command |
| `images [--prefix]` | `i`, `img` | List and import images |
| `tag UUID TAG` | `t` | Tag or untag an image |
| `ps` | | List operations (shortcut for `operation ls`) |
| `kill UUID [UUID...]` | | Cancel operations (shortcut for `operation cancel`; `--all` for all active) |
| `show UUID` | | Show operation result |
| `operation list` | `op`, `ls` | Same as `ps` (canonical) |
| `operation show UUID...` | `sh` | Multi-UUID inspect |
| `operation wait UUID...` | `w` | Block until each op finishes (or `--all`; `--timeout`) |
| `operation cancel UUID...` | `kill`, `k` | Multi-UUID cancel (or `--all`) |
| `ls [PATH]` | | List files in session image (no VM) |
| `cat PATH` | | Show file content from session image (no VM) |
| `cp PATH DEST` | | Download file from image to local path |
| `file edit PATH` | `e` | Edit remote file via local `$EDITOR` |
| `file cp SRC DEST` | `f` | Upload local file into session image |
| `cd [PATH]` | | Change working directory in session |
| `env [KEY=VALUE ...]` | | Manage session environment variables |
| `session` | `s` | Show current session info |
| `session list` | `ls` | List all sessions |
| `session branch` | `br` | Create or list branches |
| `session checkout` | `co` | Switch active branch |
| `session rollback [N]` | `rb` | Revert N steps in history |
| `session show` | | Display session history DAG |
| `auth` | | Configure authentication (secure prompt) |
| `auth list` | `ls` | List saved profiles |
| `auth switch NAME` | | Switch active profile |
| `auth remove NAME` | `rm` | Remove a saved profile |
| `skill install [SPEC ...]` | | Install agent skills |
| `skill remove SPEC [...]` | | Remove installed skills |
| `skill upgrade [SPEC ...]` | | Upgrade skills (no args = all) |
| `skill list` | `ls` | List installed skills |
| `shell` | `sh` | Start interactive REPL |
| `agent` | `man` | Show manual |

See the full [command reference](https://docs.contree.dev/cli/commands/) for all flags and options.

## Execution Modes

The `run` command supports four execution modes:

```bash
# Direct — arguments are the command
contree run uname -a

# Shell — arguments joined, passed to sh -c
contree run -s -- 'echo $HOME && ls /'

# Interpreter — local script executed remotely
contree run -I ./deploy.sh

# Piped stdin — stdin forwarded to the command
echo 'SELECT 1' | contree run -- psql
```

### File injection

Mount local files into the sandbox:

```bash
contree run --file ./app.py:/app/app.py -- python /app/app.py
contree run --file ./config.yaml --file ./data.csv -- ./process.sh
```

File specs support permissions: `host_path[:remote_path][:uUID][:gGID][:mMODE]`

### Shebang scripts

```bash
#!/usr/bin/env -S contree run -I
apt-get update -qq
apt-get install -y curl
curl https://example.com
```

Save as `setup.sh`, `chmod +x`, and run it directly.

## Sessions and Branching

Sessions track your sandbox state with git-like branching and history:

```
main:  A ── B ── C ── D
                  \
experiment:        E ── F
```

Every `run` creates a checkpoint. Branch to explore alternatives. Roll back to any point. Switch branches instantly.

```bash
contree session                       # show current state
contree session show                  # display history DAG
contree session branch feature        # create branch from HEAD
contree session checkout feature      # switch to it
contree session rollback 3            # go back 3 steps
contree session use other-session     # import image from another session
```

## Output Formats

All commands support structured output via `-f`/`--format`:

```bash
contree images -f json                # JSON (one object per line)
contree images -f json-pretty         # pretty-printed JSON array
contree ps -f csv                     # RFC 4180 CSV
contree ps -f tsv                     # tab-separated values
contree ls -f table                   # ASCII table
```

Pipe JSON output into `jq`, feed CSV into spreadsheets, or parse programmatically in your agent toolchain.

## Configuration

### Config file

`~/.config/contree-cli/config.ini`:

```ini
[DEFAULT]
profile = default

[profile:default]
token = eyJ...
url = https://api.studio.nebius.com/sandboxes
type = iam
project = your-project-id
```

### Multiple profiles

```bash
contree auth --profile=staging        # save staging token
contree auth --profile=prod           # save production token
contree auth profiles                 # list all profiles + status probe
contree auth profiles --offline       # list profiles without network checks
contree -f json auth profiles         # structured profile health output
contree auth switch staging           # switch active profile
```

### Environment variables

Read at runtime (any command):

| Variable | Purpose |
|---|---|
| `CONTREE_HOME` | Data directory (default `$XDG_CONFIG_HOME/contree`, or `~/.config/contree`) |
| `CONTREE_PROFILE` | Active profile name (selects which profile commands use) |
| `CONTREE_SESSION` | Explicit session key (for multi-terminal workflows) |
| `CONTREE_SESSION_DB` | Path to session SQLite database |

Read only by `contree auth` (registration-time fallbacks for omitted flags):

| Variable | Used for |
|---|---|
| `CONTREE_TOKEN` / `NEBIUS_API_KEY` | `--token` |
| `CONTREE_URL` | `--url` |
| `CONTREE_PROJECT` / `NEBIUS_AI_PROJECT` | `--project` |

Credentials come strictly from the saved profile at runtime. `--token`, `--url`, `--project` CLI flags override profile fields for a single invocation.

## Zero Dependencies

`contree-cli` uses only the Python standard library. No `requests`, no `click`, no `rich` — just `http.client`, `argparse`, `json`, `sqlite3`, and friends. It runs anywhere Python 3.10+ is available with nothing to install beyond the package itself.

## Development

```bash
git clone https://github.com/nebius/contree-cli.git
cd contree-cli
uv sync --group dev
```

```bash
make lint       # ruff check --fix
make types      # mypy strict mode
make check      # lint + types
make tests      # lint + types + pytest
```

The project enforces strict mypy, ruff linting (E/F/W/I/UP/B/SIM/RUF rules), and full test coverage across 23+ test modules.

## Documentation

Full documentation is available at **[docs.contree.dev/cli](https://docs.contree.dev/cli/)**, including:

- [Tutorial](https://docs.contree.dev/cli/tutorial/) — step-by-step from installation to automation
- [Command Reference](https://docs.contree.dev/cli/commands/) — every command, flag, and subcommand

## Links

- [ConTree Platform](https://contree.dev)
- [Documentation](https://docs.contree.dev/cli/)
- [PyPI](https://pypi.org/project/contree-cli/)
- [Issues](https://github.com/nebius/contree-cli/issues)
- [Releases](https://github.com/nebius/contree-cli/releases)

## Copyright

Nebius B.V. 2026, Licensed under the Apache License, Version 2.0 (see "LICENSE" file).
