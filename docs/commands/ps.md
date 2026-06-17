---
icon: list
---

# ps - List activity

:::{note}
**`contree ps` is a top-level shortcut for {doc}`operation list <operation>` (`contree op ls`).**

Both share one argparse setup and one handler — `ps` exists for the
Docker-like UX. New flags or columns added to `operation list` apply
automatically here. See the {doc}`operation` page for the full
description of dynamic columns, error handling, and multi-UUID
workflows in the operation namespace.
:::

List operations and their statuses. By default shows only active operations
(PENDING, ASSIGNED, EXECUTING).

## Examples

```bash
# Show active operations
contree ps

# Show all operations (including completed)
contree ps -a

# UUIDs only (for scripting)
contree ps -q

# Filter by status (note: --status, not -S; -S is the global session flag)
contree ps --status FAILED

# Filter by kind
contree ps -K instance

# Operations from the last hour
contree ps -a --since=1h

# Pipe to other commands
contree ps -q | xargs -I {} contree show {}
```

## Help output

```{terminal-shell} contree ps --help
```

## Operation statuses

| Status | Meaning |
|--------|---------|
| `PENDING` | Queued, waiting for resources |
| `ASSIGNED` | Assigned to a worker |
| `EXECUTING` | Running |
| `SUCCESS` | Completed successfully |
| `FAILED` | Completed with an error |
| `CANCELLED` | Cancelled by the user |

Without `-a`, only `PENDING`, `ASSIGNED`, and `EXECUTING` are shown.

## Dynamic output columns

`ps` renders every scalar top-level field the API returns (not a fixed
subset), so new server fields appear automatically. `error` is pinned
to the last column. See {doc}`operation` for the full description.

## See also

- {doc}`operation` — the canonical command (`contree ps` is its shortcut)
- {doc}`show` — inspect a specific operation
- {doc}`kill` — cancel a running operation
- {doc}`/tutorial/workflows` — monitoring and scripting patterns
