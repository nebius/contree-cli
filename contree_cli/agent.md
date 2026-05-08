ConTree CLI Manual
==================

Core workflow
=============

Agent protocol — follow this sequence for every task:

1. Choose a session key: agent_<task> or agent_<task>_<subagent>.
   Pass it with -S on every command. Do not rely on env vars.

2. Resume or start a session:
     contree session list --filter <hint>
     contree -S <key> use <image-or-tag>

3. Set working directory early:
     contree -S <key> cd /work

4. Inspect first (read-only):
     contree ls /path
     contree cat /path/file
     contree images --prefix=...
     contree session show

5. Execute in small steps — one mutating step per run:
     contree -S <key> run -- sh -lc 'apt-get update -qq'
     contree -S <key> run -- sh -lc 'apt-get install -y curl'
     contree -S <key> run -- make -C /work test

6. Tag useful results immediately:
     contree -S <key> tag my-env:latest

7. Branch before risky changes:
     contree -S <key> session branch experiment
     contree -S <key> session checkout experiment
     (if it fails: contree -S <key> session checkout main)

Full example — build and tag a Python environment:

  contree -S agent_pyenv use tag:python:3.12
  contree -S agent_pyenv cd /work
  contree -S agent_pyenv run -- pip install uv
  contree -S agent_pyenv run -- uv pip install pytest
  contree -S agent_pyenv run -- python -c 'import pytest; print(pytest.__version__)'
  contree -S agent_pyenv tag python-dev:3.12

More: contree run --help, contree session --help

Sessions
========

Sessions track image, working directory, pending files, and history.
Each non-disposable run creates a new image checkpoint you can
rollback to or branch from.

Session key resolution (priority):
  1. -S / --session flag (most reliable, survives terminal restarts)
  2. CONTREE_SESSION env var (stable within a shell session)
  3. Auto-generated from profile + PID + TTY (changes on terminal close)

Always use -S for agent workflows. Auto-generated keys are
unreliable across terminal restarts.

Starting and resuming:
  contree -S <key> use tag:alpine:latest   start new session
  contree -S <key> use                     show current state
  contree session list                     list all sessions
  contree session list --filter agent      filter by key substring

Branch workflow:
  contree -S <key> session branch experiment
  contree -S <key> session checkout experiment
  contree -S <key> run -- risky-command
  contree -S <key> session checkout main     # abandon if failed
  contree -S <key> session branch -d experiment  # clean up

Rollback:
  contree -S <key> session rollback 1       undo last run
  contree -S <key> session rollback 3       undo last 3 runs
  contree -S <key> session show             view history before rollback

History DAG (contree session show output):
  ID  IMAGE     PARENT  KIND  TITLE              BRANCHES
  1   abc123              use   tag:alpine:latest  main
  2   def456    1       run   apt-get update       main
  3   789abc    2       run   apt-get install curl main, experiment

Sessions are agent memory — reuse the same key to resume later.
Different profiles have separate session databases.

Cleanup:
  contree -S <key> session delete <key> -y   delete session
  contree -S <key> session branch --prune    prune disposable branches

More: contree session --help, contree session branch --help

Images and tags
===============

All data — images, operations, uploaded files — is scoped to a
Project. Multiple tokens can access the same project. Different
projects have separate scopes.

Listing:
  contree images                         all tagged images
  contree images --prefix=python         filter by tag prefix
  contree images -a                      include untagged
  contree images --since 1d              last 24 hours
  contree -f json images                 JSON output for scripting

Tagging:
  contree tag my-app:v1.0                tag current session image
  contree tag UUID my-app:v1.0           tag specific image by UUID
  contree tag tag:alpine:latest my-copy  re-tag by reference
  contree tag -d UUID my-tag             remove tag

Tag rules:
  - Tags are unique per project — assigning moves the tag
  - Allowed: a-z 0-9 _ - with : / . separators (max 256 chars)
  - Your tags shadow public tags with the same name
  - Removing your tag restores the public one

Importing from registries:
  contree images import ubuntu:latest
  contree images import python:3.12-slim node:20-slim
  contree images import --username=user registry.example.com/image:tag
  (password prompted securely; credentials used only for import)

Tag conventions:
  common/<purpose>/<base>:<tag>    shared environments
  <project>/<purpose>/<base>:<tag> project-specific
  my-app:latest                    simple app tags

Always search before building:
  contree images --prefix=python-dev

More: contree images --help, contree tag --help

Files and directories
=====================

contree run is remote execution. Local files are NOT visible in
the sandbox unless explicitly attached.

Single file:
  contree run --file ./app.py:/app/app.py -- python /app/app.py

Directory (recursive):
  contree run --file ./src:/app/src -- make -C /app/src

Full --file syntax:
  host_path[:instance_path][:uUID][:gGID][:mMODE]

  ./app.py                            defaults from stat
  ./app.py:/app/app.py                explicit destination
  ./script.sh:m0755                   override mode only
  ./app.py:/app.py:u0:g0:m0755       all explicit
  ./app.py:uroot:groot               uid/gid by name (local resolve)

Directory exclusions (automatic):
  .*, .git, *.pyc, __pycache__, .venv, .mypy_cache,
  .pytest_cache, node_modules, dist, build

Add custom exclusions:
  contree run --file ./project:/app --file-excludes '*.log' '*.tmp' -- ...

Upload caching:
  Files cached locally by path + inode + mtime + size.
  Cache TTL: 90 days. Server deduplicates by SHA256.
  Unchanged files skip hash calculation and API calls.

Staging files for next run:
  contree file edit /etc/nginx/nginx.conf    download, edit, stage
  contree file cp ./config.yaml /etc/app/    upload and stage

Pending files are injected into the next non-disposable run.
Explicit --file takes priority over pending files at same path.
Pending files are branch-aware.

More: contree run --help, contree file --help

Execution modes
===============

Direct command (default) — each arg is a separate argv entry:
  contree run -- uname -a
  contree run -- make -C /app test
  contree run -- sh -lc 'pip install flask'   (login shell)

Shell mode (-s) — joins args, passes to sh -c:
  contree run -s -- 'echo hello && ls /'
  contree run -s -- 'cd /app && make test'
  contree run -s -- 'cat /etc/passwd | grep root'

When to use which:
  Direct: contree run -- make test         (no shell features needed)
  Shell:  contree run -s -- 'cd /app && make'  (need cd/pipes/&&)
  Login:  contree run -- sh -lc 'cargo build'  (need PATH from profile)

Interpreter mode (-I) — shebang scripts:
  #!/usr/bin/env -S contree run -I
  echo "Hello from sandbox"
  uname -a

  chmod +x script.sh && ./script.sh

Piped stdin:
  echo 'uname -a' | contree run /bin/sh
  cat deploy.sh | contree run /bin/sh

Detached mode (-d):
  contree run -d -- long-running-task
  contree ps                              check status
  contree show UUID                       view result
  contree session wait                    block until done
  contree session wait UUID1 UUID2        wait for specific ops

Disposable mode (-D) — no image checkpoint:
  contree run -D -- rm -rf /tmp/*
  contree run -D -- cat /etc/passwd

Environment variables (-e):
  contree run -e KEY=VALUE -e DEBUG=1 -- ./app

Session-level environment variables:
  contree env PATH=/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/sbin
  contree env DEBUG=1
  contree run -- cargo build         # injects PATH and DEBUG per-run
  contree env -d DEBUG               # unset
  contree env                        # list all

Session env is injected on every run but NOT saved into the image
unless --preserve-env is passed.

Preserve env into the image (persists across runs server-side):
  contree run --preserve-env -e PATH="/root/.cargo/bin:/usr/bin:/bin" -- cargo build
  contree run -- cargo test          # PATH is in the image now

Use for PATH after tool installs (rustup, nvm, pyenv):

  contree run -s -- 'curl -sSf https://sh.rustup.rs | sh -s -- -y'
  contree run --preserve-env -e PATH=/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/sbin -- cargo build
  contree run -- cargo test          # PATH persisted in image

Or with session env:
  contree env PATH=/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/sbin
  contree run --preserve-env -- cargo build   # saves session env into image
  contree run -- cargo test                   # PATH from image

Timeout:
  contree run -t 300 -- slow-build

Working directory override:
  contree run -C /app -- make test

Exit codes propagate from sandbox to CLI process:
  contree run -- /bin/sh -c 'exit 42'; echo $?  # prints 42

More: contree run --help

Command safety
==============

Rules for reliable agent workflows:

1. Read-only first, then mutate. Never blind-write.

2. One mutating step per run. Each run = one history entry.
   Wrong:  contree run -s -- 'apt install curl && make test'
   Right:  contree run -- sh -lc 'apt install -y curl'
           contree run -- make test

3. Why split? Chained runs collapse into one checkpoint.
   If 'make test' fails, you can't rollback to just after
   'apt install'. Split runs give you granular rollback.

4. Global flags (-f, -S, -p) MUST come before the subcommand:
   Right:  contree -S key -f json images
   Wrong:  contree images -S key -f json

5. Use -f json for structured output in automation:
   contree -f json images | jq '.uuid'

6. Agents must never run 'contree auth'. Only users manage auth.

7. Use --disposable (-D) for throwaway checks that should not
   advance the session image. Omit -D to keep snapshots.

8. Prefer absolute paths for -C/--cwd and --file destinations.

9. Branch before risky changes. Rollback is always available but
   branching is cleaner.

Output formats
==============

Global -f flag goes before the subcommand:

  contree -f json images           one JSON object per line (JSONL)
  contree -f json-pretty ps        pretty-printed JSON array
  contree -f csv images            CSV with header row
  contree -f tsv ps                tab-separated values

Scripting examples:
  contree -f json images --prefix=python | jq -r '.uuid'
  contree -f json ps -a | jq 'select(.status=="SUCCESS")'
  contree -f csv images > images.csv
  contree ps -q | xargs -I {} contree show {}

Note: 'run' with default formatter prints raw stdout/stderr.
Use -f json to get structured operation metadata instead.

Profiles
========

Profiles store API tokens for different projects. Each profile
has its own session database — switching profiles isolates sessions.

  contree auth                        save token (secure prompt)
  contree auth ls                     list with status check
  contree auth ls -O                  list without network check
  contree auth switch personal        persistent switch
  contree -p personal images          per-command override
  export CONTREE_PROFILE=personal     env var override
  contree auth remove personal -y     delete profile + sessions

Per-command -p is useful for cross-project operations:
  contree -p project-a images --prefix=base
  contree -p project-b images import tag:base:latest

Data directory: $XDG_CONFIG_HOME/contree/ (or ~/.config/contree/)
  auth.ini                       profile credentials (mode 0600)
  cli.ini                        optional CLI defaults
  cli/sessions/{profile}.db      per-profile sessions, history, cache
  cli/skills.db                  installed agent skill registry
  cli/version_check.json         cached PyPI update-check state

Environment variables:
  CONTREE_HOME       data directory override
  CONTREE_PROFILE    active profile (selects which profile commands use)
  CONTREE_SESSION    explicit session key

Read only by `contree auth` (registration-time fallbacks):
  CONTREE_TOKEN / NEBIUS_API_KEY        token when --token is omitted
  CONTREE_URL                           URL when --url is omitted
  CONTREE_PROJECT / NEBIUS_AI_PROJECT   project ID when --project is omitted

More: contree auth --help

All commands
============

  use [IMAGE]             Set or show session image (aliases: ci)
  run [-- CMD]            Spawn sandbox instance (aliases: r)
  images                  List/import images (aliases: i, img)
  tag [IMAGE] TAG         Tag image (aliases: t)
  ps                      List operations
  kill UUID               Cancel operation
  show UUID               Show operation result
  ls [PATH]               List files in image (no VM)
  cat PATH                Show file content (no VM)
  cp PATH DEST            Download file from image
  cd [PATH]               Change session working directory
  env [KEY=VALUE ...]     Session env vars (-d to unset)
  file edit PATH          Edit remote file via $EDITOR
  file cp SRC DEST        Stage local file for next run
  session list            List sessions (aliases: ls)
  session branch [NAME]   Create/list branches (aliases: br)
  session checkout BRANCH Switch branch (aliases: co)
  session rollback [N]    Undo N steps (aliases: rb)
  session show            Show history DAG
  session delete KEY      Delete session (aliases: rm, del)
  session wait [OPS]      Wait for operations
  auth                    Save token
  auth ls                 List profiles (aliases: profiles)
  auth switch NAME        Switch profile
  auth remove NAME        Delete profile (aliases: rm, del)
  skill install [SPEC]    Install agent skills
  skill upgrade [SPEC]    Upgrade skills
  skill remove SPEC       Remove skills
  skill list              List installed skills (aliases: ls)
  shell                   Interactive REPL (aliases: sh)
  agent                   This manual (aliases: man)

Per-command help: contree <command> --help
Nested help: contree session branch --help
