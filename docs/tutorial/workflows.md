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
