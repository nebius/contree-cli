---
icon: leaf
---

# env - Manage session environment variables

Manage session-level environment variables. Variables set with `env` are
applied to every `contree run` automatically. Per-run `-e` flags override
session env vars with the same key.

## Examples

```bash
# Set PATH after installing tools
contree env PATH=/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin:/sbin

# Set multiple variables
contree env DEBUG=1 DB_HOST=localhost

# List current session env
contree env

# Unset variables
contree env -U PATH
contree env -U DEBUG DB_HOST

# Per-run -e overrides session env
contree run -e DEBUG=0 -- ./app
```

## Help output

```{terminal-shell} contree env --help
```

## Behavior

Session env vars are stored in SQLite per session. They persist across
terminal restarts (when using `-S` or `CONTREE_SESSION`).

When `contree run` builds the payload, it merges:
1. Session env vars (base)
2. Per-run `-e` flags (override)

Deleting a session (`session delete`) removes its env vars.

Values with `=` in them work correctly — only the first `=` is the
separator: `contree env CMD=a=b=c` sets `CMD` to `a=b=c`.
