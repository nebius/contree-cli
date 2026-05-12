% build command reference for the Docker-style Dockerfile interpreter
# build

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
- `--timeout SEC` -- per-`RUN` operation timeout in seconds (default 600).

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
| `FROM ref[:tag] [AS name]` | Resolves the base image. If the tag is not found locally, the build auto-imports it via `POST /v1/images/import`. `AS name` is parsed but ignored (multi-stage is Phase 2). |
| `RUN ...` | Shell-form (`RUN echo hi`) or JSON exec-form (`RUN ["echo","hi"]`). Spawns `POST /v1/instances`, polls until terminal status, captures the resulting image. |
| `COPY [--chown=...] [--chmod=...] SRC... DEST` | Walks local sources relative to the build context, applies `.dockerignore`, uploads files (with SHA256 dedup), and stages them for the next `RUN`. |
| `ADD ...` | Same as `COPY` for local files; URL/tar inputs emit a warning and are skipped. |
| `WORKDIR /path` | Sets the working directory for subsequent directives. |
| `ENV KEY=VALUE ...` | Accumulates environment variables passed to every `RUN`. |
| `ARG NAME[=DEFAULT]` | Declares a build-time variable. Overridden by `--build-arg`. |
| `USER name` | Subsequent `RUN` commands are wrapped in `su -s /bin/sh -c '<cmd>' <name>`. |
| `CMD`, `ENTRYPOINT`, `LABEL`, `EXPOSE`, `VOLUME`, `STOPSIGNAL`, `MAINTAINER`, `HEALTHCHECK`, `ONBUILD`, `SHELL` | Parsed but skipped with a warning. |

`COPY --from=stage` is a Phase 2 feature; in MVP it warns and skips.

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

RUN python -c "import sys; print('python', sys.version)"
RUN python /app/hello.py
```

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

Re-running the same command without `--no-cache` produces three layer
cache hits and no API instance spawns.

## See also

- {doc}`/commands/run` -- the single-shot version of what `RUN` does.
- {doc}`/commands/session` -- inspect or branch the layer history.
- {doc}`/commands/images` -- list, import, and tag images directly.
