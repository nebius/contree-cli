# operation - Manage operations

Manage operations under a single namespace. Aggregates `ps` (list),
`show` (inspect), and `kill` (cancel), and adds **multi-UUID support** to
`show` and `cancel` so several operations can be acted on in one call.

`op` is the short alias.

## Subcommands

| Subcommand | Aliases | Description |
|------------|---------|-------------|
| `list` | `ls` | List operations. Same flags as `contree ps`. |
| `show UUID [UUID...]` | `sh` | Show one or more operation results. |
| `wait UUID [UUID...]` | `w` | Wait for operations to reach a terminal status (or `--all`). |
| `cancel UUID [UUID...]` | `kill`, `k` | Cancel one or more operations (or `--all`). |

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

The top-level `op` command is a dispatcher: by itself it only prints
usage and routes to the three subcommands described below.

```{terminal-shell} contree op --help
```

## `op list` -- dynamic columns

`contree op list` (alias `op ls`) accepts the same filter flags as
`contree ps` (`-a`, `-S STATUS`, `-K KIND`, `--since`, `--until`,
`-q`/`--quiet`) and shares its rendering pipeline. Reach for it when
you want the operations namespace to feel symmetric with the
multi-UUID `show` and `cancel`; otherwise `contree ps` is just as good.

```{terminal-shell} contree op list --help
```

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

```{terminal-shell} contree op show --help
```

:::{note}
With table output (`-f table`) and several UUIDs, each operation
currently renders as its own mini-table. Use `default` or `json` for a
unified stream view across multiple UUIDs.
:::

## `op wait` -- block until completion

Poll the given operations until each reaches a terminal status
(`SUCCESS`, `FAILED`, `CANCELLED`) and print one row per completion
with the columns `uuid`, `status`, `timed_out`, `duration` (and every
other scalar field the API returns; `error` is pinned to the last
column).

`--all` waits for every currently active operation in the project.
`--timeout SECONDS` (default `60`) caps the wait — when the deadline
hits, the command emits one extra row per unfinished operation with
`timed_out=true` and the operation's last observed status (e.g.
`EXECUTING`), then exits with status `1`. Any operation that finished
non-`SUCCESS` also forces exit code `1`.

:::{warning}
`--all` is **project-scoped**. If multiple agents (or multiple shell
sessions) share the same project, `op wait --all` will block on every
active operation across all of them — not just the ones you launched.
The wait still completes correctly; it just waits for more than you
might expect. For multi-agent setups, prefer the explicit
`op wait UUID1 UUID2 ...` form with the UUIDs you actually own.
:::

```{terminal-shell} contree op wait --help
```

```bash
# Fan-out + join: spawn detached runs, wait for all
A=$(contree run -d -- make a | jq -r .uuid)
B=$(contree run -d -- make b | jq -r .uuid)
C=$(contree run -d -- make c | jq -r .uuid)
contree op wait "$A" "$B" "$C"

# Block until every active op in the project finishes (5 min cap)
contree op wait --all --timeout 300
```

## `op cancel` -- multiple UUIDs or `--all`

Either pass UUIDs explicitly or use `--all` to cancel every active
operation (`PENDING`, `ASSIGNED`, `EXECUTING`). Combining both is allowed:
`--all` wins, and the explicit UUIDs are ignored with a `WARNING`. As
with `op show`, errors on individual UUIDs do not abort the run; the
command exits `1` if any cancellation failed.

```{terminal-shell} contree op cancel --help
```

```bash
# Mixed: --all still wins, "ignored-1" is not cancelled
contree op cancel --all ignored-1
```

## Comparison with the top-level commands

`contree ps` and `contree kill` are top-level **shortcuts** that share
the same argparse setup and handler as `op list` / `op cancel`
respectively — there is no separate implementation. `contree show`
keeps its own single-UUID handler (the multi-UUID `op show` wraps it).

| Need | Use |
|------|-----|
| List active operations | `contree ps` *or* `contree op ls` |
| Inspect one operation | `contree show UUID` *or* `contree op show UUID` |
| Inspect multiple | `contree op show UUID1 UUID2 ...` |
| Block on multiple | `contree op wait UUID1 UUID2 ...` |
| Block on everything active | `contree op wait --all` |
| Cancel one operation | `contree kill UUID` *or* `contree op cancel UUID` |
| Cancel multiple | `contree kill UUID1 UUID2 ...` *or* `contree op cancel UUID1 UUID2 ...` |
| Cancel everything active | `contree kill --all` *or* `contree op cancel --all` |

## See also

- {doc}`ps` -- top-level shortcut for `op list`
- {doc}`show` -- single-UUID inspect (delegated to by `op show`)
- {doc}`kill` -- top-level shortcut for `op cancel`
- {doc}`run` -- the command that creates operations
