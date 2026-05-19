# show - Inspect an operation

:::{note}
**`contree show` is a top-level shortcut for {doc}`operation show <operation>` (`contree op show`).**

Both share one argparse setup and one handler. The top-level `show`
accepts one or more UUIDs and history references — each entry renders
as its own row. Accepted reference forms:

- `@`, `:`, or `HEAD` — the operation at the active branch tip.
- `@N`, `:N`, bare `N` — absolute history id.
- `@-N`, `:-N`, `HEAD~N` — N steps back from the tip.
- `HEAD~` — shorthand for `HEAD~1`.
- `@+N`, `:+N` — N steps forward from the tip (latest child).

See the {doc}`operation` page for the full description.
:::

Display the full result of one or more operations, including stdout
and stderr from sandbox execution.

## Examples

```bash
# Show a single operation
contree show 3f2a7b...

# Show multiple operations in one call
contree show 3f2a7b... a1b2c3... 9d8e7f...

# History references (resolved against the active session)
contree show @5 @4 @3
# Relative to the active branch tip (like `session rollback`)
contree show @-1          # the operation one step back from the tip
contree show @+1          # the next operation forward (latest child)
# Git-style HEAD notation, equivalent to @ and @-N
contree show HEAD         # current tip operation
contree show HEAD~        # one step back (shorthand for HEAD~1)
contree show HEAD~3       # three steps back from the tip

# JSON output for scripting
contree -f json show 3f2a7b...

# Show result of a detached run
contree run -d -- make test
contree show UUID
```

## Help output

```{terminal-shell} contree show --help
```

## Output

The command renders every scalar top-level field the API returns
(typically: **uuid**, **kind**, **status**, **created_at**,
**started_at**, **finished_at**, **duration**, **session_key**, …) and
adds these derived fields:

- **exit_code** -- the sandbox process exit code (extracted from
  `metadata.result.state.exit_code`)
- **image** -- resulting image UUID from `result.image`
- **tag** -- image tag from `result.tag`
- **stdout / stderr** -- sandbox output, decoded (for `default`,
  `json`, and `json-pretty` formats)

`status` is the server's word: it reflects whether the API ran the
operation to completion, not whether the sandbox process exited with
zero. A `SUCCESS` row with `exit_code=1` means "the API completed the
job; your command returned 1". `error` is pinned to the last column.
Nested objects (`metadata`, `result`) are dropped from the flat row
— use `--raw` for the full server payload, or `-f json` to keep the
flat structured row.

Pass `--raw` to skip all of the above and print each operation's
full server JSON payload as JSONL (one object per line) to stdout,
verbatim. Streams cleanly into `jq -c`. Useful for debugging or
pulling fields the table view omits (resources, full metadata, etc.).

Timestamps come back from the API in UTC and are converted to the
**local timezone** for human-readable formatters (`default`, `table`,
`csv`, `tsv`, `plain`). The JSON formatters preserve the source
timezone offset.

For `csv`, `tsv`, and `table` formats, stdout/stderr are omitted -- use
`default` or `json` to see sandbox output.

## See also

- {doc}`ps` -- list operations to find UUIDs
- {doc}`operation` -- multi-UUID variant: `contree op show UUID1 UUID2 ...`
- {doc}`run` -- the command that creates operations
