ConTree CLI User Manual
=======================

Getting started
===============

Install and authenticate:
  pip install contree-cli       or: uv tool install contree-cli
  contree auth                  save API token (secure prompt)

First sandbox:
  eval $(contree use tag:alpine:latest)
  contree run -- uname -a
  contree ls /etc
  contree cat /etc/os-release
  contree shell                 interactive REPL

More: contree --help

Sessions
========

A session tracks your image, working directory, pending files, and
full history with branching. Each non-disposable run creates a new
image checkpoint.

Session key is auto-generated from your terminal. Closing the
terminal creates a new session. To keep a stable session:

  eval $(contree use tag:alpine:latest)    export to env
  contree -S my-project use tag:alpine     explicit name
  export CONTREE_SESSION=my-project        env var

Key commands:
  contree use [IMAGE]             set or show session image
  contree cd /app                 change working directory
  contree session list            list all sessions
  contree session show            view history DAG
  contree session branch <name>   create branch
  contree session checkout <name> switch branch
  contree session rollback [N]    undo N steps
  contree session delete <key>    delete session

Branch workflow:
  contree session branch experiment
  contree session checkout experiment
  contree run -- risky-command
  contree session checkout main          go back if failed
  contree session branch -d experiment   clean up

Rollback:
  contree session rollback 1             undo last run
  contree session show                   inspect before rollback

More: contree session --help

Interactive shell
=================

  contree -S my-project shell     start REPL with explicit session
  contree shell                   start with auto session

Inside the shell:
  - Bare commands run in the sandbox (implicit contree run)
  - ls / cat use the API — no VM spawned
  - vim / nano open contree file edit with your host editor
  - cd changes session working directory
  - Tab completes commands, paths, images, branches
  - Use 'contree run' prefix for flags like -D, -e, --file

More: contree shell --help

Files
=====

Inject local files into the sandbox:
  contree run --file ./app.py:/app/app.py -- python /app/app.py

Inject a directory (recursive):
  contree run --file ./src:/app/src -- make -C /app/src

Full --file syntax:
  host_path[:instance_path][:uUID][:gGID][:mMODE]

  ./app.py                            defaults from stat
  ./app.py:/app/app.py                explicit destination
  ./script.sh:m0755                   override mode
  ./app.py:/app.py:u0:g0:m0755       all explicit

Custom directory exclusions:
  contree run --file ./project:/app --file-excludes '*.log' -- ...

Default exclusions: .*, .git, *.pyc, __pycache__, .venv,
.mypy_cache, .pytest_cache, node_modules, dist, build.

Edit remote files:
  contree file edit /etc/nginx/nginx.conf
  (downloads, opens in $EDITOR, stages changes for next run)

Stage local files:
  contree file cp ./config.yaml /etc/app/config.yaml

Pending files are injected into the next run automatically.
Upload cache: 90 day TTL, server dedup by SHA256.

More: contree run --help, contree file --help

Images and tags
===============

All data is scoped to your Project (API token). Multiple tokens
can access the same project. Different projects are isolated.

  contree images                          list tagged images
  contree images --prefix=python          filter by prefix
  contree images -a                       include untagged
  contree images --since 1d              last 24 hours
  contree tag my-app:v1.0                 tag current session image
  contree tag UUID my-app:v1.0            tag specific image
  contree tag tag:alpine:latest my-copy   re-tag by reference
  contree tag -d UUID my-tag              remove tag

Import from registries:
  contree images import ubuntu:latest
  contree images import --timeout 600 ubuntu:latest
  contree images import --username=user registry.example.com/img:tag
  (credentials used only for import, then discarded)

Tags are unique per project — assigning moves the tag.
Your tags shadow public tags; removing restores the public one.

More: contree images --help, contree tag --help

Execution modes
===============

  Direct:      contree run -- uname -a
  Shell:       contree run -s -- 'echo hello && ls /'
  Shebang:     #!/usr/bin/env -S contree run -I
  Piped:       echo 'cmd' | contree run /bin/sh
  Detached:    contree run -d -- long-task
  Disposable:  contree run -D -- rm -rf /tmp/*
  Env vars:    contree run -e KEY=VALUE -- ./app
  Session env: contree env PATH=/custom/bin:$PATH (injected per-run)
  Preserve:    contree run --preserve-env -- ./app (save env into image)
  Timeout:     contree run -t 300 -- slow-task
  Cwd:         contree run -C /app -- make test

Exit codes propagate: contree run -- sh -c 'exit 42'; echo $?

Detached workflow:
  contree run -d -- long-task
  contree ps                             check status
  contree show UUID                      view result
  contree session wait                   block until done

More: contree run --help

Output formats
==============

Global -f flag goes before the subcommand:

  contree -f json images           one JSON object per line
  contree -f json-pretty ps        pretty JSON array
  contree -f csv images            CSV with header
  contree -f tsv ps                tab-separated

Scripting:
  contree -f json images | jq -r '.uuid'
  contree ps -q | xargs -I {} contree show {}

More: contree --help

Operations
==========

  contree ps                 list active operations
  contree ps -a              list all (including completed)
  contree ps -q              UUIDs only (for scripting)
  contree show UUID          show operation result
  contree kill UUID          cancel operation
  contree kill -a            cancel all active
  contree session wait       wait for active operations

More: contree ps --help, contree show --help

Profiles
========

  contree auth                        save token (secure prompt)
  contree auth ls                     list profiles with status
  contree auth ls -O                  skip network check
  contree auth switch personal        persistent switch
  contree -p personal images          per-command override
  export CONTREE_PROFILE=personal     env var override
  contree auth remove personal -y     delete profile

Each profile has its own session database.

Auth fallback: if `NEBIUS_API_KEY` and `NEBIUS_AI_PROJECT` are set
in the environment and no `--token`/`--project` flags are passed,
`contree auth` picks them up automatically (no interactive prompts).

More: contree auth --help

Configuration
=============

Data directory: ~/.config/contree-cli/ (override: $CONTREE_HOME)

  config.ini              profile credentials
  sessions-{profile}.db   per-profile sessions
  skills.db               agent skill registry

Environment variables:
  CONTREE_HOME       data directory
  CONTREE_TOKEN      API token (overrides config)
  CONTREE_URL        API URL (overrides config)
  CONTREE_PROJECT    project ID (overrides config)
  CONTREE_PROFILE    active profile
  CONTREE_SESSION    explicit session key

More: contree --help

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
  agent                   Manual (aliases: man)

Per-command help: contree <command> --help
