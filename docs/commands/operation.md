---
icon: spinner
---

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
contree op ls -a --status FAILED # all flags from ps are accepted

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
`contree ps` (`-a`, `--status STATUS`, `-K KIND`, `--since`,
`--until`, `-q`/`--quiet`) and shares its rendering pipeline. Reach
for it when you want the operations namespace to feel symmetric with
the multi-UUID `show` and `cancel`; otherwise `contree ps` is just
as good. `-S` is the global session flag and only works BEFORE the
subcommand.

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
`contree show`, so cached terminal results and history references work
uniformly. Accepted reference forms (mirroring `session rollback`
syntax with a git-style alias):

- `@`, `:`, or `HEAD` -- the operation at the active branch tip.
- `@N` (or `:N`, bare `N`) -- absolute history id.
- `@-N`, `:-N`, or `HEAD~N` -- walk N steps back from the tip.
- `HEAD~` -- shorthand for `HEAD~1`.
- `@+N` (or `:+N`) -- walk N steps forward from the tip, picking the
  latest child at each branch point.

On API errors (e.g. 404 for an unknown UUID), the command logs the
failure and continues with the remaining UUIDs, exiting with status
`1` at the end.

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
with the columns `uuid`, `status`, `exit_code`, `timed_out`,
`duration` (and every other scalar field the API returns; `error` is
pinned to the last column).

`--all` waits for every currently active operation in the project.
`--timeout SECONDS` (default `60`) caps the wait — when the deadline
hits, the command emits one extra row per unfinished operation with
`timed_out=true` and the operation's last observed status (e.g.
`EXECUTING`), then exits with status `1`.

`status` is the server's word: it reflects orchestration (did the
API run the job?), not what the sandbox process did with its exit
code. The exit code is a separate column. The CLI's own exit status
is `1` whenever any operation finished non-`SUCCESS`, or the actual
`exit_code` when a `SUCCESS` op exited non-zero — so
`op wait UUID && next-step` composes correctly with sandbox commands
like `run -- false`.

:::{important}
`op wait` is a **pure observer**: it polls operation status and
prints rows, but it **never updates session state**. In particular,
the `detached-<op-uuid>` branch created when you ran
`contree run -d` keeps pointing at the **starting** image — `op
wait` does not advance it to the result image. The pattern therefore
fits non-image-producing runs (`--disposable`) most cleanly; for
non-disposable fan-out, the result image of each leg lives only on
the server and you must recover it explicitly (see the non-disposable
example below).
:::

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

Preferred — `--disposable` fan-out, no image to track. Note the
global `-f json` before `run` so `jq` sees JSON; the default
formatter is plain.

```bash
A=$(contree -f json run -d --disposable -- pytest tests/a | jq -r .uuid)
B=$(contree -f json run -d --disposable -- pytest tests/b | jq -r .uuid)
C=$(contree -f json run -d --disposable -- pytest tests/c | jq -r .uuid)
contree op wait "$A" "$B" "$C"
contree op show "$A" "$B" "$C"          # stdout/stderr per leg
```

Non-disposable fan-out — must recover the chosen leg's image yourself:

```bash
A=$(contree -f json run -d -- apt-get install -y curl | jq -r .uuid)
B=$(contree -f json run -d -- apt-get install -y wget | jq -r .uuid)
contree op wait "$A" "$B"

# Pull the result image out and bind it back into the session,
# or tag it for later reuse.
IMG_A=$(contree -f json op show "$A" | jq -r .image)
contree use "$IMG_A"
contree tag "$IMG_A" feature/curl-tools
```

Block on the whole project (5 min cap):

```bash
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
