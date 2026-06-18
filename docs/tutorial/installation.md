---
icon: download
---

# Install & Authenticate

## Requirements

- Python 3.10 or later
- No external dependencies (stdlib only)

## Install

::::{tab-set}
:class: no-sync

:::{tab-item} uv (recommended)
```bash
uv tool install contree-cli
```
:::

:::{tab-item} pip
```bash
pip install contree-cli
```
:::

:::{tab-item} pipx
```bash
pipx install contree-cli
```
:::

:::{tab-item} From source
```bash
git clone https://github.com/nebius/contree-cli.git
cd contree-cli
uv sync
```
:::
::::

Verify the installation:

```bash
contree --help
```

### Development setup

To work on contree-cli itself:

```bash
git clone https://github.com/nebius/contree-cli.git
cd contree-cli
uv sync --group dev
make check    # lint + type check
make tests    # lint + type check + pytest
```

## Authenticate

All ConTree API calls require a bearer token and a project ID.

### Save credentials

Get an API token and project ID from your ConTree project, then save them:

```bash
contree auth
```

You will be prompted to enter:

1. **Token** — entered securely (no echo)
2. **Project ID** — your project identifier

The CLI verifies the token with the API and writes credentials to
`~/.config/contree/auth.ini`. If a profile already exists you will be
prompted to confirm; use `-y` to skip the prompt.

Resolution order for each field during `contree auth` (first match wins):

1. CLI flag (`--token`, `--url`, `--project`)
2. Environment variables, in order:
   - token: `CONTREE_TOKEN`, then `NEBIUS_API_KEY`
   - URL: `CONTREE_URL`
   - project: `CONTREE_PROJECT`, then `NEBIUS_AI_PROJECT`
3. Interactive prompt

So if these variables are already in your environment and no flags
are passed, `contree auth` picks them up automatically, no interactive
prompts needed:

```bash
export NEBIUS_API_KEY=eyJ...
export NEBIUS_AI_PROJECT=your-project-id
contree auth -y      # fully non-interactive
```

:::{warning}
Avoid `contree auth --token=eyJ...` — the token is visible in process
listings and shell history. Omit `--token` to use the secure prompt.
:::

### Named profiles

Store multiple tokens for different projects or environments:

```bash
contree auth --profile=personal
contree auth --profile=sandbox
```

List all profiles:

```bash
contree auth ls
```

Switch the active profile permanently:

```bash
contree auth switch personal
```

Or use a profile temporarily (single session, no config change):

```bash
export CONTREE_PROFILE=personal
contree images    # uses personal
```

### Token from environment

`CONTREE_TOKEN` and `NEBIUS_API_KEY` are read **only** by `contree auth`
during profile registration; runtime commands always read credentials
from the saved profile. To bootstrap a profile entirely from environment
variables, run `auth` non-interactively:

```bash
export CONTREE_TOKEN=eyJ...
export CONTREE_URL=https://api.tokenfactory.nebius.com/sandboxes
contree auth -y --type jwt          # one-shot setup, no prompts
contree images
```

### Inline token

Pass `--token` to any command to override the saved profile for a single
invocation:

```bash
contree --token=eyJ... images
```

---

You're authenticated. Next: {doc}`first-steps`.
