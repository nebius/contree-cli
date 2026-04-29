# contree-cli

Command-line client for the [ConTree](https://contree.dev) sandboxing platform.

## What is ConTree?

[ConTree](https://contree.dev) is a secure sandbox API for AI agents with
git-like branching. Every command runs inside a VM-isolated sandbox, and
every execution produces a new **image** -- a full filesystem snapshot.
Branch from any checkpoint, explore paths in parallel, pick the winner,
and instantly roll back on failure.

Built for **AI agents that think ahead**:

- **Tree-search execution** -- branch the sandbox state so an agent can
  explore multiple solution paths in parallel and keep the best one.
- **Instant rollback** -- backtrack to any previous checkpoint when a
  path fails, without rebuilding from scratch.
- **Safe code execution** -- run untrusted or LLM-generated code inside
  VM-level isolation. Crashes and side effects stay in the sandbox.
- **Session continuity** -- rewind and resume long-running agent
  workflows with full filesystem context preserved.

`contree-cli` is the command-line client that talks to the ConTree API.
Install it, authenticate with your project token, and you can create
sandboxes, run commands, inspect filesystems, and manage sessions -- all
from your terminal, shell scripts, or agent toolchains.

```bash
eval $(contree use tag:ubuntu:latest)   # pick a base image
contree run apt update -qq              # each run snapshots the result
contree run apt install -y curl         # builds on the previous snapshot
contree ls /usr/bin/curl                # inspect without spawning a VM
```

## Get started

::::{grid} 1 1 2 2
:gutter: 3

:::{grid-item-card} Tutorial
:link: tutorial/index
:link-type: doc

Step-by-step guide from installation to automated workflows.
Six sections, each building on the previous one.
:::

:::{grid-item-card} Command Reference
:link: commands/index
:link-type: doc

Every command, flag, and subcommand documented with usage examples.
:::

::::

## Key features

::::{grid} 1 1 2 3
:gutter: 3

:::{grid-item-card} Sessions & Branching
:link: tutorial/sessions
:link-type: doc

Every run creates a checkpoint. Branch off to experiment, roll back
mistakes, resume from any point.
:::

:::{grid-item-card} File Injection
:link: tutorial/files
:link-type: doc

Map local files into sandboxes with `--file`, edit remote configs
in-place, stage changes for the next run.
:::

:::{grid-item-card} Scripting-Friendly
:link: tutorial/workflows
:link-type: doc

JSON, CSV, and TSV output. Detached runs, operation monitoring,
shebang scripts — built for automation.
:::

:::{grid-item-card} Zero Dependencies
Zero external packages. Stdlib-only Python, runs anywhere 3.10+ is
available.
:::

:::{grid-item-card} Multi-Profile
:link: tutorial/configuration
:link-type: doc

Named profiles for different projects and environments. Switch with
a single command.
:::

:::{grid-item-card} Filesystem Inspection
:link: commands/ls
:link-type: doc

Browse and download files from sandbox images without spawning a
new instance.
:::

::::

```{toctree}
:maxdepth: 2
:hidden:

tutorial/index
commands/index
```
