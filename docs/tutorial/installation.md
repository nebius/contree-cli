# Install & Authenticate

## Requirements

- Python 3.10 or later
- No external dependencies (stdlib only)

## Install

::::{tab-set}
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
`~/.config/contree-cli/config.ini`. If a profile already exists you will be
prompted to confirm; use `-y` to skip the prompt.

Resolution order for each field (first match wins):

1. CLI flag (`--token`, `--project`)
2. Environment variable (`NEBIUS_API_KEY`, `NEBIUS_AI_PROJECT`)
3. Interactive prompt

So if `NEBIUS_API_KEY` and `NEBIUS_AI_PROJECT` are already in your
environment and no flags are passed, `contree auth` picks them up
automatically — no interactive prompts needed:

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

Set `CONTREE_TOKEN` to provide the token without a config file:

```bash
export CONTREE_TOKEN=eyJ...
contree images
```

Environment variables always take precedence over the config file.

### Inline token

Pass `--token` to any command to override both config and env:

```bash
contree --token=eyJ... images
```

---

You're authenticated. Next: {doc}`first-steps`.
