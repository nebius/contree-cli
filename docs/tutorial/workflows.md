# Scripting & Automation

contree-cli is designed for scripting. Exit codes propagate, output formats
are machine-readable, and shebang mode lets you write executable sandbox
scripts.

## Shebang scripts

:::{note}
Shebang scripts are a CLI-only feature. They are not available in the
interactive shell.
:::

Any file with a `contree run -I` shebang runs inside a sandbox:

```bash
#!/usr/bin/env -S contree run -I
echo "Hello from a ConTree sandbox"
uname -a
```

Save it, `chmod +x`, and run it directly:

```bash
chmod +x hello.sh
./hello.sh
```

The `-I` (interpreter) flag reads the script, strips the shebang line, and
sends the body as stdin to `/bin/sh -s` inside the sandbox.

### Combining flags

Shebang flags stack. A disposable run with a 10-second timeout:

```bash
#!/usr/bin/env -S contree run -I -D -t 10
apt-get update -qq
apt-get install -y curl
curl https://example.com
```

Since `-D` is set, the session image is not advanced -- the script runs in
a throwaway sandbox.

### Passing arguments

Extra arguments after the script name are forwarded to the shell:

```bash
#!/usr/bin/env -S contree run -I
echo "arg1=$1 arg2=$2"
```

```bash
./script.sh foo bar
# arg1=foo arg2=bar
```

:::{note}
The `-S` flag on `/usr/bin/env` is required because the `contree` entry point
is a Python script. Without `-S`, the kernel sees a nested shebang
(script -> script -> binary) and returns ENOEXEC. Using `/usr/bin/env -S`
(a real binary) splits the argument string and avoids this.
:::

## Execution modes

### Direct command

The default mode. Each positional argument becomes a separate argv entry:

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
:::
::::

### Shell mode

`-s` joins all arguments into a single shell expression:

::::{tab-set}
:::{tab-item} CLI
```bash
contree run -s -- 'echo hello && ls /'
```
:::

:::{tab-item} Shell
```text
echo hello && ls /
```

Bare commands in the shell always use shell mode.
:::
::::

Useful when you need pipes, redirects, or `&&` chains.

### Piped stdin

:::{note}
Piped stdin is a CLI-only feature. It is not available in the interactive
shell.
:::

When stdin is not a TTY, it is read, base64-encoded, and sent to the sandbox:

```bash
echo 'uname -a' | contree run /bin/sh
```

```bash
cat deploy.sh | contree run /bin/sh
```

### Detached mode

`-d` spawns the operation and exits immediately, printing the operation UUID:

::::{tab-set}
:::{tab-item} CLI
```bash
contree run -d -- long-running-task
```

Check on it later:

```bash
contree show UUID
```
:::

:::{tab-item} Shell
```text
contree run -d -- long-running-task
contree show UUID
```

Flags like `-d` require the explicit `contree run` prefix.
:::
::::

## Exit codes

:::{note}
Exit code propagation is a CLI-only feature useful for scripting. The
interactive shell does not expose sandbox exit codes.
:::

The sandbox exit code propagates to the CLI process:

```bash
contree run -- /bin/sh -c 'exit 42'
echo $?    # 42
```

This means `contree run` works naturally in `if`, `&&`, `||`, and `set -e`
scripts:

```bash
set -e
contree run -- make test          # script aborts if tests fail
contree run -- make install
```

If the operation fails at the platform level (timeout, cancelled), the CLI
exits with code 1.

## Environment variables

Pass environment variables into the sandbox with `-e`:

::::{tab-set}
:::{tab-item} CLI
```bash
contree run -e DEBUG=1 -e DB_HOST=postgres -- ./app
```
:::

:::{tab-item} Shell
```text
contree run -e DEBUG=1 -e DB_HOST=postgres -- ./app
```

Flags like `-e` require the explicit `contree run` prefix.
:::
::::

The flag is repeatable. Format is `KEY=VALUE`.

## Output truncation

By default, stdout/stderr is capped at 64 KiB in the API response. Override
with `-T`:

::::{tab-set}
:::{tab-item} CLI
```bash
contree run -T 1048576 -- ./generate-big-output.sh
```
:::

:::{tab-item} Shell
```text
contree run -T 1048576 -- ./generate-big-output.sh
```
:::
::::

## Monitor operations

List running and recent operations:

::::{tab-set}
:::{tab-item} CLI
```bash
contree ps            # active operations only
contree ps -a         # all (including completed)
contree ps -q         # UUIDs only, one per line
```
:::

:::{tab-item} Shell
```text
contree ps
contree ps -a
contree ps -q
```
:::
::::

Show the full result of a specific operation:

::::{tab-set}
:::{tab-item} CLI
```bash
contree show UUID
```
:::

:::{tab-item} Shell
```text
contree show UUID
```
:::
::::

Cancel an operation:

::::{tab-set}
:::{tab-item} CLI
```bash
contree kill UUID
contree kill --all
```
:::

:::{tab-item} Shell
```text
contree kill UUID
contree kill --all
```
:::
::::

### Fan-out + wait

When several independent steps can run at the same time, spawn each
one detached and join them with `contree op wait` (alias `contree
operation wait`). The wait command polls the API and prints one row
per operation as soon as it reaches a terminal status, with columns
`uuid`, `status`, `timed_out`, `duration`, and any other scalar field
the API returns.

:::{important}
`op wait` is a **pure observer** — it polls completion status but
**does not touch local session state**. That makes the pattern most
natural with `--disposable` (no image to track). For non-disposable
fan-out, the result images live only on the server; the
`detached-<op-uuid>` branches created at spawn time still point at
the **starting** image and never get moved. See the non-disposable
recovery example below.
:::

The preferred shape — disposable runs, parallel independent checks.
The global `-f json` must come BEFORE the subcommand so that `jq`
gets JSON; the default `run -d` formatter is plain.

```bash
# Three parallel test suites, results discarded after the runs
A=$(contree -f json run -d --disposable -- pytest tests/a | jq -r .uuid)
B=$(contree -f json run -d --disposable -- pytest tests/b | jq -r .uuid)
C=$(contree -f json run -d --disposable -- pytest tests/c | jq -r .uuid)

# Block until each one finishes (or 60 s elapses, whichever comes first)
contree op wait "$A" "$B" "$C"

# Inspect stdout/stderr per leg
contree op show "$A" "$B" "$C"
```

Non-disposable fan-out works too, but you have to recover the result
images yourself — `op wait` will not bind them into the session:

```bash
A=$(contree -f json run -d -- apt-get install -y curl | jq -r .uuid)
B=$(contree -f json run -d -- apt-get install -y wget | jq -r .uuid)
contree op wait "$A" "$B"

# Pull the winning leg's image out of the operation result and
# attach it to the active session.
IMG_A=$(contree -f json op show "$A" | jq -r .image)
contree use "$IMG_A"

# Or tag it for reuse later.
contree tag "$IMG_A" feature/curl-tools
```

After fan-out + wait the session retains a `detached-<op-uuid>`
branch per spawn. They all point at the image that existed when the
fan-out started, so they are mostly cosmetic — feel free to delete
them with `contree session branch --prune` when you no longer need
them.

Useful flags:

- `--timeout SECONDS` — cap on the wait (default 60). If the deadline
  hits before every operation reaches a terminal status, `op wait`
  emits one extra row per unfinished op with `timed_out=true` and the
  operation's last observed status (e.g. `EXECUTING`), then exits
  with status `1`.
- `--all` — wait for every currently active operation in the project,
  not just the ones you passed.

```bash
# Block on every active op, up to 5 minutes
contree op wait --all --timeout 300
```

:::{warning}
`--all` is **project-scoped**. If multiple agents or shell sessions
share the same project, `op wait --all` will block on every active
operation across all of them — not just the ones you launched. For
multi-agent or multi-shell setups prefer the explicit
`op wait UUID1 UUID2 ...` form with the UUIDs you actually own.
:::

`op wait` exits non-zero whenever any operation finished with a
non-`SUCCESS` status (so it composes naturally with shell `&&`
chains), even when no `--timeout` was hit.

```bash
# Run fan-out + tests; bail if any leg failed
contree op wait "$A" "$B" "$C" && echo "all green" || echo "some failed"
```

### Scripting patterns

:::{note}
Shell piping and command substitution are CLI-only features. These patterns
are not available in the interactive shell.
:::

Combine `-q` with other tools:

```bash
# Show results of all active operations
contree ps -q | xargs -I {} contree show {}

# Kill all running operations
contree ps -q | xargs -I {} contree kill {}

# Launch detached, capture UUID
OP=$(contree run -d -- sleep 3600)
# ... do other work ...
contree show "$OP"
```

## Output formats

:::{note}
The `--format` flag is global and set at CLI launch time. In the interactive
shell, the format is fixed for the entire session and cannot be changed
mid-session.
:::

Use `-f` / `--format` to control output. The flag is global and goes
before the subcommand:

`default`
: Table-like output optimized for human reading. Some commands (like
  `run`) use a custom default that prints only stdout/stderr.

`table`
: Aligned columns with headers. Identical to `default` for most commands.

`csv`
: Comma-separated values with a header row. Useful for spreadsheet import
  or `cut`/`awk` processing.

`tsv`
: Tab-separated values with a header row. Works well with `column -t`.

`json`
: One JSON object per line (JSONL/NDJSON). Each output row is a separate
  JSON object. Suitable for `jq` processing.

`json-pretty`
: All rows collected into a single pretty-printed JSON array. Output is
  flushed at the end.

### Examples

```bash
# Pipe JSON to jq
contree -f json ps | jq '.uuid'

# CSV for scripting
contree -f csv images --tagged > images.csv

# Tab-separated for column alignment
contree -f tsv ps | column -t

# Get image UUID from tag
contree -f json images --prefix=ubuntu | jq -r '.uuid'
```

### Streaming behavior

`json` and `json-pretty` formatters support streaming output from
commands like `run` and `show` -- stdout/stderr are included in the
JSON payload.

`csv`, `tsv`, and `table` formatters do not include stdout/stderr
from sandbox execution. Use `default` or `json` formats to see
sandbox output.

## Session management in scripts

:::{note}
Script-level session management with `eval` and `CONTREE_SESSION` is a
CLI-only pattern. The interactive shell manages sessions automatically.
:::

The `eval $(contree use ...)` pattern exports the session key into your
shell. In scripts, set `CONTREE_SESSION` explicitly to control which
session you operate on:

```bash
#!/bin/bash
export CONTREE_SESSION=ci-build-$$
contree use tag:ubuntu:latest
contree run apt-get update -qq
contree run apt-get install -y build-essential
contree run --file ./src:/src make -C /src test
```

Using `$$` (PID) or a fixed name gives you a predictable, isolated session
per script run.

---

You now know the full CLI. Next: {doc}`configuration`.
