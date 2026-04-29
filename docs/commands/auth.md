# auth

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
4. Writes credentials to `~/.config/contree-cli/config.ini`
5. If the profile already exists, prompts for confirmation (use `-y`
   to skip)

### Flags

- `--token` — API token (prompted securely if omitted)
- `--url` — API base URL (default: `https://api.studio.nebius.com/sandboxes`)
- `--project` — Project ID (prompted if omitted)
- `--profile` — Profile name (default: `default`)
- `-y` / `--force` — Overwrite existing profile without confirmation

### Environment variable shortcuts

When CLI flags (`--token`, `--project`) are not passed, `contree auth`
checks these environment variables before falling back to an interactive
prompt:

| Variable | Fallback for | Priority |
|----------|-------------|----------|
| `NEBIUS_API_KEY` | `--token` | flag > env > prompt |
| `NEBIUS_AI_PROJECT` | `--project` | flag > env > prompt |

If both variables are set, `contree auth` runs fully non-interactively
(no prompts):

```bash
export NEBIUS_API_KEY=eyJ...
export NEBIUS_AI_PROJECT=your-project-id
contree auth -y      # no prompts, saves immediately
```

## `auth ls` status column

`contree auth ls` verifies each saved profile against the API
with a 2-second timeout and adds a `status` column.

Possible values:

- `ok` -- probe succeeded
- `timeout` -- probe did not complete within 2 seconds
- `error` -- probe failed for another reason, such as a bad token or
  another network/API error
- `offline` -- you passed `--offline`, so no probe was attempted

Use `contree auth ls --offline` when you want to inspect saved
profiles without any network traffic.

For automation and agents, prefer:

```bash
contree -f json auth ls
contree -f json auth ls --offline
```

## `auth remove`

Delete a saved profile from the config file.

```bash
contree auth remove personal
contree auth rm personal         # alias
contree auth del personal -y     # skip confirmation
```

If the removed profile was active, the CLI switches to the first
remaining profile (or `default` if none remain).

:::{warning}
Avoid `--token=eyJ...` on the command line — the token is visible in
process listings (`ps`) and shell history. Omit `--token` to use the
secure prompt instead.
:::

## Alternative authentication

You can also authenticate without the config file:

```bash
# Environment variable (avoid in shared environments)
export CONTREE_TOKEN=eyJ...

# Inline flag (per-command, visible in process listings)
contree --token=eyJ... images
```

## See also

- {doc}`/tutorial/installation` -- full authentication guide
- {doc}`/tutorial/configuration` -- config file format and precedence
