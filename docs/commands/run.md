---
icon: play
---

# run - Execute a command in the sandbox

Spawn a sandbox instance from the session image and execute a command.

## Help output

```{terminal-shell} contree run --help
```

## Quick start with `--use`

Switch session to an image and run a command in one step:

```bash
contree run --use tag:ubuntu:latest -- uname -a
```

This is equivalent to:

```bash
contree use tag:ubuntu:latest
contree run -- uname -a
```

The image switch is recorded in session history and can be rolled back
with `contree session rollback`.

## Execution modes

**Direct command** (default):

```bash
contree run uname -a
```

**Shell mode** (`-s` / `--shell`):

```bash
contree run -s -- 'echo hello && ls /'
```

Joins all command args into a single shell expression.

**Interpreter mode** (`-I` / `--interpreter`):

```bash
contree run -I ./script.sh
```

Reads a local script, strips the `#!` line, and sends the body as stdin
to `/bin/sh -s`. Enables shebang scripts:

```bash
#!/usr/bin/env -S contree run -I
echo "runs inside a ConTree sandbox"
```

**Piped stdin**:

```bash
echo 'uname -a' | contree run /bin/sh
```

Local stdin -- piped input, or even a large file -- is read in the
background and forwarded in bounded chunks rather than buffered in full
before sending, so piping in a large file doesn't load it all into
memory at once. The first chunk rides along in the spawn request itself;
the pipe is left open and more is forwarded as it arrives, until local
stdin closes. With `-d`/`--detach`, `run` doesn't return until all of
local stdin has actually been sent.

## Lifecycle

1. Resolve the session image (or switch to `--use IMAGE` first)
2. Upload any `--file` attachments (with SHA256 dedup)
3. Merge pending files from `contree file edit`/`cp`
4. POST `/v1/instances`
5. Poll until terminal status (unless `-d`)
6. Print stdout/stderr; propagate the exit code

On Ctrl-C, the first interrupt sends SIGINT to the spawned process and
keeps streaming its remaining output. A second Ctrl-C, or a failure to
deliver the first signal, cancels the whole operation.

See {doc}`/tutorial/files` for `--file` syntax details.
