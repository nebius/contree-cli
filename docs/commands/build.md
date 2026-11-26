---
icon: hammer
---

% build command reference for the Docker-style Dockerfile interpreter

# build - Build an image from a Dockerfile

Build an image from a `Dockerfile`. Each directive runs against the
contree API and produces a new image layer; successful layers are
materialised as branches named `layer:<chain-hash>` so re-running the
same Dockerfile reuses prior work.

## Synopsis

```bash
contree build [CONTEXT] [--dockerfile PATH] [--tag NAME[:TAG]]
              [--build-arg K=V ...] [--no-cache] [--timeout SEC]
```

- `CONTEXT` -- build context directory (default `.`).
- `--dockerfile PATH` -- override the default `<CONTEXT>/Dockerfile`.
- `--tag NAME[:TAG]` -- tag the final image via `PATCH /v1/images/{uuid}/tag`.
- `--build-arg KEY=VALUE` -- supply a value for an `ARG` declared in the
  Dockerfile (repeatable).
- `--no-cache` -- ignore existing `layer:<hash>` branches and rebuild.
- `--timeout SEC` -- per-instance timeout in seconds (default 600).

## Help output

```{terminal-shell} contree build --help
```

## Examples

```bash
# Simplest build; finds ./Dockerfile, tags the result
contree build . --tag myapp:dev

# Out-of-tree Dockerfile
contree build ./service --dockerfile ./service/Dockerfile.prod --tag svc:prod

# Override build-time variables
contree build . --build-arg VERSION=2.5 --build-arg DEBUG=1

# Force a rebuild ignoring cached layers
contree build . --no-cache --tag myapp:dev
```

## Supported directives (MVP)

| Directive | Behaviour |
|-----------|-----------|
| `FROM ref[:tag] [AS name]` | Resolves the base image. If the tag is not found locally, the build auto-imports it via `POST /v1/images/import`. `AS name` registers a stage for later `FROM` and `COPY --from`. |
| `RUN ...` | Shell-form (`RUN echo hi`) or JSON exec-form (`RUN ["echo","hi"]`). Spawns `POST /v1/instances`, polls until terminal status, captures the resulting image. |
| `COPY [--from=...] [--chown=...] [--chmod=...] SRC... DEST` | Transfers deterministic archives through the API. Local sources respect `.dockerignore`; `--from` accepts a stage name, index, or image reference. |
| `ADD ...` | Local paths behave like `COPY`. `https://`/`http://` URLs are **streamed straight from the source socket into `POST /v1/files`** (no temp file on disk); the URL plus its `ETag`/`Last-Modified`/`Content-MD5` validators are cached so repeat builds skip the download via a conditional `HEAD`. Tar auto-extraction is not implemented. |
| `WORKDIR /path` | Sets and preserves the working directory, including after the final `RUN`. Relative paths use the inherited image directory. |
| `ENV KEY=VALUE ...` | Passes variables to `RUN` and preserves them in the resulting image, including after the final `RUN`. |
| `ARG NAME[=DEFAULT]` | Declares a build-time variable. Overridden by `--build-arg`. |
| `USER name` | Subsequent `RUN` commands are wrapped in `su -s /bin/sh -c '<cmd>' <name>`. |
| `CMD`, `ENTRYPOINT`, `LABEL`, `EXPOSE`, `VOLUME`, `STOPSIGNAL`, `MAINTAINER`, `HEALTHCHECK`, `ONBUILD`, `SHELL` | Parsed but skipped with a warning. |

Directory transfers use archive download and upload APIs. They preserve empty
directories, symlinks, hardlinks, and permission bits. The target does not need
`sh`, `tar`, `cp`, or `mv`. File and metadata layers use an instance without a
primary command, then save its image after a normal shutdown.

Ownership changes, including restoration of `0:0`, and permission corrections
require `chown` or `chmod` in the target. These commands run directly without a shell.
Transfer failures cancel the instance and do not publish a cached layer.
The archive endpoint cannot target `/` directly. Directory copies into `/`
attach new root files at instance startup and replace existing root entries
through the live file API. Child directories use separate archives.
Root symlinks and hardlinks between these children require guest `ln` with `-T`.
Archive timestamps are not preserved. Local `ADD` archives remain ordinary files;
automatic archive extraction is not implemented.
Each outgoing archive can contain at most 10,000 entries, including its root header.
The builder rejects larger archives before uploading them.

Build tokens need `spawn` and `list`. Normal transfer shutdown also requires
`cancel` to send `TERM` to subprocess 1. Resolving an inherited working directory
on a cache miss requires `spawn_disposable`.

## Sessions and layer cache

Builds run in a dedicated session keyed by the absolute path of the
context directory: `build:<sha16(abspath(context))>`. Re-running the
same Dockerfile in the same context reuses cached layers across
invocations of `contree build`; switching to `--no-cache` rebuilds
everything.

Layers are stored as branches whose names are the chain-hash of:

```
sha256(parent_layer_hash || state(workdir/env/user/args) || directive || pending_files)
```

To inspect the resulting branches:

```bash
contree session list --filter build:
contree session show
```

:::{note}
`build` is **project-scoped from the user's point of view**: it does
not bind to the agent's `-S <key>` session. Passing `-S` is harmless
but does not move that key's image. After a successful build, attach
the result to your normal agent session by tag:

```bash
contree build . --tag myapp:dev
contree -S agent_verify use tag:myapp:dev
contree -S agent_verify run -D -- myapp --version
```
:::

## `.dockerignore`

`contree build` reads `<CONTEXT>/.dockerignore` and filters every
`COPY`/`ADD` walk. Rules are matched in order against POSIX-style
paths relative to the context root; the last matching rule wins,
so `!` re-includes a previously ignored path.

```
# .dockerignore
**/*.log
.env*
node_modules
!logs/keep.log
```

Globs:
- `*` matches a single path segment (does not cross `/`).
- `**` matches zero or more path components.
- `?` matches one character.
- `[abc]` is a character class.
- Trailing `/` matches a directory and everything below it.

The default exclude list from `run --file` (`.git`, `*.pyc`,
`__pycache__`, `.venv`, `node_modules`, `dist`, `build`, etc.) is
always applied on top of `.dockerignore`.

## Variable substitution

`$VAR` and `${VAR}` are expanded in `FROM`, `RUN`, `COPY`/`ADD`
arguments, `WORKDIR`, `ENV` values, and `USER`. The value source is:

1. `--build-arg KEY=VALUE` (highest priority for declared `ARG` names).
2. `ENV` directives processed so far.
3. `ARG` defaults.
4. Empty string for unknown names.

## End-to-end demo

A small example lives in `docs/examples/build-demo/`. The Dockerfile
exercises `FROM`, `ARG`, `ENV`, `WORKDIR`, two `COPY` directives (file
and directory), and two `RUN` directives. A `.dockerignore` filters
log files and `__pycache__` from the upload.

```dockerfile
% docs/examples/build-demo/Dockerfile
FROM python:3.12-alpine

ARG GREETING=hello
ENV APP_GREETING=${GREETING}

WORKDIR /app

COPY hello.py /app/hello.py
COPY src /app/src
ADD https://github.com/nebius/contree-cli/archive/refs/heads/master.zip /tmp/contree-cli.zip

RUN python -c "import sys; print('python', sys.version)"
RUN python -m zipfile -e /tmp/contree-cli.zip /opt/
RUN pip install --no-cache-dir /opt/contree-cli-master
RUN contree --help | head -20
RUN python /app/hello.py
```

The `ADD` line streams the zip straight from GitHub into the contree
API (no local temp file). The subsequent `RUN` steps unpack it,
`pip install` the project, and prove the installed `contree` binary
works inside the built image.

```dockerfile
% docs/examples/build-demo/.dockerignore
**/*.log
**/__pycache__
.env*
```

Build and tag it:

```bash
contree build docs/examples/build-demo --tag contree-cli-build-demo:latest
```

Expected output (truncated):

```text
[INFO] RUN spawned op=019e... RUN python -c "import sys; print('python', sys.version)"
[INFO] stdout:
python 3.12.13 ...

[INFO] RUN spawned op=019e... RUN python /app/hello.py
[INFO] stdout:
+---------------+
|     hello     |
| contree build |
+---------------+

[INFO] tagged <uuid> as contree-cli-build-demo:latest
IMAGE                                 TAG                            SESSION
<uuid>                                contree-cli-build-demo:latest  build:<sha16>
```

Re-running the same command without `--no-cache` produces layer cache
hits, and the `ADD URL` step short-circuits at the `HEAD` probe (look
for `URL cache hit (HEAD validators match)` in the log) -- no body
download, no upload.

## See also

- {doc}`/commands/run` -- the single-shot version of what `RUN` does.
- {doc}`/commands/session` -- inspect or branch the layer history.
- {doc}`/commands/images` -- list, import, and tag images directly.
