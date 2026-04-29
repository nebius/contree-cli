# Your First Sandbox

Now that you're authenticated, let's spin up a sandbox and explore it.

## Browse images

List available images:

::::{tab-set}
:::{tab-item} CLI
```bash
contree images --prefix=ubuntu
```
:::

:::{tab-item} Shell
```text
contree images --prefix=ubuntu
```
:::
::::

## Sessions

Every command in ConTree runs inside a **session** — a named workspace
that tracks which image you're on, your working directory, file uploads,
and full branch/rollback history.

### How sessions are picked

You don't have to create a session manually. When you run any command,
ConTree auto-generates a session key from your profile, parent process
ID, and terminal (TTY). This means the same terminal window gets the
same session — but **opening a new terminal creates a new session**
because the process ID changes.

There are three ways to control which session is used (in priority order):

1. **`-S` flag** — `contree -S my-session run ...` — explicit, survives terminal restarts
2. **`CONTREE_SESSION` env var** — `export CONTREE_SESSION=my-session` — stable for the shell session
3. **Auto-generated** — derived from profile + PID + TTY (default, tied to current terminal)

### Starting a session

The recommended way is `eval`, which exports the session key so it
survives across commands and is easy to resume later:

```bash
eval $(contree use tag:ubuntu:latest)
```

Without `eval`, `contree use` still works within the same terminal —
the auto-generated key is stable as long as the terminal stays open:

```bash
contree use tag:ubuntu:latest    # sets image
contree run uname -a             # same session (same terminal)
```

But if you close the terminal and open a new one, the auto-generated
session key changes. To resume a previous session, use `eval` or `-S`.

### Resuming sessions

List existing sessions and resume one:

```bash
contree session list             # find the session key
contree -S my-project+a1b2c3d4 use   # resume it
```

Or pin a human-readable name from the start:

```bash
contree -S build use tag:ubuntu:latest
contree -S build run -- make test
# close terminal, open new one — same session:
contree -S build run -- make deploy
```

:::{tip}
For agent workflows or scripts, always use `-S`:

```bash
contree -S build-agent use tag:ubuntu:latest
contree -S build-agent run -- make build
contree -S build-agent run -- make test
```

This is the most reliable — no `eval`, no terminal dependency.
:::

Inside `contree shell`, you don't need `eval` — but the shell still
needs a session. Use `-S` or `CONTREE_SESSION` to pin it:

```bash
contree -S my-project shell
```

Without `-S`, the auto-generated session is used (tied to current terminal).

## Run a command

::::{tab-set}
:::{tab-item} CLI
```bash
contree run uname -a
```
:::

:::{tab-item} Shell
```text
uname -a
```

Bare commands are executed as implicit `run` in the sandbox.
`contree run uname -a` works too -- use the explicit form when you need
flags like `-D`, `-e`, or `--file`.
:::
::::

The CLI spawns a sandbox, waits for it to finish, and prints stdout/stderr.
The resulting filesystem becomes the new session image -- every non-disposable
run produces a new checkpoint.

:::{tip}
The `--` separator is optional. ConTree parses its own flags correctly
regardless. It is a useful convention to visually separate contree options
from the sandbox command:

```bash
contree run -D -- apt-get install -y curl
```

Both `contree run uname -a` and `contree run -- uname -a` work the same way.
:::

## Check session status

See what image and session you're working with:

::::{tab-set}
:::{tab-item} CLI
```bash
contree use
```
:::

:::{tab-item} Shell
```text
contree use
```
:::
::::

Running `contree use` without arguments prints the current session info.

## Install packages

Commands chain naturally. Each run advances the session image:

::::{tab-set}
:::{tab-item} CLI
```bash
contree run apt-get update -qq
contree run apt-get install -y curl
```
:::

:::{tab-item} Shell
```text
apt-get update -qq
apt-get install -y curl
```

Or equivalently with the explicit prefix:

```text
contree run apt-get update -qq
contree run apt-get install -y curl
```
:::
::::

After these two runs the session image includes `curl`.

## Change working directory

Set a working directory for the session — subsequent commands resolve
relative paths against it:

```bash
contree cd /app
contree run -- ls            # lists /app
contree cat README.md        # reads /app/README.md
contree cd                   # reset to sandbox default
```

## Inspect the filesystem

List files and read content without spawning a new sandbox:

::::{tab-set}
:::{tab-item} CLI
```bash
contree ls /usr/bin
contree cat /etc/os-release
```
:::

:::{tab-item} Shell
```text
ls /usr/bin
cat /etc/os-release
```

`ls` and `cat` are aliases for the contree API commands (no sandbox spawned).
To run the actual instance commands instead, use the explicit prefix:

```text
contree run ls /usr/bin
contree run cat /etc/os-release
```
:::
::::

## Download a file

Copy a file from the sandbox to your local machine:

::::{tab-set}
:::{tab-item} CLI
```bash
contree cp /etc/os-release ./os-release.txt
```
:::

:::{tab-item} Shell
```text
contree cp /etc/os-release ./os-release.txt
```

There is no short alias for `cp` -- use the full `contree cp` prefix.
:::
::::

## Disposable mode

Use `-D` / `--disposable` when you want to run a command without advancing the
session image. Changes are discarded after execution:

::::{tab-set}
:::{tab-item} CLI
```bash
contree run -D -- rm -rf /important
```
:::

:::{tab-item} Shell
```text
contree run -D -- rm -rf /important
```

Flags like `-D` require the explicit `contree run` prefix.
:::
::::

The session image stays exactly where it was before this run.

---

Your session tracks every command. Next: {doc}`shell`.
