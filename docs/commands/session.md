# session

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

## `session delete`

Remove sessions and all their data (history, branches, files, shell history).

```bash
contree session delete KEY [KEY ...]
contree session rm KEY -y     # skip confirmation
contree session del KEY
```

## See also

- {doc}`/tutorial/sessions` -- full tutorial on sessions, branches, and rollback
- {doc}`use` -- start or resume a session
