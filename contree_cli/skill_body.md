# ConTree

{intro}

Use `contree` from PATH. If not found, ask the user to install it:
`uv tool install contree-cli` or `pip install contree-cli` or `pipx install contree-cli`.

## Sandbox requirements (Codex)

`contree` requires network access (API calls) and write access to its
data directory (default: ``$XDG_CONFIG_HOME/contree`` — falls back to
``~/.config/contree`` when ``XDG_CONFIG_HOME`` is unset; override via
``$CONTREE_HOME``). In Codex, add to ``~/.codex/config.toml``:

```toml
[sandbox_workspace_write]
network_access = true
writable_roots = ["~/.config/contree"]
```

If the user sets ``$CONTREE_HOME`` or ``$XDG_CONFIG_HOME``, the
writable root must point at the resolved data directory instead.
Without write access, `contree` will fail with
`sqlite3.OperationalError`. If sandbox cannot be configured, stop
and ask the user.

## Quick start

{first_step}
4. If something fails or syntax is unclear, run `contree agent <topic>` BEFORE retrying.
   Topics: `sessions`, `images`, `files`, `execution`, `output`, `profiles`,
   `core` (workflow), `command` / `command_safety`, `all_commands`,
   `all` (full manual).
5. Agents must never run `contree auth`. If auth is missing or invalid, stop and ask the user to run `contree auth`.
6. Choose an explicit session key before anything else and pass `-S <key>` on every **session-scoped** command, for example `agent_<task>` or `agent_<task>_<subagent>`. Session-scoped: `use`, `run`, `cd`, `env`, `ls`, `cat`, `cp`, `file`, `session ...`, `tag` (with a single TAG arg that targets the current session image). Project-scoped commands (`images`, `auth`, `op ls/show/cancel/wait` *unless* you also want the session view, `skill ...`, `agent`, `build`, `--help`) do not need `-S`.
7. BEFORE choosing an image, list what is available.
   Projects can have thousands of images — always use `--prefix` to filter:
   `contree images --prefix python`
   `contree images --prefix compiler/ubuntu`
   Without prefix, use `-f plain` and grep: `contree -f plain images | grep tag`
   Do NOT assume `ubuntu:latest` or any other tag exists. Pick from the actual list.
8. Bootstrap the session with:
   `contree -S <key> use <image-or-tag>`
   `contree -S <key> cd /root`
9. Inspect first with read-only commands, then mutate in small rollbackable steps.
10. After installing tools or setting up an environment, TAG the image for reuse.
    Convention: `PURPOSE/OS:TAG` — designed for search with `--prefix`.
    Examples (use one that fits your env):
      `contree -S <key> tag compiler/ubuntu:gcc`      (build-essential)
      `contree -S <key> tag compiler/ubuntu:go`       (golang)
      `contree -S <key> tag compiler/alpine:rust`     (rustup + cargo)
      `contree -S <key> tag python/ubuntu:3.12-ml`    (python + numpy + pandas)
      `contree -S <key> tag node/alpine:20`           (node.js 20)
    ALWAYS search before building a new environment:
      `contree images --prefix compiler/`   find all compiler images
      `contree images --prefix python/`     find python environments
    If a matching image exists, use it instead of rebuilding:
      `contree -S <key> use <tag-from-list>`
11. If no suitable image exists, import from any Docker registry:
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
12. To inspect whether saved auth profiles actually work, run:
    `contree -f json auth ls`
    Use `-O` / `--offline` to skip network probes.

## Session bootstrap details

- In normal CLI use, `contree use IMAGE` prints a `CONTREE_SESSION` export line. Humans often use:
  `eval $(contree use tag:ubuntu:latest)`
- Agents should prefer passing `-S <key>` on every command instead of depending on exported shell state.
- `contree use --new IMAGE` is for **humans**: it allocates a random
  session key, prints the export line, and switches to it. Agents
  should instead pick an explicit fresh key and pass it on every
  command: `contree -S agent_<task>_<id> use <image-or-tag>`. The
  `--new` random key cannot be predicted ahead of time and conflicts
  with the explicit-key workflow used everywhere else in this skill.
- `contree use` without an image is read-only and prints current session state.
- Inside `contree shell`, no `eval` is needed because the shell manages the active session internally.

## Memory loop

Sessions are the agent memory model. Reuse them deliberately instead of creating fresh state by default.

1. `contree session list --filter <hint>`
2. `contree -S <name> session show` (or `contree session show <name>` -- positional, no `--session` flag)
3. `contree -S <name> use <image-or-tag>`

If nothing suitable exists, create a new explicit session key and keep using it throughout the task.
Unsure about sessions? Run `contree session --help` or `contree agent sessions`

## Core workflow

1. Discover command shape before execution when unsure:
   `contree --help`
   `contree <command> --help`
   `contree session --help`
2. Bind the task to an image or tag (pick one that actually exists
   in the project -- `contree images --prefix <hint>` first, do not
   assume `ubuntu:latest`):
   `contree -S <key> use <image-or-tag-from-list>`
3. Set the session working directory early:
   `contree -S <key> cd /root`
4. Inspect current state with:
   `images`, `ls`, `cat`, `ps`, `show`, `session`, `session show`
5. Build environments in separate operations:
   install -> verify -> build -> test
6. Tag useful results immediately. `tag` expects an **image**
   reference, not an operation UUID. From an operation, extract the
   image first:
   ```bash
   IMG=$(contree -f json op show <op-uuid> | jq -r .image)
   contree -S <key> tag "$IMG" <tag>
   ```
   For the current session image, drop the IMAGE positional:
   `contree -S <key> tag <tag>`.
7. Use `session branch`, `session checkout`, and `session rollback` around risky changes.

## Non-negotiable rules

- Always pass `-S/--session` on **session-scoped** agent commands
  (`use`, `run`, `cd`, `env`, `ls`, `cat`, `cp`, `file ...`,
  `session ...`, and `tag` when the implicit current-session image
  is the target). Do not rely on auto-generated session keys.
  Project-scoped commands (`images`, `op ls/show/cancel/wait`,
  `auth`, `skill ...`, `agent`, `build`) do not bind to a session
  and `-S` there is a no-op -- harmless but noisy.
- `contree run` is remote execution. Host files are not visible unless attached with `--file` or staged with `contree file cp`.
- Every `run` spawns a NEW isolated microVM. There is no way to exec into a running instance, attach to a process, or connect to a server started in a previous run. If you need a server response, start the server AND make the request in the same run using `-s`.
- Keep one mutating step per `contree run`.
- Do not chain stateful steps with `&&`, long shell expressions, or pipelines when the result should remain rollbackable.
- Pick the run mode by what the command needs, not by habit:
  - **Direct mode** (no `-s`, no manual `sh`) for plain executables: `contree run -- make test`.
  - **`-s` shell mode** when you need shell features (pipes, redirects, `&&`, `;`, `$` expansion). The flag joins args and passes them to `sh -c` for you, so do NOT wrap in `sh -c '...'` yourself:
    PREFER:  `contree run -s -- 'echo $HOME && ls /'`
    AVOID:   `contree run -- sh -c 'echo $HOME && ls /'`
    Quotes are only needed around args that contain metacharacters:
      `contree run -s -- apt-get install -y curl`  (no quotes)
      `contree run -s -- 'echo $HOME && ls /'`     (quotes for `&&`/`$`)
  - **`run -- sh -lc '…'`** only when a login shell is explicitly required (sourcing `/etc/profile.d`, agent-provisioned PATH, etc.). It is a niche exception, not the default.
  Unsure? Run `contree run --help` or `contree agent execution`.
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
- `contree auth profiles` is the default profile health check. Possible
  `status` values: `ok`, `timeout`, `error`, `offline mode`, `no url`,
  `inactive`. `inactive` means the token is valid but lacks the required
  sandbox permission for the configured project -- not a generic auth
  failure. `offline mode` only appears with `-O`/`--offline`.
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
  **Not** simply a session-scoped `op wait` -- with no UUIDs it
  drains the local cache of detached operations spawned in *this*
  session, advances the active branch with the result image of each
  successful non-disposable run, and records disposable branches for
  disposable detached runs. Use this when you want fan-out from a
  single session to update the session's history DAG automatically.
- `ps` / `show` / `kill`: inspect, read, or cancel operations (top-level shortcuts; multi-UUID).
- `operation` (alias `op`): grouped namespace. Use this when monitoring background work.
  - `op ls` -- same flags as `ps`, lists operations. Pipe to `-q` for UUIDs.
  - `op show UUID1 UUID2 ...` -- fetch several operation results in one call.
  - `op wait UUID1 UUID2 ...` -- block until each reaches a terminal status,
    print one operation record per completion. Default formatter
    pins `uuid`, `status`, `timed_out`, `duration` first and `error`
    last; every other scalar field the API returns is included
    between them, so the column count is not fixed. For automation
    use `-f json` (one object per line) or `-f tsv` and select
    fields explicitly.
    `--all` waits for every active op; `--timeout SECONDS` (default 60)
    causes exit code 1 if not all complete in time. Pure observer:
    does NOT advance any session branch with result images. Pairs
    best with `run -d --disposable`; for non-disposable fan-out,
    extract result images via `op show` and `contree use` them
    yourself.
  - `op cancel UUID1 UUID2 ...` -- cancel several operations, or `--all`
    to cancel every active one.

## Execution patterns

Good (pick `<base-tag>` after `contree images --prefix ubuntu` or
similar; do not assume a specific tag exists):

```bash
contree -S agent_build use <base-tag>
contree -S agent_build cd /root
contree -S agent_build run -- apt-get update -qq
contree -S agent_build run -- apt-get install -y build-essential
contree -S agent_build run -- make -C /work build
contree -S agent_build run -- make -C /work test
```

Note: pick the run mode by the command's needs, not by category.
`apt-get install -y foo` and `pip install foo` are plain executables
and run fine in direct mode. Reach for `-s` only when you need pipes,
redirects, `&&`, `;`, or variable expansion.

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
- **Direct**: `run -- make test` — clearer, no shell escaping issues.
- **`-s`**: `run -s -- 'echo $HOME && ls /'` — when you need pipes,
  redirects, `&&`, `;`, or variable expansion. Do not wrap manually
  in `sh -c '...'`; `-s` already does that. For working directory
  use `-C /path` or `contree cd /path` (NOT `cd` inside `-s`).
- **`run -- sh -lc '…'`**: only when a login shell is explicitly
  required (e.g. you depend on `/etc/profile.d/*.sh` setting PATH).
  Prefer `cd` + `-C` + explicit `env`/`-e` over login-shell magic.

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
- Pending files are merged into the next `run` -- including
  `--disposable` runs. A disposable run sees the files in its
  sandbox but does not bake them into the active branch. The
  pending queue is only cleared after a successful non-disposable
  run baking them into the next image.
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
- Roll back small units. **`rollback N` (positive) is an ABSOLUTE jump to history id N, not "back N steps".**
  Use relative forms instead:
  `contree -S <key> session rollback`           back one entry (default)
  `contree -S <key> session rollback -- -3`     back three entries (`--` so argparse doesn't eat the `-3`)
  `contree -S <key> session rollback +1`        forward one entry
  `contree -S <key> session rollback 42`        absolute jump to history id 42 (use only after `session show`)
- Always inspect with `session show` before destructive movement.
- Use `contree session show` to inspect the history DAG.
- `session show` defaults to the last 20 entries unless asked for the full history.

## Detached operations

Use detached runs whenever a step is slow (large image imports, builds,
test suites). The CLI returns immediately with an operation UUID;
monitoring is then a polling problem rather than a blocking one.

- Start long work detached: `contree -S <key> run -d -- long-job`
- Fan out several jobs in parallel: each `run -d` returns its own UUID.

Monitoring background operations:

- `contree ps` -- **EXECUTING operations only** (the default). Note
  this does **not** include `PENDING` or `ASSIGNED`; for a full
  active snapshot use `contree -f json ps -a` and filter client-side,
  or one of `--status PENDING` / `--status ASSIGNED`.
- `contree ps -a` -- include every status (completed, failed, cancelled).
- `contree ps --status FAILED` -- filter to a single status.
- `contree ps -q` -- UUIDs only, pipe-friendly.
- `contree op ls` -- alias for `ps`, identical flags.
- `contree show UUID` -- single-operation detail (status, duration,
  exit code, stdout/stderr, resulting image).
- `contree op show UUID1 UUID2 UUID3` -- fetch several operations in
  one shot. Convenient when fanning out runs and checking the batch.
- `contree op wait UUID1 UUID2 ...` -- block until each of these ops
  reaches a terminal status; print one operation record per
  completion. The default formatter shows `uuid`, `status`,
  `timed_out`, `duration` first, every other scalar API field after
  that, and `error` last -- so do not parse by fixed column count.
  For automation use `-f json` (line-delimited objects) or `-f tsv`.
  Exit code 1 if any finished non-SUCCESS, or if `--timeout`
  (default 60s) elapsed first.
- `contree op wait --all` -- block until every active op completes.
  **Warning:** `--all` is project-wide. If another agent (or another
  shell in the same project) launched operations in parallel, you will
  block on those too -- and your wait may "complete" because of an op
  you did not start. When several agents share a project, prefer the
  explicit `op wait UUID1 UUID2 ...` form with the UUIDs you actually
  spawned. Not catastrophic, just surprising; expect it.
- `contree session wait` -- session-owned drain. Without arguments
  waits for every detached op spawned in *this* session (from the
  local cache); with UUIDs waits for those specific ops. Differs
  from `op wait` in three ways:
  1. **Scope**: limited to the active session's detached cache (not
     project-wide).
  2. **Side effects**: on success it advances the active branch to
     the result image of each non-disposable detached run, and
     records `disposable-<uuid>` branches for disposable ones.
  3. **Exit semantics**: like `op wait`, promotes SUCCESS+nonzero
     exit_code to FAILED. Propagates the actual sandbox exit code
     so `session wait && next-step` behaves correctly.

  When to use which:
  - `op wait UUID...` -- you have UUIDs from any source (different
    sessions, different agents) and just want to know when they're
    done. Pure observer; does not mutate session history.
  - `session wait` -- you spawned several detached runs in this
    session and want session history to update to the chosen result
    image. Use after `run -d` (non-disposable).

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
**the active branch is not advanced**. The session history DAG still
records a `run-disposable` entry and a `disposable-<op-uuid>` branch
per spawn -- this is cosmetic, but if you fan out a lot you can prune
them with `contree -S <key> session branch --prune`.

**Capture UUIDs with `-f json`** — the default `run -d` formatter is
table/plain, not JSON, so `jq -r .uuid` against the default output
will fail. The global `-f json` must come BEFORE the subcommand.

```bash
A=$(contree -S <key> -f json run -d --disposable -- pytest tests/a | jq -r .uuid)
B=$(contree -S <key> -f json run -d --disposable -- pytest tests/b | jq -r .uuid)
C=$(contree -S <key> -f json run -d --disposable -- pytest tests/c | jq -r .uuid)
contree -S <key> op wait "$A" "$B" "$C"    # one row per op as they finish
contree -S <key> op show "$A" "$B" "$C"    # stdout/stderr per op
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
A=$(contree -S <key> -f json run -d -- apt-get install -y curl | jq -r .uuid)
B=$(contree -S <key> -f json run -d -- apt-get install -y wget | jq -r .uuid)
contree -S <key> op wait "$A" "$B"
# Pick the winning leg and re-bind the active session image to it:
IMG_A=$(contree -f json op show "$A" | jq -r .image)
contree -S <key> use "$IMG_A"
# Or tag a build artefact for reuse:
contree -S <key> tag "$IMG_A" feature/curl-tools
```

Other useful invocations:

```bash
# Wait for every active op in the project (warning: project-scoped,
# see below).
contree op wait --all

# Bound the wait (default is 60s); fail fast if jobs take too long.
contree op wait --timeout 300 "$A" "$B" "$C"

# Snapshot what is running right now. `op ls` default is EXECUTING
# only; pass `-a` to see every status, or add `--status PENDING` etc.
contree -f json op ls -a | jq -r 'select(.status == "PENDING" or .status == "ASSIGNED" or .status == "EXECUTING") | .uuid'

# Find recent failures across the project. `--status` is the correct
# filter; `-S` at this position is the global session flag, not a
# status alias.
contree -f json ps -a --status FAILED --since=1h
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

The exact wiring mechanism depends on the host:

**Codex** (`spawn_agent` API): there are no frontmatter files in the
prompt to edit. Restate the critical ConTree rules directly inside
the spawned agent's prompt:
- Always use a unique `-S <subagent_key>` on every session-scoped
  command.
- Bash (or whatever command-execution tool the host exposes) must be
  in the spawn's allowed tools.
- Reference `contree agent` for the full built-in manual.

**Claude-style hosts** (subagent frontmatter files): preload the
skill explicitly or restate the rules in the prompt.

```yaml
---
name: build-agent
tools: Bash, Read, Grep
skills:
  - contree
---
```

In every host, the subagent's command-execution tool MUST be allowed
— without it, `contree` cannot run.

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
4. **Retrieve outputs back to the host.** `contree cp` is a
   **single-file** download (no directories). For multi-file
   outputs, archive in the sandbox first, then copy the archive:
   ```bash
   contree -S agent_task_go run -C /work -s -- 'tar -cf /tmp/out.tar output/'
   contree -S agent_task_go cp /tmp/out.tar ./results/go.tar
   ```
   For a single binary, `cp` directly works:
   `contree -S agent_task_go cp /work/main ./results/go/main`.
5. **Verify after every run** — check with `contree ls` or content inspection
   that the expected output actually exists before proceeding.
6. **Save deterministic output paths** so the parent agent can collect results.

Example — build & test in three languages simultaneously. The
`tag:compiler/ubuntu:*` references below are illustrative; in a real
project list with `contree images --prefix compiler/` first and pick
an actual tag, or `contree images import golang:1.22-alpine` etc.



```bash
# Subagent 1 (Go) -- single binary, cp the file directly:
contree -S agent_task_go use tag:compiler/ubuntu:go
contree -S agent_task_go cd /work
contree -S agent_task_go run --file ./project:/work/project -- \
  go build -o /work/bin/app ./project/...
contree -S agent_task_go run -- go test ./project/...
contree -S agent_task_go cp /work/bin/app ./results/go/app

# Subagent 2 (Rust) -- target/ is a directory, archive then cp:
contree -S agent_task_rust use tag:compiler/ubuntu:rust
contree -S agent_task_rust cd /work
contree -S agent_task_rust run --file ./project:/work/project -- \
  cargo build --manifest-path /work/project/Cargo.toml
contree -S agent_task_rust run -- \
  cargo test --manifest-path /work/project/Cargo.toml
contree -S agent_task_rust run -s -- \
  'tar -cf /tmp/target.tar -C /work/project target/'
contree -S agent_task_rust cp /tmp/target.tar ./results/rust/target.tar

# Subagent 3 (Nim) -- single binary:
contree -S agent_task_nim use tag:compiler/ubuntu:nim
contree -S agent_task_nim cd /work
contree -S agent_task_nim run --file ./project:/work/project -- \
  nim compile /work/project/main.nim
contree -S agent_task_nim cp /work/project/main ./results/nim/main
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
- Supported directives: `FROM`, `RUN`, `COPY`, `ADD` (local files,
  directories, **and** `http://` / `https://` URLs; tar
  auto-extraction is not implemented), `WORKDIR`, `ENV`, `ARG`,
  `USER`. `CMD`/`ENTRYPOINT`/`LABEL`/`EXPOSE`/`VOLUME`/`STOPSIGNAL`
  /`MAINTAINER`/`HEALTHCHECK`/`ONBUILD`/`SHELL` are parsed but
  skipped with a warning.
- Multi-stage (`FROM ... AS x`, `COPY --from=x`) is not yet supported;
  use a single linear pipeline for now.
- `<CONTEXT>/.dockerignore` filters `COPY`/`ADD` walks. Globs `*` /
  `**` / `?` / `[abc]` work; trailing `/` matches a directory and
  everything below it; lines starting with `!` re-include.
- Tag the resulting image with `--tag NAME[:TAG]` to make it
  reusable.
- **`build` runs in its own session**, keyed by `abspath(CONTEXT)`
  (visible in the success output as `"session": "build:<hash>"`).
  Passing `-S <your-agent-key>` to `build` is harmless but does not
  bind it to that key. To use the resulting image, verify it from a
  normal agent session:
  ```bash
  contree build . --tag myapp:dev
  contree -S agent_verify use tag:myapp:dev
  contree -S agent_verify run -D -- myapp --version
  ```
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
