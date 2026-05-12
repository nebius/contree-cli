# operation (op)

Manage operations under a single namespace. Aggregates `ps` (list),
`show` (inspect), and `kill` (cancel), and adds **multi-UUID support** to
`show` and `cancel` so several operations can be acted on in one call.

`op` is the short alias.

## Subcommands

| Subcommand | Aliases | Description |
|------------|---------|-------------|
| `list` | `ls` | List operations. Same flags as `contree ps`. |
| `show UUID [UUID...]` | -- | Show one or more operation results. |
| `cancel UUID [UUID...]` | -- | Cancel one or more operations (or `--all`). |

## Examples

```bash
# List active operations (same as `contree ps`)
contree op list
contree op ls
contree op ls -a -S FAILED       # all flags from ps are accepted

# Inspect a single operation
contree op show 3f2a7b...

# Inspect several operations at once
contree op show 3f2a7b... a1b2c3... 9d8e7f...

# History references (inherited from `contree show`)
contree op show @5 @4 @3

# Cancel one or more operations
contree op cancel 3f2a7b...
contree op cancel a1b2c3... 9d8e7f...

# Cancel every active operation
contree op cancel --all
```

## Help output

```{terminal-shell} contree op --help
```

```{terminal-shell} contree op list --help
```

```{terminal-shell} contree op show --help
```

```{terminal-shell} contree op cancel --help
```

## `op list` -- dynamic columns

The listing renders **every scalar top-level field** the API returns,
not a hard-coded subset. When the server adds a new field (for example
`cost`, `project_id`, `started_at`), it appears in the output without a
CLI release. Nested structures (`metadata`, `result`, `tags`) are
filtered out -- use `op show UUID` for the detail view.

Known fields are lightly typed:

| Field | Transform |
|-------|-----------|
| `created_at`, `started_at`, `finished_at`, `updated_at` | parsed to UTC datetime |
| `duration` | wrapped as `timedelta` (`total_seconds()` in JSON) |
| `error` | `None` is rendered as empty string |

Column order follows the API response, with one exception: **`error`
is pinned to the last column**. Long free-form error messages would
otherwise push the rest of the row out of alignment.

## `op show` -- multiple UUIDs

Each UUID is fetched and rendered through the same code path as
`contree show`, so cached terminal results and `@N` history references
work uniformly. On API errors (e.g. 404 for an unknown UUID), the
command logs the failure and continues with the remaining UUIDs, exiting
with status `1` at the end.

:::{note}
With table output (`-f table`) and several UUIDs, each operation
currently renders as its own mini-table. Use `default` or `json` for a
unified stream view across multiple UUIDs.
:::

## `op cancel` -- multiple UUIDs or `--all`

Either pass UUIDs explicitly or use `--all` to cancel every active
operation (`PENDING`, `ASSIGNED`, `EXECUTING`). Combining both is allowed:
`--all` wins, and the explicit UUIDs are ignored with a `WARNING`. As
with `op show`, errors on individual UUIDs do not abort the run; the
command exits `1` if any cancellation failed.

```bash
# Mixed: --all still wins, "ignored-1" is not cancelled
contree op cancel --all ignored-1
```

## Comparison with the top-level commands

`contree op` does not replace `ps`/`show`/`kill` -- those keep their
single-target semantics. The new namespace exists for grouping and for
multi-UUID workflows:

| Need | Use |
|------|-----|
| List active operations | `contree ps` *or* `contree op ls` |
| Inspect one operation | `contree show UUID` *or* `contree op show UUID` |
| Inspect multiple | `contree op show UUID1 UUID2 ...` |
| Cancel one operation | `contree kill UUID` *or* `contree op cancel UUID` |
| Cancel multiple | `contree op cancel UUID1 UUID2 ...` |
| Cancel everything active | `contree kill --all` *or* `contree op cancel --all` |

## See also

- {doc}`ps` -- single-purpose list command (delegated to by `op list`)
- {doc}`show` -- single-UUID inspect (delegated to by `op show`)
- {doc}`kill` -- single-UUID cancel
- {doc}`run` -- the command that creates operations
