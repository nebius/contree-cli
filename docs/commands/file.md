# file

Stage file changes for the next `contree run`. Pending files are
automatically included without needing `--file` flags.

## Examples

```bash
# Edit a file from the image in $EDITOR
contree file edit /etc/nginx/nginx.conf

# Stage a local file at a specific path
contree file cp ./config.yaml /etc/app/config.yaml

# Both edits apply on the next run
contree run nginx -t
```

## Help output

```{terminal-shell} contree file --help
```

## Subcommands

### `file edit`

Downloads a file from the session image, opens it in `$EDITOR` (defaults to
`vi`), and stages the changes if the file was modified. If the file does not
exist in the image, an empty file is created.

### `file cp`

Copies a local file and stages it at the given path inside the image. The
file is uploaded immediately but only applied to the sandbox on the next
`contree run`.

## Pending files

Pending files accumulate until the next `contree run` consumes them.
Explicit `--file` flags on `contree run` take priority over pending files
at the same path.

Files are uploaded with SHA256 dedup -- identical content is not re-uploaded.

## See also

- {doc}`/tutorial/files` -- full tutorial on file injection and editing
- {doc}`run` -- the `--file` syntax for inline file injection
