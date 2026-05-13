# Interactive Shell

`contree shell` starts a REPL that combines management commands and sandbox
execution in a single session. It is the fastest way to explore images, run
commands, edit files, and manage branches -- all without leaving the prompt.

## Starting the shell

The shell needs a session, just like any other command. You can pin one
explicitly or let it auto-generate:

```bash
contree -S my-project shell               # explicit session name
CONTREE_SESSION=my-project contree shell   # same via env var
contree shell                              # auto-generated (tied to terminal)
```

The shell prints a coloured prompt showing the current working directory:

```text
contree interactive shell (type 'help' for commands, Ctrl-D to exit)
contree:/>
```

If the session already has an image (from a previous `contree use`), the
shell resumes it. Otherwise, set one first:

```text
contree:/> contree use tag:ubuntu:latest
```

## Running commands

Type any command and it runs inside the sandbox:

```text
contree:/> apt-get update -qq
contree:/> apt-get install -y curl
contree:/> curl https://example.com
```

Each command is an implicit `contree run` with `shell=True`. The whole
input line is forwarded verbatim to the remote `sh -c`, so pipes,
redirects, `;`, `&&`, and `||` are interpreted by the sandbox shell
exactly as typed:

```text
contree:/> echo hello && ls / | head -5
contree:/> mount | grep cgroup
contree:/> echo 1 ; echo 2
contree:/> uname -a > /tmp/info.txt
```

Quoting from your local prompt is also preserved through to the remote
shell:

```text
contree:/> python3 -c "print('hello world')"
```

You can also use the explicit form, which is equivalent:

```text
contree:/> contree run apt-get install -y curl
```

The explicit `contree run` prefix is required when you need flags like
`-D` (disposable), `-e` (env), `-t` (timeout), `--file`, or `-d` (detach):

```text
contree:/> contree run -D -- rm -rf /tmp/*
contree:/> contree run -e DEBUG=1 -- ./app
contree:/> contree run -d -- long-running-task
```

## Tab completion

The shell supports context-aware tab completion for nearly everything.
Press Tab at any point to see available completions.

### What completes

**Commands and subcommands** -- type `contree` then Tab to see all
available subcommands. Type a partial name and Tab completes it:

```text
contree:/> contree ses<TAB>
contree:/> contree session <TAB>
branch  checkout  list  rollback  show  use
```

**Flags** -- type `-` or `--` after a command and Tab shows available
flags:

```text
contree:/> contree run --<TAB>
--cwd  --detach  --disposable  --env  --file  --hostname ...
```

**Sandbox paths** -- any command that takes a file path completes
against the actual sandbox filesystem. The shell queries the image
via the inspect API and caches the results:

```text
contree:/> ls /etc/<TAB>
apt/       bash.bashrc  default/   hostname   nginx/     passwd ...

contree:/> cat /etc/os-<TAB>
contree:/> cat /etc/os-release

contree:/> vim /etc/nginx/<TAB>
nginx.conf  sites-enabled/
```

**Directory-only paths** -- `cd` completes only directories:

```text
contree:/> cd /us<TAB>
contree:/> cd /usr/<TAB>
bin/  include/  lib/  local/  sbin/  share/
```

**Images** -- `contree use` and `contree tag` complete image references.
Type `tag:` to filter by tag names, or start typing a UUID:

```text
contree:/> contree use tag:<TAB>
tag:ubuntu:latest  tag:python:3.11-slim  tag:common/rust/ubuntu:noble ...

contree:/> contree use tag:py<TAB>
contree:/> contree use tag:python:3.11-slim
```

**Operations** -- `contree show` and `contree kill` complete operation
UUIDs:

```text
contree:/> contree show <TAB>
a1b2c3d4-...  e5f6a7b8-...
```

**Branches and sessions** -- session management subcommands complete
branch names and session keys:

```text
contree:/> contree session checkout <TAB>
main  experiment  hotfix

contree:/> contree session use <TAB>
abc123_def456  tutorial  ci-build-42
```

**Help topics** -- `help` completes all command and alias names:

```text
contree:/> help <TAB>
cat  cd  contree  exit  help  history  ls  nano  pwd  quit  vim ...
```

### What does not complete

**Bare commands** (implicit `run`) do not have tab completion. The shell
does not know what executables exist inside the sandbox, so typing a
bare command name and pressing Tab will not offer suggestions:

```text
contree:/> apt-g<TAB>     # no completion
contree:/> pyth<TAB>      # no completion
```

However, paths starting with `/` do complete even in bare command context:

```text
contree:/> python /app/<TAB>
main.py  utils.py  config.yaml
```

## Aliases

The shell intercepts several bare command names for convenience:

### `ls` and `cat`

Bare `ls` and `cat` are forwarded as contree API commands -- they inspect
the sandbox filesystem without spawning a new instance:

```text
contree:/> ls /etc
contree:/> cat /etc/os-release
```

This is equivalent to `contree ls` and `contree cat`. To run the actual
`ls` or `cat` binary inside the sandbox instead, use the explicit prefix:

```text
contree:/> contree run ls -la /etc
contree:/> contree run cat -n /etc/os-release
```

:::{note}
When pending files exist (from `contree file edit` or `contree file cp`),
`ls` and `cat` automatically fall back to running inside the sandbox so
the pending files are visible.

The same fallback happens when arguments contain flags (`-l`) or glob
characters (`*`, `?`, `[`).
:::

### `vim`, `vi`, `nvim`, `nano`

Editor names open `contree file edit` with the corresponding host editor:

```text
contree:/> vim /etc/nginx/nginx.conf
```

This downloads the file, opens it in vim on your machine, and stages any
changes as a pending file for the next run.

## Builtins

### `cd`

Change the working directory for subsequent commands:

```text
contree:/> cd /app
contree:/app> python main.py

contree:/app> cd -        # go back to previous directory
contree:/>
```

`cd` without arguments resets to the sandbox's default working directory.

:::{note}
`cd` does not validate that the path exists in the sandbox. Errors
surface only when the next command uses the invalid path.
:::

### `pwd`

Print the current working directory:

```text
contree:/app> pwd
/app
```

### `history`

Show command history for the current session, optionally filtered by a
case-insensitive substring:

```text
contree:/> history                 # show all entries
contree:/> history apt             # only lines containing "apt"
contree:/> history 'contree '      # quoted match (note trailing space)
```

History is persisted in SQLite per session (up to 10,000 lines) and
restored when you re-enter the shell. Search is scoped to the current
session key; different sessions have isolated history.

### `help`

Show general shell help, or help for a specific command or builtin:

```text
contree:/> help
contree:/> help cd
contree:/> help run
```

Bare `help` prints an overview of builtins, aliases, line continuation,
and tab completion. `help <topic>` shows detailed help for a builtin,
alias, or contree command.

### `clear`

Clear the terminal screen:

```text
contree:/> clear
```

### `timeout`

Run a command with a server-enforced operation timeout. Mirrors the GNU
`timeout` convention but sets `payload.timeout` on the API request instead
of spawning a local wrapper inside the sandbox:

```text
contree:/> timeout 30 apk add gcc
contree:/> timeout 5m make build
contree:/> timeout 1h python long_train.py
```

`DURATION` accepts a bare integer or decimal (seconds by default) and
the suffixes `s`, `m`, `h`, `d`. When the value cannot be parsed, the
shell forwards the line untouched so the in-image `timeout` binary still
handles advanced flags like `--kill-after` or `-s SIGTERM`.

When the limit fires, the API returns `state.timed_out=true` (status may
still be `SUCCESS` with `signal=9`), and the shell logs:

```text
WARNING: Operation <uuid> timed out after 30s
```

### `--format` / `-f`

Change the output format mid-session, or show the current format:

```text
contree:/> --format json   # switch to JSON output
contree:/> -f table        # switch to table output
contree:/> --format        # show current format name
```

## Workflow example

A typical shell session putting it all together:

```text
contree:/> contree use tag:ubuntu:latest
contree:/> apt-get update -qq
contree:/> apt-get install -y python3 python3-pip
contree:/> contree file cp ./app.py /app/app.py
contree:/> contree file cp ./requirements.txt /app/requirements.txt
contree:/> cd /app
contree:/app> pip install -r requirements.txt
contree:/app> python3 app.py
contree:/app> vim app.py                    # edit and re-run
contree:/app> python3 app.py
contree:/app> contree session branch stable
contree:/app> contree tag UUID my-app:v1
```

## Limitations

- **Output format is fixed** -- the `--format` flag is set at `contree shell`
  launch. To use JSON output: `contree -f json shell`.
- **No local pipes or redirects** -- `|`, `>`, `<` are sent to the sandbox,
  not interpreted locally.
- **No job control** -- no `&`, `bg`, `fg`, or Ctrl-Z. Use `contree run -d`
  for detached execution.
- **Bare commands use defaults** -- you cannot pass `--env`, `--file`, or
  `--disposable` without the explicit `contree run` prefix. The operation
  timeout has a shell shortcut: `timeout DURATION CMD...` (see above).
- **No `~` or glob expansion** -- these tokens are passed as-is to the
  sandbox.
- **Image list cache** -- newly created images during a session won't appear
  in tab completion until the shell is restarted. Path completions are
  cached per image and refresh when the session image advances.

---

The shell is the fastest way to iterate. Next: {doc}`sessions`.
