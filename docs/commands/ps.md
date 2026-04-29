# ps

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

# Filter by status
contree ps -S FAILED

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

## See also

- {doc}`show` -- inspect a specific operation
- {doc}`kill` -- cancel a running operation
- {doc}`/tutorial/workflows` -- monitoring and scripting patterns
