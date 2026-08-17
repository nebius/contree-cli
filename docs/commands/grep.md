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

# 2 lines of context before and after each match, like grep -C 2
contree grep -C 2 ERROR /var/log/app.log

# asymmetric context, like grep -A/-B (--after/--before also work)
contree grep -B 1 -A 3 ERROR /var/log/app.log

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

With `-A`/`-B`/`-C`, context lines use `-` instead of `:` and a `--` line
separates output groups that aren't directly adjacent -- the same
convention GNU grep and ripgrep use:

```
/var/log/app.log-4-starting request
/var/log/app.log:5:ERROR: connection refused
/var/log/app.log-6-retrying
--
/var/log/app.log-40-starting request
/var/log/app.log:41:ERROR: timeout
/var/log/app.log-42-giving up
```

Structured formats (`-o json`, `-o csv`, `-o table`, ...) instead get one
row per match/context line with the columns `path`, `line_number`,
`absolute_offset`, `line_bytes`, `line_text`, `type` (`match` or
`context`).

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

`-B`/`--before-context`/`--before`, `-A`/`--after-context`/`--after`, and
`-C`/`--context` request extra lines of context around each match (server
-side, up to 50 lines each way) -- `-C` sets both directions unless
overridden by an explicit `-A`/`-B`. This mirrors GNU grep/ripgrep's own
flags; `-C` is a deliberate one-off exception to this CLI's usual
uniqueness rule for short flags (`-C` is `--cwd` everywhere else).

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
