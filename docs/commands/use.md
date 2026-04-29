# use

Set the session image or show the current session state.

This is typically the first command you run -- it tells contree-cli which
image to use for subsequent commands.

## Examples

```bash
# Start a session with an image
eval $(contree use tag:ubuntu:latest)

# Start a session with a specific image UUID
eval $(contree use 3f2a7b...)

# Start or resume a named session
export CONTREE_SESSION=my-session

# Show current session info
contree use

# Start a fresh session (new session key)
eval $(contree use -N tag:python:3.11-slim)
```

The `eval` wrapper exports `CONTREE_SESSION` into your shell so all
subsequent commands share the same session. Without `eval`, contree prints
the export line but your shell doesn't pick it up.

## Help output

```{terminal-shell} contree use --help
```

## Behavior

**With an image argument**: resolves the image (UUID or `tag:NAME`), sets it
as the session's current image, and prints a shell export statement.

**Without arguments**: displays the current session info -- session key,
active branch, current image, and last operation.

**With `--new`**: generates a new random session key instead of resuming the
existing one. Useful when you want a clean slate in the same terminal.

## Shell detection

The output format adapts to your shell:

- **bash / zsh**: `export CONTREE_SESSION=<key>`
- **fish**: `set -gx CONTREE_SESSION <key>`

Detection uses the `$SHELL` environment variable.

## See also

- {doc}`/tutorial/first-steps` -- starting your first session
- {doc}`/tutorial/sessions` -- branching and rollback
