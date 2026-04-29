# Tutorial

Learn contree-cli by building a real workflow — from zero to automated
scripting in six short sections.

## What you'll build

By the end of this tutorial you will:

- Spin up sandboxes from images and run arbitrary commands
- Track sandbox state through sessions with branching and rollback
- Inject local files, edit remote configs, and tag working images
- Script everything with JSON output, detached runs, and operation monitoring

## Before you start

You need two things:

- **Python 3.10+** installed on your machine
- **A ConTree API token** — get one from your [project dashboard](https://contree.dev)

## The path

::::{grid} 1 1 2 3
:gutter: 3

:::{grid-item-card} 1. Install & Authenticate
:link: installation
:link-type: doc

Install contree-cli, save your API token, and set up named profiles for
different environments.
:::

:::{grid-item-card} 2. Your First Sandbox
:link: first-steps
:link-type: doc

Browse images, run commands, inspect the filesystem, and download files.
Understand how each run creates a new checkpoint.
:::

:::{grid-item-card} 3. Interactive Shell
:link: shell
:link-type: doc

Use the REPL for rapid iteration: tab completion for paths, images, and
branches, command aliases, and persistent history.
:::

:::{grid-item-card} 4. Sessions & Branches
:link: sessions
:link-type: doc

Branch off to experiment, roll back mistakes, share sessions across
terminals, and start fresh when needed.
:::

:::{grid-item-card} 5. Working with Files
:link: files
:link-type: doc

Inject local code into sandboxes, edit remote files in-place, and stage
changes that auto-attach on the next run.
:::

:::{grid-item-card} 6. Images & Tags
:link: images
:link-type: doc

Tag working images for reuse, import from registries, and search by
prefix. Build reusable base environments.
:::

:::{grid-item-card} 7. Scripting & Automation
:link: workflows
:link-type: doc

Shell mode, shebang scripts, detached runs, operation monitoring, and
machine-readable output formats for pipelines.
:::

:::{grid-item-card} 8. Configuration & Profiles
:link: configuration
:link-type: doc

Create and switch between profiles for different projects, understand
how profiles affect sessions, and configure environment variables.
:::

::::

## Quick taste

If you just want to see contree-cli in action before diving in:

::::{tab-set}
:::{tab-item} CLI
```bash
# install
git clone https://github.com/nebius/contree-cli.git
cd contree-cli && uv sync

# authenticate (token prompted securely)
contree auth

# start a session and run a command
eval $(contree use tag:ubuntu:latest)
contree run uname -a

# inspect the result
contree ls /
contree cat /etc/os-release
```
:::

:::{tab-item} Shell
```bash
# install and authenticate first (see CLI tab)
contree auth

# start the interactive shell
contree shell
```

Once inside the shell:

```text
contree use tag:ubuntu:latest
uname -a
ls /
cat /etc/os-release
```
:::
::::

Ready? Start with {doc}`installation`.

```{toctree}
:maxdepth: 1
:hidden:

installation
first-steps
shell
sessions
files
images
workflows
configuration
```
