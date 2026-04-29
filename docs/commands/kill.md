# kill

Cancel a running operation. Only active operations (PENDING, ASSIGNED,
EXECUTING) can be cancelled.

## Examples

```bash
# Cancel a specific operation
contree kill 3f2a7b...

# Cancel all active operations
contree kill --all
```

## Help output

```{terminal-shell} contree kill --help
```

## Behavior

The CLI sends a `DELETE` request to the API. The operation transitions to
`CANCELLED` status. If the sandbox is already running, execution is
interrupted.

`--all` finds and cancels every active operation in the project.

## See also

- {doc}`ps` -- list operations to find UUIDs
- {doc}`run` -- Ctrl-C during `contree run` also cancels the operation
