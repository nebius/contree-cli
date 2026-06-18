---
icon: circle-xmark
---

# kill - Cancel operations

:::{note}
**`contree kill` is a top-level shortcut for {doc}`operation cancel <operation>` (`contree op cancel`).**

Both share one argparse setup and one handler. The top-level `kill`
accepts the same positional UUIDs and `--all` flag as `op cancel`,
including multiple UUIDs in a single invocation. See the
{doc}`operation` page for the full description.
:::

Cancel running operations. Only active operations (PENDING, ASSIGNED,
EXECUTING) can be cancelled.

## Examples

```bash
# Cancel a specific operation
contree kill 3f2a7b...

# Cancel multiple operations in one call
contree kill 3f2a7b... a1b2c3... 9d8e7f...

# Cancel all active operations
contree kill --all
```

## Help output

```{terminal-shell} contree kill --help
```

## Behavior

The CLI sends a `DELETE` request to the API for each UUID. The
operation transitions to `CANCELLED` status. If the sandbox is already
running, execution is interrupted.

`--all` finds and cancels every active operation in the project. When
`--all` is combined with explicit UUIDs, `--all` wins and the explicit
UUIDs are ignored with a `WARNING`.

On per-UUID API errors (e.g. 404 for an unknown UUID), the command
logs the failure and continues with the remaining UUIDs, exiting with
status `1` at the end.

## See also

- {doc}`operation` — the canonical command (`contree kill` is its shortcut)
- {doc}`ps` — list operations to find UUIDs
- {doc}`run` — Ctrl-C during `contree run` also cancels the operation
