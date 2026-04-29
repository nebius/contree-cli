# Configuration & Profiles

Profiles let you store credentials for multiple projects or environments
and switch between them. This section walks through setting up profiles,
switching contexts, and understanding how configuration is resolved.

## Creating profiles

Each profile stores a token and API URL for a specific project or
environment. When you first ran `contree auth`, it created a profile
called `default`. Add more with `--profile`:

```bash
contree auth --profile=personal
contree auth --profile=sandbox
```

Each command prompts for a token securely (no echo), verifies it against
the API, and writes it to `~/.config/contree-cli/config.ini`.

The resulting config file looks like this:

```ini
[DEFAULT]
profile = default

[profile:default]
token = eyJ...
url = https://api.studio.nebius.com/sandboxes
type = iam
project = project-id-default

[profile:personal]
token = eyJ...
url = https://api.studio.nebius.com/sandboxes
type = iam
project = project-id-personal

[profile:sandbox]
token = eyJ...
url = https://api.studio.nebius.com/sandboxes
type = iam
project = project-id-sandbox
```

Each profile is a `[profile:NAME]` section with `token`, `url`, `type`,
and `project` keys. The `[DEFAULT]` section stores the name of the active
profile.

## Listing profiles

See all saved profiles and which one is active:

```bash
contree auth ls
```

The output shows the profile name, URL, a token hash (first 16 chars of
SHA256), active status, and a health check result.

`auth ls` verifies each profile against the API with a 2-second timeout.
Possible status values:

- `ok` — token is valid
- `timeout` — server did not respond in time
- `error` — bad token or network error
- `offline mode` — you passed `-O` / `--offline`

Skip the network check:

```bash
contree auth ls -O
```

For automation, use structured output:

```bash
contree -f json auth ls
```

## Switching profiles

### Persistent switch

Change the active profile for all future commands:

```bash
contree auth switch personal
```

### Per-command override

Use `-p` / `--profile` on any command:

```bash
contree -p personal images
contree -p sandbox run -- uname -a
```

### Environment variable

Override for the entire shell session:

```bash
export CONTREE_PROFILE=sandbox
contree images        # uses sandbox
```

### Inline token

Pass `--token` and `--url` directly:

```bash
contree --token=eyJ... --url=https://api.studio.nebius.com/sandboxes images
```

:::{warning}
Avoid `--token` on the command line in production — the token is
visible in process listings and shell history.
:::

## Removing profiles

Delete a profile and its session database:

```bash
contree auth remove personal
contree auth rm personal -y    # skip confirmation
```

If the removed profile was active, the CLI switches to the first
remaining profile.

## Profiles and sessions

Each profile has its own session database
(`~/.config/contree-cli/sessions-{profile}.db`), so:

- **Same profile, same terminal** — resumes the existing session
- **Different profile, same terminal** — different session, different data

Switching from `default` to `personal` does not affect your `default`
sessions — you can switch back and continue where you left off.

:::{tip}
To share a session across profiles (rare), set `CONTREE_SESSION`:

```bash
export CONTREE_SESSION=shared-session
```
:::

## Data storage

All data lives in `CONTREE_HOME` (default `~/.config/contree-cli`):

| File | Purpose |
|------|---------|
| `config.ini` | Profile credentials and settings |
| `sessions-{profile}.db` | Per-profile sessions, history, branches, cache |
| `skills.db` | Installed agent skill registry |

Override with `$CONTREE_HOME`:

```bash
export CONTREE_HOME=/custom/path
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `CONTREE_HOME` | Data directory (default `~/.config/contree-cli`) |
| `CONTREE_TOKEN` | API bearer token (overrides config) |
| `CONTREE_URL` | API base URL (overrides config) |
| `CONTREE_PROJECT` | Project ID (overrides config) |
| `CONTREE_PROFILE` | Active profile name (overrides config) |
| `CONTREE_SESSION` | Explicit session key (overrides auto-generated) |

## Resolution precedence

For token, URL, and project:

1. CLI flag (`--token`, `--url`, `--project`)
2. Environment variable (`CONTREE_TOKEN`, `CONTREE_URL`, `CONTREE_PROJECT`)
3. Config file value from the active profile
4. Built-in default URL: `https://api.studio.nebius.com/sandboxes`

For profiles:

1. `-p` / `--profile` flag
2. `CONTREE_PROFILE` environment variable
3. `profile` key in config `[DEFAULT]` section
4. Falls back to `default`

---

See {doc}`/commands/auth` for the full auth command reference, or
{doc}`/commands/index` for all commands.
