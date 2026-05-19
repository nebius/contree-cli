# ConTree

{intro}

Use `contree` from PATH. If not found, ask the user to install it:
`uv tool install contree-cli` or `pip install contree-cli` or `pipx install contree-cli`.

## Sandbox requirements (Codex)

`contree` requires network access (API calls) and write access to its
data directory (`~/.config/contree-cli`). In Codex, add to `~/.codex/config.toml`:

```toml
[sandbox_workspace_write]
network_access = true
writable_roots = ["~/.config/contree-cli"]
```

Without this, `contree` will fail with `sqlite3.OperationalError`.
If sandbox cannot be configured, stop and ask the user.

## Quick start

{first_step}
4. If something fails or syntax is unclear, run `contree agent <topic>` BEFORE retrying.
   Topics: sessions, images, files, execution, output, profiles, commands.
5. Agents must never run `contree auth`. If auth is missing or invalid, stop and ask the user to run `contree auth`.
5. Choose an explicit session key before anything else and pass it on every command with `-S`, for example `agent_<task>` or `agent_<task>_<subagent>`.
6. BEFORE choosing an image, list what is available.
   Projects can have thousands of images — always use `--prefix` to filter:
   `contree images --prefix python`
   `contree images --prefix compiler/ubuntu`
   Without prefix, use `-f plain` and grep: `contree -f plain images | grep tag`
   Do NOT assume `ubuntu:latest` or any other tag exists. Pick from the actual list.
7. Bootstrap the session with:
   `contree -S <key> use <image-or-tag>`
   `contree -S <key> cd /root`
8. Inspect first with read-only commands, then mutate in small rollbackable steps.
9. After installing tools or setting up an environment, TAG the image for reuse.
   Convention: `PURPOSE/OS:TAG` — designed for search with `--prefix`.
   Examples:
     `contree -S <key> tag compiler/ubuntu:gcc`      (build-essential)
     `contree -S <key> tag compiler/ubuntu:go`       (golang)
     `contree -S <key> tag compiler/alpine:rust`     (rustup + cargo)
     `contree -S <key> tag python/ubuntu:3.12-ml`    (python + numpy + pandas)
     `contree -S <key> tag node/alpine:20`           (node.js 20)
   ALWAYS search before building a new environment:
     `contree images --prefix compiler/`   find all compiler images
     `contree images --prefix python/`     find python environments
   If a matching image exists, use it instead of rebuilding:
     `contree -S <key> use tag:compiler/ubuntu:gcc`
10. If no suitable image exists, import from any Docker registry:
   `contree images import ubuntu:noble`            (Docker Hub)
   `contree images import --timeout 600 ubuntu:noble`
   `contree images import python:3.12-slim`        (Docker Hub)
   `contree images import golang:1.22-alpine`      (Docker Hub)
   `contree images import ghcr.io/org/image:tag`   (GitHub Container Registry)
   `contree images import registry.example.com/img:tag`  (private)
   Import is async — the CLI polls until complete. Press Ctrl+C to cancel.
   Use `--timeout <seconds>` to raise or lower the import operation timeout.
   After import, the image is available as `tag:<name>`.
   For private registries, use `--username` (password is prompted).
   TIP: importing a ready-made image is faster than installing from scratch.
   For example, `images import rust:1.79-slim` gives you a full Rust
   toolchain in seconds, vs minutes of `curl rustup | sh`.
11. To inspect whether saved auth profiles actually work, run:
   `contree -f json auth ls`
   Use `-O` / `--offline` to skip network probes.

## Session bootstrap details

- In normal CLI use, `contree use IMAGE` prints a `CONTREE_SESSION` export line. Humans often use:
  `eval $(contree use tag:ubuntu:latest)`
- Agents should prefer passing `-S <key>` on every command instead of depending on exported shell state.
- `contree use --new IMAGE` creates a fresh session key. Use it when you explicitly want new state instead of resuming an old session.
- `contree use` without an image is read-only and prints current session state.
- Inside `contree shell`, no `eval` is needed because the shell manages the active session internally.

## Memory loop

Sessions are the agent memory model. Reuse them deliberately instead of creating fresh state by default.

1. `contree session list --filter <hint>`
2. `contree session show --session <name>`
3. `contree -S <name> use <image-or-tag>`

If nothing suitable exists, create a new explicit session key and keep using it throughout the task.
Unsure about sessions? Run `contree session --help` or `contree agent sessions`

## Core workflow

1. Discover command shape before execution when unsure:
   `contree --help`
   `contree <command> --help`
   `contree session --help`
2. Bind the task to an image or tag:
   `contree -S <key> use tag:ubuntu:latest`
3. Set the session working directory early:
   `contree -S <key> cd /root`
4. Inspect current state with:
   `images`, `ls`, `cat`, `ps`, `show`, `session`, `session show`
5. Build environments in separate operations:
   install -> verify -> build -> test
6. Tag useful results immediately:
   `contree tag <result-uuid> <tag>`
7. Use `session branch`, `session checkout`, and `session rollback` around risky changes.

## Non-negotiable rules

- Always pass `-S/--session` on agent-driven commands. Do not rely on auto-generated sessions.
- `contree run` is remote execution. Host files are not visible unless attached with `--file` or staged with `contree file cp`.
- Every `run` spawns a NEW isolated microVM. There is no way to exec into a running instance, attach to a process, or connect to a server started in a previous run. If you need a server response, start the server AND make the request in the same run using `-s`.
- Keep one mutating step per `contree run`.
- Do not chain stateful steps with `&&`, long shell expressions, or pipelines when the result should remain rollbackable.
- ALWAYS use `-s` (shell mode) when passing shell commands as strings. Do NOT wrap in `sh -lc '...'` or `sh -c '...'` manually:
  WRONG: `contree run -- sh -lc 'apt-get update -qq'`
  RIGHT: `contree run -s -- apt-get update -qq`
  The `-s` flag joins all args and passes to `sh -c` automatically.
  Quotes are only needed for shell metacharacters like `&&`, `|`, `$`:
    `contree run -s -- apt-get install -y curl`  (no quotes needed)
    `contree run -s -- 'echo $HOME && ls /'`     (quotes needed for && and $)
  Use direct mode (no `-s`) for simple executables: `contree run -- make test`
  Unsure? Run `contree run --help` or `contree agent execution`
- Prefer non-disposable runs when you want the environment to persist; use `--disposable` only for throwaway checks.
- Prefer `--file` over `file cp` when you need files for a single run.
  `file cp` stages files in the session for ALL future runs.
  `--file` attaches files to just one run — cleaner and more explicit:
  RIGHT: `contree run --file ./src:/app/src -- make -C /app/src`
  AVOID: `contree file cp ./src /app/src` then `contree run -- make -C /app/src`
  Use `file cp` only when you need files to persist across multiple runs without re-attaching.
  Unsure? Run `contree file --help` or `contree agent files`
- For detached (background) runs use `-d`/`--detach`:
  `contree run -d -- long-running-server`
  Then check: `contree ps`, `contree show UUID`
- Use `contree cd /path` or `contree run -C /path` to set the working directory.
  Do NOT use `cd` inside `-s` shell expressions — it does not persist and
  clutters the command:
  RIGHT: `contree cd /root/project` then `contree run -- make test`
  RIGHT: `contree run -C /root/project -- make test`   (per-run override)
  WRONG: `contree run -s -- 'cd /root/project && make test'`
- Prefer absolute paths for sandbox workdirs and destination paths.
- Search for reusable images before rebuilding: `contree images --prefix <prefix>`.
- For common tools (rust, go, node, python, gcc, etc.) PREFER importing a ready-made
  Docker image over manual installation. It is faster and more reliable:
  Timeout values are in seconds when you use `--timeout`.
  `contree images import rust:1-slim`
  `contree images import --timeout 600 rust:1-slim`
  `contree images import golang:1.22-alpine`
  `contree images import node:20-slim`
  Only install manually when you need a custom combination not available as a single image.
- ALWAYS tag images after installing tools or setting up environments. Without tags, useful images are lost — they can only be found by UUID. Tags make images discoverable by future sessions and other agents.
  Unsure about tagging? Run `contree tag --help` or `contree agent images`
- Stay inside `contree ...` when the task specifically wants sandboxed execution rather than host-local commands.
- If auth is missing, the CLI raises an API error that effectively means "No token configured. Run `contree auth` first." Treat that as a user action item, not something the agent should self-fix.
- `contree auth profiles` is the default profile health check and shows `status` values `ok`, `timeout`, `error`, or `offline`.
- `contree auth profiles --offline` is only for explicit no-network situations.
- For automation, prefer `contree -f json auth profiles` over table output.

## Command map

- `use`: bind the session to an image or reusable tag.
- `run`: execute a command in the current session image.
- `build`: interpret a `Dockerfile` and produce a tagged image, reusing
  cached layers per context directory. Prefer this over hand-running
  each Dockerfile step when one already exists.
- `ls` / `cat`: inspect files from the image without spawning a VM.
- `cp`: download a file from the image to the host.
- `file edit`: open a remote file in a host editor and stage it for the next run.
- `file cp`: upload a local file and stage it for the next run.
- `file ls`: list uploaded files; rows produced from this host carry a
  `source` field (host path for `run --file` / `COPY`, URL for
  `ADD URL`). Add `-q` for a tight `uuid sha256 source` view.

  **`source` is THIS-MACHINE ONLY.** The mapping lives in the local
  CLI SQLite cache (`$CONTREE_HOME/cli/sessions/<profile>.db`) keyed
  by `path + inode + mtime + size` for host paths and by the URL
  itself for URL fetches. It is not synced anywhere. Rows uploaded
  from a different machine, by another teammate, or before tracking
  landed will show an empty `source` -- that is expected, not a bug.
  When working across hosts, treat the remote `uuid`/`sha256` as the
  authoritative identifier and never rely on `source` resolving.
- `session branch`: create an experimental branch.
- `session checkout`: switch active branch.
- `session rollback`: move the active branch pointer backward.
- `session wait`: wait for active operations, or specific operation UUIDs.
- `ps` / `show` / `kill`: inspect, read, or cancel operations (top-level shortcuts; multi-UUID).
- `operation` (alias `op`): grouped namespace. Use this when monitoring background work.
  - `op ls` -- same flags as `ps`, lists operations. Pipe to `-q` for UUIDs.
  - `op show UUID1 UUID2 ...` -- fetch several operation results in one call.
  - `op wait UUID1 UUID2 ...` -- block until each reaches a terminal status,
    print one row per completion (uuid|status|duration|timed_out).
    `--all` waits for every active op; `--timeout SECONDS` (default 60)
    causes exit code 1 if not all complete in time. Pure observer:
    does NOT advance any session branch with result images. Pairs
    best with `run -d --disposable`; for non-disposable fan-out,
    extract result images via `op show` and `contree use` them
    yourself.
  - `op cancel UUID1 UUID2 ...` -- cancel several operations, or `--all`
    to cancel every active one.

## Execution patterns

Good:

```bash
contree -S agent_build use tag:ubuntu:latest
contree -S agent_build cd /root
contree -S agent_build run -s -- apt-get update -qq
contree -S agent_build run -s -- apt-get install -y build-essential
contree -S agent_build run -- make -C /work build
contree -S agent_build run -- make -C /work test
```

Note: use `-s` for shell commands (apt-get, pip, etc.) and direct mode
for simple executables (make, cargo, python).

Bad — chaining multiple steps:

```bash
contree -S agent_build run -s -- 'apt-get update && apt-get install -y build-essential && make test'
```

Why: a chained run collapses several mutable steps into one history entry, which weakens rollback, branching, and reuse.

## Environment variables and PATH

After installing tools that place binaries outside the default PATH
(rustup, nvm, pyenv, etc.), you need to set env vars and persist them
into the image so subsequent runs see them.

Env vars are NEVER preserved in the image automatically. You must
explicitly pass `--preserve-env` to save them into the resulting image.

**`contree env`** sets session-level vars that the CLI sends on every run.
Without `--preserve-env` they are injected per-run but not baked into images:

```bash
contree -S agent_build env PATH=/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/sbin
contree -S agent_build run -- cargo build              # PATH injected but NOT saved
contree -S agent_build run --preserve-env -- cargo build  # PATH saved into image
contree -S agent_build run -- cargo test                  # PATH still there from image
```

**`-e` + `--preserve-env`** — same idea for one-off vars:

```bash
contree run --preserve-env -e PATH="/root/.cargo/bin:/usr/bin:/bin" -- cargo build
contree run -- cargo test   # PATH persists from preserved image
```

**`-e` without `--preserve-env`** is ephemeral — gone on next run:

```bash
contree run -e DEBUG=1 -- ./app
```

Do not use absolute binary paths — they are brittle and do not propagate
to child processes. Use `env` or `-e` to set PATH instead.

## `run` modes

`contree run` has four practical modes:

- **Direct command** (default) — each arg is a separate argv entry:
  `contree run -- uname -a`

- **Shell mode** (`-s`) — joins args into a single `sh -c` expression.
  Use when you need pipes, redirects, `&&`, or variable expansion:
  `contree run -s -- 'echo $HOME && ls /'`
  `contree run -s -- 'cat /etc/passwd | grep root'`

- **Interpreter mode** (`-I`) — runs a local script file in the sandbox:
  `contree run -I ./script.sh`

- **Piped stdin** — non-TTY stdin is forwarded to the sandbox:
  `echo 'uname -a' | contree run /bin/sh`

When to use shell mode vs direct:
- Direct: `run -- make test` — clearer, no shell escaping issues
- Shell: `run -s -- 'cd /app && make test'` — when you need shell features
- Prefer `sh -lc` in direct mode for login shell: `run -- sh -lc 'command'`

## Interactive shell behavior

Inside `contree shell`:

- Bare commands are implicit sandbox `run` commands.
- Bare `ls` and `cat` are special: they map to fast API inspection commands instead of spawning a VM.
- If pending files exist, or if `ls`/`cat` arguments use flags or globs, the shell falls back to running them inside the sandbox.
- Bare editor names like `vim`, `vi`, `nvim`, and `nano` map to `contree file edit`.
- Flags like `-D`, `-e`, `-d`, or `--file` require the explicit `contree run` prefix.

This means shell transcripts are convenient, but agent instructions should still be precise about whether a command is expected to use API inspection or remote execution.

## Files and staged changes

- Inline injection: `contree run --file ./app.py:/app/app.py -- python /app/app.py`
- Stage for next run: `contree file cp ./config.yaml /etc/app/config.yaml`
- Edit an existing remote file: `contree file edit /etc/app/config.ini`
- Pending files are merged automatically into the next non-disposable `run`.
- Explicit `--file` mappings win over pending files on the same destination path.
- Directory attachments recurse and exclude common junk by default: `.*`, `.git`, `*.pyc`, `__pycache__`, `.venv`, `.mypy_cache`, `.pytest_cache`, `node_modules`, `dist`, `build`.
- Add more directory exclusion patterns with `--file-excludes`.
- The CLI keeps a local upload cache keyed by path, inode, mtime, and size, so repeated attachments often avoid re-uploading.

Use staged files when several edits should land together on the next run. Use `--file` when the file is only needed for a single execution.

## Sessions, branching, and rollback

- Sessions are durable and backed by local SQLite state.
- Use the same session key to resume task memory later.
- Create branches before risky work:
  `contree -S <key> session branch experiment`
  `contree -S <key> session checkout experiment`
- Roll back small units:
  `contree -S <key> session rollback 1`
- `session rollback` supports absolute IDs and relative navigation; inspect with `session show` before destructive movement.
- Use `contree session show` to inspect the history DAG.
- `session show` defaults to the last 20 entries unless asked for the full history.

## Detached operations

Use detached runs whenever a step is slow (large image imports, builds,
test suites). The CLI returns immediately with an operation UUID;
monitoring is then a polling problem rather than a blocking one.

- Start long work detached: `contree -S <key> run -d -- long-job`
- Fan out several jobs in parallel: each `run -d` returns its own UUID.

Monitoring background operations:

- `contree ps` -- active operations (PENDING, ASSIGNED, EXECUTING).
- `contree ps -a` -- include completed/failed/cancelled.
- `contree ps -q` -- UUIDs only, pipe-friendly.
- `contree op ls` -- alias for `ps`, identical flags.
- `contree show UUID` -- single-operation detail (status, duration,
  exit code, stdout/stderr, resulting image).
- `contree op show UUID1 UUID2 UUID3` -- fetch several operations in
  one shot. Convenient when fanning out runs and checking the batch.
- `contree op wait UUID1 UUID2 ...` -- block until each of these ops
  reaches a terminal status; print one row per completion
  (uuid|status|duration|timed_out). Exit code 1 if any finished
  non-SUCCESS, or if `--timeout` (default 60s) elapsed first.
- `contree op wait --all` -- block until every active op completes.
  **Warning:** `--all` is project-wide. If another agent (or another
  shell in the same project) launched operations in parallel, you will
  block on those too -- and your wait may "complete" because of an op
  you did not start. When several agents share a project, prefer the
  explicit `op wait UUID1 UUID2 ...` form with the UUIDs you actually
  spawned. Not catastrophic, just surprising; expect it.
- `contree session wait` -- session-scoped variant; waits for active
  ops of the current session.

Cancelling:

- `contree kill UUID` -- single operation.
- `contree op cancel UUID1 UUID2` -- batch of UUIDs.
- `contree op cancel --all` -- every active operation (use sparingly).

Common patterns:

**Fan-out + wait** — the canonical parallel pattern.

`op wait` is a pure observer: it polls the API and prints
completions, but it **does not touch session state**. That has
important consequences for whether fan-out is disposable or not.

PREFERRED: fan-out + wait with `--disposable`. The result images of
each leg are discarded, you only care about exit codes / stdout, and
the session stays clean.

```bash
A=$(contree run -d --disposable -- pytest tests/a | jq -r .uuid)
B=$(contree run -d --disposable -- pytest tests/b | jq -r .uuid)
C=$(contree run -d --disposable -- pytest tests/c | jq -r .uuid)
contree op wait "$A" "$B" "$C"             # one row per op as they finish
contree op show "$A" "$B" "$C"             # stdout/stderr per op
```

NON-DISPOSABLE fan-out works, but with caveats:

- Each `run -d` creates a `detached-<op-uuid>` branch in the session.
  It points at the **starting** image, not at the eventual result.
- `op wait` polls until completion but does **not** advance any
  branch with the result image. After the wait, the session looks
  exactly like it did before the wait, just with `detached-*`
  branches accumulating.
- The actual result images live only on the server. To use one,
  extract it from `op wait` / `op show` output and attach it
  explicitly:

```bash
A=$(contree run -d -- apt-get install -y curl | jq -r .uuid)
B=$(contree run -d -- apt-get install -y wget | jq -r .uuid)
contree op wait "$A" "$B"
# Pick the winning leg and re-bind the active session image to it:
IMG_A=$(contree -f json op show "$A" | jq -r .image)
contree use "$IMG_A"
# Or tag a build artefact for reuse:
contree tag "$IMG_A" feature/curl-tools
```

Other useful invocations:

```bash
# Wait for every active op in the project (warning: project-scoped,
# see below).
contree op wait --all

# Bound the wait (default is 60s); fail fast if jobs take too long.
contree op wait --timeout 300 "$A" "$B" "$C"

# Snapshot what is running right now.
contree -f json op ls | jq '.uuid'

# Find recent failures across the project.
contree -f json ps -a -S FAILED --since=1h
```

## Output and automation

- Prefer structured output in automation with `-f json`, `-f json-pretty`, `-f csv`, or `-f tsv`.
- `contree run` propagates the sandbox exit code, so it works naturally in scripts.
- For executable host scripts that should run inside the sandbox, prefer `contree run -I`.
- If the environment might drop session-related env vars, keep `-S <key>` on every command instead of relying on exported state.
- Global flags like `-f json` must go before the subcommand.
- `run` with the default formatter prints raw stdout/stderr, not structured rows.
- `cat` and `cp` are content-oriented commands; do not assume they will emit table/json-style records like listing commands do.

## Using contree in subagents

This skill teaches how to wire subagents correctly. It does NOT grant
permission to spawn them — that requires explicit user authorization
or a top-level agent policy.

### Wiring a subagent for contree

Subagents do NOT inherit skills automatically. You MUST either:

1. Preload the skill in subagent frontmatter:
   ```yaml
   ---
   name: build-agent
   tools: Bash, Read, Grep
   skills:
     - contree
   ---
   ```

2. Or restate the critical rules directly in the subagent prompt:
   - Always use `-S <key>` on every command
   - Use `contree agent` for the full built-in manual
   - Bash must be in the subagent's allowed tools

The subagent's `allowed-tools` MUST include `Bash` — without it,
contree cannot execute.

### Session isolation (mandatory)

Every subagent MUST use its own unique session key. Sharing sessions
between parallel subagents corrupts image state.

Convention: `agent_<task>_<concern>`, e.g.:
- `agent_build_go`, `agent_build_rust`, `agent_build_nim`
- `agent_solve_approach1`, `agent_solve_approach2`

### Parallel execution pattern

When a task has multiple independent concerns (languages, approaches,
experiments), launch one subagent per concern with isolated sessions:

1. **One concern per subagent** — one language, one approach, one experiment.
2. **Search for existing images first** — `contree images --prefix compiler/`.
   Do NOT assume any tag exists. Pick from the actual list.
3. **Use `--file` to inject local source** into the sandbox:
   `contree -S agent_task_go run --file ./src:/work/src -- go build /work/src/...`
4. **Use `contree cp` to retrieve outputs** back to the host:
   `contree -S agent_task_go cp /work/output ./results/go/`
5. **Verify after every run** — check with `contree ls` or content inspection
   that the expected output actually exists before proceeding.
6. **Save deterministic output paths** so the parent agent can collect results.

Example — build & test in three languages simultaneously:

```bash
# Subagent 1 (Go):
contree -S agent_task_go use tag:compiler/ubuntu:go
contree -S agent_task_go cd /work
contree -S agent_task_go run --file ./project:/work/project -- go build ./project/...
contree -S agent_task_go run -- go test ./project/...
contree -S agent_task_go cp /work/project/output ./results/go/

# Subagent 2 (Rust):
contree -S agent_task_rust use tag:compiler/ubuntu:rust
contree -S agent_task_rust cd /work
contree -S agent_task_rust run --file ./project:/work/project -- cargo build --manifest-path /work/project/Cargo.toml
contree -S agent_task_rust run -- cargo test --manifest-path /work/project/Cargo.toml
contree -S agent_task_rust cp /work/project/target ./results/rust/

# Subagent 3 (Nim):
contree -S agent_task_nim use tag:compiler/ubuntu:nim
contree -S agent_task_nim cd /work
contree -S agent_task_nim run --file ./project:/work/project -- nim compile /work/project/main.nim
contree -S agent_task_nim cp /work/project/main ./results/nim/
```

Each subagent works in complete isolation. The parent agent collects
`./results/<lang>/` after all subagents finish.

## Building from a Dockerfile

When a repo already has a `Dockerfile`, do not reproduce each step by
hand. Run `contree build` instead:

```bash
contree build . --tag myapp:dev
contree build ./app --dockerfile ./app/Dockerfile.prod --tag svc:prod
contree build . --build-arg VERSION=1.2
contree build . --no-cache
```

- Cache is keyed by `abspath(CONTEXT)`. Same context + same Dockerfile
  + same build args = full layer cache hit on re-runs.
- Supported directives: `FROM`, `RUN`, `COPY`, `ADD` (local paths
  only), `WORKDIR`, `ENV`, `ARG`, `USER`. `CMD`/`ENTRYPOINT`/`LABEL`
  /`EXPOSE`/`VOLUME`/`STOPSIGNAL`/`MAINTAINER`/`HEALTHCHECK`/`ONBUILD`
  /`SHELL` are parsed but skipped with a warning.
- Multi-stage (`FROM ... AS x`, `COPY --from=x`) is not yet supported;
  use a single linear pipeline for now.
- `<CONTEXT>/.dockerignore` filters `COPY`/`ADD` walks. Globs `*` /
  `**` / `?` / `[abc]` work; trailing `/` matches a directory and
  everything below it; lines starting with `!` re-include.
- Tag the resulting image with `--tag NAME[:TAG]` to make it
  reusable.

Use `contree build --help` for the full flag list.

## Built-in manual

If something doesn't work or you need more details on a specific topic,
consult the built-in manual:

  `contree agent`                full manual
  `contree agent sessions`       session management details
  `contree agent files`          file attachment syntax and caching
  `contree agent images`         tagging, importing, conventions
  `contree agent execution`      run modes, shebang, detach
  `contree agent output`         JSON/CSV output and jq examples
  `contree agent profiles`       multi-project setup

Each topic is self-contained with examples and edge cases.
{fallback}{references}
