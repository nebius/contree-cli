---
icon: binoculars
---

# grep - Search file contents in the image

Search file contents in the session image using server-side ripgrep, without
spawning a sandbox.

## Examples

```bash
# Search the session cwd for a pattern
contree grep TODO

# Search a specific file or directory
contree grep "^PermitRootLogin" /etc/ssh/sshd_config

# Search the whole image (PATH omitted defaults to cwd; pass / for the root)
contree grep TODO /

# Restrict to a glob, limit match count, case-insensitive
contree grep --glob '*.py' --max-count 5 --case insensitive def /app

# JSON output for scripting (one row per match)
contree -o json grep TODO /app
```

## Output

The default format prints classic `PATH:LINE:TEXT` lines, one per match --
the same shape as running `grep -rn` locally:

```
/etc/hosts:1:127.0.0.1 localhost
/etc/hosts:2:255.255.255.255 broadcasthost
```

Structured formats (`-o json`, `-o csv`, `-o table`, ...) instead get one
row per match with the columns `path`, `line_number`, `absolute_offset`,
`line_bytes`, `line_text`.

## Help output

```{terminal-shell} contree grep --help
```

## Behavior

`grep` searches file contents directly in the image filesystem -- no sandbox
is started. Matching is powered by ripgrep with Rust regex syntax and
linear-time matching; symlinks are never followed, hidden files are searched,
and binary files (containing a NUL byte) are skipped.

When PATH is omitted, the search defaults to the session working directory
(set via `cd`), consistent with {doc}`ls` and {doc}`cat`. Pass `/` explicitly
to search the whole image.

Nested match detail (`submatches`) is dropped from structured output; pass
`--raw` to get the full JSON response, including `submatches`, the searched
`patterns`, and the `truncated` flag -- useful for `jq` pipelines.

The command exits with status 1 when no matches are found (like POSIX
`grep`), so it composes in shell conditionals:

```bash
if contree grep ERROR /var/log/app.log > /dev/null; then
  echo "found errors"
fi
```

## See also

- {doc}`ls` -- list files before searching
- {doc}`cat` -- view a single file's full contents
