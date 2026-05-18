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

`contree file edit PATH` (alias `e`) downloads the file at `PATH` from
the session image, opens it in `$EDITOR` (defaults to `vi`), and stages
the modified buffer as a pending upload that will be injected into the
next `contree run`. Missing files are created as empty buffers so the
command doubles as `touch + open`.

```{terminal-shell} contree file edit --help
```

### `file cp`

`contree file cp SRC DEST` (alias `f`) reads a local file at `SRC`, uploads
it to the project's file store, and stages it for delivery at `DEST` inside
the session image on the next `contree run`. Use this when you have a file
ready on disk locally and just want it materialised inside the sandbox
without spawning an instance first.

```{terminal-shell} contree file cp --help
```

### `file ls`

`contree file ls` lists files uploaded to the project (`GET /v1/files`)
and joins each row with the local upload cache. The `SOURCE` column shows
whatever this machine produced the file from:

- absolute host path for files uploaded via `run --file` or `COPY`;
- `https://...` URL for files fetched via `ADD URL`.

:::{important}
`SOURCE` resolves **only for files uploaded from this very machine**.
The mapping lives in the local SQLite cache (per-profile, under
`$CONTREE_HOME/cli/sessions/<profile>.db`) keyed by
`path + inode + mtime + size` (host paths) or by the URL itself (URL
fetches). It is **not** synced anywhere, so a row will show an empty
`SOURCE` whenever:

- the file was uploaded by a different machine, container, or teammate;
- the file was uploaded by an earlier CLI version that did not yet
  track its origin (those entries backfill the next time the file is
  matched by the local cache);
- the host file has been moved, renamed, or its `inode/mtime/size` has
  changed since upload (the cache key no longer matches and the
  mapping is treated as missing until the next upload).

There is no way to recover the source of a file uploaded from another
machine -- the server stores only `uuid`, `sha256`, `size`,
`created_at`, and `updated_at`.
:::

```bash
contree file ls
contree file ls --since 1d --limit 200
contree file ls -q                # uuid + sha256 + source only
contree -f json file ls | jq 'select(.source != "")'
```

```{terminal-shell} contree file ls --help
```

## Pending files

Pending files accumulate until the next `contree run` consumes them.
Explicit `--file` flags on `contree run` take priority over pending files
at the same path.

Files are uploaded with SHA256 dedup -- identical content is not re-uploaded.

## See also

- {doc}`/tutorial/files` -- full tutorial on file injection and editing
- {doc}`run` -- the `--file` syntax for inline file injection
