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

When stdin is not a TTY, it is read, base64-encoded, and sent as the
`stdin` field.

## Lifecycle

1. Resolve the session image (or switch to `--use IMAGE` first)
2. Upload any `--file` attachments (with SHA256 dedup)
3. Merge pending files from `contree file edit`/`cp`
4. POST `/v1/instances`
5. Poll until terminal status (unless `-d`)
6. Print stdout/stderr; propagate the exit code

On Ctrl-C the operation is cancelled via DELETE.

See {doc}`/tutorial/files` for `--file` syntax details.
