# cd

Change the working directory for subsequent commands in the current session.

## Examples

```bash
contree cd /app
contree run -- ls           # runs in /app
contree cd /etc
contree cat os-release      # reads /etc/os-release
contree cd                  # reset to sandbox default
```

## Help output

```{terminal-shell} contree cd --help
```

## Behavior

`cd` stores the path in the session state. Subsequent `run`, `ls`, `cat`,
and `cp` commands resolve relative paths against it.

`cd` without arguments resets to the sandbox's default working directory.

:::{note}
`cd` does not validate that the path exists in the sandbox. Errors
surface only when the next command uses the invalid path.
:::
