---
icon: folder
---

# cd - Change session working directory

Change the working directory for subsequent commands in the current session.

## Examples

```bash
contree cd /app
contree run -- ls           # runs in /app
contree cd /etc
contree cat os-release      # reads /etc/os-release
contree cd                  # print current working directory
```

## Help output

```{terminal-shell} contree cd --help
```

## Behavior

`cd` stores the path in the session state. Subsequent `run`, `ls`, `cat`,
and `cp` commands resolve relative paths against it.

`cd` without arguments prints the current working directory.

:::{note}
`cd` validates the target against the image filesystem via the
inspect API and reports an error when the directory does not exist.
:::
