---
icon: hammer
---

# Building from a Dockerfile

`contree build` turns a familiar `Dockerfile` into a ConTree image. Each
directive becomes one image layer, every layer is a real session
checkpoint, and re-running the same `Dockerfile` reuses prior layers
through a content-addressed cache. This tutorial walks through the
`build-demo` example shipped with the repo so you can see the moving
parts end-to-end.

## The example project

The tree at `docs/examples/build-demo` contains a minimal Python app
plus a Dockerfile that exercises the directives most builds actually
use:

```
docs/examples/build-demo/
├── .dockerignore
├── Dockerfile
├── hello.py
└── src/
    ├── __init__.py
    └── banner.py
```

`hello.py` reads a greeting from an environment variable and prints a
boxed banner; `src/banner.py` provides the box renderer. Nothing
exotic -- it just gives the Dockerfile something to `COPY` and `RUN`.

The Dockerfile itself:

```{literalinclude} /examples/build-demo/Dockerfile
:language: dockerfile
```

Six directives, in order:

1. `FROM python:3.12-alpine` -- resolves the base image. If
   `tag:python:3.12-alpine` is not already in the project, `contree build`
   auto-imports it from the registry.
2. `ARG GREETING=hello` and `ENV APP_GREETING=${GREETING}` -- declare a
   build-time variable and pin its value into a runtime environment
   variable that the app will read.
3. `WORKDIR /app` -- sets the working directory for everything below.
4. Two `COPY` directives stage local files (`hello.py` and the `src/`
   directory) into the build's pending uploads.
5. `ADD https://...master.zip /tmp/contree-cli.zip` -- streams a remote
   archive straight from GitHub into the contree file store, without
   creating a local temp file.
6. Five `RUN` directives prove the toolchain works, unpack the zip,
   install the CLI from source, and run the demo app.

## Build context and `.dockerignore`

The first positional argument to `contree build` is the **build
context** -- the directory that anchors every `COPY` and `ADD` source
path. Anything outside that directory is invisible to the build. In
this example the context is `docs/examples/build-demo`, and the
Dockerfile sits at the top of it.

A `.dockerignore` next to the Dockerfile keeps junk out of the upload:

```{literalinclude} /examples/build-demo/.dockerignore
:language: text
```

The matcher uses the same rule set as `run --file`: `*` is a
single-segment wildcard, `**` crosses directories, `?` matches one
character, and a leading `!` re-includes a previously ignored path.
The last matching rule wins. On top of your `.dockerignore`, the CLI
always filters `.git`, `__pycache__`, `*.pyc`, `.venv`,
`node_modules`, `dist`, and `build`, so you do not need to repeat the
usual suspects.

## Your first build

From the repository root, run:

```bash
contree build docs/examples/build-demo --tag contree-cli-build-demo:latest
```

You should see one log line per directive plus a stdout dump after
each `RUN`:

```text
[INFO] FROM python:3.12-alpine -> tag:python:3.12-alpine
[INFO] COPY hello.py -> /app/hello.py
[INFO] COPY src -> /app/src
[INFO] ADD https://.../master.zip -> /tmp/contree-cli.zip
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

The final tagged image is now usable everywhere a tag is accepted:

```bash
eval $(contree use tag:contree-cli-build-demo:latest)
contree run python /app/hello.py
```

## Layer cache: the second build is free

Run the same command a second time. Every step prints **cache hit**
and the build finishes in seconds without spawning a single instance.

The cache key for each layer is a chain hash:

```
sha256(parent_layer_hash || state(workdir/env/user/args)
       || directive || pending_files)
```

That means a layer is reused if and only if:

- the previous layer was identical,
- the directive text is byte-for-byte the same,
- the resolved environment (`WORKDIR`, `ENV`, `USER`, `ARG`) matches, and
- for `COPY`/`ADD`, the **content** of the staged files matches (the
  SHA-256 of every uploaded file, not their timestamps).

Edit `hello.py`, run the build again, and only the last `RUN` step
plus everything depending on it rebuilds. The earlier `RUN python -c
'import sys; print(sys.version)'` layer is reused because it has no
dependency on `hello.py`.

Each cached layer is materialised as a branch named
`layer:<chain-hash>` inside a session keyed by the absolute path of
the context directory: `build:<sha16(abspath(context))>`. So the
cache is **per-context-path** -- moving the directory or building from
a sibling worktree starts a fresh cache.

To inspect the layer history:

```bash
contree session list --filter build:
contree session use build:<sha16>
contree session show
```

`contree session show` prints the DAG with one row per layer and the
chain hash visible in the branch column. Switching to a `layer:` branch
puts you on that layer's image, so you can `contree run` against any
intermediate snapshot to debug a step in isolation.

To force a rebuild ignoring all cached layers:

```bash
contree build docs/examples/build-demo --no-cache \
  --tag contree-cli-build-demo:latest
```

## Build args and variable substitution

Variables (`$VAR` and `${VAR}`) expand in `FROM`, `RUN`, `COPY`/`ADD`
arguments, `WORKDIR`, `ENV` values, and `USER`. The lookup order is:

1. `--build-arg KEY=VALUE` for any `ARG` already declared.
2. `ENV` directives processed so far.
3. `ARG` defaults from the Dockerfile.
4. Empty string for unknown names.

The demo declares `ARG GREETING=hello` and uses it through
`ENV APP_GREETING=${GREETING}`. Override it at the CLI:

```bash
contree build docs/examples/build-demo --build-arg GREETING=ciao \
  --tag contree-cli-build-demo:ciao
```

The final `RUN python /app/hello.py` step now prints `ciao` in the
boxed banner because the chain hash of the layer that ran
`ENV APP_GREETING=...` changed, invalidating every layer below it.

## `ADD URL` streams without a temp file

The `ADD` line in the demo points at a GitHub archive:

```dockerfile
ADD https://github.com/nebius/contree-cli/archive/refs/heads/master.zip /tmp/contree-cli.zip
```

`contree build` opens the HTTP connection and pipes the response body
**directly** into `POST /v1/files` -- the bytes never touch your local
disk. The CLI also remembers the URL's `ETag`, `Last-Modified`, and
`Content-MD5` validators in the per-context cache. On the next build
it issues a conditional `HEAD` first; if the validators match, the
upload is skipped entirely and the log line reads
`URL cache hit (HEAD validators match)`.

Two things this does **not** do:

- It does not extract tarballs/zips. Use a `RUN python -m zipfile -e`
  (or `tar xf`) directive when you need extraction, exactly like the
  demo does.
- It does not follow private auth -- the request is anonymous. Mirror
  the asset to a public URL, or `COPY` it from your build context.

## Supported and skipped directives

The MVP interpreter implements the directives most Dockerfiles
actually rely on:

| Implemented | Notes |
|-------------|-------|
| `FROM ref[:tag] [AS name]` | Auto-imports missing tags; `AS name` is parsed but multi-stage is not yet executed. |
| `RUN ...` | Shell-form and JSON exec-form. Spawns one instance per `RUN`. |
| `COPY [--chown=] [--chmod=] SRC... DEST` | Honours `.dockerignore`, dedups by SHA-256. |
| `ADD ...` | Local paths behave like `COPY`; URLs stream through `POST /v1/files`. |
| `WORKDIR`, `ENV`, `ARG`, `USER` | Accumulated and applied to subsequent steps. |

Directives that are parsed and **skipped with a warning** (the build
continues, the image is still produced):

`CMD`, `ENTRYPOINT`, `LABEL`, `EXPOSE`, `VOLUME`, `STOPSIGNAL`,
`MAINTAINER`, `HEALTHCHECK`, `ONBUILD`, `SHELL`,
`COPY --from=stage`.

ConTree images are filesystem snapshots, not OCI runtime configs, so
`CMD`/`ENTRYPOINT` have nowhere to live -- you express the entrypoint
explicitly at `contree run` time instead.

## When to reach for `build` vs `run`

The same image you can produce with `contree build` can be produced
by hand with a sequence of `contree run` calls. Pick the right tool:

| Situation | Prefer |
|-----------|--------|
| You already have a working `Dockerfile` | `contree build` -- just reuse it. |
| You want reproducible, cacheable setup driven from version control | `contree build`. |
| You are still experimenting and do not know the final steps | `contree run` interactively; tag a checkpoint when you are happy. |
| You need `CMD`/`ENTRYPOINT`/`HEALTHCHECK` semantics | Neither -- those are runtime concerns for OCI runtimes, not for ConTree. |
| You want multi-stage builds today | Not yet -- stage `AS` parses but is skipped. Track Phase 2. |

## Cheat sheet

```bash
# Simplest build; finds ./Dockerfile in the context, tags the result.
contree build . --tag myapp:dev

# Out-of-tree Dockerfile.
contree build ./service \
  --dockerfile ./service/Dockerfile.prod \
  --tag svc:prod

# Override build-time variables.
contree build . \
  --build-arg VERSION=2.5 \
  --build-arg DEBUG=1

# Force a full rebuild.
contree build . --no-cache --tag myapp:dev

# Raise the per-RUN timeout to 30 minutes.
contree build . --timeout 1800 --tag myapp:dev

# Inspect the build's layer history.
contree session list --filter build:
contree session use build:<sha16>
contree session show
```

---

You now have a tagged image that came from a `Dockerfile`, a cached
layer history you can branch off, and a feel for which directives
behave and which are parsed-but-skipped. Next, see
{doc}`workflows` for scripting builds into pipelines, or
{doc}`/commands/build` for the full reference.
