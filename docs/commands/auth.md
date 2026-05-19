# auth - Configure credentials and profiles

Configure authentication tokens and manage profiles. Each profile stores
credentials for a different project or environment.

## Examples

```bash
# Save a token (secure prompt, no echo)
contree auth

# Save to a named profile
contree auth --profile=personal

# Force overwrite existing profile
contree auth -y

# List all profiles and probe whether they work
contree auth ls

# Structured output for scripts and agents
contree -f json auth ls

# List profiles without network probes
contree auth ls --offline

# Switch active profile
contree auth switch personal

# Remove a profile
contree auth remove personal
contree auth rm personal -y
```

## Help output

```{terminal-shell} contree auth --help
```

## Behavior

When you run `contree auth`, the CLI:

1. Prompts for the **token** securely via `getpass` (no echo, not stored in
   shell history)
2. Prompts for the **project ID**
3. Verifies the token with the API (`GET /v1/whoami`)
4. Writes credentials to `~/.config/contree/auth.ini`
5. If the profile already exists, prompts for confirmation (use `-y`
   to skip)

### Flags

- `--token` — API token (prompted securely if omitted)
- `--url` — API base URL (default: `https://api.tokenfactory.nebius.com/sandboxes`)
- `--project` — Project ID (prompted if omitted)
- `--profile` — Profile name (default: `default`)
- `-y` / `--force` — Overwrite existing profile without confirmation

### Environment variable shortcuts

When CLI flags (`--token`, `--url`, `--project`) are not passed,
`contree auth` checks these environment variables before falling back
to an interactive prompt:

| Variable | Fallback for | Priority |
|----------|-------------|----------|
| `CONTREE_TOKEN` | `--token` | flag > `CONTREE_TOKEN` > `NEBIUS_API_KEY` > prompt |
| `NEBIUS_API_KEY` | `--token` | (see above) |
| `CONTREE_URL` | `--url` | flag > env > type-specific default > prompt |
| `CONTREE_PROJECT` | `--project` | flag > `CONTREE_PROJECT` > `NEBIUS_AI_PROJECT` > prompt |
| `NEBIUS_AI_PROJECT` | `--project` | (see above) |

These variables are read **only** during `contree auth`. Other commands
ignore them and read credentials strictly from the saved profile.

If the relevant variables are set, `contree auth` runs fully
non-interactively (no prompts):

```bash
export NEBIUS_API_KEY=eyJ...
export NEBIUS_AI_PROJECT=your-project-id
contree auth -y      # no prompts, saves immediately
```

## `auth list`

`contree auth list` (alias `auth ls`, `profiles`) prints every saved
profile from `auth.ini` and verifies each one against the API with a
2-second timeout, adding a `status` column that tells you at a glance
which profiles are usable. Pass `--offline` to suppress the probe
entirely when you only want to inspect what is saved locally.

```{terminal-shell} contree auth list --help
```

Possible `status` values:

- `ok` -- probe succeeded and the token has the `list` permission
- `inactive` -- probe succeeded but the token lacks the `list`
  permission, meaning sandboxes are disabled on this project
- `timeout` -- probe did not complete within 2 seconds
- `error` -- probe failed for another reason, such as a bad token or
  another network/API error
- `offline` -- you passed `--offline`, so no probe was attempted

For automation and agents, prefer:

```bash
contree -f json auth ls
contree -f json auth ls --offline
```

## `auth switch`

`contree auth switch NAME` rewrites the `active` pointer in `auth.ini`
so subsequent commands resolve credentials from that profile. The
profile must already exist (created by `contree auth --profile=NAME`).
Switching does not touch token data, so it is safe to run as often as
you like to bounce between projects.

```{terminal-shell} contree auth switch --help
```

## `auth remove`

`contree auth remove NAME` (aliases `rm`, `del`) deletes the profile
from `auth.ini` and removes its per-profile session database
(`sessions-NAME.db`). If the deleted profile was the active one, the
CLI promotes the first remaining profile to active (or falls back to
`default` if none remain). Confirmation is required unless `-y` is
passed.

```{terminal-shell} contree auth remove --help
```

```bash
contree auth remove personal
contree auth rm personal         # alias
contree auth del personal -y     # skip confirmation
```

:::{warning}
Avoid `--token=eyJ...` on the command line — the token is visible in
process listings (`ps`) and shell history. Omit `--token` to use the
secure prompt instead.
:::

## Alternative authentication

Runtime commands always read credentials from the saved profile.
To authenticate without an interactive `auth` flow, either:

```bash
# 1. Bootstrap the profile non-interactively from environment vars
export CONTREE_TOKEN=eyJ...
export CONTREE_URL=https://api.tokenfactory.nebius.com/sandboxes
contree auth -y --type jwt
contree images

# 2. Or pass the token inline per-command (visible in process listings)
contree --token=eyJ... images
```

Setting `CONTREE_TOKEN` alone (without first running `contree auth`)
will not authenticate runtime commands.

## See also

- {doc}`/tutorial/installation` -- full authentication guide
- {doc}`/tutorial/configuration` -- config file format and precedence
