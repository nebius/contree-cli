# session - Manage sessions, branches, history

Manage session branches and history. Sessions track the image state as you
run commands, with support for branching and rollback.

## Examples

```bash
# Show current session
contree session

# List all sessions
contree session list

# Show full history
contree session show

# Create and switch to a branch
contree session branch experiment
contree session checkout experiment

# Switch back
contree session checkout main

# Create a branch from another branch
contree session branch hotfix --from main

# List branches (* marks active)
contree session branch

# Undo last operation
contree session rollback

# Undo last 3 operations
contree session rollback 3

# Import image from another session
contree session use other-session

# Delete a session
contree session delete my-old-session
contree session rm my-old-session -y
```

## Help output

```{terminal-shell} contree session --help
```

## Concepts

Each non-disposable `contree run` creates a new history entry and advances
the branch pointer. Branches share the underlying history -- creating a
branch just adds a new pointer at the current position.

Rollback moves the branch pointer backwards. History entries are preserved
and can be recovered by creating a new branch.

## Subcommands

### `session list`

`contree session list` (alias `ls`) prints every session known to the
current profile, with the active session marked. The optional
`--filter` flag narrows the list by substring match against the session
key, which is handy when you keep many disposable sessions named after
features or tickets.

```{terminal-shell} contree session list --help
```

### `session use`

`contree session use KEY` imports the **current image** of another
session into the active session as a new history entry. The source
session is not modified; this is a "fork the snapshot, keep working
here" operation, distinct from the top-level `contree use` which starts
or resumes a session against an image reference.

```{terminal-shell} contree session use --help
```

### `session branch`

`contree session branch` (alias `br`) lists branches with `*` marking
the active one. Pass a name to create a new branch pointing at the
current history position, or combine with `--from BRANCH` to fork off a
different branch. The `-U`/`--prune` flag removes branches that no
longer reference live history.

```{terminal-shell} contree session branch --help
```

### `session checkout`

`contree session checkout BRANCH` (alias `co`) switches the active
branch pointer. Working directory, pending files, and the current
image are all reset to whatever the target branch currently points at,
so it is the safe way to bounce between parallel experiments.

```{terminal-shell} contree session checkout --help
```

### `session rollback`

`contree session rollback [TARGET]` (alias `rb`) navigates the history
of the current branch. With no argument it steps back one entry; a
positive number jumps to that absolute history index, `-N` steps back
`N` entries, and `+N` steps forward. History entries are preserved --
rollback only moves the branch pointer.

```{terminal-shell} contree session rollback --help
```

### `session show`

`contree session show` prints the session history DAG with one row per
entry, including operation IDs, image UUIDs, branch pointers, and
relative timestamps. Use `-a` to include hidden entries, `-k KIND` to
filter by entry kind (e.g. `run`, `cd`), and `-l LAST` to show only the
last N rows.

```{terminal-shell} contree session show --help
```

### `session wait`

`contree session wait [OP_ID ...]` blocks until the specified operations
reach a terminal state (`SUCCESS`, `FAILED`, or `CANCELLED`). When no
IDs are given it waits for every active operation in the session, which
is the canonical way to drain background `contree run -d` jobs before
moving on.

```{terminal-shell} contree session wait --help
```

### `session delete`

`contree session delete KEY [KEY ...]` (aliases `rm`, `del`) removes
sessions and all their data -- history, branches, pending files, shell
history. The command prompts before deleting unless `-y` is passed.
Use this to garbage-collect throwaway sessions; the disk savings on
the SQLite database can be substantial when many short-lived sessions
accumulate.

```{terminal-shell} contree session delete --help
```

```bash
contree session delete KEY [KEY ...]
contree session rm KEY -y     # skip confirmation
contree session del KEY
```

## See also

- {doc}`/tutorial/sessions` -- full tutorial on sessions, branches, and rollback
- {doc}`use` -- start or resume a session
